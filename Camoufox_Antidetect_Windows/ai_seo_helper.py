import os
import re
import json
from pydantic import BaseModel, Field

# Thử import Google GenAI
try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

from platform_utils import load_config

# Bảng ánh xạ mã quốc gia VPN sang ngôn ngữ và phong cách viết bản địa
REGION_LANGUAGE_MAP = {
    "VN": "Vietnamese (tự nhiên, bắt trend TikTok/Shorts Việt Nam, hấp dẫn)",
    "US": "American English (viral, punchy TikTok/Shorts tone, high retention)",
    "UK": "British English (natural UK TikTok/Shorts tone)",
    "MX": "Spanish (Mexican slang/natural tone - Español de México)",
    "ES": "Spanish (Spain natural tone - Español de España)",
    "FR": "French (natural Metropolitan French TikTok slang)",
    "DE": "German (natural Deutsch TikTok tone)",
    "BR": "Portuguese (Brazilian Portuguese - Português do Brasil)",
    "JP": "Japanese (natural Japanese SNS/TikTok style)",
    "KR": "Korean (natural Korean Shorts/TikTok style)"
}

class TikTokCaptionSchema(BaseModel):
    hook_caption: str = Field(description="Câu dẫn ngắn dưới 120 ký tự bằng ngôn ngữ bản địa của vùng VPN.")
    hashtags: list[str] = Field(description="10-12 hashtag viết thường, kết hợp 70% ngôn ngữ bản địa và 30% tiếng Anh.")

class VideoSEOMetadataSchema(BaseModel):
    title: str = Field(description="Tiêu đề video giật tít, tò mò, chuẩn SEO YouTube Shorts & TikTok (dưới 90 ký tự, không xuống dòng).")
    hook_caption: str = Field(description="Câu mở đầu (hook) thu hút trong 3 giây đầu tiên bằng ngôn ngữ vùng mục tiêu (dưới 120 ký tự).")
    description: str = Field(description="Đoạn mô tả SEO chi tiết 2-4 câu giải thích nội dung video và có lời kêu gọi hành động (Subscribe, Follow, Like).")
    hashtags: list[str] = Field(description="10-15 hashtag viết thường có dấu # ở đầu, 70% ngôn ngữ bản địa và 30% tiếng Anh ngách toàn cầu.")

AVAILABLE_GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-flash-latest",
]

def generate_with_fallback(client, contents, config=None):
    last_err = None
    for model_name in AVAILABLE_GEMINI_MODELS:
        try:
            if config:
                return client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
            else:
                return client.models.generate_content(
                    model=model_name,
                    contents=contents
                )
        except Exception as e:
            last_err = e
            err_msg = str(e)
            if any(code in err_msg for code in ["503", "UNAVAILABLE", "404", "NOT_FOUND", "429", "RESOURCE_EXHAUSTED"]):
                print(f"[Gemini Auto-Fallback]: Model '{model_name}' overloaded/unavailable ({err_msg[:60]}). Switching to next model...")
                continue
            raise e
    raise last_err

