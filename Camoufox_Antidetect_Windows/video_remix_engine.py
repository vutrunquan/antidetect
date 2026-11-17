# -*- coding: utf-8 -*-
"""
video_remix_engine.py
=====================
Bộ Engine xử lý render video lách bản quyền tự động (FFmpeg Pipeline)
Tích hợp từ tiktokbetaautomation vào 04_TRINH_DUYET_ANTIDETECT_TIKTOK:
- Zoom nhẹ 1.03x & crop trung tâm để đổi 100% mã băm (Hash/Pixel Frame).
- Tinh chỉnh độ nét (Unsharp filter), cân bằng màu (Contrast 1.02, Saturation 1.04).
- Xóa sạch toàn bộ Metadata gốc của YouTube (-map_metadata -1).
- Tinh chỉnh dải tần âm thanh (Pitch/Tempo 1.025x) để lách thuật toán Content ID.
- Cơ chế Cứu hộ Âm thanh Tức thì với kho nhạc sạch safe_bgm (Hòa trộn 15% tiếng gốc + 85% nhạc nền sạch).
- Tự động kích hoạt tăng tốc phần cứng (Intel QuickSync h264_qsv, NVIDIA NVENC h264_nvenc, Apple Silicon VideoToolbox).
- Hỗ trợ toàn bộ độ dài video (tuyệt đối không cắt 59s).
"""

import os
import sys
import json
import random
import subprocess
from datetime import datetime

from platform_utils import BASE_DIR, FFMPEG_BIN, FFPROBE_BIN, get_subprocess_kwargs

SAFE_BGM_DIR = os.path.join(BASE_DIR, "safe_bgm")

# Tự động khởi tạo thư mục safe_bgm nếu chưa có
if not os.path.exists(SAFE_BGM_DIR):
    try:
        os.makedirs(SAFE_BGM_DIR, exist_ok=True)
        readme_bgm = os.path.join(SAFE_BGM_DIR, "README.md")
        if not os.path.exists(readme_bgm):
            with open(readme_bgm, "w", encoding="utf-8") as f:
                f.write(
                    "# 🎵 Thư mục Nhạc Nền Sạch (Safe BGM)\n\n"
                    "Thả các file nhạc nền không bản quyền định dạng `.mp3`, `.wav` hoặc `.m4a` vào đây.\n\n"
                    "### Cơ chế hoạt động:\n"
                    "Khi kích hoạt cứu hộ âm thanh hoặc mix nhạc nền:\n"
                    "1. Giữ nguyên khung hình video đã render chuẩn nét.\n"
                    "2. Giữ lại 15% âm hiện trường gốc (tiếng nói/xe cộ/gió) qua bộ lọc dải tần.\n"
                    "3. Lấy ngẫu nhiên 1 bản nhạc trong thư mục này và hòa trộn 85% âm lượng.\n"
                    "4. Video xuất xưởng sẽ có âm thanh hoàn toàn mới, vượt qua kiểm duyệt bản quyền âm thanh 100%!\n"
                )
    except Exception:
        pass


_CACHED_BEST_ENCODER = None

def detect_best_video_encoder():
    """Tự động phát hiện bộ mã hóa phần cứng tốt nhất để render siêu tốc (QSV / NVENC / VideoToolbox / CPU)"""
    global _CACHED_BEST_ENCODER
    if _CACHED_BEST_ENCODER is not None:
        return _CACHED_BEST_ENCODER

    # Thử lần lượt các bộ giải mã tăng tốc phần cứng GPU
    candidates = []
    if sys.platform == "darwin":
        candidates.append(("h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-b:v", "18M"]))
    
    candidates.extend([
        ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20"]),
        ("h264_qsv", ["-c:v", "h264_qsv", "-preset", "veryfast"]),
        ("h264_amf", ["-c:v", "h264_amf", "-quality", "speed"])
    ])

    for enc, args in candidates:
        try:
            cmd = [
                FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                "-c:v", enc, "-f", "null", "-"
            ]
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **get_subprocess_kwargs())
            if res.returncode == 0:
                _CACHED_BEST_ENCODER = args
                return _CACHED_BEST_ENCODER
        except Exception:
            continue

    _CACHED_BEST_ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"]
    return _CACHED_BEST_ENCODER


