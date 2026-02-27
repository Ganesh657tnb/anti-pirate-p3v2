import streamlit as st
import numpy as np
import wave, os, sqlite3, tempfile, subprocess, bcrypt
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter as AESCounter
from collections import Counter as VoteCounter

# ================= CONFIG =================
DB_NAME = "guardian_v8.db"
UPLOAD_DIR = "master_videos"
SECRET_KEY = b"SixteenByteKey!!"
GAIN = 0.025
FRAME = 65536  # ~1.5 sec

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ================= DB =================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            username TEXT PRIMARY KEY,
            email TEXT,
            phone TEXT,
            password BLOB
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS videos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            uploader TEXT
        )
    """)
    conn.commit()
    conn.close()

# ================= WATERMARK =================
def get_bits(uid):
    ctr = AESCounter.new(64, prefix=b"GUARDIAN", initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    data = uid.ljust(16).encode()
    enc = cipher.encrypt(data)
    return "".join(f"{b:08b}" for b in enc)

def decode_bits(bitstr):
    try:
        raw = bytes(int(bitstr[i:i+8], 2) for i in range(0, 128, 8))
        ctr = AESCounter.new(64, prefix=b"GUARDIAN", initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(raw).decode().strip()
    except:
        return None

def pn_seq(n):
    np.random.seed(42)
    return (np.random.randint(0, 2, n) * 2 - 1).astype(np.float32)

def embed(audio, bits):
    pn = pn_seq(len(audio))
    spb = len(audio) // len(bits)
    wm = np.zeros(len(audio))
    for i, b in enumerate(bits):
        v = 1 if b == "1" else -1
        wm[i*spb:(i+1)*spb] = v * pn[i*spb:(i+1)*spb]
    rms = np.sqrt(np.mean(audio**2)) + 1e-6
    return np.clip(audio + GAIN * wm * rms, -32768, 32767)

# ================= APP =================
def main():
    st.set_page_config("Guardian v8", layout="wide")
    init_db()

    if "user" not in st.session_state:
        st.session_state.user = None

    st.sidebar.title("🛡️ GUARDIAN v8")

    if st.session_state.user:
        menu = st.sidebar.radio("Menu", [
            "Home", "Upload", "Download", "Detect", "Users"
        ])
        if st.sidebar.button("Logout", key="logout"):
            st.session_state.user = None
            st.rerun()
    else:
        menu = st.sidebar.radio("Menu", ["Home", "Login", "Register"])

    # ================= HOME =================
    if menu == "Home":
        st.title("Guardian Anti-Piracy System")
        st.info("DSSS + AES + Trim-Resistant Sliding Window")

    # ================= REGISTER =================
    elif menu == "Register":
        st.subheader("Register")
        u = st.text_input("Username")
        e = st.text_input("Email")
        ph = st.text_input("Phone")
        p = st.text_input("Password", type="password")

        if st.button("Create Account", key="reg"):
            h = bytes(bcrypt.hashpw(p.encode(), bcrypt.gensalt()))
            try:
                conn = sqlite3.connect(DB_NAME)
                conn.execute("INSERT INTO users VALUES (?,?,?,?)", (u, e, ph, h))
                conn.commit()
                conn.close()
                st.success("Registered successfully ✅")
            except:
                st.error("Username already exists")

    # ================= LOGIN =================
    elif menu == "Login":
        st.subheader("Login")
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")

        if st.button("Login", key="login"):
            conn = sqlite3.connect(DB_NAME)
            cur = conn.cursor()
            cur.execute("SELECT password FROM users WHERE username=?", (u,))
            row = cur.fetchone()
            conn.close()

            if not row:
                st.error("User not found")
            else:
                h = row[0]
                if isinstance(h, memoryview):
                    h = h.tobytes()
                if bcrypt.checkpw(p.encode(), h):
                    st.session_state.user = u
                    st.success("Login success ✅")
                    st.rerun()
                else:
                    st.error("Invalid credentials ❌")

    # ================= UPLOAD =================
    elif menu == "Upload":
        st.subheader("Upload Master Video")
        f = st.file_uploader("MP4 file", type=["mp4"])
        if f and st.button("Upload", key="upload"):
            path = os.path.join(UPLOAD_DIR, f.name)
            with open(path, "wb") as w:
                w.write(f.read())
            conn = sqlite3.connect(DB_NAME)
            conn.execute("INSERT INTO videos(filename,uploader) VALUES (?,?)",
                         (f.name, st.session_state.user))
            conn.commit()
            conn.close()
            st.success("Uploaded")

    # ================= DOWNLOAD =================
    elif menu == "Download":
        st.subheader("Secure Download")
        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute("SELECT filename FROM videos").fetchall()
        conn.close()

        for i, v in enumerate(vids):
            if st.button(f"Secure & Download {v[0]}", key=f"dl_{i}"):
                bits = get_bits(st.session_state.user)
                with tempfile.TemporaryDirectory() as tmp:
                    in_v = os.path.join(UPLOAD_DIR, v[0])
                    a1 = os.path.join(tmp, "a.wav")
                    a2 = os.path.join(tmp, "b.wav")
                    out = os.path.join(tmp, "out.mp4")

                    subprocess.run(["ffmpeg","-y","-i",in_v,"-vn",
                                    "-acodec","pcm_s16le","-ar","44100",a1],
                                   capture_output=True)

                    with wave.open(a1,'rb') as w:
                        audio = np.frombuffer(w.readframes(w.getnframes()),
                                               dtype=np.int16).astype(np.float32)
                        params = w.getparams()

                    out_audio = []
                    for j in range(0,len(audio),FRAME):
                        seg = audio[j:j+FRAME]
                        out_audio.append(embed(seg,bits) if len(seg)==FRAME else seg)

                    final = np.concatenate(out_audio).astype(np.int16)

                    with wave.open(a2,'wb') as w:
                        w.setparams(params)
                        w.writeframes(final.tobytes())

                    subprocess.run(["ffmpeg","-y","-i",in_v,"-i",a2,
                                    "-map","0:v","-map","1:a",
                                    "-c:v","copy","-c:a","aac",out],
                                   capture_output=True)

                    with open(out,"rb") as f:
                        st.download_button("Download",
                                           f.read(),
                                           file_name="secured_"+v[0],
                                           key=f"down_{i}")

    # ================= DETECT =================
    elif menu == "Detect":
        st.subheader("Detect Piracy")
        f = st.file_uploader("Upload leaked video", type=["mp4"])
        if f and st.button("Scan", key="scan"):
            with tempfile.TemporaryDirectory() as tmp:
                v = os.path.join(tmp,"l.mp4")
                a = os.path.join(tmp,"l.wav")
                with open(v,"wb") as w:
                    w.write(f.read())

                subprocess.run(["ffmpeg","-y","-i",v,"-vn",
                                "-acodec","pcm_s16le","-ar","44100",a],
                               capture_output=True)

                with wave.open(a,'rb') as w:
                    audio = np.frombuffer(w.readframes(w.getnframes()),
                                           dtype=np.int16).astype(np.float32)

                pn = pn_seq(FRAME)
                spb = FRAME // 128
                found = []

                for i in range(0,len(audio)-FRAME,FRAME//4):
                    seg = audio[i:i+FRAME]
                    bits = "".join(
                        "1" if np.sum(seg[b*spb:(b+1)*spb] *
                        pn[b*spb:(b+1)*spb]) > 0 else "0"
                        for b in range(128)
                    )
                    uid = decode_bits(bits)
                    if uid:
                        found.append(uid)

                if found:
                    uid = VoteCounter(found).most_common(1)[0][0]
                    conn = sqlite3.connect(DB_NAME)
                    info = conn.execute(
                        "SELECT email,phone FROM users WHERE username=?",
                        (uid,)
                    ).fetchone()
                    conn.close()
                    st.error(f"🚨 PIRACY DETECTED: {uid}")
                    if info:
                        st.warning(f"Email: {info[0]} | Phone: {info[1]}")
                else:
                    st.success("No piracy signature found")

    # ================= USERS =================
    elif menu == "Users":
        conn = sqlite3.connect(DB_NAME)
        df = pd.read_sql("SELECT username,email,phone FROM users", conn)
        conn.close()
        st.dataframe(df, use_container_width=True)

# ================= RUN =================
if __name__ == "__main__":
    main()
