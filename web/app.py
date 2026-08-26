"""Task 3 — mini audio collection app (Streamlit).

Two tabs:
  * Submit          — name + phone, record in the browser OR upload a file; the clip is stored, its
                      audio properties are extracted, and a row is written to the Task-1 database.
  * All submissions — every submission with a play button and its extracted properties.

Run:  streamlit run web/app.py   (run pipeline/merge.py first so the `people` table exists to link to)
"""
import os
import uuid

import audio_meta
import streamlit as st

import db

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(WEB_DIR, "audio_files")
os.makedirs(AUDIO_DIR, exist_ok=True)

MIME_EXT = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3",
            "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/ogg": "ogg", "audio/webm": "webm",
            "audio/flac": "flac"}


def file_ext(audio):
    """Pick a file extension for the uploaded/recorded clip from its name or MIME type."""
    name = getattr(audio, "name", "") or ""
    if "." in name:
        return name.rsplit(".", 1)[1].lower()
    return MIME_EXT.get(getattr(audio, "type", ""), "wav")


def show_metrics(metrics):
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Duration", f"{metrics['duration_s']} s")
    c2.metric("Sample rate", f"{metrics['sample_rate_khz']} kHz")
    c3.metric("Bitrate", f"{metrics['bitrate_kbps']} kbps" if metrics["bitrate_kbps"] else "n/a")
    c4.metric("Loudness", f"{metrics['loudness_dbfs']} dB")
    st.caption(f"noise floor {metrics['noise_floor_dbfs']} dB  ·  SNR {metrics['snr_db']} dB  ·  "
               f"quality: **{metrics['quality']}**")


st.set_page_config(page_title="ConsultBae Audio Collection", page_icon="🎙")
st.title("🎙 ConsultBae — Audio Collection")

conn = db.connect()
tab_submit, tab_list = st.tabs(["Submit", "All submissions"])

# ---------------------------------------------------------------- Submit
with tab_submit:
    st.subheader("Submit a recording")

    # Result of the previous submission, shown until the next one replaces it.
    result = st.session_state.get("last_result")
    if result:
        st.success("Submitted and saved to the database.")
        if result["person_id"]:
            st.info(f"Matched an existing person from Task 1: "
                    f"**{result['person_name']}** (#{result['person_id']})")
        else:
            st.caption("New submitter (phone not in the Task-1 data).")
        st.markdown("**Extracted audio properties**")
        show_metrics(result["metrics"])
        st.divider()

    # Widgets are keyed by a round counter. After each submit we bump the counter, so the inputs come
    # back as brand-new widgets (empty) instead of being cleared in place -- which is what made the
    # audio recorder throw "An error has occurred". Fresh widget = clean recorder, no reload needed.
    r = st.session_state.get("round", 0)
    name = st.text_input("Name", key=f"name_{r}")
    phone = st.text_input("Phone number", key=f"phone_{r}")
    st.markdown("Record in the browser **or** upload a file:")
    recorded = st.audio_input("Record audio", key=f"rec_{r}")
    uploaded = st.file_uploader("...or upload a file",
                                type=["wav", "mp3", "m4a", "ogg", "webm", "flac"], key=f"up_{r}")

    if st.button("Submit", type="primary"):
        audio = recorded or uploaded
        if not name.strip():
            st.error("Please enter your name.")
        elif audio is None:
            st.error("Please record or upload an audio clip first.")
        else:
            fname = f"{uuid.uuid4().hex}.{file_ext(audio)}"
            path = os.path.join(AUDIO_DIR, fname)
            with open(path, "wb") as f:
                f.write(audio.getvalue())

            try:
                metrics = audio_meta.analyse(path)
            except Exception as exc:  # noqa: BLE001
                os.remove(path)
                st.error(f"Could not analyse the audio: {exc}")
                st.stop()

            person_id, person_name = db.resolve_person(conn, phone)
            db.insert_submission(conn, name.strip(), phone, f"audio_files/{fname}",
                                 getattr(audio, "type", None), metrics, person_id)

            # Stash the result, hand the widgets fresh keys, and rerun to a clean form.
            st.session_state["last_result"] = {"person_id": person_id, "person_name": person_name,
                                               "metrics": metrics}
            st.session_state["round"] = r + 1
            st.rerun()

# ---------------------------------------------------------------- All submissions
with tab_list:
    st.subheader("All submissions")
    rows = db.list_submissions(conn)
    if not rows:
        st.info("No submissions yet — add one in the Submit tab.")
    else:
        st.caption(f"{len(rows)} submission(s)")
        for row in rows:
            with st.container(border=True):
                header = f"**{row['name']}**"
                if row["phone"]:
                    header += f"  ·  📞 {row['phone']}"
                if row["linked_person"]:
                    header += f"  ·  🔗 linked to {row['linked_person']} (#{row['person_id']})"
                st.markdown(header)

                audio_path = os.path.join(WEB_DIR, row["audio_path"])
                if os.path.exists(audio_path):
                    st.audio(audio_path)
                else:
                    st.caption("_(audio file missing)_")

                st.caption(
                    f"⏱ {row['duration_s']} s  ·  🎚 {row['sample_rate_khz']} kHz  ·  "
                    f"{row['bitrate_kbps']} kbps  ·  🔊 {row['loudness_dbfs']} dB  ·  "
                    f"quality: {row['quality']}  ·  {row['created_at']}"
                )
