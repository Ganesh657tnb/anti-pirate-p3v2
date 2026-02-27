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
DB_NAME = "guardian_vault_v3.db" 
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!' # Must be 16 bytes
BLOCK_DURATION = 5  
GAIN_FACTOR = 0.008 # Lightly increased for better detection

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 1. DATABASE & AUTHENTICATION ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
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

# --- 2. CRYPTOGRAPHY (AES-128 CTR - ROBUST SYNC) ---
def encrypt_id(user_id):
    # FIXED PREFIX for guaranteed sync between embedder and extractor
    ctr = Counter.new(64, prefix=b'SYNC_CTR', initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    
    # Pad User ID to exactly 16 bytes to avoid bit-length issues
    padded_id = user_id.ljust(16, ' ')
    encrypted_bytes = cipher.encrypt(padded_id.encode('utf-8'))
    
    return "".join(format(b, '08b') for b in encrypted_bytes)

def decrypt_bits(bit_string):
    try:
        # Convert bit string back to bytes
        byte_data = bytes(int(bit_string[i:i+8], 2) for i in range(0, len(bit_string), 8))
        
        # EXACT SAME counter settings as encryption
        ctr = Counter.new(64, prefix=b'SYNC_CTR', initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        
        decrypted = cipher.decrypt(byte_data).decode('utf-8', errors='ignore')
        return decrypted.strip() # Remove padding spaces
    except:
        return None

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
        if end <= n:
            watermark[start:end] = val * pn[start:end]
            
    local_rms = np.sqrt(np.mean(audio_seg**2))
    strength = GAIN_FACTOR * (local_rms if local_rms > 0.001 else 0.001)
    
    return np.clip(audio_seg + (strength * watermark), -32768, 32767)

# --- 4. CORE LOGIC ---
def process_video_download(video_path, user_id):
    bit_str = encrypt_id(user_id)
    
    with tempfile.TemporaryDirectory() as tmp:
        audio_in, audio_out, video_final = os.path.join(tmp, "in.wav"), os.path.join(tmp, "out.wav"), os.path.join(tmp, "final.mp4")
        
        # Audio Extraction
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", audio_in], capture_output=True)
        
        with wave.open(audio_in, 'rb') as wav:
            params = wav.getparams()
            audio_samples = np.frombuffer(wav.readframes(params.nframes), dtype=np.int16).astype(np.float32)
            
        samples_per_block = BLOCK_DURATION * params.framerate
        processed_list = []
        
        for i in range(0, len(audio_samples), samples_per_block):
            block = audio_samples[i : i + samples_per_block]
            if len(block) == samples_per_block:
                processed_list.append(embed_dss_block(block, bit_str))
            else:
                processed_list.append(block)
        
        processed_audio = np.concatenate(processed_list)
        
        with wave.open(audio_out, 'wb') as wav:
            wav.setparams(params)
            wav.writeframes(processed_audio.astype(np.int16).tobytes())
            
        # Merge back
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-i", audio_out, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", video_final], capture_output=True)
        
        with open(video_final, 'rb') as f:
            return f.read()

def detect_watermark(video_file):
    with tempfile.TemporaryDirectory() as tmp:
        v_path, a_path = os.path.join(tmp, "leak.mp4"), os.path.join(tmp, "leak.wav")
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
            
            bits = ""
            for b in range(bit_len):
                corr = np.sum(block[b*spb:(b+1)*spb] * pn[b*spb:(b+1)*spb])
                bits += "1" if corr > 0 else "0"
            bit_votes.append(bits)
            
        if not bit_votes: return None
        
        # Majority Voting for robust bit recovery
        final_bits = ""
        for b_idx in range(128):
            votes = [v[b_idx] for v in bit_votes]
            final_bits += max(set(votes), key=votes.count)
            
        return decrypt_bits(final_bits)

# --- 5. UI ---
def main():
    st.set_page_config(page_title="Guardian Anti-Piracy v3", layout="wide")
    init_db()
    
    if "user" not in st.session_state: st.session_state.user = None

    choice = st.sidebar.radio("Menu", ["Home", "Login", "Register"] if not st.session_state.user else ["Home", "Upload Video", "Download Video", "Detect Leak", "User Database"])

    if choice == "Home":
        st.title("🛡️ Guardian Anti-Piracy")
        st.info("Robust DSSS Audio Watermarking with Junk-Free AES-CTR Sync")

    elif choice == "Register":
        u = st.text_input("Username")
        e = st.text_input("Email")
        ph = st.text_input("Phone Number")
        p = st.text_input("Password", type="password")
        if st.button("Register"):
            conn = sqlite3.connect(DB_NAME)
            try:
                conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, ph, hash_pass(p)))
                conn.commit()
                st.success("Registration Successful!")
            except: st.error("Error: Try a different username.")
            conn.close()

    elif choice == "Login":
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Log In"):
            conn = sqlite3.connect(DB_NAME)
            res = conn.execute("SELECT password FROM users WHERE username=?", (u,)).fetchone()
            if res and check_pass(p, res[0]):
                st.session_state.user = u
                st.rerun()
            else: st.error("Invalid Login.")

    if st.session_state.user:
        st.sidebar.write(f"Logged in: **{st.session_state.user}**")
        if st.sidebar.button("Logout"):
            st.session_state.user = None
            st.rerun()

        if choice == "Upload Video":
            up = st.file_uploader("Upload Master", type=['mp4'])
            if up and st.button("Save"):
                with open(os.path.join(UPLOAD_DIR, up.name), "wb") as f: f.write(up.read())
                conn = sqlite3.connect(DB_NAME)
                conn.execute("INSERT INTO videos (filename, uploader) VALUES (?,?)", (up.name, st.session_state.user))
                conn.commit()
                st.success("Video Uploaded.")

        elif choice == "Download Video":
            conn = sqlite3.connect(DB_NAME)
            vids = conn.execute("SELECT filename FROM videos").fetchall()
            for v in vids:
                if st.button(f"Secure Download: {v[0]}"):
                    with st.spinner("Embedding Identity..."):
                        data = process_video_download(os.path.join(UPLOAD_DIR, v[0]), st.session_state.user)
                        st.download_button("Download Now", data, file_name=f"secured_{v[0]}")

        elif choice == "Detect Leak":
            leak = st.file_uploader("Upload Suspected Leak")
            if leak and st.button("Identify Pirater"):
                with st.spinner("Analyzing audio blocks..."):
                    res = detect_watermark(leak)
                    if res:
                        # Find user in DB to verify
                        conn = sqlite3.connect(DB_NAME)
                        u_info = conn.execute("SELECT email, phone FROM users WHERE username=?", (res,)).fetchone()
                        if u_info:
                            st.error(f"🚨 PIRATER FOUND: {res}")
                            st.write(f"**Email:** {u_info[0]} | **Phone:** {u_info[1]}")
                        else:
                            st.warning(f"Watermark detected as '{res}', but user not in DB. Might be a partial sync issue.")
                    else:
                        st.success("No piracy signature detected.")

        elif choice == "User Database":
            conn = sqlite3.connect(DB_NAME)
            st.table(pd.read_sql_query("SELECT username, email, phone FROM users", conn))

if __name__ == "__main__":
    main()
