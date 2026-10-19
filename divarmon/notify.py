"""Telegram alert composition and delivery."""

import html
import io
import random
import time

import telebot
from telebot.types import InputMediaPhoto

from . import log, net
from .divar import download_image, fetch_post_detail, format_time_ago
from .state import ARROW, FSI, PDI, iso, parse_price_number, rtl


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))

logger = log.get(__name__)

TELEGRAM_RETRY_DELAY = 2.0
MAX_ALBUM_IMAGES = 10


FA_DIGIT_MAP = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa(text) -> str:
    return str(text).translate(FA_DIGIT_MAP)


def fa_money(n: int | None) -> str:
    """20000000 -> '۲۰م', 1500000000 -> '۱.۵میلیارد'."""
    if n is None:
        return "—"
    if n >= 1_000_000_000:
        value, suffix = n / 1_000_000_000, "میلیارد"
    elif n >= 1_000_000:
        value, suffix = n / 1_000_000, "م"
    elif n >= 1_000:
        value, suffix = n / 1_000, "هزار"
    else:
        return to_fa(n)
    return to_fa(f"{value:.1f}".rstrip("0").rstrip(".")) + suffix


def compact_price(raw: str) -> str:
    """'ودیعه: ۲۰۰,۰۰۰,۰۰۰ تومان' -> 'ودیعه ۲۰۰م'. Non-numeric text is kept."""
    if not raw:
        return ""
    label, sep, rest = raw.partition(":")
    if not sep:
        label, rest = "", raw
    number = parse_price_number(rest)
    if number is None:
        return raw.replace("تومان", "").strip()
    return f"{label.strip()} {fa_money(number)}".strip()


def _yes_no(value) -> str:
    return "✅" if value else "❌"


def fmt_floor(value: str) -> str:
    """'۳' -> 'طبقه ۳', '۳ از ۵' -> 'طبقه ۳ از ۵', 'همکف' -> 'همکف'."""
    value = (value or "").strip()
    if not value:
        return ""
    first = value[0]
    return f"طبقه {value}" if first.isdigit() or first in "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩" else value


def clean_agent(agent: str) -> str:
    """Divar reuses the listing's bottom line for the agency name, but on
    personal ads it holds a timestamp ('دقایقی پیش در بلوار کشاورز') that the
    ⏰ and 📍 fields already cover. Agency names carry the same trailing
    ' در <محله>' — also already on the 📍 line."""
    agent = (agent or "").strip()
    if not agent or "پیش" in agent:
        return ""
    return agent.split(" در ")[0].strip()


def build_money_line(listing: dict) -> str:
    """'💰 ودیعه ۴۰۰م · اجاره ۳۰م' — prices get a line of their own."""
    parts = [compact_price(listing.get("deposit", "")),
             compact_price(listing.get("price", ""))]
    money = " · ".join(p for p in parts if p)
    return f"💰 {money}" if money else ""


def build_slider_line(detail: dict) -> str:
    """'🔄 قابل تبدیل: ودیعه ۵۰۰م · اجاره ۲۵م' — the slider's other end."""
    if not detail.get("convertible"):
        return ""
    alt_c, alt_r = detail.get("credit_alt_num"), detail.get("rent_alt_num")
    if alt_c is None and alt_r is None:
        return ""
    base_c, base_r = detail.get("credit_num"), detail.get("rent_num")
    if alt_c == base_c and alt_r == base_r:
        return ""
    parts = []
    if alt_c is not None:
        parts.append(f"ودیعه {fa_money(alt_c)}")
    if alt_r is not None:
        parts.append(f"اجاره {fa_money(alt_r)}")
    return f"🔄 قابل تبدیل: {' · '.join(parts)}" if parts else ""


# Shown in this order, each with ✅/❌ so a missing amenity is stated outright
# instead of silently left out.
AMENITIES = (("پارکینگ", "🚗"), ("آسانسور", "🛗"), ("انباری", "📦"), ("بالکن", "🌇"))


def build_spec_line(detail: dict) -> str:
    """'📐 ۴۵متر · 🛏 ۱خواب · 🏢 همکف'."""
    parts = []
    if detail.get("size"):
        parts.append(f"📐 {detail['size']}متر")
    if detail.get("rooms"):
        parts.append(f"🛏 {detail['rooms']}خواب")
    floor = fmt_floor(detail.get("floor", ""))
    if floor:
        parts.append(f"🏢 {floor}")
    return " · ".join(parts)


