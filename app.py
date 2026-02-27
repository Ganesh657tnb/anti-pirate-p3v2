import os
import sqlite3
import tempfile
import subprocess
import numpy as np
import wave
import bcrypt
import streamlit as st
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
DB_NAME        = "guardian.db"
UPLOAD_DIR     = "master_videos"

AES_KEY        = b"GuardianKey16!!!"   # 16 bytes
AES_PREFIX     = b"GUARDIAN"           # 8  bytes

SAMPLE_RATE    = 44100
BLOCK_SEC      = 0.25
BLOCK_SIZE     = int(SAMPLE_RATE * BLOCK_SEC)

SYNC_INTERVAL_SEC    = 5
SYNC_INTERVAL_BLOCKS = int(SYNC_INTERVAL_SEC / BLOCK_SEC)

BASE_GAIN  = 0.025
MIN_GAIN   = 0.010
SYNC_GAIN  = 0.06
REDUNDANCY = 5
CORR_THRESHOLD = 0.12

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ─────────────────────────────────────────────
#  PAGE CONFIG & GLOBAL CSS
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Guardian · Anti-Piracy",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap');

/* ── ROOT PALETTE ── */
:root {
  --bg:        #080c10;
  --surface:   #0e1520;
  --border:    #1c2a3a;
  --accent:    #00e5ff;
  --accent2:   #ff3c6e;
  --text:      #e8f0f8;
  --muted:     #5a7080;
  --success:   #00e676;
  --warning:   #ffd740;
}

/* ── GLOBAL ── */
html, body, [data-testid="stAppViewContainer"] {
  background: var(--bg) !important;
  color: var(--text) !important;
  font-family: 'Syne', sans-serif !important;
}

[data-testid="stHeader"] { background: transparent !important; }
[data-testid="stSidebar"] { display: none !important; }

/* ── HIDE DEFAULT STREAMLIT CHROME ── */
#MainMenu, footer, header { visibility: hidden; }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: var(--surface); }
::-webkit-scrollbar-thumb { background: var(--accent); border-radius: 2px; }

/* ── HERO BANNER ── */
.hero {
  position: relative;
  padding: 3rem 2rem 2rem;
  text-align: center;
  overflow: hidden;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}
.hero::before {
  content: '';
  position: absolute; inset: 0;
  background:
    radial-gradient(ellipse 70% 60% at 50% -10%, rgba(0,229,255,.12), transparent),
    radial-gradient(ellipse 40% 30% at 80%  80%, rgba(255,60,110,.08), transparent);
  pointer-events: none;
}
.hero-logo {
  font-family: 'Space Mono', monospace;
  font-size: 0.75rem;
  letter-spacing: 0.35em;
  color: var(--accent);
  text-transform: uppercase;
  margin-bottom: 0.6rem;
}
.hero h1 {
  font-family: 'Syne', sans-serif;
  font-size: clamp(2rem, 5vw, 3.4rem);
  font-weight: 800;
  color: var(--text);
  margin: 0 0 0.5rem;
  line-height: 1.1;
}
.hero h1 span { color: var(--accent); }
.hero-sub {
  font-family: 'Space Mono', monospace;
  font-size: 0.78rem;
  color: var(--muted);
  letter-spacing: 0.1em;
}

/* ── CARDS ── */
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 2rem;
  margin-bottom: 1.5rem;
  position: relative;
  overflow: hidden;
}
.card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--accent), var(--accent2));
}
.card-title {
  font-family: 'Space Mono', monospace;
  font-size: 0.7rem;
  letter-spacing: 0.3em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 1.2rem;
}

/* ── FORM INPUTS ── */
input, textarea, select,
[data-testid="stTextInput"] input,
[data-testid="stTextInput"] textarea {
  background: #0a1520 !important;
  border: 1px solid var(--border) !important;
  border-radius: 6px !important;
  color: var(--text) !important;
  font-family: 'Space Mono', monospace !important;
  font-size: 0.85rem !important;
  padding: 0.6rem 0.9rem !important;
  transition: border-color .2s !important;
}
input:focus, textarea:focus {
  border-color: var(--accent) !important;
  outline: none !important;
  box-shadow: 0 0 0 2px rgba(0,229,255,.1) !important;
}

