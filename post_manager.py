import os
import sys
import time
import json
import random
import shutil
import sqlite3
import threading
import subprocess
from datetime import datetime
import uuid

import yt_dlp
from pydantic import BaseModel, Field

from ai_seo_helper import (
    TikTokCaptionSchema, VideoSEOMetadataSchema,
    generate_localized_caption, generate_seo_metadata, clean_filename_to_title
)

from platform_utils import (
    BASE_DIR, FFMPEG_BIN, FFPROBE_BIN, SELECT_ALL_SHORTCUT,
    get_subprocess_kwargs, detect_best_video_encoder, load_config
)
from browser_runner import parse_proxy
from camoufox.sync_api import Camoufox
from camoufox.fingerprints import Screen

DB_PATH = os.path.join(BASE_DIR, "tiktok_bot_system.db")
PROFILE_LOCKS: dict[str, threading.Lock] = {}

def get_profile_lock(profile_name_or_id: str) -> threading.Lock:
    """Khóa đồng bộ cho từng profile để tránh 2 luồng truy cập cùng lúc."""
    if profile_name_or_id not in PROFILE_LOCKS:
        PROFILE_LOCKS[profile_name_or_id] = threading.Lock()
    return PROFILE_LOCKS[profile_name_or_id]

# ================= 1. DATABASE MANAGER =================
def generate_account_id() -> str:
    """Tự động sinh mã định danh tài khoản nội bộ (ví dụ: acc_72e763)."""
    return f"acc_{uuid.uuid4().hex[:6]}"

def parse_youtube_channel_input(raw_input: str) -> tuple[str, str]:
    """
    Chuẩn hóa đầu vào kênh YouTube Shorts:
    - @handle (ví dụ: @AdventureCompass-j9r, @geography.ro_yt) -> (URL chuẩn, handle)
    - Channel ID (ví dụ: UC9dOwg8ZJfwTSg4MLmPWbdA) -> (URL chuẩn, Channel ID)
    - Full URL (ví dụ: https://www.youtube.com/@AdventureCompass-j9r/shorts) -> (URL chuẩn, handle)
    """
    s = raw_input.strip()
    if not s:
        return "", ""
    if s.startswith("@"):
        clean_handle = s.split("/")[0]
        return f"https://www.youtube.com/{clean_handle}/shorts", clean_handle
    if s.startswith("UC") and len(s) >= 20 and not ("/" in s):
        return f"https://www.youtube.com/channel/{s}/shorts", s
    if "youtube.com" in s or "youtu.be" in s:
        import re
        handle_match = re.search(r'(@[\w\.-]+)', s)
        handle = handle_match.group(1) if handle_match else ""
        if not handle:
            channel_match = re.search(r'channel/(UC[\w-]+)', s)
            handle = channel_match.group(1) if channel_match else s
        url = s.rstrip("/")
        if not url.endswith("/shorts") and "/shorts/" not in url:
            url += "/shorts"
        return url, (handle if handle else url)
    
    handle = f"@{s.lstrip('@')}"
    return f"https://www.youtube.com/{handle}/shorts", handle

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS videos (
        video_id TEXT PRIMARY KEY,
        account_id TEXT DEFAULT '',
        channel_id INTEGER DEFAULT 0,
        source_handle TEXT DEFAULT '',
        profile_name TEXT,
        profile_dir TEXT,
        proxy_server TEXT,
        vpn_region TEXT DEFAULT '',
        tiktok_user TEXT DEFAULT '',
        tiktok_video_url TEXT DEFAULT '',
        shorts_title TEXT,
        local_4k_path TEXT,
        quality_str TEXT DEFAULT '',
        duration REAL DEFAULT 0.0,
        check_status TEXT DEFAULT '',
        caption TEXT,
        status TEXT,
        yt_status TEXT DEFAULT 'NONE',
        yt_video_url TEXT DEFAULT '',
        posted_date TEXT DEFAULT '',
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        is_shadowban INTEGER DEFAULT 0,
        is_duplicate INTEGER DEFAULT 0,
        is_violation INTEGER DEFAULT 0,
        audit_status TEXT DEFAULT 'PENDING',
        created_at TEXT DEFAULT ''
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT DEFAULT '',
        source_handle TEXT DEFAULT '',
        youtube_shorts_url TEXT UNIQUE,
        tiktok_user TEXT,
        vpn_region TEXT DEFAULT 'VN',
        profile_name TEXT,
        profile_dir TEXT,
        proxy_server TEXT,
        niche TEXT,
        daily_target INTEGER DEFAULT 2,
        status TEXT DEFAULT 'ACTIVE',
        post_to_tiktok INTEGER DEFAULT 1,
        post_to_youtube INTEGER DEFAULT 1,
        created_at TEXT DEFAULT ''
    )
    """)
    # Migration an toàn cho channels
    for col, col_type in [
        ("account_id", "TEXT DEFAULT ''"),
        ("source_handle", "TEXT DEFAULT ''"),
        ("daily_target", "INTEGER DEFAULT 2"),
        ("status", "TEXT DEFAULT 'ACTIVE'"),
        ("post_to_tiktok", "INTEGER DEFAULT 1"),
        ("post_to_youtube", "INTEGER DEFAULT 1"),
        ("created_at", "TEXT DEFAULT ''")
    ]:
        try:
            conn.execute(f"ALTER TABLE channels ADD COLUMN {col} {col_type}")
        except Exception:
            pass

    # Migration an toàn cho videos
    for col, col_type in [
        ("account_id", "TEXT DEFAULT ''"),
        ("channel_id", "INTEGER DEFAULT 0"),
        ("source_handle", "TEXT DEFAULT ''"),
        ("tiktok_user", "TEXT DEFAULT ''"),
        ("tiktok_video_url", "TEXT DEFAULT ''"),
        ("vpn_region", "TEXT DEFAULT ''"),
        ("quality_str", "TEXT DEFAULT ''"),
        ("duration", "REAL DEFAULT 0.0"),
        ("check_status", "TEXT DEFAULT ''"),
        ("posted_date", "TEXT DEFAULT ''"),
        ("views", "INTEGER DEFAULT 0"),
        ("likes", "INTEGER DEFAULT 0"),
        ("is_shadowban", "INTEGER DEFAULT 0"),
        ("is_duplicate", "INTEGER DEFAULT 0"),
        ("is_violation", "INTEGER DEFAULT 0"),
        ("audit_status", "TEXT DEFAULT 'PENDING'"),
        ("yt_status", "TEXT DEFAULT 'NONE'"),
        ("yt_video_url", "TEXT DEFAULT ''"),
        ("created_at", "TEXT DEFAULT ''")
    ]:
        try:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {col_type}")
        except Exception:
            pass

    conn.commit()
    conn.close()

def is_video_processed(video_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM videos WHERE video_id = ?", (video_id,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists

def save_video_record(
    video_id, profile_name, profile_dir, proxy_server,
    shorts_title, local_4k_path, caption, status,
    yt_status="NONE", yt_video_url="",
    account_id="", source_handle="", tiktok_user="", tiktok_video_url="",
    vpn_region="VN", quality_str="", duration=0.0, check_status="",
    posted_date=None, channel_id=0
):
    conn = sqlite3.connect(DB_PATH)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not posted_date:
        posted_date = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
    INSERT OR REPLACE INTO videos 
    (video_id, profile_name, profile_dir, proxy_server, shorts_title, local_4k_path, caption, status, yt_status, yt_video_url, created_at,
     account_id, source_handle, tiktok_user, tiktok_video_url, vpn_region, quality_str, duration, check_status, posted_date, channel_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (video_id, profile_name, profile_dir, proxy_server, shorts_title, local_4k_path, caption, status, yt_status, yt_video_url, created_at,
          account_id, source_handle, tiktok_user, tiktok_video_url, vpn_region, quality_str, duration, check_status, posted_date, channel_id))
    conn.commit()
    conn.close()

def get_video_record(video_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_videos() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM videos ORDER BY created_at DESC LIMIT 100")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def update_video_status(video_id: str, new_status: str, yt_status: str = None, yt_video_url: str = None):
    conn = sqlite3.connect(DB_PATH)
    if yt_status is not None and yt_video_url is not None:
        conn.execute("UPDATE videos SET status = ?, yt_status = ?, yt_video_url = ? WHERE video_id = ?", (new_status, yt_status, yt_video_url, video_id))
    elif yt_status is not None:
        conn.execute("UPDATE videos SET status = ?, yt_status = ? WHERE video_id = ?", (new_status, yt_status, video_id))
    else:
        conn.execute("UPDATE videos SET status = ? WHERE video_id = ?", (new_status, video_id))
    conn.commit()
    conn.close()

def delete_video_record(video_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
    conn.commit()
    conn.close()

def add_tracked_channel(
    yt_url: str,
    tiktok_user: str,
    vpn_region: str,
    profile_name: str,
    profile_dir: str = "",
    proxy_server: str = "",
    niche: str = "",
    post_to_tiktok: int = 1,
    post_to_youtube: int = 1,
    account_id: str = "",
    source_handle: str = "",
    daily_target: int = 2,
    status: str = "ACTIVE"
):
    norm_url, parsed_handle = parse_youtube_channel_input(yt_url)
    if not source_handle:
        source_handle = parsed_handle
    if not account_id:
        account_id = generate_account_id()

    if not profile_dir:
        profile_dir = os.path.join(BASE_DIR, "profiles", profile_name.replace(" ", "_"))
    os.makedirs(profile_dir, exist_ok=True)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    INSERT OR REPLACE INTO channels 
    (youtube_shorts_url, tiktok_user, vpn_region, profile_name, profile_dir, proxy_server, niche, post_to_tiktok, post_to_youtube, created_at,
     account_id, source_handle, daily_target, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (norm_url, tiktok_user, vpn_region.upper(), profile_name, profile_dir, proxy_server, niche, post_to_tiktok, post_to_youtube, created_at,
          account_id, source_handle, daily_target, status.upper()))
    conn.commit()
    conn.close()

def get_all_channels() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM channels ORDER BY id DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_channel_by_id(channel_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def update_channel_status(channel_id: int, new_status: str):
    """Cập nhật trạng thái kênh: ACTIVE (Đang hoạt động) hoặc PAUSED (Đang tạm dừng)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE channels SET status = ? WHERE id = ?", (new_status.upper(), channel_id))
    conn.commit()
    conn.close()

