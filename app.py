import streamlit as st
import numpy as np
import wave, os, sqlite3, tempfile, subprocess, bcrypt
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter
from collections import Counter as VoteCounter

# ================= CONFIG =================
DB_NAME = "guardian_v8.db"
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!'
SAMPLE_RATE = 44100

GAIN_FACTOR = 0.02
SAMPLES_PER_BIT = 256

SYNC_BITS = "10101011110011010011101010101100"  # 32 bits
PAYLOAD_BITS = 128
FRAME_BITS = len(SYNC_BITS) + PAYLOAD_BITS
FRAME_SIZE = FRAME_BITS * SAMPLES_PER_BIT

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY,
        email TEXT,
        phone TEXT,
        password BLOB
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS videos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT,
        uploader TEXT
    )""")
    conn.commit()
    conn.close()

# ================= CRYPTO =================
def encrypt_user_id(uid):
    ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    enc = cipher.encrypt(uid.ljust(16).encode())
    return "".join(format(b, "08b") for b in enc)

def decrypt_user_id(bit_str):
    try:
        data = bytes(int(bit_str[i:i+8], 2) for i in range(0, 128, 8))
        ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(data).decode().strip()
    except:
        return None

# ================= DSSS =================
def get_pn():
    np.random.seed(42)
    return (np.random.randint(0, 2, SAMPLES_PER_BIT) * 2 - 1).astype(np.float32)

def build_watermark_bits(uid):
    return SYNC_BITS + encrypt_user_id(uid)

def embed_audio(audio, uid):
    pn = get_pn()
    bits = build_watermark_bits(uid)
    out = audio.copy()

    for i in range(0, len(audio) - FRAME_SIZE, FRAME_SIZE):
        frame = out[i:i+FRAME_SIZE]
        wm = np.zeros(FRAME_SIZE, dtype=np.float32)

        for b, bit in enumerate(bits):
            start = b * SAMPLES_PER_BIT
            end = start + SAMPLES_PER_BIT
            wm[start:end] = (1 if bit == "1" else -1) * pn

        rms = np.sqrt(np.mean(frame**2)) + 1e-6
        out[i:i+FRAME_SIZE] += GAIN_FACTOR * wm * rms

    return np.clip(out, -32768, 32767).astype(np.int16)

def detect_audio(audio):
    pn = get_pn()
    found_ids = []

    for i in range(0, len(audio) - FRAME_SIZE, SAMPLES_PER_BIT):
        seg = audio[i:i+FRAME_SIZE]
        bits = ""

        for b in range(FRAME_BITS):
            s = seg[b*SAMPLES_PER_BIT:(b+1)*SAMPLES_PER_BIT]
            corr = np.sum(s * pn)
            bits += "1" if corr > 0 else "0"

        if bits.startswith(SYNC_BITS):
            payload = bits[len(SYNC_BITS):len(SYNC_BITS)+128]
            uid = decrypt_user_id(payload)
            if uid:
                found_ids.append(uid)

    return found_ids

# ================= STREAMLIT UI =================
def main():
    st.set_page_config("Guardian Anti-Piracy v8", layout="wide")
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    st.sidebar.title("🛡️ Guardian v8")

    if st.session_state.user:
        page = st.sidebar.radio("Menu", [
            "Home", "Upload Video", "Secure Download",
            "Detect Piracy", "User Database"
        ])
        if st.sidebar.button("Logout"):
            st.session_state.user = None
            st.rerun()
    else:
        page = st.sidebar.radio("Menu", ["Home", "Login", "Register"])

    # -------- HOME --------
    if page == "Home":
        st.title("Guardian Anti-Piracy System")
        st.success("Trim-Invariant DSSS + AES-CTR Watermarking Active")

    # -------- REGISTER --------
    elif page == "Register":
        st.title("Register")
        u = st.text_input("Username")
        e = st.text_input("Email")
        pno = st.text_input("Phone")
        p = st.text_input("Password", type="password")

        if st.button("Create Account"):
            conn = sqlite3.connect(DB_NAME)
            try:
                h = bcrypt.hashpw(p.encode(), bcrypt.gensalt())
                conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, pno, h))
                conn.commit()
                st.success("Account created")
            except:
                st.error("Username exists")
            conn.close()

    # -------- LOGIN --------
    elif page == "Login":
        st.title("Login")
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Login"):
            conn = sqlite3.connect(DB_NAME)
            r = conn.execute("SELECT password FROM users WHERE username=?", (u,)).fetchone()
            if r and bcrypt.checkpw(p.encode(), r[0]):
                st.session_state.user = u
                st.rerun()
            else:
                st.error("Invalid login")
            conn.close()

    # -------- UPLOAD --------
    elif page == "Upload Video":
        st.title("Upload Master Video")
        f = st.file_uploader("MP4", type=["mp4"])
        if f and st.button("Save"):
            path = os.path.join(UPLOAD_DIR, f.name)
            with open(path, "wb") as w:
                w.write(f.read())
            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO videos(filename,uploader) VALUES (?,?)",
                         (f.name, st.session_state.user))
            conn.commit()
            conn.close()
            st.success("Uploaded")

    # -------- DOWNLOAD --------
    elif page == "Secure Download":
        st.title("Generate Secured Video")
        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute("SELECT filename FROM videos").fetchall()
        conn.close()

        for v in vids:
            if st.button(f"Secure {v[0]}"):
                with tempfile.TemporaryDirectory() as tmp:
                    in_v = os.path.join(UPLOAD_DIR, v[0])
                    wav = os.path.join(tmp, "a.wav")
                    out_wav = os.path.join(tmp, "b.wav")
                    out_v = os.path.join(tmp, "final.mp4")

                    subprocess.run([
                        "ffmpeg", "-y", "-i", in_v,
                        "-vn", "-acodec", "pcm_s16le",
                        "-ar", str(SAMPLE_RATE), wav
                    ], capture_output=True)

                    with wave.open(wav, 'rb') as w:
                        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
                        params = w.getparams()

                    secured = embed_audio(audio, st.session_state.user)

                    with wave.open(out_wav, 'wb') as w:
                        w.setparams(params)
                        w.writeframes(secured.tobytes())

                    subprocess.run([
                        "ffmpeg", "-y", "-i", in_v, "-i", out_wav,
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", out_v
                    ], capture_output=True)

                    with open(out_v, "rb") as f:
                        st.download_button("Download Secured Video", f.read(), file_name="secured_"+v[0])

    # -------- DETECT --------
    elif page == "Detect Piracy":
        st.title("Detect Piracy")
        f = st.file_uploader("Upload Suspected Video", type=["mp4"])
        if f and st.button("Scan"):
            with tempfile.TemporaryDirectory() as tmp:
                v = os.path.join(tmp, "x.mp4")
                wav = os.path.join(tmp, "x.wav")
                with open(v, "wb") as w:
                    w.write(f.read())

                subprocess.run([
                    "ffmpeg", "-y", "-i", v,
                    "-vn", "-acodec", "pcm_s16le",
                    "-ar", str(SAMPLE_RATE), wav
                ], capture_output=True)

                with wave.open(wav, 'rb') as w:
                    audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)

                ids = detect_audio(audio)

                if ids:
                    uid = VoteCounter(ids).most_common(1)[0][0]
                    conn = sqlite3.connect(DB_NAME)
                    info = conn.execute(
                        "SELECT email, phone FROM users WHERE username=?",
                        (uid,)
                    ).fetchone()
                    conn.close()

                    st.error(f"🚨 PIRACY DETECTED : {uid}")
                    if info:
                        st.warning(f"Email: {info[0]} | Phone: {info[1]}")
                else:
                    st.success("No piracy signature found")

    # -------- USERS --------
    elif page == "User Database":
        conn = sqlite3.connect(DB_NAME)
        df = pd.read_sql("SELECT username,email,phone FROM users", conn)
        conn.close()
        st.dataframe(df, use_container_width=True)

if __name__ == "__main__":
    main()
