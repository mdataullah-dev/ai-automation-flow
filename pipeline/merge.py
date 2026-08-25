import os
import re
import sqlite3
from collections import Counter, defaultdict

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')

# ==========================================================================
# DATA-QUALITY LOG
# Every planted problem we detect gets recorded here. This is what powers the
# "Data Issues Report" for Task 4 -- we generate it from the pipeline instead
# of writing it by hand, so it can never drift out of sync with the code.
# ==========================================================================
ISSUES = []
def log_issue(source, kind, detail):
    ISSUES.append({"source": source, "kind": kind, "detail": detail})


# ==========================================================================
# 1. CLEANING FUNCTIONS
# ==========================================================================
def clean_phone(phone):
    """Normalise every phone to a bare 10-digit national number.

    The three files use SIX different phone formats between them:
    '+919000000254', '9000000254', '09000000254', '919000000254',
    '+91-9000000131'. If we don't normalise, the same person's phone from two
    files never matches and the merge silently fails. Strategy: throw away every
    non-digit, drop a leading '91' country code, keep the last 10 digits.
    """
    if phone is None:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]          # 919000000254 -> 9000000254
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]          # 09000000254  -> 9000000254
    return digits if len(digits) == 10 else None


def clean_email(email):
    """Lower-case and trim. ~1/3 of source-2 emails are UPPERCASE
    (e.g. 'ISHA.CHOPRA95@MAILTEST.EXAMPLE.ORG'); without folding case they would
    look like different addresses and break email-based matching."""
    if email is None:
        return None
    email = str(email).strip().lower()
    return email or None


def clean_name(name):
    """Trim + Title-case so 'RITU SHARMA' and 'Ritu Sharma' collapse together."""
    if name is None:
        return ""
    return " ".join(str(name).split()).title()


def name_key(name):
    """A comparable key for name matching: lower-case, punctuation stripped,
    whitespace collapsed. 'R. Verma' -> 'r verma', 'Ritu  Sharma' -> 'ritu sharma'."""
    if not name:
        return ""
    key = re.sub(r"[^a-z ]", " ", str(name).lower())
    return " ".join(key.split())


# City normalisation. The raw data has 17 spellings of 5 real cities, including
# case noise ('NOIDA'/'noida'), TRAILING SPACES ('Noida ', 'gurugram '), the
# 'Delhi'/'New Delhi'/'Delhi NCR' family, and two genuine city renames
# (Gurgaon==Gurugram, Bangalore==Bengaluru). We MUST strip before mapping --
# the trailing space on source-3's 'Noida ' is exactly what split Manish Bhatia
# into two records in the first-pass code.
CITY_CANON = {
    "gurgaon": "Gurgaon", "gurugram": "Gurgaon",
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru",
    "delhi": "Delhi", "new delhi": "Delhi", "delhi ncr": "Delhi",
    "noida": "Noida",
    "pune": "Pune",
}
def clean_city(city):
    if city is None:
        return None
    raw = str(city).strip()
    if not raw:
        return None
    return CITY_CANON.get(raw.lower(), raw.title())


def clean_skills(skills):
    """Split a skills string into a normalised set. Source-1 writes
    'React, JavaScript, MySQL' and source-2 writes 'react, javascript, mysql' for
    the SAME person -- lower-casing before de-duping stops us storing each skill
    twice when we merge them."""
    if not skills:
        return set()
    return {s.strip().lower() for s in str(skills).split(",") if s.strip()}


# Skill display map: turn our lowercase keys back into presentable names.
# Plain .title() would mangle 'n8n' -> 'N8N' and 'rest apis' -> 'Rest Apis',
# so a few special cases are spelled out.
SKILL_DISPLAY = {
    "n8n": "n8n", "rest apis": "REST APIs", "fastapi": "FastAPI",
    "sql": "SQL", "mysql": "MySQL", "mongodb": "MongoDB", "javascript": "JavaScript",
    "langchain": "LangChain",
}
def display_skill(key):
    return SKILL_DISPLAY.get(key, key.title())


def parse_ctc(value):
    """Source-1 'Current CTC' mixes two units in one column: absolute rupees
    (e.g. 417964) and lakhs-per-annum (e.g. 4.2 meaning 4.2 lakh = 420000).
    Anything under 100 is clearly lakhs, so we scale it up to rupees."""
    if not value:
        return None
    try:
        num = float(value)
    except ValueError:
        return None
    return int(num * 100000) if num < 100 else int(num)


