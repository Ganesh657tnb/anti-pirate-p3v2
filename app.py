import streamlit as st
import numpy as np
import wave, os, sqlite3, tempfile, subprocess, bcrypt
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# --- CONFIGURATION ---
DB_NAME = "guardian_v6.db" 
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!' 
SYNC_HEADER = "10101011"
GAIN_FACTOR = 0.02

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 1. DATABASE & AUTH ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (username TEXT PRIMARY KEY, email TEXT, phone TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS videos 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, uploader TEXT)''')
    conn.commit()
    conn.close()

# --- 2. CORE SIGNAL & CRYPTO LOGIC ---
def get_watermark_bits(user_id):
    ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    padded = user_id.ljust(16, ' ').encode('utf-8')
    encrypted = cipher.encrypt(padded)
    payload_bits = "".join(format(b, '08b') for b in encrypted)
    return SYNC_HEADER + payload_bits

def recover_from_bits(full_bit_str):
    if SYNC_HEADER not in full_bit_str: return None
    start_idx = full_bit_str.find(SYNC_HEADER) + len(SYNC_HEADER)
    payload_bits = full_bit_str[start_idx : start_idx + 128]
    if len(payload_bits) < 128: return None
    try:
        byte_data = bytes(int(payload_bits[i:i+8], 2) for i in range(0, 128, 8))
        ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(byte_data).decode('utf-8', errors='ignore').strip()
    except: return None

def get_pn_sequence(n):
    np.random.seed(123)
    return (np.random.randint(0, 2, n) * 2 - 1).astype(np.float32)

def embed_dss(audio_seg, bits):
    n = len(audio_seg)
    pn = get_pn_sequence(n)
    bit_len = len(bits)
    spb = n // bit_len
    watermark = np.zeros(n)
    for i in range(bit_len):
        val = 1 if bits[i] == '1' else -1
        watermark[i*spb : (i+1)*spb] = val * pn[i*spb : (i+1)*spb]
    rms = np.sqrt(np.mean(audio_seg**2)) + 1e-6
    return np.clip(audio_seg + (GAIN_FACTOR * watermark * rms), -32768, 32767)

# --- 3. UI SECTIONS ---
def main():
    st.set_page_config(page_title="Guardian Anti-Piracy", layout="wide")
    init_db()
    
    if "user" not in st.session_state: st.session_state.user = None

    # --- SIDEBAR NAVIGATION ---
    st.sidebar.title("🛡️ GUARDIAN V6.0")
    
    if st.session_state.user:
        nav = st.sidebar.radio("Navigation", ["Home", "Upload Video", "Download Video", "Detect Piracy", "User Database"])
        if st.sidebar.button("Logout"):
            st.session_state.user = None
            st.rerun()
    else:
        nav = st.sidebar.radio("Navigation", ["Home", "Login", "Register"])

    # --- SECTION: HOME ---
    if nav == "Home":
        st.title("Project Guardian: DSSS Watermarking")
        st.markdown("""
        ### Academic Anti-Piracy Demonstration
        - **DSSS Embedding:** Spread spectrum technique for inaudible watermarks.
        - **AES-128 CTR:** Secure identity encryption.
        - **Sync-Lock:** Robust against temporal trimming.
        """)

    # --- SECTION: REGISTER ---
    elif nav == "Register":
        st.subheader("Create New Account")
        u = st.text_input("Username (User ID)")
        e = st.text_input("Email Address")
        ph = st.text_input("Phone Number")
        p = st.text_input("Password", type="password")
        if st.button("Register"):
            conn = sqlite3.connect(DB_NAME)
            try:
                h = bcrypt.hashpw(p.encode(), bcrypt.gensalt())
                conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, ph, h))
                conn.commit()
                st.success("Account created successfully!")
            except: st.error("Username already taken.")
            conn.close()

    # --- SECTION: LOGIN ---
    elif nav == "Login":
        st.subheader("Login to Guardian")
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Log In"):
            conn = sqlite3.connect(DB_NAME)
            res = conn.execute("SELECT password FROM users WHERE username=?", (u,)).fetchone()
            if res and bcrypt.checkpw(p.encode(), res[0]):
                st.session_state.user = u
                st.rerun()
            else: st.error("Invalid credentials.")

    # --- SECTION: UPLOAD ---
    elif nav == "Upload Video":
        st.subheader("Upload Original Master Content")
        up = st.file_uploader("Select Video File (MP4)", type=['mp4'])
        if up and st.button("Save Master"):
            path = os.path.join(UPLOAD_DIR, up.name)
            with open(path, "wb") as f: f.write(up.read())
            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO videos (filename, uploader) VALUES (?,?)", (up.name, st.session_state.user))
            conn.commit()
            st.success("Master video stored safely.")

    # --- SECTION: DOWNLOAD ---
    elif nav == "Download Video":
        st.subheader("Watermarked Content Download")
        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute("SELECT filename FROM videos").fetchall()
        for v in vids:
            col1, col2 = st.columns([4, 1])
            col1.write(f"🎥 {v[0]}")
            if col2.button("Embed & Download", key=v[0]):
                with st.spinner("Applying Sync-Lock Watermark..."):
                    # Embedding Logic
                    bit_str = get_watermark_bits(st.session_state.user)
                    with tempfile.TemporaryDirectory() as tmp:
                        in_v = os.path.join(UPLOAD_DIR, v[0])
                        a_in, a_out, v_out = os.path.join(tmp, "1.wav"), os.path.join(tmp, "2.wav"), os.path.join(tmp, "final.mp4")
                        subprocess.run(["ffmpeg", "-y", "-i", in_v, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_in], capture_output=True)
                        with wave.open(a_in, 'rb') as wav:
                            params = wav.getparams()
                            audio = np.frombuffer(wav.readframes(params.nframes), dtype=np.int16).astype(np.float32)
                        
                        # Apply to 10s blocks
                        b_size = 10 * 44100
                        processed = []
                        for i in range(0, len(audio), b_size):
                            seg = audio[i:i+b_size]
                            processed.append(embed_dss(seg, bit_str) if len(seg) == b_size else seg)
                        
                        final_a = np.concatenate(processed).astype(np.int16)
                        with wave.open(a_out, 'wb') as wav:
                            wav.setparams(params); wav.writeframes(final_a.tobytes())
                        subprocess.run(["ffmpeg", "-y", "-i", in_v, "-i", a_out, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", v_out], capture_output=True)
                        with open(v_out, 'rb') as f:
                            st.download_button("Click here to Download", f.read(), file_name=f"secured_{v[0]}")

    # --- SECTION: DETECT ---
    elif nav == "Detect Piracy":
        st.subheader("Scan for Hidden Watermarks")
        leak = st.file_uploader("Upload Pirated Video", type=['mp4'])
        if leak and st.button("Identify Pirater"):
            with st.spinner("Searching for SYNC Header..."):
                with tempfile.TemporaryDirectory() as tmp:
                    v_p, a_p = os.path.join(tmp, "l.mp4"), os.path.join(tmp, "l.wav")
                    with open(v_p, "wb") as f: f.write(leak.read())
                    subprocess.run(["ffmpeg", "-y", "-i", v_p, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_p], capture_output=True)
                    try:
                        with wave.open(a_p, 'rb') as wav:
                            audio = np.frombuffer(wav.readframes(wav.getparams().nframes), dtype=np.int16).astype(np.float32)
                        
                        found_id = None
                        b_size = 10 * 44100
                        for i in range(0, len(audio) - b_size + 1, b_size):
                            seg = audio[i:i+b_size]
                            pn = get_pn_sequence(len(seg))
                            spb = len(seg) // (8 + 128)
                            bits = "".join(["1" if np.sum(seg[b*spb:(b+1)*spb]*pn[b*spb:(b+1)*spb]) > 0 else "0" for b in range(8+128)])
                            res = recover_from_bits(bits)
                            if res: found_id = res; break
                        
                        if found_id:
                            conn = sqlite3.connect(DB_NAME)
                            u_info = conn.execute("SELECT email, phone FROM users WHERE username=?", (found_id,)).fetchone()
                            st.error(f"🚨 PIRATER DETECTED: {found_id}")
                            if u_info: st.info(f"Contact Details: Email: {u_info[0]} | Phone: {u_info[1]}")
                        else: st.success("No piracy signature detected.")
                    except: st.warning("Error processing file.")

    # --- SECTION: DATABASE ---
    elif nav == "User Database":
        st.subheader("Registered Users Directory")
        conn = sqlite3.connect(DB_NAME)
        st.table(pd.read_sql_query("SELECT username, email, phone FROM users", conn))

if __name__ == "__main__":
    main()
