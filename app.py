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

GAIN_FACTOR = 0.035
SAMPLES_PER_BIT = 256

SYNC_BITS = "10101011110011010011101010101100"  # 32 bits
PAYLOAD_BITS = 128
FRAME_BITS = len(SYNC_BITS) + PAYLOAD_BITS
FRAME_SIZE = FRAME_BITS * SAMPLES_PER_BIT
STEP = FRAME_SIZE // 3

# =========================================
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ================= DATABASE =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users
        (username TEXT PRIMARY KEY, email TEXT, phone TEXT, password BLOB)""")
    c.execute("""CREATE TABLE IF NOT EXISTS videos
        (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, uploader TEXT)""")
    conn.commit()
    conn.close()

# ================= PN =================
def get_pn_sequence():
    np.random.seed(42)
    return (np.random.randint(0, 2, SAMPLES_PER_BIT) * 2 - 1).astype(np.float32)

PN_SEQ = get_pn_sequence()

# ================= CRYPTO =================
def get_watermark_bits(user_id):
    ctr = Counter.new(64, prefix=b"GUARDIAN", initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    enc = cipher.encrypt(user_id.ljust(16).encode())
    payload = "".join(format(b, "08b") for b in enc)
    return SYNC_BITS + payload

def decrypt_payload(bit_str):
    try:
        data = bytes(int(bit_str[i:i+8], 2) for i in range(0, 128, 8))
        ctr = Counter.new(64, prefix=b"GUARDIAN", initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(data).decode().strip()
    except:
        return None

# ================= EMBED =================
def embed_frame(audio, bits):
    wm = np.zeros(FRAME_SIZE, dtype=np.float32)
    for i, bit in enumerate(bits):
        s = i * SAMPLES_PER_BIT
        e = s + SAMPLES_PER_BIT
        wm[s:e] = (1 if bit == "1" else -1) * PN_SEQ
    rms = np.sqrt(np.mean(audio[:FRAME_SIZE] ** 2)) + 1e-6
    audio[:FRAME_SIZE] += GAIN_FACTOR * wm * rms
    return audio

# ================= DETECT =================
def detect_watermark(audio):
    found = []
    for i in range(0, len(audio) - FRAME_SIZE, STEP):
        seg = audio[i:i+FRAME_SIZE]
        bits = ""
        for b in range(FRAME_BITS):
            s = seg[b*SAMPLES_PER_BIT:(b+1)*SAMPLES_PER_BIT]
            bits += "1" if np.sum(s * PN_SEQ) > 0 else "0"
        if bits.startswith(SYNC_BITS):
            uid = decrypt_payload(bits[len(SYNC_BITS):len(SYNC_BITS)+128])
            if uid:
                found.append(uid)
    return found

# ================= UI =================
def main():
    st.set_page_config("Guardian v8 Anti-Piracy", layout="wide")
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    st.sidebar.title("🛡️ GUARDIAN v8")

    if st.session_state.user:
        nav = st.sidebar.radio("Menu",
            ["Home","Upload Video","Download Secured Video","Detect Piracy","User Database"])
        if st.sidebar.button("Logout"):
            st.session_state.user = None
            st.rerun()
    else:
        nav = st.sidebar.radio("Menu",["Home","Login","Register"])

    # ===== HOME =====
    if nav == "Home":
        st.title("Guardian v8 – DSSS Anti-Piracy System")
        st.write("Final academic-grade trim-invariant watermarking system")

    # ===== REGISTER =====
    elif nav == "Register":
        u = st.text_input("Username")
        e = st.text_input("Email")
        pno = st.text_input("Phone")
        p = st.text_input("Password", type="password")
        if st.button("Register"):
            try:
                h = bcrypt.hashpw(p.encode(), bcrypt.gensalt())
                sqlite3.connect(DB_NAME).execute(
                    "INSERT INTO users VALUES (?,?,?,?)",(u,e,pno,h))
                st.success("Registered")
            except:
                st.error("Username exists")

    # ===== LOGIN =====
    elif nav == "Login":
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Login"):
            r = sqlite3.connect(DB_NAME).execute(
                "SELECT password FROM users WHERE username=?",(u,)).fetchone()
            if r and bcrypt.checkpw(p.encode(), r[0]):
                st.session_state.user = u
                st.rerun()
            else:
                st.error("Invalid login")

    # ===== UPLOAD =====
    elif nav == "Upload Video":
        up = st.file_uploader("Upload MP4", type=["mp4"])
        if up and st.button("Save"):
            path = os.path.join(UPLOAD_DIR, up.name)
            open(path,"wb").write(up.read())
            sqlite3.connect(DB_NAME).execute(
                "INSERT INTO videos (filename,uploader) VALUES (?,?)",
                (up.name, st.session_state.user))
            st.success("Video stored")

    # ===== DOWNLOAD =====
    elif nav == "Download Secured Video":
        vids = sqlite3.connect(DB_NAME).execute(
            "SELECT filename FROM videos").fetchall()
        for idx,v in enumerate(vids):
            if st.button(f"Secure & Download – {v[0]}", key=f"dl_{idx}"):
                bits = get_watermark_bits(st.session_state.user)
                with tempfile.TemporaryDirectory() as tmp:
                    a_in,a_out,v_out = tmp+"/a.wav",tmp+"/b.wav",tmp+"/f.mp4"
                    subprocess.run(["ffmpeg","-y","-i",
                        os.path.join(UPLOAD_DIR,v[0]),
                        "-vn","-acodec","pcm_s16le","-ar","44100",a_in])
                    w = wave.open(a_in)
                    audio = np.frombuffer(
                        w.readframes(w.getnframes()),
                        dtype=np.int16).astype(np.float32)
                    for i in range(0,len(audio)-FRAME_SIZE,FRAME_SIZE):
                        audio[i:i+FRAME_SIZE] = embed_frame(audio[i:i+FRAME_SIZE],bits)
                    w2 = wave.open(a_out,"wb")
                    w2.setparams(w.getparams())
                    w2.writeframes(audio.astype(np.int16))
                    subprocess.run(["ffmpeg","-y","-i",
                        os.path.join(UPLOAD_DIR,v[0]),"-i",a_out,
                        "-map","0:v:0","-map","1:a:0",
                        "-c:v","copy","-c:a","aac","-b:a","320k",v_out])
                    st.download_button("Download",open(v_out,"rb").read(),
                        file_name="secured_"+v[0])

    # ===== DETECT =====
    elif nav == "Detect Piracy":
        leak = st.file_uploader("Upload Video", type=["mp4"])
        if leak and st.button("Scan"):
            with tempfile.TemporaryDirectory() as tmp:
                v_p,a_p = tmp+"/v.mp4",tmp+"/a.wav"
                open(v_p,"wb").write(leak.read())
                subprocess.run(["ffmpeg","-y","-i",v_p,
                    "-vn","-acodec","pcm_s16le","-ar","44100",a_p])
                w = wave.open(a_p)
                audio = np.frombuffer(
                    w.readframes(w.getnframes()),
                    dtype=np.int16).astype(np.float32)
                ids = detect_watermark(audio)
                if len(ids)>=3:
                    final = VoteCounter(ids).most_common(1)[0][0]
                    info = sqlite3.connect(DB_NAME).execute(
                        "SELECT email,phone FROM users WHERE username=?",
                        (final,)).fetchone()
                    st.error(f"🚨 PIRACY DETECTED : {final}")
                    st.info(f"Email: {info[0]} | Phone: {info[1]}")
                else:
                    st.success("No piracy signature detected")

    # ===== USERS =====
    elif nav == "User Database":
        df = pd.read_sql("SELECT username,email,phone FROM users",
                         sqlite3.connect(DB_NAME))
        st.dataframe(df,use_container_width=True)

if __name__ == "__main__":
    main()
