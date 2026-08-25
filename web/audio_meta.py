"""Audio metadata extraction for the Task 3 collection app.

Uses the ffmpeg binary bundled by `imageio-ffmpeg` (no system install needed) plus numpy:
- `probe()`   reads container metadata (sample rate, bitrate, codec) from ffmpeg's info banner.
- `analyse()` decodes the audio to raw samples and computes duration, loudness, and a noise estimate.

ffmpeg decodes every common format (wav, mp3, m4a, ogg, flac, and the webm/opus the browser records),
so both in-browser recordings and uploaded files go through the same path.

Run it directly on a file:  python web/audio_meta.py path/to/clip.wav
"""
import math
import re
import subprocess
import sys

import imageio_ffmpeg
import numpy as np

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _db(amplitude):
    """Convert a 0..1 amplitude to decibels, guarding against log(0)."""
    return 20 * math.log10(amplitude) if amplitude > 1e-9 else -120.0


def probe(path):
    """Read container metadata from ffmpeg's info banner (which it prints to stderr)."""
    banner = subprocess.run([FFMPEG, "-i", path], capture_output=True, text=True).stderr  # noqa: PLW1510
    meta = {"codec": None, "sample_rate": None, "channels": None, "bitrate_kbps": None}
    m = re.search(r"bitrate: (\d+) kb/s", banner)
    if m:
        meta["bitrate_kbps"] = int(m.group(1))
    m = re.search(r"Audio: (\w+).*?, (\d+) Hz, (\w+)", banner)
    if m:
        meta["codec"], meta["sample_rate"], meta["channels"] = m.group(1), int(m.group(2)), m.group(3)
    return meta


def _decode_pcm(path, sample_rate):
    """Decode any audio file to mono 16-bit PCM at `sample_rate`, as floats in [-1, 1]."""
    raw = subprocess.run(  # noqa: PLW1510
        [FFMPEG, "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "-"],
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def analyse(path):
    """Extract every stored metric for one audio file and return them as a dict:
    duration_s, sample_rate_khz, bitrate_kbps, loudness_dbfs, peak_dbfs,
    noise_floor_dbfs, snr_db, quality."""
    meta = probe(path)
    sample_rate = meta["sample_rate"] or 44100
    x = _decode_pcm(path, sample_rate)
    if x.size == 0:
        raise ValueError("Could not decode any audio from this file.")

    # Duration straight from the sample count -- exact, and independent of the header.
    duration = x.size / sample_rate
    rms = float(np.sqrt(np.mean(x ** 2)))          # overall loudness
    peak = float(np.max(np.abs(x)))                # loudest single sample

    # Noise floor = the quietest 10% of 20 ms frames (on speech, that's the gaps between
    # words). SNR is how far the overall level sits above that floor -> a rough quality score.
    frame = max(1, int(sample_rate * 0.02))
    usable = x[: x.size - (x.size % frame)]
    frame_rms = (np.sqrt(np.mean(usable.reshape(-1, frame) ** 2, axis=1))
                 if usable.size else np.array([rms]))
    nonzero = frame_rms[frame_rms > 0]
    noise = float(np.percentile(nonzero, 10)) if nonzero.size else 0.0
    snr = _db(rms) - _db(noise)
    quality = "good" if snr > 20 else "fair" if snr > 10 else "poor"

    return {
        "duration_s": round(duration, 2),
        "sample_rate_khz": round(sample_rate / 1000, 1),
        "bitrate_kbps": meta["bitrate_kbps"],
        "loudness_dbfs": round(_db(rms), 1),
        "peak_dbfs": round(_db(peak), 1),
        "noise_floor_dbfs": round(_db(noise), 1),
        "snr_db": round(snr, 1),
        "quality": quality,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python web/audio_meta.py <audio-file>")
        sys.exit(1)
    for key, value in analyse(sys.argv[1]).items():
        print(f"  {key:<16} {value}")
