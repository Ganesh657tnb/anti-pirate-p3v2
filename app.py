import os
import sqlite3
import tempfile
import subprocess
import numpy as np
import wave
import bcrypt
import streamlit as st
import pandas as pd

from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# ================= CONFIG =================
DB_NAME = "guardian.db"
UPLOAD_DIR = "master_videos"

AES_KEY = b'SixteenByteKey!!'      # AES-128
AES_PREFIX = b'GUARD'

BASE_GAIN = 0.012                 # base watermark gain
SYNC_GAIN = 0.02                  # stronger sync marker
BLOCK_SEC = 0.25                  # time-based embedding (seconds)
SYNC_INTERVAL_SEC = 5             # sync marker every 5 seconds

SAMPLE_RATE = 44100

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            email TEXT,
            phone TEXT,
            password BLOB
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS videos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            uploader_id INTEGER
        )
    """)
    conn.commit()
    conn.close()

# ================= DSSS CORE =================
def generate_pn(n, seed):
    rng = np.random.RandomState(seed)
    return rng.choice([-1, 1], size=n).astype(np.float64)

# ================= AES ENCRYPT =================
def encrypt_user_id(uid: int) -> list:
    msg = str(uid).ljust(16)[:16].encode()
    ctr = Counter.new(64, prefix=AES_PREFIX, initial_value=1)
    cipher = AES.new(AES_KEY, AES.MODE_CTR, counter=ctr)
    enc = cipher.encrypt(msg)
    return [int(b) for byte in enc for b in f"{byte:08b}"]

# ================= PSYCHOACOUSTIC GAIN =================
def adaptive_gain(block):
    rms = np.sqrt(np.mean(block**2))
    gain = BASE_GAIN * rms
    return np.clip(gain, 0.001, 0.03)

# ================= WATERMARK + SYNC EMBED =================
def embed_watermark(in_wav, out_wav, user_id):
    with wave.open(in_wav, 'rb') as w:
        params = w.getparams()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)

    bits = encrypt_user_id(user_id)
    bit_idx = 0

    block_size = int(SAMPLE_RATE * BLOCK_SEC)
    sync_interval = int(SAMPLE_RATE * SYNC_INTERVAL_SEC)

    pn_data = generate_pn(len(audio), seed=42)
    pn_sync = generate_pn(block_size, seed=999)

    out = audio.copy()

    for i in range(0, len(audio) - block_size, block_size):
        block = audio[i:i+block_size]

        # 🔹 SYNC MARKER
        if i % sync_interval == 0:
            out[i:i+block_size] += SYNC_GAIN * pn_sync * np.max(np.abs(block))
            continue

        if bit_idx >= len(bits):
            break

        bit = bits[bit_idx]
        bit_idx += 1

        gain = adaptive_gain(block)
        sign = 1 if bit == 1 else -1

        out[i:i+block_size] += sign * gain * pn_data[i:i+block_size]

    out = np.clip(out, -32768, 32767).astype(np.int16)

    with wave.open(out_wav, 'wb') as w:
        w.setparams(params)
        w.writeframes(out.tobytes())

# ================= TRIM-RESISTANT DETECTOR =================
def detect_watermark(wav_path):
    with wave.open(wav_path, 'rb') as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)

    block_size = int(SAMPLE_RATE * BLOCK_SEC)
    pn_data = generate_pn(len(audio), seed=42)
    pn_sync = generate_pn(block_size, seed=999)

    detected_bits = []

    for i in range(0, len(audio) - block_size, block_size):
        block = audio[i:i+block_size]

        # 🔍 SYNC SEARCH
        sync_corr = np.dot(block, pn_sync)
        if abs(sync_corr) > 0.3 * np.linalg.norm(block):
            detected_bits.clear()
            continue

        corr = np.dot(block, pn_data[i:i+block_size])
        detected_bits.append(1 if corr > 0 else 0)

    # 🗳 MAJORITY VOTING
    final_bits = []
    for i in range(0, len(detected_bits), 3):
        chunk = detected_bits[i:i+3]
        if len(chunk) == 3:
            final_bits.append(1 if sum(chunk) >= 2 else 0)

    return final_bits

# ================= FFMPEG =================
def run_ffmpeg(cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

# ================= STREAMLIT APP =================
def main():
    st.set_page_config("Anti-Piracy Portal", layout="wide")
    init_db()

    if "uid" not in st.session_state:
        st.session_state.uid = None

    # ---------- LOGIN / REGISTER ----------
    if st.session_state.uid is None:
        c1, c2 = st.columns(2)

        with c1:
            st.subheader("Login")
            u = st.text_input("Username", key="lu")
            p = st.text_input("Password", type="password", key="lp")

            if st.button("Login", key="login"):
                conn = sqlite3.connect(DB_NAME)
                row = conn.execute(
                    "SELECT id,password FROM users WHERE username=?",(u,)
                ).fetchone()
                conn.close()

                if row:
                    stored = row[1]
                    if isinstance(stored, memoryview):
                        stored = stored.tobytes()
                    if bcrypt.checkpw(p.encode(), stored):
                        st.session_state.uid = row[0]
                        st.rerun()

                st.error("Invalid login")

        with c2:
            st.subheader("Register")
            ru = st.text_input("Username", key="ru")
            re = st.text_input("Email")
            rp = st.text_input("Phone")
            rpw = st.text_input("Password", type="password", key="rpw")

            if st.button("Register", key="reg"):
                h = bcrypt.hashpw(rpw.encode(), bcrypt.gensalt())
                try:
                    conn = sqlite3.connect(DB_NAME)
                    conn.execute(
                        "INSERT INTO users(username,email,phone,password) VALUES(?,?,?,?)",
                        (ru,re,rp,h)
                    )
                    conn.commit()
                    conn.close()
                    st.success("Registered successfully")
                except:
                    st.error("Username exists")

        st.stop()

    # ---------- DASHBOARD ----------
    st.sidebar.success(f"User ID: {st.session_state.uid}")
    if st.sidebar.button("Logout"):
        st.session_state.uid = None
        st.rerun()

    tab1, tab2 = st.tabs(["📚 Library", "📤 Upload"])

    # ---------- LIBRARY ----------
    with tab1:
        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute("SELECT filename FROM videos").fetchall()
        conn.close()

        for (fname,) in vids:
            if st.button(f"Secure & Download – {fname}", key=f"dl_{fname}"):
                with tempfile.TemporaryDirectory() as tmp:
                    in_v = os.path.join(UPLOAD_DIR, fname)
                    wav1 = os.path.join(tmp, "a.wav")
                    wav2 = os.path.join(tmp, "b.wav")
                    out_v = os.path.join(tmp, "out.mp4")

                    run_ffmpeg(["ffmpeg","-y","-i",in_v,"-vn","-acodec","pcm_s16le","-ar","44100",wav1])
                    embed_watermark(wav1, wav2, st.session_state.uid)
                    run_ffmpeg(["ffmpeg","-y","-i",in_v,"-i",wav2,"-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac",out_v])

                    with open(out_v,"rb") as f:
                        st.download_button("Download", f.read(), file_name=f"protected_{fname}")

    # ---------- UPLOAD ----------
    with tab2:
        up = st.file_uploader("Upload master video", type=["mp4","mkv","mov"])
        if up and st.button("Upload", key="upload"):
            path = os.path.join(UPLOAD_DIR, up.name)
            with open(path,"wb") as f:
                f.write(up.read())

            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO videos(filename,uploader_id) VALUES(?,?)",(up.name,st.session_state.uid))
            conn.commit()
            conn.close()
            st.success("Uploaded")

if __name__ == "__main__":
    main()
