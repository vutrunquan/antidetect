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
import html
import hmac
import hashlib
import struct
import base64
import requests
from datetime import datetime

SMAIL1S_API_URL = "https://smail1s.com/get_messages"
SMAIL1S_TEMPMAIL_OTP_URL = "https://smail1s.com/api/v2/mail/otp"


def _looks_like_2fa_secret(val: str) -> bool:
    """Kiểm tra chuỗi có giống mã bí mật 2FA TOTP (Base32: A-Z, 2-7, dài >= 12 ký tự, không chứa @)."""
    if not val or "@" in val:
        return False
    clean = re.sub(r"[\s\-=]+", "", val).upper()
    return len(clean) >= 12 and bool(re.fullmatch(r"[A-Z2-7]+", clean))


def generate_totp_2fa(secret: str, digits: int = 6, interval: int = 30) -> tuple:
    """
    Sinh mã OTP 2FA chuẩn RFC 6238 (TOTP - Google Authenticator / 2fa.live) hoàn toàn nội bộ.
    Trả về tuple (code_6_so, so_giay_con_lai).
    """
    if not secret or not isinstance(secret, str):
        return "", 0
    clean = re.sub(r"[\s\-]+", "", secret).upper()
    if not clean:
        return "", 0
    try:
        pad = (-len(clean)) % 8
        key = base64.b32decode(clean + ("=" * pad), casefold=True)
        now = int(time.time())
        counter = now // interval
        remaining = interval - (now % interval)
        msg = struct.pack(">Q", counter)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        code = str(binary % (10 ** digits)).zfill(digits)
        return code, remaining
    except Exception:
        return "", 0


def get_fresh_totp_2fa(secret: str, min_remaining: int = 4, logger=print) -> str:
    """
    Lấy mã 2FA TOTP còn đủ thời hạn an toàn (>= min_remaining giây).
    Nếu mã hiện tại sắp hết hạn, tự động chờ sang chu kỳ 30s mới để tránh lỗi hết hạn khi bấm gửi.
    """
    code, remaining = generate_totp_2fa(secret)
    if not code:
        return ""
    if remaining <= min_remaining:
        wait_s = remaining + 1
        logger(f"[*] Mã 2FA hiện tại ({code}) chỉ còn {remaining}s, chờ {wait_s}s để lấy mã chu kỳ mới...")
        time.sleep(wait_s)
        code, remaining = generate_totp_2fa(secret)
    logger(f"[✓] Đã sinh mã 2FA (TOTP): {code} (còn hiệu lực {remaining}s)")
    return code


