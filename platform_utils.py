import os
import sys
import json
import shutil
import platform
import subprocess

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

def get_base_dir() -> str:
    """
    Lấy thư mục gốc thực tế nơi đặt file chạy (.exe trên Win hoặc .app trên Mac)
    để lưu trữ bền vững config.json, SQLite DB, và thư mục profiles/.
    """
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if IS_MAC and ".app/Contents/MacOS" in exe_dir:
            return os.path.abspath(os.path.join(exe_dir, "../../.."))
        return exe_dir
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()

# Đảm bảo các thư mục dữ liệu luôn tồn tại
for folder in ["profiles", "safe_bgm", "downloads", "bin"]:
    os.makedirs(os.path.join(BASE_DIR, folder), exist_ok=True)

def get_binary_path(bin_name: str) -> str:
    """Tìm ffmpeg/ffprobe trong thư mục ./bin trước, nếu không có mới tìm trong PATH hệ thống hoặc Homebrew."""
    ext = ".exe" if IS_WIN else ""
    local_bin = os.path.join(BASE_DIR, "bin", f"{bin_name}{ext}")
    if os.path.exists(local_bin) and os.access(local_bin, os.X_OK):
        return local_bin

    sys_bin = shutil.which(bin_name)
    if sys_bin:
        return sys_bin

    # Kiểm tra các đường dẫn quen thuộc trên macOS
    if IS_MAC:
        for common_mac_dir in ["/opt/homebrew/bin", "/usr/local/bin"]:
            candidate = os.path.join(common_mac_dir, f"{bin_name}{ext}")
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate

    return local_bin if os.path.exists(local_bin) else f"{bin_name}{ext}"

FFMPEG_BIN = get_binary_path("ffmpeg")
FFPROBE_BIN = get_binary_path("ffprobe")

# Phím tắt chọn tất cả văn bản theo hệ điều hành
SELECT_ALL_SHORTCUT = "Meta+a" if IS_MAC else "Control+a"

def get_subprocess_kwargs() -> dict:
    """Chặn cửa sổ CMD đen nhấp nháy khi gọi FFmpeg/FFprobe trên Windows."""
    kwargs = {}
    if IS_WIN and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs

def detect_best_video_encoder() -> list[str]:
    """
    Tự động chọn phần cứng tăng tốc render 4K:
    - macOS: Apple Silicon / Intel VideoToolbox (h264_videotoolbox)
    - Windows: NVIDIA NVENC (h264_nvenc) nếu có card rời, ngược lại dùng CPU (libx264)
    """
    try:
        res = subprocess.run(
            [FFMPEG_BIN, "-encoders"],
            capture_output=True,
            text=True,
            **get_subprocess_kwargs()
        )
        encoders_out = res.stdout

        if IS_MAC and "h264_videotoolbox" in encoders_out:
            return ["-c:v", "h264_videotoolbox", "-b:v", "18M"]

        if IS_WIN and "h264_nvenc" in encoders_out:
            test_nvenc = subprocess.run(
                [
                    FFMPEG_BIN, "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                    "-c:v", "h264_nvenc", "-f", "null", "-"
                ],
                capture_output=True,
                **get_subprocess_kwargs()
            )
            if test_nvenc.returncode == 0:
                return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20"]
    except Exception:
        pass

    return ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_CAFFEINATE_PROC = None

def set_keep_awake(enable: bool = True):
    """
    Chống hệ điều hành rơi vào trạng thái Sleep/Standby khi chạy lịch đăng ngầm.
    - macOS: Chạy ngầm 'caffeinate' giữ máy thức.
    - Windows: Gọi API kernel32 SetThreadExecutionState.
    """
    global _CAFFEINATE_PROC
    try:
        if IS_WIN:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_AWAYMODE_REQUIRED = 0x00000040
            if enable:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        elif IS_MAC:
            if enable:
                if _CAFFEINATE_PROC is None or _CAFFEINATE_PROC.poll() is not None:
                    _CAFFEINATE_PROC = subprocess.Popen(
                        ["caffeinate", "-d", "-i", "-m", "-u"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
            else:
                if _CAFFEINATE_PROC and _CAFFEINATE_PROC.poll() is None:
                    _CAFFEINATE_PROC.terminate()
                    _CAFFEINATE_PROC = None
        return enable
    except Exception:
        return False

def load_config() -> dict:
    default_cfg = {
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "gemini_api_key": "",
        "scan_interval_seconds": 600
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                default_cfg.update(json.load(f))
        except Exception:
            pass
    return default_cfg

def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

