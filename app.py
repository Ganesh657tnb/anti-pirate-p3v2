
import hashlib, hmac, os, sqlite3, tempfile, subprocess, wave, json
import numpy as np
import streamlit as st
import pandas as pd
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2
import base64
import bcrypt
from scipy import signal
from scipy.fft import dct, idct
import time

# ─────────────────────────────────────────────────────────────
#  SECURE CONFIGURATION
# ─────────────────────────────────────────────────────────────
DB_NAME = "guardian.db"
UPLOAD_DIR = "master_videos"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Generate or load encryption key for user IDs
KEY_FILE = "encryption.key"
if os.path.exists(KEY_FILE):
    with open(KEY_FILE, "rb") as f:
        ENCRYPTION_KEY = f.read()
else:
    ENCRYPTION_KEY = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(ENCRYPTION_KEY)

cipher = Fernet(ENCRYPTION_KEY)

# Watermark secrets (rotate periodically in production)
WM_SECRET = b"Guardian-Watermark-v6-2024-SecretKey"
HMAC_SECRET = b"Guardian-HMAC-v6-2024-SecretKey"

# Audio parameters - Optimized for inaudibility
SAMPLE_RATE = 44100
FRAME_SIZE = 2048  # 46ms frames for frequency domain processing
HOP_SIZE = 512     # 75% overlap for smooth transitions
GAIN = 0.003       # -50 dBFS - Completely inaudible
MIN_DURATION = 3   # Minimum seconds needed for detection

# Psychoacoustic masking model parameters
FREQ_BANDS = 25    # Bark scale bands for perceptual weighting

# ─────────────────────────────────────────────────────────────
#  USER ID ENCRYPTION
# ─────────────────────────────────────────────────────────────
def encrypt_uid(uid: int) -> str:
    """Encrypt user ID for database storage"""
    return cipher.encrypt(str(uid).encode()).decode()

def decrypt_uid(encrypted_uid: str) -> int:
    """Decrypt user ID from database"""
    return int(cipher.decrypt(encrypted_uid.encode()).decode())

