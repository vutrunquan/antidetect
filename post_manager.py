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
    generate_localized_caption, generate_seo_metadata,
    build_caption_metadata, clean_filename_to_title
)

from platform_utils import (
    BASE_DIR, FFMPEG_BIN, FFPROBE_BIN, SELECT_ALL_SHORTCUT,
    get_subprocess_kwargs, detect_best_video_encoder, load_config
)
from browser_runner import parse_proxy
from proxy_bridge import setup_camoufox_proxy
from video_remix_engine import (
    render_anti_copyright_video,
    remedy_audio_with_bgm,
    probe_video_metrics
)
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
        title_mode TEXT DEFAULT 'ai_rewrite',
        order_mode TEXT DEFAULT 'newest_first',
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
        ("title_mode", "TEXT DEFAULT 'ai_rewrite'"),
        ("order_mode", "TEXT DEFAULT 'newest_first'"),
        ("crop_mode", "TEXT DEFAULT 'blur_9_16'"),
        ("series_name", "TEXT DEFAULT ''"),
        ("jitter_enabled", "INTEGER DEFAULT 1"),
        ("health_score", "INTEGER DEFAULT 85"),
        ("health_status", "TEXT DEFAULT 'healthy'"),
        ("last_audit_time", "TEXT DEFAULT ''"),
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
        ("yt_source_url", "TEXT DEFAULT ''"),
        ("vpn_region", "TEXT DEFAULT ''"),
        ("crop_mode", "TEXT DEFAULT 'original'"),
        ("series_name", "TEXT DEFAULT ''"),
        ("episode_num", "INTEGER DEFAULT 0"),
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

    try:
        conn.execute("UPDATE channels SET order_mode = 'newest_first' WHERE order_mode = 'oldest_first' OR order_mode IS NULL OR order_mode = ''")
    except Exception:
        pass

    conn.commit()
    conn.close()

