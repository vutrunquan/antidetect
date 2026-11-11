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

import time
import re
import requests

DONGVANFB_API_URL = "https://tools.dongvanfb.net/api/get_code_oauth2"

def parse_account_string(raw: str) -> dict:
    """
    Phân tích chuỗi thông tin tài khoản theo định dạng:
    username|password|email|passmail|refreshToken|clientId|mailBackup
    Hỗ trợ xử lý khoảng trắng, dòng thừa hoặc ghi chú kèm theo.
    """
    if not raw or not isinstance(raw, str):
        return {}

    valid_line = ""
    for line in raw.strip().splitlines():
        line = line.strip()
        if "|" in line:
            valid_line = line
            break

    if not valid_line:
        valid_line = raw.strip()

    parts = [p.strip() for p in valid_line.split("|")]
    if len(parts) < 2:
        return {}

    creds = {
        "username": parts[0] if len(parts) > 0 else "",
        "password": parts[1] if len(parts) > 1 else "",
        "email": parts[2] if len(parts) > 2 else "",
        "passmail": parts[3] if len(parts) > 3 else "",
        "refresh_token": parts[4] if len(parts) > 4 else "",
        "client_id": parts[5] if len(parts) > 5 else "",
        "mail_backup": ""
    }

    if len(parts) > 6:
        mb = parts[6].strip()
        creds["mail_backup"] = mb.split()[0] if mb else ""

    return creds

def get_code_from_dongvanfb(email: str, refresh_token: str, client_id: str, app_type: str = "tiktok", timeout: int = 15) -> dict:
    """
    Gọi API DongVanFB để lấy mã OTP qua OAuth2 Token
    Endpoint: POST https://tools.dongvanfb.net/api/get_code_oauth2
    """
    if not email or not refresh_token or not client_id:
        return {
            "status": False,
            "message": "Thiếu email, refresh_token hoặc client_id để lấy OTP"
        }

    payload = {
        "email": email.strip(),
        "refresh_token": refresh_token.strip(),
        "client_id": client_id.strip(),
        "type": app_type.strip()
    }

    try:
        resp = requests.post(
            DONGVANFB_API_URL,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=timeout
        )
        if resp.status_code == 200:
            data = resp.json()
            return data
        else:
            return {
                "status": False,
                "message": f"DongVanFB HTTP {resp.status_code}: {resp.text[:100]}"
            }
    except Exception as e:
        return {
            "status": False,
            "message": f"Lỗi kết nối DongVanFB: {str(e)}"
        }

def poll_latest_mail_code(email: str, refresh_token: str, client_id: str, app_type: str = "tiktok", initial_code: str = None, max_wait: int = 75, interval: int = 3, logger=print) -> str:
    """
    Thực hiện polling API DongVanFB để chờ mã code mới nhất
    """
    start_time = time.time()
    logger(f"[*] Bắt đầu chờ mã OTP từ DongVanFB cho email {email} (tối đa {max_wait}s)...")

    while time.time() - start_time < max_wait:
        res = get_code_from_dongvanfb(email, refresh_token, client_id, app_type=app_type)
        if res.get("status"):
            code = str(res.get("code", "")).strip()
            # Nếu code hợp lệ và (không có initial_code hoặc khác initial_code hoặc đã chờ trên 12s)
            if code and (not initial_code or code != initial_code or (time.time() - start_time > 12)):
                logger(f"[✓] Đã nhận mã OTP: {code} (Nội dung: {res.get('content', '')})")
                return code
            elif code:
                logger(f"[*] Mã hiện tại ({code}) có thể là mã cũ, đang chờ TikTok gửi mã mới...")
        else:
            logger(f"[*] Đang kiểm tra hộp thư... ({res.get('message', 'Chưa có thư mới')})")

        time.sleep(interval)

    logger("[!] Hết thời gian chờ mã OTP từ DongVanFB.")
    return None

def is_tiktok_logged_in(page) -> bool:
    """
    Kiểm tra xem trang hiện tại đã đăng nhập TikTok chưa
    Dựa vào cookie sessionid hoặc các selector đặc trưng khi đã đăng nhập
    """
    try:
        cookies = page.context.cookies()
        for c in cookies:
            if c.get("name") in ["sessionid", "sessionid_ss"] and c.get("value"):
                return True
    except Exception:
        pass

    try:
        avatar = page.query_selector("header [data-e2e='profile-icon'], header img[alt*='avatar'], header a[href*='/@']")
        if avatar:
            return True
    except Exception:
        pass

    return False

