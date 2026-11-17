import sys
import os

# Đảm bảo UTF-8 cho stdout/stderr trên Windows
if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr is not None:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import subprocess
import threading
import json
import time
import webview
import requests

from profile_manager import ProfileManager
from browser_runner import parse_proxy
from platform_utils import BASE_DIR, load_config, save_config
from ai_seo_helper import generate_seo_metadata, clean_filename_to_title
import post_manager

class Api:
    def __init__(self):
        self.profile_mgr = ProfileManager()
        self.running_processes = {}  # {profile_id: subprocess.Popen}
        self.logs = []
        self._window = None

    def set_window(self, window):
        self._window = window

    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {msg}"
        self.logs.append(entry)
        if len(self.logs) > 500:
            self.logs.pop(0)
        if self._window:
            try:
                safe_json = json.dumps(entry)
                self._window.evaluate_js(f"window.appendLiveLog && window.appendLiveLog({safe_json})")
            except Exception:
                pass

    def get_live_logs(self):
        return self.logs

    def clear_live_logs(self):
        self.logs.clear()
        return True

    # ================= PROFILE APIS =================
    def get_profiles(self):
        return self.profile_mgr.profiles

    def get_running_status(self):
        dead = [pid for pid, proc in self.running_processes.items() if proc.poll() is not None]
        for pid in dead:
            del self.running_processes[pid]
        return {pid: True for pid in self.running_processes.keys()}

    def create_profile(self, data):
        return self.profile_mgr.create_profile(
            name=data.get("name", ""),
            group=data.get("group", "Mặc định"),
            os_type=data.get("os", "windows"),
            proxy=data.get("proxy", ""),
            start_url=data.get("start_url", "https://browserleaks.com/ip"),
            screen=data.get("screen", "1920x1080"),
            locale=data.get("locale", ""),
            block_images=data.get("block_images", False),
            block_webrtc=data.get("block_webrtc", False),
            humanize=data.get("humanize", True),
            headless=data.get("headless", False),
            disable_coop=data.get("disable_coop", False),
            canvas_noise=data.get("canvas_noise", False),
            disable_ublock=data.get("disable_ublock", False),
            addons=data.get("addons", ""),
            notes=data.get("notes", ""),
            auto_login=data.get("auto_login", False),
            auto_login_type=data.get("auto_login_type", "tiktok"),
            account_str=data.get("account_str", ""),
            proxy_expiry_date=data.get("proxy_expiry_date", "")
        )

    def update_profile_proxy_expiry(self, profile_id, expiry_date, apply_same_ip=False):
        res = self.profile_mgr.update_proxy_expiry(profile_id, expiry_date, apply_same_ip)
        self.log(f"📅 Cập nhật HSD Proxy cho {res.get('updated_count', 1)} profile: {expiry_date}")
        return res

    def test_proxy_connection(self, proxy_str):
        if not proxy_str or not proxy_str.strip():
            return {"ok": False, "msg": "Chưa nhập proxy"}
        from browser_runner import parse_proxy
        cfg = parse_proxy(proxy_str)
        if not cfg:
            return {"ok": False, "msg": "Định dạng proxy không hợp lệ (Dùng IP:Port hoặc IP:Port:User:Pass)"}
        server = cfg["server"]
        u = cfg.get("username", "")
        p = cfg.get("password", "")
        if u and p:
            parts = server.split("://")
            proxy_url = f"{parts[0]}://{u}:{p}@{parts[1]}"
        else:
            proxy_url = server
        import requests
        proxies = {"http": proxy_url, "https": proxy_url}
        t0 = time.time()
        try:
            r = requests.get("https://ipinfo.io/json", proxies=proxies, timeout=8)
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                data = r.json()
                return {
                    "ok": True,
                    "ip": data.get("ip", ""),
                    "country": data.get("country", ""),
                    "city": data.get("city", ""),
                    "org": data.get("org", ""),
                    "ping_ms": ms,
                    "msg": f"Live! IP: {data.get('ip')} ({data.get('country')}) - Ping {ms}ms"
                }
        except Exception:
            pass
        try:
            r = requests.get("http://ip-api.com/json/", proxies=proxies, timeout=8)
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                data = r.json()
                return {
                    "ok": True,
                    "ip": data.get("query", ""),
                    "country": data.get("countryCode", ""),
                    "city": data.get("city", ""),
                    "org": data.get("isp", ""),
                    "ping_ms": ms,
                    "msg": f"Live! IP: {data.get('query')} ({data.get('countryCode')}) - Ping {ms}ms"
                }
        except Exception as e:
            return {"ok": False, "msg": f"Lỗi kết nối proxy: {e}"}
        return {"ok": False, "msg": "Proxy không phản hồi (Timeout)"}


    def parse_account(self, account_str):
        from auto_login import parse_account_string
        return parse_account_string(account_str)

    def test_dongvan_api(self, account_str, app_type="tiktok"):
        from auto_login import parse_account_string, get_code_from_dongvanfb
        creds = parse_account_string(account_str)
        if not creds:
            return {"ok": False, "msg": "Không thể phân tích định dạng tài khoản!"}
        if not creds.get("email") or not creds.get("refresh_token") or not creds.get("client_id"):
            return {"ok": False, "msg": "Thiếu email, refreshToken hoặc clientId!"}
        
        res = get_code_from_dongvanfb(creds["email"], creds["refresh_token"], creds["client_id"], app_type)
        if res.get("status"):
            return {
                "ok": True,
                "code": res.get("code", ""),
                "content": res.get("content", ""),
                "date": res.get("date", ""),
                "from": res.get("from", ""),
                "creds": {
                    "username": creds.get("username", ""),
                    "email": creds.get("email", ""),
                    "client_id": creds.get("client_id", "")
                }
            }
        else:
            return {"ok": False, "msg": res.get("message", "Chưa có thư mới hoặc token hết hạn")}

    def clone_profile(self, profile_id):
        cloned = self.profile_mgr.clone_profile(profile_id)
        if cloned:
            return {"ok": True, "profile": cloned}
        return {"ok": False, "msg": "Không thể nhân bản profile"}

    def update_profile(self, profile_id, data):
        return self.profile_mgr.update_profile(profile_id, **data)

    def delete_profile(self, profile_id):
        was_running = profile_id in self.running_processes
        if was_running:
            self.stop_profile(profile_id)
            time.sleep(0.4)
        return self.profile_mgr.delete_profile(profile_id, delete_data=True)

    def launch_profile(self, profile_id):
        if profile_id in self.running_processes and self.running_processes[profile_id].poll() is None:
            return {"ok": False, "msg": "Profile này đang chạy rồi!"}

        p = self.profile_mgr.get_profile(profile_id)
        if not p:
            return {"ok": False, "msg": "Không tìm thấy profile!"}

        from profile_manager import _resolve_profile_path, sync_autologin_file
        profile_dir = _resolve_profile_path(p["path"])

        # Dọn dẹp file khóa cũ nếu phiên trước bị crash hoặc tắt đột ngột
        for lock_name in [".parentlock", "lock", "parent.lock"]:
            lp = os.path.join(profile_dir, lock_name)
            if os.path.exists(lp):
                try:
                    os.remove(lp)
                except Exception:
                    pass

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--run-profile"]
        else:
            runner_path = os.path.join(BASE_DIR, "browser_runner.py")
            cmd = [sys.executable, runner_path]

        cmd.extend([
            "--profile-dir", profile_dir,
            "--os", p.get("os", "windows"),
            "--url", p.get("start_url", "https://browserleaks.com/ip"),
            "--screen", p.get("screen", "1920x1080"),
        ])
        if p.get("proxy"):
            cmd.extend(["--proxy", p["proxy"]])
        if p.get("locale"):
            cmd.extend(["--locale", p["locale"]])
        if p.get("block_images"):
            cmd.append("--block-images")
        if p.get("block_webrtc"):
            cmd.append("--block-webrtc")
        if not p.get("humanize", True):
            cmd.append("--disable-humanize")
        if p.get("headless"):
            cmd.append("--headless")
        if p.get("disable_coop"):
            cmd.append("--disable-coop")
        if p.get("canvas_noise"):
            cmd.append("--canvas-noise")
        if p.get("disable_ublock"):
            cmd.append("--disable-ublock")
        if p.get("addons"):
            cmd.extend(["--addons", p["addons"]])

        sync_autologin_file(p)
        if p.get("auto_login"):
            cmd.append("--auto-login")
            if p.get("auto_login_type"):
                cmd.extend(["--auto-login-type", p["auto_login_type"]])

        try:
            log_path = "/tmp/camoufox_browser.log" if sys.platform == "darwin" else os.path.join(BASE_DIR, "camoufox_browser.log")
            log_file = open(log_path, "a", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            proc = subprocess.Popen(cmd, cwd=BASE_DIR, stdout=log_file, stderr=log_file, env=env)
            self.running_processes[profile_id] = proc
            return {"ok": True, "msg": "Đã khởi chạy trình duyệt Camoufox thành công"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def stop_profile(self, profile_id):
        if profile_id in self.running_processes:
            proc = self.running_processes[profile_id]
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    )
                else:
                    proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            del self.running_processes[profile_id]
        return {"ok": True}

    def check_proxy(self, proxy_str):
        cfg = parse_proxy(proxy_str)
        if not cfg:
            return {"ok": False, "msg": "Định dạng proxy không hợp lệ"}

        server = cfg["server"]
        user = cfg.get("username")
        pwd = cfg.get("password")
        scheme = server.split("://")[0]
        host_port = server.split("://")[1]

        if user and pwd:
            proxy_url = f"{scheme}://{user}:{pwd}@{host_port}"
        else:
            proxy_url = f"{scheme}://{host_port}"

        proxies = {"http": proxy_url, "https": proxy_url}
        try:
            r = requests.get("https://ipinfo.io/json", proxies=proxies, timeout=8)
            if r.status_code == 200:
                data = r.json()
                return {"ok": True, "msg": f"IP: {data.get('ip')} ({data.get('city', '')}, {data.get('country', '')})"}
            return {"ok": False, "msg": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"ok": False, "msg": str(e)[:35]}

    def open_profiles_folder(self):
        folder = os.path.abspath(self.profile_mgr.profiles_dir)
        os.makedirs(folder, exist_ok=True)
        if sys.platform == "darwin":
            subprocess.run(["open", folder])
        elif sys.platform == "win32":
            os.startfile(folder)
        else:
            subprocess.run(["xdg-open", folder])
        return True

    # ================= AUTO POST & AI SEO APIS =================
    def select_video_file(self):
        """Mở hộp thoại chọn file video hệ thống thông qua pywebview."""
        if not self._window:
            return {"ok": False, "msg": "Cửa sổ chưa khởi tạo"}
        try:
            file_types = ('Video Files (*.mp4;*.mov;*.mkv;*.webm;*.avi;*.m4v)', 'All files (*.*)')
            chosen = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=file_types
            )
            if chosen and len(chosen) > 0:
                file_path = chosen[0]
                size_mb = round(os.path.getsize(file_path) / (1024 * 1024), 2)
                suggested_title = clean_filename_to_title(file_path)
                return {
                    "ok": True,
                    "file_path": file_path,
                    "size_mb": size_mb,
                    "suggested_title": suggested_title
                }
            return {"ok": False, "msg": "Người dùng đã hủy chọn file"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def generate_ai_seo(self, topic, target_region="VN", niche="", notes=""):
        """Tạo bộ SEO (Title, Hook, Description, Hashtags) bằng Gemini AI."""
        try:
            self.log(f"🤖 Đang gọi Gemini AI tạo nội dung SEO cho: '{topic}' (Vùng: {target_region})...")
            res = generate_seo_metadata(
                topic_or_filename=topic,
                target_region=target_region,
                niche_hint=niche,
                custom_notes=notes
            )
            self.log(f"✅ Gemini AI đã tạo xong SEO: '{res.get('title')}'")
            return {"ok": True, "data": res}
        except Exception as e:
            self.log(f"❌ Lỗi khi sinh nội dung SEO: {e}")
            return {"ok": False, "msg": str(e)}

    def publish_local_video(self, data):
        """Khởi chạy tiến trình render và đăng video cục bộ trong một luồng độc lập."""
        video_path = data.get("video_path", "").strip()
        if not video_path or not os.path.exists(video_path):
            return {"ok": False, "msg": "File video không tồn tại!"}

        profile_id = data.get("profile_id", "").strip()
        profile_data = self.profile_mgr.get_profile(profile_id)
        if not profile_data:
            return {"ok": False, "msg": "Không tìm thấy thông tin Profile đã chọn!"}

        title = data.get("title", "")
        description = data.get("description", "")
        hashtags = data.get("hashtags", "")
        vpn_region = data.get("vpn_region", "VN")
        post_to_tiktok = data.get("post_to_tiktok", True)
        post_to_youtube = data.get("post_to_youtube", True)
        apply_4k = data.get("apply_4k_upscale", True)

        self.log(f"🚀 [Bắt đầu]: Đăng video '{os.path.basename(video_path)}' qua Profile [{profile_data.get('name')}]...")

        def _worker():
            try:
                res = post_manager.process_and_upload_local_video(
                    video_path=video_path,
                    title=title,
                    description=description,
                    hashtags=hashtags,
                    profile_data=profile_data,
                    vpn_region=vpn_region,
                    post_to_tiktok=post_to_tiktok,
                    post_to_youtube=post_to_youtube,
                    apply_4k_upscale=apply_4k,
                    log_callback=self.log
                )
                self.log(f"🎉 Hoàn tất chu trình đăng bài! TikTok: {res.get('tiktok_status')} | YouTube: {res.get('youtube_status')}")
                if self._window:
                    self._window.evaluate_js(f"window.onPostTaskComplete && window.onPostTaskComplete({json.dumps(res)})")
            except Exception as e:
                self.log(f"❌ Xử lý bài đăng thất bại: {e}")
                if self._window:
                    self._window.evaluate_js(f"window.onPostTaskError && window.onPostTaskError({json.dumps(str(e))})")

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True, "msg": "Đã bắt đầu xử lý và đăng video ở nền ngầm!"}

    def get_post_history(self):
        """Lấy danh sách lịch sử bài đăng từ SQLite."""
        try:
            return post_manager.get_all_videos()
        except Exception as e:
            return []

    def delete_post_record(self, video_id, also_delete_tiktok=False):
        """Xóa bản ghi lịch sử bài đăng (và xóa trên TikTok nếu yêu cầu)."""
        rec = post_manager.get_video_record(video_id)
        if not rec:
            return {"ok": False, "msg": "Không tìm thấy video"}

        def _task():
            if also_delete_tiktok:
                post_manager.delete_tiktok_video_and_file(rec, log_callback=self.log)
            else:
                post_manager.delete_video_record(video_id)
            self.log(f"🗑️ Đã xóa bản ghi video ID {video_id}")
            if self._window:
                self._window.evaluate_js("window.loadPostHistory && window.loadPostHistory()")

        threading.Thread(target=_task, daemon=True).start()
        return {"ok": True, "msg": "Đang thực hiện xóa..."}

    def force_publish_draft_post(self, video_id):
        """Mở Camoufox và phát hành bản nháp video trên TikTok."""
        rec = post_manager.get_video_record(video_id)
        if not rec:
            return {"ok": False, "msg": "Không tìm thấy video"}

        def _task():
            self.log(f"⏳ Đang phát hành bản nháp video: {rec.get('shorts_title')}...")
            ok = post_manager.force_publish_draft(rec, log_callback=self.log)
            if ok:
                self.log("✅ Đã phát hành bản nháp thành công!")
            else:
                self.log("❌ Không thể phát hành bản nháp!")
            if self._window:
                self._window.evaluate_js("window.loadPostHistory && window.loadPostHistory()")

        threading.Thread(target=_task, daemon=True).start()
        return {"ok": True, "msg": "Đang tiến hành mở Camoufox để xuất bản bản nháp..."}

    def open_warmup_browser(self, profile_id, target_url="https://www.tiktok.com/tiktokstudio/upload"):
        """Mở Camoufox để người dùng kiểm tra hoặc đăng nhập thủ công."""
        if profile_id in self.running_processes and self.running_processes[profile_id].poll() is None:
            return {"ok": False, "msg": "Profile này đang chạy rồi! Hãy đóng cửa sổ trình duyệt hiện tại trước."}

        p = self.profile_mgr.get_profile(profile_id)
        if not p:
            return {"ok": False, "msg": "Không tìm thấy Profile!"}

        profile_dir = os.path.abspath(os.path.join(BASE_DIR, p["path"]))
        proxy = p.get("proxy", "")
        os_type = p.get("os", "windows")

        def _task():
            self.log(f"🔑 Mở Camoufox Warm-up cho Profile [{p.get('name')}]...")
            post_manager.open_profile_warmup(profile_dir, proxy, target_url=target_url, os_type=os_type)
            self.log(f"🔑 Đã đóng Camoufox Warm-up [{p.get('name')}].")

        threading.Thread(target=_task, daemon=True).start()
        return {"ok": True, "msg": "Đã mở trình duyệt Camoufox để thao tác thủ công."}

    # ================= TRACKED CHANNELS & TIKTOK BETA APIS =================
    def get_tracked_channels(self):
        return post_manager.get_all_channels()

    def get_channels_kpi_summary(self, target_date=None):
        return post_manager.get_all_channels_kpi_summary(target_date)

    def add_channel_track(self, data):
        try:
            post_manager.add_tracked_channel(
                yt_url=data.get("youtube_shorts_url", "").strip(),
                tiktok_user=data.get("tiktok_user", "").strip(),
                vpn_region=data.get("vpn_region", "VN").strip(),
                profile_name=data.get("profile_name", "").strip(),
                profile_dir=data.get("profile_dir", "").strip(),
                proxy_server=data.get("proxy_server", "").strip(),
                niche=data.get("niche", "").strip(),
                post_to_tiktok=1 if data.get("post_to_tiktok", True) else 0,
                post_to_youtube=1 if data.get("post_to_youtube", True) else 0,
                account_id=data.get("account_id", "").strip(),
                source_handle=data.get("source_handle", "").strip(),
                daily_target=int(data.get("daily_target", 2)),
                status=data.get("status", "ACTIVE"),
                title_mode=data.get("title_mode", "ai_rewrite")
            )
            self.log(f"➕ Đã thêm kênh TikTok Beta: {data.get('youtube_shorts_url')}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def toggle_channel_status(self, channel_id, new_status):
        try:
            post_manager.update_channel_status(int(channel_id), new_status)
            self.log(f"🔄 Đã chuyển trạng thái kênh #{channel_id} sang: {new_status}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def toggle_channel_title_mode(self, channel_id, new_mode):
        try:
            norm = "keep_original" if str(new_mode).strip().lower() == "keep_original" else "ai_rewrite"
            post_manager.update_channel_details(int(channel_id), {"title_mode": norm})
            label = "Giữ tiêu đề gốc" if norm == "keep_original" else "AI viết lại"
            self.log(f"📝 Kênh #{channel_id} chuyển chế độ tiêu đề sang: {label}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def update_channel_target(self, channel_id, target):
        try:
            post_manager.update_channel_target(int(channel_id), int(target))
            self.log(f"🎯 Đã cập nhật chỉ tiêu kênh #{channel_id} thành: {target} video/ngày")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def update_channel_details(self, channel_id, data):
        try:
            post_manager.update_channel_details(int(channel_id), data)
            self.log(f"📝 Đã cập nhật thông tin kênh #{channel_id}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def run_channel_now(self, channel_id):
        def _task():
            self.log(f"🚀 Kích hoạt chạy ngay cho kênh #{channel_id}...")
            res = post_manager.run_single_channel_download_and_post(int(channel_id), log_callback=self.log)
            if res.get("success"):
                self.log(f"✅ Kênh #{channel_id} hoàn tất lượt đăng video!")
            else:
                self.log(f"❌ Kênh #{channel_id} chạy thất bại: {res.get('error')}")

        threading.Thread(target=_task, daemon=True).start()
        return {"ok": True, "msg": "Đang xử lý quét và đăng video..."}

    def execute_compensation(self, mode="bu1"):
        post_manager.execute_compensation(mode=mode, log_callback=self.log)
        self.log(f"⚡️ Đã kích hoạt tiến trình đăng bù (Chế độ: {mode})!")
        return {"ok": True, "msg": f"Đã bắt đầu đăng bù ({mode})"}

    def trigger_kpi_report(self):
        def _task():
            self.log("📊 Đang tổng hợp và gửi Báo cáo Chỉ tiêu (KPI) về Telegram...")
            try:
                from telegram_controller import send_daily_kpi_report
                send_daily_kpi_report()
                self.log("✅ Đã gửi Báo cáo Chỉ tiêu (KPI) về Telegram thành công!")
            except Exception as e:
                self.log(f"❌ Lỗi gửi báo cáo KPI: {e}")

        threading.Thread(target=_task, daemon=True).start()
        return {"ok": True, "msg": "Đang gửi báo cáo KPI về Telegram..."}

    def trigger_video_audit(self):
        def _task():
            self.log("🛡 Đang kích hoạt Kiểm định Video qua Bot TikTok Check Video...")
            try:
                from telegram_controller import send_daily_audit_report
                send_daily_audit_report()
                self.log("✅ Đã gửi Báo cáo Kiểm định Video về Telegram thành công!")
            except Exception as e:
                self.log(f"❌ Lỗi gửi báo cáo kiểm định: {e}")

        threading.Thread(target=_task, daemon=True).start()
        return {"ok": True, "msg": "Đang gửi báo cáo kiểm định về Telegram..."}

    def delete_channel_track(self, channel_id):
        try:
            post_manager.delete_tracked_channel(int(channel_id))
            self.log(f"🗑️ Đã xóa kênh theo dõi ID: {channel_id}")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def get_scanner_status(self):
        return {"running": post_manager.is_scanner_running()}

    def toggle_auto_scanner(self, enable):
        if enable:
            post_manager.start_auto_scanner(log_callback=self.log)
            try:
                from telegram_controller import start_telegram_polling_thread
                start_telegram_polling_thread()
            except Exception:
                pass
            self.log("▶️ Đã bật trình quét kênh tự động.")
        else:
            post_manager.stop_auto_scanner()
            self.log("⏹️ Đã dừng trình quét kênh tự động.")
        return {"running": post_manager.is_scanner_running()}

    # ================= SETTINGS & SYSTEM APIS =================
    def get_system_settings(self):
        return load_config()

    def save_system_settings(self, cfg):
        try:
            save_config(cfg)
            self.log("💾 Đã lưu cấu hình hệ thống thành công.")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def test_telegram_connection(self):
        cfg = load_config()
        token = cfg.get("telegram_bot_token", "").strip()
        chat_id = cfg.get("telegram_chat_id", "").strip()
        if not token or not chat_id:
            return {"ok": False, "msg": "Chưa cấu hình Telegram Bot Token hoặc Chat ID!"}
        try:
            import telebot
            b = telebot.TeleBot(token, parse_mode="HTML")
            b.send_message(
                chat_id,
                "🤖 <b>[Camoufox Antidetect & Auto Post]</b> Kết nối Telegram thành công! Hệ thống sẵn sàng nhận báo cáo."
            )
            self.log("✅ Đã gửi tin nhắn thử nghiệm thành công tới Telegram!")
            return {"ok": True, "msg": "Đã gửi tin nhắn thử nghiệm thành công tới Telegram!"}
        except Exception as e:
            self.log(f"❌ Lỗi gửi Telegram: {e}")
            return {"ok": False, "msg": str(e)}

    def test_gemini_connection(self):
        cfg = load_config()
        api_key = cfg.get("gemini_api_key", "").strip()
        if not api_key:
            return {"ok": False, "msg": "Chưa nhập Gemini API Key!"}
        try:
            from google import genai
            from ai_seo_helper import AVAILABLE_GEMINI_MODELS
            client = genai.Client(api_key=api_key)
            last_err = None
            for m in AVAILABLE_GEMINI_MODELS:
                try:
                    response = client.models.generate_content(
                        model=m,
                        contents="Ping! Trả lời đúng 1 từ: OK",
                    )
                    return {"ok": True, "msg": f"Kết nối Gemini AI thành công! (Đang dùng: {m})"}
                except Exception as e:
                    last_err = e
                    continue
            return {"ok": False, "msg": str(last_err)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

def main():
    api = Api()
    # Tự động khởi động Telegram bot polling nếu có cấu hình
    try:
        from telegram_controller import start_telegram_polling_thread
        start_telegram_polling_thread()
    except Exception:
        pass
    resource_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    ui_path = os.path.join(resource_dir, "ui", "index.html")
    if not os.path.exists(ui_path):
        ui_path = os.path.join(BASE_DIR, "ui", "index.html")

    window = webview.create_window(
        title="Camoufox Antidetect & Auto Studio",
        url=ui_path,
        js_api=api,
        width=1200,
        height=800,
        min_size=(1020, 680),
        background_color="#090D16"
    )
    api.set_window(window)
    webview.start(debug=False)

if __name__ == "__main__":
    if "--run-profile" in sys.argv:
        sys.argv.remove("--run-profile")
        from browser_runner import main as runner_main
        runner_main()
    else:
        main()