def parse_account_string(raw: str) -> dict:
    """
    Phân tích chuỗi thông tin tài khoản theo các định dạng phổ biến:
    1. Định dạng 9 trường có 2FA:
       user|pass|2fa|mail|passmail|token|id|mailkhoiphuc|quocgia
    2. Định dạng 7 trường (không 2FA):
       username|password|email|passmail|refreshToken|clientId|mailBackup
    3. Định dạng 3 trường cơ bản có 2FA:
       user|pass|2fa
    4. Định dạng trực tiếp Smail1s (khi test lấy OTP nhanh):
       - OAuth2/Graph: email|passmail|refreshToken|clientId
       - OAuth2 rút gọn: email|refreshToken|clientId
       - Roundcube: email|passmail
       - EmailFake/TempMail: email@domain.com
    Hỗ trợ xử lý khoảng trắng, dòng thừa hoặc ghi chú kèm theo.
    """
    if not raw or not isinstance(raw, str):
        return {}

    valid_line = ""
    for line in raw.strip().splitlines():
        line = line.strip()
        if "|" in line or "@" in line:
            valid_line = line
            break

    if not valid_line:
        valid_line = raw.strip()

    parts = [p.strip() for p in valid_line.split("|")]

    # Trường hợp 1: Định dạng 9 trường có 2FA: user|pass|2fa|mail|passmail|token|id|mailkhoiphuc|quocgia
    # Nhận diện: có ít nhất 4 trường, trường [2] KHÔNG phải email và trường [3] LÀ email
    if len(parts) >= 4 and "@" not in parts[2] and "@" in parts[3]:
        return {
            "username": parts[0],
            "password": parts[1],
            "two_fa": parts[2],
            "email": parts[3],
            "passmail": parts[4] if len(parts) > 4 else "",
            "refresh_token": parts[5] if len(parts) > 5 else "",
            "client_id": parts[6] if len(parts) > 6 else "",
            "mail_backup": parts[7].split()[0] if len(parts) > 7 and parts[7] else "",
            "country": parts[8].split()[0] if len(parts) > 8 and parts[8] else ""
        }

    # Trường hợp 2: Định dạng 3 trường user|pass|2fa (không có email ở trường 0 hoặc 2)
    if len(parts) == 3 and "@" not in parts[0] and _looks_like_2fa_secret(parts[2]):
        return {
            "username": parts[0],
            "password": parts[1],
            "two_fa": parts[2],
            "email": "",
            "passmail": "",
            "refresh_token": "",
            "client_id": "",
            "mail_backup": "",
            "country": ""
        }

    # Trường hợp 3: Người dùng dán trực tiếp định dạng mail Smail1s (trường đầu là email, trường thứ 3 không phải email)
    if len(parts) >= 1 and "@" in parts[0] and (len(parts) <= 4 or "@" not in parts[2]):
        if len(parts) == 1:
            return {
                "username": "",
                "password": "",
                "two_fa": "",
                "email": parts[0],
                "passmail": "",
                "refresh_token": "",
                "client_id": "",
                "mail_backup": "",
                "country": ""
            }
        elif len(parts) == 2:
            return {
                "username": "",
                "password": "",
                "two_fa": "",
                "email": parts[0],
                "passmail": parts[1],
                "refresh_token": "",
                "client_id": "",
                "mail_backup": "",
                "country": ""
            }
        elif len(parts) == 3:
            return {
                "username": "",
                "password": "",
                "two_fa": "",
                "email": parts[0],
                "passmail": "",
                "refresh_token": parts[1],
                "client_id": parts[2],
                "mail_backup": "",
                "country": ""
            }
        else:
            return {
                "username": "",
                "password": "",
                "two_fa": "",
                "email": parts[0],
                "passmail": parts[1],
                "refresh_token": parts[2],
                "client_id": parts[3],
                "mail_backup": parts[4].split()[0] if len(parts) > 4 and parts[4] else "",
                "country": ""
            }

    if len(parts) < 2:
        return {}

    # Trường hợp 4: Định dạng 7 trường chuẩn: username|password|email|passmail|refreshToken|clientId|mailBackup
    creds = {
        "username": parts[0] if len(parts) > 0 else "",
        "password": parts[1] if len(parts) > 1 else "",
        "two_fa": "",
        "email": parts[2] if len(parts) > 2 else "",
        "passmail": parts[3] if len(parts) > 3 else "",
        "refresh_token": parts[4] if len(parts) > 4 else "",
        "client_id": parts[5] if len(parts) > 5 else "",
        "mail_backup": "",
        "country": ""
    }

    if len(parts) > 6:
        mb = parts[6].strip()
        creds["mail_backup"] = mb.split()[0] if mb else ""
    if len(parts) > 7:
        ct = parts[7].strip()
        creds["country"] = ct.split()[0] if ct else ""

    return creds


