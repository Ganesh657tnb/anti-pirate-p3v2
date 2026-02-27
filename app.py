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

AES_KEY = b"SixteenByteKey!!"      # 16 bytes
AES_PREFIX = b"GUARDIAN"           # 8 bytes

SAMPLE_RATE = 44100
BLOCK_SEC = 0.25
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_SEC)

SYNC_INTERVAL_SEC = 5
SYNC_INTERVAL_BLOCKS = int(SYNC_INTERVAL_SEC / BLOCK_SEC)

BASE_GAIN = 0.02
MIN_GAIN = 0.008
SYNC_GAIN = 0.05

REDUNDANCY = 3
CORR_THRESHOLD = 0.15

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

# ================= PN =================
def generate_pn(n, seed):
    rng = np.random.RandomState(seed)
    return rng.choice([-1, 1], size=n).astype(np.float64)

PN_DATA = generate_pn(BLOCK_SIZE, 42)
PN_SYNC = generate_pn(BLOCK_SIZE, 999)

# ================= AES =================
def encrypt_uid(uid: int):
    msg = str(uid).ljust(16)[:16].encode()
    ctr = Counter.new(64, prefix=AES_PREFIX, initial_value=1)
    cipher = AES.new(AES_KEY, AES.MODE_CTR, counter=ctr)
    enc = cipher.encrypt(msg)
    return [int(b) for byte in enc for b in f"{byte:08b}"]

def decrypt_bits(bits):
    raw = bytearray()
    for i in range(0, len(bits), 8):
        raw.append(int("".join(map(str, bits[i:i+8])), 2))
    ctr = Counter.new(64, prefix=AES_PREFIX, initial_value=1)
    cipher = AES.new(AES_KEY, AES.MODE_CTR, counter=ctr)
    try:
        return int(cipher.decrypt(bytes(raw)).decode().strip())
    except:
        return None

# ================= PSYCHOACOUSTIC =================
def adaptive_gain(block):
    rms = np.sqrt(np.mean(block ** 2))
    return max(MIN_GAIN, BASE_GAIN * rms)

# ================= EMBED =================
def embed_watermark(in_wav, out_wav, uid):
    with wave.open(in_wav, 'rb') as w:
        params = w.getparams()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)

    bits = encrypt_uid(uid) * REDUNDANCY
    bit_idx = 0

    out = audio.copy()
    total_blocks = len(audio) // BLOCK_SIZE

    for b in range(total_blocks):
        i = b * BLOCK_SIZE
        block = audio[i:i+BLOCK_SIZE]

        # SYNC block
        if b % SYNC_INTERVAL_BLOCKS == 0:
            out[i:i+BLOCK_SIZE] += SYNC_GAIN * PN_SYNC * np.max(np.abs(block))
            continue

        if bit_idx >= len(bits):
            break

        sign = 1 if bits[bit_idx] == 1 else -1
        bit_idx += 1

        gain = adaptive_gain(block)
        out[i:i+BLOCK_SIZE] += sign * gain * PN_DATA

    out = np.clip(out, -32768, 32767).astype(np.int16)

    with wave.open(out_wav, 'wb') as w:
        w.setparams(params)
        w.writeframes(out.tobytes())