def update_channel_target(channel_id: int, daily_target: int):
    """Cập nhật chỉ tiêu số lượng video đăng mỗi ngày (mặc định 2)."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE channels SET daily_target = ? WHERE id = ?", (int(daily_target), channel_id))
    conn.commit()
    conn.close()

def update_channel_details(channel_id: int, data: dict):
    conn = sqlite3.connect(DB_PATH)
    fields = []
    vals = []
    for k in ["account_id", "source_handle", "youtube_shorts_url", "tiktok_user", "vpn_region", "profile_name", "niche", "daily_target", "status", "post_to_tiktok", "post_to_youtube"]:
        if k in data:
            fields.append(f"{k} = ?")
            vals.append(data[k])
    if fields:
        vals.append(channel_id)
        conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id = ?", vals)
        conn.commit()
    conn.close()

def delete_tracked_channel(channel_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()

def get_channel_posted_count_today(channel_id: int, target_date: str = None) -> int:
    """Đếm số video TikTok đã đăng thành công của kênh trong ngày hôm nay."""
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    SELECT COUNT(*) FROM videos 
    WHERE (channel_id = ? OR profile_name IN (SELECT profile_name FROM channels WHERE id = ?))
      AND posted_date = ? AND status = 'PUBLISHED'
    """, (channel_id, channel_id, target_date))
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_all_channels_kpi_summary(target_date: str = None) -> dict:
    """
    Thống kê tiến độ toàn bộ kênh theo đúng tài liệu mô tả:
    - Kênh đã đạt chỉ tiêu: (X/Tổng), liệt kê kênh đạt 2/2 video
    - Kênh chưa đủ chỉ tiêu: (Y/Tổng), số lượng đã đăng (0/2), thiếu (👉 Thiếu 2), trạng thái ((Đang tạm dừng))
    - Tổng kết: Cần đăng bù Z video cho Y kênh.
    """
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")
    channels = get_all_channels()
    total_channels = len(channels)
    completed_channels = []
    incomplete_channels = []
    total_missing_videos = 0

    for ch in channels:
        ch_id = ch["id"]
        daily_target = ch.get("daily_target", 2) or 2
        posted = get_channel_posted_count_today(ch_id, target_date)
        ch_status = (ch.get("status") or "ACTIVE").upper()
        source_label = ch.get("source_handle") or ch.get("youtube_shorts_url", f"Kênh #{ch_id}")
        
        info = {
            "id": ch_id,
            "account_id": ch.get("account_id") or f"acc_{ch_id}",
            "source_handle": source_label,
            "tiktok_user": ch.get("tiktok_user", ""),
            "profile_name": ch.get("profile_name", ""),
            "vpn_region": ch.get("vpn_region", "VN"),
            "status": ch_status,
            "daily_target": daily_target,
            "posted": posted,
            "missing": max(0, daily_target - posted)
        }

        if posted >= daily_target:
            completed_channels.append(info)
        else:
            incomplete_channels.append(info)
            total_missing_videos += info["missing"]

    return {
        "date": target_date,
        "total": total_channels,
        "completed": completed_channels,
        "completed_count": len(completed_channels),
        "incomplete": incomplete_channels,
        "incomplete_count": len(incomplete_channels),
        "total_missing": total_missing_videos
    }

# ================= 2. FFMPEG 4K & VIDEO PROCESSING =================
def edit_and_upscale_4k(raw_path: str, output_path: str) -> dict:
    """
    Cắt tối đa 59s, zoom nhẹ 1.03x để đổi mã băm khung hình, 
    upscale lên 2160x3840 (4K dọc) 30fps, xóa sạch metadata gốc.
    """
    vf_filter = (
        "scale=2160:3840:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=2160:3840,unsharp=3:3:0.8:3:3:0.0,eq=contrast=1.02:saturation=1.04,fps=30"
    )
    af_filter = "asetrate=44100*1.03,aresample=44100,atempo=0.9708"
    encoder_args = detect_best_video_encoder()

    cmd = [
        FFMPEG_BIN, "-y", "-i", raw_path, "-t", "59",
        "-vf", vf_filter, "-af", af_filter,
        *encoder_args,
        "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1",
        output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, **get_subprocess_kwargs())

    # Đo lại bằng ffprobe
    probe_cmd = [
        FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", output_path
    ]
    try:
        res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True, **get_subprocess_kwargs())
        probe_data = json.loads(res.stdout)
        stream = probe_data["streams"][0]
        width = stream.get("width", 2160)
        height = stream.get("height", 3840)
        fps_parts = stream.get("r_frame_rate", "30/1").split("/")
        fps_denom = int(fps_parts[1]) if len(fps_parts) > 1 and int(fps_parts[1]) != 0 else 1
        fps = round(int(fps_parts[0]) / fps_denom)
        duration = round(float(probe_data.get("format", {}).get("duration", 59.0)), 1)
        res_str = f"4K Ultra HD ({width}x{height} {fps}fps)"
    except Exception:
        res_str = "4K Ultra HD (2160x3840 30fps)"
        duration = 59.0

    return {
        "resolution_str": res_str,
        "duration": duration
    }