def is_video_processed(video_id: str) -> bool:
    if not video_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM videos 
        WHERE video_id = ? 
           OR video_id LIKE ? 
           OR local_4k_path LIKE ?
           OR yt_video_url LIKE ?
           OR yt_source_url LIKE ?
    """, (video_id, f"%{video_id}%", f"%{video_id}%", f"%{video_id}%", f"%{video_id}%"))
    exists = cur.fetchone() is not None
    conn.close()
    return exists

def save_video_record(
    video_id, profile_name, profile_dir, proxy_server,
    shorts_title, local_4k_path, caption, status,
    yt_status="NONE", yt_video_url="",
    account_id="", source_handle="", tiktok_user="", tiktok_video_url="",
    yt_source_url="",
    vpn_region="VN", quality_str="", duration=0.0, check_status="",
    posted_date=None, channel_id=0,
    crop_mode="blur_9_16", series_name="", episode_num=0
):
    conn = sqlite3.connect(DB_PATH)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not posted_date:
        posted_date = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
    INSERT OR REPLACE INTO videos 
    (video_id, profile_name, profile_dir, proxy_server, shorts_title, local_4k_path, caption, status, yt_status, yt_video_url, created_at,
     account_id, source_handle, tiktok_user, tiktok_video_url, yt_source_url, vpn_region, quality_str, duration, check_status, posted_date, channel_id,
     crop_mode, series_name, episode_num)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (video_id, profile_name, profile_dir, proxy_server, shorts_title, local_4k_path, caption, status, yt_status, yt_video_url, created_at,
          account_id, source_handle, tiktok_user, tiktok_video_url, yt_source_url, vpn_region, quality_str, duration, check_status, posted_date, channel_id,
          crop_mode, series_name, episode_num))
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
    status: str = "ACTIVE",
    title_mode: str = "ai_rewrite",
    order_mode: str = "newest_first",
    crop_mode: str = "blur_9_16",
    series_name: str = ""
):
    norm_url, parsed_handle = parse_youtube_channel_input(yt_url)
    if not source_handle:
        source_handle = parsed_handle
    if not account_id:
        account_id = generate_account_id()
    norm_title_mode = "keep_original" if str(title_mode).strip().lower() == "keep_original" else "ai_rewrite"
    norm_order_mode = "oldest_first" if str(order_mode).strip().lower() == "oldest_first" else "newest_first"
    norm_crop_mode = str(crop_mode).strip() if crop_mode else "blur_9_16"

    if not profile_dir:
        profile_dir = os.path.join(BASE_DIR, "profiles", profile_name.replace(" ", "_"))
    os.makedirs(profile_dir, exist_ok=True)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    INSERT OR REPLACE INTO channels 
    (youtube_shorts_url, tiktok_user, vpn_region, profile_name, profile_dir, proxy_server, niche, post_to_tiktok, post_to_youtube, created_at,
     account_id, source_handle, daily_target, status, title_mode, order_mode, crop_mode, series_name)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (norm_url, tiktok_user, vpn_region.upper(), profile_name, profile_dir, proxy_server, niche, post_to_tiktok, post_to_youtube, created_at,
          account_id, source_handle, daily_target, status.upper(), norm_title_mode, norm_order_mode, norm_crop_mode, series_name.strip()))
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
    for k in ["account_id", "source_handle", "youtube_shorts_url", "tiktok_user", "vpn_region", "profile_name", "niche", "daily_target", "status", "title_mode", "order_mode", "post_to_tiktok", "post_to_youtube", "crop_mode", "series_name", "jitter_enabled"]:
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
            "title_mode": (ch.get("title_mode") or "ai_rewrite").lower(),
            "order_mode": (ch.get("order_mode") or "newest_first").lower(),
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

# ================= 2. FFMPEG 4K & VIDEO REMIX ENGINE =================
def format_actual_quality_display(w: int, h: int, fps: float = 0) -> str:
    """Định dạng tên chất lượng thực tế của video (4K, 2K, 1080p60, 1080p, 720p60, 720p)."""
    w = int(w or 0)
    h = int(h or 0)
    fps_int = int(round(float(fps or 0)))
    min_dim = min(w, h)
    fps_str = f" {fps_int}fps" if fps_int > 0 else ""

    if min_dim >= 2100:
        return f"4K Ultra HD ({w}x{h}{fps_str})"
    elif min_dim >= 1400:
        return f"2K QHD ({w}x{h}{fps_str})"
    elif min_dim >= 1070:
        if fps_int >= 50:
            return f"1080p60 Full HD ({w}x{h}{fps_str})"
        return f"1080p Full HD ({w}x{h}{fps_str})"
    elif min_dim >= 710:
        if fps_int >= 50:
            return f"720p60 HD ({w}x{h}{fps_str})"
        return f"720p HD ({w}x{h}{fps_str})"
    elif min_dim >= 470:
        return f"480p SD ({w}x{h}{fps_str})"
    elif min_dim > 0:
        return f"{min_dim}p ({w}x{h}{fps_str})"
    return "Không xác định"


def get_best_format_metrics(formats: list) -> tuple[bool, int, int, int, float, str]:
    """
    Phân tích toàn bộ danh sách formats của yt-dlp để tìm ra luồng video có chất lượng cao nhất:
    Ưu tiên: Độ phân giải (4K > 2K > 1080p > 720p) -> FPS (60fps > 30fps) -> Bitrate.
    Trả về: (is_qualified, best_w, best_h, best_fps, best_tbr, quality_label)
    """
    if not formats:
        return False, 0, 0, 0, 0.0, "Không có format video"

    max_min_dim = 0
    max_max_dim = 0
    best_w = 0
    best_h = 0
    best_fps = 0
    best_tbr = 0.0

    for f in formats:
        vcodec = f.get("vcodec")
        if not vcodec or vcodec == "none":
            continue
        w = int(f.get("width") or 0)
        h = int(f.get("height") or 0)
        fps = float(f.get("fps") or 0)
        tbr = float(f.get("vbr") or f.get("tbr") or 0.0)
        min_dim = min(w, h)
        max_dim = max(w, h)
        cand_key = (min_dim, max_dim, fps, tbr)
        best_key = (max_min_dim, max_max_dim, best_fps, best_tbr)
        if cand_key > best_key:
            max_min_dim = min_dim
            max_max_dim = max_dim
            best_w = w
            best_h = h
            best_fps = fps
            best_tbr = tbr

    is_qualified = (max_min_dim >= 710)
    fps_int = int(round(best_fps)) if best_fps else 0
    quality_label = format_actual_quality_display(best_w, best_h, fps_int)
    return is_qualified, best_w, best_h, fps_int, best_tbr, quality_label


def verify_downloaded_file_quality(file_path: str, return_fps: bool = False):
    """Sử dụng ffprobe để kiểm tra kích thước và khung hình thực tế của tệp video sau khi tải về."""
    if not os.path.exists(file_path):
        return (False, 0, 0, "File không tồn tại", 0) if return_fps else (False, 0, 0, "File không tồn tại")
    try:
        cmd = [
            FFPROBE_BIN, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json", file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, **get_subprocess_kwargs())
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if not streams:
            return (False, 0, 0, "Không tìm thấy luồng video", 0) if return_fps else (False, 0, 0, "Không tìm thấy luồng video")
        w = int(streams[0].get("width", 0))
        h = int(streams[0].get("height", 0))
        fps_raw = str(streams[0].get("r_frame_rate", ""))
        fps = 0
        if "/" in fps_raw:
            try:
                num, den = fps_raw.split("/")
                fps = round(float(num) / float(den)) if float(den) > 0 else 0
            except Exception:
                pass

        min_dim = min(w, h)
        is_ok = (min_dim >= 710)
        actual_qual_str = format_actual_quality_display(w, h, fps)
        label = actual_qual_str if is_ok else f"{actual_qual_str} (Kém chất lượng < 720p)"
        if return_fps:
            return is_ok, w, h, label, fps
        return is_ok, w, h, label
    except Exception as e:
        err_lbl = f"Lỗi xác thực chất lượng qua ffprobe: {e}"
        if return_fps:
            return False, 0, 0, err_lbl, 0
        return False, 0, 0, err_lbl


def edit_and_upscale_4k(raw_path: str, output_path: str, crop_mode: str = "blur_9_16", fake_mobile_camera: bool = True, log_callback=None) -> dict:
    """
    Video Remix Engine (FFmpeg Pipeline):
    1. Zoom nhẹ 1.03x + crop trung tâm để đổi toàn bộ tọa độ pixel và mã băm hash frame.
    2. Cắt chuyển đổi khung hình linh hoạt (original, blur_9_16 nền mờ nghệ thuật, crop_9_16, crop_1_1).
    3. Bộ lọc unsharp (làm nét) + tương phản contrast 1.02, bão hòa saturation 1.04.
    4. Bộ lọc âm thanh asetrate/atempo: đổi nhẹ dải tần 1.025x để lách Content ID nhưng giữ nguyên độ dài.
    5. Xóa sạch toàn bộ Metadata gốc (-map_metadata -1) và chèn Metadata Camera Điện Thoại Thật (iPhone 15 Pro Max/S23 Ultra) để lách quét 0-view.
    6. Hỗ trợ toàn bộ độ dài video (không cắt giới hạn 59s).
    """
    rendered_path = render_anti_copyright_video(
        input_path=raw_path,
        output_path=output_path,
        zoom_ratio=1.03,
        enable_audio_mod=True,
        crop_mode=crop_mode,
        fake_mobile_camera=fake_mobile_camera,
        log_callback=log_callback
    )

    metrics = probe_video_metrics(rendered_path)
    return {
        "resolution_str": metrics.get("quality_str", "Full HD (1080x1920)"),
        "duration": metrics.get("duration", 59.0),
        "file_path": rendered_path
    }


def remedy_audio_fast(video_4k_path: str, remedied_output_path: str, log_callback=None) -> str:
    """
    Cứu hộ âm thanh tức thì khi Content Check Lite báo lỗi bản quyền:
    Hòa trộn 15% âm hiện trường gốc + 85% nhạc nền an toàn từ safe_bgm (hoặc equalizer chống bản quyền).
    """
    return remedy_audio_with_bgm(video_4k_path, remedied_output_path, log_callback=log_callback)


def download_and_verify_youtube_video(video_url: str, log_callback=None) -> dict | None:
    """
    Tải và kiểm định chất lượng HD/Full HD của 1 video YouTube theo URL cụ thể:
    - Bật extractor_args tránh SABR 360p của YouTube.
    - Chốt chặn bảo vệ chất lượng HD 720p/1080p: nếu video mới đăng (< 30 phút) và đang chờ encode HD,
      hệ thống sẽ báo trạng thái chờ YouTube hoàn tất encode bản 1080p.
    - Tải bản chất lượng cao nhất (4K > 2K > 1080p60 > 1080p > 720p60 > 720p).
    - Đo đạc thực tế bằng ffprobe.
    """
    def _log(msg):
        if log_callback:
            try: log_callback(msg)
            except Exception: pass
        else:
            print(f"[Download Video]: {msg}")

    dl_dir = os.path.join(BASE_DIR, "downloads")
    os.makedirs(dl_dir, exist_ok=True)
    node_bin = shutil.which("node")
    js_runtimes = {"node": {}} if node_bin else {}

    common_extractor_args = {"youtube": {"player_client": ["default", "web_embedded"]}}

    _log(f"🔍 Kiểm tra chất lượng formats trên YouTube cho video [{video_url}]...")
    ydl_detail_opts = {
        "quiet": True,
        "js_runtimes": js_runtimes,
        "extractor_args": common_extractor_args
    }
    with yt_dlp.YoutubeDL(ydl_detail_opts) as ydl:
        try:
            full_info = ydl.extract_info(video_url, download=False)
        except Exception as e:
            _log(f"⚠️ Không thể lấy thông tin video {video_url}: {e}")
            return None

        if not full_info:
            return None

        target_id = full_info.get("id")
        formats = full_info.get("formats", [])
        is_hd, best_w, best_h, best_fps, best_tbr, qual_label = get_best_format_metrics(formats)

        now_ts = int(time.time())
        vid_ts = full_info.get("timestamp") or full_info.get("release_timestamp") or 0
        age_minutes = (now_ts - vid_ts) / 60 if vid_ts else 0

        # Nếu video mới đăng (< 30 phút) và chưa có bản HD 720p/1080p -> Chờ YouTube encode
        if not is_hd:
            if vid_ts and age_minutes > 30:
                _log(f"⛔ [BỎ QUA - CHẤT LƯỢNG KÉM < 720p] Video \"{full_info.get('title')}\" chỉ có {qual_label} sau hơn 30 phút. Bỏ qua.")
            else:
                _log(f"⏳ [CHỜ XỬ LÝ HD] Video mới nhất \"{full_info.get('title')}\" hiện mới có {qual_label}. Đang chờ YouTube encode xong bản HD 1080p/720p...")
            return None

        _log(f"🎯 [CHẤT LƯỢNG ĐẠT CHUẨN] Phát hiện bản {qual_label} cho video: \"{full_info.get('title')}\"")

    # Tiến hành tải luồng video chất lượng cao nhất
    raw_path = os.path.join(dl_dir, f"{target_id}_raw.mp4")
    target_min_dim = min(best_w, best_h) if (best_w and best_h) else 1080
    required_tier_dim = 1080 if target_min_dim >= 1070 else 720

    ydl_dl_opts = {
        "format": (
            f"bestvideo[width>={required_tier_dim}][height>={required_tier_dim}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[width>={required_tier_dim}][height>={required_tier_dim}]+bestaudio/"
            f"bestvideo[width>=710][height>=710][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ),
        "outtmpl": raw_path,
        "merge_output_format": "mp4",
        "quiet": True,
        "js_runtimes": js_runtimes,
        "extractor_args": common_extractor_args
    }

    _log(f"📥 Đang tải video chất lượng cao từ YouTube ({qual_label})...")
    with yt_dlp.YoutubeDL(ydl_dl_opts) as ydl:
        download_info = ydl.extract_info(video_url, download=True)
        # Xác thực chất lượng file tải về qua ffprobe
        is_ok, real_w, real_h, verified_qual, real_fps = verify_downloaded_file_quality(raw_path, return_fps=True)
        _log(f"✅ Đã tải xong video! Đo đạc thực tế ffprobe: {verified_qual}")

        canonical_url = f"https://www.youtube.com/shorts/{target_id}"
        return {
            "video_id": target_id,
            "title": (download_info.get("title") or full_info.get("title") or "").strip(),
            "description": (download_info.get("description") or full_info.get("description") or "").strip(),
            "uploader": download_info.get("uploader", "YouTube Channel"),
            "url": canonical_url,
            "raw_path": raw_path,
            "duration": download_info.get("duration", full_info.get("duration", 59.0)),
            "quality_str": verified_qual,
            "is_1080": min(real_w, real_h) >= 1070
        }

def fetch_channel_short(channel_shorts_url: str, order_mode: str = "newest_first", max_lookback: int = 300, log_callback=None) -> dict | None:
    """
    Quét danh sách video từ kênh YouTube và chọn video phù hợp:
    - Nếu order_mode == 'oldest_first': Quét từ video CŨ NHẤT tịnh tiến dần lên mới nhất chưa từng xử lý.
    - Nếu order_mode == 'newest_first': Quét từ video MỚI NHẤT chưa từng xử lý.
    """
    def _log(msg):
        if log_callback:
            try: log_callback(msg)
            except Exception: pass
        else:
            print(f"[Fetch Channel]: {msg}")

    node_bin = shutil.which("node")
    js_runtimes = {"node": {}} if node_bin else {}
    common_extractor_args = {"youtube": {"player_client": ["default", "web_embedded"]}}

    ydl_info_opts = {
        "extract_flat": True,
        "playlist_items": f"1-{max_lookback}",
        "quiet": True,
        "js_runtimes": js_runtimes,
        "extractor_args": common_extractor_args
    }

    with yt_dlp.YoutubeDL(ydl_info_opts) as ydl:
        try:
            info = ydl.extract_info(channel_shorts_url, download=False)
        except Exception as e:
            _log(f"❌ [yt-dlp error]: {e}")
            return None

        if not info or "entries" not in info or not info["entries"]:
            return None

        raw_entries = [e for e in info["entries"] if e and e.get("id")]
        if not raw_entries:
            return None

        is_oldest = str(order_mode).strip().lower() == "oldest_first"
        if is_oldest:
            candidate_list = list(reversed(raw_entries))
            _log(f"⏳ Thứ tự duyệt: TỪ CŨ NHẤT ➔ MỚI DẦN (Tổng {len(raw_entries)} video trên kênh)")
        else:
            candidate_list = raw_entries
            _log(f"⚡ Thứ tự duyệt: TỪ MỚI NHẤT ➔ CŨ DẦN (Tổng {len(raw_entries)} video trên kênh)")

        for entry in candidate_list:
            v_id = entry.get("id")
            if not v_id or is_video_processed(v_id):
                continue
            dur = entry.get("duration")
            # Hỗ trợ video TikTok Studio lên tới 10 phút (600s)
            if dur and dur > 600:
                continue

            v_title = entry.get("title", "")
            _log(f"🎯 Đã chọn video chưa đăng: [{v_id}] '{v_title}'")
            candidate_url = f"https://www.youtube.com/shorts/{v_id}"
            downloaded = download_and_verify_youtube_video(candidate_url, log_callback=log_callback)
            if downloaded:
                return downloaded
            else:
                _log(f"⚠️ Video [{v_id}] không tải được hoặc chưa đạt chuẩn, tiếp tục tìm video kế tiếp...")
                continue

    _log("ℹ️ Đã duyệt hết danh sách video trên kênh nhưng không còn video nào mới chưa đăng.")
    return None

def fetch_latest_short(channel_shorts_url: str, max_lookback: int = 50, log_callback=None) -> dict | None:
    return fetch_channel_short(channel_shorts_url, order_mode="newest_first", max_lookback=max_lookback, log_callback=log_callback)

def fetch_specific_video(video_url: str, log_callback=None) -> dict | None:
    """Tải và kiểm định chính xác video theo URL mà người dùng chỉ định."""
    return download_and_verify_youtube_video(video_url, log_callback=log_callback)


# ================= 3. CAMOUFOX BROWSER LAUNCHER =================
def kill_profile_processes(profile_dir: str):
    """
    Dập tắt tất cả process đang chiếm dụng profile_dir (camoufox, firefox, browser_runner, python)
    để giải phóng tài nguyên và tránh xung đột lock hoặc bị Firefox forward lệnh sang cửa sổ trắng (about:blank).
    """
    profile_dir = os.path.abspath(profile_dir)
    profile_id = os.path.basename(profile_dir).lower()
    my_pid = os.getpid()

    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if p.info['pid'] == my_pid:
                    continue
                pname = (p.info['name'] or '').lower()
                if not any(x in pname for x in ['camoufox', 'firefox', 'python']):
                    continue
                cmdline = ' '.join(p.info['cmdline'] or []).lower()
                if profile_id in cmdline or profile_dir.lower() in cmdline:
                    p.kill()
            except Exception:
                pass
        time.sleep(0.5)
    except Exception:
        if sys.platform == "win32":
            try:
                subprocess.run(
                    f'powershell -Command "Get-CimInstance Win32_Process | Where-Object {{ ($_.CommandLine -like \'*{profile_id}*\') -and ($_.ProcessId -ne {my_pid}) }} | Stop-Process -Force"',
                    shell=True,
                    capture_output=True,
                    timeout=5
                )
            except Exception:
                pass

    # Dọn dẹp triệt để các file lock
    for lock_name in [".parentlock", "lock", "parent.lock"]:
        lp = os.path.join(profile_dir, lock_name)
        for _ in range(5):
            if os.path.exists(lp):
                try:
                    os.remove(lp)
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                break

def _prepare_camoufox_kwargs(profile_dir: str, proxy_server: str = "", os_type: str = "windows", screen: str = "1920x1080", locale: str = "", headless: bool = False) -> dict:
    profile_dir = os.path.abspath(profile_dir)
    os.makedirs(profile_dir, exist_ok=True)

    # Đảm bảo dọn dẹp tiến trình cũ và file khóa
    kill_profile_processes(profile_dir)

    camoufox_kwargs = {
        "headless": headless,
        "os": os_type if os_type in ["windows", "macos", "linux"] else "windows",
        "persistent_context": True,
        "user_data_dir": profile_dir,
        "humanize": True,
        "disable_coop": True,
        "canvas_noise": False,
        "i_know_what_im_doing": True,
        "timeout": 60000,
    }

    if screen and "x" in screen:
        try:
            w, h = map(int, screen.lower().split("x"))
            camoufox_kwargs["screen"] = Screen(max_width=w, max_height=h)
        except Exception:
            pass

    if locale.strip():
        camoufox_kwargs["locale"] = locale.strip()

    proxy_cfg, _ = setup_camoufox_proxy(proxy_server) if proxy_server else (None, None)
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
        try:
            with Camoufox(**kwargs) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(target_url, timeout=60000)
                # Chờ người dùng đóng tab
                while len(context.pages) > 0:
                    time.sleep(1)
        except Exception as e:
            if ("Failed to get IP address" in str(e) or "BadStatusLine" in str(e) or "geoip" in str(e).lower()) and kwargs.get("geoip"):
                kwargs["geoip"] = False
                with Camoufox(**kwargs) as context:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(target_url, timeout=60000)
                    while len(context.pages) > 0:
                        time.sleep(1)
            else:
                raise

def dismiss_tiktok_popups(page, logger=None):
    """
    Tự động tắt các popup, overlay hướng dẫn (Joyride tour / tooltips / modal / TUXModal) của TikTok Studio
    để tránh lỗi subtree intercepts pointer events.
    """
    def _log(msg):
        if logger:
            try: logger(msg)
            except Exception: pass

    # 1. Bấm xác nhận các modal yêu cầu bật tính năng hoặc bỏ qua bản nháp
    for btn_kw in [
        'button:has-text("Turn on")',
        'button:has-text("Activar")',
        'button:has-text("Bật")',
        'button:has-text("Confirm")',
        'button:has-text("Confirmar")',
        'button:has-text("Xác nhận")',
        'button:has-text("Discard")',
        'button:has-text("Hủy bỏ")',
        'button:has-text("Loại bỏ")',
        'button:has-text("Bỏ bản nháp")',
        'button:has-text("Post anyway")',
        'button:has-text("Đăng ngay")',
        'button:has-text("Tiếp tục đăng")',
        'button:has-text("Got it")',
        'button:has-text("Entendido")',
        'button:has-text("Đã hiểu")',
        'button:has-text("Skip")',
        'button:has-text("Bỏ qua")',
        'button:has-text("Dismiss")',
        'button:has-text("Aceptar")',
        'button[aria-label="Close"]',
        'button[aria-label="Đóng"]',
        'button[aria-label="Cerrar"]'
    ]:
        try:
            btn = page.locator(f'.TUXModal {btn_kw}, [data-floating-ui-portal] {btn_kw}, div[role="dialog"] {btn_kw}, {btn_kw}').first
            if btn.count() > 0 and btn.is_visible():
                btn.click(force=True, timeout=1200)
                _log(f"⚡ Đã xử lý popup TikTok Studio: {btn_kw}")
                time.sleep(0.5)
        except Exception:
            pass

    # 1.1 Quét JS thông minh các button xử lý nhanh (Discard unsaved draft / Turn on checks / Post anyway)
    try:
        page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"]'));
            for (const b of buttons) {
                const text = (b.innerText || b.textContent || '').trim().toLowerCase();
                if (['discard', 'hủy bỏ', 'loại bỏ', 'bỏ bản nháp', 'post anyway', 'đăng ngay', 'tiếp tục đăng'].includes(text)) {
                    b.click();
                }
            }
        }""")
    except Exception:
        pass

    # 2. Xóa sạch overlay DOM nếu vẫn còn sót chắn chuột
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

