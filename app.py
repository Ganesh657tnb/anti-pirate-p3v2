import streamlit as st
import numpy as np
import wave
import os
import sqlite3
import tempfile
import subprocess
import bcrypt
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# --- CONFIGURATION & CONSTANTS ---
DB_NAME = "guardian_vault.db"
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!' # 16 bytes exactly
BLOCK_DURATION = 5  # Seconds per redundant block
GAIN_FACTOR = 0.006 # Adjustable strength

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 1. DATABASE & AUTHENTICATION ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (username TEXT PRIMARY KEY, email TEXT, password TEXT)''')
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
    """Encrypts UserID to binary string using AES-CTR"""
    # Fixed counter for synchronization across embedder/extractor
    ctr = Counter.new(64, prefix=b'\x00'*8, initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    encrypted_bytes = cipher.encrypt(user_id.encode().ljust(16, b'\0'))
    return "".join(format(b, '08b') for b in encrypted_bytes)

def decrypt_bits(bit_string):
    """Converts bits back to UserID"""
    try:
        byte_data = int(bit_string, 2).to_bytes(len(bit_string) // 8, 'big')
        ctr = Counter.new(64, prefix=b'\x00'*8, initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        decrypted = cipher.decrypt(byte_data).decode('utf-8', errors='ignore')
        return decrypted.strip('\x00')
    except Exception as e:
        return None

# --- 3. SIGNAL PROCESSING (DSSS) ---
def get_pn_sequence(n):
    """Generates a stable Pseudo-Noise sequence"""
    np.random.seed(42) 
    return (np.random.randint(0, 2, n) * 2 - 1).astype(np.float32)

def embed_dss_block(audio_seg, bits):
    """Embeds bits into a single audio block using DSSS"""
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
        
    # Adaptive Scaling: Psychoacoustic Masking
    local_rms = np.sqrt(np.mean(audio_seg**2))
    strength = GAIN_FACTOR * (local_rms if local_rms > 0.001 else 0.001)
    
    # Updated: Removed Walrus Operator for compatibility
    clipt_audio = np.clip(audio_seg + (strength * watermark), -32768, 32767)
    return clipt_audio

# --- 4. CORE LOGIC: WATERMARKING ---
def process_video_download(video_path, user_id):
    bit_str = encrypt_id(user_id)
    
    with tempfile.TemporaryDirectory() as tmp:
        audio_in = os.path.join(tmp, "in.wav")
        audio_out = os.path.join(tmp, "out.wav")
        video_final = os.path.join(tmp, "final.mp4")
        
        # Extract Audio
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", audio_in], capture_output=True)
        
        with wave.open(audio_in, 'rb') as wav:
            params = wav.getparams()
            audio_samples = np.frombuffer(wav.readframes(params.nframes), dtype=np.int16).astype(np.float32)
            
        # Time-Interval Redundant Embedding
        samples_per_block = BLOCK_DURATION * params.framerate
        processed_audio_list = []
        
        for i in range(0, len(audio_samples), samples_per_block):
            block = audio_samples[i : i + samples_per_block]
            if len(block) == samples_per_block:
                wm_block = embed_dss_block(block, bit_str)
                processed_audio_list.append(wm_block)
            else:
                processed_audio_list.append(block)
        
        processed_audio = np.concatenate(processed_audio_list)
            
        with wave.open(audio_out, 'wb') as wav:
            wav.setparams(params)
            wav.writeframes(processed_audio.astype(np.int16).tobytes())
            
        # Re-merge Video & Watermarked Audio
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-i", audio_out, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", video_final], capture_output=True)
        
        with open(video_final, 'rb') as f:
            return f.read()

def detect_watermark(video_file):
    with tempfile.TemporaryDirectory() as tmp:
        v_path = os.path.join(tmp, "leak.mp4")
        a_path = os.path.join(tmp, "leak.wav")
        with open(v_path, "wb") as f: f.write(video_file.read())
        
        subprocess.run(["ffmpeg", "-y", "-i", v_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_path], capture_output=True)
        
        try:
            with wave.open(a_path, 'rb') as wav:
                params = wav.getparams()
                audio = np.frombuffer(wav.readframes(params.nframes), dtype=np.int16).astype(np.float32)
        except: return None
            
        samples_per_block = BLOCK_DURATION * params.framerate
        bit_votes = []

        # Sliding Window / Block-based Extraction
        for i in range(0, len(audio) - samples_per_block + 1, samples_per_block):
            block = audio[i : i + samples_per_block]
            pn = get_pn_sequence(len(block))
            bit_len = 128 # 16 bytes * 8 bits
            spb = len(block) // bit_len
            
            block_bits = ""
            for b in range(bit_len):
                corr = np.sum(block[b*spb:(b+1)*spb] * pn[b*spb:(b+1)*spb])
                block_bits += "1" if corr > 0 else "0"
            bit_votes.append(block_bits)
            
        if not bit_votes: return None
        
        # Majority Voting for Robustness
        final_bits = ""
        for b_idx in range(128):
            votes = [v[b_idx] for v in bit_votes]
            final_bits += max(set(votes), key=votes.count)
            
        return decrypt_bits(final_bits)

# --- 5. STREAMLIT UI ---
def main():
    st.set_page_config(page_title="Guardian Anti-Piracy", layout="wide")
    init_db()
    
    if "user" not in st.session_state: st.session_state.user = None

    st.sidebar.title("🛡️ Guardian System")
    menu = ["Home", "Login", "Register"]
    if st.session_state.user:
        menu = ["Home", "Upload Video", "Download Video", "Detect Leak"]
    
    choice = st.sidebar.radio("Navigation", menu)

    if choice == "Home":
        st.title("Academic Anti-Piracy Project")
        st.info("Direct Sequence Spread Spectrum (DSSS) + AES-128 CTR Watermarking")
        st.write("This system embeds an invisible, encrypted user identity into the audio stream of a video.")

    elif choice == "Register":
        u = st.text_input("Username")
        e = st.text_input("Email")
        p = st.text_input("Password", type="password")
        if st.button("Create Account"):
            conn = sqlite3.connect(DB_NAME)
            try:
                conn.execute("INSERT INTO users VALUES (?,?,?)", (u, e, hash_pass(p)))
                conn.commit()
                st.success("Success! Please Login.")
            except: st.error("User exists.")
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
            else: st.error("Wrong credentials.")

    if st.session_state.user:
        st.sidebar.warning(f"Active User: {st.session_state.user}")
        if st.sidebar.button("Logout"):
            st.session_state.user = None
            st.rerun()

        if choice == "Upload Video":
            up = st.file_uploader("Upload Master Video", type=['mp4', 'mkv'])
            if up and st.button("Save to Server"):
                path = os.path.join(UPLOAD_DIR, up.name)
                with open(path, "wb") as f: f.write(up.read())
                conn = sqlite3.connect(DB_NAME)
                conn.execute("INSERT INTO videos (filename, uploader) VALUES (?,?)", (up.name, st.session_state.user))
                conn.commit()
                st.success("File uploaded.")

        elif choice == "Download Video":
            conn = sqlite3.connect(DB_NAME)
            vids = conn.execute("SELECT filename FROM videos").fetchall()
            for v in vids:
                with st.container():
                    col1, col2 = st.columns([3, 1])
                    col1.write(v[0])
                    if col2.button("Embed & Download", key=v[0]):
                        with st.spinner("Applying DSSS Redundancy..."):
                            data = process_video_download(os.path.join(UPLOAD_DIR, v[0]), st.session_state.user)
                            st.download_button("Download Secured File", data, file_name=f"secured_{v[0]}")

        elif choice == "Detect Leak":
            leak = st.file_uploader("Suspected Video")
            if leak and st.button("Analyze"):
                with st.spinner("Extracting Watermark via Majority Voting..."):
                    result = detect_watermark(leak)
                    if result:
                        st.error(f"PIRATER FOUND: {result}")
                    else:
                        st.success("Clean Video: No Watermark Found.")

if __name__ == "__main__":
    main()