def _strip_html(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", raw_text, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_msg_timestamp(date_str: str) -> float:
    if not date_str or not isinstance(date_str, str):
        return 0.0
    s = date_str.strip().replace("T", " ").replace("Z", "")
    s = re.sub(r"\.\d+$", "", s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def _extract_otp_from_message(msg: dict, app_type: str = "tiktok") -> str:
    """Trích xuất mã OTP từ trường `code` của Smail1s hoặc phân tích regex từ subject/message."""
    api_code = str(msg.get("code") or "").strip()
    if api_code and re.fullmatch(r"\d{4,8}", api_code):
        return api_code

    subject = str(msg.get("subject") or "")
    body = _strip_html(str(msg.get("message") or ""))
    combined = f"{subject} {body}"

    app_lower = (app_type or "").strip().lower()
    if app_lower == "tiktok":
        m = re.search(r"\b(\d{6})\b", combined)
        if m:
            return m.group(1)

    for pattern in (r"\b(\d{6})\b", r"\b(\d{5})\b", r"\b(\d{4,8})\b"):
        m = re.search(pattern, combined)
        if m:
            return m.group(1)
    return ""


def _pick_best_otp_message(messages: list, app_type: str = "tiktok") -> tuple:
    """
    Chọn thư mới nhất chứa mã OTP phù hợp với `app_type` (mặc định ưu tiên thư từ TikTok).
    Trả về (msg_dict, otp_code) hoặc (None, "").
    """
    if not messages or not isinstance(messages, list):
        return None, ""

    valid_msgs = [m for m in messages if isinstance(m, dict)]
    if not valid_msgs:
        return None, ""

    # Sắp xếp mới nhất lên đầu nếu có trường date hợp lệ (giữ nguyên thứ tự gốc khi cùng timestamp)
    indexed = list(enumerate(valid_msgs))
    if any(_parse_msg_timestamp(m.get("date", "")) > 0 for _, m in indexed):
        indexed.sort(key=lambda item: (-_parse_msg_timestamp(item[1].get("date", "")), item[0]))
    ordered_msgs = [m for _, m in indexed]

    app_kw = (app_type or "").strip().lower()

    # Ưu tiên 1: Thư khớp đúng app_type (VD: tiktok) và có mã OTP
    if app_kw and app_kw != "all":
        for msg in ordered_msgs:
            haystack = " ".join([
                str(msg.get("from") or ""),
                str(msg.get("subject") or ""),
                _strip_html(str(msg.get("message") or ""))[:500]
            ]).lower()
            if app_kw in haystack:
                code = _extract_otp_from_message(msg, app_type=app_type)
                if code:
                    return msg, code

    # Ưu tiên 2: Thư mới nhất bất kỳ có chứa mã OTP
    for msg in ordered_msgs:
        code = _extract_otp_from_message(msg, app_type=app_type)
        if code:
            return msg, code

    return None, ""


def get_code_from_smail1s(
    email: str,
    refresh_token: str = "",
    client_id: str = "",
    passmail: str = "",
    app_type: str = "tiktok",
    mode: str = "auto",
    timeout: int = 20
) -> dict:
    """
    Gọi API Smail1s (https://smail1s.com/api-docs) để đọc hộp thư và trích xuất mã OTP.
    - Endpoint chính: POST https://smail1s.com/get_messages
    - Tự động chọn mode phù hợp:
      + Có refresh_token & client_id -> thử 'oauth' (IMAP/POP3), lỗi sẽ fallback sang 'graph' (Microsoft Graph API).
      + Chỉ có email & passmail -> dùng 'roundcube'.
      + Đuôi @smail1s.com (không pass/token) -> dùng TempMail API v2 (/api/v2/mail/otp).
      + Chỉ có email (Gmail Edu / EmailFake) -> dùng 'emailfake'.
    """
    email = (email or "").strip()
    refresh_token = (refresh_token or "").strip()
    client_id = (client_id or "").strip()
    passmail = (passmail or "").strip()

    if not email:
        return {
            "status": False,
            "message": "Thiếu địa chỉ email để lấy OTP qua Smail1s"
        }

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    # Hỗ trợ TempMail v2 nếu là địa chỉ @smail1s.com không kèm token/pass
    if mode == "auto" and not refresh_token and not client_id and not passmail and email.lower().endswith("@smail1s.com"):
        try:
            resp = requests.get(
                SMAIL1S_TEMPMAIL_OTP_URL,
                params={"email": email},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=timeout
            )
            if resp.status_code == 200:
                code = resp.text.strip()
                if code and re.fullmatch(r"\d{4,8}", code):
                    return {
                        "status": True,
                        "code": code,
                        "content": f"TempMail OTP ({email})",
                        "date": "",
                        "from": "smail1s.com",
                        "uid": code,
                        "mail_source": "tempmail_v2"
                    }
                return {"status": False, "message": "TempMail Smail1s chưa nhận được mã OTP mới"}
            return {"status": False, "message": f"Smail1s TempMail HTTP {resp.status_code}"}
        except Exception as e:
            return {"status": False, "message": f"Lỗi kết nối Smail1s TempMail: {e}"}

    # Xác định danh sách mode sẽ thử
    if mode and mode != "auto":
        modes_to_try = [mode]
    elif refresh_token and client_id:
        modes_to_try = ["oauth", "graph"]
    elif passmail:
        modes_to_try = ["roundcube"]
    else:
        modes_to_try = ["emailfake"]

    last_error = ""

    for current_mode in modes_to_try:
        if current_mode in ("oauth", "graph"):
            # Định dạng Smail1s OAuth2/Graph: email|pass|refresh_token|client_id
            data_str = f"{email}|{passmail or 'pass'}|{refresh_token}|{client_id}"
        elif current_mode == "roundcube":
            data_str = f"{email}|{passmail}"
        else:
            data_str = email

        payload = {
            "mode": current_mode,
            "data": data_str
        }

        try:
            resp = requests.post(
                SMAIL1S_API_URL,
                json=payload,
                headers=headers,
                timeout=timeout
            )
            if resp.status_code != 200:
                last_error = f"Smail1s ({current_mode}) HTTP {resp.status_code}: {resp.text[:120]}"
                continue

            res_json = resp.json()
            items = res_json.get("data") if isinstance(res_json, dict) else None
            if not items or not isinstance(items, list):
                last_error = f"Smail1s ({current_mode}) trả về dữ liệu trống"
                continue

            acc_res = items[0] if isinstance(items[0], dict) else {}
            err_msg = acc_res.get("error")
            messages = acc_res.get("messages") or []

            if messages:
                best_msg, code = _pick_best_otp_message(messages, app_type=app_type)
                if code and best_msg:
                    snippet = best_msg.get("subject") or _strip_html(str(best_msg.get("message") or ""))[:100]
                    return {
                        "status": True,
                        "code": code,
                        "content": snippet,
                        "date": str(best_msg.get("date") or ""),
                        "from": str(best_msg.get("from") or ""),
                        "uid": str(best_msg.get("uid") or ""),
                        "mail_source": str(acc_res.get("mailSource") or current_mode)
                    }
                # Đọc được hòm thư thành công nhưng chưa có thư OTP mới -> không cần báo lỗi token
                return {
                    "status": False,
                    "message": f"Hộp thư đã kết nối ({current_mode}) nhưng chưa có thư chứa mã OTP"
                }

            if err_msg:
                last_error = f"Smail1s ({current_mode}): {err_msg}"
                continue

            # Kết nối ok, hòm thư chưa có thư nào
            return {
                "status": False,
                "message": f"Hộp thư trống ({current_mode}), chưa có thư mới"
            }

        except Exception as e:
            last_error = f"Lỗi kết nối Smail1s ({current_mode}): {str(e)}"
            continue

    return {
        "status": False,
        "message": last_error or "Không thể lấy mã OTP từ Smail1s"
    }


def get_code_from_dongvanfb(email: str, refresh_token: str = "", client_id: str = "", app_type: str = "tiktok", timeout: int = 20, passmail: str = "") -> dict:
    """
    Alias tương thích ngược: chuyển hướng hoàn toàn sang Smail1s API (https://smail1s.com/get_messages).
    """
    return get_code_from_smail1s(
        email=email,
        refresh_token=refresh_token,
        client_id=client_id,
        passmail=passmail,
        app_type=app_type,
        timeout=timeout
    )


def poll_latest_mail_code(
    email: str,
    refresh_token: str = "",
    client_id: str = "",
    app_type: str = "tiktok",
    initial_code: str = None,
    max_wait: int = 75,
    interval: int = 3,
    logger=print,
    passmail: str = "",
    initial_uid: str = None
) -> str:
    """
    Thực hiện polling API Smail1s để chờ mã OTP mới nhất
    """
    start_time = time.time()
    logger(f"[*] Bắt đầu chờ mã OTP từ Smail1s cho email {email} (tối đa {max_wait}s)...")

    while time.time() - start_time < max_wait:
        res = get_code_from_smail1s(
            email=email,
            refresh_token=refresh_token,
            client_id=client_id,
            passmail=passmail,
            app_type=app_type
        )
        if res.get("status"):
            code = str(res.get("code", "")).strip()
            uid = str(res.get("uid", "")).strip()
            is_new_uid = bool(initial_uid and uid and uid != initial_uid)
            # Nếu code hợp lệ và (khác initial_code hoặc UID thư mới hoặc đã chờ trên 12s)
            if code and (not initial_code or code != initial_code or is_new_uid or (time.time() - start_time > 12)):
                src = res.get("mail_source", "smail1s")
                logger(f"[✓] Đã nhận mã OTP ({src}): {code} (Nội dung: {res.get('content', '')})")
                return code
            elif code:
                logger(f"[*] Mã hiện tại ({code}) có thể là mã cũ, đang chờ TikTok gửi mã mới...")
        else:
            logger(f"[*] Đang kiểm tra hộp thư Smail1s... ({res.get('message', 'Chưa có thư mới')})")

        time.sleep(interval)

    logger("[!] Hết thời gian chờ mã OTP từ Smail1s.")
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

def _click_modal_continue_button(page, logger=print):
    """Bấm nút Next / Siguiente / Tiếp tục / Send code trong modal sau khi chọn phương thức xác minh."""
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
                logger(f"[*] Bấm nút tiếp tục sau khi chọn phương thức: {next_sel}")
                n_btn.first.click()
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass
    return False


def _detect_verification_channel(page, has_two_fa: bool, selected_method: str = "") -> str:
    """
    Xác định màn hình nhập mã hiện tại của TikTok đang yêu cầu:
    - '2fa': Mã 6 số từ ứng dụng xác thực (Authenticator App / TOTP)
    - 'email': Mã OTP gửi về hộp thư Email (Smail1s)
    """
    if selected_method in ("2fa", "email"):
        return selected_method

    modal_text = ""
    try:
        container = page.locator('#idv-modal-container, div[role="dialog"]').first
        if container.count() > 0 and container.is_visible():
            modal_text = (container.inner_text() or "").lower()
        else:
            modal_text = (page.locator("body").inner_text() or "").lower()[:1500]
    except Exception:
        pass

    auth_keywords = [
        "authenticator",
        "ứng dụng xác thực",
        "aplicación de autenticación",
        "app de autenticación",
        "application d'authentification",
        "authentifizierungs",
        "google authenticator",
        "2-step",
        "two-step",
        "xác minh 2 bước",
        "verification app",
        "auth app",
    ]
    email_keywords = [
        "sent to",
        "đã gửi đến",
        "enviado a",
        "envoyé à",
        "check your email",
        "kiểm tra email",
        "revisa tu correo",
    ]

    is_auth_prompt = any(k in modal_text for k in auth_keywords)
    is_email_prompt = any(k in modal_text for k in email_keywords) or bool(re.search(r"[a-zA-Z0-9*._-]+@[a-zA-Z0-9.-]+", modal_text))

    if has_two_fa and is_auth_prompt:
        return "2fa"
    if is_email_prompt and not is_auth_prompt:
        return "email"
    if has_two_fa:
        return "2fa"
    return "email"


def handle_tiktok_verification_challenge(
    page,
    email: str = "",
    refresh_token: str = "",
    client_id: str = "",
    initial_otp: str = None,
    logger=print,
    passmail: str = "",
    initial_uid: str = None,
    two_fa: str = ""
) -> bool:
    """
    Xử lý quy trình xác minh 2FA (Authenticator App TOTP) & Email OTP theo đúng luồng TikTok:
    1. Nhận diện popup 'Verify it's really you' / 'Verifica que eres tú' hoặc màn hình nhập 2FA trực tiếp
    2. Nếu tài khoản có 2FA: ưu tiên chọn phương thức Authenticator App (hoặc điền thẳng nếu đã ở màn hình 2FA)
       Nếu không có 2FA hoặc TikTok yêu cầu xác minh Email: chọn ô Email để nhận mã qua Smail1s
    3. Lấy mã 6 số (TOTP 2FA nội bộ hoặc OTP Email từ Smail1s)
    4. Điền mã 6 số vào ô nhập
    5. Click nút 'Siguiente' / 'Next' / 'Tiếp tục' / 'Verify' để hoàn tất đăng nhập
    """
    logger("[*] Đang kiểm tra thử thách xác minh danh tính TikTok...")
    has_two_fa = bool(two_fa and generate_totp_2fa(two_fa)[0])
    selected_method = ""

    # BƯỚC 1: Kiểm tra đã ở màn hình nhập code hay đang ở màn hình chọn phương thức
    start_step1 = time.time()
    while time.time() - start_step1 < 25:
        # Kiểm tra nếu đã hiển thị sẵn ô nhập mã 6 số
        code_test = page.locator(
            '#idv-modal-container input:not([type="radio"]):not([type="checkbox"]), '
            'div[role="dialog"] input:not([type="radio"]):not([type="checkbox"]), '
            'input[placeholder*="6"], input[placeholder*="código" i], input[placeholder*="code" i], '
            'input[autocomplete="one-time-code"]'
        )
        if code_test.count() > 0 and code_test.first.is_visible():
            break

        # Nếu tài khoản có 2FA: ưu tiên tìm & bấm chọn phương thức Authenticator App trước
        if has_two_fa:
            auth_pattern = re.compile(
                r"(authenticator|ứng dụng xác thực|aplicación de autenticación|app de autenticación|application d'authentification|verification app|2-step)",
                re.I
            )
            auth_loc = page.locator('#idv-modal-container, div[role="dialog"], body').locator('div, li, button, label, span').filter(has_text=auth_pattern).last
            if auth_loc.count() > 0 and auth_loc.is_visible():
                try:
                    txt = auth_loc.inner_text().strip()
                    if len(txt) < 120:
                        logger(f"[*] Tìm thấy phương thức 2FA Authenticator ({txt}). Đang click chọn...")
                        auth_loc.click(force=True)
                        selected_method = "2fa"
                        page.wait_for_timeout(1000)
                        _click_modal_continue_button(page, logger=logger)
                        break
                except Exception as e:
                    logger(f"[!] Thử click ô 2FA Authenticator: {e}")

        # Nếu không thấy ô Authenticator (hoặc tài khoản không có 2FA): tìm ô phương thức Email
        loc = page.locator('#idv-modal-container, div[role="dialog"], body').locator('*').filter(has_text=re.compile(r'@[a-zA-Z0-9.-]+')).last
        if loc.count() > 0:
            try:
                txt = loc.inner_text().strip()
                logger(f"[*] Tìm thấy ô phương thức Email ({txt}). Đang click để nhận mã...")
                loc.click(force=True)
                selected_method = "email"
                page.wait_for_timeout(1000)
                _click_modal_continue_button(page, logger=logger)
                break
            except Exception as e:
                logger(f"[!] Thử click ô email: {e}")

        time.sleep(1)

    # BƯỚC 2: Chờ xuất hiện ô nhập mã 6 số (Verificar identidad / Verify identity / 2-step verification)
    logger("[*] Chờ hiển thị ô nhập mã xác thực 6 số...")
    code_input = None
    start_find_box = time.time()

    while time.time() - start_find_box < 20:
        for sel in [
            '#idv-modal-container input:not([type="radio"]):not([type="checkbox"])',
            'div[role="dialog"] input:not([type="radio"]):not([type="checkbox"])',
            'input[autocomplete="one-time-code"]',
            'input[placeholder*="código" i]',
            'input[placeholder*="6"]',
            'input[placeholder*="code" i]',
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
        logger("[!] Không tìm thấy ô nhập mã xác thực.")
        return False

    logger("[✓] Đã xuất hiện ô nhập mã xác thực!")

    # BƯỚC 3: Lấy mã 6 số theo đúng kênh xác minh (2FA TOTP hoặc Email OTP qua Smail1s)
    channel = _detect_verification_channel(page, has_two_fa=has_two_fa, selected_method=selected_method)
    new_otp = None

    if channel == "2fa" and has_two_fa:
        logger("[*] Chế độ xác minh: 2FA Authenticator App (TOTP)...")
        new_otp = get_fresh_totp_2fa(two_fa, min_remaining=4, logger=logger)
        if not new_otp:
            logger("[!] Không sinh được mã 2FA từ khóa bí mật.")
            return False
    else:
        if not email:
            if has_two_fa:
                logger("[*] Không có email, chuyển sang dùng mã 2FA (TOTP)...")
                new_otp = get_fresh_totp_2fa(two_fa, min_remaining=4, logger=logger)
            else:
                logger("[!] Thiếu địa chỉ email để tự động lấy mã OTP từ Smail1s!")
                return False
        else:
            logger("[*] Chế độ xác minh: Email OTP qua Smail1s...")
            new_otp = poll_latest_mail_code(
                email=email,
                refresh_token=refresh_token,
                client_id=client_id,
                app_type="tiktok",
                initial_code=initial_otp,
                max_wait=65,
                interval=3,
                logger=logger,
                passmail=passmail,
                initial_uid=initial_uid
            )

    if not new_otp:
        logger("[!] Không lấy được mã xác thực trong thời gian chờ.")
        return False

    # BƯỚC 4: Điền mã 6 số vào ô
    logger(f"[*] Điền mã xác thực ({new_otp}) vào ô xác minh...")
    try:
        inputs = page.locator(
            '#idv-modal-container input:not([type="radio"]):not([type="checkbox"]), '
            'div[role="dialog"] input:not([type="radio"]):not([type="checkbox"])'
        )
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
        logger(f"[!] Lỗi khi điền mã xác thực: {e}")
        return False

    # BƯỚC 5: Nhấn nút xác nhận / đăng nhập (Siguiente / Next / Verify / Tiếp tục)
    logger("[*] Bấm nút xác nhận (Siguiente / Next / Verify)...")
    submit_clicked = False
    for sel in [
        '#idv-modal-container button:has-text("Siguiente")',
        '#idv-modal-container button:has-text("Next")',
        '#idv-modal-container button:has-text("Verify")',
        '#idv-modal-container button:has-text("Xác nhận")',
        '#idv-modal-container button:has-text("Tiếp tục")',
        '#idv-modal-container button:has-text("Continuar")',
        '#idv-modal-container button:has-text("Confirmar")',
        '#idv-modal-container button:has-text("Log in")',
        '#idv-modal-container button:has-text("Đăng nhập")',
        '#idv-modal-container button[type="submit"]',
        'div[role="dialog"] button:has-text("Siguiente")',
        'div[role="dialog"] button:has-text("Next")',
        'div[role="dialog"] button:has-text("Verify")',
        'div[role="dialog"] button:has-text("Tiếp tục")',
        'div[role="dialog"] button[type="submit"]',
        'button:has-text("Next")',
        'button:has-text("Siguiente")',
        'button:has-text("Verify")',
        'button[type="submit"]'
    ]:
        btn = page.locator(sel)
        if btn.count() > 0 and btn.first.is_visible() and btn.first.is_enabled():
            try:
                btn.first.click()
                submit_clicked = True
                logger(f"[✓] Đã nhấn nút xác nhận ({sel})!")
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
    4. Hỗ trợ cả 2FA Authenticator App (TOTP) và Email OTP qua Smail1s API
    5. Điền mã 6 số và nhấn nút xác nhận để hoàn tất đăng nhập
    """
    username = creds.get("username", "").strip()
    password = creds.get("password", "").strip()
    two_fa = creds.get("two_fa", "").strip()
    email = creds.get("email", "").strip()
    passmail = creds.get("passmail", "").strip()
    refresh_token = creds.get("refresh_token", "").strip()
    client_id = creds.get("client_id", "").strip()

    if not username and not email:
        logger("[!] Không có username hoặc email để đăng nhập TikTok!")
        return False
    if not password:
        logger("[!] Không có mật khẩu để đăng nhập TikTok!")
        return False

    login_id = username if username else email
    auth_mode_lbl = "2FA + Email Smail1s" if (two_fa and email) else ("2FA TOTP" if two_fa else "Email Smail1s")
    logger(f"[*] Khởi động quá trình tự động đăng nhập TikTok cho: {login_id} (Xác minh: {auth_mode_lbl})")

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

        # Ghi nhận mã OTP gần nhất trong mail nếu có (chỉ gọi nếu cần fallback sang Email OTP)
        initial_otp = None
        initial_uid = None
        if email:
            try:
                chk = get_code_from_smail1s(
                    email=email,
                    refresh_token=refresh_token,
                    client_id=client_id,
                    passmail=passmail,
                    app_type="tiktok",
                    timeout=8
                )
                if chk.get("status"):
                    initial_otp = str(chk.get("code", "")).strip()
                    initial_uid = str(chk.get("uid", "")).strip() or None
                    logger(f"[*] Mã OTP gần nhất trước đó trong hòm thư (Smail1s): {initial_otp}")
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

        # 6. Chờ phản hồi & xử lý xác minh danh tính (2FA Authenticator & Email OTP)
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

            # Kiểm tra popup xác minh HOẶC màn hình nhập mã 2FA/OTP trực tiếp trên trang
            modal = page.locator('#idv-modal-container, div[role="dialog"]')
            inline_otp = page.locator(
                'input[autocomplete="one-time-code"], '
                'input[placeholder*="6"], input[placeholder*="code" i], input[placeholder*="código" i]'
            )
            has_inline_otp = inline_otp.count() > 0 and inline_otp.first.is_visible()
            if modal.count() > 0 or has_inline_otp:
                logger("[*] Phát hiện giao diện xác minh danh tính / 2FA TikTok!")
                if handle_tiktok_verification_challenge(
                    page,
                    email=email,
                    refresh_token=refresh_token,
                    client_id=client_id,
                    initial_otp=initial_otp,
                    logger=logger,
                    passmail=passmail,
                    initial_uid=initial_uid,
                    two_fa=two_fa
                ):
                    page.wait_for_timeout(4000)
                    if is_tiktok_logged_in(page) or "/login" not in page.url:
                        logger("[✓] Xác thực 2FA/OTP thành công! Đã đăng nhập vào TikTok.")
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
