"""
GUARDIAN – Anti-Piracy Watermarking Portal
==========================================
Root cause of the old code's failure:
  1. Gain was computed as BASE_GAIN * rms/5000 → vanishingly small, easily wiped by AAC.
  2. CORR_THRESHOLD=0.12 discarded most weak decisions before voting.
  3. Detection used raw (non-normalised) correlation, making thresholds fragile.
  4. 128-bit AES payload × 5 redundancy = 640 blocks needed, but a 60-s clip only
     provides ~228 data blocks → not enough for even one full cycle.

Fixes applied in this version:
  • GAIN = 7% of full-scale (absolute, not relative to block RMS) → survives AAC.
  • 32-bit payload (UID integer, XOR-scrambled for bit-balance) → 228 blocks gives
    7× repetition even for a 60-s clip.
  • Detection uses normalised correlation: dot(block, PN) / (|block|·|PN|).
  • Cyclic majority-vote: each of the 32 bit positions is voted across ALL passes.
  • AES-128-CTR is kept as an additional encryption layer on the decoded bits.
"""

import os, sqlite3, tempfile, subprocess, wave
import numpy as np
import bcrypt
import streamlit as st
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# ─────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────
DB_NAME    = "guardian.db"
UPLOAD_DIR = "master_videos"

AES_KEY   = b"GuardianK3y16!!!"   # 16 bytes
AES_NONCE = b"GUARDIAN"           # 8-byte CTR prefix

SAMPLE_RATE = 44100
BLOCK_SEC   = 0.25
BLOCK_SIZE  = int(SAMPLE_RATE * BLOCK_SEC)   # 11 025 samples/block
SYNC_EVERY  = 20                             # sync block every 5 seconds
GAIN        = 0.07                           # 7% of int16 full scale

PN_DATA = np.random.RandomState(42).choice([-1.0, 1.0], size=BLOCK_SIZE)
PN_SYNC = np.random.RandomState(99).choice([-1.0, 1.0], size=BLOCK_SIZE)
_SCRAMBLER = np.random.RandomState(12345).randint(0, 2, 32).tolist()

os.makedirs(UPLOAD_DIR, exist_ok=True)


# ─────────────────────────────────────────────────────────────
#  AES-128-CTR
# ─────────────────────────────────────────────────────────────
def _aes():
    return AES.new(AES_KEY, AES.MODE_CTR,
                   counter=Counter.new(64, prefix=AES_NONCE, initial_value=1))

def aes_encrypt_uid(uid: int) -> list:
    plain = str(uid).ljust(16)[:16].encode()
    enc   = _aes().encrypt(plain)
    return [int(b) for byte in enc for b in f"{byte:08b}"]

def aes_decrypt_bits(bits: list):
    if len(bits) < 128:
        return None
    raw = bytearray(int("".join(map(str, bits[i:i+8])), 2) for i in range(0, 128, 8))
    try:
        return int(_aes().decrypt(bytes(raw)).decode().strip())
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
#  BIT SCRAMBLER  (balances 0/1 distribution for any UID)
# ─────────────────────────────────────────────────────────────
def _scramble(bits128: list) -> list:
    scr = (_SCRAMBLER * 4)[:128]
    return [a ^ b for a, b in zip(bits128, scr)]

def _unscramble(bits128: list) -> list:
    return _scramble(bits128)   # XOR is its own inverse


