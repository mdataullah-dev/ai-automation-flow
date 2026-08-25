"""Task 2 — skill category tagger.

Reads people who don't have a skill category yet, sends them to a Make.com webhook
(which runs the LLM classification step on Groq), and writes the returned category
back into the database.

The LLM call lives inside the Make scenario, not here -- this script only moves data
in and out of the database. Run it with:

    python automation/tag_skills.py            # tag everyone still untagged
    python automation/tag_skills.py --retag    # clear all tags and re-classify everyone
    python automation/tag_skills.py --sample   # send a 2-person test (no DB writes)
"""
import json
import os
import sqlite3
import sys

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "db", "consultbae.sqlite3")

load_dotenv(os.path.join(BASE_DIR, ".env"))
WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL")

# The four categories the Make/Groq step is allowed to return. Kept here too so we
# never write a value the classifier hallucinated outside the agreed set.
CATEGORIES = {"automation-heavy", "web-dev", "data", "ai-ml"}


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def ensure_column(conn):
    """Add people.skill_category the first time we run. (merge.py rebuilds the table,
    so after re-running the pipeline this column has to be re-created and re-filled.)"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(people)")]
    if "skill_category" not in cols:
        conn.execute("ALTER TABLE people ADD COLUMN skill_category TEXT")
        conn.commit()
        print("added column people.skill_category")


def call_make(input_text):
    """POST the people block to the Make webhook and return the list of results.

    Make relays Groq's raw response, so the body we get back is the full Groq object;
    we dig out choices[0].message.content and parse that. We also accept an already
    unwrapped {"results": [...]} in case the scenario is set to parse the response."""
    resp = requests.post(WEBHOOK_URL, json={"input": input_text}, timeout=180)
    text = resp.text.strip()

    # A Make webhook with no Webhook-Response module (or a scenario that is OFF) just
    # replies "Accepted". Catch that early with a clear message instead of a JSON crash.
    if not text.startswith(("{", "[")):
        die(f"Make replied '{text[:60]}' instead of JSON. "
            "Is module 2 (Groq) + module 3 (Webhook response) built and the scenario switched ON?")

    try:
        data = resp.json()
    except json.JSONDecodeError:
        die(f"Make did not return JSON. First 200 chars:\n{text[:200]}")

    if isinstance(data, dict) and "choices" in data:            # full Groq response
        content = data["choices"][0]["message"]["content"]
        data = json.loads(content) if isinstance(content, str) else content

    if not isinstance(data, dict) or "results" not in data:
        die(f"Unexpected response shape (no 'results' field). Got: {str(data)[:200]}")
    return data["results"]


def run_tagging(retag=False):
    conn = sqlite3.connect(DB_PATH)
    ensure_column(conn)

    if retag:
        n = conn.execute("UPDATE people SET skill_category = NULL").rowcount
        conn.commit()
        print(f"--retag: cleared existing tags on {n} rows")

    rows = conn.execute(
        "SELECT id, skills FROM people "
        "WHERE skill_category IS NULL AND TRIM(COALESCE(skills, '')) <> '' "
        "ORDER BY id"
    ).fetchall()
    if not rows:
        print("Nothing to tag -- every person already has a skill_category. (safe to re-run)")
        conn.close()
        return

    print(f"tagging {len(rows)} people via Make + Groq ...")
    # One single line, people separated by ' ; '. Kept newline-free on purpose: Make injects
    # this straight into a raw JSON body without escaping, and a newline would break that JSON.
    input_text = " ; ".join(f"id={pid} | {skills}" for pid, skills in rows)
    results = call_make(input_text)

    valid_ids = {pid for pid, _ in rows}
    written, skipped = 0, []
    for item in results:
        pid, cat = item.get("id"), (item.get("category") or "").strip()
        if pid not in valid_ids:
            skipped.append((pid, cat, "unknown id"))
        elif cat not in CATEGORIES:
            skipped.append((pid, cat, "unknown category"))
        else:
            conn.execute("UPDATE people SET skill_category = ? WHERE id = ?", (cat, pid))
            written += 1
    conn.commit()

    print(f"wrote {written} tags.")
    if skipped:
        print(f"skipped {len(skipped)} (id, category, reason): {skipped[:5]}")

    still_null = conn.execute(
        "SELECT COUNT(*) FROM people "
        "WHERE skill_category IS NULL AND TRIM(COALESCE(skills, '')) <> ''"
    ).fetchone()[0]
    if still_null:
        print(f"{still_null} taggable people still untagged -- re-run to fill them.")

    print("\ncategory distribution:")
    for cat, n in conn.execute(
        "SELECT skill_category, COUNT(*) FROM people "
        "WHERE skill_category IS NOT NULL GROUP BY skill_category ORDER BY 2 DESC"
    ):
        print(f"  {cat:<16} {n}")
    conn.close()


def run_sample():
    """Send a tiny 2-person payload and print whatever comes back. No DB writes.
    Use this to teach the listening webhook its schema and to debug the scenario."""
    sample = "id=1 | LangChain, MongoDB, n8n, REST APIs, SQL ; id=2 | Docker, JavaScript, Zapier"
    print("sending a 2-person sample to the webhook ...")
    resp = requests.post(WEBHOOK_URL, json={"input": sample}, timeout=180)
    print(f"HTTP {resp.status_code}")
    print("response body:\n" + resp.text[:500])


if __name__ == "__main__":
    if not WEBHOOK_URL:
        die("MAKE_WEBHOOK_URL is missing from .env")
    if "--sample" in sys.argv:
        run_sample()
    else:
        run_tagging(retag="--retag" in sys.argv)