def parse_rate(value):
    """Source-2 'rate' mixes '1415/hr' and '15k/month'. We keep the amount and
    the period separately rather than pretending we can convert hours to months."""
    if not value:
        return (None, None)
    v = str(value).strip().lower()
    m = re.match(r"([\d.]+)\s*k?/(hr|month)", v)
    if not m:
        return (None, None)
    amount = float(m.group(1))
    if "k" in v.split("/")[0]:
        amount *= 1000
    return (amount, "hour" if m.group(2) == "hr" else "month")


MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
def parse_date(value):
    """Source-1 'Applied Date' hides FOUR formats in one column. The delimiter
    tells us the convention: dashes are day-first (24-07-2026 = 24 Jul, and 24>12
    proves it), slashes are month-first (07/13/2026 = 13 Jul, 13>12 proves it).
    Feeding this column to a naive date parser silently corrupts the 8 dates
    where both halves are <=12. We normalise everything to ISO yyyy-mm-dd."""
    if not value:
        return None
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):                 # already ISO
        return s
    if re.fullmatch(r"\d{2}-\d{2}-\d{4}", s):                 # dash = DAY first
        d, m, y = s.split("-")
        return f"{y}-{m}-{d}"
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", s):                 # slash = MONTH first
        m, d, y = s.split("/")
        return f"{y}-{m}-{d}"
    m = re.fullmatch(r"(\d{1,2}) (\w{3}) (\d{4})", s)         # '7 Jul 2026'
    if m and m.group(2).lower() in MONTHS:
        d, mon, y = m.groups()
        return f"{y}-{MONTHS[mon.lower()]:02d}-{int(d):02d}"
    return s


# ==========================================================================
# 2. INGESTION  (each source -> a list of plain record dicts)
# ==========================================================================
def _read_csv(name):
    # dtype=str + keep_default_na=False so pandas never coerces '09000000287' into
    # an int (losing the leading zero) or turns blanks into NaN floats.
    return pd.read_csv(os.path.join(DATA_DIR, name), dtype=str, keep_default_na=False)


def ingest_source1():
    df = _read_csv("source1_naukri_applicants.csv")
    log_issue("source1", "mixed_phone_formats", "+91.., 0.., and bare-10 all present; normalised to 10 digits")
    log_issue("source1", "mixed_date_formats", "4 date formats in 'Applied Date'; normalised via delimiter convention")
    log_issue("source1", "mixed_ctc_units", "'Current CTC' mixes absolute rupees and lakhs; normalised to annual INR")

    # Catch identity duplicates INSIDE this one file before we even merge across
    # files: e.g. 'R. Verma' and 'Rohit Verma' share one email+phone, and
    # 'Nikhil Chopra' appears twice on one phone with two different emails.
    seen_email, seen_phone = {}, {}
    records = []
    for _, r in df.iterrows():
        email = clean_email(r["Email"])
        phone = clean_phone(r["Phone"])
        if email in seen_email:
            log_issue("source1", "duplicate_identity", f"email {email} appears as '{seen_email[email]}' and '{r['Full Name']}'")
        if phone and phone in seen_phone:
            log_issue("source1", "duplicate_identity", f"phone {phone} appears as '{seen_phone[phone]}' and '{r['Full Name']}'")
        seen_email[email] = r["Full Name"]
        if phone:
            seen_phone[phone] = r["Full Name"]
        records.append({
            "source": "Naukri",
            "name": clean_name(r["Full Name"]),
            "email": email,
            "phone": phone,
            "city": clean_city(r["City"]),
            "skills": clean_skills(r["Skills"]),
            "experience_years": float(r["Experience (Years)"]) if r["Experience (Years)"] else None,
            "ctc_annual": parse_ctc(r["Current CTC"]),
            "applied_date": parse_date(r["Applied Date"]),
            "gig_status": None, "rate_amount": None, "rate_unit": None,
            "verified": None, "projects": None,
        })
    return records