def handle_tiktok_verification_challenge(page, email: str, refresh_token: str, client_id: str, initial_otp: str = None, logger=print) -> bool:
    """
    Xử lý quy trình xác minh 2FA Email theo đúng luồng TikTok:
    1. Nhận diện popup 'Verify it's really you' / 'Verifica que eres tú'
    2. Click vào ô Email (chứa email s***r@outlook.com / a***8@hotmail.com) để làm xuất hiện ô nhập code
    3. Nhận diện màn hình 'Verificar identidad' / 'Verify identity' chứa ô nhập 6 số
    4. Gọi API DongVanFB để lấy mã OTP mới nhất
    5. Điền mã OTP 6 số vào ô nhập
    6. Click nút màu đỏ 'Siguiente' / 'Next' / 'Tiếp tục' để hoàn tất đăng nhập
    """
    logger("[*] Đang kiểm tra thử thách xác minh danh tính TikTok...")

    # BƯỚC 1: Bấm vào ô Email trong popup 'Verify it's really you'
    email_option_clicked = False
    start_click_email = time.time()

    while time.time() - start_click_email < 25:
        # Kiểm tra xem có ô chứa email trong dialog không
        # Tìm phần tử leaf chứa '@' (ví dụ: a***8@hotmail.com hoặc s***r@outlook.com)
        loc = page.locator('#idv-modal-container, div[role="dialog"], body').locator('*').filter(has_text=re.compile(r'@[a-zA-Z0-9.-]+')).last
        if loc.count() > 0:
            try:
                txt = loc.inner_text().strip()
                logger(f"[*] Tìm thấy ô phương thức Email ({txt}). Đang click để nhận mã...")
                loc.click(force=True)
                email_option_clicked = True
                page.wait_for_timeout(1000)

                # Bấm nút 'Next' / 'Siguiente' / 'Tiếp tục' / 'Send code' nếu có trong modal sau khi chọn email
                for next_sel in [
                    '#idv-modal-container button:has-text("Next")',
                    '#idv-modal-container button:has-text("Siguiente")',
                    '#idv-modal-container button:has-text("Tiếp tục")',
                    '#idv-modal-container button:has-text("Continuar")',
                    '#idv-modal-container button:has-text("Send code")',
                    '#idv-modal-container button:has-text("Enviar código")',
                    'div[role="dialog"] button:has-text("Next")',
                    'div[role="dialog"] button:has-text("Siguiente")',
                    'div[role="dialog"] button:has-text("Tiếp tục")',
                    'div[role="dialog"] button:has-text("Continuar")',
                    'div[role="dialog"] button:has-text("Send code")',
                    'div[role="dialog"] button:has-text("Enviar código")',
                    '#idv-modal-container button[type="submit"]',
                    'div[role="dialog"] button[type="submit"]'
                ]:
                    try:
                        n_btn = page.locator(next_sel)
                        if n_btn.count() > 0 and n_btn.first.is_visible() and n_btn.first.is_enabled():
                            logger(f"[*] Bấm nút tiếp tục sau khi chọn email: {next_sel}")
                            n_btn.first.click()
                            page.wait_for_timeout(2000)
                            break
                    except Exception:
                        pass
                break
            except Exception as e:
                logger(f"[!] Thử click ô email: {e}")

        # Kiểm tra nếu đã ở sẵn màn hình nhập code rồi
        code_test = page.locator('#idv-modal-container input, div[role="dialog"] input, input[placeholder*="6"], input[placeholder*="código"], input[placeholder*="code"]')
        if code_test.count() > 0:
            email_option_clicked = True
            break

        time.sleep(1)

    # BƯỚC 2: Chờ xuất hiện ô nhập mã 6 số (Verificar identidad / Verify identity)
    logger("[*] Chờ hiển thị ô nhập mã OTP 6 số...")
    code_input = None
    start_find_box = time.time()

    while time.time() - start_find_box < 20:
        for sel in [
            '#idv-modal-container input',
            'div[role="dialog"] input',
            'input[placeholder*="código"]',
            'input[placeholder*="6"]',
            'input[placeholder*="code"]',
            'input[type="text"]:not([name="username"])',
            'input[type="number"]'
        ]:
            l = page.locator(sel)
            if l.count() > 0 and l.first.is_visible():
                code_input = l.first
                break
        if code_input:
            break
        time.sleep(1)

    if not code_input:
        logger("[!] Không tìm thấy ô nhập mã OTP sau khi chọn email.")
        return False

    logger("[✓] Đã xuất hiện ô nhập mã xác thực OTP!")

    # BƯỚC 3: Lấy mã OTP mới nhất từ DongVanFB
    if not email or not refresh_token or not client_id:
        logger("[!] Thiếu thông tin email/refreshToken/clientId để tự động lấy mã OTP!")
        return False

    new_otp = poll_latest_mail_code(
        email=email,
        refresh_token=refresh_token,
        client_id=client_id,
        app_type="tiktok",
        initial_code=initial_otp,
        max_wait=65,
        interval=3,
        logger=logger
    )

    if not new_otp:
        logger("[!] Không lấy được mã OTP từ DongVanFB trong thời gian chờ.")
        return False

    # BƯỚC 4: Điền mã OTP 6 số vào ô
    logger(f"[*] Điền mã OTP ({new_otp}) vào ô xác minh...")
    try:
        inputs = page.locator('#idv-modal-container input, div[role="dialog"] input')
        visible_inputs = [inputs.nth(i) for i in range(inputs.count()) if inputs.nth(i).is_visible()]
        if len(visible_inputs) == 6:
            logger("[*] Phát hiện 6 ô nhập mã riêng biệt. Đang điền từng số...")
            for idx, digit in enumerate(new_otp[:6]):
                inp = visible_inputs[idx]
                inp.click()
                inp.fill(digit)
                page.wait_for_timeout(80)
        else:
            code_input.click()
            page.wait_for_timeout(200)
            code_input.fill("")
            code_input.type(new_otp, delay=70)
        page.wait_for_timeout(800)
    except Exception as e:
        logger(f"[!] Lỗi khi điền mã OTP: {e}")
        return False

    # BƯỚC 5: Nhấn nút màu đỏ để đăng nhập (Siguiente / Next / Tiếp tục)
    logger("[*] Bấm nút đăng nhập màu đỏ (Siguiente / Next)...")
    submit_clicked = False
    for sel in [
        '#idv-modal-container button:has-text("Siguiente")',
        '#idv-modal-container button:has-text("Next")',
        '#idv-modal-container button:has-text("Tiếp tục")',
        '#idv-modal-container button:has-text("Continuar")',
        '#idv-modal-container button:has-text("Log in")',
        '#idv-modal-container button:has-text("Đăng nhập")',
        '#idv-modal-container button[type="submit"]',
        'div[role="dialog"] button:has-text("Siguiente")',
        'div[role="dialog"] button:has-text("Next")',
        'div[role="dialog"] button:has-text("Tiếp tục")',
        'div[role="dialog"] button[type="submit"]'
    ]:
        btn = page.locator(sel)
        if btn.count() > 0 and btn.first.is_visible():
            try:
                btn.first.click()
                submit_clicked = True
                logger(f"[✓] Đã nhấn nút màu đỏ ({sel})!")
                break
            except Exception:
                pass

    if not submit_clicked:
        logger("[*] Nhấn Enter trong ô nhập mã để gửi...")
        code_input.press("Enter")

    page.wait_for_timeout(5000)
    return True

