import os
import sys
import stat
import json
import uuid
import time
import ctypes
import shutil
import subprocess
from datetime import datetime, date

from platform_utils import BASE_DIR, get_subprocess_kwargs

PROFILES_FILE = os.path.join(BASE_DIR, "profiles.json")
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")


def parse_proxy_host(proxy_str: str) -> str:
    """Trích xuất địa chỉ IP / Host từ chuỗi proxy để phân nhóm hoặc đồng bộ hạn dùng."""
    if not proxy_str or not str(proxy_str).strip():
        return ""
    raw = str(proxy_str).strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    if "@" in raw:
        raw = raw.split("@", 1)[1]
    tokens = raw.split(":")
    return tokens[0].strip() if tokens else ""


def parse_proxy_expiry_date(date_str: str) -> date | None:
    """Phân tích chuỗi ngày hết hạn proxy hỗ trợ nhiều định dạng (YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY)."""
    if not date_str or not str(date_str).strip():
        return None
    cleaned = str(date_str).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            pass
    return None


def get_proxy_expiry_info(date_str: str) -> dict:
    """
    Tính toán chi tiết hạn sử dụng Proxy:
    - days_left <= 0: HẾT HẠN (ĐỎ)
    - days_left == 1: CẢNH BÁO SẮP HẾT HẠN (CAM/ĐỎ)
    - days_left > 1: CÒN HẠN (XÁM DỊU)
    """
    p_date = parse_proxy_expiry_date(date_str)
    if not p_date:
        return {
            "has_expiry": False,
            "expiry_date": "",
            "display_date": "",
            "days_left": None,
            "status": "none",
            "badge_text": "📅 + HSD",
            "badge_class": "hsd-none"
        }

    today = date.today()
    days_left = (p_date - today).days
    fmt_date = p_date.strftime("%d/%m/%Y")

    if days_left < 0:
        return {
            "has_expiry": True,
            "expiry_date": p_date.strftime("%Y-%m-%d"),
            "display_date": fmt_date,
            "days_left": days_left,
            "status": "expired",
            "badge_text": f"⛔ Quá hạn {abs(days_left)} ngày ({fmt_date})",
            "badge_class": "hsd-expired"
        }
    elif days_left == 0:
        return {
            "has_expiry": True,
            "expiry_date": p_date.strftime("%Y-%m-%d"),
            "display_date": fmt_date,
            "days_left": 0,
            "status": "today",
            "badge_text": f"⛔ HẾT HẠN HÔM NAY ({fmt_date})",
            "badge_class": "hsd-today"
        }
    elif days_left == 1:
        return {
            "has_expiry": True,
            "expiry_date": p_date.strftime("%Y-%m-%d"),
            "display_date": fmt_date,
            "days_left": 1,
            "status": "warning",
            "badge_text": f"⚠️ Còn 1 ngày ({fmt_date})",
            "badge_class": "hsd-warning"
        }
    else:
        return {
            "has_expiry": True,
            "expiry_date": p_date.strftime("%Y-%m-%d"),
            "display_date": fmt_date,
            "days_left": days_left,
            "status": "ok",
            "badge_text": f"📅 HSD: {fmt_date} (còn {days_left} ngày)",
            "badge_class": "hsd-ok"
        }



def _resolve_profile_path(p_path: str) -> str:
    if not p_path:
        return ""
    if os.path.isabs(p_path):
        return os.path.abspath(p_path)
    return os.path.abspath(os.path.join(BASE_DIR, p_path))


def _clear_readonly(path: str):
    """Gỡ bỏ thuộc tính Read-Only/Hidden/System trên Windows và cấp quyền ghi."""
    try:
        if sys.platform == "win32":
            # 0x80 = FILE_ATTRIBUTE_NORMAL
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
    except Exception:
        pass
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except Exception:
        pass