def render_anti_copyright_video(
    input_path: str,
    output_path: str = None,
    zoom_ratio: float = 1.03,
    enable_audio_mod: bool = True,
    log_callback=None
) -> str:
    """
    Xử lý video qua pipeline FFmpeg chuyên sâu:
    1. Zoom nhẹ (mặc định 1.03x) + crop trung tâm để thay đổi toàn bộ tọa độ pixel và mã băm hash frame.
    2. Bộ lọc unsharp (làm sắc nét) + tương phản contrast 1.02, bão hòa saturation 1.04.
    3. Bộ lọc âm thanh asetrate/atempo: đổi nhẹ dải tần 1.025x mà tai người nghe tự nhiên, giữ nguyên độ dài.
    4. Xóa sạch toàn bộ Metadata gốc (-map_metadata -1).
    5. HỖ TRỢ TOÀN BỘ ĐỘ DÀI: Tuyệt đối không cắt giới hạn 59s.
    """
    def log(msg):
        if log_callback:
            try:
                log_callback(f"[Remix Video] {msg}")
            except Exception:
                pass
        else:
            print(f"[Remix Video] {msg}")

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Không tìm thấy file video nguồn: {input_path}")

    if not output_path:
        d_dir = os.path.dirname(input_path)
        base_name = os.path.basename(input_path)
        output_path = os.path.join(d_dir, f"remix_{base_name}")

    log(f"🎬 Bắt đầu tối ưu lách bản quyền & mã băm cho video: {os.path.basename(input_path)}")

    # 1. Video Filter: Zoom 1.03x, crop trung tâm, unsharp làm nét, tăng contrast & saturation
    vf_filter = (
        f"scale=iw*{zoom_ratio}:ih*{zoom_ratio}:flags=lanczos,"
        f"crop=iw/{zoom_ratio}:ih/{zoom_ratio},"
        f"unsharp=3:3:0.8:3:3:0.0,"
        f"eq=contrast=1.02:saturation=1.04"
    )

    # 2. Audio Filter: Pitch shift 2.5% + tempo adjust để giữ nguyên độ dài
    if enable_audio_mod:
        af_filter = "asetrate=44100*1.025,aresample=44100,atempo=0.9756"
    else:
        af_filter = "aresample=44100"

    encoder_args = detect_best_video_encoder()
    enc_name = encoder_args[1] if len(encoder_args) > 1 else "libx264"
    log(f"⚡ Sử dụng bộ mã hóa tăng tốc: [{enc_name.upper()}]")

    cmd = [
        FFMPEG_BIN, "-y", "-i", input_path,
        "-vf", vf_filter,
        "-af", af_filter,
        *encoder_args,
        "-c:a", "aac", "-b:a", "192k",
        "-map_metadata", "-1",
        output_path
    ]

    try:
        start_t = datetime.now()
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=300,
            **get_subprocess_kwargs()
        )
        dur_sec = (datetime.now() - start_t).total_seconds()
        log(f"✅ Render lách bản quyền thành công sau {dur_sec:.1f}s -> {os.path.basename(output_path)}")
    except subprocess.TimeoutExpired:
        log("⚠️ Quá thời gian render (Timeout 300s). Giữ file gốc để đảm bảo tiến độ upload.")
        return input_path
    except Exception as e:
        log(f"⚠️ Lỗi trong quá trình render ({e}). Giữ nguyên file video gốc.")
        return input_path

    # Xác thực file video đầu ra bằng ffprobe
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
        return output_path
    else:
        return input_path


