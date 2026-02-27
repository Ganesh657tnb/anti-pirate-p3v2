import streamlit as st
import numpy as np
import wave, os, sqlite3, tempfile, subprocess, bcrypt
from Cryptodome.Cipher import AES
from Cryptodome.Util import Counter

# --- CONFIGURATION ---
DB_NAME = "guardian_v5.db" 
UPLOAD_DIR = "master_videos"
SECRET_KEY = b'SixteenByteKey!!' 
SYNC_HEADER = "10101011" # The magic key to find the start
GAIN_FACTOR = 0.02 # Increased gain for solid detection

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR, exist_ok=True)

# --- 1. CRYPTO & SYNC LOGIC ---
def get_watermark_bits(user_id):
    # AES Encryption
    ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
    cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
    padded = user_id.ljust(16, ' ').encode('utf-8')
    encrypted = cipher.encrypt(padded)
    payload_bits = "".join(format(b, '08b') for b in encrypted)
    # Final String: Header + Payload
    return SYNC_HEADER + payload_bits

def recover_from_bits(full_bit_str):
    if SYNC_HEADER not in full_bit_str:
        return None
    
    # Find the start of the header
    start_idx = full_bit_str.find(SYNC_HEADER) + len(SYNC_HEADER)
    payload_bits = full_bit_str[start_idx : start_idx + 128]
    
    if len(payload_bits) < 128: return None
    
    try:
        byte_data = bytes(int(payload_bits[i:i+8], 2) for i in range(0, 128, 8))
        ctr = Counter.new(64, prefix=b'GUARDIAN', initial_value=1)
        cipher = AES.new(SECRET_KEY, AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(byte_data).decode('utf-8', errors='ignore').strip()
    except: return None

# --- 2. DSSS ENGINE ---
def get_pn_sequence(n):
    np.random.seed(123) # Global seed
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
    
    rms = np.sqrt(np.mean(audio_seg**2)) + 1e-6
    return np.clip(audio_seg + (GAIN_FACTOR * watermark * rms), -32768, 32767)

# --- 3. VIDEO PROCESSING ---
def process_video_download(video_path, user_id):
    full_bits = get_watermark_bits(user_id)
    with tempfile.TemporaryDirectory() as tmp:
        a_in, a_out, v_final = os.path.join(tmp, "1.wav"), os.path.join(tmp, "2.wav"), os.path.join(tmp, "out.mp4")
        subprocess.run(["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", a_in], capture_output=True)
        
        with wave.open(a_in, 'rb') as wav:
            params = wav.getparams()
            audio = np.frombuffer(wav.readframes(params.nframes), dtype=np.int16).astype(np.float32)
        
        # We embed the whole payload multiple times for redundancy
        block_size = 10 * 44100 # 10 second blocks
        processed = []
        for i in range(0, len(audio), block_size):
            seg = audio[i:i+block_size]
            if len(seg) == block_size:
                processed.append(embed_dss(seg, full_bits))
            else:
                processed.append(seg)
        
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
        
        block_size = 10 * 44100
        # Check first 2 blocks and find the best match
        for i in range(0, len(audio) - block_size + 1, block_size):
            seg = audio[i : i+block_size]
            pn = get_pn_sequence(len(seg))
            bit_len = 8 + 128 # Header + Payload
            spb = len(seg) // bit_len
            
            # Extract raw bits
            extracted_bits = ""
            for b in range(bit_len):
                corr = np.sum(seg[b*spb:(b+1)*spb] * pn[b*spb:(b+1)*spb])
                extracted_bits += "1" if corr > 0 else "0"
            
            res = recover_from_bits(extracted_bits)
            if res: return res
        return None

# --- 4. MAIN APP ---
def main():
    st.set_page_config(page_title="Guardian Sync-Lock v5")
    # (Rest of the UI logic like Login/Register from previous version goes here)
    # Make sure to initialize the DB and session state
    if "user" not in st.session_state: st.session_state.user = None
    
    # ... (Standard UI code) ...
    # Quick UI for testing:
    st.title("🛡️ Guardian Sync-Lock v5")
    
    # For speed, I'm adding a simple Register/Login here
    if not st.session_state.user:
        u = st.text_input("User ID")
        if st.button("Start Testing"):
            st.session_state.user = u
            st.rerun()
    else:
        st.write(f"Testing as: {st.session_state.user}")
        up = st.file_uploader("Upload Video", type=['mp4'])
        if up:
            if st.button("Download Secured"):
                # Save master first
                with open(os.path.join(UPLOAD_DIR, up.name), "wb") as f: f.write(up.read())
                data = process_video_download(os.path.join(UPLOAD_DIR, up.name), st.session_state.user)
                st.download_button("Download Now", data, file_name="secured.mp4")
            
            leak = st.file_uploader("Test Detection")
            if leak and st.button("Analyze"):
                res = detect_watermark(leak)
                if res: st.success(f"PIRATER FOUND: {res}")
                else: st.error("Detection Failed. Still out of Sync.")

if __name__ == "__main__":
    main()