/* ── LABELS ── */
label, [data-testid="stTextInput"] label,
[data-baseweb="form-control-label"] {
  font-family: 'Space Mono', monospace !important;
  font-size: 0.7rem !important;
  letter-spacing: 0.15em !important;
  text-transform: uppercase !important;
  color: var(--muted) !important;
}

/* ── BUTTONS ── */
[data-testid="stButton"] > button {
  background: linear-gradient(135deg, var(--accent), #0097a7) !important;
  border: none !important;
  border-radius: 6px !important;
  color: #000 !important;
  font-family: 'Space Mono', monospace !important;
  font-size: 0.78rem !important;
  font-weight: 700 !important;
  letter-spacing: 0.15em !important;
  text-transform: uppercase !important;
  padding: 0.6rem 1.8rem !important;
  cursor: pointer !important;
  transition: opacity .2s, transform .1s !important;
}
[data-testid="stButton"] > button:hover {
  opacity: .88 !important;
  transform: translateY(-1px) !important;
}
[data-testid="stButton"] > button:active { transform: translateY(0) !important; }

/* secondary (logout etc) */
.btn-secondary [data-testid="stButton"] > button {
  background: transparent !important;
  border: 1px solid var(--border) !important;
  color: var(--muted) !important;
}
.btn-secondary [data-testid="stButton"] > button:hover {
  border-color: var(--accent2) !important;
  color: var(--accent2) !important;
}

/* ── TABS ── */
[data-testid="stTabs"] [role="tablist"] {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px;
  gap: 2px;
}
[data-testid="stTabs"] [role="tab"] {
  font-family: 'Space Mono', monospace !important;
  font-size: 0.72rem !important;
  letter-spacing: 0.12em !important;
  text-transform: uppercase !important;
  color: var(--muted) !important;
  border-radius: 6px !important;
  padding: 0.5rem 1.2rem !important;
  transition: all .2s !important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  background: var(--accent) !important;
  color: #000 !important;
}

/* ── ALERTS ── */
[data-testid="stAlert"] {
  border-radius: 8px !important;
  border-left-width: 3px !important;
  font-family: 'Space Mono', monospace !important;
  font-size: 0.8rem !important;
}

/* ── DATAFRAME ── */
[data-testid="stDataFrame"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: 8px !important;
  overflow: hidden !important;
}

/* ── FILE UPLOADER ── */
[data-testid="stFileUploader"] {
  background: #0a1520 !important;
  border: 1px dashed var(--border) !important;
  border-radius: 8px !important;
  transition: border-color .2s !important;
}
[data-testid="stFileUploader"]:hover { border-color: var(--accent) !important; }

/* ── DOWNLOAD BUTTON ── */
[data-testid="stDownloadButton"] > button {
  background: linear-gradient(135deg, var(--accent2), #c62828) !important;
  color: #fff !important;
  font-family: 'Space Mono', monospace !important;
  font-size: 0.72rem !important;
  letter-spacing: 0.1em !important;
  text-transform: uppercase !important;
  border: none !important;
  border-radius: 6px !important;
  padding: 0.5rem 1.4rem !important;
}

/* ── VIDEO ROW ── */
.video-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.85rem 1rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 0.6rem;
  background: #0a1520;
  transition: border-color .2s;
}
.video-row:hover { border-color: var(--accent); }
.video-name {
  font-family: 'Space Mono', monospace;
  font-size: 0.82rem;
  color: var(--text);
}
.video-meta {
  font-size: 0.68rem;
  color: var(--muted);
  margin-top: 2px;
}

/* ── STATUS BADGE ── */
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-family: 'Space Mono', monospace;
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
.badge-online  { background: rgba(0,230,118,.15); color: var(--success); border: 1px solid var(--success); }
.badge-danger  { background: rgba(255,60,110,.15); color: var(--accent2); border: 1px solid var(--accent2); }

/* ── USER PILL ── */
.user-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 4px 14px 4px 8px;
  font-family: 'Space Mono', monospace;
  font-size: 0.72rem;
  color: var(--accent);
}
.user-pill-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--success);
  display: inline-block;
}

