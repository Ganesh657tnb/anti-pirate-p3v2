import streamlit as st
import numpy as np
import wave, os, sqlite3, tempfile, subprocess, bcrypt
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# --- CONFIGURATION ---
DB_NAME = "guardian_final_v4.db" 
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!' 
BLOCK_DURATION = 10 # Block duration increased for better stability
GAIN_FACTOR = 0.015 # Gain slightly higher for 15s clips

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 1. DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    conn.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, email TEXT, phone TEXT, password TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS videos (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, uploader TEXT)''')
    conn.commit()
    conn.close()

# --- 2. CRYPTO & SYNC (THE FIX) ---
def get_binary_id(user_id):
    """AES-128 CTR Encryption with strict alignment"""
    ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    # Pad to 16 bytes
    padded = user_id.ljust(16, ' ').encode('utf-8')
    encrypted = cipher.encrypt(padded)
    return "".join(format(b, '08b') for b in encrypted)

def recover_id(bit_string):
    """AES-128 CTR Decryption with Junk Filtering"""
    try:
        byte_data = bytes(int(bit_string[i:i+8], 2) for i in range(0, len(bit_string), 8))
        ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        decrypted = cipher.decrypt(byte_data).decode('utf-8', errors='ignore')
        return decrypted.strip()
    except: return None

# --- 3. DSSS ENGINE ---
def get_pn_sequence(n):
    np.random.seed(99) # Constant Seed for Global Sync
    return (np.random.randint(0, 2, n) * 2 - 1).astype(np.float32)

def embed_dss(audio_seg, bits):
    n = len(audio_seg)
    pn = get_pn_sequence(n)
    bit_len = len(bits)
    spb = n // bit_len # Samples per bit
    
    watermark = np.zeros(n)
    for i in range(bit_len):
        val = 1 if bits[i] == '1' else -1
        watermark[i*spb : (i+1)*spb] = val * pn[i*spb : (i+1)*spb]
    
    # Adaptive Gain
    rms = np.sqrt(np.mean(audio_seg**2)) + 1e-6
    return np.clip(audio_seg + (GAIN_FACTOR * watermark * rms), -32768, 32767)

# --- 4. SYSTEM LOGIC ---
def process_video_download(video_path, user_id):
    bit_str = get_binary_id(user_id)
    with tempfile.TemporaryDirectory() as tmp:
        a_in, a_out, v_final = os.path.join(tmp, "1.wav"), os.path.join(tmp, "2.wav"), os.path.join(tmp, "out.mp4")
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_in], capture_output=True)
        
        with wave.open(a_in, 'rb') as wav:
            params = wav.getparams()
            audio = np.frombuffer(wav.readframes(params.nframes), dtype=np.int16).astype(np.float32)
        
        # Block-based redundancy
        spb_block = BLOCK_DURATION * 44100
        processed = []
        for i in range(0, len(audio), spb_block):
            seg = audio[i:i+spb_block]
            if len(seg) == spb_block: processed.append(embed_dss(seg, bit_str))
            else: processed.append(seg)
        
        final_audio = np.concatenate(processed).astype(np.int16)
        with wave.open(a_out, 'wb') as wav:
            wav.setparams(params); wav.writeframes(final_audio.tobytes())
            
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-i", a_out, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", v_final], capture_output=True)
        with open(v_final, 'rb') as f: return f.read()

def detect_watermark(video_file):
    with tempfile.TemporaryDirectory() as tmp:
        v_p, a_p = os.path.join(tmp, "l.mp4"), os.path.join(tmp, "l.wav")
        with open(v_p, "wb") as f: f.write(video_file.read())
        subprocess.run(["ffmpeg", "-y", "-i", v_p, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_p], capture_output=True)
        
        try:
            with wave.open(a_p, 'rb') as wav:
                audio = np.frombuffer(wav.readframes(wav.getparams().nframes), dtype=np.int16).astype(np.float32)
        except: return None
        
        spb_block = BLOCK_DURATION * 44100
        bit_votes = []
        for i in range(0, len(audio) - spb_block + 1, spb_block):
            seg = audio[i:i+spb_block]
            pn, bit_len = get_pn_sequence(len(seg)), 128
            spb = len(seg) // bit_len
            bits = "".join(["1" if np.sum(seg[b*spb:(b+1)*spb] * pn[b*spb:(b+1)*spb]) > 0 else "0" for b in range(bit_len)])
            bit_votes.append(bits)
            
        if not bit_votes: return None
        # Majority Vote
        final_bits = "".join([max(set([v[b_idx] for v in bit_votes]), key=[v[b_idx] for v in bit_votes].count) for b_idx in range(128)])
        return recover_id(final_bits)

# --- 5. STREAMLIT UI ---
def main():
    st.set_page_config(page_title="Guardian Ultra Sync")
    init_db()
    if "user" not in st.session_state: st.session_state.user = None

    menu = ["Home", "Login", "Register"] if not st.session_state.user else ["Home", "Upload", "Download", "Detect", "Users"]
    choice = st.sidebar.radio("Menu", menu)

    if choice == "Home":
        st.title("🛡️ Guardian Anti-Piracy v4")
        st.write("Academic Robust Watermarking with Global PN Sync.")

    elif choice == "Register":
        u, e, ph, p = st.text_input("User"), st.text_input("Email"), st.text_input("Phone"), st.text_input("Pass", type="password")
        if st.button("Register"):
            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, ph, bcrypt.hashpw(p.encode(), bcrypt.gensalt())))
            conn.commit(); st.success("Done!")

    elif choice == "Login":
        u, p = st.text_input("User"), st.text_input("Pass", type="password")
        if st.button("Login"):
            conn = sqlite3.connect(DB_NAME)
            res = conn.execute("SELECT password FROM users WHERE username=?", (u,)).fetchone()
            if res and bcrypt.checkpw(p.encode(), res[0]):
                st.session_state.user = u
                st.rerun()

    if st.session_state.user:
        if choice == "Upload":
            up = st.file_uploader("Video", type=['mp4'])
            if up and st.button("Save"):
                with open(os.path.join(UPLOAD_DIR, up.name), "wb") as f: f.write(up.read())
                conn = sqlite3.connect(DB_NAME); conn.execute("INSERT INTO videos (filename, uploader) VALUES (?,?)", (up.name, st.session_state.user)); conn.commit()
        
        elif choice == "Download":
            conn = sqlite3.connect(DB_NAME)
            for v in conn.execute("SELECT filename FROM videos").fetchall():
                if st.button(f"Secure Download: {v[0]}"):
                    data = process_video_download(os.path.join(UPLOAD_DIR, v[0]), st.session_state.user)
                    st.download_button("Download Now", data, file_name=f"secured_{v[0]}")

        elif choice == "Detect":
            leak = st.file_uploader("Leak")
            if leak and st.button("Identify"):
                res = detect_watermark(leak)
                if res:
                    conn = sqlite3.connect(DB_NAME)
                    info = conn.execute("SELECT email, phone FROM users WHERE username=?", (res,)).fetchone()
                    if info:
                        st.error(f"🚨 PIRATER: {res}\nEmail: {info[0]}\nPhone: {info[1]}")
                    else: st.warning(f"Detected Junk: {res}. Sync is still off.")
                else: st.success("Clean.")

        elif choice == "Users":
            conn = sqlite3.connect(DB_NAME)
            st.table(pd.read_sql_query("SELECT username, email, phone FROM users", conn))

if __name__ == "__main__":
    main()
