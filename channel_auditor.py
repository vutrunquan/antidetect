# -*- coding: utf-8 -*-
"""
channel_auditor.py
==================
BÁC SĨ KÊNH & PHÂN TÍCH TIKTOK CHUYÊN SÂU (CHANNEL HEALTH AUDITOR)
Tích hợp từ JL Tool sang Antidetect Browser (sử dụng lõi Camoufox an toàn tuyệt đối):
- Tự động quét chỉ số thực tế của từng kênh qua TikTok Studio (/tiktokstudio/content).
- Đếm chính xác số video 0 view, phát hiện sớm nguy cơ Shadowban / Bóp tương tác.
- Chấm điểm sức khỏe kênh (Health Score 0-100) và xếp loại (Healthy / Warning / Shadowban).
- Đưa ra Chiến lược Tăng trưởng 10.000 Follower chuẩn thực chiến từ AI.
"""

import os
import re
import time
import sqlite3
from typing import Dict, Any, Optional

from platform_utils import BASE_DIR
from browser_runner import parse_proxy
from proxy_bridge import setup_camoufox_proxy
from camoufox.sync_api import Camoufox

DB_PATH = os.path.join(BASE_DIR, "tiktok_bot_system.db")

def parse_tiktok_number(text: Any) -> int:
    """Chuyển đổi số kiểu TikTok (1.2K, 3.5M, 500, '0') thành số nguyên."""
    if text is None:
        return 0
    t = str(text).strip().upper()
    if not t or t == '0':
        return 0
    try:
        if 'K' in t:
            clean = t.replace('K', '').replace(',', '.').strip()
            return int(float(clean) * 1000)
        elif 'M' in t:
            clean = t.replace('M', '').replace(',', '.').strip()
            return int(float(clean) * 1000000)
        elif 'B' in t:
            clean = t.replace('B', '').replace(',', '.').strip()
            return int(float(clean) * 1000000000)
        clean = re.sub(r'[^\d]', '', t)
        return int(clean) if clean else 0
    except Exception:
        return 0

def build_10k_growth_strategy(res: Dict[str, Any], channel_name: str) -> str:
    """Xây dựng báo cáo Chiến lược Tăng trưởng 10K Follower dựa trên dữ liệu thực tế."""
    v_count = res.get("videos_count", 0)
    avg_v = res.get("avg_views", 0)
    zero_cnt = res.get("zero_view_count", 0)
    viral_cnt = res.get("viral_count", 0)
    status = res.get("health_status", "healthy")
    score = res.get("health_score", 85)

    advice_items = []
    if status == "shadowban" or zero_cnt >= 3:
        advice_items.append("🚨 <b>BÁO ĐỘNG SHADOWBAN:</b> Kênh có 3+ video 0 view. Tạm dừng đăng 24-48h, bật lách bản quyền FFmpeg và tiêm metadata camera iPhone/Samsung.")
    elif zero_cnt > 0:
        advice_items.append(f"⚠️ <b>CẢNH BÁO 0 VIEW:</b> Phát hiện {zero_cnt} video 0 view. Cần bật bộ lọc đổi mã băm và nhạc nền Safe BGM để tránh bị AI TikTok đánh trùng lặp.")
    else:
        advice_items.append("✅ <b>KÊNH AN TOÀN:</b> Không phát hiện video 0 view. Tiếp tục duy trì tần suất đăng 2 video/ngày theo lịch Jitter.")

    advice_items.append("💡 <b>CHIẾN LƯỢC SERIES:</b> Cắt video dài thành các phần (Tập 1, Tập 2) và gắn hashtag #Part1 để kích thích người xem tò mò bấm Follow xem tiếp.")
    advice_items.append("⏰ <b>KHUNG GIỜ VÀNG:</b> Đăng vào các khung giờ vàng: Sáng 07:15, Trưa 12:15, Tối 19:45 kèm độ lệch ngẫu nhiên +-10 phút chống bot.")

    return "\n\n".join(advice_items)

