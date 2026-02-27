import streamlit as st
import numpy as np
import wave
import os
import sqlite3
import tempfile
import subprocess
import bcrypt
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# --- CONFIGURATION ---
DB_NAME = "guardian_vault_v2.db" # Database version updated
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!' 
BLOCK_DURATION = 5  
GAIN_FACTOR = 0.006 

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 1. DATABASE & AUTHENTICATION ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Added phone column here
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (username TEXT PRIMARY KEY, email TEXT, phone TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS videos 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, uploader TEXT)''')
    conn.commit()
    conn.close()

def hash_pass(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt())

def check_pass(password, hashed):
    return bcrypt.checkpw(password.encode(), hashed)

# --- 2. CRYPTOGRAPHY (AES-128 CTR) ---
def encrypt_id(user_id):
    ctr = Counter.new(64, prefix=b'\x00'*8, initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    encrypted_bytes = cipher.encrypt(user_id.encode().ljust(16, b'\0'))
    return "".join(format(b, '08b') for b in encrypted_bytes)

def decrypt_bits(bit_string):
    try:
        byte_data = int(bit_string, 2).to_bytes(len(bit_string) // 8, 'big')
        ctr = Counter.new(64, prefix=b'\x00'*8, initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        decrypted = cipher.decrypt(byte_data).decode('utf-8', errors='ignore')
        return decrypted.strip('\x00')
    except: return None

# --- 3. SIGNAL PROCESSING (DSSS) ---
def get_pn_sequence(n):
    np.random.seed(42) 
    return (np.random.randint(0, 2, n) * 2 - 1).astype(np.float32)

def embed_dss_block(audio_seg, bits):
    n = len(audio_seg)
    pn = get_pn_sequence(n)
    bit_len = len(bits)
    samples_per_bit = n // bit_len
    watermark = np.zeros(n)
    for i in range(bit_len):
        val = 1 if bits[i] == '1' else -1
        start, end = i * samples_per_bit, (i + 1) * samples_per_bit
        if end <= n: watermark[start:end] = val * pn[start:end]
    local_rms = np.sqrt(np.mean(audio_seg**2))
    strength = GAIN_FACTOR * (local_rms if local_rms > 0.001 else 0.001)
    return np.clip(audio_seg + (strength * watermark), -32768, 32767)

# --- 4. CORE LOGIC ---
def process_video_download(video_path, user_id):
    bit_str = encrypt_id(user_id)
    with tempfile.TemporaryDirectory() as tmp:
        audio_in, audio_out, video_final = os.path.join(tmp, "in.wav"), os.path.join(tmp, "out.wav"), os.path.join(tmp, "final.mp4")
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", audio_in], capture_output=True)
        with wave.open(audio_in, 'rb') as wav:
            params = wav.getparams()
            audio_samples = np.frombuffer(wav.readframes(params.nframes), dtype=np.int16).astype(np.float32)
        samples_per_block = BLOCK_DURATION * params.framerate
        processed_list = []
        for i in range(0, len(audio_samples), samples_per_block):
            block = audio_samples[i : i + samples_per_block]
            if len(block) == samples_per_block: processed_list.append(embed_dss_block(block, bit_str))
            else: processed_list.append(block)
        processed_audio = np.concatenate(processed_list)
        with wave.open(audio_out, 'wb') as wav:
            wav.setparams(params); wav.writeframes(processed_audio.astype(np.int16).tobytes())
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-i", audio_out, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", video_final], capture_output=True)
        with open(video_final, 'rb') as f: return f.read()

def detect_watermark(video_file):
    with tempfile.TemporaryDirectory() as tmp:
        v_path, a_path = os.path.join(tmp, "l.mp4"), os.path.join(tmp, "l.wav")
        with open(v_path, "wb") as f: f.write(video_file.read())
        subprocess.run(["ffmpeg", "-y", "-i", v_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_path], capture_output=True)
        try:
            with wave.open(a_path, 'rb') as wav:
                audio = np.frombuffer(wav.readframes(wav.getparams().nframes), dtype=np.int16).astype(np.float32)
                rate = wav.getparams().framerate
        except: return None
        samples_per_block = BLOCK_DURATION * rate
        bit_votes = []
        for i in range(0, len(audio) - samples_per_block + 1, samples_per_block):
            block = audio[i : i + samples_per_block]
            pn, bit_len = get_pn_sequence(len(block)), 128
            spb = len(block) // bit_len
            bits = "".join(["1" if np.sum(block[b*spb:(b+1)*spb]*pn[b*spb:(b+1)*spb]) > 0 else "0" for b in range(bit_len)])
            bit_votes.append(bits)
        if not bit_votes: return None
        final_bits = "".join([max(set([v[b_idx] for v in bit_votes]), key=[v[b_idx] for v in bit_votes].count) for b_idx in range(128)])
        return decrypt_bits(final_bits)

# --- 5. UI ---
def main():
    st.set_page_config(page_title="Guardian v2.0", layout="wide")
    init_db()
    if "user" not in st.session_state: st.session_state.user = None

    choice = st.sidebar.radio("Navigation", ["Home", "Login", "Register"] if not st.session_state.user else ["Home", "Upload Video", "Download Video", "Detect Leak", "Database Views"])

    if choice == "Home":
        st.title("🛡️ Guardian System")
        st.info("Robust DSSS Watermarking with AES-128 CTR Encryption")

    elif choice == "Register":
        u = st.text_input("Username")
        e = st.text_input("Email")
        ph = st.text_input("Phone Number") # Added field
        p = st.text_input("Password", type="password")
        if st.button("Sign Up"):
            conn = sqlite3.connect(DB_NAME)
            try:
                conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, ph, hash_pass(p)))
                conn.commit()
                st.success("Account created!")
            except: st.error("Error creating account.")
            conn.close()

    elif choice == "Login":
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Login"):
            conn = sqlite3.connect(DB_NAME)
            res = conn.execute("SELECT password FROM users WHERE username=?", (u,)).fetchone()
            if res and check_pass(p, res[0]):
                st.session_state.user = u
                st.rerun()
            else: st.error("Access Denied.")

    if st.session_state.user:
        st.sidebar.button("Logout", on_click=lambda: st.session_state.update({"user": None}))

        if choice == "Upload Video":
            up = st.file_uploader("Video", type=['mp4'])
            if up and st.button("Upload"):
                with open(os.path.join(UPLOAD_DIR, up.name), "wb") as f: f.write(up.read())
                conn = sqlite3.connect(DB_NAME); conn.execute("INSERT INTO videos (filename, uploader) VALUES (?,?)", (up.name, st.session_state.user)); conn.commit()
                st.success("Saved.")

        elif choice == "Download Video":
            conn = sqlite3.connect(DB_NAME)
            vids = conn.execute("SELECT filename FROM videos").fetchall()
            for v in vids:
                if st.button(f"Embed & Download: {v[0]}"):
                    data = process_video_download(os.path.join(UPLOAD_DIR, v[0]), st.session_state.user)
                    st.download_button("Click to Download", data, file_name=f"secured_{v[0]}")

        elif choice == "Detect Leak":
            leak = st.file_uploader("Upload Suspected File")
            if leak and st.button("Scan"):
                res = detect_watermark(leak)
                if res:
                    st.error(f"🚨 PIRATER IDENTIFIED: {res}")
                    # Fetching phone number from DB
                    conn = sqlite3.connect(DB_NAME)
                    p_info = conn.execute("SELECT email, phone FROM users WHERE username=?", (res,)).fetchone()
                    if p_info: st.write(f"Contact Info: Email - {p_info[0]}, Phone - {p_info[1]}")
                else: st.success("No watermark found.")

        elif choice == "Database Views":
            st.subheader("Registered Users")
            conn = sqlite3.connect(DB_NAME)
            st.table(pd.read_sql_query("SELECT username, email, phone FROM users", conn))

if __name__ == "__main__":
    main()