def build_feature_line(detail: dict) -> str:
    """'🚗 پارکینگ ✅ · 🛗 آسانسور ❌ · 📦 انباری ✅'."""
    features = detail.get("features") or {}
    parts = [f"{icon} {name} {_yes_no(features[name])}"
             for name, icon in AMENITIES if name in features]
    return " · ".join(parts)


def build_price_line(price_change: dict | None) -> str:
    """'💱 اجاره ۳۰م ⬅️ ۲۸م (🔻۶.۷٪)' — both amounts on one line."""
    if not price_change:
        return ""
    chunks = []
    for label, old_raw, new_raw, old_num, new_num in (
        ("ودیعه", price_change.get("deposit_old_raw"), price_change.get("deposit_new_raw"),
         price_change.get("deposit_old_num"), price_change.get("deposit_new_num")),
        ("اجاره", price_change.get("price_old_raw"), price_change.get("price_new_raw"),
         price_change.get("price_old_num"), price_change.get("price_new_num")),
    ):
        if (old_raw or "") == (new_raw or ""):
            continue
        old_txt = fa_money(old_num) if old_num else (compact_price(old_raw) or "—")
        new_txt = fa_money(new_num) if new_num else (compact_price(new_raw) or "—")
        chunk = f"{label} {iso(old_txt)} {ARROW} {iso(new_txt)}"
        if old_num and new_num and old_num != new_num:
            pct = (new_num - old_num) / abs(old_num) * 100
            arrow = "🔺" if new_num > old_num else "🔻"
            chunk += f" ({arrow}{to_fa(f'{abs(pct):.1f}'.rstrip('0').rstrip('.'))}٪)"
        chunks.append(chunk)
    return rtl(f"💱 {' · '.join(chunks)}") if chunks else ""


def build_caption(listing: dict, detail: dict, kind: str = "new",
                  price_change: dict | None = None, search_name: str = "",
                  change_lines: list | None = None) -> str:
    """HTML caption as separated blocks: title, prices, specs, what changed,
    where/when, search, link. One blank line between each so it stays readable
    on a phone."""
    blocks = [rtl(f"{'♻️' if kind == 'updated' else '🆕'} "
                  f"<b>{FSI}{esc(listing.get('title', ''))}{PDI}</b>")]

    money_line = build_money_line(listing)
    if money_line:
        blocks.append(rtl(esc(money_line)))

    slider_line = build_slider_line(detail)
    if slider_line:
        blocks.append(rtl(esc(slider_line)))

    if kind == "updated":
        price_line = build_price_line(price_change)
        if price_line:
            blocks.append(esc(price_line))
        if change_lines:
            blocks.append("\n".join(esc(line) for line in change_lines))
        elif not price_line:
            blocks.append(rtl("✏️ آگهی ویرایش شد"))

    spec_line = build_spec_line(detail)
    if spec_line:
        blocks.append(rtl(esc(spec_line)))

    feature_line = build_feature_line(detail)
    if feature_line:
        blocks.append(rtl(esc(feature_line)))

    time_ago = detail.get("time_ago") or format_time_ago(listing.get("sort_date", ""))
    where = [f"📍 {iso(listing.get('location', ''))}"]
    if time_ago:
        where.append(f"⏰ {iso(time_ago)}")
    agent = clean_agent(listing.get("agent", ""))
    if agent:
        where.append(f"👤 {iso(agent)}")
    blocks.append(rtl(esc(" · ".join(where))))

    if search_name:
        blocks.append(rtl(esc(f"🔎 {iso(search_name)}")))
    blocks.append(f'🔗 <a href="{esc(listing["web_url"])}">{esc(listing["web_url"])}</a>')
    return "\n\n".join(blocks)


