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
SECRET_KEY = b"SixteenByteKey!!"

GAIN_FACTOR = 0.035          # AAC survive panna
SAMPLES_PER_BIT = 256

SYNC_BITS = "10101011110011010011101010101100"  # 32 bits
PAYLOAD_BITS = 128
FRAME_BITS = len(SYNC_BITS) + PAYLOAD_BITS
FRAME_SIZE = FRAME_BITS * SAMPLES_PER_BIT

STEP = FRAME_SIZE // 3       # sliding window step

# =========================================

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            email TEXT,
            phone TEXT,
            password BLOB
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            uploader TEXT
        )
    """)
    conn.commit()
    conn.close()

# ================= PN SEQUENCE =================
def get_pn_sequence():
    np.random.seed(42)  # MUST be constant
    return (np.random.randint(0, 2, SAMPLES_PER_BIT) * 2 - 1).astype(np.float32)

PN_SEQ = get_pn_sequence()

# ================= CRYPTO =================
def get_watermark_bits(user_id):
    ctr = Counter.new(64, prefix=b"GUARDIAN", initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    padded = user_id.ljust(16).encode("utf-8")
    encrypted = cipher.encrypt(padded)
    payload_bits = "".join(format(b, "08b") for b in encrypted)
    return SYNC_BITS + payload_bits

def decrypt_payload(bit_str):
    try:
        byte_data = bytes(
            int(bit_str[i:i+8], 2) for i in range(0, PAYLOAD_BITS, 8)
        )
        ctr = Counter.new(64, prefix=b"GUARDIAN", initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(byte_data).decode("utf-8").strip()
    except:
        return None

# ================= EMBEDDING =================
def embed_frame(audio, bits):
    wm = np.zeros(FRAME_SIZE, dtype=np.float32)

    for i, bit in enumerate(bits):
        start = i * SAMPLES_PER_BIT
        end = start + SAMPLES_PER_BIT
        val = 1 if bit == "1" else -1
        wm[start:end] = val * PN_SEQ

    rms = np.sqrt(np.mean(audio[:FRAME_SIZE] ** 2)) + 1e-6
    audio[:FRAME_SIZE] += GAIN_FACTOR * wm * rms
    return audio

# ================= DETECTION =================
def detect_watermark(audio):
    recovered_ids = []

    for i in range(0, len(audio) - FRAME_SIZE, STEP):
        seg = audio[i:i + FRAME_SIZE]

        bits = ""
        for b in range(FRAME_BITS):
            s = seg[b*SAMPLES_PER_BIT:(b+1)*SAMPLES_PER_BIT]
            corr = np.sum(s * PN_SEQ)
            bits += "1" if corr > 0 else "0"

        if bits.startswith(SYNC_BITS):
            payload = bits[len(SYNC_BITS):len(SYNC_BITS)+PAYLOAD_BITS]
            uid = decrypt_payload(payload)
            if uid:
                recovered_ids.append(uid)

    return recovered_ids

# ================= UI =================
def main():
    st.set_page_config("Guardian v8 Anti-Piracy", layout="wide")
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    st.sidebar.title("🛡️ GUARDIAN v8")

    if st.session_state.user:
        nav = st.sidebar.radio(
            "Navigation",
            ["Home", "Upload Video", "Download Secured Video", "Detect Piracy", "User Database"]
        )
        if st.sidebar.button("Logout"):
            st.session_state.user = None
            st.rerun()
    else:
        nav = st.sidebar.radio("Navigation", ["Home", "Login", "Register"])

    # ================= HOME =================
    if nav == "Home":
        st.title("Guardian v8 – Robust DSSS Watermarking")
        st.markdown("""
        **Features**
        - AES-128-CTR encrypted user identity  
        - DSSS audio watermarking  
        - Sync-based trim-invariant detection  
        - Majority voting  
        """)

    # ================= REGISTER =================
    elif nav == "Register":
        st.subheader("Create Account")
        u = st.text_input("Username")
        e = st.text_input("Email")
        ph = st.text_input("Phone")
        p = st.text_input("Password", type="password")

        if st.button("Register"):
            conn = sqlite3.connect(DB_NAME)
            try:
                h = bcrypt.hashpw(p.encode(), bcrypt.gensalt())
                conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, ph, h))
                conn.commit()
                st.success("Account created")
            except:
                st.error("Username exists")
            conn.close()

    # ================= LOGIN =================
    elif nav == "Login":
        st.subheader("Login")
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")

        if st.button("Login"):
            conn = sqlite3.connect(DB_NAME)
            res = conn.execute(
                "SELECT password FROM users WHERE username=?", (u,)
            ).fetchone()
            conn.close()
            if res and bcrypt.checkpw(p.encode(), res[0]):
                st.session_state.user = u
                st.rerun()
            else:
                st.error("Invalid credentials")

    # ================= UPLOAD =================
    elif nav == "Upload Video":
        st.subheader("Upload Master Video")
        up = st.file_uploader("MP4 file", type=["mp4"])

        if up and st.button("Save"):
            path = os.path.join(UPLOAD_DIR, up.name)
            with open(path, "wb") as f:
                f.write(up.read())

            conn = sqlite3.connect(DB_NAME)
            conn.execute(
                "INSERT INTO videos (filename, uploader) VALUES (?,?)",
                (up.name, st.session_state.user)
            )
            conn.commit()
            conn.close()
            st.success("Video saved")

    # ================= DOWNLOAD =================
    elif nav == "Download Secured Video":
        st.subheader("Download Watermarked Video")
        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute("SELECT filename FROM videos").fetchall()
        conn.close()

        for v in vids:
            if st.button(f"Secure & Download – {v[0]}"):
                bits = get_watermark_bits(st.session_state.user)

                with tempfile.TemporaryDirectory() as tmp:
                    in_v = os.path.join(UPLOAD_DIR, v[0])
                    a_in = os.path.join(tmp, "a.wav")
                    a_out = os.path.join(tmp, "aw.wav")
                    v_out = os.path.join(tmp, "final.mp4")

                    subprocess.run([
                        "ffmpeg", "-y", "-i", in_v,
                        "-vn", "-acodec", "pcm_s16le", "-ar", "44100",
                        a_in
                    ], capture_output=True)

                    with wave.open(a_in, "rb") as w:
                        params = w.getparams()
                        audio = np.frombuffer(
                            w.readframes(params.nframes),
                            dtype=np.int16
                        ).astype(np.float32)

                    for i in range(0, len(audio) - FRAME_SIZE, FRAME_SIZE):
                        audio[i:i+FRAME_SIZE] = embed_frame(
                            audio[i:i+FRAME_SIZE], bits
                        )

                    with wave.open(a_out, "wb") as w:
                        w.setparams(params)
                        w.writeframes(audio.astype(np.int16).tobytes())

                    subprocess.run([
                        "ffmpeg", "-y",
                        "-i", in_v, "-i", a_out,
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "320k",
                        v_out
                    ], capture_output=True)

                    with open(v_out, "rb") as f:
                        st.download_button(
                            "Download Secured Video",
                            f.read(),
                            file_name=f"secured_{v[0]}"
                        )

    # ================= DETECT =================
    elif nav == "Detect Piracy":
        st.subheader("Piracy Detection")
        leak = st.file_uploader("Upload Suspected Video", type=["mp4"])

        if leak and st.button("Scan"):
            with tempfile.TemporaryDirectory() as tmp:
                v_p = os.path.join(tmp, "l.mp4")
                a_p = os.path.join(tmp, "l.wav")

                with open(v_p, "wb") as f:
                    f.write(leak.read())

                subprocess.run([
                    "ffmpeg", "-y", "-i", v_p,
                    "-vn", "-acodec", "pcm_s16le", "-ar", "44100",
                    a_p
                ], capture_output=True)

                with wave.open(a_p, "rb") as w:
                    audio = np.frombuffer(
                        w.readframes(w.getparams().nframes),
                        dtype=np.int16
                    ).astype(np.float32)

                ids = detect_watermark(audio)

                if len(ids) >= 3:
                    final_id = VoteCounter(ids).most_common(1)[0][0]
                    conn = sqlite3.connect(DB_NAME)
                    info = conn.execute(
                        "SELECT email, phone FROM users WHERE username=?",
                        (final_id,)
                    ).fetchone()
                    conn.close()

                    st.error(f"🚨 PIRACY DETECTED: {final_id}")
                    if info:
                        st.info(f"Email: {info[0]} | Phone: {info[1]}")
                else:
                    st.success("No piracy signature detected")

    # ================= USERS =================
    elif nav == "User Database":
        conn = sqlite3.connect(DB_NAME)
        df = pd.read_sql_query("SELECT username,email,phone FROM users", conn)
        conn.close()
        st.dataframe(df, use_container_width=True)

# ================= RUN =================
if __name__ == "__main__":
    main()
