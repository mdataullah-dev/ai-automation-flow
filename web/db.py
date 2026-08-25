"""Database layer for the Task 3 audio app.

Submissions live in a NEW `audio_submissions` table inside the same `db/consultbae.sqlite3` built by
Task 1. It is a separate table, so re-running `pipeline/merge.py` (which rebuilds `people`) never touches
submissions. Each submission links to one of the 56 people when its phone matches -- using the exact same
`clean_phone` normalisation as the merge, so the app matches whoever the merge would have.
"""
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "db", "consultbae.sqlite3")

# Reuse the merge pipeline's phone normaliser (repo root must be importable when Streamlit runs web/).
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from pipeline.merge import clean_phone


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_audio_table(conn)
    return conn


def ensure_audio_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_submissions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id        INTEGER REFERENCES people(id),   -- linked by phone; NULL if no match
            name             TEXT NOT NULL,
            phone            TEXT,                             -- normalised, same rule as the merge
            audio_path       TEXT NOT NULL,                   -- path relative to web/
            mime_type        TEXT,
            duration_s       REAL,
            sample_rate_khz  REAL,
            bitrate_kbps     INTEGER,
            loudness_dbfs    REAL,
            peak_dbfs        REAL,
            noise_floor_dbfs REAL,
            snr_db           REAL,
            quality          TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def resolve_person(conn, phone):
    """Look up a Task-1 person by phone. Returns (person_id, person_name), or (None, None) if there is
    no match (or the pipeline has not been run yet, so `people` doesn't exist)."""
    norm = clean_phone(phone)
    if not norm:
        return None, None
    try:
        row = conn.execute("SELECT id, name FROM people WHERE phone = ?", (norm,)).fetchone()
    except sqlite3.OperationalError:
        return None, None
    return (row["id"], row["name"]) if row else (None, None)


def insert_submission(conn, name, phone, audio_path, mime_type, metrics, person_id):
    conn.execute("""
        INSERT INTO audio_submissions
            (person_id, name, phone, audio_path, mime_type,
             duration_s, sample_rate_khz, bitrate_kbps, loudness_dbfs, peak_dbfs,
             noise_floor_dbfs, snr_db, quality)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (person_id, name, clean_phone(phone), audio_path, mime_type,
          metrics["duration_s"], metrics["sample_rate_khz"], metrics["bitrate_kbps"],
          metrics["loudness_dbfs"], metrics["peak_dbfs"], metrics["noise_floor_dbfs"],
          metrics["snr_db"], metrics["quality"]))
    conn.commit()


def list_submissions(conn):
    """All submissions, newest first, with the linked person's name when there is one."""
    return conn.execute("""
        SELECT s.*, p.name AS linked_person
        FROM audio_submissions s
        LEFT JOIN people p ON p.id = s.person_id
        ORDER BY s.id DESC
    """).fetchall()
