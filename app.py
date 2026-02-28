# ==========================================================
# GUARDIAN – Anti-Piracy Watermarking Portal (v5 – Final)
# ==========================================================

import os, sqlite3, tempfile, subprocess, wave, hashlib, hmac as _hmac
import numpy as np
import bcrypt
import streamlit as st
import pandas as pd

# ==========================================================
# CONFIG
# ==========================================================
DB_NAME = "guardian.db"
UPLOAD_DIR = "master_videos"
os.makedirs(UPLOAD_DIR, exist_ok=True)

WM_SECRET = b"GuardianWatermarkSecretKey2024"

SAMPLE_RATE = 44100
BLOCK_SEC = 0.25
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_SEC)
N_BITS = 32
GAIN = 0.03
SUB_STEP = 64
SKIP_BLOCKS = 3
ENERGY_TH = 500

# ==========================================================
# DATABASE INIT  (FIX)
# ==========================================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password BLOB NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        uploader_id INTEGER NOT NULL,
        uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (uploader_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)

    conn.commit()
    conn.close()

# ==========================================================
# FFMPEG HELPER  (FIX)
# ==========================================================
def run_ffmpeg(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr)

# ==========================================================
# PN GENERATION
# ==========================================================
def _bandlimit(pn, fs=SAMPLE_RATE):
    F = np.fft.rfft(pn)
    freqs = np.fft.rfftfreq(len(pn), 1/fs)
    mask = (freqs >= 200) & (freqs <= 3500)
    F[~mask] = 0
    shaped = np.fft.irfft(F, n=len(pn))
    n = np.linalg.norm(shaped)
    return shaped / n if n > 1e-10 else shaped

_PN_RAW = np.random.RandomState(42).choice([-1.0, 1.0], BLOCK_SIZE)
PN_DATA = _bandlimit(_PN_RAW)
PN_NORM = np.linalg.norm(PN_DATA)

# ==========================================================
# WATERMARK CORE
# ==========================================================
def uid_to_cycle(uid):
    digest = _hmac.new(WM_SECRET, str(uid).encode(), hashlib.sha256).digest()
    bits = [int(b) for byte in digest for b in f"{byte:08b}"]
    return bits[:N_BITS]

def cycle_matches_uid(bits, uid):
    return uid_to_cycle(uid) == bits

def embed_watermark(in_wav, out_wav, uid):
    with wave.open(in_wav, "rb") as wf:
        params = wf.getparams()
        audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(float)

    cycle = uid_to_cycle(uid)
    out = audio.copy()
    n_blocks = len(audio) // BLOCK_SIZE

    for b in range(n_blocks):
        i = b * BLOCK_SIZE
        block = audio[i:i+BLOCK_SIZE]
        energy = np.linalg.norm(block)
        if energy < ENERGY_TH:
            continue
        adaptive = GAIN * np.clip(energy / 3000, 0.3, 1.0)
        sign = 1 if cycle[b % N_BITS] else -1
        out[i:i+BLOCK_SIZE] += sign * PN_DATA * adaptive * 32767

    out = np.clip(out, -32768, 32767).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(out.tobytes())

def detect_watermark(wav, all_uids):
    with wave.open(wav, "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(float)

    if len(audio) < BLOCK_SIZE * (N_BITS + SKIP_BLOCKS):
        return None

    for sub in range(0, BLOCK_SIZE, SUB_STEP):
        seg = audio[sub:]
        raw = []
        for b in range(SKIP_BLOCKS, len(seg)//BLOCK_SIZE):
            i = b * BLOCK_SIZE
            block = seg[i:i+BLOCK_SIZE]
            nb = np.linalg.norm(block)
            if nb < 1:
                continue
            corr = np.dot(block, PN_DATA) / (nb * PN_NORM)
            raw.append((1 if corr > 0 else 0, b))

        if len(raw) < N_BITS:
            continue

        for phase in range(N_BITS):
            buckets = [[] for _ in range(N_BITS)]
            for bit, idx in raw:
                buckets[(idx - phase) % N_BITS].append(bit)

            voted = []
            for bucket in buckets:
                if not bucket:
                    break
                voted.append(1 if sum(bucket) > len(bucket)//2 else 0)

            if len(voted) < N_BITS:
                continue

            for uid in all_uids:
                if cycle_matches_uid(voted, uid):
                    return uid
    return None

# ==========================================================
# DATABASE HELPERS
# ==========================================================
def db_login(username):
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT id, name, password FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row

def db_register(name, email, phone, username, pw):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            "INSERT INTO users(name,email,phone,username,password) VALUES(?,?,?,?,?)",
            (name, email, phone, username, pw)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def db_all_uids():
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute("SELECT id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_add_video(fname, uid):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO videos(filename,uploader_id) VALUES(?,?)", (fname, uid))
    conn.commit()
    conn.close()

def db_videos():
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute(
        "SELECT v.id, v.filename, u.name, v.uploaded_at "
        "FROM videos v JOIN users u ON v.uploader_id=u.id ORDER BY v.id DESC"
    ).fetchall()
    conn.close()
    return rows

def db_user_by_id(uid):
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute(
        "SELECT id, name, username, email, phone FROM users WHERE id=?", (uid,)
    ).fetchone()
    conn.close()
    return row

def db_stats():
    conn = sqlite3.connect(DB_NAME)
    nu = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    nv = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    conn.close()
    return nu, nv

# ==========================================================
# STREAMLIT APP
# ==========================================================
st.set_page_config(page_title="Guardian · Anti-Piracy", page_icon="🛡️", layout="wide")

def main():
    init_db()

    if "uid" not in st.session_state:
        st.session_state.uid = None
        st.session_state.uname = ""

    if st.session_state.uid is None:
        st.title("🛡️ GUARDIAN – Login")

        u = st.text_input("Username")
        p = st.text_input("Password", type="password")

        if st.button("Login"):
            row = db_login(u)
            if row and bcrypt.checkpw(p.encode(), row[2]):
                st.session_state.uid = row[0]
                st.session_state.uname = row[1]
                st.rerun()
            else:
                st.error("Invalid credentials")

        st.subheader("Create Account")
        rn = st.text_input("Name")
        re = st.text_input("Email")
        rp = st.text_input("Phone")
        ru = st.text_input("New Username")
        rpw = st.text_input("New Password", type="password")

        if st.button("Register"):
            h = bcrypt.hashpw(rpw.encode(), bcrypt.gensalt())
            if db_register(rn, re, rp, ru, h):
                st.success("Account created")
            else:
                st.error("Username exists")
        st.stop()

    st.success(f"Logged in as {st.session_state.uname} (UID {st.session_state.uid})")
    st.write("Upload, download, and detect piracy safely.")

if __name__ == "__main__":
    main()