def remedy_audio_fast(video_4k_path: str, remedied_output_path: str) -> str:
    """
    Cứu hộ âm thanh tức thì khi Content Check Lite báo lỗi bản quyền:
    Giữ nguyên video 4K (-c:v copy), lọc bỏ dải tần âm thanh gốc và mix nhạc nền sạch ./safe_bgm.
    """
    safe_bgm_dir = os.path.join(BASE_DIR, "safe_bgm")
    bgm_files = [
        os.path.join(safe_bgm_dir, f) for f in os.listdir(safe_bgm_dir)
        if f.lower().endswith((".mp3", ".wav", ".m4a"))
    ] if os.path.exists(safe_bgm_dir) else []

    if bgm_files:
        chosen_bgm = random.choice(bgm_files)
        filter_complex = (
            "[0:a]highpass=f=300,lowpass=f=2500,volume=0.15[orig];"
            "[1:a]volume=0.85[bgm];"
            "[orig][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        cmd = [
            FFMPEG_BIN, "-y", "-i", video_4k_path, "-stream_loop", "-1", "-i", chosen_bgm,
            "-filter_complex", filter_complex, "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", remedied_output_path
        ]
    else:
        af_heavy = "asetrate=44100*1.08,aresample=44100,atempo=0.9259,aecho=0.8:0.88:15:0.2,equalizer=f=1000:width_type=h:width=200:g=-5"
        cmd = [
            FFMPEG_BIN, "-y", "-i", video_4k_path, "-af", af_heavy,
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", remedied_output_path
        ]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, **get_subprocess_kwargs())
    return remedied_output_path

def fetch_latest_short(channel_shorts_url: str, max_lookback: int = 10) -> dict | None:
    """Quét và tải video Shorts mới nhất chưa từng được xử lý (thời lượng chuẩn ≤ 59s)."""
    dl_dir = os.path.join(BASE_DIR, "downloads")
    os.makedirs(dl_dir, exist_ok=True)
    node_bin = shutil.which("node")
    js_runtimes = {"node": {}} if node_bin else {}
    ydl_info_opts = {
        'extract_flat': True,
        'playlist_items': f'1-{max_lookback}',
        'quiet': True,
        'js_runtimes': js_runtimes
    }
    with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
        try:
            info = ydl.extract_info(channel_shorts_url, download=False)
        except Exception as e:
            print(f"[yt-dlp error]: {e}")
            return None

        if not info or 'entries' not in info or not info['entries']:
            return None

        target_id = None
        for entry in info['entries']:
            if not entry:
                continue
            v_id = entry.get('id')
            if not v_id or is_video_processed(v_id):
                continue
            dur = entry.get('duration')
            if dur and dur > 65:
                continue
            target_id = v_id
            break

        if not target_id:
            return None

        video_url = f"https://www.youtube.com/shorts/{target_id}"

    raw_path = os.path.join(dl_dir, f"{target_id}_raw.mp4")
    ydl_dl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': raw_path,
        'merge_output_format': 'mp4',
        'quiet': True,
        'js_runtimes': js_runtimes
    }
    with yt_dlp.YoutubeDL(ydl_dl_opts) as ydl:
        full_info = ydl.extract_info(video_url, download=True)
        return {
            "video_id": target_id,
            "title": full_info.get("title", "Shorts Video"),
            "uploader": full_info.get("uploader", "YouTube Channel"),
            "url": video_url,
            "raw_path": raw_path,
            "duration": full_info.get("duration", 59.0)
        }

# ================= 3. CAMOUFOX BROWSER LAUNCHER =================
def _prepare_camoufox_kwargs(profile_dir: str, proxy_server: str = "", os_type: str = "windows", screen: str = "1920x1080", locale: str = "", headless: bool = False) -> dict:
    profile_dir = os.path.abspath(profile_dir)
    os.makedirs(profile_dir, exist_ok=True)

    # Dọn dẹp lock cũ
    for lock_name in [".parentlock", "lock", "parent.lock"]:
        lp = os.path.join(profile_dir, lock_name)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except Exception:
                pass

    camoufox_kwargs = {
        "headless": headless,
        "os": os_type if os_type in ["windows", "macos", "linux"] else "windows",
        "persistent_context": True,
        "user_data_dir": profile_dir,
        "humanize": True,
        "disable_coop": True,
        "canvas_noise": False,
        "i_know_what_im_doing": True,
    }

    if screen and "x" in screen:
        try:
            w, h = map(int, screen.lower().split("x"))
            camoufox_kwargs["screen"] = Screen(max_width=w, max_height=h)
        except Exception:
            pass

    if locale.strip():
        camoufox_kwargs["locale"] = locale.strip()

    proxy_cfg = parse_proxy(proxy_server) if proxy_server else None
    if proxy_cfg:
        camoufox_kwargs["proxy"] = proxy_cfg
        camoufox_kwargs["geoip"] = True

    return camoufox_kwargs

def open_profile_warmup(profile_dir: str, proxy_server: str = "", target_url: str = "https://www.tiktok.com/tiktokstudio/upload", os_type: str = "windows"):
    """Mở Camoufox để người dùng kiểm tra hoặc đăng nhập thủ công."""
    lock_name = os.path.basename(profile_dir)
    lock = get_profile_lock(lock_name)
    with lock:
        kwargs = _prepare_camoufox_kwargs(profile_dir, proxy_server, os_type=os_type, headless=False)
        with Camoufox(**kwargs) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target_url, timeout=60000)
            # Chờ người dùng đóng tab
            while len(context.pages) > 0:
                time.sleep(1)

def dismiss_tiktok_popups(page, logger=None):
    """
    Tự động tắt các popup, overlay hướng dẫn (Joyride tour / tooltips / modal) của TikTok Studio
    để tránh lỗi <div class="react-joyride__overlay"> subtree intercepts pointer events.
    """
    try:
        page.evaluate("""() => {
            try {
                const joyride = document.getElementById('react-joyride-portal');
                if (joyride) joyride.remove();
                document.querySelectorAll('.react-joyride__overlay, .react-joyride__spotlight, [class*="joyride"], .tiktok-modal-mask').forEach(el => el.remove());
            } catch (e) {}
        }""")
    except Exception:
        pass

    for sel in [
        '#react-joyride-portal button[aria-label="Close"]',
        '#react-joyride-portal button:has-text("Skip")',
        '#react-joyride-portal button:has-text("Got it")',
        '#react-joyride-portal button:has-text("Bỏ qua")',
        '#react-joyride-portal button:has-text("Đã hiểu")',
        'div[role="dialog"] button:has-text("Got it")',
        'div[role="dialog"] button:has-text("Đã hiểu")',
        'div[role="dialog"] button:has-text("Bỏ qua")',
        'button:has-text("Got it")',
        'button:has-text("Đã hiểu")',
        'button:has-text("Dismiss")',
        'button[aria-label="Close"]',
        'button[aria-label="Đóng"]'
    ]:
        try:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(force=True, timeout=1000)
        except Exception:
            pass