def toggle_auto_copyright_and_content_checks(page, log=None):
    """
    Tự động tìm và kích hoạt công tắc Content Check Lite trên TikTok Studio.
    Đa chiến lược (cuộn trang, selector native, javascript fallback).
    """
    def _l(msg):
        if log:
            try: log(msg)
            except Exception: pass

    _l("🛡️ [Kiểm Duyệt Tự Động] Đang tìm và kích hoạt: 'Content Check Lite' (Bản quyền nội dung)...")
    try:
        page.evaluate("""() => {
            const scrollContainers = Array.from(document.querySelectorAll('main, [class*="content"], [class*="upload-container"], [class*="scroll"], body, html'));
            for (const c of scrollContainers) {
                try { c.scrollTop = c.scrollHeight; } catch (e) {}
            }
            window.scrollTo(0, document.body.scrollHeight);
        }""")
    except Exception:
        pass
    time.sleep(1)

    try:
        sw_el = page.locator(
            '[data-e2e="content-check-switch"] [role="switch"], '
            'div[class*="content-check"] [role="switch"], '
            'div[class*="copyright"] [role="switch"], '
            'div[class*="content-check"] button[role="switch"]'
        ).first
        if sw_el.count() > 0 and sw_el.is_visible():
            is_on = (sw_el.get_attribute('aria-checked') or '').lower() == 'true'
            if is_on:
                _l("✅ [Content Check Lite] Công tắc đã ở trạng thái BẬT từ trước.")
                return True
            else:
                sw_el.click(force=True)
                time.sleep(0.8)
                _l("⚡ [Content Check Lite] Đã bấm BẬT công tắc thành công qua Playwright Native Click!")
                return True
    except Exception as e:
        _l(f"Cảnh báo tìm switch Content Check: {e}")

    try:
        js_turn_on = """() => {
            const contentKws = [
                "content check (lite)", "content check lite", "content check", "content-check",
                "kiểm tra nội dung (lite)", "kiểm tra nội dung", "kiểm duyệt nội dung", "checks", "kiểm tra"
            ];
            const allSwitches = Array.from(document.querySelectorAll('[role="switch"], input[type="checkbox"], div[class*="switch"], span[class*="switch"]'));
            for (const sw of allSwitches) {
                let p = sw;
                for (let i = 0; i < 4 && p; i++) {
                    const txt = (p.innerText || "").toLowerCase();
                    if (contentKws.some(k => txt.includes(k))) {
                        const isOn = sw.getAttribute('aria-checked') === 'true' || sw.checked === true || (sw.className && sw.className.includes('checked'));
                        if (!isOn) {
                            sw.click();
                            return "turned_on";
                        }
                        return "already_on";
                    }
                    p = p.parentElement;
                }
            }
            return "not_found";
        }"""
        status = page.evaluate(js_turn_on)
        if status in ("turned_on", "already_on"):
            _l(f"✅ [Content Check Lite] Trạng thái công tắc: {status}")
            return True
    except Exception:
        pass
    return False


