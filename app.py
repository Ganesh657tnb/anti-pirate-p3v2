import streamlit as st
import numpy as np
import wave, os, sqlite3, tempfile, subprocess, bcrypt
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter
from collections import Counter as VoteCounter

# --- CONFIGURATION ---
DB_NAME = "guardian_v7_final.db"
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!'
GAIN_FACTOR = 0.025 
FRAME_SIZE = 65536 # Approx 1.5s per frame

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 1. DATABASE LOGIC ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (username TEXT PRIMARY KEY, email TEXT, phone TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS videos 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, uploader TEXT)''')
    conn.commit()
    conn.close()

# --- 2. CRYPTO & DSSS LOGIC (TRIM-PROOF) ---
def get_payload_bits(user_id):
    ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    padded = user_id.ljust(16, ' ').encode('utf-8')
    return "".join(format(b, '08b') for b in cipher.encrypt(padded))

def decrypt_payload(bit_str):
    try:
        byte_data = bytes(int(bit_str[i:i+8], 2) for i in range(0, 128, 8))
        ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(byte_data).decode('utf-8', errors='ignore').strip()
    except: return None

def get_pn_sequence(n):
    np.random.seed(42)
    return (np.random.randint(0, 2, n) * 2 - 1).astype(np.float32)

def embed_frame(audio_seg, bits):
    n = len(audio_seg)
    pn = get_pn_sequence(n)
    spb = n // len(bits)
    watermark = np.zeros(n, dtype=np.float32)
    for i, bit in enumerate(bits):
        val = 1 if bit == '1' else -1
        watermark[i*spb : (i+1)*spb] = val * pn[i*spb : (i+1)*spb]
    rms = np.sqrt(np.mean(audio_seg**2)) + 1e-6
    return np.clip(audio_seg + (GAIN_FACTOR * watermark * rms), -32768, 32767)

# --- 3. UI NAVIGATION ---
def main():
    st.set_page_config(page_title="Guardian Anti-Piracy v7", layout="wide")
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    # Sidebar Navigation
    st.sidebar.title("🛡️ GUARDIAN V7.0")
    
    if st.session_state.user:
        menu = ["Home", "Upload Video", "Download Secured Video", "Detect Piracy (Deep Scan)", "User Database"]
        choice = st.sidebar.radio("Navigation", menu)
        if st.sidebar.button("Logout"):
            st.session_state.user = None
            st.rerun()
    else:
        menu = ["Home", "Login", "Register"]
        choice = st.sidebar.radio("Navigation", menu)

    # --- HOME PAGE ---
    if choice == "Home":
        st.title("Welcome to Project Guardian")
        st.write("This is a Robust Audio Watermarking System designed to detect video piracy even after trimming.")
        st.info("System Status: **Trim-Invariant Sliding-Window Frame Sync Active**")

    # --- REGISTER PAGE ---
    elif choice == "Register":
        st.title("📝 User Registration")
        with st.form("reg_form"):
            u = st.text_input("Username (Unique ID)")
            e = st.text_input("Email")
            ph = st.text_input("Phone Number")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Create Account"):
                conn = sqlite3.connect(DB_NAME)
                try:
                    h = bcrypt.hashpw(p.encode(), bcrypt.gensalt())
                    conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, ph, h))
                    conn.commit()
                    st.success("Account created! Go to Login page.")
                except: st.error("Username already exists.")
                conn.close()

    # --- LOGIN PAGE ---
    elif choice == "Login":
        st.title("🔐 User Login")
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Login"):
            conn = sqlite3.connect(DB_NAME)
            res = conn.execute("SELECT password FROM users WHERE username=?", (u,)).fetchone()
            if res and bcrypt.checkpw(p.encode(), res[0]):
                st.session_state.user = u
                st.rerun()
            else: st.error("Invalid username or password.")

    # --- UPLOAD PAGE ---
    elif choice == "Upload Video":
        st.title("📤 Master Video Upload")
        up = st.file_uploader("Upload Original MP4", type=['mp4'])
        if up and st.button("Save to Vault"):
            path = os.path.join(UPLOAD_DIR, up.name)
            with open(path, "wb") as f: f.write(up.read())
            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO videos (filename, uploader) VALUES (?,?)", (up.name, st.session_state.user))
            conn.commit()
            st.success(f"Master file '{up.name}' uploaded successfully.")

    # --- DOWNLOAD PAGE ---
    elif choice == "Download Secured Video":
        st.title("📥 Secure Download")
        st.write("Embedding your unique ID into the video audio...")
        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute("SELECT filename FROM videos").fetchall()
        for v in vids:
            with st.expander(f"Video: {v[0]}"):
                if st.button(f"Generate Secured Copy for {st.session_state.user}", key=v[0]):
                    with st.spinner("Applying continuous DSSS embedding..."):
                        bits = get_payload_bits(st.session_state.user)
                        with tempfile.TemporaryDirectory() as tmp:
                            in_v = os.path.join(UPLOAD_DIR, v[0])
                            a_in, a_out, v_out = os.path.join(tmp, "1.wav"), os.path.join(tmp, "2.wav"), os.path.join(tmp, "f.mp4")
                            subprocess.run(["ffmpeg", "-y", "-i", in_v, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_in], capture_output=True)
                            with wave.open(a_in, 'rb') as wav:
                                audio = np.frombuffer(wav.readframes(wav.getparams().nframes), dtype=np.int16).astype(np.float32)
                                params = wav.getparams()
                            processed = [embed_frame(audio[i:i+FRAME_SIZE], bits) if len(audio[i:i+FRAME_SIZE]) == FRAME_SIZE else audio[i:i+FRAME_SIZE] for i in range(0, len(audio), FRAME_SIZE)]
                            final_a = np.concatenate(processed).astype(np.int16)
                            with wave.open(a_out, 'wb') as wav:
                                wav.setparams(params); wav.writeframes(final_a.tobytes())
                            subprocess.run(["ffmpeg", "-y", "-i", in_v, "-i", a_out, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", v_out], capture_output=True)
                            with open(v_out, 'rb') as f:
                                st.download_button("Download Secured MP4", f.read(), file_name=f"secured_{v[0]}")

    # --- DETECT PAGE ---
    elif choice == "Detect Piracy (Deep Scan)":
        st.title("🔍 Piracy Detection")
        leak = st.file_uploader("Upload Suspected Pirated Video", type=['mp4'])
        if leak and st.button("Start Deep Scan"):
            with st.spinner("Searching for watermark frames in audio..."):
                with tempfile.TemporaryDirectory() as tmp:
                    v_p, a_p = os.path.join(tmp, "l.mp4"), os.path.join(tmp, "l.wav")
                    with open(v_p, "wb") as f: f.write(leak.read())
                    subprocess.run(["ffmpeg", "-y", "-i", v_p, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_p], capture_output=True)
                    try:
                        with wave.open(a_p, 'rb') as wav:
                            audio = np.frombuffer(wav.readframes(wav.getparams().nframes), dtype=np.int16).astype(np.float32)
                        pn = get_pn_sequence(FRAME_SIZE)
                        spb = FRAME_SIZE // 128
                        recovered_ids = []
                        step = FRAME_SIZE // 4 # Sliding window jump
                        for i in range(0, len(audio) - FRAME_SIZE + 1, step):
                            seg = audio[i : i + FRAME_SIZE]
                            bits = "".join(["1" if np.sum(seg[b*spb : (b+1)*spb] * pn[b*spb : (b+1)*spb]) > 0 else "0" for b in range(128)])
                            uid = decrypt_payload(bits)
                            if uid and len(uid) > 1: recovered_ids.append(uid)
                        
                        if recovered_ids:
                            final_id = VoteCounter(recovered_ids).most_common(1)[0][0]
                            conn = sqlite3.connect(DB_NAME)
                            info = conn.execute("SELECT email, phone FROM users WHERE username=?", (final_id,)).fetchone()
                            st.error(f"🚨 PIRACY DETECTED! User Identity: {final_id}")
                            if info:
                                st.warning(f"**Pirater Contact Info:** Email: {info[0]} | Phone: {info[1]}")
                        else: st.success("No piracy signature detected in this file.")
                    except: st.error("Error processing audio stream.")

    # --- DATABASE PAGE ---
    elif choice == "User Database":
        st.title("👥 Registered Users Database")
        conn = sqlite3.connect(DB_NAME)
        df = pd.read_sql_query("SELECT username, email, phone FROM users", conn)
        st.dataframe(df, use_container_width=True) # Professional table view

if __name__ == "__main__":
    main()
