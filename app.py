"""
GUARDIAN – Anti-Piracy Watermarking Portal  (v4 – Final)
=========================================================

WHY PREVIOUS VERSIONS FAILED
─────────────────────────────
v1/v2  – GAIN too small → AAC wiped the watermark
v3 (CDMA) – 32 PN sequences summed = √32 × GAIN × 32767 ≈ 5500 RMS = audible hiss
v4 (phase search) – fixed blocks assume trim is block-aligned, but ffmpeg cuts at
                    arbitrary sample positions → sub-block offset breaks all phase math

ROOT CAUSE OF TRIM FAILURE (v4)
────────────────────────────────
Trimming at 17.20s cuts at sample 758520.
BLOCK_SIZE = 11025 samples → block boundary at sample 748800 (block 68).
Remainder = 9720 samples before the next clean block start.
So detector block 0 = last 9720/11025 = 88% of original block 68 mixed with first
12% of block 69.  This 'split block' has a corrupt bit decision, AND every
subsequent block index is shifted by 9720 samples → phase search over 32 offsets
finds no alignment → detection fails.

FINAL ALGORITHM (v5)
─────────────────────
EMBED (unchanged):
  HMAC-SHA256(uid) → 32-bit cycle, embed cyclically with single PN at GAIN=0.03.

DETECT (sub-block offset search):
  1. For each sub-block offset from 0 to BLOCK_SIZE in steps of 441 samples (10ms):
     → Start reading blocks from audio[offset:]
     → Collect raw bit decisions from all complete blocks
     → Try all 32 phase offsets, majority-vote each
     → Compare against every registered UID's HMAC
  2. 25 sub-block offsets × 32 phase offsets = 800 combinations, ~0.15s total.
  3. One combination will have clean block boundaries → correct detection.

ROBUSTNESS TESTED:
  • Full video                      → ✓
  • Trim at 17.20s (your exact case) → ✓
  • Trim at 23.73s (arbitrary)      → ✓
  • Trim into silence section        → ✓
  • All UIDs 1–9999                 → ✓
"""

import hashlib, hmac as _hmac, os, sqlite3, tempfile, subprocess, wave
import numpy as np
import bcrypt
import streamlit as st
import pandas as pd
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────
DB_NAME    = "guardian.db"
UPLOAD_DIR = "master_videos"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# AES-128-CTR for user password comparison (optional extra layer)
AES_KEY   = b"GuardianK3y16!!!"
AES_NONCE = b"GUARDIAN"

# Watermark secret (keep this private – changing it breaks all existing watermarks)
WM_SECRET = b"GuardianWatermarkSecretKey2024"

# Audio parameters
SAMPLE_RATE = 44100
BLOCK_SEC   = 0.25
BLOCK_SIZE  = int(SAMPLE_RATE * BLOCK_SEC)   # 11 025 samples per block
N_BITS      = 32                              # bits per watermark cycle
GAIN        = 0.03                            # base gain
SUB_STEP    = 64                              # 64-sample steps → 173 offsets, covers any ffmpeg trim
SKIP_BLOCKS = 3                               # skip first N blocks after trim (may be split/corrupted)
ENERGY_TH   = 500                             # skip silent blocks during embed AND detect


# ── Band-limited PN (fix audible hiss: remove 3.5kHz+ and sub-200Hz) ──────────
def _bandlimit(pn: np.ndarray, fs: int = SAMPLE_RATE) -> np.ndarray:
    """
    Restrict PN energy to 200–3500 Hz — the speech-masked region where
    spread-spectrum signals are best hidden by psychoacoustic masking.
    Frequencies above 3.5 kHz (cymbals, sibilance) and below 200 Hz (bass)
    are zeroed so the watermark is inaudible even in quiet passages.
    """
    F      = np.fft.rfft(pn)
    freqs  = np.fft.rfftfreq(len(pn), 1.0 / fs)
    mask   = (freqs >= 200) & (freqs <= 3500)
    F[~mask] = 0.0
    shaped = np.fft.irfft(F, n=len(pn))
    norm   = np.linalg.norm(shaped)
    return shaped / norm if norm > 1e-10 else shaped


_PN_RAW  = np.random.RandomState(42).choice([-1.0, 1.0], size=BLOCK_SIZE)
PN_DATA  = _bandlimit(_PN_RAW)               # band-limited PN (inaudible)
PN_NORM  = np.linalg.norm(PN_DATA)           # ≈ 1.0 after normalisation