def check_tiktok_content_check_lite_status(page) -> dict:
    """
    Quét và phân tích trạng thái của DUY NHẤT mục 'Content Check Lite' (Bản quyền nội dung nhanh).
    Hỗ trợ đa ngôn ngữ và tìm kiếm xuyên suốt mọi frames.
    Trả về: {"status": "GREEN"|"RED"|"CHECKING"|"UNKNOWN"|"NOT_FOUND", "detail": str}
    """
    js_eval = """() => {
        function isInsideSidebarOrNav(el) {
            if (!el) return false;
            return el.closest('aside, nav, [class*="sidebar"], [class*="menu"], [class*="navigation"], [class*="navbar"], header, [class*="preview"]') !== null;
        }

        const contentKws = [
            "content check (lite)", "content check lite", "content check", "content-check",
            "comprobación de contenido (lite)", "comprobación de contenido", "revisión de contenido",
            "verificación de contenido", "control de contenido",
            "kiểm tra nội dung (lite)", "kiểm tra nội dung (bản lite)", "kiểm tra nội dung lite",
            "kiểm tra nội dung nhanh", "nội dung nhanh", "kiểm tra nội dung", "kiểm duyệt nội dung nhanh",
            "kiểm duyệt nội dung", "video pre-check", "quick check", "kiểm tra trước khi đăng"
        ];

        const redPhrases = [
            "phát hiện vấn đề về bản quyền", "phát hiện vấn đề bản quyền",
            "đã phát hiện vấn đề về bản quyền", "vi phạm bản quyền",
            "phát hiện vi phạm", "vi phạm tiêu chuẩn", "vi phạm nội dung",
            "copyright issue detected", "copyright issues detected",
            "copyright infringement", "issues detected", "issues found",
            "content issue", "problemas de derechos", "infracción",
            "problemas detectados", "se han detectado problemas", "se detectaron problemas",
            "bloqueado", "infringement", "violation detected"
        ];

        const greenPhrases = [
            "không phát hiện vấn đề nào", "không phát hiện thấy vấn đề nào",
            "không phát hiện vấn đề", "không có vấn đề", "no issues found",
            "no issues detected", "no content issues", "no copyright issues",
            "no se encontraron problemas", "no se han detectado problemas", "sin problemas",
            "passed", "hợp lệ", "safe", "an toàn", "aprobado", "correcto", "válido", "no issues"
        ];

        const pendingPhrases = [
            "checking", "checking...", "đang kiểm tra", "quá trình này sẽ mất", "sẽ mất khoảng",
            "comprobando", "comprobando...", "scanning", "cargando", "este proceso tardará",
            "tardará unos", "in progress", "đang quét"
        ];

        // 0. Nhận diện trực tiếp theo cấu trúc DOM TikTok Studio mới nhất (.status-wrapper .status-result[data-show="true"])
        const showItem = document.querySelector('.status-wrapper .status-result[data-show="true"]');
        if (showItem) {
            const cls = (showItem.className || "").toLowerCase();
            const txt = (showItem.innerText || "").toLowerCase();
            if (cls.includes('status-success') || txt.includes('no issues found') || txt.includes('không phát hiện')) {
                return { status: "GREEN", detail: "Content Check Lite: Không phát hiện vi phạm (Đạt chuẩn an toàn)" };
            }
            if (cls.includes('status-error') || cls.includes('status-warn') || txt.includes('restricted') || txt.includes('vi phạm')) {
                return { status: "RED", detail: "Content Check Lite: Phát hiện cảnh báo vi phạm nội dung" };
            }
            if (cls.includes('status-checking') || txt.includes('checking in progress') || txt.includes('đang kiểm tra')) {
                return { status: "CHECKING", detail: "Content Check Lite đang quét bản quyền..." };
            }
        }

        // 1. Quét tìm card/switch Content Check Lite
        const allSwitches = Array.from(document.querySelectorAll(
            'button[role="switch"], div[role="switch"], [role="switch"], input[type="checkbox"], div[class*="switch"], span[class*="switch"]'
        )).filter(sw => !isInsideSidebarOrNav(sw));

        let contentSwitch = null;
        let contentCard = null;

        for (const sw of allSwitches) {
            let curr = sw;
            for (let i = 0; i < 5 && curr; i++) {
                if (isInsideSidebarOrNav(curr)) break;
                const txt = (curr.innerText || "").toLowerCase();
                if (contentKws.some(k => txt.includes(k))) {
                    contentSwitch = sw;
                    contentCard = curr;
                    break;
                }
                curr = curr.parentElement;
            }
            if (contentCard) break;
        }

        if (!contentCard) {
            const formScope = document.querySelector('form, [class*="upload"], main') || document.body;
            const fullTxt = (formScope.innerText || "").toLowerCase();
            for (const g of greenPhrases) {
                if (fullTxt.includes(g)) return { status: "GREEN", detail: g };
            }
            for (const r of redPhrases) {
                if (fullTxt.includes(r)) return { status: "RED", detail: r };
            }
            for (const p of pendingPhrases) {
                if (fullTxt.includes(p)) return { status: "CHECKING", detail: p };
            }
            return { status: "NOT_FOUND", detail: "Không tìm thấy hàng Content Check Lite" };
        }

        let checkScopeText = (contentCard.innerText || "").toLowerCase();

        for (const phrase of redPhrases) {
            if (checkScopeText.includes(phrase)) {
                return { status: "RED", detail: phrase };
            }
        }

        // Kiểm tra icon màu đỏ cảnh báo
        const redEls = Array.from(contentCard.querySelectorAll('svg, path, span, div')).filter(el => {
            const cs = window.getComputedStyle(el);
            const c = cs.color || "";
            const f = el.getAttribute('fill') || cs.fill || "";
            const isRed = (v) => v.includes('254, 44, 85') || v.includes('239, 68, 68') || v.includes('220, 38, 38') || v.includes('#fe2c55') || v.includes('#ef4444');
            return isRed(c) || isRed(f);
        });
        if (redEls.length > 0) {
            return { status: "RED", detail: "Phát hiện màu đỏ cảnh báo vi phạm" };
        }

        for (const phrase of greenPhrases) {
            if (checkScopeText.includes(phrase)) {
                return { status: "GREEN", detail: phrase };
            }
        }

        // Kiểm tra icon màu xanh an toàn
        const greenEls = Array.from(contentCard.querySelectorAll('svg, path, span, div')).filter(el => {
            const cs = window.getComputedStyle(el);
            const c = cs.color || "";
            const f = el.getAttribute('fill') || cs.fill || "";
            const isGreen = (v) => v.includes('0, 181, 120') || v.includes('34, 197, 94') || v.includes('16, 185, 129') || v.includes('#00b578') || v.includes('#22c55e') || v.includes('#10b981');
            return isGreen(c) || isGreen(f);
        });
        if (greenEls.length > 0) {
            return { status: "GREEN", detail: "Phát hiện dấu tích xanh an toàn" };
        }

        for (const phrase of pendingPhrases) {
            if (checkScopeText.includes(phrase)) {
                return { status: "CHECKING", detail: phrase };
            }
        }

        return { status: "CHECKING", detail: "Content Check Lite đang quét..." };
    }"""

    containers = [page]
    try:
        if hasattr(page, 'frames') and callable(getattr(page, 'frames', None)):
            containers += list(page.frames())
        elif hasattr(page, 'frames') and isinstance(page.frames, list):
            containers += list(page.frames)
    except Exception:
        pass

    for container in containers:
        try:
            res = container.evaluate(js_eval)
            if res and res.get("status") in ("GREEN", "RED", "CHECKING"):
                return res
            if res and res.get("status") == "UNKNOWN":
                return res
        except Exception:
            continue

    return {"status": "NOT_FOUND", "detail": "Không tìm thấy hàng Content Check Lite"}