def auto_login_tiktok(page, creds: dict, target_url: str = "https://www.tiktok.com", logger=print) -> bool:
    """
    Quy trình tự động đăng nhập TikTok hoàn chỉnh:
    1. Kiểm tra cookie / session đã có sẵn chưa
    2. Điền username & password
    3. Nhấn đăng nhập
    4. Tự động bấm ô Email trong popup 'Verify it's really you' để mở ô nhập code
    5. Lấy mã OTP từ DongVanFB API
    6. Điền mã OTP và nhấn nút màu đỏ 'Siguiente' / 'Next' để đăng nhập
    """
    username = creds.get("username", "").strip()
    password = creds.get("password", "").strip()
    email = creds.get("email", "").strip()
    refresh_token = creds.get("refresh_token", "").strip()
    client_id = creds.get("client_id", "").strip()

    if not username and not email:
        logger("[!] Không có username hoặc email để đăng nhập TikTok!")
        return False
    if not password:
        logger("[!] Không có mật khẩu để đăng nhập TikTok!")
        return False

    login_id = username if username else email
    logger(f"[*] Khởi động quá trình tự động đăng nhập TikTok cho: {login_id}")

    try:
        # 1. Kiểm tra xem đã có phiên đăng nhập cũ chưa
        if is_tiktok_logged_in(page):
            logger("[✓] Tài khoản TikTok này đã được đăng nhập từ trước (cookie hợp lệ)! Bỏ qua đăng nhập.")
            dest = target_url if target_url and target_url != "about:blank" else "https://www.tiktok.com"
            if page.url == "about:blank" or not page.url:
                try:
                    logger(f"[*] Đang chuyển đến trang web: {dest}")
                    page.goto(dest, timeout=45000, wait_until="domcontentloaded")
                except Exception as e:
                    logger(f"[!] Không thể tải {dest}: {e}")
            return True

        # Ghi nhận mã OTP gần nhất trong mail nếu có
        initial_otp = None
        if email and refresh_token and client_id:
            try:
                chk = get_code_from_dongvanfb(email, refresh_token, client_id, "tiktok", timeout=5)
                if chk.get("status"):
                    initial_otp = str(chk.get("code", "")).strip()
                    logger(f"[*] Mã OTP gần nhất trước đó trong hòm thư: {initial_otp}")
            except Exception:
                pass

        # 2. Điều hướng tới trang đăng nhập TikTok
        login_url = "https://www.tiktok.com/login/phone-or-email/email"
        logger(f"[*] Điều hướng tới: {login_url}")
        page.goto(login_url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        # 3. Tìm và nhập ô Username / Email
        user_input = None
        for sel in ['input[name="username"]', 'input[placeholder*="Email"]', 'input[placeholder*="username"]', 'input[type="text"]']:
            try:
                el = page.wait_for_selector(sel, timeout=5000)
                if el and el.is_visible():
                    user_input = el
                    break
            except Exception:
                continue

        if not user_input:
            logger("[!] Không tìm thấy ô nhập Username/Email trên trang đăng nhập.")
            return False

        logger(f"[*] Điền tài khoản: {login_id}")
        user_input.click()
        page.wait_for_timeout(300)
        user_input.fill("")
        user_input.type(login_id, delay=60)
        page.wait_for_timeout(400)

        # 4. Tìm và nhập Mật khẩu
        pass_input = None
        for sel in ['input[type="password"]', 'input[placeholder*="Password"]', 'input[placeholder*="Mật khẩu"]', 'input[placeholder*="Contraseña"]']:
            try:
                el = page.wait_for_selector(sel, timeout=5000)
                if el and el.is_visible():
                    pass_input = el
                    break
            except Exception:
                continue

        if not pass_input:
            logger("[!] Không tìm thấy ô nhập Mật khẩu.")
            return False

        logger("[*] Điền mật khẩu...")
        pass_input.click()
        page.wait_for_timeout(300)
        pass_input.fill("")
        pass_input.type(password, delay=70)
        page.wait_for_timeout(600)

        # 5. Bấm nút Đăng nhập ban đầu
        submit_btn = None
        for sel in ['button[type="submit"]', 'button:has-text("Log in")', 'button:has-text("Đăng nhập")', 'button:has-text("Iniciar sesión")']:
            try:
                el = page.wait_for_selector(sel, timeout=4000)
                if el and el.is_visible():
                    submit_btn = el
                    break
            except Exception:
                continue

        if submit_btn:
            logger("[*] Nhấn nút Đăng nhập...")
            submit_btn.click()
        else:
            logger("[*] Không tìm thấy nút submit, nhấn Enter...")
            pass_input.press("Enter")

        # 6. Chờ phản hồi & xử lý xác minh danh tính
        logger("[*] Đang chờ phản hồi từ TikTok...")
        page.wait_for_timeout(4000)

        start_wait = time.time()
        while time.time() - start_wait < 90:
            if is_tiktok_logged_in(page):
                logger("[✓] Đăng nhập TikTok thành công!")
                return True

            current_url = page.url
            if "/login" not in current_url:
                logger(f"[✓] Đã chuyển hướng khỏi trang đăng nhập ({current_url}). Đăng nhập thành công!")
                return True

            # Kiểm tra Captcha trượt
            captcha_el = page.query_selector("div[id*='captcha'], div[class*='captcha'], div.secsdk-captcha-drag-icon")
            if captcha_el and captcha_el.is_visible():
                logger("[!] TikTok hiển thị Captcha kéo trượt. Vui lòng kéo thanh trượt trên màn hình để tiếp tục...")
                page.wait_for_timeout(3000)
                continue

            # Kiểm tra popup xác minh (Verify it's really you / Verificar identidad)
            modal = page.locator('#idv-modal-container, div[role="dialog"]')
            if modal.count() > 0:
                logger("[*] Phát hiện giao diện xác minh danh tính TikTok!")
                if handle_tiktok_verification_challenge(page, email, refresh_token, client_id, initial_otp=initial_otp, logger=logger):
                    page.wait_for_timeout(4000)
                    if is_tiktok_logged_in(page) or "/login" not in page.url:
                        logger("[✓] Xác thực OTP thành công! Đã đăng nhập vào TikTok.")
                        return True

            page.wait_for_timeout(2000)

        if is_tiktok_logged_in(page) or "/login" not in page.url:
            logger("[✓] Đăng nhập TikTok hoàn tất!")
            return True

        logger("[*] Kết thúc quy trình tự động. Trình duyệt vẫn giữ mở để người dùng tiếp tục.")
        return False

    except Exception as e:
        logger(f"[!] Lỗi trong quá trình tự động đăng nhập TikTok: {e}")
        return False