def _kill_orphaned_browsers_for_profile(profile_path: str):
    """Chỉ tắt tiến trình firefox/camoufox bị treo đang giữ khóa chính xác thư mục profile này."""
    if sys.platform != "win32":
        return
    folder_name = os.path.basename(os.path.normpath(profile_path))
    if not folder_name or folder_name in ("profiles", "antidetech"):
        return
    try:
        # Dùng PowerShell lọc chính xác Process đang truyền --profile hoặc user_data_dir chứa folder_name
        ps_cmd = (
            f"Get-CimInstance Win32_Process -Filter \"Name='firefox.exe' OR Name='camoufox.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{folder_name}*' }} | "
            f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            timeout=5,
            **get_subprocess_kwargs()
        )
    except Exception:
        pass


def force_remove_tree(target_path: str, retries: int = 4) -> tuple[bool, str | None]:
    """
    Xóa triệt để thư mục profile trên Windows/macOS:
    - Tự động gỡ thuộc tính Read-Only (thường gặp ở cache2\\doomed của Firefox/Camoufox)
    - Thử lại nếu OneDrive / Antivirus / Browser vừa đóng đang giữ khóa tạm thời
    """
    resolved = _resolve_profile_path(target_path)
    if not resolved or not os.path.exists(resolved):
        return True, None

    # Chốt an toàn: tuyệt đối không xóa nhầm BASE_DIR hoặc PROFILES_DIR gốc
    norm_resolved = os.path.normcase(os.path.abspath(resolved))
    if norm_resolved in (
        os.path.normcase(os.path.abspath(BASE_DIR)),
        os.path.normcase(os.path.abspath(PROFILES_DIR)),
    ):
        return False, "Từ chối xóa thư mục gốc hệ thống."

    def _on_rm_error(func, path, exc_info=None):
        _clear_readonly(path)
        func(path)

    last_err = None
    for attempt in range(retries):
        try:
            # Gỡ Read-Only trước cho toàn bộ cây thư mục con
            if sys.platform == "win32":
                try:
                    for root, dirs, files in os.walk(resolved, topdown=False):
                        for name in files:
                            _clear_readonly(os.path.join(root, name))
                        for name in dirs:
                            _clear_readonly(os.path.join(root, name))
                    _clear_readonly(resolved)
                except Exception:
                    pass

            if sys.version_info >= (3, 12):
                shutil.rmtree(resolved, onexc=_on_rm_error)
            else:
                shutil.rmtree(resolved, onerror=_on_rm_error)

            if not os.path.exists(resolved):
                return True, None
        except Exception as e:
            last_err = e

        # Fallback trên Windows bằng attrib + rmdir /s /q
        if sys.platform == "win32" and os.path.exists(resolved):
            try:
                subprocess.run(
                    ["cmd", "/c", "attrib", "-R", "-S", "-H", f"{resolved}\\*.*", "/S", "/D"],
                    capture_output=True,
                    timeout=8,
                    **get_subprocess_kwargs()
                )
                subprocess.run(
                    ["cmd", "/c", "rmdir", "/S", "/Q", resolved],
                    capture_output=True,
                    timeout=8,
                    **get_subprocess_kwargs()
                )
                if not os.path.exists(resolved):
                    return True, None
            except Exception as e2:
                last_err = e2

        # Nếu vẫn còn kẹt tiến trình trình duyệt cũ giữ khóa, giải phóng và thử lại
        if attempt == 0:
            _kill_orphaned_browsers_for_profile(resolved)
        time.sleep(0.4 * (attempt + 1))

    return (not os.path.exists(resolved)), str(last_err) if last_err else None