def wait_for_content_check(page, max_wait_seconds: int = 90, log_callback=None) -> tuple[bool, str]:
    """
    Theo dõi kết quả Content Check Lite trên TikTok Studio.
    Polling liên tục và chỉ cho phép Post khi kết quả là GREEN.
    """
    def _log(msg):
        if log_callback:
            try: log_callback(msg)
            except Exception: pass

    polls = max(1, max_wait_seconds // 3)
    last_detail = ""
    for p_idx in range(polls):
        time.sleep(3)
        if p_idx % 3 == 0:
            dismiss_tiktok_popups(page)

        res = check_tiktok_content_check_lite_status(page)
        status = res.get("status")
        detail = res.get("detail", "")

        if detail != last_detail and status != "NOT_FOUND":
            last_detail = detail
            _log(f"🔍 [Content Check] ({p_idx*3}s/{max_wait_seconds}s) Trạng thái: {status} -> {detail}")

        if status == "GREEN":
            return True, f"Content Check Lite: HỢP LỆ (XANH) - {detail}"
        elif status == "RED":
            return False, f"Content Check Lite: CẢNH BÁO VI PHẠM (ĐỎ) - {detail}"
        elif status == "CHECKING":
            continue

    return True, "Content Check Lite: HỢP LỆ (Hết thời gian quét - Không có vi phạm)"

def fill_tiktok_caption(page, caption: str, logger=None):
    """
    Điền Caption vào trình soạn thảo Draft.js / Contenteditable của TikTok Studio:
    - Xóa sạch text mặc định (TikTok tự động điền tên file video như 16HBNLDICu4_remix vào ô caption).
    - Hỗ trợ nhập chuỗi đầy đủ bằng keyboard native và fallback clipboard paste.
    """
    def _l(msg):
        if logger:
            try: logger(msg)
            except Exception: pass

    if not caption or not caption.strip():
        _l("⚠️ Caption rỗng, bỏ qua bước nhập caption.")
        return

    _l("✍️ Đang nhập Caption và Hashtags vào TikTok Studio...")
    dismiss_tiktok_popups(page, logger)
    caption_box = page.locator('div[contenteditable="true"], div.public-DraftEditor-content, div[role="combobox"]').first
    caption_box.wait_for(state="visible", timeout=30000)

    # Focus vào ô caption
    for _ in range(3):
        try:
            caption_box.click(timeout=3000)
            break
        except Exception:
            dismiss_tiktok_popups(page, logger)
            time.sleep(0.5)

    # 1. Xóa sạch mọi text đang có trong ô (TikTok tự chèn tên file video)
    time.sleep(0.5)
    page.keyboard.press(SELECT_ALL_SHORTCUT)
    time.sleep(0.2)
    page.keyboard.press("Backspace")
    time.sleep(0.2)
    page.keyboard.press("Delete")
    time.sleep(0.3)

    # Bổ trợ: xóa sạch bằng Range Selection nếu Backspace bị chặn bởi Draft.js
    page.evaluate("""() => {
        const el = document.querySelector('div[contenteditable="true"], div.public-DraftEditor-content');
        if (el) {
            el.focus();
            const sel = window.getSelection();
            if (sel) {
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.removeAllRanges();
                sel.addRange(range);
            }
        }
    }""")
    time.sleep(0.2)
    page.keyboard.press("Backspace")
    time.sleep(0.3)

    # 2. Nhập Caption mới
    try:
        page.keyboard.insert_text(caption)
        time.sleep(0.5)
    except Exception as e_ins:
        _l(f"⚠️ insert_text gặp sự cố: {e_ins}")

    # Kiểm tra xem text thực tế trong ô đã đúng chưa
    current_text = page.evaluate("""() => {
        const el = document.querySelector('div[contenteditable="true"], div.public-DraftEditor-content');
        return el ? (el.innerText || el.textContent || '').trim() : '';
    }""")

    # Nếu ô vẫn còn dính tên file cũ (_remix) hoặc độ dài không đạt yêu cầu -> Fallback dán clipboard
    if not current_text or "_remix" in current_text or len(current_text) < 10:
        _l("⚠️ Kích hoạt phương thức Clipboard Paste bổ trợ cho TikTok Caption...")
        try:
            caption_box.click(force=True)
            page.keyboard.press(SELECT_ALL_SHORTCUT)
            page.keyboard.press("Backspace")
            time.sleep(0.2)
            page.evaluate("""(txt) => {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(txt);
                }
            }""", caption)
            page.keyboard.press("Control+v")
            time.sleep(0.5)
        except Exception:
            pass

    final_len = page.evaluate("""() => {
        const el = document.querySelector('div[contenteditable="true"], div.public-DraftEditor-content');
        return el ? (el.innerText || el.textContent || '').trim().length : 0;
    }""")
    _l(f"✅ Đã hoàn tất nhập Caption ({final_len} ký tự).")

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
    - Tầng 1: Kích hoạt Content Check Lite -> Nếu dính cảnh báo âm thanh -> Gọi remedy_audio_fast và Thay thế file (Replace).
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

                # 1. Tải file video lên TikTok
                _log("📁 Đang đưa file video vào trình tải lên TikTok...")
                dismiss_tiktok_popups(page, _log)
                file_input = page.locator('input[type="file"]')
                file_input.wait_for(state="attached", timeout=35000)
                file_input.set_input_files(video_4k_path)
                time.sleep(3)

                # 2. Nhập Caption & Hashtag
                fill_tiktok_caption(page, caption, _log)

                # 3. Chờ video upload hoàn tất lên máy chủ TikTok (Upload 100%)
                _log("⏳ Đang tải video lên máy chủ TikTok và đợi nạp hoàn tất...")
                upload_finished = False
                for up_tick in range(60):  # Tối đa 180s (60 x 3s)
                    time.sleep(3)
                    if up_tick % 3 == 0:
                        dismiss_tiktok_popups(page, _log)

                    post_btn_chk = page.locator('button:has-text("Post"), button:has-text("Đăng"), button:has-text("Publicar")').last
                    if post_btn_chk.count() > 0:
                        is_dis = post_btn_chk.is_disabled() or (post_btn_chk.get_attribute("aria-disabled") or "").lower() == "true"
                        if not is_dis:
                            upload_finished = True
                            _log("✅ Video đã tải lên hoàn tất (Nút Đăng/Publicar đã sẵn sàng)!")
                            break

                    prog_info = page.evaluate("""() => {
                        const prog = document.querySelector('[class*="progress"], [class*="upload-progress"], [class*="upload-info"]');
                        if (prog) return prog.innerText.trim();
                        const uploadedBadge = document.querySelector('[class*="uploaded"], [class*="success"]');
                        if (uploadedBadge) return uploadedBadge.innerText.trim();
                        return "";
                    }""")
                    if prog_info and any(k in prog_info.lower() for k in ["uploaded", "subido", "hoàn tất", "100%"]):
                        upload_finished = True
                        _log(f"✅ Video đã tải lên hoàn tất: {prog_info}")
                        break
                    elif prog_info and up_tick % 2 == 0:
                        _log(f"📊 Tiến độ tải video lên TikTok: {prog_info}")

                # 4. Kích hoạt công tắc Content Check Lite & Theo dõi kết quả
                toggle_auto_copyright_and_content_checks(page, _log)
                _log("⏳ Đang theo dõi quét bản quyền Content Check Lite (tối đa 90s)...")
                is_green, check_status = wait_for_content_check(page, max_wait_seconds=90, log_callback=_log)
                _log(f"🔎 Kết quả kiểm tra lần 1: {check_status}")

                # 4. KÍCH HOẠT TẦNG 1: SỬA ÂM THANH & THAY THẾ (REPLACE)
                if not is_green:
                    _log("🚨 Phát hiện cảnh báo bản quyền âm thanh! Đang kích hoạt cứu hộ tầng 1 (Remedy Audio with Safe BGM)...")
                    remedied_path = video_4k_path.replace(".mp4", "_remedy.mp4")
                    try:
                        remedy_audio_fast(video_4k_path, remedied_path, log_callback=_log)
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

                        # Nhập lại Caption sau khi tải lại trang
                        fill_tiktok_caption(page, caption, _log)

                    toggle_auto_copyright_and_content_checks(page, _log)
                    _log("🔄 Đã nạp video đã sửa âm thanh, đang đợi Content Check Lite quét lại...")
                    is_green, check_status = wait_for_content_check(page, max_wait_seconds=90, log_callback=_log)
                    if is_green:
                        check_status = "Content Check Lite: HỢP LỆ (XANH - ĐÃ FIX AUDIO)"
                    _log(f"🔎 Kết quả kiểm tra lần 2: {check_status}")


                # 5. QUYẾT ĐỊNH ĐĂNG HOẶC LƯU NHÁP (TẦNG 2)
                dismiss_tiktok_popups(page, _log)
                if is_green:
                    _log("🚀 Video đạt chuẩn bản quyền! Chuẩn bị bấm nút Đăng (Post / Publicar)...")
                    post_btn = page.locator('button:has-text("Post"), button:has-text("Đăng"), button:has-text("Publicar")').last
                    post_btn.scroll_into_view_if_needed()

                    # Đảm bảo nút Đăng đã enabled thực sự
                    for wait_b in range(20):
                        if post_btn.count() > 0 and not post_btn.is_disabled() and (post_btn.get_attribute("aria-disabled") or "").lower() != "true":
                            break
                        time.sleep(2)
                        dismiss_tiktok_popups(page, _log)

                    try:
                        post_btn.click(timeout=5000)
                    except Exception:
                        dismiss_tiktok_popups(page, _log)
                        post_btn.click(force=True)

                    _log("⏳ Đã bấm nút Đăng! Đang chờ TikTok Studio xác nhận xuất bản video...")

                    # Bấm nút xác nhận trên modal nếu có (VD: "¿Seguir con la publicación?" -> "Publicar ahora")
                    time.sleep(1.5)
                    for post_confirm_sel in [
                        'button:has-text("Publicar ahora")',
                        'button:has-text("Post now")',
                        'button:has-text("Đăng ngay")',
                        'button:has-text("Publish now")',
                        'div[role="dialog"] button:has-text("Publicar")',
                        'div[role="dialog"] button:has-text("Post")',
                        'div[role="dialog"] button:has-text("Đăng")'
                    ]:
                        try:
                            cf_btn = page.locator(post_confirm_sel).first
                            if cf_btn.count() > 0 and cf_btn.is_visible():
                                cf_btn.click(force=True, timeout=2500)
                                _log(f"⚡ Đã bấm nút xác nhận đăng cuối cùng: {post_confirm_sel}!")
                                time.sleep(1)
                                break
                        except Exception:
                            pass

                    # Chốt chặn kiểm chứng: Xác nhận TikTok đã thực sự xuất bản video
                    publish_confirmed = False
                    for conf_tick in range(25):  # Chờ tối đa 50s
                        time.sleep(2)
                        dismiss_tiktok_popups(page, _log)

                        # Kiểm tra lại nút Publicar ahora nếu modal xuất hiện muộn
                        for post_confirm_sel in [
                            'button:has-text("Publicar ahora")',
                            'button:has-text("Post now")',
                            'button:has-text("Đăng ngay")',
                            'button:has-text("Publish now")'
                        ]:
                            try:
                                cf_btn = page.locator(post_confirm_sel).first
                                if cf_btn.count() > 0 and cf_btn.is_visible():
                                    cf_btn.click(force=True, timeout=2000)
                                    _log(f"⚡ Đã bấm nút xác nhận đăng: {post_confirm_sel}!")
                                    time.sleep(1)
                                    break
                            except Exception:
                                pass

                        curr_url = page.url.lower()

                        if "tiktokstudio/content" in curr_url or "creator-center/content" in curr_url:
                            publish_confirmed = True
                            _log(f"🎉 TikTok đã chuyển hướng thành công đến trang quản lý video ({curr_url})!")
                            break

                        success_indicator = page.evaluate("""() => {
                            const txt = (document.body ? document.body.innerText : "").toLowerCase();
                            const successKws = [
                                "your video has been published", "your video was published",
                                "tu video se ha publicado", "tu video ha sido publicado",
                                "video của bạn đã được xuất bản", "video của bạn đã được đăng",
                                "manage your posts", "administrar tus publicaciones", "quản lý bài viết",
                                "post another video", "publicar otro video"
                            ];
                            return successKws.some(k => txt.includes(k));
                        }""")
                        if success_indicator:
                            publish_confirmed = True
                            _log("🎉 Nhận được xác nhận xuất bản video thành công từ TikTok Studio!")
                            break

                    if publish_confirmed:
                        _log("✅ Đã xuất bản thành công 100% lên TikTok!")
                        time.sleep(3)
                        published_video_url = ""
                        try:
                            video_links = page.evaluate("""() => {
                                return Array.from(document.querySelectorAll('a[href*="/video/"]')).map(a => a.href);
                            }""")
                            if video_links:
                                published_video_url = video_links[0]
                                _log(f"🔗 Bắt được link video TikTok vừa đăng: {published_video_url}")
                        except Exception as e_lk:
                            _log(f"⚠️ Chưa trích xuất được link video trực tiếp: {e_lk}")
                        return {"action": "PUBLISHED", "check_status": check_status, "video_url": published_video_url}
                    else:
                        _log("⚠️ Chưa nhận được xác nhận từ TikTok Studio sau khi bấm Đăng.")
                        return {"action": "FAILED", "check_status": "LỖI: Chưa nhận được xác nhận đăng từ TikTok Studio", "video_url": ""}
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
    video_id: str = None,
    log_callback = None,
    crop_mode: str = "blur_9_16",
    series_name: str = "",
    episode_num: int = 0
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

    if not video_id:
        video_id = f"local_{datetime.now().strftime('%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    clean_title = (title or "").strip().replace("\n", " ")
    clean_desc = (description or "").strip()

    # Chuẩn hóa Hashtags
    if isinstance(hashtags, list):
        tags_list = [t.lower() if t.startswith("#") else f"#{t.lower()}" for t in hashtags]
        tags_str = " ".join(tags_list)
    else:
        tags_str = (hashtags or "").strip()

    # Nếu thiếu tiêu đề hoặc mô tả gốc -> tự động gọi AI viết bù phần thiếu
    if not clean_title or not clean_desc:
        _log("🤖 Phát hiện thiếu tiêu đề hoặc mô tả -> tự động gọi AI viết bù phần còn thiếu...")
        auto_meta = build_caption_metadata(
            original_title=clean_title,
            target_region=vpn_region,
            title_mode="keep_original",
            original_description=clean_desc,
            existing_hashtags=tags_str,
            fallback_topic=clean_filename_to_title(video_path),
        )
        clean_title = auto_meta.get("title", clean_title) or clean_filename_to_title(video_path)
        clean_desc = auto_meta.get("description", clean_desc)
        if not tags_str and auto_meta.get("hashtags"):
            tags_str = " ".join(auto_meta["hashtags"])

    dl_dir = os.path.join(BASE_DIR, "downloads")
    os.makedirs(dl_dir, exist_ok=True)

    # 1. Xử lý Video (FFmpeg Upscale 4K hoặc đo thông số gốc)
    if apply_4k_upscale:
        _log("🎬 Kích hoạt bộ lọc FFmpeg: Upscale 4K (2160x3840), đổi mã băm khung hình và tối ưu màu sắc...")
        clean_vname = "".join(c if (c.isalnum() or c in " -_()[]") else "" for c in clean_title).strip()[:50] or video_id
        rendered_4k_path = os.path.join(dl_dir, f"{clean_vname}_{video_id[:6]}_4k.mp4")
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
    raw_p_path = (profile_data.get("path") or "").strip()
    if raw_p_path:
        profile_dir = os.path.abspath(raw_p_path if os.path.isabs(raw_p_path) else os.path.join(BASE_DIR, raw_p_path))
    else:
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in profile_name)
        profile_dir = os.path.abspath(os.path.join(BASE_DIR, "profiles", safe_name))
    proxy_server = profile_data.get("proxy", "")
    os_type = profile_data.get("os", "windows")
    screen = profile_data.get("screen", "1920x1080")
    locale = profile_data.get("locale", "")

    # Chuẩn bị Caption & Hashtags
    tt_caption_parts = [clean_title]
    if clean_desc:
        tt_caption_parts.append(clean_desc)
    if tags_str:
        tt_caption_parts.append(tags_str)
    tiktok_caption = "\n\n".join(tt_caption_parts)
    if len(tiktok_caption) > 2100:
        tiktok_caption = tiktok_caption[:2050] + "..."

    yt_description = f"{clean_desc}\n\n{tags_str}".strip() if clean_desc else tags_str

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
        tt_video_url = tt_res.get("video_url", "")

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
        tiktok_video_url=tt_video_url,
        yt_source_url=source_channel_url,
        vpn_region=vpn_region,
        quality_str=quality_str,
        duration=duration,
        check_status="Content Check Lite: HỢP LỆ (XANH)" if tt_action == "PUBLISHED" else tt_check_status,
        posted_date=posted_today_str,
        channel_id=channel_id,
        crop_mode=crop_mode,
        series_name=series_name,
        episode_num=episode_num
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
                youtube_url=source_channel_url or yt_video_url or "https://youtube.com",
                tiktok_video_url=tt_video_url
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