def clean_filename_to_title(filename_or_topic: str) -> str:
    """Chuyển tên file video (VD: meo_nau_an_nhanh_v1.mp4) thành tiêu đề sạch dễ đọc."""
    base = os.path.splitext(os.path.basename(filename_or_topic))[0]
    # Thay thế gạch dưới, gạch ngang thành khoảng trắng
    clean = re.sub(r"[_\-]+", " ", base).strip()
    # Loại bỏ các từ khóa thừa như v1, 1080p, 4k, output, raw
    clean = re.sub(r"\b(v\d+|1080p|720p|4k|raw|edit|final|output)\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean.title() if clean else "Video Shorts"

def generate_localized_caption(original_title: str, vpn_region: str, niche_hint: str = "", fallback_hashtags: str = "") -> str:
    """Tạo caption & hashtag bản địa cho luồng tự động quét kênh."""
    cfg = load_config()
    api_key = cfg.get("gemini_api_key", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()
    target_region = vpn_region.upper() if vpn_region else "US"
    target_lang = REGION_LANGUAGE_MAP.get(target_region, f"the native language of country {target_region}")

    if not api_key or not HAS_GENAI:
        if fallback_hashtags:
            return f"{original_title}\n\n{fallback_hashtags}"
        return f"{original_title}\n\n#shorts #fyp #viral"

    try:
        client = genai.Client(api_key=api_key)
        prompt = f"""
Bạn là chuyên gia SEO TikTok Quốc tế. Hãy viết lại tiêu đề video YouTube Shorts sau thành caption đăng TikTok:
- Tiêu đề gốc: "{original_title}"
- Ngách nội dung (Niche): "{niche_hint if niche_hint else 'Tự nhận diện dựa trên tiêu đề'}"
- Vùng mục tiêu (VPN Region): {target_region} ({target_lang})

Yêu cầu:
1. Viết `hook_caption` bằng ngôn ngữ {target_lang}, ngắn gọn dưới 120 ký tự, hấp dẫn, giữ đúng ngữ cảnh video.
2. Tạo 10-12 `hashtags` bám sát nội dung: 70% hashtag bản địa theo ngôn ngữ {target_lang} và 30% hashtag tiếng Anh ngách toàn cầu.
"""
        response = generate_with_fallback(
            client,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TikTokCaptionSchema,
                temperature=0.7,
            ),
        )
        res: TikTokCaptionSchema = response.parsed
        clean_tags = [t.lower() if t.startswith("#") else f"#{t.lower()}" for t in res.hashtags]
        tags = " ".join(clean_tags)
        return f"{res.hook_caption}\n\n{tags}"
    except Exception as e:
        print(f"[Gemini Fallback]: {e}")
        if fallback_hashtags:
            return f"{original_title}\n\n{fallback_hashtags}"
        return f"{original_title}\n\n#shorts #viral #fyp"

def generate_seo_metadata(
    topic_or_filename: str,
    target_region: str = "VN",
    niche_hint: str = "",
    custom_notes: str = ""
) -> dict:
    """
    Sử dụng Gemini AI (gemini-3.8-flash) để tạo bộ dữ liệu SEO hoàn chỉnh:
    - Tiêu đề giật tít (<90 ký tự, tối ưu YouTube Shorts & TikTok)
    - Câu mở đầu hấp dẫn (Hook Caption)
    - Mô tả SEO chi tiết có lời kêu gọi hành động (Call To Action)
    - Danh sách 10-15 Hashtags chuẩn thuật toán
    Hỗ trợ cơ chế Fallback thông minh khi chưa có API Key hoặc mất mạng.
    """
    clean_topic = clean_filename_to_title(topic_or_filename)
    region_code = target_region.upper() if target_region else "VN"
    target_lang = REGION_LANGUAGE_MAP.get(region_code, f"ngôn ngữ quốc gia {region_code}")

    cfg = load_config()
    api_key = cfg.get("gemini_api_key", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()

    if api_key and HAS_GENAI:
        try:
            client = genai.Client(api_key=api_key)
            prompt = f"""
Bạn là chuyên gia Content Creator & Viral SEO hàng đầu thế giới cho TikTok và YouTube Shorts.
Hãy phân tích thông tin video dưới đây và sáng tạo bộ Metadata SEO hoàn chỉnh tối ưu thuật toán tìm kiếm và đề xuất FYP:
- Tên file / Chủ đề video: "{clean_topic}"
- Ngách nội dung (Niche): "{niche_hint if niche_hint else 'Tự động nhận diện theo chủ đề'}"
- Ghi chú phong cách: "{custom_notes if custom_notes else 'Tự nhiên, giật tít cuốn hút, giữ chân người xem'}"
- Vùng mục tiêu (VPN Region): {region_code} ({target_lang})

Yêu cầu chi tiết:
1. `title`: Tiêu đề giật tít, tò mò, ngắn gọn dưới 90 ký tự, không chứa ký tự xuống dòng.
2. `hook_caption`: Câu mở đầu (hook) cực kỳ lôi cuốn dưới 120 ký tự bằng ngôn ngữ {target_lang}.
3. `description`: Đoạn mô tả SEO chi tiết 2-4 câu, tóm tắt điểm hấp dẫn nhất của video và có lời kêu gọi hành động (Follow / Like / Đăng ký kênh).
4. `hashtags`: 10-15 hashtags viết thường bắt đầu bằng '#', bám sát chủ đề ({target_lang} 70%, tiếng Anh phổ quát 30%).
"""
            response = generate_with_fallback(
                client,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=VideoSEOMetadataSchema,
                    temperature=0.7,
                ),
            )
            res: VideoSEOMetadataSchema = response.parsed
            clean_tags = [t.lower() if t.startswith("#") else f"#{t.lower()}" for t in res.hashtags]

            return {
                "title": res.title.strip().replace("\n", " ")[:90],
                "hook_caption": res.hook_caption.strip(),
                "description": res.description.strip(),
                "hashtags": clean_tags
            }
        except Exception as e:
            print(f"[Gemini SEO Error -> Fallback]: {e}")

    # Fallback khi không có API hoặc có lỗi mạng
    return _build_fallback_seo(clean_topic, region_code)

def _build_fallback_seo(clean_topic: str, region_code: str) -> dict:
    """Sinh dữ liệu SEO dự phòng thông minh dựa trên tên file và vùng."""
    if region_code == "VN":
        fallback_title = f"{clean_topic} Cực Đỉnh | Xem Ngay Kẻo Lỡ"
        fallback_hook = f"Bạn đã biết điều kỳ diệu này chưa? Xem ngay {clean_topic}!"
        fallback_desc = f"Khám phá ngay video về {clean_topic}. Đừng quên nhấn Like, Follow và chia sẻ nếu thấy video này hữu ích nhé!"
        fallback_tags = [
            "#shorts", "#xuhuong", "#fyp", "#tiktokvietnam", "#viral",
            "#trending", "#videohot", "#giaitri", "#kienthuc", "#khampha"
        ]
    elif region_code in ("MX", "ES"):
        fallback_title = f"{clean_topic} ¡Increíble! Mira Esto"
        fallback_hook = f"¡No te vas a creer lo que pasó con {clean_topic}!"
        fallback_desc = f"Video completo sobre {clean_topic}. ¡No olvides darle Like y Suscribirte para más videos diarios!"
        fallback_tags = [
            "#shorts", "#parati", "#fyp", "#viral", "#tendencia",
            "#tiktokviral", "#mexico", "#shortsyoutube", "#explora"
        ]
    else:
        fallback_title = f"{clean_topic} - Mind Blowing Moments!"
        fallback_hook = f"Wait until the end! Check out {clean_topic}."
        fallback_desc = f"Must-see moments in {clean_topic}. Don't forget to Like, Share and Subscribe for daily updates!"
        fallback_tags = [
            "#shorts", "#viral", "#fyp", "#trending", "#foryou",
            "#reels", "#explore", "#trend", "#shortsvideo", "#daily"
        ]

    # Thêm tag riêng của topic nếu có
    topic_tag = re.sub(r"[^a-zA-Z0-9]", "", clean_topic.lower())
    if topic_tag and f"#{topic_tag}" not in fallback_tags:
        fallback_tags.insert(1, f"#{topic_tag}")

    return {
        "title": fallback_title[:90],
        "hook_caption": fallback_hook,
        "description": fallback_desc,
        "hashtags": fallback_tags
    }

class HashtagsOnlySchema(BaseModel):
    hashtags: list[str] = Field(description="10-12 hashtag viết thường có dấu '#', 70% ngôn ngữ vùng mục tiêu và 30% tiếng Anh.")

def _dedup_hashtags(raw_tags: list[str], existing_text: str = "") -> list[str]:
    """Chuẩn hóa định dạng #tag, loại bỏ trùng lặp và bỏ các tag đã có sẵn trong tiêu đề."""
    existing = set(re.findall(r"#[\w]+", existing_text.lower()))
    result = []
    for t in raw_tags:
        tag = t.strip().lower()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        if tag not in existing and tag not in result:
            result.append(tag)
    return result

def generate_localized_hashtags(original_title: str, target_region: str = "VN", niche_hint: str = "") -> list[str]:
    """Chỉ sinh bộ hashtag bản địa phù hợp với tiêu đề gốc và vùng mục tiêu."""
    clean_topic = clean_filename_to_title(original_title)
    region_code = target_region.upper() if target_region else "VN"
    target_lang = REGION_LANGUAGE_MAP.get(region_code, f"ngôn ngữ quốc gia {region_code}")

    cfg = load_config()
    api_key = cfg.get("gemini_api_key", "").strip() or os.getenv("GEMINI_API_KEY", "").strip()

    if api_key and HAS_GENAI:
        try:
            client = genai.Client(api_key=api_key)
            prompt = (
                f'Tạo 10-12 hashtag TikTok/Shorts phù hợp nhất cho video có tiêu đề "{original_title}" '
                f'(ngách: "{niche_hint or "tự nhận diện"}", vùng mục tiêu: {region_code} - {target_lang}). '
                f'Yêu cầu 70% hashtag bằng ngôn ngữ {target_lang} và 30% hashtag tiếng Anh phổ biến.'
            )
            response = generate_with_fallback(
                client,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=HashtagsOnlySchema,
                    temperature=0.6,
                ),
            )
            res: HashtagsOnlySchema = response.parsed
            return _dedup_hashtags(res.hashtags, original_title)
        except Exception as e:
            print(f"[Gemini Hashtags Fallback]: {e}")

    fallback_tags = _build_fallback_seo(clean_topic, region_code).get("hashtags", [])
    return _dedup_hashtags(fallback_tags, original_title)