/* ── SECTION DIVIDER ── */
.section-divider {
  border: none;
  border-top: 1px solid var(--border);
  margin: 1.5rem 0;
}

/* ── STAT CARDS ── */
.stats-row {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1.2rem 1.4rem;
  text-align: center;
}
.stat-num {
  font-family: 'Syne', sans-serif;
  font-size: 2rem;
  font-weight: 800;
  color: var(--accent);
  line-height: 1;
}
.stat-label {
  font-family: 'Space Mono', monospace;
  font-size: 0.65rem;
  color: var(--muted);
  letter-spacing: 0.15em;
  text-transform: uppercase;
  margin-top: 4px;
}

/* ── WAVEFORM GRAPHIC ── */
.waveform {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  height: 32px;
  margin: 1rem 0;
}
.waveform span {
  display: inline-block;
  width: 3px;
  background: var(--accent);
  border-radius: 1px;
  opacity: .6;
  animation: wave 1.2s ease-in-out infinite;
}
.waveform span:nth-child(2n)   { animation-delay: .15s; opacity: .4; }
.waveform span:nth-child(3n)   { animation-delay: .3s;  opacity: .8; }
.waveform span:nth-child(4n)   { animation-delay: .45s; opacity: .5; }
@keyframes wave {
  0%,100% { height: 4px;  }
  50%      { height: 24px; }
}

