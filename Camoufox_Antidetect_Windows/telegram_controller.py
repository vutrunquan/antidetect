import os
import html
import threading
from datetime import datetime
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from platform_utils import load_config
from post_manager import (
    get_video_record, update_video_status, add_tracked_channel,
    get_all_channels, force_publish_draft, delete_tiktok_video_and_file,
    get_all_channels_kpi_summary, execute_compensation, audit_daily_videos
)

_bot_instance: telebot.TeleBot | None = None
_bot_token_used: str = ""

def get_bot() -> telebot.TeleBot | None:
    global _bot_instance, _bot_token_used
    cfg = load_config()
    token = cfg.get("telegram_bot_token", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None
    if _bot_instance is None or _bot_token_used != token:
        _bot_instance = telebot.TeleBot(token, parse_mode="HTML")
        _bot_token_used = token
        _register_handlers(_bot_instance)
    return _bot_instance

def build_action_keyboard(video_id: str, action: str, yt_video_url: str = "") -> InlineKeyboardMarkup:
    """Tạo bàn phím Inline Keyboard ngay dưới tin nhắn báo cáo."""
    markup = InlineKeyboardMarkup(row_width=2)
    action_buttons = []
    if action == "DRAFTED":
        action_buttons.append(InlineKeyboardButton("🚀 Đăng TikTok nháp", callback_data=f"pub:{video_id}"))
        action_buttons.append(InlineKeyboardButton("🗑️ Xóa nháp TikTok", callback_data=f"del:{video_id}"))
    elif action == "PUBLISHED":
        action_buttons.append(InlineKeyboardButton("🗑️ Xóa video & File gốc", callback_data=f"del:{video_id}"))

    if yt_video_url:
        action_buttons.append(InlineKeyboardButton("📺 Xem Shorts trên YouTube", url=yt_video_url))

    if action_buttons:
        markup.add(*action_buttons)

    btn_add_ch = InlineKeyboardButton("➕ Thêm kênh nguồn", callback_data="cmd:add_guide")
    btn_list_ch = InlineKeyboardButton("📋 DS Kênh đang chạy", callback_data="cmd:list_channels")
    markup.add(btn_add_ch, btn_list_ch)
    return markup

def send_tiktok_beta_success_report(
    source_handle: str,
    account_id: str,
    tiktok_user: str,
    vpn_region: str,
    profile_name: str,
    shorts_title: str,
    quality_str: str,
    duration: float,
    check_status: str,
    caption_text: str,
    youtube_url: str
):
    """
    Gửi thông báo ĐĂNG VIDEO THÀNH CÔNG theo đúng mẫu quy chuẩn:
    🎉 [TIKTOK BETA] ĐĂNG VIDEO THÀNH CÔNG!
    """
    bot = get_bot()
    if not bot:
        return
    cfg = load_config()
    chat_id = cfg.get("telegram_chat_id", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        return

    now_str = datetime.now().strftime("%H:%M:%S - %d/%m/%Y")
    safe_channel = html.escape(str(source_handle or "Kênh nguồn"))
    safe_acc = html.escape(str(account_id or "acc_n/a"))
    safe_user = html.escape(str(tiktok_user or "N/A"))
    safe_vpn = html.escape(str(vpn_region or "VN"))
    safe_profile = html.escape(str(profile_name or "Default"))
    safe_title = html.escape(str(shorts_title or ""))
    safe_quality = html.escape(str(quality_str or ""))
    safe_check = html.escape(str(check_status or "").split("\n")[0].strip())
    safe_caption = html.escape(str(caption_text or ""))

    msg = (
        f"🎉 <b>[TIKTOK BETA] ĐĂNG VIDEO THÀNH CÔNG!</b>\n\n"
        f"👤 <b>Kênh nhận:</b> {safe_channel} ({safe_acc})\n"
        f"🎵 <b>TikTok User:</b> {safe_user}\n"
        f"🌐 <b>Vùng VPN:</b> {safe_vpn} | <b>Profile:</b> {safe_profile}\n"
        f"----------------------------------------\n"
        f"🎬 <b>Tiêu đề Shorts:</b> {safe_title}\n"
        f"📐 <b>Chất lượng thực tế:</b> {safe_quality}\n"
        f"⏱ <b>Thời lượng:</b> {duration}s (Chuẩn ≤ 59s)\n"
        f"🛡 <b>Kiểm tra:</b> {safe_check}\n"
        f"⏰ <b>Thời gian đăng:</b> {now_str}\n\n"
        f"📝 <b>Caption đăng tải:</b>\n"
        f"<blockquote>{safe_caption}</blockquote>\n\n"
        f"🔗 <a href=\"{youtube_url}\">Xem video gốc trên YouTube</a>\n\n"
        f"🤖 <i>Thông báo tự động từ Hệ thống Nuôi Kênh TikTok Beta</i>"
    )
    markup = InlineKeyboardMarkup()
    if youtube_url:
        markup.add(InlineKeyboardButton("🔗 Xem video gốc trên YouTube", url=youtube_url))
    try:
        bot.send_message(chat_id, msg, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        print(f"[Telegram Success Report Error]: {e}")
        try:
            import re
            clean_text = re.sub(r"<[^>]+>", "", msg)
            bot.send_message(chat_id, clean_text, reply_markup=markup, disable_web_page_preview=True, parse_mode=None)
        except Exception:
            pass

def send_daily_kpi_report(target_date: str = None):
    """
    Thống kê tiến độ cuối ngày (22:01) chia 2 nhóm:
    - Kênh đã đạt chỉ tiêu (2/2 video)
    - Kênh chưa đủ chỉ tiêu (0/2 video, thiếu, tạm dừng)
    - Nút duyệt đăng bù trực tiếp
    """
    bot = get_bot()
    if not bot:
        return
    cfg = load_config()
    chat_id = cfg.get("telegram_chat_id", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        return

    summary = get_all_channels_kpi_summary(target_date)
    total = summary["total"]
    completed = summary["completed"]
    incomplete = summary["incomplete"]
    total_missing = summary["total_missing"]

    comp_lines = [f"✅ <b>KÊNH ĐÃ ĐẠT CHỈ TIÊU ({len(completed)}/{total}):</b>"]
    if completed:
        for c in completed:
            comp_lines.append(f"• {c['source_handle']}: {c['posted']}/{c['daily_target']} video")
    else:
        comp_lines.append("• <i>Chưa có kênh nào đạt chỉ tiêu hôm nay.</i>")

    incomp_lines = [f"\n⚠️ <b>KÊNH CHƯA ĐỦ CHỈ TIÊU ({len(incomplete)}/{total}):</b>"]
    if incomplete:
        for c in incomplete:
            paused_str = " (Đang tạm dừng)" if c.get("status") == "PAUSED" else ""
            incomp_lines.append(f"• {c['source_handle']}: {c['posted']}/{c['daily_target']} video (👉 Thiếu {c['missing']}){paused_str}")
    else:
        incomp_lines.append("• <i>Không có kênh nào thiếu chỉ tiêu!</i>")

    msg_parts = [
        "\n".join(comp_lines),
        "\n".join(incomp_lines),
        "________________________________________\n",
        f"📌 <b>Tổng kết:</b> Cần đăng bù <b>{total_missing}</b> video cho <b>{len(incomplete)}</b> kênh.\n",
        "👉 <b>Bạn có muốn duyệt đăng bù cho các kênh này ngay bây giờ không?</b>\n"
        "(Bấm nút bên dưới hoặc gõ <code>/bu1</code>, <code>/duyet</code>, <code>OK</code>)"
    ]
    full_msg = "\n".join(msg_parts)

    markup = InlineKeyboardMarkup(row_width=1)
    if total_missing > 0:
        markup.add(
            InlineKeyboardButton("🚀 DUYỆT BÙ 1 VIDEO / KÊNH", callback_data="kpi:bu1"),
            InlineKeyboardButton("⚡️ DUYỆT BÙ ĐỦ 2 VIDEO (TẤT CẢ)", callback_data="kpi:bu_all"),
            InlineKeyboardButton("❌ BỎ QUA HÔM NAY", callback_data="kpi:skip")
        )
    try:
        bot.send_message(chat_id, full_msg, reply_markup=markup if total_missing > 0 else None)
    except Exception as e:
        print(f"[Telegram KPI Report Error]: {e}")

def send_daily_audit_report(target_date: str = None):
    """
    Báo cáo kiểm định video đã đăng (22:03) qua Bot TikTok Check Video:
    Views, Likes, Shadowban, Trùng lặp, Vi phạm.
    """
    bot = get_bot()
    if not bot:
        return
    cfg = load_config()
    chat_id = cfg.get("telegram_chat_id", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        return

    audit = audit_daily_videos(target_date)
    grouped = audit["grouped"]
    has_error = audit["has_error"]
    error_details = audit["error_details"]

    lines = ["🛡 <b>TÌNH TRẠNG KIỂM ĐỊNH VIDEO ĐÃ ĐĂNG</b>\n"]
    if not grouped:
        lines.append("ℹ️ <i>Hôm nay chưa có video nào được đăng để kiểm định.</i>")
    else:
        for channel_key, vids in grouped.items():
            lines.append(f"👤 <b>{channel_key}:</b>")
            for idx, v in enumerate(vids, 1):
                shadow_status = "✅ Không (Đề xuất FYP)" if not v["is_shadowban"] else "❌ Có (Bị chặn FYP)"
                dup_status = "✅ An toàn" if not v["is_duplicate"] else "❌ Bị trùng lặp"
                violation_status = "✅ Hợp lệ" if not v["is_violation"] else "❌ Vi phạm chính sách"
                time_label = v.get("created_at", "")
                lines.append(
                    f"🎬 #{idx}: <b>{html.escape(v['title'])}</b> ({time_label})\n"
                    f"  • 👁 {v['views']} Views | ❤️ {v['likes']} Likes\n"
                    f"  • 🚫 Shadowban: {shadow_status}\n"
                    f"  • 🚫 Trùng lặp: {dup_status}\n"
                    f"  • 🚫 Vi phạm: {violation_status}"
                )
            lines.append("")

    lines.append("________________________________________\n")
    if not has_error:
        lines.append("🎉 <b>100% VIDEO AN TOÀN TUYỆT ĐỐI!</b>\nKhông có video nào bị Shadowban hay vi phạm.")
    else:
        lines.append("⚠️ <b>CẢNH BÁO KIỂM ĐỊNH PHÁT HIỆN LỖI:</b>\n" + "\n".join(error_details))

    try:
        bot.send_message(chat_id, "\n".join(lines))
    except Exception as e:
        print(f"[Telegram Audit Report Error]: {e}")

def send_telegram_report_with_buttons(
    video_id: str,
    action: str,
    source_channel: str,
    tiktok_user: str,
    vpn_region: str,
    profile_name: str,
    shorts_title: str,
    quality_str: str,
    duration: float,
    check_status: str,
    caption_text: str,
    youtube_url: str,
    yt_status: str = "NONE",
    yt_video_url: str = ""
):
    """Gửi thông báo định dạng HTML chuẩn kèm bàn phím tương tác 2 chiều về Telegram."""
    bot = get_bot()
    if not bot:
        return

    cfg = load_config()
    chat_id = cfg.get("telegram_chat_id", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        return

    now_str = datetime.now().strftime("%H:%M:%S - %d/%m/%Y")
    safe_channel = html.escape(str(source_channel or "Kênh nguồn"))
    safe_user = html.escape(str(tiktok_user or "N/A"))
    safe_title = html.escape(str(shorts_title or ""))
    safe_caption = html.escape(str(caption_text or ""))
    safe_vpn = html.escape(str(vpn_region or "VN"))
    safe_profile = html.escape(str(profile_name or "Default"))
    safe_quality = html.escape(str(quality_str or ""))

    # Lọc bỏ lỗi dài, lấy dòng đầu tiên và escape HTML an toàn cho Telegram
    raw_check = str(check_status or "").split("\n")[0].strip()
    if len(raw_check) > 150:
        raw_check = raw_check[:147] + "..."
    safe_check_status = html.escape(raw_check)
    safe_yt_status = html.escape(str(yt_status or ""))

    if action == "PUBLISHED" or yt_status == "PUBLISHED":
        header = "🎉 <b>[AUTO ĐĂNG BÀI] HOÀN TẤT XỬ LÝ VIDEO!</b>"
    elif action == "DRAFTED":
        header = "⚠️ <b>[AUTO ĐĂNG BÀI] CẢNH BÁO TIKTOK - ĐÃ LƯU NHÁP!</b>"
    else:
        header = "ℹ️ <b>[AUTO ĐĂNG BÀI] BÁO CÁO TIẾN TRÌNH</b>"

    tt_line = ""
    if tiktok_user:
        shield = "🛡️ " if action == "PUBLISHED" else ("🚨 " if action == "DRAFTED" else "⏳ ")
        tt_line = f"🎵 <b>TikTok ({safe_user}):</b> {shield}{safe_check_status}\n"

    yt_line = ""
    if yt_status != "NONE":
        yt_shield = "🎬 " if yt_status == "PUBLISHED" else "⚠️ "
        yt_line = f"📺 <b>YouTube Studio:</b> {yt_shield}{safe_yt_status}\n"
        if yt_video_url:
            yt_line += f"   👉 <a href=\"{yt_video_url}\">Link Shorts đã đăng</a>\n"

    message_html = (
        f"{header}\n\n"
        f"👤 <b>Kênh nguồn:</b> {safe_channel}\n"
        f"🌐 <b>Vùng VPN:</b> {safe_vpn} | <b>Profile:</b> {safe_profile}\n"
        f"----------------------------------------\n"
        f"🎬 <b>Tiêu đề Shorts:</b> {safe_title}\n"
        f"📐 <b>Chất lượng thực tế:</b> {safe_quality}\n"
        f"⏱️ <b>Thời lượng:</b> {duration}s (Chuẩn ≤ 59s)\n"
        f"----------------------------------------\n"
        f"{tt_line}"
        f"{yt_line}"
        f"⏰ <b>Thời gian đăng:</b> {now_str}\n\n"
        f"📝 <b>Caption đăng tải:</b>\n"
        f"<blockquote>{safe_caption}</blockquote>\n\n"
        f"🔗 <a href=\"{youtube_url}\">Xem video gốc nguồn</a>\n\n"
        f"🤖 <i>Hệ thống Auto Đăng Bài TikTok Beta & YouTube Shorts</i>"
    )
    keyboard = build_action_keyboard(video_id, action, yt_video_url)
    try:
        bot.send_message(
            chat_id=chat_id,
            text=message_html,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception as e:
        print(f"[Telegram Send Error]: {e}")
        try:
            import re
            clean_text = re.sub(r"<[^>]+>", "", message_html)
            bot.send_message(
                chat_id=chat_id,
                text=clean_text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
                parse_mode=None
            )
        except Exception:
            pass

def _register_handlers(bot: telebot.TeleBot):
    @bot.callback_query_handler(func=lambda call: True)
    def handle_inline_buttons(call):
        data = call.data
        if data.startswith("pub:"):
            video_id = data.split(":", 1)[1]
            record = get_video_record(video_id)
            if not record:
                bot.answer_callback_query(call.id, "❌ Không tìm thấy dữ liệu video!", show_alert=True)
                return

            bot.answer_callback_query(call.id, f"⏳ Đang mở Camoufox Profile {record['profile_name']} để đăng nháp...")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

            def _publish_task():
                ok = force_publish_draft(record)
                if ok:
                    bot.send_message(call.message.chat.id, f"✅ Đã đăng thành công bản nháp video <b>{record['shorts_title']}</b> lên TikTok!")
                else:
                    bot.send_message(call.message.chat.id, f"❌ Không thể đăng bản nháp. Vui lòng kiểm tra lại trình duyệt!")

            threading.Thread(target=_publish_task, daemon=True).start()

        elif data.startswith("del:"):
            video_id = data.split(":", 1)[1]
            record = get_video_record(video_id)
            if not record:
                bot.answer_callback_query(call.id, "❌ Không tìm thấy dữ liệu video!", show_alert=True)
                return

            bot.answer_callback_query(call.id, f"⏳ Đang xóa file & video trên TikTok...")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)

            def _delete_task():
                ok = delete_tiktok_video_and_file(record)
                if ok:
                    bot.send_message(call.message.chat.id, f"🗑️ Đã xóa video <b>{record['shorts_title']}</b> và file 4K thành công!")
                else:
                    bot.send_message(call.message.chat.id, f"⚠️ Đã xóa dữ liệu cục bộ nhưng có lỗi khi xóa trên TikTok Studio.")

            threading.Thread(target=_delete_task, daemon=True).start()

        elif data == "kpi:bu1":
            bot.answer_callback_query(call.id, "🚀 Đang kích hoạt duyệt bù 1 video / kênh...")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            res = execute_compensation("bu1")
            bot.send_message(call.message.chat.id, f"🚀 <b>ĐÃ DUYỆT BÙ 1 VIDEO / KÊNH!</b>\nĐang bắt đầu xử lý {res.get('total_planned', 0)} video cho các kênh còn thiếu...")

        elif data == "kpi:bu_all":
            bot.answer_callback_query(call.id, "⚡️ Đang kích hoạt duyệt bù đủ chỉ tiêu cho tất cả kênh...")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            res = execute_compensation("bu_all")
            bot.send_message(call.message.chat.id, f"⚡️ <b>ĐÃ DUYỆT BÙ ĐỦ 2 VIDEO (TẤT CẢ)!</b>\nĐang bắt đầu xử lý {res.get('total_planned', 0)} video cho toàn bộ kênh còn thiếu...")

        elif data == "kpi:skip":
            bot.answer_callback_query(call.id, "❌ Đã hủy lịch đăng bù hôm nay.")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(call.message.chat.id, "❌ <i>Đã ghi nhận: Bỏ qua lịch đăng bù hôm nay.</i>")

        elif data == "cmd:list_channels":
            channels = get_all_channels()
            if not channels:
                bot.answer_callback_query(call.id, "Danh sách trống!")
                bot.send_message(call.message.chat.id, "📋 Chưa có kênh nào được cấu hình theo dõi.")
                return

            lines = ["📋 <b>DANH SÁCH KÊNH ĐANG THEO DÕI:</b>\n"]
            for idx, ch in enumerate(channels, 1):
                lines.append(
                    f"{idx}. <b>{ch.get('profile_name', 'NoProfile')}</b> ({ch.get('vpn_region', 'VN')})\n"
                    f"   🔗 {ch.get('youtube_shorts_url')}\n"
                    f"   👤 TikTok: {ch.get('tiktok_user', 'N/A')} | Ngách: {ch.get('niche', 'N/A')}\n"
                )
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "\n".join(lines), disable_web_page_preview=True)

        elif data == "cmd:add_guide":
            bot.answer_callback_query(call.id)
            guide = (
                "➕ <b>HƯỚNG DẪN THÊM KÊNH TỰ ĐỘNG QUA TELEGRAM:</b>\n\n"
                "Hãy gửi tin nhắn theo đúng cú pháp sau:\n"
                "<code>/add [Link_YouTube_Shorts] | [TikTok_User] | [Vùng_VPN] | [Tên_Profile] | [Proxy_Server] | [Ngách]</code>\n\n"
                "<i>Ví dụ:</i>\n"
                "<code>/add https://youtube.com/@RedBull/shorts | @pilot_pov | US | US_01 | | Stunt Aviation POV</code>"
            )
            bot.send_message(call.message.chat.id, guide, disable_web_page_preview=True)

    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        bot.reply_to(
            message,
            "🤖 <b>Chào mừng bạn đến với Hệ thống Auto Đăng Bài Camoufox Antidetect!</b>\n\n"
            "Các lệnh hỗ trợ:\n"
            "• /channels - Xem danh sách các kênh đang theo dõi\n"
            "• /status - Xem trạng thái hệ thống\n"
            "• /add [thông tin kênh] - Thêm kênh quét tự động\n"
        )

    @bot.message_handler(commands=['bu1'])
    def cmd_bu1(message):
        res = execute_compensation("bu1")
        bot.reply_to(message, f"🚀 <b>Đã kích hoạt bù 1 video / kênh!</b> Đang bắt đầu xử lý {res.get('total_planned', 0)} video...")

    @bot.message_handler(commands=['duyet'])
    def cmd_duyet(message):
        res = execute_compensation("bu_all")
        bot.reply_to(message, f"⚡️ <b>Đã kích hoạt bù đủ toàn bộ chỉ tiêu!</b> Đang bắt đầu xử lý {res.get('total_planned', 0)} video...")

    @bot.message_handler(commands=['kpi'])
    def cmd_kpi(message):
        send_daily_kpi_report()

    @bot.message_handler(commands=['kiemdinh', 'check'])
    def cmd_audit(message):
        send_daily_audit_report()

    @bot.message_handler(func=lambda msg: msg.text and msg.text.strip().upper() in ["OK", "ĐỒNG Ý", "DONG Y", "DUYET"])
    def cmd_text_ok(message):
        res = execute_compensation("bu_all")
        bot.reply_to(message, f"⚡️ <b>Lệnh xác nhận: Đã kích hoạt duyệt bù toàn bộ!</b> Đang bắt đầu xử lý {res.get('total_planned', 0)} video...")

    @bot.message_handler(commands=['channels'])
    def list_ch_cmd(message):
        channels = get_all_channels()
        if not channels:
            bot.reply_to(message, "📋 Chưa có kênh nào trong hệ thống.")
            return
        lines = ["📋 <b>DANH SÁCH KÊNH THEO DÕI:</b>\n"]
        for idx, ch in enumerate(channels, 1):
            lines.append(f"{idx}. <b>{ch.get('profile_name')}</b>: {ch.get('youtube_shorts_url')}")
        bot.reply_to(message, "\n".join(lines), disable_web_page_preview=True)

    @bot.message_handler(commands=['add'])
    def add_ch_cmd(message):
        text = message.text.replace("/add", "").strip()
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 4:
            bot.reply_to(message, "❌ Cú pháp chưa đúng! Gửi <code>/add [Link_YT] | [TikTok_User] | [VPN] | [Profile] | [Proxy] | [Niche]</code>")
            return
        yt_url = parts[0]
        tt_user = parts[1]
        vpn = parts[2]
        profile = parts[3]
        proxy = parts[4] if len(parts) > 4 else ""
        niche = parts[5] if len(parts) > 5 else ""
        try:
            add_tracked_channel(yt_url, tt_user, vpn, profile, "", proxy, niche)
            bot.reply_to(message, f"✅ Đã thêm kênh <b>{yt_url}</b> cho Profile <b>{profile}</b>!")
        except Exception as e:
            bot.reply_to(message, f"❌ Lỗi: {e}")

_tele_polling_thread = None

def start_telegram_polling_thread():
    global _tele_polling_thread
    bot = get_bot()
    if not bot:
        return None
    if _tele_polling_thread is not None and _tele_polling_thread.is_alive():
        return _tele_polling_thread

    def _poll():
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e:
            print(f"[Telegram Polling Stopped]: {e}")

    _tele_polling_thread = threading.Thread(target=_poll, daemon=True)
    _tele_polling_thread.start()
    return _tele_polling_thread