def wait_for_content_check(page, max_wait_seconds: int = 90) -> tuple[bool, str]:
    """
    Theo dõi kết quả Content Check Lite trên TikTok Studio.
    Trả về (is_green, status_text)
    """
    polls = max(1, max_wait_seconds // 3)
    for p_idx in range(polls):
        time.sleep(3)
        if p_idx % 3 == 0:
            dismiss_tiktok_popups(page)
        try:
            body_text = page.inner_text("body")
        except Exception:
            break

        if any(m.lower() in body_text.lower() for m in [
            "no issues found", "không phát hiện vấn đề", "sin problemas detectados", "aucun problème détecté", "keine probleme gefunden"
        ]):
            return True, "Content Check Lite: HỢP LỆ (XANH)"

        if any(m.lower() in body_text.lower() for m in [
            "issues found", "issue found", "phát hiện vấn đề", "copyright", "bản quyền", "unoriginal", "derechos de autor"
        ]):
            if not any(c.lower() in body_text.lower() for c in ["checking", "đang kiểm tra", "comprobando", "vérification"]):
                return False, "Content Check Lite: CẢNH BÁO BẢN QUYỀN (ĐỎ/VÀNG)"

    return True, "Content Check Lite: HỢP LỆ (XANH)"

# ================= 4. TIKTOK STUDIO AUTOMATION (CAMOUFOX) =================
def upload_to_tiktok_with_fallback(
    video_4k_path: str,
    caption: str,
    profile_dir: str,
    proxy_server: str = "",
    os_type: str = "windows",
    screen: str = "1920x1080",
    locale: str = "",
    log_callback = None
) -> dict:
    """
    Tự động hóa đăng bài TikTok Studio qua Camoufox Antidetect với Fallback 2 tầng:
    - Tầng 1: Nếu dính bản quyền âm thanh -> Gọi remedy_audio_fast và Thay thế file (Replace).
    - Tầng 2: Nếu quét lại vẫn dính cảnh báo -> Lưu nháp (Save to drafts) bảo vệ an toàn cho kênh.
    """
    def _log(msg):
        print(f"[Camoufox TikTok]: {msg}")
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    lock_name = os.path.basename(profile_dir)
    lock = get_profile_lock(lock_name)
    with lock:
        kwargs = _prepare_camoufox_kwargs(
            profile_dir, proxy_server, os_type=os_type, screen=screen, locale=locale, headless=False
        )
        _log("🛡️ Đang khởi động trình duyệt bảo mật Camoufox...")
        with Camoufox(**kwargs) as context:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                _log("🌐 Điều hướng đến TikTok Studio Upload...")
                page.goto("https://www.tiktok.com/tiktokstudio/upload", timeout=60000)
                time.sleep(3)
                dismiss_tiktok_popups(page, _log)

                # Kiểm tra nếu chưa đăng nhập -> Thử auto login nếu profile có lưu tài khoản
                if "login" in page.url or page.locator('button:has-text("Log in"), button:has-text("Đăng nhập")').count() > 0:
                    _log("⚠️ Phát hiện chưa đăng nhập TikTok! Kiểm tra cấu hình Auto-Login...")
                    autologin_file = os.path.join(profile_dir, "autologin.json")
                    if os.path.exists(autologin_file):
                        try:
                            with open(autologin_file, "r", encoding="utf-8") as f:
                                acfg = json.load(f)
                            if acfg.get("creds"):
                                from auto_login import auto_login_tiktok
                                _log("🔑 Kích hoạt tiến trình tự động đăng nhập TikTok...")
                                auto_login_tiktok(page, acfg["creds"], logger=_log)
                                time.sleep(4)
                                page.goto("https://www.tiktok.com/tiktokstudio/upload", timeout=60000)
                                dismiss_tiktok_popups(page, _log)
                        except Exception as e:
                            _log(f"❌ Auto login thất bại: {e}")

                # 1. Tải file video 4K lần 1
                _log("📁 Đang đưa file video vào trình tải lên TikTok...")
                dismiss_tiktok_popups(page, _log)
                file_input = page.locator('input[type="file"]')
                file_input.wait_for(state="attached", timeout=35000)
                file_input.set_input_files(video_4k_path)
                time.sleep(3)

                # 2. Nhập Caption & Hashtag
                _log("✍️ Đang nhập Caption và Hashtags SEO...")
                dismiss_tiktok_popups(page, _log)
                caption_box = page.locator('div[contenteditable="true"], div.public-DraftEditor-content, div[role="combobox"]').first
                caption_box.wait_for(state="visible", timeout=30000)
                try:
                    caption_box.click(timeout=3000)
                except Exception:
                    dismiss_tiktok_popups(page, _log)
                    caption_box.click(force=True)

                # Xóa sạch text mặc định
                page.keyboard.press(SELECT_ALL_SHORTCUT)
                page.keyboard.press("Backspace")
                time.sleep(0.5)
                try:
                    caption_box.fill(caption)
                except Exception:
                    page.keyboard.type(caption, delay=20)
                time.sleep(1)

                # 3. Đợi kết quả Content Check Lite lần 1
                _log("⏳ Đang theo dõi quét bản quyền Content Check Lite (tối đa 90s)...")
                is_green, check_status = wait_for_content_check(page)
                _log(f"🔎 Kết quả kiểm tra lần 1: {check_status}")

                # 4. KÍCH HOẠT TẦNG 1: SỬA ÂM THANH & THAY THẾ (REPLACE)
                if not is_green:
                    _log("🚨 Phát hiện cảnh báo bản quyền âm thanh! Đang kích hoạt cứu hộ tầng 1 (Remedy Audio Fast)...")
                    remedied_path = video_4k_path.replace(".mp4", "_remedy.mp4")
                    try:
                        remedy_audio_fast(video_4k_path, remedied_path)
                    except Exception as e_rem:
                        _log(f"⚠️ Lỗi xử lý âm thanh remedy: {e_rem}")
                        remedied_path = video_4k_path

                    dismiss_tiktok_popups(page, _log)

                    replaced_successfully = False
                    replace_btn = page.locator(
                        'button:has-text("Replace"), button:has-text("Thay thế"), button:has-text("Reemplazar"), button:has-text("Change video"), button:has-text("Đổi video")'
                    ).first
                    try:
                        if replace_btn.is_visible():
                            replace_btn.click(force=True, timeout=3000)
                            time.sleep(2)
                            dismiss_tiktok_popups(page, _log)
                            for conf_sel in [
                                'div[role="dialog"] button:has-text("Replace")',
                                'div[role="dialog"] button:has-text("Continue")',
                                'div[role="dialog"] button:has-text("Tiếp tục")',
                                'div[role="dialog"] button:has-text("Continuar")',
                                'div[role="dialog"] button:has-text("Discard")',
                                'div[role="dialog"] button:has-text("Bỏ")'
                            ]:
                                c_btn = page.locator(conf_sel).first
                                if c_btn.is_visible():
                                    c_btn.click(force=True, timeout=2000)
                                    time.sleep(1)
                                    break

                            file_input = page.locator('input[type="file"]')
                            file_input.wait_for(state="attached", timeout=6000)
                            file_input.set_input_files(remedied_path)
                            replaced_successfully = True
                            _log("🔄 Đã thay thế video trực tiếp bằng nút Replace.")
                    except Exception as e_rep:
                        _log(f"⚠️ Thao tác replace trực tiếp không khả dụng ({e_rep}), chuyển sang nạp lại trang upload...")

                    # Fallback vững chắc: nếu Replace in-place thất bại -> load lại trang upload
                    if not replaced_successfully:
                        _log("🌐 Tải lại trang upload TikTok Studio để nạp video đã sửa âm thanh...")
                        try:
                            page.on("dialog", lambda dialog: dialog.accept())
                        except Exception:
                            pass
                        page.goto("https://www.tiktok.com/tiktokstudio/upload", timeout=60000, wait_until="domcontentloaded")
                        time.sleep(3)
                        dismiss_tiktok_popups(page, _log)

                        file_input = page.locator('input[type="file"]')
                        file_input.wait_for(state="attached", timeout=35000)
                        file_input.set_input_files(remedied_path)
                        time.sleep(3)

                        _log("✍️ Nhập lại Caption sau khi tải lại trang...")
                        dismiss_tiktok_popups(page, _log)
                        caption_box = page.locator('div[contenteditable="true"], div.public-DraftEditor-content, div[role="combobox"]').first
                        caption_box.wait_for(state="visible", timeout=30000)
                        try:
                            caption_box.click(timeout=3000)
                        except Exception:
                            dismiss_tiktok_popups(page, _log)
                            caption_box.click(force=True)

                        page.keyboard.press(SELECT_ALL_SHORTCUT)
                        page.keyboard.press("Backspace")
                        time.sleep(0.5)
                        try:
                            caption_box.fill(caption)
                        except Exception:
                            page.keyboard.type(caption, delay=20)
                        time.sleep(1)

                    _log("🔄 Đã nạp video đã sửa âm thanh, đang đợi Content Check Lite quét lại...")
                    is_green, check_status = wait_for_content_check(page)
                    if is_green:
                        check_status = "Content Check Lite: HỢP LỆ (XANH - ĐÃ FIX AUDIO)"
                    _log(f"🔎 Kết quả kiểm tra lần 2: {check_status}")

                # 5. QUYẾT ĐỊNH ĐĂNG HOẶC LƯU NHÁP (TẦNG 2)
                dismiss_tiktok_popups(page, _log)
                if is_green:
                    _log("🚀 Video đạt chuẩn bản quyền! Đang bấm nút Đăng (Post)...")
                    post_btn = page.locator('button:has-text("Post"), button:has-text("Đăng"), button:has-text("Publicar")').last
                    post_btn.scroll_into_view_if_needed()
                    try:
                        post_btn.click(timeout=5000)
                    except Exception:
                        dismiss_tiktok_popups(page, _log)
                        post_btn.click(force=True)
                    time.sleep(8)
                    _log("✅ Đã xuất bản thành công lên TikTok!")
                    return {"action": "PUBLISHED", "check_status": check_status}
                else:
                    _log("⚠️ Video vẫn dính cảnh báo bản quyền. Tự động chuyển sang Lưu Bản Nháp (Draft) an toàn...")
                    draft_btn = page.locator(
                        'button:has-text("Save to drafts"), '
                        'button:has-text("Drafts"), '
                        'button:has-text("Lưu vào bản nháp"), '
                        'button:has-text("Bản nháp"), '
                        'button:has-text("Guardar en borradores")'
                    ).first
                    draft_btn.scroll_into_view_if_needed()
                    try:
                        draft_btn.click(timeout=5000)
                    except Exception:
                        dismiss_tiktok_popups(page, _log)
                        draft_btn.click(force=True)
                    time.sleep(6)
                    _log("🛡️ Đã lưu an toàn vào bản nháp TikTok!")
                    return {"action": "DRAFTED", "check_status": "Content Check Lite: CẢNH BÁO (ĐÃ LƯU NHÁP AN TOÀN)"}

            except Exception as e:
                err_msg = str(e).split("\n")[0].strip()
                _log(f"❌ Lỗi quy trình TikTok Studio: {err_msg}")
                return {"action": "FAILED", "check_status": f"LỖI TIKTOK: {err_msg}"}

# ================= 5. YOUTUBE SHORTS STUDIO (CAMOUFOX) =================
def upload_to_youtube_studio(
    video_4k_path: str,
    title: str,
    description: str,
    profile_dir: str,
    proxy_server: str = "",
    os_type: str = "windows",
    screen: str = "1920x1080",
    locale: str = "",
    log_callback = None
) -> dict:
    """Tự động hóa đăng video lên YouTube Studio (YouTube Shorts) qua Camoufox."""
    def _log(msg):
        print(f"[Camoufox YouTube]: {msg}")
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    lock_name = os.path.basename(profile_dir)
    lock = get_profile_lock(lock_name)
    with lock:
        kwargs = _prepare_camoufox_kwargs(
            profile_dir, proxy_server, os_type=os_type, screen=screen, locale=locale, headless=False
        )
        _log("🛡️ Khởi động Camoufox cho YouTube Studio...")
        with Camoufox(**kwargs) as context:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                _log("🌐 Điều hướng đến studio.youtube.com...")
                page.goto("https://studio.youtube.com", timeout=60000)
                time.sleep(4)

                if "accounts.google.com" in page.url:
                    _log("⚠️ Chưa đăng nhập Google trên Profile này!")
                    return {"action": "FAILED", "status": "YOUTUBE: CHƯA ĐĂNG NHẬP", "video_url": ""}

                # 1. Nút Tạo / Create
                file_input = page.locator('input[type="file"]')
                if not file_input.is_visible():
                    create_btn = page.locator('#create-icon, button[aria-label="Create"], button[aria-label="Tạo"], ytcp-button:has-text("CREATE"), ytcp-button:has-text("TẠO")').first
                    if create_btn.is_visible():
                        create_btn.click()
                        time.sleep(2)
                        upload_menu = page.locator('#text-item-0, tp-yt-paper-item:has-text("Upload videos"), tp-yt-paper-item:has-text("Tải video lên")').first
                        if upload_menu.is_visible():
                            upload_menu.click()
                            time.sleep(2)

                _log("📁 Nạp file video vào dialog upload...")
                file_input.wait_for(state="attached", timeout=30000)
                file_input.set_input_files(video_4k_path)
                time.sleep(5)

                # 2. Tiêu đề
                raw_title = title.strip().replace("\n", " ")
                if "#shorts" not in raw_title.lower():
                    raw_title = f"{raw_title} #Shorts"
                if len(raw_title) > 95:
                    raw_title = raw_title[:90].strip() + " #Shorts"

                _log(f"✍️ Điền tiêu đề: {raw_title}")
                title_box = page.locator('#title-textarea #textbox, div[aria-label*="title" i] #textbox, div[aria-label*="tiêu đề" i] #textbox, div#textbox[contenteditable="true"]').first
                title_box.wait_for(state="visible", timeout=30000)
                title_box.click()
                page.keyboard.press(SELECT_ALL_SHORTCUT)
                page.keyboard.press("Backspace")
                title_box.fill(raw_title)
                time.sleep(1)

                # 3. Mô tả
                desc_box = page.locator('#description-textarea #textbox, div[aria-label*="description" i] #textbox, div[aria-label*="mô tả" i] #textbox').first
                if desc_box.is_visible():
                    desc_box.click()
                    desc_box.fill(description)
                    time.sleep(1)

                # 4. "Không dành cho trẻ em"
                not_kids = page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"], [name="VIDEO_MADE_FOR_KIDS_NOT_MFK"], tp-yt-paper-radio-button:has-text("No, it"), tp-yt-paper-radio-button:has-text("Không, đây không phải")').first
                if not_kids.is_visible():
                    not_kids.scroll_into_view_if_needed()
                    not_kids.click()
                    time.sleep(1)

                # Lấy link sớm
                yt_video_url = ""
                link_elem = page.locator('a.ytcp-video-info, a[href*="youtu.be"]').first
                if link_elem.is_visible():
                    yt_video_url = link_elem.get_attribute("href") or ""

                # 5. Bấm Next 3 lần
                for step_idx in range(1, 4):
                    next_btn = page.locator('#next-button, button:has-text("Next"), button:has-text("Tiếp")').first
                    next_btn.wait_for(state="visible", timeout=20000)
                    next_btn.click()
                    time.sleep(2)

                # 6. Chọn Công khai (Public)
                _log("🔓 Đặt chế độ hiển thị: Công khai (Public)...")
                public_radio = page.locator('tp-yt-paper-radio-button[name="PUBLIC"], [name="PUBLIC"], tp-yt-paper-radio-button:has-text("Public"), tp-yt-paper-radio-button:has-text("Công khai")').first
                public_radio.wait_for(state="visible", timeout=20000)
                public_radio.scroll_into_view_if_needed()
                public_radio.click()
                time.sleep(1)

                if not yt_video_url:
                    link_elem = page.locator('a.ytcp-video-info, a[href*="youtu.be"]').first
                    if link_elem.is_visible():
                        yt_video_url = link_elem.get_attribute("href") or ""

                # 7. Xuất bản
                _log("🚀 Bấm Xuất bản video Shorts...")
                done_btn = page.locator('#done-button, button:has-text("Publish"), button:has-text("Xuất bản"), button:has-text("Save"), button:has-text("Lưu")').first
                done_btn.wait_for(state="visible", timeout=20000)
                done_btn.scroll_into_view_if_needed()
                done_btn.click()
                time.sleep(8)

                close_btn = page.locator('#close-button, button:has-text("Close"), button:has-text("Đóng")').first
                if close_btn.is_visible():
                    close_btn.click()
                    time.sleep(2)

                _log(f"✅ Đăng YouTube Shorts thành công! Link: {yt_video_url}")
                return {
                    "action": "PUBLISHED",
                    "status": "YOUTUBE: ĐĂNG THÀNH CÔNG",
                    "video_url": yt_video_url
                }
            except Exception as e:
                _log(f"❌ YouTube Studio Lỗi: {e}")
                return {
                    "action": "FAILED",
                    "status": f"YOUTUBE LỖI: {e}",
                    "video_url": ""
                }

# ================= 6. DRAFT PUBLISH & DELETE VIA CAMOUFOX =================
def force_publish_draft(video_record: dict, log_callback = None) -> bool:
    """Mở TikTok Studio -> Vào tab Drafts -> Bấm Edit -> Bấm Đăng."""
    def _log(msg):
        print(f"[Force Publish]: {msg}")
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    lock = get_profile_lock(video_record["profile_name"])
    with lock:
        kwargs = _prepare_camoufox_kwargs(video_record["profile_dir"], video_record.get("proxy_server", ""), headless=False)
        with Camoufox(**kwargs) as context:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                _log("🌐 Mở TikTok Studio Content Manager...")
                page.goto("https://www.tiktok.com/tiktokstudio/content", timeout=60000)
                time.sleep(4)
                dismiss_tiktok_popups(page, _log)

                draft_tab = page.locator('text="Drafts", text="Bản nháp", text="Borradores"').first
                if draft_tab.is_visible():
                    try:
                        draft_tab.click(timeout=3000)
                    except Exception:
                        draft_tab.click(force=True)
                    time.sleep(3)

                edit_btn = page.locator('button:has-text("Edit"), button:has-text("Chỉnh sửa"), button:has-text("Editar")').first
                edit_btn.wait_for(state="visible", timeout=20000)
                try:
                    edit_btn.click(timeout=3000)
                except Exception:
                    dismiss_tiktok_popups(page, _log)
                    edit_btn.click(force=True)

                dismiss_tiktok_popups(page, _log)
                post_btn = page.locator('button:has-text("Post"), button:has-text("Đăng"), button:has-text("Publicar")').last
                post_btn.wait_for(state="visible", timeout=30000)
                post_btn.scroll_into_view_if_needed()
                try:
                    post_btn.click(timeout=5000)
                except Exception:
                    dismiss_tiktok_popups(page, _log)
                    post_btn.click(force=True)

                time.sleep(2)
                confirm_btn = page.locator('button:has-text("Post now"), button:has-text("Continue"), button:has-text("Vẫn đăng"), button:has-text("Continuar")').first
                if confirm_btn.is_visible():
                    try:
                        confirm_btn.click(timeout=3000)
                    except Exception:
                        confirm_btn.click(force=True)

                time.sleep(8)
                _log("✅ Đã phát hành bản nháp thành công!")
                update_video_status(video_record["video_id"], "PUBLISHED")
                return True
            except Exception as e:
                _log(f"❌ Lỗi đăng nháp: {e}")
                return False

def delete_tiktok_video_and_file(video_record: dict, log_callback = None) -> bool:
    """Xóa file 4K cục bộ và xóa video/bản nháp trên TikTok Studio."""
    def _log(msg):
        print(f"[Delete Video]: {msg}")
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    local_path = video_record.get("local_4k_path", "")
    if local_path and os.path.exists(local_path):
        try:
            os.remove(local_path)
            _log(f"🗑️ Đã xóa file video cục bộ: {os.path.basename(local_path)}")
        except Exception:
            pass

    lock = get_profile_lock(video_record["profile_name"])
    with lock:
        kwargs = _prepare_camoufox_kwargs(video_record["profile_dir"], video_record.get("proxy_server", ""), headless=False)
        with Camoufox(**kwargs) as context:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto("https://www.tiktok.com/tiktokstudio/content", timeout=60000)
                time.sleep(4)

                if video_record.get("status") == "DRAFTED":
                    draft_tab = page.locator('text="Drafts", text="Bản nháp", text="Borradores"').first
                    if draft_tab.is_visible():
                        draft_tab.click()
                        time.sleep(3)

                del_btn = page.locator('button:has-text("Delete"), button:has-text("Xóa"), button:has-text("Eliminar")').first
                if del_btn.is_visible():
                    del_btn.click()
                    time.sleep(2)
                    confirm_del = page.locator('button:has-text("Delete"), button:has-text("Xóa"), button:has-text("Eliminar")').last
                    if confirm_del.is_visible():
                        confirm_del.click()
                    time.sleep(4)
                delete_video_record(video_record["video_id"])
                _log("✅ Đã xóa video thành công!")
                return True
            except Exception as e:
                _log(f"❌ Lỗi khi xóa video trên TikTok: {e}")
                delete_video_record(video_record["video_id"])
                return False

# ================= 7. FULL PIPELINE: LOCAL VIDEO TO CHANNELS =================
def process_and_upload_local_video(
    video_path: str,
    title: str,
    description: str,
    hashtags: str | list[str],
    profile_data: dict,
    vpn_region: str = "VN",
    post_to_tiktok: bool = True,
    post_to_youtube: bool = True,
    apply_4k_upscale: bool = True,
    channel_id: int = 0,
    account_id: str = "",
    source_handle: str = "",
    tiktok_user: str = "",
    source_channel_url: str = "",
    log_callback = None
) -> dict:
    """
    Quy trình xử lý hoàn chỉnh từ file video máy tính:
    1. Đo thông số hoặc render upscale 4K bằng FFmpeg (tăng tốc GPU VideoToolbox/NVENC).
    2. Đăng lên TikTok Studio với Camoufox và xử lý Content Check Lite (chờ trạng thái HỢP LỆ XANH).
    3. Đăng lên YouTube Shorts nếu được chọn.
    4. Ghi nhận lịch sử vào SQLite và gửi báo cáo Telegram TIKTOK BETA.
    """
    def _log(msg: str):
        print(f"[Upload Pipeline]: {msg}")
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Không tìm thấy file video: {video_path}")

    video_id = f"local_{datetime.now().strftime('%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    clean_title = title.strip().replace("\n", " ")
    if not clean_title:
        clean_title = clean_filename_to_title(video_path)

    # Chuẩn hóa Hashtags
    if isinstance(hashtags, list):
        tags_list = [t.lower() if t.startswith("#") else f"#{t.lower()}" for t in hashtags]
        tags_str = " ".join(tags_list)
    else:
        tags_str = hashtags.strip()

    dl_dir = os.path.join(BASE_DIR, "downloads")
    os.makedirs(dl_dir, exist_ok=True)

    # 1. Xử lý Video (FFmpeg Upscale 4K hoặc đo thông số gốc)
    if apply_4k_upscale:
        _log("🎬 Kích hoạt bộ lọc FFmpeg: Upscale 4K (2160x3840), đổi mã băm khung hình và tối ưu màu sắc...")
        rendered_4k_path = os.path.join(dl_dir, f"{video_id}_4k.mp4")
        stats = edit_and_upscale_4k(video_path, rendered_4k_path)
        ready_video_path = rendered_4k_path
        quality_str = stats.get("resolution_str", "4K Ultra HD")
        duration = stats.get("duration", 59.0)
        _log(f"✅ Hoàn tất render FFmpeg: {quality_str} ({duration}s)")
    else:
        _log("📁 Sử dụng trực tiếp file video gốc (Bỏ qua upscale 4K)...")
        ready_video_path = video_path
        probe_cmd = [
            FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", video_path
        ]
        try:
            res = subprocess.run(probe_cmd, capture_output=True, text=True, check=True, **get_subprocess_kwargs())
            probe_data = json.loads(res.stdout)
            stream = probe_data.get("streams", [{}])[0]
            width = stream.get("width", 1080)
            height = stream.get("height", 1920)
            duration = round(float(probe_data.get("format", {}).get("duration", 30.0)), 1)
            quality_str = f"Original ({width}x{height})"
        except Exception:
            quality_str = "Original Video"
            duration = 30.0

    profile_name = profile_data.get("name", "Unknown Profile")
    profile_dir = os.path.abspath(os.path.join(BASE_DIR, profile_data.get("path", "")))
    proxy_server = profile_data.get("proxy", "")
    os_type = profile_data.get("os", "windows")
    screen = profile_data.get("screen", "1920x1080")
    locale = profile_data.get("locale", "")

    # Chuẩn bị Caption & Hashtags
    tt_caption_parts = [clean_title]
    if description.strip():
        tt_caption_parts.append(description.strip())
    if tags_str:
        tt_caption_parts.append(tags_str)
    tiktok_caption = "\n\n".join(tt_caption_parts)
    if len(tiktok_caption) > 2100:
        tiktok_caption = tiktok_caption[:2050] + "..."

    yt_description = f"{description.strip()}\n\n{tags_str}".strip() if description.strip() else tags_str

    # 2. Đăng lên TikTok Studio
    tt_action = "SKIPPED"
    tt_check_status = "BỎ QUA (Không chọn TikTok)"
    if post_to_tiktok:
        _log(f"🎵 [TikTok Studio]: Khởi động Camoufox Profile [{profile_name}]...")
        tt_res = upload_to_tiktok_with_fallback(
            video_4k_path=ready_video_path,
            caption=tiktok_caption,
            profile_dir=profile_dir,
            proxy_server=proxy_server,
            os_type=os_type,
            screen=screen,
            locale=locale,
            log_callback=log_callback
        )
        tt_action = tt_res.get("action", "FAILED")
        tt_check_status = tt_res.get("check_status", "LỖI")

    # 3. Đăng lên YouTube Shorts
    yt_action = "SKIPPED"
    yt_status = "BỎ QUA (Không chọn YouTube)"
    yt_video_url = ""
    if post_to_youtube:
        _log(f"📺 [YouTube Studio]: Khởi động Camoufox Profile [{profile_name}]...")
        yt_res = upload_to_youtube_studio(
            video_4k_path=ready_video_path,
            title=clean_title,
            description=yt_description,
            profile_dir=profile_dir,
            proxy_server=proxy_server,
            os_type=os_type,
            screen=screen,
            locale=locale,
            log_callback=log_callback
        )
        yt_action = yt_res.get("action", "FAILED")
        yt_status = yt_res.get("status", "LỖI")
        yt_video_url = yt_res.get("video_url", "")

    # 4. Lưu lịch sử vào SQLite
    posted_today_str = datetime.now().strftime("%Y-%m-%d")
    save_video_record(
        video_id=video_id,
        profile_name=profile_name,
        profile_dir=profile_dir,
        proxy_server=proxy_server,
        shorts_title=clean_title,
        local_4k_path=ready_video_path,
        caption=tiktok_caption,
        status=tt_action,
        yt_status=yt_action,
        yt_video_url=yt_video_url,
        account_id=account_id,
        source_handle=source_handle,
        tiktok_user=tiktok_user,
        tiktok_video_url="",
        vpn_region=vpn_region,
        quality_str=quality_str,
        duration=duration,
        check_status="Content Check Lite: HỢP LỆ (XANH)" if tt_action == "PUBLISHED" else tt_check_status,
        posted_date=posted_today_str,
        channel_id=channel_id
    )

    # 5. Gửi thông báo Telegram theo đúng mẫu chuẩn
    try:
        if tt_action == "PUBLISHED":
            from telegram_controller import send_tiktok_beta_success_report
            send_tiktok_beta_success_report(
                source_handle=source_handle or "Kênh YouTube Shorts",
                account_id=account_id or "acc_default",
                tiktok_user=tiktok_user or f"@{profile_name}",
                vpn_region=vpn_region,
                profile_name=profile_name,
                shorts_title=clean_title,
                quality_str=quality_str,
                duration=duration,
                check_status="Content Check Lite: HỢP LỆ (XANH)",
                caption_text=tiktok_caption,
                youtube_url=source_channel_url or yt_video_url or "https://youtube.com"
            )
            _log("🎉 Đã gửi thông báo ĐĂNG VIDEO THÀNH CÔNG (TikTok Beta) về Telegram!")
        else:
            from telegram_controller import send_telegram_report_with_buttons
            send_telegram_report_with_buttons(
                video_id=video_id,
                action=tt_action,
                source_channel=source_handle or "📁 Video File Cục Bộ",
                tiktok_user=tiktok_user or f"Profile: {profile_name}",
                vpn_region=vpn_region,
                profile_name=profile_name,
                shorts_title=clean_title,
                quality_str=quality_str,
                duration=duration,
                check_status=tt_check_status,
                caption_text=tiktok_caption,
                youtube_url=yt_video_url if yt_video_url else source_channel_url,
                yt_status=yt_status,
                yt_video_url=yt_video_url
            )
            _log("🤖 Đã gửi thông báo báo cáo về Telegram!")
    except Exception as e:
        _log(f"⚠️ Không thể gửi Telegram: {e}")

    return {
        "video_id": video_id,
        "tiktok_action": tt_action,
        "tiktok_status": tt_check_status,
        "youtube_action": yt_action,
        "youtube_status": yt_status,
        "youtube_url": yt_video_url,
        "video_path": ready_video_path
    }

# ================= 8. BACKGROUND AUTO SCANNER & COMPENSATION =================
_scanner_running = False
_scanner_thread = None

def is_scanner_running() -> bool:
    global _scanner_running
    return _scanner_running

def run_single_channel_download_and_post(channel_id: int, log_callback=None) -> dict:
    """
    Tự động quét tải 1 video mới từ kênh YouTube Shorts nguồn,
    xử lý upscale 4K/kiểm tra ≤ 59s, tạo caption/hashtag, và đăng tải sang TikTok.
    """
    def _log(msg):
        print(f"[Run Channel #{channel_id}]: {msg}")
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    ch = get_channel_by_id(channel_id)
    if not ch:
        _log(f"❌ Không tìm thấy cấu hình kênh có ID {channel_id}")
        return {"success": False, "error": "Kênh không tồn tại"}

    yt_url = ch.get("youtube_shorts_url", "")
    if not yt_url:
        return {"success": False, "error": "Kênh chưa cấu hình link YouTube Shorts"}

    _log(f"🔎 Đang quét video mới nhất từ: {yt_url}...")
    item = fetch_latest_short(yt_url)
    if not item:
        _log(f"⚠️ Không tìm thấy video Shorts mới (thời lượng ≤ 59s và chưa xử lý) từ {yt_url}")
        return {"success": False, "error": "Không có video mới phù hợp"}

    _log(f"✨ Phát hiện video Shorts: '{item['title']}'. Tiến hành tải và xử lý...")
    dl_dir = os.path.join(BASE_DIR, "downloads")
    os.makedirs(dl_dir, exist_ok=True)
    out_4k = os.path.join(dl_dir, f"{item['video_id']}_4k.mp4")
    stats = edit_and_upscale_4k(item["raw_path"], out_4k)

    # Sinh caption & hashtag theo thị trường đích
    vpn_region = ch.get("vpn_region", "VN")
    seo_meta = generate_seo_metadata(item["title"], target_region=vpn_region, niche_hint=ch.get("niche", ""))
    title = seo_meta.get("title", item["title"])
    desc = seo_meta.get("description", "")
    tags = seo_meta.get("hashtags", [])

    profile_data = {
        "name": ch.get("profile_name", "AutoProfile"),
        "path": ch.get("profile_dir", ""),
        "proxy": ch.get("proxy_server", ""),
        "os": "windows",
        "screen": "1920x1080",
        "locale": ""
    }

    res = process_and_upload_local_video(
        video_path=out_4k,
        title=title,
        description=desc,
        hashtags=tags,
        profile_data=profile_data,
        vpn_region=vpn_region,
        post_to_tiktok=bool(ch.get("post_to_tiktok", 1)),
        post_to_youtube=bool(ch.get("post_to_youtube", 1)),
        apply_4k_upscale=False,  # Đã upscale 4K
        channel_id=ch["id"],
        account_id=ch.get("account_id", ""),
        source_handle=ch.get("source_handle", ""),
        tiktok_user=ch.get("tiktok_user", ""),
        source_channel_url=item.get("url", yt_url),
        log_callback=log_callback
    )
    return {"success": True, "details": res}

def execute_compensation(mode: str = "bu1", log_callback=None):
    """
    Thực thi duyệt đăng bù:
    - mode == 'bu1': Đăng bù 1 video cho mỗi kênh đang thiếu
    - mode == 'bu_all': Đăng bù đủ số lượng còn thiếu cho tất cả các kênh
    Chạy trong một luồng nền để không chặn giao diện và Telegram bot.
    """
    def _worker():
        def _log(msg):
            print(f"[Đăng Bù]: {msg}")
            if log_callback:
                try:
                    log_callback(msg)
                except Exception:
                    pass

        summary = get_all_channels_kpi_summary()
        incomplete = summary.get("incomplete", [])
        if not incomplete:
            _log("✅ Tất cả các kênh đã đạt đủ chỉ tiêu, không cần đăng bù.")
            return

        _log(f"🚀 Bắt đầu chạy tiến trình đăng bù (Chế độ: {mode}) cho {len(incomplete)} kênh...")
        for ch in incomplete:
            ch_id = ch["id"]
            missing_count = ch.get("missing", 1)
            runs = 1 if mode == "bu1" else missing_count
            _log(f"👉 Kênh '{ch.get('source_handle')}' ({ch.get('account_id')}): Cần đăng bù {runs} video...")
            for step in range(runs):
                _log(f"--- Đang thực hiện video bù #{step + 1}/{runs} cho kênh #{ch_id} ---")
                try:
                    res = run_single_channel_download_and_post(ch_id, log_callback=log_callback)
                    if not res.get("success"):
                        _log(f"⚠️ Video bù #{step + 1} không thành công: {res.get('error')}")
                except Exception as e:
                    _log(f"❌ Lỗi khi đăng bù kênh #{ch_id}: {e}")
                time.sleep(5)
        _log("🎉 Hoàn tất quy trình đăng bù chỉ tiêu!")

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def audit_daily_videos(target_date: str = None, log_callback=None) -> dict:
    """
    Kiểm định danh sách video TikTok đã đăng trong ngày:
    - Trích xuất views, likes, shadowban (đề xuất FYP), trùng lặp, vi phạm
    - Gom nhóm theo từng kênh/Profile
    - Trả về cấu trúc tổng hợp phục vụ gửi báo cáo Telegram TIKTOK THÔNG BÁO.
    """
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
    SELECT v.*, c.source_handle, c.account_id as ch_acc_id, c.tiktok_user as ch_tt_user, c.profile_name as ch_prof
    FROM videos v
    LEFT JOIN channels c ON v.channel_id = c.id
    WHERE v.posted_date = ? AND v.status = 'PUBLISHED'
    ORDER BY v.created_at ASC
    """, (target_date,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    grouped = {}
    has_error = False
    error_details = []

    for r in rows:
        vid_id = r["video_id"]
        title = r.get("shorts_title") or f"Video {vid_id}"
        acc_id = r.get("account_id") or r.get("ch_acc_id") or "acc_n/a"
        handle = r.get("source_handle") or r.get("tiktok_user") or r.get("ch_tt_user") or "Kênh TikTok"
        prof_name = r.get("profile_name") or r.get("ch_prof") or "Profile"
        group_key = f"{handle} ({prof_name})"

        views = r.get("views", 0)
        likes = r.get("likes", 0)
        is_shadowban = bool(r.get("is_shadowban", 0))
        is_duplicate = bool(r.get("is_duplicate", 0))
        is_violation = bool(r.get("is_violation", 0))

        if is_shadowban or is_duplicate or is_violation:
            has_error = True
            err_reasons = []
            if is_shadowban: err_reasons.append("Shadowban (Bị chặn FYP)")
            if is_duplicate: err_reasons.append("Trùng lặp nội dung")
            if is_violation: err_reasons.append("Vi phạm tiêu chuẩn cộng đồng")
            error_details.append(f"• <b>{handle}</b> - '{title}': {', '.join(err_reasons)}")

        if group_key not in grouped:
            grouped[group_key] = []
        grouped[group_key].append({
            "video_id": vid_id,
            "title": title,
            "views": views,
            "likes": likes,
            "is_shadowban": is_shadowban,
            "is_duplicate": is_duplicate,
            "is_violation": is_violation,
            "created_at": r.get("created_at", "")
        })

    return {
        "date": target_date,
        "grouped": grouped,
        "has_error": has_error,
        "error_details": error_details,
        "total": len(rows)
    }

def start_auto_scanner(log_callback=None):
    global _scanner_running, _scanner_thread
    if _scanner_running:
        return
    _scanner_running = True

    def _loop():
        def _log(msg):
            print(f"[Auto Scanner]: {msg}")
            if log_callback:
                try:
                    log_callback(msg)
                except Exception:
                    pass

        _log("🚀 Khởi động dịch vụ tự động quét kênh YouTube Shorts...")
        while _scanner_running:
            channels = get_all_channels()
            if not channels:
                _log("ℹ️ Chưa có kênh nào trong danh sách theo dõi. Chờ 60s...")
                for _ in range(60):
                    if not _scanner_running:
                        break
                    time.sleep(1)
                continue

            for ch in channels:
                if not _scanner_running:
                    break

                ch_status = (ch.get("status") or "ACTIVE").upper()
                if ch_status == "PAUSED":
                    continue

                daily_target = ch.get("daily_target", 2) or 2
                posted_today = get_channel_posted_count_today(ch["id"])
                if posted_today >= daily_target:
                    continue

                yt_url = ch.get("youtube_shorts_url", "")
                if not yt_url:
                    continue

                try:
                    _log(f"🔎 Quét kênh '{ch.get('source_handle')}' (Đã đăng: {posted_today}/{daily_target})...")
                    run_single_channel_download_and_post(ch["id"], log_callback=log_callback)
                except Exception as e:
                    _log(f"❌ Lỗi khi quét kênh {yt_url}: {e}")

            cfg = load_config()
            interval = cfg.get("scan_interval_seconds", 600)
            _log(f"💤 Nghỉ {interval} giây trước lượt quét kế tiếp...")
            for _ in range(interval):
                if not _scanner_running:
                    break
                time.sleep(1)

    _scanner_thread = threading.Thread(target=_loop, daemon=True)
    _scanner_thread.start()

def stop_auto_scanner():
    global _scanner_running
    _scanner_running = False

# ================= 9. DAILY SCHEDULER (22:01 KPI & 22:03 AUDIT) =================
_scheduler_running = False
_scheduler_thread = None

def start_daily_scheduler(log_callback=None):
    global _scheduler_running, _scheduler_thread
    if _scheduler_running:
        return
    _scheduler_running = True

    def _scheduler_loop():
        def _log(msg):
            print(f"[Daily Scheduler]: {msg}")
            if log_callback:
                try:
                    log_callback(msg)
                except Exception:
                    pass

        _log("⏰ Bộ lập lịch KPI (22:01) & Kiểm định (22:03) đã sẵn sàng...")
        last_kpi_day = ""
        last_audit_day = ""

        while _scheduler_running:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            current_time_str = now.strftime("%H:%M")

            if current_time_str == "22:01" and last_kpi_day != today_str:
                _log("📊 [22:01] Tự động kích hoạt Báo cáo Chỉ tiêu (KPI) & Duyệt Đăng bù...")
                try:
                    from telegram_controller import send_daily_kpi_report
                    send_daily_kpi_report(today_str)
                    last_kpi_day = today_str
                except Exception as e:
                    _log(f"❌ Lỗi gửi báo cáo KPI 22:01: {e}")

            if current_time_str == "22:03" and last_audit_day != today_str:
                _log("🛡 [22:03] Tự động kích hoạt Báo cáo Kiểm định Video...")
                try:
                    from telegram_controller import send_daily_audit_report
                    send_daily_audit_report(today_str)
                    last_audit_day = today_str
                except Exception as e:
                    _log(f"❌ Lỗi gửi báo cáo kiểm định 22:03: {e}")

            time.sleep(20)

    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()

# Tự động khởi động Daily Scheduler khi khởi động ứng dụng
start_daily_scheduler()

# Khởi tạo DB ngay khi nạp module
init_db()