/* ── PROGRESS BAR ── */
[data-testid="stProgress"] > div > div {
  background: linear-gradient(90deg, var(--accent), var(--accent2)) !important;
  border-radius: 4px !important;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT,
            username TEXT UNIQUE,
            email    TEXT,
            phone    TEXT,
            password BLOB
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS videos(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            uploader_id INTEGER,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
#  PN SEQUENCES
# ─────────────────────────────────────────────
def generate_pn(n, seed):
    rng = np.random.RandomState(seed)
    return rng.choice([-1.0, 1.0], size=n)

PN_DATA = generate_pn(BLOCK_SIZE, 42)
PN_SYNC = generate_pn(BLOCK_SIZE, 999)


# ─────────────────────────────────────────────
#  AES-128-CTR
# ─────────────────────────────────────────────
def encrypt_uid(uid: int):
    msg = str(uid).ljust(16)[:16].encode()
    ctr = Counter.new(64, prefix=AES_PREFIX, initial_value=1)
    cipher = AES.new(AES_KEY, AES.MODE_CTR, counter=ctr)
    enc = cipher.encrypt(msg)
    return [int(b) for byte in enc for b in f"{byte:08b}"]


def decrypt_bits(bits):
    raw = bytearray()
    for i in range(0, len(bits), 8):
        raw.append(int("".join(map(str, bits[i:i+8])), 2))
    ctr = Counter.new(64, prefix=AES_PREFIX, initial_value=1)
    cipher = AES.new(AES_KEY, AES.MODE_CTR, counter=ctr)
    try:
        return int(cipher.decrypt(bytes(raw)).decode().strip())
    except Exception:
        return None


# ─────────────────────────────────────────────
#  WATERMARK EMBED
# ─────────────────────────────────────────────
def adaptive_gain(block):
    rms = np.sqrt(np.mean(block ** 2)) + 1e-9
    return max(MIN_GAIN, BASE_GAIN * (rms / 5000))


def embed_watermark(in_wav, out_wav, uid):
    with wave.open(in_wav, 'rb') as wf:
        params = wf.getparams()
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float64)

    bits = encrypt_uid(uid) * REDUNDANCY
    bit_idx = 0
    out = audio.copy()
    total_blocks = len(audio) // BLOCK_SIZE

    for b in range(total_blocks):
        i = b * BLOCK_SIZE
        block = audio[i:i + BLOCK_SIZE]

        if b % SYNC_INTERVAL_BLOCKS == 0:
            peak = np.max(np.abs(block)) or 1.0
            out[i:i + BLOCK_SIZE] += SYNC_GAIN * PN_SYNC * peak
            continue

        if bit_idx >= len(bits):
            break

        sign = 1 if bits[bit_idx] == 1 else -1
        bit_idx += 1
        gain = adaptive_gain(block)
        out[i:i + BLOCK_SIZE] += sign * gain * PN_DATA

    out = np.clip(out, -32768, 32767).astype(np.int16)
    with wave.open(out_wav, 'wb') as wf:
        wf.setparams(params)
        wf.writeframes(out.tobytes())


# ─────────────────────────────────────────────
#  WATERMARK DETECT
# ─────────────────────────────────────────────
def detect_watermark(wav_path):
    with wave.open(wav_path, 'rb') as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float64)

    total_blocks = len(audio) // BLOCK_SIZE
    data_bits = []

    for b in range(total_blocks):
        i = b * BLOCK_SIZE
        block = audio[i:i + BLOCK_SIZE]

        sync_corr = np.dot(block, PN_SYNC) / (np.linalg.norm(block) * np.linalg.norm(PN_SYNC) + 1e-9)
        if abs(sync_corr) > 0.35:
            continue

        corr = np.dot(block, PN_DATA) / (np.linalg.norm(block) * np.linalg.norm(PN_DATA) + 1e-9)
        if abs(corr) < CORR_THRESHOLD:
            continue

        data_bits.append(1 if corr > 0 else 0)

    if len(data_bits) < 128 * REDUNDANCY:
        return None

    final_bits = []
    for i in range(0, 128 * REDUNDANCY, REDUNDANCY):
        chunk = data_bits[i:i + REDUNDANCY]
        if not chunk:
            break
        final_bits.append(1 if sum(chunk) > len(chunk) // 2 else 0)

    return decrypt_bits(final_bits[:128])


# ─────────────────────────────────────────────
#  FFMPEG HELPER
# ─────────────────────────────────────────────
def run_ffmpeg(cmd):
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


# ─────────────────────────────────────────────
#  UI HELPERS
# ─────────────────────────────────────────────
def waveform_html(n=28):
    bars = "".join(f'<span style="height:{np.random.randint(6,26)}px"></span>' for _ in range(n))
    return f'<div class="waveform">{bars}</div>'


def render_hero():
    st.markdown("""
    <div class="hero">
      <div class="hero-logo">⬡ Anti-Piracy Intelligence System</div>
      <h1>GUARDIAN<span>.</span></h1>
      <div class="hero-sub">Inaudible Audio Watermarking · AES-128-CTR Encryption · User Tracing</div>
    </div>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    init_db()

    if "uid" not in st.session_state:
        st.session_state.uid = None
    if "uname" not in st.session_state:
        st.session_state.uname = ""

    render_hero()

    # ═══════════════════════════════════════
    #  AUTH GATE
    # ═══════════════════════════════════════
    if st.session_state.uid is None:
        col_login, col_sep, col_reg = st.columns([1, 0.05, 1])

        with col_login:
            st.markdown('<div class="card"><div class="card-title">🔐 Sign In</div>', unsafe_allow_html=True)

            username = st.text_input("Username", key="li_user", placeholder="your_username")
            password = st.text_input("Password", key="li_pass", type="password", placeholder="••••••••")

            if st.button("Login →", key="btn_login"):
                if not username or not password:
                    st.error("Please fill in all fields.")
                else:
                    conn = sqlite3.connect(DB_NAME)
                    row = conn.execute(
                        "SELECT id, name, password FROM users WHERE username=?", (username,)
                    ).fetchone()
                    conn.close()
                    if row and bcrypt.checkpw(password.encode(), row[2]):
                        st.session_state.uid   = row[0]
                        st.session_state.uname = row[1]
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")

            st.markdown('</div>', unsafe_allow_html=True)

        with col_sep:
            st.markdown('<div style="border-left:1px solid #1c2a3a;height:320px;margin:auto"></div>',
                        unsafe_allow_html=True)

        with col_reg:
            st.markdown('<div class="card"><div class="card-title">✦ Create Account</div>', unsafe_allow_html=True)

            r_name  = st.text_input("Full Name",  key="rn", placeholder="Jane Doe")
            r_user  = st.text_input("Username",   key="ru", placeholder="jane_doe")
            r_email = st.text_input("Email",       key="re", placeholder="jane@example.com")
            r_phone = st.text_input("Phone",       key="rp", placeholder="+91 98765 43210")
            r_pass  = st.text_input("Password",    key="rw", type="password", placeholder="min 6 chars")

            if st.button("Create Account →", key="btn_reg"):
                if not all([r_name, r_user, r_email, r_phone, r_pass]):
                    st.error("Please fill in all fields.")
                elif len(r_pass) < 6:
                    st.warning("Password must be at least 6 characters.")
                else:
                    h = bcrypt.hashpw(r_pass.encode(), bcrypt.gensalt())
                    try:
                        conn = sqlite3.connect(DB_NAME)
                        conn.execute(
                            "INSERT INTO users(name,username,email,phone,password) VALUES(?,?,?,?,?)",
                            (r_name, r_user, r_email, r_phone, h)
                        )
                        conn.commit()
                        conn.close()
                        st.success("✓ Account created – please log in.")
                    except sqlite3.IntegrityError:
                        st.error("Username already taken.")

            st.markdown('</div>', unsafe_allow_html=True)

        st.stop()

    # ═══════════════════════════════════════
    #  LOGGED-IN HEADER
    # ═══════════════════════════════════════
    conn = sqlite3.connect(DB_NAME)
    n_videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    n_users  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()

    top_l, top_r = st.columns([6, 1])
    with top_l:
        st.markdown(f"""
        <div class="user-pill">
          <span class="user-pill-dot"></span>
          {st.session_state.uname} &nbsp;·&nbsp; UID #{st.session_state.uid}
        </div>
        """, unsafe_allow_html=True)
    with top_r:
        with st.container():
            st.markdown('<div class="btn-secondary">', unsafe_allow_html=True)
            if st.button("Logout", key="logout"):
                st.session_state.uid   = None
                st.session_state.uname = ""
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    st.markdown('<hr class="section-divider">', unsafe_allow_html=True)

    # Stats row
    st.markdown(f"""
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-num">{n_videos}</div>
        <div class="stat-label">Videos Protected</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{n_users}</div>
        <div class="stat-label">Registered Users</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">AES-128</div>
        <div class="stat-label">Encryption Standard</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ═══════════════════════════════════════
    #  TABS
    # ═══════════════════════════════════════
    tab1, tab2, tab3, tab4 = st.tabs([
        "📚  Library",
        "📤  Upload",
        "🔍  Detector",
        "🗄  Database"
    ])

    # ─── LIBRARY ───────────────────────────
    with tab1:
        st.markdown('<div class="card"><div class="card-title">📚 Video Library</div>', unsafe_allow_html=True)
        st.markdown("Each download is uniquely watermarked with your User ID using an inaudible spread-spectrum signal.")

        conn = sqlite3.connect(DB_NAME)
        vids = conn.execute(
            "SELECT v.id, v.filename, u.name, v.uploaded_at "
            "FROM videos v JOIN users u ON v.uploader_id = u.id"
        ).fetchall()
        conn.close()

        if not vids:
            st.info("No videos uploaded yet. Head to the Upload tab to add one.")
        else:
            for vid_id, fname, uploader, uploaded_at in vids:
                c1, c2 = st.columns([4, 2])
                with c1:
                    st.markdown(f"""
                    <div class="video-row">
                      <div>
                        <div class="video-name">🎬 {fname}</div>
                        <div class="video-meta">Uploaded by {uploader} · {uploaded_at[:10]}</div>
                      </div>
                      <span class="badge badge-online">Protected</span>
                    </div>
                    """, unsafe_allow_html=True)
                with c2:
                    if st.button(f"Prepare Download", key=f"prep_{vid_id}"):
                        with st.spinner("Embedding your unique watermark…"):
                            try:
                                with tempfile.TemporaryDirectory() as tmp:
                                    in_v  = os.path.join(UPLOAD_DIR, fname)
                                    wav1  = os.path.join(tmp, "audio_raw.wav")
                                    wav2  = os.path.join(tmp, "audio_wm.wav")
                                    out_v = os.path.join(tmp, "output.mp4")

                                    run_ffmpeg([
                                        "ffmpeg", "-y", "-i", in_v,
                                        "-vn", "-ac", "1", "-ar", "44100",
                                        "-acodec", "pcm_s16le", wav1
                                    ])
                                    embed_watermark(wav1, wav2, st.session_state.uid)
                                    run_ffmpeg([
                                        "ffmpeg", "-y", "-i", in_v, "-i", wav2,
                                        "-map", "0:v:0", "-map", "1:a:0",
                                        "-c:v", "copy", "-c:a", "aac", out_v
                                    ])

                                    with open(out_v, "rb") as f:
                                        data = f.read()

                                st.download_button(
                                    label="⬇  Download Now",
                                    data=data,
                                    file_name=f"guardian_{fname}",
                                    mime="video/mp4",
                                    key=f"dl_{vid_id}_{np.random.randint(1e6)}"
                                )
                                st.success("Watermark embedded successfully.")
                            except Exception as e:
                                st.error(f"Processing error: {e}")

        st.markdown('</div>', unsafe_allow_html=True)

    # ─── UPLOAD ────────────────────────────
    with tab2:
        st.markdown('<div class="card"><div class="card-title">📤 Upload a Video</div>', unsafe_allow_html=True)
        st.markdown("Upload the original master file. It will be stored securely and made available for watermarked distribution.")

        uploaded = st.file_uploader(
            "Drag & drop or click to browse",
            type=["mp4", "mkv", "mov"],
            key="uploader"
        )

        if uploaded:
            st.markdown(waveform_html(), unsafe_allow_html=True)
            col_a, col_b = st.columns(2)
            col_a.metric("File name",  uploaded.name)
            col_b.metric("Size",       f"{uploaded.size / 1_048_576:.2f} MB")

            if st.button("Upload & Protect →", key="btn_upload"):
                path = os.path.join(UPLOAD_DIR, uploaded.name)
                with open(path, "wb") as out:
                    out.write(uploaded.read())
                conn = sqlite3.connect(DB_NAME)
                conn.execute(
                    "INSERT INTO videos(filename,uploader_id) VALUES(?,?)",
                    (uploaded.name, st.session_state.uid)
                )
                conn.commit()
                conn.close()
                st.success(f"✓ '{uploaded.name}' uploaded and registered in the protected library.")

        st.markdown('</div>', unsafe_allow_html=True)

    # ─── DETECTOR ──────────────────────────
    with tab3:
        st.markdown('<div class="card"><div class="card-title">🔍 Leak Detector</div>', unsafe_allow_html=True)
        st.markdown("Upload a suspected leaked video. The system will extract the audio, correlate against registered PN sequences, decrypt the embedded User ID, and identify the leaker.")

        leak = st.file_uploader(
            "Upload suspected leaked file",
            type=["mp4", "mkv", "mov"],
            key="leak_uploader"
        )

        if leak:
            if st.button("Analyse for Watermark →", key="btn_detect"):
                bar = st.progress(0, text="Extracting audio…")
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        vid = os.path.join(tmp, "suspect.mp4")
                        wav = os.path.join(tmp, "suspect.wav")
                        with open(vid, "wb") as f:
                            f.write(leak.read())

                        bar.progress(20, text="Running FFmpeg…")
                        run_ffmpeg([
                            "ffmpeg", "-y", "-i", vid,
                            "-vn", "-ac", "1", "-ar", "44100",
                            "-acodec", "pcm_s16le", wav
                        ])

                        bar.progress(55, text="Correlating PN sequences…")
                        uid = detect_watermark(wav)

                        bar.progress(90, text="Decrypting User ID…")

                    bar.progress(100, text="Done.")

                    if uid is not None:
                        conn = sqlite3.connect(DB_NAME)
                        user = conn.execute(
                            "SELECT name, username, email, phone FROM users WHERE id=?", (uid,)
                        ).fetchone()
                        conn.close()

                        st.markdown(f"""
                        <div style="
                          background:rgba(255,60,110,.08);
                          border:1px solid var(--accent2);
                          border-radius:10px;
                          padding:1.4rem;
                          margin-top:1rem;
                        ">
                          <div style="font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.2em;color:var(--accent2);margin-bottom:.6rem;">
                            🚨 PIRACY DETECTED
                          </div>
                          <div style="font-family:'Syne',sans-serif;font-size:1.4rem;font-weight:800;color:#fff;">
                            User ID #{uid} identified as leaker
                          </div>
                        """, unsafe_allow_html=True)

                        if user:
                            name, uname, email, phone = user
                            st.markdown(f"""
                          <div style="margin-top:.8rem;display:grid;grid-template-columns:1fr 1fr;gap:.5rem;">
                            <div><span style="color:var(--muted);font-size:.7rem;font-family:'Space Mono',monospace;">NAME</span><br>
                                 <span style="font-family:'Space Mono',monospace;">{name}</span></div>
                            <div><span style="color:var(--muted);font-size:.7rem;font-family:'Space Mono',monospace;">USERNAME</span><br>
                                 <span style="font-family:'Space Mono',monospace;">@{uname}</span></div>
                            <div><span style="color:var(--muted);font-size:.7rem;font-family:'Space Mono',monospace;">EMAIL</span><br>
                                 <span style="font-family:'Space Mono',monospace;">{email}</span></div>
                            <div><span style="color:var(--muted);font-size:.7rem;font-family:'Space Mono',monospace;">PHONE</span><br>
                                 <span style="font-family:'Space Mono',monospace;">{phone}</span></div>
                          </div>
                            """, unsafe_allow_html=True)

                        st.markdown("</div>", unsafe_allow_html=True)

                    else:
                        st.markdown("""
                        <div style="
                          background:rgba(0,230,118,.06);
                          border:1px solid var(--success);
                          border-radius:10px;
                          padding:1.2rem;
                          margin-top:1rem;
                          font-family:'Space Mono',monospace;
                          font-size:.85rem;
                        ">
                          ✓ No watermark detected — file appears clean or watermark may have been destroyed.
                        </div>
                        """, unsafe_allow_html=True)

                except Exception as e:
                    st.error(f"Detection error: {e}")

        st.markdown('</div>', unsafe_allow_html=True)

    # ─── DATABASE ──────────────────────────
    with tab4:
        st.markdown('<div class="card"><div class="card-title">🗄 Database Records</div>', unsafe_allow_html=True)

        st.markdown("##### Users")
        conn = sqlite3.connect(DB_NAME)
        df_users = pd.read_sql("SELECT id, name, username, email, phone FROM users", conn)
        df_vids  = pd.read_sql(
            "SELECT v.id, v.filename, u.name AS uploader, v.uploaded_at "
            "FROM videos v JOIN users u ON v.uploader_id = u.id",
            conn
        )
        conn.close()

        st.dataframe(df_users, use_container_width=True)

        st.markdown('<hr class="section-divider">', unsafe_allow_html=True)
        st.markdown("##### Videos")
        st.dataframe(df_vids, use_container_width=True)

        st.markdown('</div>', unsafe_allow_html=True)


if __name__ == "__main__":
    main()