def ingest_source2():
    df = _read_csv("source2_gig_workers.csv")
    log_issue("source2", "uppercase_emails", "~1/3 of emails are UPPERCASE; folded to lowercase")
    log_issue("source2", "mixed_rate_units", "'rate' mixes NNNN/hr and NNk/month; amount + period stored separately")
    records = []
    for i, r in df.iterrows():
        line = i + 2  # +1 for header, +1 for 0-based index -> physical CSV line
        email_id = r["email_id"]; worker_name = r["worker_name"]
        rate = r["rate"]; location = r["location"]
        status = r["status"]; skill_tags = r["skill_tags"]

        # PLANTED BUG: a fully blank row (source-2 line 12). Skip it.
        if not any(str(x).strip() for x in [email_id, worker_name, rate, location, status, skill_tags]):
            log_issue("source2", "blank_row", f"line {line}: empty row dropped")
            continue

        # PLANTED BUG: a ROTATED row (source-2 line 20). Every value is shifted one
        # column to the right and the last wrapped around to the front, so the email
        # sits in 'worker_name' and the real name 'Isha Chopra' hides in 'rate'.
        # The tell-tale: the email_id cell has no '@' but worker_name does. We rotate
        # the row back one step to the left to recover every field.
        if "@" not in str(email_id) and "@" in str(worker_name):
            # the real name sits in the 'rate' column of the rotated row
            log_issue("source2", "rotated_row", f"line {line}: columns shifted right by one; realigned to recover '{clean_name(rate)}'")
            email_id, worker_name, rate, location, status, skill_tags = (
                worker_name, rate, location, status, skill_tags, email_id
            )

        rate_amount, rate_unit = parse_rate(rate)
        records.append({
            "source": "GigWorkers",
            "name": clean_name(worker_name),
            "email": clean_email(email_id),
            "phone": None,                      # source-2 has no phone column at all
            "city": clean_city(location),
            "skills": clean_skills(skill_tags),
            "experience_years": None, "ctc_annual": None, "applied_date": None,
            "gig_status": str(status).strip().lower() or None,
            "rate_amount": rate_amount, "rate_unit": rate_unit,
            "verified": None, "projects": None,
        })
    return records


def ingest_source3():
    df = _read_csv("source3_cbnexus_contacts.csv")
    log_issue("source3", "mixed_phone_formats", "+91-.., 91.., and bare-10 all present; normalised to 10 digits")
    log_issue("source3", "mixed_verified_values", "'Verified' uses Y/yes/Yes/N/No; normalised to 0/1")
    records = []
    for i, r in df.iterrows():
        line = i + 2
        # PLANTED BUG: the header row is repeated in the middle of the data
        # (source-3 line 16). Drop any row whose Name literally equals 'Name'.
        if str(r["Name"]).strip() == "Name":
            log_issue("source3", "repeated_header", f"line {line}: header row embedded in data dropped")
            continue
        raw_city = str(r["City"])
        if raw_city != raw_city.strip():
            log_issue("source3", "city_trailing_space", f"line {line}: city {raw_city!r} had stray whitespace")
        verified = str(r["Verified"]).strip().lower() in ("y", "yes", "true", "1")
        records.append({
            "source": "CBNexus",
            "name": clean_name(r["Name"]),
            "email": None,                      # source-3 has no email column
            "phone": clean_phone(r["Phone Number"]),
            "city": clean_city(r["City"]),
            "skills": set(),
            "experience_years": None, "ctc_annual": None, "applied_date": None,
            "gig_status": None, "rate_amount": None, "rate_unit": None,
            "verified": 1 if verified else 0,
            "projects": int(r["Projects Completed"]) if str(r["Projects Completed"]).strip().isdigit() else None,
        })
    return records


def ingest_all():
    print("Ingesting and cleaning raw data...")
    records = ingest_source1() + ingest_source2() + ingest_source3()
    print(f"  staged {len(records)} rows after structural cleanup")
    return records


# ==========================================================================
# 3. ENTITY RESOLUTION  (no fuzzy matching -- see note below)
# ==========================================================================
# NOTE ON FUZZY MATCHING: the first draft matched names with fuzz.ratio > 85.
# It cannot work on this data. 'r verma' vs 'rohit verma' scores 77.8 (a REQUIRED
# merge) while 'arjun mehta' vs 'arjun mishra' scores 78.3 (two DIFFERENT people).
# No threshold separates them, so any fuzzy rule either misses real matches or
# invents fake ones. We drop it entirely and match on exact identifiers instead.