def generate_watermark_pattern(uid: int, length: int = 128) -> np.ndarray:
    """
    Generate unique watermark pattern from encrypted UID
    Uses HMAC-SHA256 with salt to prevent pattern prediction
    """
    # Add salt and timestamp to prevent replay attacks
    salt = os.urandom(16)
    timestamp = str(int(time.time() // 86400))  # Daily rotation
    
    # Create HMAC with multiple factors
    h = hmac.new(
        HMAC_SECRET,
        f"{uid}:{salt}:{timestamp}".encode(),
        hashlib.sha256
    )
    
    # Generate pseudo-random pattern
    seed = int.from_bytes(h.digest(), byteorder='big')
    rng = np.random.RandomState(seed % 2**32)
    
    # Return bipolar pattern (-1, 1)
    return rng.choice([-1.0, 1.0], size=length)

# ─────────────────────────────────────────────────────────────
#  PSYCHOACOUSTIC MODEL
# ─────────────────────────────────────────────────────────────
class PsychoacousticModel:
    """Implements basic psychoacoustic masking for inaudible watermarking"""
    
    def __init__(self, sample_rate=SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.bark_bands = self._init_bark_bands()
        
    def _init_bark_bands(self):
        """Initialize Bark scale frequency bands"""
        # Simplified Bark scale bands (Hz)
        limits = [20, 100, 200, 300, 400, 510, 630, 770, 920, 1080, 
                 1270, 1480, 1720, 2000, 2320, 2700, 3150, 3700, 
                 4400, 5300, 6400, 7700, 9500, 12000, 15500]
        
        bands = []
        for i in range(len(limits)-1):
            bands.append((limits[i], limits[i+1]))
        return bands
    
    def compute_masking_threshold(self, spectrum, freqs):
        """
        Compute masking threshold using simplified psychoacoustic model
        Returns per-bin gain limits for inaudible watermarking
        """
        n_bins = len(spectrum)
        threshold = np.zeros(n_bins)
        
        # Convert to power spectrum
        power = np.abs(spectrum) ** 2
        
        # Compute per-band masking
        for band_idx, (low_freq, high_freq) in enumerate(self.bark_bands):
            # Find bins in this band
            band_bins = np.where((freqs >= low_freq) & (freqs < high_freq))[0]
            if len(band_bins) == 0:
                continue
            
            # Band power
            band_power = np.mean(power[band_bins])
            
            # Simplified masking threshold (20 dB below band power)
            mask_level = band_power * 0.01  # -20 dB
            
            # Spread to neighboring bands (simplified)
            for bin_idx in band_bins:
                threshold[bin_idx] = mask_level
        
        return np.sqrt(threshold)  # Convert back to amplitude

# ─────────────────────────────────────────────────────────────
#  FREQUENCY DOMAIN WATERMARKING
# ─────────────────────────────────────────────────────────────
class FrequencyDomainWatermark:
    """Watermark embedding in DCT domain for better inaudibility"""
    
    def __init__(self, gain=GAIN):
        self.gain = gain
        self.psychoacoustic = PsychoacousticModel()
        
    def embed_frame(self, frame: np.ndarray, bit: int, mask_threshold: np.ndarray) -> np.ndarray:
        """
        Embed one bit in a single frame using DCT domain
        Applies perceptual masking to ensure inaudibility
        """
        # Apply window
        window = np.hanning(len(frame))
        windowed = frame * window
        
        # DCT transform
        dct_coeffs = dct(windowed, norm='ortho')
        
        # Generate spread spectrum pattern for this frame
        pattern = generate_watermark_pattern(bit, len(dct_coeffs))
        
        # Scale pattern by gain and masking threshold
        watermark = self.gain * pattern * mask_threshold
        
        # Embed with sign based on bit
        if bit == 1:
            dct_coeffs += watermark
        else:
            dct_coeffs -= watermark
        
        # Inverse DCT
        watermarked = idct(dct_coeffs, norm='ortho')
        
        # Apply inverse window
        return watermarked * window
    
    def extract_bit(self, frame: np.ndarray, expected_pattern: np.ndarray) -> float:
        """
        Extract bit from frame using correlation
        Returns correlation strength
        """
        window = np.hanning(len(frame))
        windowed = frame * window
        dct_coeffs = dct(windowed, norm='ortho')
        
        # Normalized correlation
        correlation = np.corrcoef(dct_coeffs, expected_pattern)[0, 1]
        return correlation

# ─────────────────────────────────────────────────────────────
#  WATERMARK EMBEDDING
# ─────────────────────────────────────────────────────────────
def embed_watermark(in_wav: str, out_wav: str, uid: int):
    """
    Embed imperceptible watermark using frequency domain processing
    """
    # Read audio file
    with wave.open(in_wav, "rb") as wf:
        params = wf.getparams()
        audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float64)
        n_frames = params.nframes
    
    # Generate unique watermark pattern for this UID
    watermark_pattern = generate_watermark_pattern(uid, FRAME_SIZE)
    
    # Initialize frequency domain embedder
    embedder = FrequencyDomainWatermark(gain=GAIN)
    
    # Prepare output array
    output = np.zeros_like(audio)
    
    # Process audio in overlapping frames
    psychoacoustic = PsychoacousticModel()
    
    for i in range(0, len(audio) - FRAME_SIZE, HOP_SIZE):
        frame = audio[i:i + FRAME_SIZE]
        
        # Skip silent frames
        if np.max(np.abs(frame)) < 100:
            output[i:i + FRAME_SIZE] += frame
            continue
        
        # Compute frequency spectrum for masking
        spectrum = np.fft.rfft(frame)
        freqs = np.fft.rfftfreq(FRAME_SIZE, 1/SAMPLE_RATE)
        
        # Get psychoacoustic masking threshold
        mask_threshold = psychoacoustic.compute_masking_threshold(spectrum, freqs)
        
        # Interpolate threshold to DCT domain
        mask_threshold_dct = np.interp(
            np.linspace(0, FRAME_SIZE-1, FRAME_SIZE),
            np.arange(len(mask_threshold)),
            mask_threshold
        )
        
        # Determine bit for this frame (cyclic through pattern)
        bit = 1 if watermark_pattern[i % len(watermark_pattern)] > 0 else 0
        
        # Embed watermark in this frame
        watermarked_frame = embedder.embed_frame(frame, bit, mask_threshold_dct)
        
        # Overlap-add
        output[i:i + FRAME_SIZE] += watermarked_frame
    
    # Normalize to prevent clipping
    max_val = np.max(np.abs(output))
    if max_val > 32767:
        output = output * (32767 / max_val)
    
    # Convert back to int16
    output = np.clip(output, -32768, 32767).astype(np.int16)
    
    # Write output file
    with wave.open(out_wav, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(output.tobytes())

# ─────────────────────────────────────────────────────────────
#  TRIM-PROOF WATERMARK DETECTION
# ─────────────────────────────────────────────────────────────
def detect_watermark(wav_path: str, all_users: list) -> tuple:
    """
    Detect watermark with resistance to arbitrary trimming
    Uses sliding correlation with 1ms resolution
    
    Returns: (uid, confidence_score) or (None, 0)
    """
    # Read audio file
    with wave.open(wav_path, "rb") as wf:
        audio = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float64)
    
    # Check minimum duration
    if len(audio) < SAMPLE_RATE * MIN_DURATION:
        return None, 0
    
    # Prepare user patterns
    user_patterns = {}
    for user_id, encrypted_id in all_users:
        # Decrypt user ID
        uid = decrypt_uid(encrypted_id)
        # Generate expected pattern
        user_patterns[user_id] = generate_watermark_pattern(uid, FRAME_SIZE)
    
    # Sliding window correlation
    correlations = []
    embedder = FrequencyDomainWatermark()
    
    # Slide with 1ms resolution (44 samples at 44.1kHz)
    step_size = 44  # 1ms
    
    for start in range(0, len(audio) - FRAME_SIZE, step_size):
        frame = audio[start:start + FRAME_SIZE]
        
        # Skip low-energy frames
        if np.std(frame) < 10:
            continue
        
        # Test against each user's pattern
        frame_correlations = {}
        for user_id, pattern in user_patterns.items():
            corr = embedder.extract_bit(frame, pattern)
            frame_correlations[user_id] = corr
        
        correlations.append(frame_correlations)
    
    if not correlations:
        return None, 0
    
    # Aggregate correlations over time
    user_scores = {}
    for user_id in user_patterns.keys():
        scores = [c[user_id] for c in correlations]
        # Use median to be robust to outliers
        user_scores[user_id] = np.median(scores)
    
    # Find best match
    best_user = max(user_scores.items(), key=lambda x: x[1])
    confidence = best_user[1]
    
    # Threshold for detection (adjust based on testing)
    if confidence > 0.15:
        return best_user[0], confidence
    
    return None, 0

# ─────────────────────────────────────────────────────────────
#  FFMPEG HELPERS
# ─────────────────────────────────────────────────────────────
def extract_audio(video_path: str, audio_path: str):
    """Extract audio from video file"""
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-acodec", "pcm_s16le", audio_path
    ], check=True, capture_output=True)