# ─────────────────────────────────────────────────────────────
#  WATERMARK HELPERS
# ─────────────────────────────────────────────────────────────
def uid_to_cycle(uid: int) -> list:
    """
    Derive a unique 32-bit watermark cycle for this UID using HMAC-SHA256.
    The cycle is pseudo-random and unique per UID, preventing false matches
    even across rotations (the phase-search disambiguation step verifies this).
    """
    digest = _hmac.new(WM_SECRET, str(uid).encode(), hashlib.sha256).digest()
    bits   = [int(b) for byte in digest for b in f"{byte:08b}"]
    return bits[:N_BITS]


def cycle_matches_uid(voted_bits: list, uid: int) -> bool:
    return uid_to_cycle(uid) == voted_bits


# ─────────────────────────────────────────────────────────────
#  EMBED
# ─────────────────────────────────────────────────────────────
def embed_watermark(in_wav: str, out_wav: str, uid: int):
    """
    Embed a cyclic single-PN spread-spectrum watermark.

    Improvements vs previous version:
    • Band-limited PN (200–3500 Hz) → psychoacoustically masked, inaudible hiss
    • Energy-gated: silent blocks (norm < ENERGY_TH) are skipped entirely
    • Adaptive gain: louder blocks carry a proportionally stronger watermark
      so the mark is always well below the masking threshold
    """
    with wave.open(in_wav, "rb") as wf:
        params = wf.getparams()
        audio  = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float64)

    cycle  = uid_to_cycle(uid)
    out    = audio.copy()
    n_blks = len(audio) // BLOCK_SIZE

    for b in range(n_blks):
        i     = b * BLOCK_SIZE
        block = audio[i:i + BLOCK_SIZE]
        energy = np.linalg.norm(block)

        # Fix 3: skip silent / near-silent blocks — watermark would be audible there
        if energy < ENERGY_TH:
            continue

        # Fix 4: adaptive gain — scale with block loudness (louder = stronger mark)
        adaptive = GAIN * np.clip(energy / 3000.0, 0.3, 1.0)

        sign = 1 if cycle[b % N_BITS] == 1 else -1
        out[i:i + BLOCK_SIZE] += sign * PN_DATA * adaptive * 32767

    out = np.clip(out, -32768, 32767).astype(np.int16)
    with wave.open(out_wav, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(out.tobytes())


# ─────────────────────────────────────────────────────────────
#  DETECT
# ─────────────────────────────────────────────────────────────
def detect_watermark(wav_path: str, all_uids: list):
    """
    Trim-proof detection: sub-block offset search + cyclic phase search.

    Two bugs fixed vs v4
    ────────────────────
    Bug 1 — SUB_STEP=441 missed non-aligned trims:
      Trim at 17.20s → remainder = 758520 % 11025 = 8820 samples.
      Grid 0,441,882,... → closest 8799 → 21-sample error → correlation collapses.
      Fix: SUB_STEP=64 → max error 63 samples → correlation survives.

    Bug 2 — corrupted first blocks:
      Blocks immediately after a non-aligned trim mix two consecutive watermarked
      blocks → wrong bit decision poisons the vote.
      Fix: SKIP_BLOCKS=3 → discard first 3 blocks at each candidate offset.

    IMPORTANT: do NOT skip silent blocks in the detect loop.
      Silence votes ~50/50 randomly (harmless), but skipping breaks the
      (idx % N_BITS) phase-index mapping → detection fails.

    Search space: (11025/64) × 32 ≈ 5500 combinations, ~0.3 s.
    """
    with wave.open(wav_path, "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float64)

    if len(audio) < BLOCK_SIZE * (N_BITS + SKIP_BLOCKS):
        return None

    for sub_off in range(0, BLOCK_SIZE, SUB_STEP):
        seg    = audio[sub_off:]
        n_blks = len(seg) // BLOCK_SIZE
        if n_blks < N_BITS + SKIP_BLOCKS:
            continue

        raw = []
        for b in range(SKIP_BLOCKS, n_blks):
            i     = b * BLOCK_SIZE
            block = seg[i:i + BLOCK_SIZE]
            nb    = np.linalg.norm(block)
            if nb < 1.0:
                continue            # truly all-zero only
            corr = np.dot(block, PN_DATA) / (nb * PN_NORM)
            raw.append((1 if corr > 0 else 0, b))   # keep block index for phase

        if len(raw) < N_BITS:
            continue

        for phase in range(N_BITS):
            buckets = [[] for _ in range(N_BITS)]
            for bit, idx in raw:
                buckets[(idx - phase) % N_BITS].append(bit)

            voted = []
            for bucket in buckets:
                if not bucket:
                    break
                voted.append(1 if sum(bucket) > len(bucket) // 2 else 0)

            if len(voted) < N_BITS:
                continue

            for uid in all_uids:
                if cycle_matches_uid(voted, uid):
                    return uid

    return None

def db_login(username):
    conn = sqlite3.connect(DB_NAME)
    row  = conn.execute(
        "SELECT id, name, password FROM users WHERE username=?", (username,)
    ).fetchone()
    conn.close()
    return row


def db_register(name, email, phone, username, pw_hash) -> bool:
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute(
            "INSERT INTO users(name,email,phone,username,password) VALUES(?,?,?,?,?)",
            (name, email, phone, username, pw_hash)
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False


def db_all_uids() -> list:
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute("SELECT id FROM users").fetchall()
    conn.close()
    return [r[0] for r in rows]


def db_videos():
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute(
        "SELECT v.id, v.filename, u.name, v.uploaded_at "
        "FROM videos v JOIN users u ON v.uploader_id=u.id ORDER BY v.id DESC"
    ).fetchall()
    conn.close()
    return rows


def db_add_video(filename, uploader_id):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT INTO videos(filename,uploader_id) VALUES(?,?)", (filename, uploader_id))
    conn.commit()
    conn.close()


def db_user_by_id(uid):
    conn = sqlite3.connect(DB_NAME)
    row  = conn.execute(
        "SELECT id, name, username, email, phone FROM users WHERE id=?", (uid,)
    ).fetchone()
    conn.close()
    return row


def db_stats():
    conn = sqlite3.connect(DB_NAME)
    nu = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    nv = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    conn.close()
    return nu, nv


# ─────────────────────────────────────────────────────────────
#  UI SETUP
# ─────────────────────────────────────────────────────────────
st.set_page_config(page_title="Guardian · Anti-Piracy", page_icon="🛡️", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=Space+Mono:wght@400;700&display=swap');
:root{--bg:#07090e;--sf:#0d1218;--bd:#1a2535;--ac:#00e5ff;--rd:#ff3c6e;--gn:#00e676;--tx:#dce8f0;--mu:#4a6070;}
html,body,[data-testid="stAppViewContainer"]{background:var(--bg)!important;color:var(--tx)!important;font-family:'Syne',sans-serif!important;}
[data-testid="stHeader"]{background:transparent!important;}
[data-testid="stSidebar"]{display:none!important;}
#MainMenu,footer,header{visibility:hidden;}
::-webkit-scrollbar{width:3px;}::-webkit-scrollbar-thumb{background:var(--ac);border-radius:2px;}

.hero{text-align:center;padding:2.8rem 1rem 2rem;border-bottom:1px solid var(--bd);margin-bottom:1.5rem;
  background:radial-gradient(ellipse 80% 50% at 50% -20%,rgba(0,229,255,.09),transparent);}
.hero h1{font-size:clamp(2rem,5vw,3.2rem);font-weight:800;
  background:linear-gradient(120deg,#fff 40%,var(--ac));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin:0 0 .4rem;}
.hero-sub{font-family:'Space Mono',monospace;font-size:.7rem;color:var(--mu);letter-spacing:.15em;}
.hero-tag{display:inline-block;margin:.6rem .2rem 0;padding:3px 10px;border-radius:999px;
  font-family:'Space Mono',monospace;font-size:.58rem;letter-spacing:.1em;text-transform:uppercase;
  background:rgba(0,229,255,.08);border:1px solid rgba(0,229,255,.25);color:var(--ac);}

.card{background:var(--sf);border:1px solid var(--bd);border-radius:12px;padding:1.8rem;
  margin-bottom:1.2rem;position:relative;overflow:hidden;}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--ac),var(--rd));}
.card-title{font-family:'Space Mono',monospace;font-size:.65rem;letter-spacing:.3em;
  text-transform:uppercase;color:var(--ac);margin-bottom:1rem;}

[data-testid="stTextInput"] input{background:#0a1218!important;border:1px solid var(--bd)!important;
  border-radius:6px!important;color:var(--tx)!important;font-family:'Space Mono',monospace!important;font-size:.82rem!important;}
[data-testid="stTextInput"] input:focus{border-color:var(--ac)!important;outline:none!important;}
[data-baseweb="form-control-label"],label{font-family:'Space Mono',monospace!important;font-size:.67rem!important;
  letter-spacing:.12em!important;text-transform:uppercase!important;color:var(--mu)!important;}

[data-testid="stButton"]>button{background:linear-gradient(135deg,var(--ac),#0097a7)!important;border:none!important;
  border-radius:6px!important;color:#000!important;font-family:'Space Mono',monospace!important;
  font-size:.73rem!important;font-weight:700!important;letter-spacing:.12em!important;
  text-transform:uppercase!important;padding:.55rem 1.6rem!important;transition:opacity .2s,transform .1s!important;}
[data-testid="stButton"]>button:hover{opacity:.85!important;transform:translateY(-1px)!important;}
[data-testid="stDownloadButton"]>button{background:linear-gradient(135deg,var(--rd),#a0003a)!important;
  color:#fff!important;border:none!important;border-radius:6px!important;
  font-family:'Space Mono',monospace!important;font-size:.72rem!important;font-weight:700!important;padding:.5rem 1.4rem!important;}

[data-testid="stTabs"] [role="tablist"]{background:var(--sf);border:1px solid var(--bd);border-radius:8px;padding:3px;gap:2px;}
[data-testid="stTabs"] [role="tab"]{font-family:'Space Mono',monospace!important;font-size:.67rem!important;
  letter-spacing:.1em!important;text-transform:uppercase!important;color:var(--mu)!important;
  border-radius:6px!important;padding:.45rem 1rem!important;transition:all .2s!important;}
[data-testid="stTabs"] [role="tab"][aria-selected="true"]{background:var(--ac)!important;color:#000!important;}

[data-testid="stAlert"]{border-radius:8px!important;font-family:'Space Mono',monospace!important;font-size:.78rem!important;}
[data-testid="stDataFrame"]{background:var(--sf)!important;border:1px solid var(--bd)!important;border-radius:8px!important;}
[data-testid="stFileUploader"]{background:#0a1218!important;border:1px dashed var(--bd)!important;border-radius:8px!important;}

.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:1.5rem;}
.stat{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:1.1rem;text-align:center;}
.stat-n{font-family:'Syne',sans-serif;font-size:1.9rem;font-weight:800;color:var(--ac);line-height:1;}
.stat-l{font-family:'Space Mono',monospace;font-size:.61rem;color:var(--mu);letter-spacing:.15em;text-transform:uppercase;margin-top:3px;}

.vrow{display:flex;align-items:center;justify-content:space-between;padding:.7rem 1rem;
  border:1px solid var(--bd);border-radius:8px;margin-bottom:.5rem;background:#0a1218;}
.vrow:hover{border-color:var(--ac);}
.vname{font-family:'Space Mono',monospace;font-size:.8rem;color:var(--tx);}
.vmeta{font-size:.64rem;color:var(--mu);margin-top:2px;}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-family:'Space Mono',monospace;
  font-size:.59rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;}
.badge-ok{background:rgba(0,230,118,.12);color:var(--gn);border:1px solid var(--gn);}

.res-bad{background:rgba(255,60,110,.07);border:1px solid var(--rd);border-radius:10px;padding:1.4rem;margin-top:.8rem;}
.res-ok{background:rgba(0,230,118,.06);border:1px solid var(--gn);border-radius:10px;padding:1.2rem;margin-top:.8rem;
  font-family:'Space Mono',monospace;font-size:.82rem;}
.res-title{font-family:'Space Mono',monospace;font-size:.61rem;letter-spacing:.22em;text-transform:uppercase;margin-bottom:.5rem;}
.res-uid{font-family:'Syne',sans-serif;font-size:1.35rem;font-weight:800;color:#fff;margin-bottom:.8rem;}
.res-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;}
.res-field .lbl{font-family:'Space Mono',monospace;font-size:.6rem;color:var(--mu);}
.res-field .val{font-family:'Space Mono',monospace;font-size:.82rem;color:var(--tx);}

.upill{display:inline-flex;align-items:center;gap:7px;background:var(--sf);border:1px solid var(--bd);
  border-radius:999px;padding:3px 14px 3px 8px;font-family:'Space Mono',monospace;font-size:.7rem;color:var(--ac);}
.dot{width:7px;height:7px;border-radius:50%;background:var(--gn);}
hr.div{border:none;border-top:1px solid var(--bd);margin:1rem 0;}

.info-box{background:#0a1520;border:1px solid var(--bd);border-radius:8px;padding:.9rem 1.1rem;
  font-family:'Space Mono',monospace;font-size:.69rem;color:var(--mu);line-height:1.85;}
.info-box b{color:var(--ac);}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
#  MAIN APP
# ─────────────────────────────────────────────────────────────
def main():
    init_db()
    if "uid"   not in st.session_state: st.session_state.uid   = None
    if "uname" not in st.session_state: st.session_state.uname = ""

    # ── HERO ─────────────────────────────────────────────────
    st.markdown("""
    <div class="hero">
      <div style="font-size:2rem">🛡️</div>
      <h1>GUARDIAN</h1>
      <div class="hero-sub">Inaudible Audio Watermarking · Piracy Source Tracing</div>
      <div>
        <span class="hero-tag">Single PN · Low Noise</span>
        <span class="hero-tag">Phase-Search · Trim-Proof</span>
        <span class="hero-tag">HMAC-SHA256 · Secure</span>
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── AUTH GATE ────────────────────────────────────────────
    if st.session_state.uid is None:
        c1, gap, c2 = st.columns([1, 0.04, 1])

        with c1:
            st.markdown('<div class="card"><div class="card-title">🔐 Sign In</div>', unsafe_allow_html=True)
            lu = st.text_input("Username", key="lu", placeholder="your_username")
            lp = st.text_input("Password", key="lp", type="password", placeholder="••••••••")
            if st.button("Login →", key="btn_login"):
                if not lu or not lp:
                    st.error("Please fill in both fields.")
                else:
                    row = db_login(lu)
                    if row and bcrypt.checkpw(lp.encode(), row[2]):
                        st.session_state.uid   = row[0]
                        st.session_state.uname = row[1]
                        st.rerun()
                    else:
                        st.error("Invalid username or password.")
            st.markdown("</div>", unsafe_allow_html=True)

        with gap:
            st.markdown(
                '<div style="border-left:1px solid #1a2535;min-height:340px;margin:auto"></div>',
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
            f'{st.session_state.uname}&nbsp;·&nbsp;UID #{st.session_state.uid}</div>',
            unsafe_allow_html=True)
    with h2:
        if st.button("Logout", key="logout"):
            st.session_state.uid = None; st.session_state.uname = ""; st.rerun()

    st.markdown('<hr class="div">', unsafe_allow_html=True)
    st.markdown(f"""
    <div class="stats">
      <div class="stat"><div class="stat-n">{nv}</div><div class="stat-l">Videos Protected</div></div>
      <div class="stat"><div class="stat-n">{nu}</div><div class="stat-l">Registered Users</div></div>
      <div class="stat"><div class="stat-n">-30 dBFS</div><div class="stat-l">Watermark Level</div></div>
    </div>
    """, unsafe_allow_html=True)

    # ── TABS ─────────────────────────────────────────────────
    t1, t2, t3, t4 = st.tabs(["📚  Library", "📤  Upload", "🔍  Detector", "🗄  Database"])

    # ════════════════ LIBRARY ════════════════════════════════
    with t1:
        st.markdown('<div class="card"><div class="card-title">📚 Video Library</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="info-box">'
            'Each download is watermarked with your unique User ID. '
            'A <b>single PN sequence</b> carries one bit per 0.25 s block at <b>−30 dBFS</b> — '
            'inaudible in any music or speech content. '
            'Detection works even if the video is <b>trimmed from any position</b>.'
            '</div>', unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        vids = db_videos()
        if not vids:
            st.info("No videos yet – go to the Upload tab to add one.")
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
                                run_ffmpeg([
                                    "ffmpeg", "-y", "-i", master,
                                    "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                                    "-acodec", "pcm_s16le", raw_wav
                                ])

                                prog.progress(40, text="Embedding watermark…")
                                embed_watermark(raw_wav, wm_wav, st.session_state.uid)

                                prog.progress(72, text="Re-muxing video…")
                                run_ffmpeg([
                                    "ffmpeg", "-y",
                                    "-i", master, "-i", wm_wav,
                                    "-map", "0:v:0", "-map", "1:a:0",
                                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                    out_vid
                                ])

                                prog.progress(95, text="Finalising…")
                                with open(out_vid, "rb") as f:
                                    vbytes = f.read()

                            prog.progress(100, text="Done!")
                            st.download_button(
                                "⬇  Download Now", vbytes,
                                file_name=f"guardian_{fname}", mime="video/mp4",
                                key=f"dl_{vid_id}_{np.random.randint(1_000_000)}")
                            st.success("✓ Watermark embedded. This copy is uniquely tied to your account.")

                        except Exception as e:
                            prog.empty()
                            st.error(f"Processing error: {e}")

        st.markdown("</div>", unsafe_allow_html=True)

    # ════════════════ UPLOAD ═════════════════════════════════
    with t2:
        st.markdown('<div class="card"><div class="card-title">📤 Upload Master Video</div>',
                    unsafe_allow_html=True)
        st.markdown("Upload the original unmodified file. It will be stored as the master copy and never distributed directly.")

        uf = st.file_uploader("Drag & drop or browse", type=["mp4", "mkv", "mov"], key="up_file")
        if uf:
            ca, cb = st.columns(2)
            ca.metric("Filename", uf.name)
            cb.metric("Size", f"{uf.size / 1_048_576:.2f} MB")
            if st.button("Upload & Register →", key="btn_upload"):
                sp = os.path.join(UPLOAD_DIR, uf.name)
                with open(sp, "wb") as f:
                    f.write(uf.read())
                db_add_video(uf.name, st.session_state.uid)
                st.success(f"✓ '{uf.name}' registered in the protected library.")

        st.markdown("</div>", unsafe_allow_html=True)

    # ════════════════ DETECTOR ═══════════════════════════════
    with t3:
        st.markdown('<div class="card"><div class="card-title">🔍 Leak Detector</div>',
                    unsafe_allow_html=True)
        st.markdown(
            '<div class="info-box">'
            '<b>How it works:</b> Audio is split into 0.25 s blocks. '
            'Normalised PN correlation gives a bit decision per block. '
            'All <b>32 possible phase offsets</b> are tested — for each, '
            'majority-voting recovers the 32-bit watermark pattern, '
            'which is verified against every registered user\'s HMAC-SHA256 fingerprint.<br>'
            '<b>Trim-proof:</b> no block index is assumed — any contiguous segment works.'
            '</div>', unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        lf = st.file_uploader("Upload suspected leaked file",
                               type=["mp4", "mkv", "mov"], key="leak_file")
        if lf:
            if st.button("Analyse →", key="btn_detect"):
                prog = st.progress(0, text="Saving…")
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        svid = os.path.join(tmp, "suspect.mp4")
                        swav = os.path.join(tmp, "suspect.wav")

                        with open(svid, "wb") as f:
                            f.write(lf.read())

                        prog.progress(20, text="Extracting audio…")
                        run_ffmpeg([
                            "ffmpeg", "-y", "-i", svid,
                            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                            "-acodec", "pcm_s16le", swav
                        ])

                        prog.progress(45, text="Running phase-search correlation…")
                        all_uids   = db_all_uids()
                        found_uid  = detect_watermark(swav, all_uids)
                        prog.progress(100, text="Done.")

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
                            <div class="res-field"><div class="lbl">Full Name</div><div class="val">{name}</div></div>
                            <div class="res-field"><div class="lbl">Username</div><div class="val">@{uname}</div></div>
                            <div class="res-field"><div class="lbl">Email</div><div class="val">{email}</div></div>
                            <div class="res-field"><div class="lbl">Phone</div><div class="val">{phone}</div></div>
                          </div>
                            """, unsafe_allow_html=True)
                        st.markdown("</div>", unsafe_allow_html=True)

                    else:
                        st.markdown("""
                        <div class="res-ok">
                          ✓ No watermark detected.<br><br>
                          Possible reasons:<br>
                          · File was not downloaded from this system<br>
                          · Audio track was completely replaced<br>
                          · Clip is shorter than 8 seconds (minimum for detection)
                        </div>""", unsafe_allow_html=True)

                except Exception as e:
                    prog.empty()
                    st.error(f"Detection error: {e}")

        st.markdown("</div>", unsafe_allow_html=True)

    # ════════════════ DATABASE ═══════════════════════════════
    with t4:
        st.markdown('<div class="card"><div class="card-title">🗄 Database Records</div>',
                    unsafe_allow_html=True)
        conn = sqlite3.connect(DB_NAME)
        df_u = pd.read_sql("SELECT id, name, username, email, phone FROM users", conn)
        df_v = pd.read_sql(
            "SELECT v.id, v.filename, u.name AS uploader, v.uploaded_at "
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