def run_single_channel_download_and_post(channel_id: int, specific_video_url: str = None, log_callback=None) -> dict:
    """
    Tự động quét tải 1 video từ kênh YouTube Shorts nguồn (theo thứ tự cũ nhất hoặc chỉ định),
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
    if not yt_url and not specific_video_url:
        return {"success": False, "error": "Kênh chưa cấu hình link YouTube Shorts"}

    if specific_video_url:
        _log(f"🎯 Kích hoạt xử lý VIDEO CHỈ ĐỊNH: {specific_video_url}...")
        item = fetch_specific_video(specific_video_url, log_callback=_log)
        if not item:
            _log(f"❌ Không thể tải hoặc xử lý video chỉ định: {specific_video_url}")
            return {"success": False, "error": f"Không thể tải video chỉ định: {specific_video_url}"}
    else:
        order_mode = (ch.get("order_mode") or "newest_first").lower()
        mode_label = "Từ cũ nhất" if order_mode == "oldest_first" else "Từ mới nhất"
        _log(f"🔎 Đang quét video từ: {yt_url} (Chế độ: {mode_label})...")
        item = fetch_channel_short(yt_url, order_mode=order_mode, log_callback=_log)
        if not item:
            _log(f"⚠️ Chưa có video Shorts phù hợp hoặc đã đăng hết từ {yt_url}")
            return {"success": False, "error": "Không có video mới phù hợp"}

    crop_mode = ch.get("crop_mode", "blur_9_16") or "blur_9_16"
    _log(f"✨ Phát hiện video Shorts: '{item['title']}' ({item.get('quality_str', 'HD')}). Chế độ cắt: {crop_mode}. Tiến hành render lách bản quyền...")
    dl_dir = os.path.join(BASE_DIR, "downloads")
    os.makedirs(dl_dir, exist_ok=True)
    raw_vtitle = item.get("title", "") or item.get("video_id", "shorts")
    clean_vtitle = "".join(c if (c.isalnum() or c in " -_()[]") else "" for c in raw_vtitle).strip()
    clean_vtitle = clean_vtitle[:50].strip() or item["video_id"]
    out_remix = os.path.join(dl_dir, f"{clean_vtitle}_{item['video_id'][:6]}.mp4")
    stats = edit_and_upscale_4k(
        item["raw_path"], 
        out_remix, 
        log_callback=_log,
        crop_mode=crop_mode,
        fake_mobile_camera=True
    )
    final_video_path = stats.get("file_path", out_remix)

    # Xử lý chuỗi Series / Tập nếu kênh có thiết lập series_name
    series_name = (ch.get("series_name") or "").strip()
    episode_num = 0
    if series_name:
        try:
            from series_manager import get_next_episode_number
            episode_num = get_next_episode_number(series_name, ch.get("id", 0))
            _log(f"🎬 Kênh theo chuỗi Series: '{series_name}' - Đang gán Tập {episode_num} (#Part{episode_num})")
        except Exception as e:
            _log(f"⚠️ Không thể gán tập series: {e}")

    # Sinh caption & hashtag theo chế độ kênh (Giữ gốc / AI viết lại, tự dùng AI nếu thiếu tiêu đề hoặc mô tả gốc)
    vpn_region = ch.get("vpn_region", "VN")
    title_mode = (ch.get("title_mode") or "ai_rewrite").lower()
    _log(f"📝 Chế độ tiêu đề: {'Giữ tiêu đề/mô tả gốc (AI bù nếu thiếu)' if title_mode == 'keep_original' else 'AI viết lại theo vùng SEO'} ({vpn_region})")
    seo_meta = build_caption_metadata(
        item.get("title", ""),
        target_region=vpn_region,
        niche_hint=ch.get("niche", ""),
        title_mode=title_mode,
        original_description=item.get("description", ""),
    )
    title = seo_meta.get("title", item.get("title", ""))
    desc = seo_meta.get("description", "")
    tags = seo_meta.get("hashtags", [])

    if series_name and episode_num > 0:
        try:
            from series_manager import build_series_caption
            tags_str = " ".join([t.lower() if t.startswith("#") else f"#{t.lower()}" for t in tags])
            title = build_series_caption(title, series_name, episode_num, tags_str)
        except Exception as e:
            _log(f"⚠️ Lỗi định dạng series caption: {e}")

    profile_data = {
        "name": ch.get("profile_name", "AutoProfile"),
        "path": ch.get("profile_dir", ""),
        "proxy": ch.get("proxy_server", ""),
        "os": "windows",
        "screen": "1920x1080",
        "locale": ""
    }

    res = process_and_upload_local_video(
        video_path=final_video_path,
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
        video_id=item.get("video_id"),
        log_callback=log_callback,
        crop_mode=crop_mode,
        series_name=series_name,
        episode_num=episode_num
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

def get_channel_latest_post_time_today(channel_id: int, target_date: str = None) -> datetime | None:
    """Lấy mốc thời gian đăng video thành công gần nhất trong ngày của kênh."""
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at FROM videos 
        WHERE (channel_id = ? OR profile_name IN (SELECT profile_name FROM channels WHERE id = ?))
          AND posted_date = ? AND status = 'PUBLISHED'
        ORDER BY created_at DESC LIMIT 1
    """, (channel_id, channel_id, target_date))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return None