def cluster_by_identifiers(records):
    """Pass 1 -- STRONG match. Two records are the same person if they share an
    exact email or an exact phone. We group them by walking each record and
    looking it up in an email index and a phone index; when a record bridges two
    existing groups (its email is in one, its phone in another) we fuse them.
    This is transitive and order-independent, and it needs no fuzzy guessing."""
    clusters = {}                 # cid -> {records, emails, phones}
    email_idx, phone_idx = {}, {}
    next_id = 0
    for rec in records:
        e, p = rec["email"], rec["phone"]
        hits = []
        if e and e in email_idx:
            hits.append(email_idx[e])
        if p and p in phone_idx:
            hits.append(phone_idx[p])
        hits = list(dict.fromkeys(hits))       # de-dupe, keep order

        if not hits:
            cid = next_id; next_id += 1
            clusters[cid] = {"records": [], "emails": set(), "phones": set()}
            target = cid
        else:
            target = hits[0]
            for other in hits[1:]:             # fuse any bridged clusters into target
                clusters[target]["records"] += clusters[other]["records"]
                clusters[target]["emails"] |= clusters[other]["emails"]
                clusters[target]["phones"] |= clusters[other]["phones"]
                for em in clusters[other]["emails"]:
                    email_idx[em] = target
                for ph in clusters[other]["phones"]:
                    phone_idx[ph] = target
                del clusters[other]

        clusters[target]["records"].append(rec)
        if e:
            clusters[target]["emails"].add(e); email_idx[e] = target
        if p:
            clusters[target]["phones"].add(p); phone_idx[p] = target
    return list(clusters.values())


def _compatible(c1, c2):
    """Two clusters may be merged on name+city ONLY if their identifier kinds are
    disjoint -- one brings an email, the other a phone. If both hold a phone (or
    both hold an email) and they're still separate clusters, the values must
    DIFFER, which means they are different people. This single rule is the veto
    that stops the two Arjun Mehtas (phones ...131 and ...272) from fusing."""
    both_email = bool(c1["emails"]) and bool(c2["emails"])
    both_phone = bool(c1["phones"]) and bool(c2["phones"])
    return not both_email and not both_phone


def merge_by_name_city(clusters):
    """Pass 2 -- WEAK match. Source-2 (email only) and source-3 (phone only) share
    no identifier field, so four people are ONLY linkable by name+city
    (Divya Chopra, Karan Chopra, Manish Bhatia, Vikram Mehta). We block clusters
    by (name_key, city) and merge a block only if EVERY pair in it is compatible.
    A block that mixes compatible and conflicting pairs (the Arjun Mehta case) is
    genuinely ambiguous, so we merge none of it and flag it for review."""
    # Give each cluster a single (name_key, city) signature = its most common one.
    for c in clusters:
        sigs = Counter(
            (name_key(r["name"]), r["city"])
            for r in c["records"] if name_key(r["name"]) and r["city"]
        )
        c["sig"] = sigs.most_common(1)[0][0] if sigs else None

    blocks = defaultdict(list)
    for idx, c in enumerate(clusters):
        if c["sig"]:
            blocks[c["sig"]].append(idx)

    absorbed = [False] * len(clusters)
    for sig, idxs in blocks.items():
        idxs = [i for i in idxs if not absorbed[i]]
        if len(idxs) < 2:
            continue
        # every pair in the block must be compatible for the whole block to merge
        all_ok = all(
            _compatible(clusters[a], clusters[b])
            for k, a in enumerate(idxs) for b in idxs[k + 1:]
        )
        if not all_ok:
            log_issue("cross-source", "ambiguous_identity",
                      f"name+city block {sig} has conflicting identifiers ({len(idxs)} records); left unmerged for review")
            continue
        base = idxs[0]
        for other in idxs[1:]:
            clusters[base]["records"] += clusters[other]["records"]
            clusters[base]["emails"] |= clusters[other]["emails"]
            clusters[base]["phones"] |= clusters[other]["phones"]
            absorbed[other] = True
        log_issue("cross-source", "cross_file_match", f"merged {sig} across files via name+city")

    return [c for i, c in enumerate(clusters) if not absorbed[i]]