# ================= DETECT =================
def detect_watermark(wav_path):
    with wave.open(wav_path, 'rb') as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)

    total_blocks = len(audio) // BLOCK_SIZE
    data_bits = []

    for b in range(total_blocks):
        i = b * BLOCK_SIZE
        block = audio[i:i+BLOCK_SIZE]

        # Detect sync
        sync_corr = np.dot(block, PN_SYNC) / (np.linalg.norm(block) * np.linalg.norm(PN_SYNC) + 1e-9)
        if abs(sync_corr) > 0.4:
            continue  # skip sync blocks

        corr = np.dot(block, PN_DATA) / (np.linalg.norm(block) * np.linalg.norm(PN_DATA) + 1e-9)

        if abs(corr) < CORR_THRESHOLD:
            continue

        data_bits.append(1 if corr > 0 else 0)

    if len(data_bits) < 128 * REDUNDANCY:
        return None

    # Redundancy voting
    final_bits = []
    for i in range(0, len(data_bits), REDUNDANCY):
        chunk = data_bits[i:i+REDUNDANCY]
        final_bits.append(1 if sum(chunk) > len(chunk)//2 else 0)

    return decrypt_bits(final_bits[:128])

# ================= FFMPEG =================
def run_ffmpeg(cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

# ================= APP =================
def main():
    st.set_page_config("Anti-Piracy Portal", layout="wide")
    init_db()

    if "uid" not in st.session_state:
        st.session_state.uid = None

    # -------- LOGIN --------
    if st.session_state.uid is None:
        c1, c2 = st.columns(2)

        with c1:
            st.subheader("Login")
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            if st.button("Login"):
                conn = sqlite3.connect(DB_NAME)
                row = conn.execute("SELECT id,password FROM users WHERE username=?", (u,)).fetchone()
                conn.close()
                if row and bcrypt.checkpw(p.encode(), row[1]):
                    st.session_state.uid = row[0]
                    st.rerun()
                st.error("Invalid login")

        with c2:
            st.subheader("Register")
            ru = st.text_input("Username", key="r1")
            re = st.text_input("Email")
            rp = st.text_input("Phone")
            rpw = st.text_input("Password", type="password", key="r2")
            if st.button("Register"):
                h = bcrypt.hashpw(rpw.encode(), bcrypt.gensalt())
                try:
                    conn = sqlite3.connect(DB_NAME)
                    conn.execute(
                        "INSERT INTO users(username,email,phone,password) VALUES(?,?,?,?)",
                        (ru, re, rp, h)
                    )
                    conn.commit()
                    conn.close()
                    st.success("Registered")
                except:
                    st.error("Username exists")
        st.stop()

    st.sidebar.success(f"UID {st.session_state.uid}")
    if st.sidebar.button("Logout"):
        st.session_state.uid = None
        st.rerun()

    tab1, tab2, tab3, tab4 = st.tabs(["📚 Library", "📤 Upload", "🔍 Detector", "🗄 Database"])

    # -------- LIBRARY --------
    with tab1:
        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute("SELECT filename FROM videos").fetchall()
        conn.close()

        for i, (fname,) in enumerate(vids):
            if st.button(f"Download {fname}", key=f"d{i}"):
                with tempfile.TemporaryDirectory() as tmp:
                    in_v = os.path.join(UPLOAD_DIR, fname)
                    wav1 = os.path.join(tmp, "a.wav")
                    wav2 = os.path.join(tmp, "b.wav")
                    out_v = os.path.join(tmp, "out.mp4")

                    run_ffmpeg([
                        "ffmpeg","-y","-i",in_v,
                        "-vn","-ac","1","-ar","44100",
                        "-acodec","pcm_s16le", wav1
                    ])
                    embed_watermark(wav1, wav2, st.session_state.uid)
                    run_ffmpeg([
                        "ffmpeg","-y","-i",in_v,"-i",wav2,
                        "-map","0:v:0","-map","1:a:0",
                        "-c:v","copy","-c:a","aac", out_v
                    ])

                    with open(out_v,"rb") as f:
                        st.download_button("Download", f.read(), f"protected_{fname}")

    # -------- UPLOAD --------
    with tab2:
        f = st.file_uploader("Upload video", type=["mp4","mkv","mov"])
        if f and st.button("Upload"):
            path = os.path.join(UPLOAD_DIR, f.name)
            with open(path,"wb") as out:
                out.write(f.read())
            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO videos(filename,uploader_id) VALUES(?,?)", (f.name, st.session_state.uid))
            conn.commit()
            conn.close()
            st.success("Uploaded")

    # -------- DETECTOR --------
    with tab3:
        leak = st.file_uploader("Upload leaked video", type=["mp4","mkv","mov"])
        if leak:
            with tempfile.TemporaryDirectory() as tmp:
                vid = os.path.join(tmp, "x.mp4")
                wav = os.path.join(tmp, "x.wav")
                with open(vid,"wb") as f:
                    f.write(leak.read())

                run_ffmpeg([
                    "ffmpeg","-y","-i",vid,
                    "-vn","-ac","1","-ar","44100",
                    "-acodec","pcm_s16le", wav
                ])

                uid = detect_watermark(wav)
                if uid is not None:
                    st.success(f"🚨 Piracy detected – User ID: {uid}")
                else:
                    st.error("No watermark detected")

    # -------- DATABASE --------
    with tab4:
        conn = sqlite3.connect(DB_NAME)
        st.dataframe(pd.read_sql("SELECT id,username,email,phone FROM users", conn))
        st.dataframe(pd.read_sql("SELECT * FROM videos", conn))
        conn.close()

if __name__ == "__main__":
    main()