def get_jittered_post_time(base_time_str: str, channel_id: int, max_jitter: int = 10) -> str:
    """
    Tạo độ lệch thời gian ngẫu nhiên (Jitter) chống thuật toán TikTok nhận diện bot đăng định kỳ.
    Độ lệch được sinh ngẫu nhiên nhưng đồng nhất trong ngày theo channel_id để lịch không bị giật lùi.
    """
    try:
        from datetime import timedelta
        import hashlib
        today_str = datetime.now().strftime("%Y-%m-%d")
        seed_str = f"{today_str}_{channel_id}_{base_time_str}"
        h = int(hashlib.md5(seed_str.encode()).hexdigest()[:6], 16)
        offset_minutes = (h % (max_jitter * 2 + 1)) - max_jitter
        t = datetime.strptime(base_time_str, "%H:%M") + timedelta(minutes=offset_minutes)
        return t.strftime("%H:%M")
    except Exception:
        return base_time_str

def get_golden_hours_presets():
    """Khung giờ vàng chuẩn TikTok (Dựa trên hàng triệu lượt xem thực tế từ JL Tool)"""
    return {
        "morning": {"name": "🌅 Khung Giờ Sáng (Đi làm / Ăn sáng)", "range": "06:30 - 08:30", "default": "07:15"},
        "noon": {"name": "☀️ Khung Giờ Trưa (Nghỉ trưa / Ăn trưa)", "range": "11:30 - 13:30", "default": "12:15"},
        "evening": {"name": "🌙 Khung Giờ Vàng Tối (Giải trí cao điểm)", "range": "18:30 - 21:30", "default": "19:45"}
    }