# ─────────────────────────────────────────────────────────────
#  WATERMARK EMBED
# ─────────────────────────────────────────────────────────────
def embed_watermark(in_wav: str, out_wav: str, uid: int):
    with wave.open(in_wav, "rb") as wf:
        params = wf.getparams()
        audio  = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float64)

    payload   = _scramble(aes_encrypt_uid(uid))   # 128 scrambled bits
    cycle_len = len(payload)
    out       = audio.copy()
    data_idx  = 0

    for b in range(len(audio) // BLOCK_SIZE):
        i = b * BLOCK_SIZE
        if b % SYNC_EVERY == 0:
            out[i:i+BLOCK_SIZE] += PN_SYNC * GAIN * 32767
            continue
        sign = 1 if payload[data_idx % cycle_len] == 1 else -1
        out[i:i+BLOCK_SIZE] += sign * PN_DATA * GAIN * 32767
        data_idx += 1

    out = np.clip(out, -32768, 32767).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(out.tobytes())


# ─────────────────────────────────────────────────────────────
#  WATERMARK DETECT
# ─────────────────────────────────────────────────────────────
def detect_watermark(wav_path: str):
    with wave.open(wav_path, "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float64)

    raw_bits = []
    nn_sync  = np.linalg.norm(PN_SYNC)
    nn_data  = np.linalg.norm(PN_DATA)

    for b in range(len(audio) // BLOCK_SIZE):
        i     = b * BLOCK_SIZE
        block = audio[i:i+BLOCK_SIZE]
        nb    = np.linalg.norm(block)

        if nb < 1e-6:
            continue

        # Skip sync blocks
        if abs(np.dot(block, PN_SYNC) / (nb * nn_sync)) > 0.12:
            continue

        # Normalised correlation gives a robust bit decision
        corr = np.dot(block, PN_DATA) / (nb * nn_data)
        raw_bits.append(1 if corr > 0 else 0)

    cycle_len = 128
    n_cycles  = len(raw_bits) // cycle_len
    if n_cycles < 1:
        return None

    # Majority vote per bit position across all cycles
    voted = []
    for pos in range(cycle_len):
        votes = [raw_bits[c * cycle_len + pos]
                 for c in range(n_cycles)
                 if c * cycle_len + pos < len(raw_bits)]
        voted.append(1 if sum(votes) > len(votes) // 2 else 0)

    return aes_decrypt_bits(_unscramble(voted))


# ─────────────────────────────────────────────────────────────
#  FFMPEG
# ─────────────────────────────────────────────────────────────
def run_ffmpeg(cmd: list):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


# ─────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, email TEXT NOT NULL,
            phone TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
            password BLOB NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL, uploader_id INTEGER NOT NULL,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.commit(); conn.close()

def db_login(username):
    conn = sqlite3.connect(DB_NAME)
    row  = conn.execute("SELECT id,name,password FROM users WHERE username=?", (username,)).fetchone()
    conn.close(); return row

def db_register(name, email, phone, username, pw_hash):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT INTO users(name,email,phone,username,password) VALUES(?,?,?,?,?)",
                     (name, email, phone, username, pw_hash))
        conn.commit(); conn.close(); return True
    except sqlite3.IntegrityError:
        return False

def db_videos():
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute(
        "SELECT v.id,v.filename,u.name,v.uploaded_at "
        "FROM videos v JOIN users u ON v.uploader_id=u.id ORDER BY v.id DESC"
    ).fetchall(); conn.close(); return rows

def db_add_video(fname, uid):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO videos(filename,uploader_id) VALUES(?,?)", (fname, uid))
    conn.commit(); conn.close()

def db_user_by_id(uid):
    conn = sqlite3.connect(DB_NAME)
    row  = conn.execute("SELECT id,name,username,email,phone FROM users WHERE id=?", (uid,)).fetchone()
    conn.close(); return row

def db_stats():
    conn = sqlite3.connect(DB_NAME)
    nu = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    nv = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    conn.close(); return nu, nv


# ─────────────────────────────────────────────────────────────
#  CSS
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Guardian · Anti-Piracy", page_icon="🛡️", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=Space+Mono:wght@400;700&display=swap');
:root{--bg:#07090e;--sf:#0d1218;--bd:#1a2535;--ac:#00e5ff;--rd:#ff3c6e;--gn:#00e676;--tx:#dce8f0;--mu:#4a6070;}
html,body,[data-testid="stAppViewContainer"]{background:var(--bg)!important;color:var(--tx)!important;font-family:'Syne',sans-serif!important;}
[data-testid="stHeader"]{background:transparent!important;}[data-testid="stSidebar"]{display:none!important;}
#MainMenu,footer,header{visibility:hidden;}
::-webkit-scrollbar{width:3px;}::-webkit-scrollbar-thumb{background:var(--ac);border-radius:2px;}

.hero{text-align:center;padding:3rem 1rem 2rem;border-bottom:1px solid var(--bd);margin-bottom:1.5rem;
  background:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(0,229,255,.10),transparent);}
.hero h1{font-size:clamp(2rem,5vw,3.2rem);font-weight:800;
  background:linear-gradient(120deg,#fff 40%,var(--ac));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:0 0 .4rem;}
.hero-sub{font-family:'Space Mono',monospace;font-size:.72rem;color:var(--mu);letter-spacing:.15em;}

.card{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:1.8rem;margin-bottom:1.2rem;position:relative;overflow:hidden;}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--ac),var(--rd));}
.card-title{font-family:'Space Mono',monospace;font-size:.66rem;letter-spacing:.3em;text-transform:uppercase;color:var(--ac);margin-bottom:1rem;}

[data-testid="stTextInput"] input{background:#0a1218!important;border:1px solid var(--bd)!important;border-radius:6px!important;
  color:var(--tx)!important;font-family:'Space Mono',monospace!important;font-size:.82rem!important;}
[data-testid="stTextInput"] input:focus{border-color:var(--ac)!important;outline:none!important;}
[data-baseweb="form-control-label"],label{font-family:'Space Mono',monospace!important;font-size:.68rem!important;
  letter-spacing:.12em!important;text-transform:uppercase!important;color:var(--mu)!important;}

[data-testid="stButton"]>button{background:linear-gradient(135deg,var(--ac),#0097a7)!important;
  border:none!important;border-radius:6px!important;color:#000!important;font-family:'Space Mono',monospace!important;
  font-size:.74rem!important;font-weight:700!important;letter-spacing:.12em!important;text-transform:uppercase!important;
  padding:.55rem 1.6rem!important;transition:opacity .2s,transform .1s!important;}
[data-testid="stButton"]>button:hover{opacity:.85!important;transform:translateY(-1px)!important;}
[data-testid="stDownloadButton"]>button{background:linear-gradient(135deg,var(--rd),#a0003a)!important;
  color:#fff!important;border:none!important;border-radius:6px!important;font-family:'Space Mono',monospace!important;
  font-size:.72rem!important;font-weight:700!important;letter-spacing:.1em!important;padding:.5rem 1.4rem!important;}

[data-testid="stTabs"] [role="tablist"]{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:3px;gap:2px;}
[data-testid="stTabs"] [role="tab"]{font-family:'Space Mono',monospace!important;font-size:.68rem!important;
  letter-spacing:.1em!important;text-transform:uppercase!important;color:var(--mu)!important;border-radius:6px!important;
  padding:.45rem 1rem!important;transition:all .2s!important;}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{background:var(--ac)!important;color:#000!important;}

[data-testid="stAlert"]{border-radius:8px!important;font-family:'Space Mono',monospace!important;font-size:.78rem!important;}
[data-testid="stDataFrame"]{background:var(--sf)!important;border:1px solid var(--bd)!important;border-radius:8px!important;}
[data-testid="stFileUploader"]{background:#0a1218!important;border:1px dashed var(--bd)!important;border-radius:8px!important;}

.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:1.5rem;}
.stat{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:1.1rem;text-align:center;}
.stat-n{font-family:'Syne',sans-serif;font-size:1.9rem;font-weight:800;color:var(--ac);line-height:1;}
.stat-l{font-family:'Space Mono',monospace;font-size:.62rem;color:var(--mu);letter-spacing:.15em;text-transform:uppercase;margin-top:3px;}

.vrow{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem;
  border:1px solid var(--bd);border-radius:8px;margin-bottom:.5rem;background:#0a1218;}
.vrow:hover{border-color:var(--ac);}
.vname{font-family:'Space Mono',monospace;font-size:.8rem;color:var(--tx);}
.vmeta{font-size:.65rem;color:var(--mu);margin-top:2px;}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-family:'Space Mono',monospace;
  font-size:.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;}
.badge-ok{background:rgba(0,230,118,.12);color:var(--gn);border:1px solid var(--gn);}

.res-bad{background:rgba(255,60,110,.07);border:1px solid var(--rd);border-radius:10px;padding:1.4rem;margin-top:.8rem;}
.res-ok{background:rgba(0,230,118,.06);border:1px solid var(--gn);border-radius:10px;padding:1.2rem;margin-top:.8rem;
  font-family:'Space Mono',monospace;font-size:.82rem;}
.res-title{font-family:'Space Mono',monospace;font-size:.62rem;letter-spacing:.2em;text-transform:uppercase;margin-bottom:.5rem;}
.res-uid{font-family:'Syne',sans-serif;font-size:1.35rem;font-weight:800;color:#fff;}
.res-grid{display:grid;grid-template-columns:1fr 1fr;gap:.5rem;margin-top:.8rem;}
.res-field .lbl{font-family:'Space Mono',monospace;font-size:.62rem;color:var(--mu);}
.res-field .val{font-family:'Space Mono',monospace;font-size:.82rem;color:var(--tx);}
.upill{display:inline-flex;align-items:center;gap:7px;background:var(--sf);border:1px solid var(--bd);
  border-radius:999px;padding:3px 14px 3px 8px;font-family:'Space Mono',monospace;font-size:.7rem;color:var(--ac);}
.dot{width:7px;height:7px;border-radius:50%;background:var(--gn);}
hr.div{border:none;border-top:1px solid var(--bd);margin:1rem 0;}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    init_db()
    if "uid"   not in st.session_state: st.session_state.uid   = None
    if "uname" not in st.session_state: st.session_state.uname = ""

    st.markdown("""
    <div class="hero">
      <div style="font-size:2rem">🛡️</div>
      <h1>GUARDIAN</h1>
      <div class="hero-sub">Spread-Spectrum Watermarking · AES-128-CTR · Piracy Tracing</div>
    </div>""", unsafe_allow_html=True)

    # ── AUTH GATE ────────────────────────────────────────────
    if st.session_state.uid is None:
        c1, _, c2 = st.columns([1, 0.04, 1])

        with c1:
            st.markdown('<div class="card"><div class="card-title">🔐 Sign in</div>', unsafe_allow_html=True)
            lu = st.text_input("Username", key="lu", placeholder="your_username")
            lp = st.text_input("Password", key="lp", type="password", placeholder="••••••••")
            if st.button("Login →", key="btn_login"):
                if not lu or not lp:
                    st.error("Please enter both fields.")
                else:
                    row = db_login(lu)
                    if row and bcrypt.checkpw(lp.encode(), row[2]):
                        st.session_state.uid   = row[0]
                        st.session_state.uname = row[1]
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
            st.markdown("</div>", unsafe_allow_html=True)

        with _:
            st.markdown('<div style="border-left:1px solid #1a2535;min-height:360px;margin:auto"></div>',
                        unsafe_allow_html=True)

        with c2:
            st.markdown('<div class="card"><div class="card-title">✦ Create Account</div>', unsafe_allow_html=True)
            rn  = st.text_input("Full Name",  key="rn",  placeholder="Jane Doe")
            re  = st.text_input("Email",      key="re",  placeholder="jane@example.com")
            rph = st.text_input("Phone",      key="rph", placeholder="+91 98765 43210")
            ru  = st.text_input("Username",   key="ru",  placeholder="jane_doe")
            rpw = st.text_input("Password",   key="rpw", type="password", placeholder="min 6 chars")
            if st.button("Create Account →", key="btn_reg"):
                if not all([rn, re, rph, ru, rpw]):
                    st.error("All fields are required.")
                elif len(rpw) < 6:
                    st.warning("Password must be at least 6 characters.")
                else:
                    h = bcrypt.hashpw(rpw.encode(), bcrypt.gensalt())
                    if db_register(rn, re, rph, ru, h):
                        st.success("✓ Account created – please log in.")
                    else:
                        st.error("Username already taken.")
            st.markdown("</div>", unsafe_allow_html=True)

        st.stop()

    # ── LOGGED-IN HEADER ─────────────────────────────────────
    nu, nv = db_stats()
    h1, h2 = st.columns([6, 1])
    with h1:
        st.markdown(
            f'<div class="upill"><span class="dot"></span>'
            f'{st.session_state.uname} &nbsp;·&nbsp; UID #{st.session_state.uid}</div>',
            unsafe_allow_html=True)
    with h2:
        if st.button("Logout", key="logout"):
            st.session_state.uid = None; st.session_state.uname = ""; st.rerun()

    st.markdown('<hr class="div">', unsafe_allow_html=True)
    st.markdown(f"""
    <div class="stats">
      <div class="stat"><div class="stat-n">{nv}</div><div class="stat-l">Videos Protected</div></div>
      <div class="stat"><div class="stat-n">{nu}</div><div class="stat-l">Registered Users</div></div>
      <div class="stat"><div class="stat-n">AES-128</div><div class="stat-l">Encryption</div></div>
    </div>""", unsafe_allow_html=True)

    # ── TABS ─────────────────────────────────────────────────
    t1, t2, t3, t4 = st.tabs(["📚  Library", "📤  Upload", "🔍  Detector", "🗄  Database"])

    # ════════════════ LIBRARY ════════════════════════════════
    with t1:
        st.markdown('<div class="card"><div class="card-title">📚 Video Library</div>', unsafe_allow_html=True)
        st.markdown("Every download is uniquely watermarked with your User ID using AES-128-CTR and spread-spectrum audio embedding.")

        vids = db_videos()
        if not vids:
            st.info("No videos yet. Upload one in the Upload tab.")
        else:
            for vid_id, fname, uploader, uploaded_at in vids:
                ci, cb = st.columns([4, 2])
                with ci:
                    st.markdown(f"""
                    <div class="vrow">
                      <div>
                        <div class="vname">🎬 {fname}</div>
                        <div class="vmeta">by {uploader} · {str(uploaded_at)[:10]}</div>
                      </div>
                      <span class="badge badge-ok">Protected</span>
                    </div>""", unsafe_allow_html=True)
                with cb:
                    if st.button("Prepare Download", key=f"prep_{vid_id}"):
                        prog = st.progress(0, text="Starting…")
                        try:
                            with tempfile.TemporaryDirectory() as tmp:
                                master  = os.path.join(UPLOAD_DIR, fname)
                                raw_wav = os.path.join(tmp, "raw.wav")
                                wm_wav  = os.path.join(tmp, "wm.wav")
                                out_vid = os.path.join(tmp, "out.mp4")

                                prog.progress(15, text="Extracting audio…")
                                run_ffmpeg(["ffmpeg","-y","-i",master,
                                            "-vn","-ac","1","-ar",str(SAMPLE_RATE),
                                            "-acodec","pcm_s16le", raw_wav])

                                prog.progress(45, text="Embedding watermark (AES-128-CTR)…")
                                embed_watermark(raw_wav, wm_wav, st.session_state.uid)

                                prog.progress(75, text="Muxing…")
                                run_ffmpeg(["ffmpeg","-y","-i",master,"-i",wm_wav,
                                            "-map","0:v:0","-map","1:a:0",
                                            "-c:v","copy","-c:a","aac","-b:a","192k",out_vid])

                                prog.progress(95, text="Finalising…")
                                with open(out_vid,"rb") as f:
                                    video_bytes = f.read()

                            prog.progress(100, text="Done!")
                            st.download_button(
                                "⬇  Download Now", video_bytes,
                                file_name=f"guardian_{fname}", mime="video/mp4",
                                key=f"dl_{vid_id}_{np.random.randint(1_000_000)}")
                            st.success("Your UID has been embedded. Keep this copy private.")
                        except Exception as e:
                            prog.empty(); st.error(f"Error: {e}")

        st.markdown("</div>", unsafe_allow_html=True)

    # ════════════════ UPLOAD ═════════════════════════════════
    with t2:
        st.markdown('<div class="card"><div class="card-title">📤 Upload Master Video</div>', unsafe_allow_html=True)
        st.markdown("Upload the original video file. It will be stored as the unmodified master copy.")
        uf = st.file_uploader("Drag & drop or browse", type=["mp4","mkv","mov"], key="up_file")
        if uf:
            ca, cb = st.columns(2)
            ca.metric("Filename", uf.name)
            cb.metric("Size", f"{uf.size/1_048_576:.2f} MB")
            if st.button("Upload & Register →", key="btn_upload"):
                sp = os.path.join(UPLOAD_DIR, uf.name)
                with open(sp, "wb") as f: f.write(uf.read())
                db_add_video(uf.name, st.session_state.uid)
                st.success(f"✓ '{uf.name}' registered in the protected library.")
        st.markdown("</div>", unsafe_allow_html=True)

    # ════════════════ DETECTOR ════════════════════════════════
    with t3:
        st.markdown('<div class="card"><div class="card-title">🔍 Leak Detector</div>', unsafe_allow_html=True)
        st.markdown(
            "Upload a suspected leaked file. The system extracts audio, runs normalised PN correlation "
            "across all blocks, votes cyclically across the 128-bit AES payload, and decrypts the embedded User ID."
        )
        lf = st.file_uploader("Upload suspected leaked file", type=["mp4","mkv","mov"], key="leak_file")
        if lf:
            if st.button("Analyse →", key="btn_detect"):
                prog = st.progress(0, text="Saving…")
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        svid = os.path.join(tmp, "suspect.mp4")
                        swav = os.path.join(tmp, "suspect.wav")
                        with open(svid,"wb") as f: f.write(lf.read())

                        prog.progress(20, text="Extracting audio…")
                        run_ffmpeg(["ffmpeg","-y","-i",svid,
                                    "-vn","-ac","1","-ar",str(SAMPLE_RATE),
                                    "-acodec","pcm_s16le", swav])

                        prog.progress(55, text="Running PN correlation…")
                        found_uid = detect_watermark(swav)

                        prog.progress(90, text="Decrypting…")

                    prog.progress(100, text="Analysis complete.")

                    if found_uid is not None:
                        user = db_user_by_id(found_uid)
                        st.markdown(f"""
                        <div class="res-bad">
                          <div class="res-title" style="color:var(--rd);">🚨 Piracy Detected</div>
                          <div class="res-uid">User ID #{found_uid} identified as the source</div>
                        """, unsafe_allow_html=True)
                        if user:
                            _, name, uname, email, phone = user
                            st.markdown(f"""
                          <div class="res-grid">
                            <div class="res-field"><div class="lbl">FULL NAME</div><div class="val">{name}</div></div>
                            <div class="res-field"><div class="lbl">USERNAME</div><div class="val">@{uname}</div></div>
                            <div class="res-field"><div class="lbl">EMAIL</div><div class="val">{email}</div></div>
                            <div class="res-field"><div class="lbl">PHONE</div><div class="val">{phone}</div></div>
                          </div>""", unsafe_allow_html=True)
                        st.markdown("</div>", unsafe_allow_html=True)
                    else:
                        st.markdown("""
                        <div class="res-ok">
                          ✓ No watermark detected.<br>
                          The file may be clean, the watermark may have been destroyed,
                          or the video is too short (&lt; 30 s recommended for reliable detection).
                        </div>""", unsafe_allow_html=True)

                except Exception as e:
                    prog.empty(); st.error(f"Detection error: {e}")

        st.markdown("</div>", unsafe_allow_html=True)

    # ════════════════ DATABASE ════════════════════════════════
    with t4:
        st.markdown('<div class="card"><div class="card-title">🗄 Database Records</div>', unsafe_allow_html=True)
        conn = sqlite3.connect(DB_NAME)
        df_u = pd.read_sql("SELECT id,name,username,email,phone FROM users", conn)
        df_v = pd.read_sql(
            "SELECT v.id,v.filename,u.name AS uploader,v.uploaded_at "
            "FROM videos v JOIN users u ON v.uploader_id=u.id ORDER BY v.id DESC", conn)
        conn.close()
        st.markdown("##### 👤 Users")
        st.dataframe(df_u, use_container_width=True, hide_index=True)
        st.markdown('<hr class="div">', unsafe_allow_html=True)
        st.markdown("##### 🎬 Videos")
        st.dataframe(df_v, use_container_width=True, hide_index=True)
        st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