def audit_channel_health(channel_data: Dict[str, Any], headless: bool = True, log_func=None) -> Dict[str, Any]:
    """
    Truy cập vào TikTok Studio của kênh bằng Camoufox,
    đọc danh sách video & lượt xem thực tế để đánh giá sức khỏe kênh.
    """
    def _log(msg: str):
        if log_func:
            try: log_func(msg)
            except Exception: pass
        print(f"[Channel Auditor] {msg}")

    channel_id = channel_data.get("id", 0)
    channel_name = channel_data.get("tiktok_user") or channel_data.get("profile_name") or f"Kênh #{channel_id}"
    profile_dir = channel_data.get("profile_dir", "")
    proxy_server = channel_data.get("proxy_server", "")
    os_type = channel_data.get("vpn_region", "windows")

    _log(f"🩺 Bắt đầu khám sức khỏe cho kênh: {channel_name}...")

    result = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "videos_count": 0,
        "avg_views": 0,
        "zero_view_count": 0,
        "viral_count": 0,
        "health_status": "healthy",
        "health_score": 85,
        "recent_videos": [],
        "strategy": ""
    }

    if not profile_dir or not os.path.exists(profile_dir):
        _log(f"⚠️ Không tìm thấy thư mục profile: {profile_dir}")
        result["health_status"] = "unknown"
        result["health_score"] = 50
        result["strategy"] = "Vui lòng mở profile ít nhất 1 lần để đăng nhập TikTok."
        return result

    # Cấu hình Camoufox an toàn
    camoufox_kwargs = {
        "headless": headless,
        "os": "windows" if os_type.lower() not in ["windows", "macos", "linux"] else os_type.lower(),
        "persistent_context": True,
        "user_data_dir": profile_dir,
        "humanize": True,
        "disable_coop": True,
        "canvas_noise": False,
        "i_know_what_im_doing": True
    }

    proxy_cfg = parse_proxy(proxy_server) if proxy_server else None
    if proxy_cfg:
        camoufox_kwargs["proxy"] = proxy_cfg
        camoufox_kwargs["geoip"] = True

    try:
        _log("🛡️ Khởi chạy trình duyệt Camoufox để kiểm tra TikTok Studio...")
        with Camoufox(**camoufox_kwargs) as context:
            page = context.pages[0] if context.pages else context.new_page()
            studio_url = "https://www.tiktok.com/tiktokstudio/content"
            _log(f"🌐 Đang mở: {studio_url}...")
            page.goto(studio_url, timeout=45000, wait_until="domcontentloaded")
            time.sleep(4)

            # Quét dữ liệu video từ trang TikTok Studio
            extracted = page.evaluate("""() => {
                const rows = Array.from(document.querySelectorAll('tr, div[class*="table-row"], div[class*="item-row"], div[class*="video-item"]'));
                let vids = [];
                for (const r of rows) {
                    const txt = r.innerText || '';
                    if (!txt || txt.length < 5) continue;
                    
                    // Tìm số view (cụm chữ số kèm K/M hoặc số nguyên đứng cạnh icon view)
                    const mViews = txt.match(/(\\d+[\\.,]?\\d*\\s*[KMBkmb]?)\\s*(lượt xem|views|reproducciones)?/i);
                    const viewStr = mViews ? mViews[1] : '';

                    // Lấy tiêu đề video
                    const titleEl = r.querySelector('span, a, p, div[class*="title"], div[class*="desc"]');
                    const title = titleEl ? titleEl.innerText.trim().slice(0, 80) : txt.slice(0, 60);

                    if (viewStr || title) {
                        vids.push({ title: title, view_str: viewStr, raw_text: txt.slice(0, 100) });
                    }
                    if (vids.length >= 20) break;
                }
                return vids;
            }""")

            total_views = 0
            zero_views = 0
            viral_vids = 0
            parsed_vids = []

            for item in extracted:
                v_num = parse_tiktok_number(item.get("view_str", 0))
                if v_num == 0:
                    zero_views += 1
                elif v_num >= 10000:
                    viral_vids += 1
                total_views += v_num
                parsed_vids.append({
                    "title": item.get("title", ""),
                    "views": v_num,
                    "view_str": item.get("view_str", "0")
                })

            v_count = len(parsed_vids)
            avg_v = (total_views // v_count) if v_count > 0 else 0

            # Tính điểm sức khỏe (Health Score)
            score = 85
            if zero_views >= 3:
                status = "shadowban"
                score = 25
            elif zero_views > 0:
                status = "warning"
                score = max(45, 80 - (zero_views * 15))
            elif avg_v >= 2000 or viral_vids >= 1:
                status = "viral"
                score = 98
            else:
                status = "healthy"
                score = 85

            result.update({
                "videos_count": v_count,
                "avg_views": avg_v,
                "zero_view_count": zero_views,
                "viral_count": viral_vids,
                "health_status": status,
                "health_score": score,
                "recent_videos": parsed_vids[:10],
                "strategy": build_10k_growth_strategy(result, channel_name)
            })

            _log(f"✅ Khám sức khỏe hoàn tất: Điểm {score}/100 [{status.upper()}] - {zero_views} video 0-view.")

            # Cập nhật kết quả vào database
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    UPDATE channels 
                    SET health_score = ?, health_status = ?, last_audit_time = ?
                    WHERE id = ?
                """, (score, status, time.strftime("%Y-%m-%d %H:%M:%S"), channel_id))
                conn.commit()
                conn.close()
            except Exception as e:
                _log(f"⚠️ Lưu DB thất bại: {e}")

    except Exception as e:
        _log(f"❌ Lỗi trong quá trình khám sức khỏe kênh: {e}")
        result["health_status"] = "error"
        result["health_score"] = 50
        result["strategy"] = f"Lỗi kết nối kiểm tra: {e}"

    return result