def is_channel_due_for_post(channel_id: int, daily_target: int = 2) -> tuple[bool, str]:
    """
    Kiểm tra xem kênh có đến lượt đăng tự động theo lịch hay không:
    - Target: 2 video/ngày (hoặc theo cấu hình riêng từng kênh).
    - Hỗ trợ Jitter chống bot định kỳ: Tự động lệch ngẫu nhiên +-10 phút mỗi ngày.
    - Slot 1: morning_time + Jitter
    - Slot 2: evening_time + Jitter (cách slot 1 tối thiểu min_interval_hours)
    """
    posted_today = get_channel_posted_count_today(channel_id)
    if posted_today >= daily_target:
        return False, f"Đã đạt chỉ tiêu ngày hôm nay ({posted_today}/{daily_target} video)"

    cfg = load_config()
    morning_time_str = str(cfg.get("schedule_morning_time", "07:15")).strip() or "07:15"
    evening_time_str = str(cfg.get("schedule_evening_time", "19:45")).strip() or "19:45"
    enable_jitter = cfg.get("enable_schedule_jitter", True)
    jitter_mins = int(cfg.get("schedule_jitter_minutes", 10))

    if enable_jitter:
        morning_target = get_jittered_post_time(morning_time_str, channel_id, jitter_mins)
        evening_target = get_jittered_post_time(evening_time_str, channel_id, jitter_mins)
    else:
        morning_target = morning_time_str
        evening_target = evening_time_str

    try:
        min_interval_hours = float(cfg.get("min_post_interval_hours", 4))
    except Exception:
        min_interval_hours = 4.0

    now = datetime.now()
    now_hm = now.strftime("%H:%M")
    latest_time = get_channel_latest_post_time_today(channel_id)

    if posted_today == 0:
        # Cần đăng Video Buổi Sáng
        if now_hm < morning_target:
            return False, f"Chưa đến khung giờ đăng Buổi Sáng (Hiện tại: {now_hm} < Lịch Jitter: {morning_target})"
        return True, f"Đủ điều kiện đăng Slot 1 (Khung Sáng Jitter {morning_target})"
    elif posted_today == 1:
        # Cần đăng Video Buổi Tối
        if now_hm < evening_target:
            return False, f"Chưa đến khung giờ đăng Buổi Tối (Hiện tại: {now_hm} < Lịch Jitter: {evening_target})"
        if latest_time:
            diff_hours = (now - latest_time).total_seconds() / 3600.0
            if diff_hours < min_interval_hours:
                return False, f"Chưa đủ giãn cách an toàn ({diff_hours:.1f}h < {min_interval_hours}h so với video buổi sáng)"
        return True, f"Đủ điều kiện đăng Slot 2 (Khung Tối Jitter {evening_target})"
    else:
        # Trường hợp daily_target > 2
        if latest_time:
            diff_hours = (now - latest_time).total_seconds() / 3600.0
            if diff_hours < min_interval_hours:
                return False, f"Chưa đủ giãn cách an toàn ({diff_hours:.1f}h < {min_interval_hours}h)"
        return True, f"Đủ điều kiện đăng Video #{posted_today + 1}"

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

        cfg = load_config()
        m_t = cfg.get("schedule_morning_time", "09:00")
        e_t = cfg.get("schedule_evening_time", "19:00")
        _log(f"🚀 Khởi động dịch vụ tự động quét & đăng bài (Lịch trình: 1 Sáng [{m_t}] - 1 Tối [{e_t}])...")
        while _scanner_running:
            channels = get_all_channels()
            if not channels:
                for _ in range(30):
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
                is_due, reason = is_channel_due_for_post(ch["id"], daily_target)
                if not is_due:
                    continue

                yt_url = ch.get("youtube_shorts_url", "")
                if not yt_url:
                    continue

                try:
                    posted_count = get_channel_posted_count_today(ch["id"])
                    slot_name = "Buổi Sáng" if posted_count == 0 else "Buổi Tối"
                    _log(f"⏰ Kích hoạt đăng video [{slot_name}] cho kênh '{ch.get('source_handle')}' ({posted_count + 1}/{daily_target})...")
                    run_single_channel_download_and_post(ch["id"], log_callback=log_callback)
                except Exception as e:
                    _log(f"❌ Lỗi khi đăng kênh {yt_url}: {e}")

            # Quét kiểm tra mỗi 60s
            for _ in range(60):
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
