import sys
import os

# Đảm bảo UTF-8 cho stdout/stderr trên Windows
if sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass
if sys.stderr is not None:
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

import argparse
import time
import threading
from urllib.parse import urlparse
from camoufox.sync_api import Camoufox
from camoufox.fingerprints import Screen
from camoufox.addons import DefaultAddons
from proxy_bridge import parse_proxy, setup_camoufox_proxy

def main():
    parser = argparse.ArgumentParser(description="Camoufox Standalone Profile Runner")
    parser.add_argument("--profile-dir", required=True, help="Thư mục lưu profile")
    parser.add_argument("--os", default="windows", choices=["windows", "macos", "linux"], help="Hệ điều hành giả lập")
    parser.add_argument("--proxy", default="", help="Chuỗi proxy")
    parser.add_argument("--url", default="https://browserleaks.com/ip", help="Trang web mở ban đầu")
    parser.add_argument("--screen", default="1920x1080", help="Độ phân giải màn hình")
    parser.add_argument("--locale", default="", help="Ngôn ngữ trình duyệt")
    parser.add_argument("--block-images", action="store_true", help="Chặn tải hình ảnh")
    parser.add_argument("--block-webrtc", action="store_true", help="Vô hiệu hóa WebRTC")
    parser.add_argument("--disable-humanize", action="store_true", help="Tắt giả lập rê chuột")
    parser.add_argument("--headless", action="store_true", help="Chạy ở chế độ ẩn ngầm")
    parser.add_argument("--disable-coop", action="store_true", help="Tắt COOP để tự động click Turnstile iframe")
    parser.add_argument("--canvas-noise", action="store_true", help="Bật chống vân tay Canvas")
    parser.add_argument("--disable-ublock", action="store_true", help="Tắt tiện ích uBlock Origin")
    parser.add_argument("--addons", default="", help="Đường dẫn thư mục tiện ích mở rộng Firefox")
    parser.add_argument("--auto-login", action="store_true", help="Kích hoạt tự động đăng nhập")
    parser.add_argument("--auto-login-type", default="tiktok", help="Loại nền tảng tự động đăng nhập (tiktok)")

    args = parser.parse_args()

    profile_dir = os.path.abspath(args.profile_dir)
    os.makedirs(profile_dir, exist_ok=True)

    # Tự động dọn dẹp file khóa cũ (.parentlock) nếu phiên trước bị tắt đột ngột
    for lock_name in [".parentlock", "lock", "parent.lock"]:
        lp = os.path.join(profile_dir, lock_name)
        if os.path.exists(lp):
            try:
                os.remove(lp)
            except Exception:
                pass

    proxy_cfg, bridge = setup_camoufox_proxy(args.proxy) if args.proxy else (None, None)
    has_proxy = proxy_cfg is not None

    print(f"[*] Khởi chạy Camoufox Profile: {profile_dir}")
    print(f"[*] OS Giả lập: {args.os}")
    print(f"[*] Proxy: {args.proxy if args.proxy else 'None (Mạng trực tiếp)'}")
    if bridge:
        print(f"[*] Kích hoạt Cầu nối SOCKS5 -> HTTP Cục bộ: {proxy_cfg['server']}")
    print(f"[*] Màn hình: {args.screen}")
    print(f"[*] URL ban đầu: {args.url}")

    camoufox_kwargs = {
        "headless": args.headless,
        "os": args.os,
        "persistent_context": True,
        "user_data_dir": profile_dir,
        "humanize": not args.disable_humanize,
        "block_images": args.block_images,
        "block_webrtc": args.block_webrtc,
        "disable_coop": args.disable_coop,
        "canvas_noise": args.canvas_noise,
        "i_know_what_im_doing": True,
    }

    # Quản lý tiện ích mở rộng (uBlock Origin mặc định)
    if args.disable_ublock:
        camoufox_kwargs["exclude_addons"] = [DefaultAddons.UBO]

    if args.addons.strip() and os.path.isdir(args.addons.strip()):
        camoufox_kwargs["addons"] = [args.addons.strip()]

    # Cấu hình kích thước màn hình
    if args.screen and "x" in args.screen:
        try:
            w, h = map(int, args.screen.lower().split("x"))
            camoufox_kwargs["screen"] = Screen(max_width=w, max_height=h)
        except Exception:
            pass

    # Cấu hình ngôn ngữ
    if args.locale.strip():
        camoufox_kwargs["locale"] = args.locale.strip()

    # Cấu hình Proxy & GeoIP
    if has_proxy:
        camoufox_kwargs["proxy"] = proxy_cfg
        camoufox_kwargs["geoip"] = True

    # Đọc cấu hình Auto-Login nếu có trong profile
    autologin_file = os.path.join(profile_dir, "autologin.json")
    autologin_cfg = None
    if os.path.exists(autologin_file):
        try:
            import json
            with open(autologin_file, "r", encoding="utf-8") as f:
                autologin_cfg = json.load(f)
        except Exception:
            pass

    should_auto_login = args.auto_login or (autologin_cfg and autologin_cfg.get("enabled"))
    app_type = (autologin_cfg.get("type") if autologin_cfg else None) or args.auto_login_type or "tiktok"

    def run_browser_session(kwargs):
        with Camoufox(**kwargs) as context:
            page = context.pages[0] if context.pages else context.new_page()

            target_url = args.url.strip() if args.url.strip() else "https://www.tiktok.com"

            if should_auto_login and autologin_cfg and autologin_cfg.get("creds"):
                print(f"[*] Kích hoạt tự động đăng nhập {app_type.upper()}...")
                try:
                    from auto_login import auto_login_tiktok
                    if app_type.lower() == "tiktok":
                        auto_login_tiktok(page, autologin_cfg["creds"], target_url=target_url, logger=print)
                except Exception as e:
                    print(f"[!] Lỗi khi tự động đăng nhập: {e}")

            # Đảm bảo sau khi xử lý (hoặc nếu đã đăng nhập từ trước), trình duyệt luôn mở target_url thay vì đứng im ở about:blank
            if page.url == "about:blank" or not page.url:
                try:
                    print(f"[*] Điều hướng tới trang web: {target_url}")
                    page.goto(target_url, timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"[!] Không thể tải ngay URL ({target_url}): {e}")

            print("[*] Trình duyệt đang hoạt động. Giữ cửa sổ mở không giới hạn thời gian...")

            browser_closed = threading.Event()

            def on_close(_=None):
                browser_closed.set()

            context.on("close", on_close)

            while not browser_closed.is_set():
                try:
                    if len(context.pages) == 0:
                        time.sleep(1)
                        if len(context.pages) == 0:
                            print("[*] Người dùng đã đóng tất cả các tab. Thoát.")
                            break
                    time.sleep(1)
                except Exception:
                    break

    try:
        try:
            run_browser_session(camoufox_kwargs)
        except Exception as e:
            err_str = str(e)
            if ("Failed to get IP address" in err_str or "BadStatusLine" in err_str or "geoip" in err_str.lower()) and camoufox_kwargs.get("geoip"):
                print(f"[!] Lỗi định vị GeoIP qua proxy ({e}). Đang khởi động lại trình duyệt ở chế độ an toàn...", file=sys.stderr)
                camoufox_kwargs["geoip"] = False
                run_browser_session(camoufox_kwargs)
            else:
                raise
    except Exception as e:
        print(f"[!] Lỗi khi chạy Camoufox: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if bridge:
            bridge.stop()

    print("[*] Profile đã đóng hoàn tất.")

if __name__ == "__main__":
    main()