def _is_missing_text(value: str) -> bool:
    """Kiểm tra tiêu đề/mô tả gốc có bị trống hoặc chỉ là chuỗi giữ chỗ mặc định hay không."""
    cleaned = (value or "").strip()
    if not cleaned:
        return True
    return cleaned.lower() in ("shorts video", "untitled", "none")

def build_caption_metadata(
    original_title: str,
    target_region: str = "VN",
    niche_hint: str = "",
    title_mode: str = "ai_rewrite",
    original_description: str = "",
    existing_hashtags: str | list[str] = "",
    fallback_topic: str = "",
) -> dict:
    """
    Tạo metadata đăng bài theo chế độ kênh:
    - 'keep_original': Giữ tiêu đề & mô tả gốc; nếu tiêu đề hoặc mô tả gốc bị trống sẽ tự động gọi AI viết bù phần còn thiếu.
    - 'ai_rewrite': AI viết lại hoàn toàn tiêu đề giật tít, mô tả SEO và hashtag bản địa.
    """
    clean_title = (original_title or "").strip().replace("\n", " ")
    clean_desc = (original_description or "").strip()
    has_title = not _is_missing_text(clean_title)
    has_desc = not _is_missing_text(clean_desc)

    if isinstance(existing_hashtags, list):
        manual_tags = _dedup_hashtags(existing_hashtags)
    else:
        manual_tags = _dedup_hashtags((existing_hashtags or "").split())

    mode = (title_mode or "ai_rewrite").strip().lower()
    if mode == "keep_original":
        # Trường hợp 1: Có đủ cả tiêu đề gốc và mô tả gốc -> giữ nguyên hoàn toàn
        if has_title and has_desc:
            tags = manual_tags or generate_localized_hashtags(
                f"{clean_title} {clean_desc}",
                target_region=target_region,
                niche_hint=niche_hint,
            )
            tags = _dedup_hashtags(tags, f"{clean_title} {clean_desc}")
            return {
                "title": clean_title,
                "hook_caption": "",
                "description": clean_desc,
                "hashtags": tags,
            }

        # Trường hợp 2: Thiếu tiêu đề gốc hoặc thiếu mô tả gốc -> tự động dùng AI viết bù phần thiếu
        ai_topic = clean_title if has_title else (clean_desc if has_desc else (fallback_topic or niche_hint or "Shorts Video"))
        ai_meta = generate_seo_metadata(
            topic_or_filename=ai_topic,
            target_region=target_region,
            niche_hint=niche_hint,
        )
        final_title = clean_title if has_title else ai_meta.get("title", "Shorts Video")
        final_desc = clean_desc if has_desc else ai_meta.get("description", "")
        raw_tags = manual_tags if manual_tags else ai_meta.get("hashtags", [])
        final_tags = _dedup_hashtags(raw_tags, f"{final_title} {final_desc}")
        return {
            "title": final_title,
            "hook_caption": "" if has_title else ai_meta.get("hook_caption", ""),
            "description": final_desc,
            "hashtags": final_tags,
        }

    topic_for_ai = clean_title if has_title else (clean_desc if has_desc else (fallback_topic or niche_hint or "Shorts Video"))
    return generate_seo_metadata(
        topic_or_filename=topic_for_ai,
        target_region=target_region,
        niche_hint=niche_hint,
    )