def build_golden(cluster):
    """Collapse one cluster's records into a single golden record, choosing the
    best value for each field and never silently throwing conflicting data away."""
    recs = cluster["records"]

    # NAME: prefer a variant with no single-letter initials ('Rohit Verma' over
    # 'R. Verma'), then a mixed-case one ('Ritu Sharma' over 'RITU SHARMA').
    def name_score(n):
        toks = n.split()
        no_initials = all(len(t) > 1 for t in toks)
        mixed_case = n != n.upper()
        return (no_initials, mixed_case, len(n))
    names = [r["name"] for r in recs if r["name"]]
    best_name = max(names, key=name_score) if names else ""

    emails = sorted(cluster["emails"])
    phones = sorted(cluster["phones"])
    if len(emails) > 1:
        # Nikhil Chopra: one phone, two emails ('nikhil.chopra70@' + 'alt....').
        log_issue("cross-source", "same_person_multiple_emails",
                  f"{best_name} has {len(emails)} emails: {', '.join(emails)}")
    # primary email = the shortest one (drops the 'alt.' prefix duplicate); every
    # value was still kept together in the cluster, so nothing is lost silently.
    primary_email = min(emails, key=len) if emails else None
    primary_phone = phones[0] if phones else None

    cities = [r["city"] for r in recs if r["city"]]
    city = Counter(cities).most_common(1)[0][0] if cities else None

    skills = set().union(*[r["skills"] for r in recs]) if recs else set()
    skills_display = ", ".join(display_skill(s) for s in sorted(skills))

    sources = ", ".join(sorted({r["source"] for r in recs}))

    def first(field):
        for r in recs:
            if r.get(field) is not None:
                return r[field]
        return None

    return {
        "name": best_name,
        "email": primary_email,
        "phone": primary_phone,
        "city": city,
        "skills": skills_display,
        "sources": sources,
        "experience_years": first("experience_years"),
        "ctc_annual": first("ctc_annual"),
        "applied_date": first("applied_date"),
        "gig_status": first("gig_status"),
        "rate_amount": first("rate_amount"),
        "rate_unit": first("rate_unit"),
        "verified": first("verified"),
        "projects": first("projects"),
    }


def resolve_entities(records):
    print("Resolving entities and merging duplicates...")
    clusters = cluster_by_identifiers(records)
    print(f"  {len(clusters)} clusters after exact email/phone matching")
    clusters = merge_by_name_city(clusters)
    print(f"  {len(clusters)} people after name+city matching")
    return [build_golden(c) for c in clusters]


# ==========================================================================
# 4. DATABASE EXPORT
# ==========================================================================
def save_to_db(people):
    print("Saving clean golden records to SQLite database...")
    db_dir = os.path.join(BASE_DIR, "db")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "consultbae.sqlite3")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Build the table ourselves and INSERT into it. The first draft called
    # to_sql(if_exists='replace'), which DROPS this table and recreates it from
    # the DataFrame -- silently deleting the id column and the UNIQUE constraints.
    cur.execute("DROP TABLE IF EXISTS people")
    cur.execute("""
        CREATE TABLE people (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT,
            email            TEXT UNIQUE,
            phone            TEXT UNIQUE,
            city             TEXT,
            skills           TEXT,
            sources          TEXT,
            experience_years REAL,
            ctc_annual       INTEGER,
            applied_date     TEXT,
            gig_status       TEXT,
            rate_amount      REAL,
            rate_unit        TEXT,
            verified         INTEGER,
            projects         INTEGER
        )
    """)
    cols = ["name", "email", "phone", "city", "skills", "sources",
            "experience_years", "ctc_annual", "applied_date",
            "gig_status", "rate_amount", "rate_unit", "verified", "projects"]
    cur.executemany(
        f"INSERT INTO people ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [[p[c] for c in cols] for p in people],
    )
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM people")
    count = cur.fetchone()[0]
    conn.close()
    print(f"Success! {count} unique people saved to {db_path}")


def print_issue_report():
    print("\n=== DATA ISSUES DETECTED (Task 4 source) ===")
    for kind, n in Counter(i["kind"] for i in ISSUES).most_common():
        print(f"  {kind:<28} x{n}")


if __name__ == "__main__":
    records = ingest_all()
    people = resolve_entities(records)
    save_to_db(people)
    print_issue_report()