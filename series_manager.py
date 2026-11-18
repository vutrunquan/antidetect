# -*- coding: utf-8 -*-
"""
series_manager.py
=================
QUẢN LÝ VIDEO DẠNG SERIES / TẬP (PART 1, PART 2, PART 3...)
Tích hợp từ JL Tool sang Antidetect:
- Quản lý danh sách các tập trong chuỗi series nội dung.
- Tự động sinh số tập kế tiếp (Episode Auto-Increment).
- Tự động chèn thẻ Tập và Hashtag #Part{N} vào Caption để kích thích người xem bấm Follow xem tiếp.
- Đảm bảo các tập được tải lên tuần tự (FIFO), không bị lộn xộn nội dung.
"""

import os
import sqlite3
import re
from typing import Dict, Any, List, Optional
from platform_utils import BASE_DIR

DB_PATH = os.path.join(BASE_DIR, "tiktok_bot_system.db")

def get_next_episode_number(series_name: str, channel_id: int = 0) -> int:
    """Lấy số tập kế tiếp của chuỗi series."""
    if not series_name:
        return 1
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            SELECT MAX(episode_num) FROM videos 
            WHERE series_name = ? AND (channel_id = ? OR ? = 0)
        """, (series_name.strip(), channel_id, channel_id))
        row = cur.fetchone()
        conn.close()
        max_ep = row[0] if row and row[0] is not None else 0
        return int(max_ep) + 1
    except Exception:
        return 1

def build_series_caption(raw_title: str, series_name: str, episode_num: int, hashtags: str = "") -> str:
    """Tự động kết hợp tiêu đề, số tập và hashtag mồi câu follow."""
    clean_title = raw_title.strip()
    ep_tag = f"Tập {episode_num}" if episode_num > 0 else ""
    part_tag = f"#Part{episode_num}" if episode_num > 0 else ""

    parts = [clean_title]
    if ep_tag and ep_tag.lower() not in clean_title.lower():
        parts.append(f"({ep_tag})")
    if series_name and series_name.lower() not in clean_title.lower():
        parts.append(f"#{re.sub(r'[^a-zA-Z0-9_]', '', series_name)}")
    if part_tag:
        parts.append(part_tag)
    if hashtags:
        parts.append(hashtags.strip())
    
    parts.append("👉 Follow kênh để đón xem tập tiếp theo nhé!")
    return " ".join(parts)

def get_all_series(channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Lấy danh sách tất cả các series và tiến độ video đã tải lên."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = """
        SELECT series_name, COUNT(*) as total_videos, 
               SUM(CASE WHEN status = 'PUBLISHED' THEN 1 ELSE 0 END) as published_videos,
               MAX(episode_num) as max_episode
        FROM videos 
        WHERE series_name != '' AND series_name IS NOT NULL
    """
    params = []
    if channel_id:
        query += " AND channel_id = ?"
        params.append(channel_id)
    query += " GROUP BY series_name ORDER BY total_videos DESC"

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    series_list = []
    for r in rows:
        series_list.append({
            "series_name": r[0],
            "total_videos": r[1],
            "published_videos": r[2],
            "max_episode": r[3] or 0
        })
    return series_list