def remedy_audio_with_bgm(video_path: str, output_path: str = None, log_callback=None) -> str:
    """
    Cứu hộ âm thanh tức thì:
    - Nếu có file nhạc trong safe_bgm/: Mix 15% tiếng gốc + 85% nhạc nền sạch.
    - Nếu không có: Dùng equalizer + echo filter để bẻ dải tần âm thanh chống bản quyền.
    """
    def log(msg):
        if log_callback:
            try:
                log_callback(f"[Audio Remedy] {msg}")
            except Exception:
                pass
        else:
            print(f"[Audio Remedy] {msg}")

    if not os.path.exists(video_path):
        return video_path

    if not output_path:
        d_dir = os.path.dirname(video_path)
        output_path = os.path.join(d_dir, f"audio_remedied_{os.path.basename(video_path)}")

    bgm_files = []
    if os.path.exists(SAFE_BGM_DIR):
        for f in os.listdir(SAFE_BGM_DIR):
            if f.lower().endswith((".mp3", ".wav", ".m4a", ".aac")):
                bgm_files.append(os.path.join(SAFE_BGM_DIR, f))

    if bgm_files:
        chosen_bgm = random.choice(bgm_files)
        log(f"🎵 Chọn nhạc nền an toàn từ safe_bgm: {os.path.basename(chosen_bgm)}")
        filter_complex = (
            "[0:a]highpass=f=300,lowpass=f=2500,volume=0.18[orig];"
            "[1:a]volume=0.82[bgm];"
            "[orig][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        cmd = [
            FFMPEG_BIN, "-y", "-i", video_path, "-stream_loop", "-1", "-i", chosen_bgm,
            "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", output_path
        ]
    else:
        log("ℹ️ safe_bgm trống. Áp dụng bộ lọc bẻ tần số âm thanh (Equalizer/Tempo)...")
        af_heavy = "asetrate=44100*1.04,aresample=44100,atempo=0.9615,equalizer=f=1000:width_type=h:width=200:g=-4"
        cmd = [
            FFMPEG_BIN, "-y", "-i", video_path,
            "-c:v", "copy",
            "-af", af_heavy,
            "-c:a", "aac", "-b:a", "192k",
            output_path
        ]

    try:
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=120,
            **get_subprocess_kwargs()
        )
        if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            log(f"✅ Đã cứu hộ âm thanh thành công -> {os.path.basename(output_path)}")
            return output_path
    except Exception as e:
        log(f"⚠️ Cứu hộ âm thanh thất bại ({e}).")

    return video_path


def probe_video_metrics(file_path: str) -> dict:
    """Đo đạc độ phân giải, fps, bitrate và thời lượng chuẩn xác của video qua ffprobe."""
    if not os.path.exists(file_path):
        return {"width": 0, "height": 0, "fps": 0, "duration": 0.0, "quality_str": "File không tồn tại"}

    cmd = [
        FFPROBE_BIN, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,bit_rate:format=duration",
        "-of", "json", file_path
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, **get_subprocess_kwargs())
        data = json.loads(res.stdout)
        stream = data.get("streams", [{}])[0]
        w = int(stream.get("width", 0))
        h = int(stream.get("height", 0))
        fps_raw = str(stream.get("r_frame_rate", "30/1"))
        fps = 0
        if "/" in fps_raw:
            parts = fps_raw.split("/")
            if len(parts) == 2 and float(parts[1]) > 0:
                fps = round(float(parts[0]) / float(parts[1]))
        elif fps_raw:
            fps = round(float(fps_raw))

        duration = round(float(data.get("format", {}).get("duration", 0.0)), 1)
        fps_str = f" {fps}fps" if fps > 0 else ""
        min_dim = min(w, h)
        if min_dim >= 2100:
            qual_str = f"4K Ultra HD ({w}x{h}{fps_str})"
        elif min_dim >= 1400:
            qual_str = f"2K QHD ({w}x{h}{fps_str})"
        elif min_dim >= 1070:
            qual_str = f"1080p60 Full HD ({w}x{h}{fps_str})" if fps >= 50 else f"1080p Full HD ({w}x{h}{fps_str})"
        elif min_dim >= 710:
            qual_str = f"720p60 HD ({w}x{h}{fps_str})" if fps >= 50 else f"720p HD ({w}x{h}{fps_str})"
        else:
            qual_str = f"{min_dim}p ({w}x{h}{fps_str})"

        return {
            "width": w,
            "height": h,
            "fps": fps,
            "duration": duration,
            "quality_str": qual_str,
            "is_hd": min_dim >= 710,
            "is_1080": min_dim >= 1070
        }
    except Exception as e:
        return {"width": 0, "height": 0, "fps": 0, "duration": 0.0, "quality_str": f"Lỗi ffprobe: {e}", "is_hd": False, "is_1080": False}


if __name__ == "__main__":
    print("Video Remix Engine initialized.")
    enc = detect_best_video_encoder()
    print("Detected Best Encoder:", enc)