def sync_autologin_file(profile_data):
    try:
        p_path = _resolve_profile_path(profile_data.get("path", ""))
        if not p_path or not os.path.exists(p_path):
            return
        auto_login = profile_data.get("auto_login", False)
        auto_login_type = profile_data.get("auto_login_type", "tiktok")
        account_str = profile_data.get("account_str", "") or profile_data.get("notes", "")

        from auto_login import parse_account_string
        creds = parse_account_string(account_str)

        cfg_path = os.path.join(p_path, "autologin.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({
                "enabled": bool(auto_login),
                "type": auto_login_type,
                "account_str": account_str,
                "creds": creds
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Lỗi khi đồng bộ autologin.json: {e}")


class ProfileManager:
    def __init__(self, profiles_file=PROFILES_FILE, profiles_dir=PROFILES_DIR):
        self.profiles_file = _resolve_profile_path(profiles_file)
        self.profiles_dir = _resolve_profile_path(profiles_dir)
        os.makedirs(self.profiles_dir, exist_ok=True)
        self.profiles = self.load_profiles()

    def load_profiles(self):
        if not os.path.exists(self.profiles_file):
            return []
        try:
            with open(self.profiles_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Tự động đồng bộ và nâng cấp trường auto-login, proxy expiry cho profile cũ
                for p in data:
                    if "group" not in p or not p.get("group"):
                        p["group"] = "Mặc định"
                    if "auto_login" not in p:
                        p["auto_login"] = False
                    if "auto_login_type" not in p:
                        p["auto_login_type"] = "tiktok"
                    if "account_str" not in p:
                        p["account_str"] = p.get("notes", "") if "|" in p.get("notes", "") else ""
                    if "proxy_expiry_date" not in p:
                        p["proxy_expiry_date"] = ""
                    p["proxy_expiry_info"] = get_proxy_expiry_info(p.get("proxy_expiry_date", ""))
                    sync_autologin_file(p)
                return data
        except Exception:
            return []

    def save_profiles(self):
        # Tránh lưu trường động proxy_expiry_info vào JSON nếu muốn file gọn gàng
        clean_list = []
        for p in self.profiles:
            cp = dict(p)
            cp.pop("proxy_expiry_info", None)
            clean_list.append(cp)
        with open(self.profiles_file, "w", encoding="utf-8") as f:
            json.dump(clean_list, f, ensure_ascii=False, indent=2)

    def create_profile(
        self,
        name,
        group="Mặc định",
        os_type="windows",
        proxy="",
        start_url="https://browserleaks.com/ip",
        screen="1920x1080",
        locale="",
        block_images=False,
        block_webrtc=False,
        humanize=True,
        headless=False,
        disable_coop=False,
        canvas_noise=False,
        disable_ublock=False,
        addons="",
        notes="",
        auto_login=False,
        auto_login_type="tiktok",
        account_str="",
        proxy_expiry_date=""
    ):
        profile_id = str(uuid.uuid4())[:8]
        profile_dir_abs = os.path.join(self.profiles_dir, profile_id)
        os.makedirs(profile_dir_abs, exist_ok=True)
        try:
            rel_path = os.path.relpath(profile_dir_abs, BASE_DIR)
        except ValueError:
            rel_path = profile_dir_abs

        profile_data = {
            "id": profile_id,
            "name": name.strip() or f"Profile {profile_id}",
            "group": group.strip() or "Mặc định",
            "os": os_type,
            "proxy": proxy.strip(),
            "proxy_expiry_date": proxy_expiry_date.strip(),
            "start_url": start_url.strip() or "https://browserleaks.com/ip",
            "screen": screen.strip() or "1920x1080",
            "locale": locale.strip(),
            "block_images": bool(block_images),
            "block_webrtc": bool(block_webrtc),
            "humanize": bool(humanize),
            "headless": bool(headless),
            "disable_coop": bool(disable_coop),
            "canvas_noise": bool(canvas_noise),
            "disable_ublock": bool(disable_ublock),
            "addons": addons.strip(),
            "notes": notes.strip(),
            "auto_login": bool(auto_login),
            "auto_login_type": auto_login_type.strip() if auto_login_type else "tiktok",
            "account_str": account_str.strip() or (notes.strip() if "|" in notes else ""),
            "path": rel_path,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        profile_data["proxy_expiry_info"] = get_proxy_expiry_info(profile_data["proxy_expiry_date"])
        self.profiles.append(profile_data)
        self.save_profiles()
        sync_autologin_file(profile_data)
        return profile_data

    def clone_profile(self, profile_id):
        orig = self.get_profile(profile_id)
        if not orig:
            return None

        new_id = str(uuid.uuid4())[:8]
        new_dir_abs = os.path.join(self.profiles_dir, new_id)
        orig_dir_abs = _resolve_profile_path(orig.get("path", ""))

        # Sao chép thư mục dữ liệu cookie nếu có (bỏ qua file lock & cache2 dễ lỗi)
        if orig_dir_abs and os.path.exists(orig_dir_abs):
            try:
                shutil.copytree(
                    orig_dir_abs,
                    new_dir_abs,
                    ignore=shutil.ignore_patterns(
                        "parent.lock", ".parentlock", "lock",
                        "cache2", "Cache", "startupCache", "minidumps"
                    )
                )
            except Exception:
                os.makedirs(new_dir_abs, exist_ok=True)
        else:
            os.makedirs(new_dir_abs, exist_ok=True)

        try:
            rel_path = os.path.relpath(new_dir_abs, BASE_DIR)
        except ValueError:
            rel_path = new_dir_abs

        cloned = dict(orig)
        cloned["id"] = new_id
        cloned["name"] = f"{orig.get('name', 'Profile')} (Bản sao)"
        cloned["path"] = rel_path
        cloned["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        cloned["proxy_expiry_info"] = get_proxy_expiry_info(cloned.get("proxy_expiry_date", ""))

        self.profiles.append(cloned)
        self.save_profiles()
        sync_autologin_file(cloned)
        return cloned

    def update_profile(self, profile_id, **kwargs):
        for p in self.profiles:
            if p["id"] == profile_id:
                for k, v in kwargs.items():
                    p[k] = v
                p["proxy_expiry_info"] = get_proxy_expiry_info(p.get("proxy_expiry_date", ""))
                self.save_profiles()
                sync_autologin_file(p)
                return p
        return None

    def update_proxy_expiry(self, profile_id: str, expiry_date: str, apply_same_ip: bool = False) -> dict:
        """Cập nhật ngày hết hạn proxy cho profile chỉ định hoặc toàn bộ các profile có cùng IP host."""
        target_p = self.get_profile(profile_id)
        if not target_p:
            return {"success": False, "error": "Profile không tồn tại", "updated_count": 0}

        cleaned_date = expiry_date.strip()
        parsed = parse_proxy_expiry_date(cleaned_date)
        stored_date = parsed.strftime("%Y-%m-%d") if parsed else cleaned_date
        target_host = parse_proxy_host(target_p.get("proxy", ""))

        updated_count = 0
        for p in self.profiles:
            match = False
            if p["id"] == profile_id:
                match = True
            elif apply_same_ip and target_host and parse_proxy_host(p.get("proxy", "")) == target_host:
                match = True

            if match:
                p["proxy_expiry_date"] = stored_date
                p["proxy_expiry_info"] = get_proxy_expiry_info(stored_date)
                updated_count += 1

        self.save_profiles()
        return {
            "success": True,
            "updated_count": updated_count,
            "expiry_date": stored_date,
            "expiry_info": get_proxy_expiry_info(stored_date)
        }


    def delete_profile(self, profile_id, delete_data=True):
        for i, p in enumerate(self.profiles):
            if p["id"] == profile_id:
                if delete_data and p.get("path"):
                    ok, err = force_remove_tree(p["path"])
                    if not ok and err:
                        print(f"Lỗi khi xóa thư mục {p['path']}: {err}")
                del self.profiles[i]
                self.save_profiles()
                return True
        return False

    def get_profile(self, profile_id):
        for p in self.profiles:
            if p["id"] == profile_id:
                return p
        return None
