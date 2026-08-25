# ConsultBae — Data Merge Pipeline

Three CSV exports from three different systems (Naukri recruitment applicants, gig workers, and CBNexus
contacts) describe an overlapping set of people — but no single ID is shared across all three files.
This project ingests all three, resolves the duplicates, and produces **one clean SQLite database with
56 unique people**, one row per real person.

It also documents every data-quality problem planted in the source files and how each one is handled
(Task 4). On top of that, a no-code Make.com + Groq automation tags each person with a skill category and
writes it back (Task 2).

**Pipeline at a glance:** `105 raw rows -> 103 staged -> 60 clusters -> 56 people`

## Contents

1. [Setup and run](#setup-and-run)
2. [How the merge works](#how-the-merge-works)
3. [Database schema](#database-schema)
4. [Data issues report](#data-issues-report)
5. [Skill tagging automation (Task 2)](#skill-tagging-automation-task-2)
6. [Stuck log](#stuck-log)

---

## Setup and run

Requires **Python 3.10+**.

```bash
# 1. Clone
git clone https://github.com/mdataullah-dev/ai-automation-flow.git
cd ai-automation-flow

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows (PowerShell)
# source .venv/bin/activate         # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the pipeline
python pipeline/merge.py
```

The pipeline reads the three CSVs from `data/`, merges them, and writes the result to
`db/consultbae.sqlite3`. It also prints a summary of every data issue it detected.

**Expected output:**

```
Ingesting and cleaning raw data...
  staged 103 rows after structural cleanup
Resolving entities and merging duplicates...
  60 clusters after exact email/phone matching
  56 people after name+city matching
Saving clean golden records to SQLite database...
Success! 56 unique people saved to ...\db\consultbae.sqlite3
```

---

## How the merge works

No column is common to all three files: source 1 has email and phone, source 2 has email only, source 3
has phone only. People are matched in two passes.

**Pass 1 — exact identifiers.** Two records are the same person if they share an exact (normalised) email
or phone. This links transitively: a source-1 record that has both an email and a phone connects a
source-2 record (matched on its email) and a source-3 record (matched on its phone) into a single person.

**Pass 2 — name and city, guarded.** Four people appear only in the email-only and phone-only files, so
they share no identifier and can be linked only by name and city. This merge happens **only when the two
records have no conflicting evidence** — one contributes an email, the other a phone. If two records share
a name and city but carry *different* phone numbers, they are kept as different people.

**Why 56 and not 55?** Three different "Arjun Mehta" records exist in Noida with conflicting phone
numbers. Their identity cannot be inferred safely, so the pipeline does not merge them — it keeps them
separate and flags the case for review instead of inventing a person who does not exist. The full
explanation is in [the ambiguous case](#the-ambiguous-case-arjun-mehta) below.

Fuzzy string matching is deliberately **not** used; [the note below](#why-not-fuzzy-matching) shows why it
cannot work on this data.

---

## Database schema

One flat table, `people`, with one row per person:

| Column | Source | Notes |
|--------|--------|-------|
| `id` | — | primary key |
| `name`, `email`, `phone`, `city` | all | golden values; `email` and `phone` are `UNIQUE` |
| `skills` | all | de-duplicated union of every skill across the person's rows |
| `sources` | — | which files the person appeared in |
| `experience_years`, `ctc_annual`, `applied_date` | Naukri | |
| `gig_status`, `rate_amount`, `rate_unit` | Gig workers | |
| `verified`, `projects` | CBNexus | |
| `skill_category` | Task 2 | LLM-assigned skill category (added by the tagger) |

---

## Data issues report

The source files are deliberately messy. Rather than fix problems by hand, the pipeline **detects and
logs each one as it runs** and prints a summary at the end, so this report always matches what the code
actually does. Running `python pipeline/merge.py` reproduces every line number quoted below.

**Total: 23 issue instances across 14 distinct types.**

### Structural problems (broken rows)

| Where | Problem | Fix |
|-------|---------|-----|
| source2, line 20 | **Shifted row** — every column moved one place to the right; the name "Isha Chopra" ended up in the `rate` column | Detected (email sitting in the wrong column) and realigned to recover every field |
| source2, line 12 | **Blank row** (six empty fields) | Dropped |
| source3, line 16 | **Repeated header** row pasted into the middle of the data | Dropped |

### Inconsistent formats (same value, many encodings)

| Field | Problem | Fix |
|-------|---------|-----|
| Phone | 6 formats: `+91…`, `0…`, bare 10-digit, `91…`, `+91-…` | Strip non-digits, drop the country code / leading zero, keep the last 10 digits |
| City | 17 spellings of 5 cities, incl. trailing spaces (`Noida `) and renames (Gurgaon = Gurugram, Bangalore = Bengaluru) | Strip whitespace, then map to a canonical set of 5 |
| Applied date | 4 formats; dashes are day-first, slashes are month-first (8 values are ambiguous without this rule) | Parse by delimiter, normalise to ISO `YYYY-MM-DD` |
| CTC | mixes absolute rupees and lakhs in one column | Values below 100 are treated as lakhs and scaled up; stored as annual INR |
| Rate | mixes `NNNN/hr` and `NNk/month` | Amount and period stored separately (no invented conversion) |
| Email | about one third of source-2 emails are UPPERCASE | Lower-cased before matching |
| Verified | `Y` / `yes` / `Yes` / `N` / `No` | Normalised to `1` / `0` |

### Duplicates and identity

- **Duplicate inside one file:** `R. Verma` and `Rohit Verma` (source 1) share one email and one phone — an
  abbreviated-name duplicate, caught by exact identifiers. The full name is kept.
- **One person, two emails:** `Nikhil Chopra` has two emails on the same phone. Both are kept in the
  cluster; the cleaner one is chosen as primary and the duplicate is flagged, never dropped silently.
- **Cross-file matches:** four people (`Manish Bhatia`, `Divya Chopra`, `Karan Chopra`, `Vikram Mehta`)
  exist only in the email-only and phone-only files and are correctly merged on name and city.

### The ambiguous case (Arjun Mehta)

There are clearly at least two distinct Arjun Mehtas in Noida:

| Record | Email | Phone | Sources |
|--------|-------|-------|---------|
| A | `arjun.mehta9@…` | `9000000131` | Naukri + CBNexus (both agree) |
| B? | `arjun.mehta77@…` | — | Gig workers |
| C? | — | `9000000272` | CBNexus |

Record A is certain. Records B and C share only name and city — but that bucket is already proven **not**
unique (A is in it too), so B and C could be one person or two, and the data cannot tell which. The
pipeline refuses to guess: it keeps them separate and flags the case. This is why the final count is
**56, not 55** — the first draft merged all three and produced a record with one person's email next to
another person's phone.

### Why not fuzzy matching

The first attempt matched names with `fuzz.ratio > 85`. Measured on the real data it is impossible to
tune correctly:

| Pair | Score | Correct action |
|------|-------|----------------|
| `R. Verma` vs `Rohit Verma` | 77.8 | **merge** (same person) |
| `Arjun Mehta` vs `Arjun Mishra` | 78.3 | **do not merge** (different people) |

No threshold keeps the first and rejects the second, so fuzzy matching was removed entirely (along with
the `thefuzz` dependency). Matching relies on exact identifiers plus the guarded name-and-city rule above.

---

## Skill tagging automation (Task 2)

A no-code Make.com automation that uses an LLM to tag each person with a skill category and write it back
to the database. **The LLM step runs inside Make, not in Python.**

**Flow:** `tag_skills.py` reads untagged people from the DB → POSTs them to a Make **webhook** → Make's
**HTTP module** calls **Groq** (`gpt-oss-120b`) to classify → Make returns the JSON → `tag_skills.py`
writes `skill_category` back.

Make is kept as a **dumb relay** — it holds the prompt and calls Groq, while Python does all the parsing
and database writes (the reasons are in the stuck log). The scenario is exported to
[`automation/make_scenario.blueprint.json`](automation/make_scenario.blueprint.json), importable into any
Make account with the Groq key scrubbed.

**Categories:** `automation-heavy`, `web-dev`, `data`, `ai-ml` — each person gets the group that holds the
most of their skills.

**Run it** (after the pipeline, with `GROQ_API_KEY` and `MAKE_WEBHOOK_URL` set in `.env` — see
`.env.example`):

```bash
python automation/tag_skills.py          # tag people not yet tagged
python automation/tag_skills.py --retag  # clear all tags and re-classify everyone
```

**Result** — 55 of 56 tagged (Arjun Mehta id 56 has no skills; CBNexus carries none):

```
web-dev  20    data  19    ai-ml  9    automation-heavy  7
```

---

## Stuck log

*The hardest places I got stuck, and exactly how I got out.*

### 1. Groq kept failing on the full batch of 55 people

- **Where I got stuck:** the Make scenario only said "Scenario failed to complete" — no detail. It
  classified 2 test people fine, but the real run of 55 failed every single time.
- **How I got unstuck:** I took the Groq call out of Make and ran it directly from Python to see the real
  error — `json_validate_failed`, with an empty response. That gave me something to search.
- **What I searched:** "groq json_validate_failed", and why a reasoning model returns empty JSON on a
  large request.
- **What I asked AI:** why Groq fails JSON validation only on the big batch when 2 people work fine. The
  answer: `gpt-oss-120b` is a reasoning model, and on 55 people at once it spends its whole token budget
  "thinking" before it writes the JSON, so nothing valid comes out.
- **What I rejected, and why:** my first fix was to raise `max_completion_tokens` to 8000 — that hit a
  "request too large" rate-limit wall on the free tier, so I dropped it. I used `reasoning_effort: "low"`
  with `max_completion_tokens: 3000` instead: enough room for the answer, safely under the limit.

### 2. Make.com JSON parser breaking on newlines & rejecting UI-heavy logic

- **Where I got stuck:** When sending raw JSON data to Make.com, the newlines (`\n`) in the payload
  completely broke Make's HTTP module parser. The flow kept failing because Make couldn't handle the raw
  JSON structure properly in the HTTP request body.
- **What I searched:** How to correctly pass and parse JSON arrays in Make.com webhooks without breaking
  the HTTP payload.
- **What I asked AI:** I asked the Gemini LLM for suggestions on how to fix this JSON array parsing issue
  natively inside Make.com.
- **What I rejected, and why:** Gemini suggested a "Make-native" design: using built-in Iterators to loop
  through the JSON array and parse it inside the Make UI. I completely rejected this suggestion. Doing
  complex array manipulation inside a no-code UI is brittle, hard to debug, and over-engineers the flow.
- **How I got unstuck (the defensive programming move):** Instead of fighting Make's UI, I took a
  'defensive programming' approach. I pre-processed the data in Python, converting all records into a
  single-line string (`id=1 | skills ; id=2 | skills`) to guarantee no newline breaks. I decided to treat
  Make.com purely as a "dumb relay" and handle all the robust JSON parsing back in Python, where it's much
  safer and errors are actually readable.

### 3. The tagging ran, but the result was clearly wrong

- **Where I got stuck:** the tagger happily wrote all 55 tags — but 44 of them came back
  `automation-heavy`. 80% of people in one bucket is obviously wrong.
- **How I got unstuck:** I queried the database and read the actual people. The model was tagging anyone
  with a *single* automation tool (one Zapier) as automation-heavy, even people with five web-dev or data
  skills.
- **What I searched:** how to make an LLM classify by the *dominant* group instead of any single match,
  and prompt patterns for balanced classification.
- **What I asked AI:** why the model over-tagged automation, and how to word the prompt so it weighs the
  whole skill mix.
- **What I rejected, and why:** I rejected accepting the working-but-skewed output, and I rejected simply
  raising the reasoning effort (that brings back the JSON crash from #1). Instead I rewrote the prompt to
  count each person's skills per group and pick the biggest, with an explicit "one automation tool does
  not make you automation-heavy" rule and a tie-break order. The spread balanced out: web-dev 20, data 19,
  ai-ml 9, automation-heavy 7.

<!-- I will add more entries as I build Tasks 3 and 5. -->