def send_with_retry(bot: telebot.TeleBot, func, max_retries: int = net.MAX_RETRIES, *args, **kwargs):
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except telebot.apihelper.ApiTelegramException as e:
            msg = str(e)
            if "429" in msg or "Too Many Requests" in msg or getattr(e, "error_code", None) == 429:
                retry_after = (getattr(e, "result_json", {}) or {}).get("parameters", {}).get(
                    "retry_after", TELEGRAM_RETRY_DELAY)
                try:
                    retry_after = int(retry_after)
                except Exception:
                    retry_after = TELEGRAM_RETRY_DELAY
                logger.warning(f"Telegram 429, retry_after {retry_after}s (attempt {attempt+1}/{max_retries})")
                time.sleep(retry_after + random.uniform(0, 1))
                continue
            if getattr(e, "error_code", None) == 400:
                raise
            logger.warning(f"Telegram API error attempt {attempt+1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                time.sleep(net.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1))
            else:
                raise
        except Exception as e:
            logger.warning(f"Telegram send error attempt {attempt+1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                time.sleep(net.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1))
            else:
                raise
    raise RuntimeError("Telegram retries exhausted")


def notify_text(bot: telebot.TeleBot, chat_ids, text: str, max_retries: int = net.MAX_RETRIES,
                markup=None, parse_mode: str = "Markdown"):
    """Broadcast a plain message; never raises."""
    for chat_id in chat_ids:
        try:
            send_with_retry(bot, bot.send_message, max_retries, chat_id=chat_id, text=text,
                            parse_mode=parse_mode, reply_markup=markup,
                            disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to message {chat_id}: {e}")


def send_listing_alert(bot: telebot.TeleBot, listing: dict, chat_ids: list,
                       kind: str = "new", price_change: dict | None = None,
                       search_name: str = "", max_retries: int = net.MAX_RETRIES,
                       detail: dict | None = None, change_lines: list | None = None):
    if detail is None:
        detail = fetch_post_detail(listing["token"], max_retries=max_retries)
    caption = build_caption(listing, detail, kind=kind, price_change=price_change,
                            search_name=search_name, change_lines=change_lines)

    images = detail.get("images", []) or ([listing["image_url"]] if listing.get("image_url") else [])
    seen, uniq = set(), []
    for u in images:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
        if len(uniq) >= MAX_ALBUM_IMAGES:
            break
    images = uniq

    # Download once, reuse the bytes for every recipient.
    blobs = [b for b in (download_image(u) for u in images) if b]

    for chat_id in chat_ids:
        try:
            if not blobs:
                if images:
                    # Let Telegram fetch the URL itself as a last resort.
                    try:
                        send_with_retry(bot, bot.send_photo, max_retries, chat_id=chat_id,
                                        photo=images[0], caption=caption, parse_mode="HTML")
                        continue
                    except telebot.apihelper.ApiTelegramException as e:
                        if getattr(e, "error_code", None) != 400:
                            raise
                        logger.warning(f"Photo 400 for {listing['token']}, text fallback for {chat_id}")
                send_with_retry(bot, bot.send_message, max_retries, chat_id=chat_id,
                                text=caption, parse_mode="HTML")
            elif len(blobs) == 1:
                send_with_retry(bot, bot.send_photo, max_retries, chat_id=chat_id,
                                photo=io.BytesIO(blobs[0]), caption=caption, parse_mode="HTML")
            else:
                media = []
                for idx, blob in enumerate(blobs):
                    bio = io.BytesIO(blob)
                    bio.name = f"image_{idx}.jpg"
                    media.append(InputMediaPhoto(media=bio, caption=caption, parse_mode="HTML")
                                 if idx == 0 else InputMediaPhoto(media=bio))
                send_with_retry(bot, bot.send_media_group, max_retries, chat_id=chat_id, media=media)
        except telebot.apihelper.ApiTelegramException as e:
            if getattr(e, "error_code", None) == 400:
                try:
                    logger.warning(f"Album/photo 400 for {listing['token']} -> text fallback for {chat_id}")
                    send_with_retry(bot, bot.send_message, max_retries, chat_id=chat_id,
                                    text=caption, parse_mode="HTML")
                except Exception as e2:
                    logger.error(f"Fallback text to {chat_id} for {listing['token']} failed: {e2}")
            else:
                logger.error(f"Error sending to {chat_id} for {listing['token']}: {e}")
        except Exception as e:
            logger.error(f"Error sending to {chat_id} for {listing['token']}: {e}")