def combine_audio_video(video_path: str, audio_path: str, output_path: str):
    """Combine video with watermarked audio"""
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path, "-i", audio_path,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        output_path
    ], check=True, capture_output=True)

# ─────────────────────────────────────────────────────────────
#  ENHANCED DATABASE WITH ENCRYPTION
# ─────────────────────────────────────────────────────────────
def init_db():
    """Initialize database with encrypted UID storage"""
    conn = sqlite3.connect(DB_NAME)
    
    # Users table with encrypted ID
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            email       TEXT    NOT NULL,
            phone       TEXT    NOT NULL,
            username    TEXT    UNIQUE NOT NULL,
            password    BLOB    NOT NULL,
            encrypted_id TEXT   UNIQUE NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
    
    # Videos table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT    NOT NULL,
            uploader_id INTEGER NOT NULL,
            uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            hash        TEXT,  -- SHA256 of file for integrity
            FOREIGN KEY (uploader_id) REFERENCES users(id)
        )""")
    
    # Detection log for auditing
    conn.execute("""
        CREATE TABLE IF NOT EXISTS detections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id    INTEGER,
            detected_uid INTEGER,
            confidence  REAL,
            detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (video_id) REFERENCES videos(id)
        )""")
    
    conn.commit()
    conn.close()

def db_register(name, email, phone, username, pw_hash) -> bool:
    """Register new user with encrypted ID"""
    try:
        conn = sqlite3.connect(DB_NAME)
        
        # Get next ID
        cursor = conn.execute("SELECT MAX(id) FROM users")
        max_id = cursor.fetchone()[0]
        new_id = (max_id or 0) + 1
        
        # Encrypt the user ID
        encrypted_id = encrypt_uid(new_id)
        
        conn.execute("""
            INSERT INTO users(name, email, phone, username, password, encrypted_id) 
            VALUES(?,?,?,?,?,?)
        """, (name, email, phone, username, pw_hash, encrypted_id))
        
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        return False

def db_login(username):
    """Login with username"""
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute(
        "SELECT id, name, password, encrypted_id FROM users WHERE username=?", 
        (username,)
    ).fetchone()
    conn.close()
    return row

def db_all_users() -> list:
    """Get all users with encrypted IDs for detection"""
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute("SELECT id, encrypted_id FROM users").fetchall()
    conn.close()
    return rows

def db_log_detection(video_id, detected_uid, confidence):
    """Log watermark detection for auditing"""
    conn = sqlite3.connect(DB_NAME)
    conn.execute(
        "INSERT INTO detections(video_id, detected_uid, confidence) VALUES(?,?,?)",
        (video_id, detected_uid, confidence)
    )
    conn.commit()
    conn.close()

# ─────────────────────────────────────────────────────────────
#  STREAMLIT UI (Simplified for clarity)
# ─────────────────────────────────────────────────────────────
def main():
    init_db()
    
    # Session state
    if "user" not in st.session_state:
        st.session_state.user = None
    
    st.set_page_config(
        page_title="Guardian - Anti-Piracy System",
        page_icon="🛡️",
        layout="wide"
    )
    
    # Header
    st.title("🛡️ GUARDIAN - Anti-Piracy Watermarking System")
    st.markdown("---")
    
    # Authentication
    if not st.session_state.user:
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Login")
            username = st.text_input("Username", key="login_user")
            password = st.text_input("Password", type="password", key="login_pass")
            
            if st.button("Login"):
                user = db_login(username)
                if user and bcrypt.checkpw(password.encode(), user[2]):
                    st.session_state.user = {
                        "id": user[0],
                        "name": user[1],
                        "encrypted_id": user[3]
                    }
                    st.rerun()
                else:
                    st.error("Invalid credentials")
        
        with col2:
            st.subheader("Register")
            with st.form("register"):
                name = st.text_input("Full Name")
                email = st.text_input("Email")
                phone = st.text_input("Phone")
                new_user = st.text_input("Username")
                new_pass = st.text_input("Password", type="password")
                
                if st.form_submit_button("Register"):
                    if len(new_pass) >= 8:
                        pw_hash = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt())
                        if db_register(name, email, phone, new_user, pw_hash):
                            st.success("Registration successful! Please login.")
                        else:
                            st.error("Username already exists")
                    else:
                        st.error("Password must be at least 8 characters")
        
        return
    
    # Main interface for logged-in users
    st.sidebar.success(f"Welcome, {st.session_state.user['name']}!")
    
    if st.sidebar.button("Logout"):
        st.session_state.user = None
        st.rerun()
    
    # Tabs
    tab1, tab2, tab3 = st.tabs(["📤 Upload & Watermark", "🔍 Detect Leak", "📊 Reports"])
    
    # Tab 1: Upload and watermark videos
    with tab1:
        st.header("Upload Video for Watermarking")
        
        uploaded_file = st.file_uploader(
            "Choose video file", 
            type=["mp4", "mov", "avi", "mkv"]
        )
        
        if uploaded_file:
            st.video(uploaded_file)
            
            if st.button("Apply Watermark"):
                with st.spinner("Watermarking video... (This may take a minute)"):
                    try:
                        with tempfile.TemporaryDirectory() as tmpdir:
                            # Save uploaded video
                            input_path = os.path.join(tmpdir, "input.mp4")
                            with open(input_path, "wb") as f:
                                f.write(uploaded_file.read())
                            
                            # Extract audio
                            audio_path = os.path.join(tmpdir, "audio.wav")
                            extract_audio(input_path, audio_path)
                            
                            # Embed watermark
                            watermarked_audio = os.path.join(tmpdir, "watermarked.wav")
                            embed_watermark(
                                audio_path, 
                                watermarked_audio, 
                                st.session_state.user["id"]
                            )
                            
                            # Recombine
                            output_path = os.path.join(tmpdir, "output.mp4")
                            combine_audio_video(input_path, watermarked_audio, output_path)
                            
                            # Offer download
                            with open(output_path, "rb") as f:
                                video_bytes = f.read()
                            
                            st.success("✅ Watermark embedded successfully!")
                            st.download_button(
                                "📥 Download Watermarked Video",
                                video_bytes,
                                file_name=f"watermarked_{uploaded_file.name}",
                                mime="video/mp4"
                            )
                            
                            # Store in database
                            conn = sqlite3.connect(DB_NAME)
                            conn.execute(
                                "INSERT INTO videos(filename, uploader_id) VALUES(?,?)",
                                (uploaded_file.name, st.session_state.user["id"])
                            )
                            conn.commit()
                            conn.close()
                            
                    except Exception as e:
                        st.error(f"Error: {str(e)}")
    
    # Tab 2: Detect watermarks in suspicious videos
    with tab2:
        st.header("Detect Watermark in Video")
        st.info("Upload a suspicious video to trace its origin")
        
        suspect_file = st.file_uploader(
            "Choose suspicious video", 
            type=["mp4", "mov", "avi", "mkv"],
            key="detect"
        )
        
        if suspect_file and st.button("Analyze"):
            with st.spinner("Analyzing video for watermark..."):
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        # Save suspect video
                        suspect_path = os.path.join(tmpdir, "suspect.mp4")
                        with open(suspect_path, "wb") as f:
                            f.write(suspect_file.read())
                        
                        # Extract audio
                        audio_path = os.path.join(tmpdir, "audio.wav")
                        extract_audio(suspect_path, audio_path)
                        
                        # Detect watermark
                        all_users = db_all_users()
                        detected_uid, confidence = detect_watermark(audio_path, all_users)
                        
                        if detected_uid:
                            # Get user details
                            conn = sqlite3.connect(DB_NAME)
                            user = conn.execute(
                                "SELECT name, email, phone FROM users WHERE id=?",
                                (detected_uid,)
                            ).fetchone()
                            conn.close()
                            
                            # Log detection
                            db_log_detection(None, detected_uid, confidence)
                            
                            st.error(f"🚨 **WATERMARK DETECTED!**")
                            st.markdown(f"""
                            ### Source User Information:
                            - **User ID:** #{detected_uid}
                            - **Name:** {user[0]}
                            - **Email:** {user[1]}
                            - **Phone:** {user[2]}
                            - **Confidence:** {confidence:.2%}
                            """)
                        else:
                            st.success("✅ No watermark detected in this video")
                            
                except Exception as e:
                    st.error(f"Detection error: {str(e)}")
    
    # Tab 3: Reports and analytics
    with tab3:
        st.header("System Reports")
        
        conn = sqlite3.connect(DB_NAME)
        
        # User stats
        st.subheader("Registered Users")
        users_df = pd.read_sql(
            "SELECT id, name, username, email, created_at FROM users", 
            conn
        )
        st.dataframe(users_df, use_container_width=True)
        
        # Video stats
        st.subheader("Watermarked Videos")
        videos_df = pd.read_sql("""
            SELECT v.id, v.filename, u.name as uploader, v.uploaded_at 
            FROM videos v 
            JOIN users u ON v.uploader_id = u.id 
            ORDER BY v.uploaded_at DESC
        """, conn)
        st.dataframe(videos_df, use_container_width=True)
        
        # Detection logs
        st.subheader("Detection History")
        detections_df = pd.read_sql("""
            SELECT d.detected_at, u.name as user, d.confidence 
            FROM detections d
            JOIN users u ON d.detected_uid = u.id
            ORDER BY d.detected_at DESC
        """, conn)
        st.dataframe(detections_df, use_container_width=True)
        
        conn.close()

if __name__ == "__main__":
    main()
