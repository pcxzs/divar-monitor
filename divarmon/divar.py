"""Divar backend calls: post list search, post detail, image download.

The request shape here is the load-bearing part of the project — the
pagination cursor and the echoed form_data must be replayed verbatim or the
API answers 400.
"""

import hashlib
import random
import re
import time
from datetime import datetime, timezone

import requests

from . import log, net
from .search_url import DIVAR_API_URL, DIVAR_POST_DETAIL_URL, build_api_payload

logger = log.get(__name__)

EMPTY_DETAIL = {"images": [], "time_ago": "", "floor": "", "size": "", "rooms": "",
                "year": "", "features": {}, "credit_num": None, "rent_num": None,
                "credit_alt_num": None, "rent_alt_num": None, "convertible": False,
                "latitude": None, "longitude": None}

# "نیم ساعت پیش در تهران، یوسف‌آباد" -> "نیم ساعت پیش"
# (the location is already on its own line in the alert)
_TIME_LOCATION_RE = re.compile(r"\s+در\s+.*$")


def _strip_location(title: str) -> str:
    return _TIME_LOCATION_RE.sub("", (title or "").strip()).strip()


NEGATIVE_SUFFIX = "ندارد"


def normalize_feature(title: str, available=True) -> tuple[str, bool]:
    """Divar spells a missing amenity into the title and still sends
    `available: true` — "آسانسور ندارد" means *no* elevator. Turn that into
    ("آسانسور", False) so callers can just look the amenity up by name."""
    name = (title or "").strip()
    if name.endswith(NEGATIVE_SUFFIX):
        return name[: -len(NEGATIVE_SUFFIX)].strip(), False
    return name, bool(available)


def image_signature(images: list) -> str:
    """Stable id for a photo set: CDN hosts rotate, the file names do not."""
    names = sorted(u.rsplit("/", 1)[-1] for u in images if u)
    return f"{len(names)}:" + hashlib.blake2s("|".join(names).encode(), digest_size=8).hexdigest()


def fetch_listings(search_spec: dict, page_fetch_delay: float = 0.8,
                   max_retries: int = net.MAX_RETRIES, max_pages: int = 5) -> tuple[list, int]:
    """Walk the post list for one search link. Returns (listings, total_count)."""
    all_listings: list[dict] = []
    seen_tokens: set[str] = set()
    pagination_data = None
    form_data_override = None   # server-canonical filters echoed back (page 2+)
    total_count = 0
    page = 0
    consecutive_failures = 0
    name = search_spec.get("name", "?")

    while True:
        page += 1
        payload = build_api_payload(search_spec, pagination_data)
        if form_data_override is not None:
            # Divar validates paginated requests against the canonical filter
            # form it echoed in the previous response (with nested `value`
            # objects). Sending our original form triggers HTTP 400.
            payload["search_data"]["form_data"]["data"] = form_data_override
        logger.info(f"[{name}] Fetching page {page}...")

        try:
            resp = net.post_with_retry(DIVAR_API_URL, payload, net.DIVAR_HEADERS,
                                       timeout=30, max_retries=max_retries)
            data = resp.json()
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"[{name}] Failed to fetch page {page} after retries: {e}")
            if consecutive_failures >= 3:
                logger.error(f"[{name}] Too many consecutive page failures, returning what we have")
                break
            time.sleep(page_fetch_delay)
            if page > 1:
                break
            return all_listings, total_count

        consecutive_failures = 0
        total_count = data.get("map_data", {}).get("post_count", total_count)

        widgets = [w for w in data.get("list_widgets", []) if w.get("widget_type") == "POST_ROW"]
        logger.info(f"  [{name}] Page {page}: {len(widgets)} widgets (unique so far: {len(all_listings)})")

        for widget in widgets:
            try:
                wdata = widget.get("data", {})
                action_payload = wdata.get("action", {}).get("payload", {})
                web_info = action_payload.get("web_info", {})
                token = action_payload.get("token", "")
                if not token or token in seen_tokens:
                    continue
                seen_tokens.add(token)
                district = web_info.get("district_persian", "")
                city = web_info.get("city_persian", "")
                location = f"{district}, {city}" if district else city
                sort_date = (widget.get("action_log", {}).get("server_side_info", {})
                             .get("info", {}).get("sort_date", ""))
                all_listings.append({
                    "token": token,
                    "title": wdata.get("title", "بدون عنوان"),
                    "price": wdata.get("middle_description_text", "قیمت ذکر نشده"),
                    "deposit": wdata.get("top_description_text", ""),
                    "location": location or "موقعیت ذکر نشده",
                    "agent": wdata.get("bottom_description_text", ""),
                    "image_url": wdata.get("image_url", ""),
                    "image_count": wdata.get("image_count", 0),
                    "sort_date": sort_date,
                    "web_url": f"https://divar.ir/v/{token}",
                })
            except Exception as e:
                logger.warning(f"[{name}] Skipping malformed widget on page {page}: {e}")
                continue

        pag = data.get("pagination", {})
        if not pag.get("has_next_page"):
            logger.info(f"  [{name}] No more pages (has_next_page=false). Done at page {page}.")
            break
        if page >= max_pages:
            logger.info(f"  [{name}] Page cap reached ({max_pages}). Done at page {page}.")
            break

        # Replay the server-returned pagination cursor verbatim (it already
        # carries @type, last_post_date, search_uid, viewed_tokens,
        # search_bookmark_info, first_page_viewed_at, filters_hash, ...).
        # Do NOT increment page/layer_page: Divar paginates via the
        # last_post_date/viewed_tokens cursor and rejects altered counters
        # with HTTP 400.
        pagination_data = dict(pag.get("data", {}))
        pagination_data.setdefault("@type", "type.googleapis.com/post_list.PaginationData")
        echoed = data.get("search_data", {}).get("form_data", {}).get("data")
        if isinstance(echoed, dict) and echoed:
            form_data_override = echoed
        time.sleep(page_fetch_delay)

    return all_listings, total_count


def probe_search(url: str, max_retries: int = 2) -> dict:
    """Ask Divar what it makes of a link, without walking the pages.

    Divar echoes the filters it understood in `search_data.form_data.data` and
    reports how many posts match, so this says whether a hand-added filter is
    actually supported for that category.
    """
    from .search_url import parse_divar_search_url
    spec = parse_divar_search_url(url)
    resp = net.post_with_retry(DIVAR_API_URL, build_api_payload(spec), net.DIVAR_HEADERS,
                               timeout=30, max_retries=max_retries)
    data = resp.json()
    echoed = set(data.get("search_data", {}).get("form_data", {}).get("data", {}).keys())
    return {"count": data.get("map_data", {}).get("post_count"), "echoed": echoed}


def format_time_ago(sort_date: str) -> str:
    if not sort_date:
        return ""
    try:
        dt = datetime.fromisoformat(sort_date.replace("Z", "+00:00"))
        seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
        if seconds < 0:
            return ""
        if seconds < 60:
            return "همین الان"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} دقیقه پیش"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} ساعت پیش"
        days = hours // 24
        if days < 30:
            return f"{days} روز پیش"
        return f"{days // 30} ماه پیش"
    except Exception:
        return ""


def fetch_post_detail(token: str, max_retries: int = net.MAX_RETRIES) -> dict:
    """Images, floor, features and exact coordinates for one listing."""
    for attempt in range(max_retries):
        try:
            url = DIVAR_POST_DETAIL_URL.format(token=token)
            data = net.get_with_retry(url, net.DIVAR_HEADERS, timeout=15, max_retries=max_retries).json()
            images = []
            features: dict[str, bool] = {}
            time_ago = floor = size = rooms = year = ""
            credit_num = rent_num = None
            credit_alt_num = rent_alt_num = None
            convertible = False
            latitude = longitude = None
            for section in data.get("sections", []):
                for widget in section.get("widgets", []):
                    wtype = widget.get("widget_type", "")
                    wdata = widget.get("data", {})
                    if wtype == "IMAGE_CAROUSEL":
                        for item in wdata.get("items", []):
                            u = item.get("image", {}).get("url") or item.get("image", {}).get("thumbnail_url") or ""
                            if u:
                                images.append(u)
                    elif wtype == "EXPANDABLE_SECTION":
                        time_ago = _strip_location(wdata.get("title", ""))
                    elif wtype == "GROUP_INFO_ROW":
                        # متراژ / ساخت / اتاق
                        for item in wdata.get("items", []):
                            title = (item.get("title") or "").strip()
                            value = (item.get("value") or "").strip()
                            if title == "متراژ":
                                size = value
                            elif title == "ساخت":
                                year = value
                            elif title == "اتاق":
                                rooms = value
                    elif wtype == "UNEXPANDABLE_ROW":
                        if wdata.get("title", "").strip() == "طبقه":
                            floor = wdata.get("value", "").strip()
                    elif wtype == "RENT_SLIDER":
                        # Convertible listing (قابل تبدیل): Divar sends both ends
                        # of the slider — `value` (base) and `transformed_value`
                        # (other end). e.g. credit 700M/19M <-> 500M/25M.
                        # The old code only read `value`, so a listing whose
                        # base end fell outside the search ranges was invisible
                        # even when the other end matched.
                        def _to_int(v):
                            try:
                                return int(v)
                            except (TypeError, ValueError):
                                return None
                        credit_num = _to_int(wdata.get("credit", {}).get("value"))
                        rent_num = _to_int(wdata.get("rent", {}).get("value"))
                        credit_alt_num = _to_int(wdata.get("credit", {}).get("transformed_value"))
                        rent_alt_num = _to_int(wdata.get("rent", {}).get("transformed_value"))
                        if (credit_alt_num is not None or rent_alt_num is not None) and \
                           ((credit_alt_num != credit_num) or (rent_alt_num != rent_num)):
                            convertible = True
                        else:
                            # identical or absent transformed end = single position
                            credit_alt_num = rent_alt_num = None
                    elif wtype == "GROUP_FEATURE_ROW":
                        # {"title": "پارکینگ", "available": true} — or
                        # {"title": "پارکینگ ندارد", "available": true}
                        for item in wdata.get("items", []):
                            name, available = normalize_feature(
                                item.get("title"), item.get("available", True))
                            if name:
                                features[name] = available
                    elif wtype == "MAP_ROW":
                        try:
                            loc = wdata.get("location", {})
                            pt = ((loc.get("exact_data", {}) or {}).get("point", {}) or
                                  (loc.get("fuzzy_data", {}) or {}).get("point", {}))
                            if pt.get("latitude") is not None and pt.get("longitude") is not None:
                                latitude = float(pt["latitude"])
                                longitude = float(pt["longitude"])
                        except (TypeError, ValueError):
                            pass
            return {"images": images, "time_ago": time_ago, "floor": floor,
                    "size": size, "rooms": rooms, "year": year, "features": features,
                    "credit_num": credit_num, "rent_num": rent_num,
                    "credit_alt_num": credit_alt_num, "rent_alt_num": rent_alt_num,
                    "convertible": convertible,
                    "latitude": latitude, "longitude": longitude}
        except Exception as e:
            logger.warning(f"fetch_post_detail {token} attempt {attempt+1}/{max_retries}: {e}")
            if attempt < max_retries - 1:
                time.sleep(net.RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1))
    logger.warning(f"Giving up on detail for {token}, using fallback")
    return dict(EMPTY_DETAIL)


def slider_ends(listing: dict | None = None, detail: dict | None = None) -> list[tuple[int | None, int | None]]:
    """Both (credit, rent) positions of a listing.

    Returns [(base_credit, base_rent), (alt_credit, alt_rent)] for a
    convertible (قابل تبدیل) listing, or a single-element list otherwise.
    Detail's exact RENT_SLIDER numbers win; falls back to the list-view
    price text when detail is missing.
    """
    listing = listing or {}
    detail = detail or {}
    base_c = detail.get("credit_num")
    base_r = detail.get("rent_num")
    if base_c is None or base_r is None:
        try:
            from .state import parse_price_number as _ppn
        except Exception:  # pragma: no cover — import should always work
            _ppn = None
        if _ppn is not None:
            if base_c is None:
                base_c = _ppn(listing.get("deposit", ""))
            if base_r is None:
                base_r = _ppn(listing.get("price", ""))
    ends = [(base_c, base_r)]
    alt_c = detail.get("credit_alt_num")
    alt_r = detail.get("rent_alt_num")
    if alt_c is not None or alt_r is not None:
        # An end missing one side inherits the base side (slider only moves
        # the converted amount); identical pairs collapse to one end.
        alt_pair = (alt_c if alt_c is not None else base_c,
                    alt_r if alt_r is not None else base_r)
        if alt_pair != ends[0]:
            ends.append(alt_pair)
    return ends


def _in_range(value: int | None, lo: int | None, hi: int | None) -> bool:
    if value is None:
        return False
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def matches_price_ranges(ends: list[tuple[int | None, int | None]],
                         rent_range: tuple[int | None, int | None] | None = None,
                         credit_range: tuple[int | None, int | None] | None = None) -> bool:
    """True if EITHER slider end satisfies BOTH ranges at that position.

    A range of None (or (None, None)) means "no constraint" for that axis.
    Listings with no usable numbers fail closed only when a constraint
    exists; unconstrained searches always pass.
    """
    rent_lo, rent_hi = rent_range or (None, None)
    credit_lo, credit_hi = credit_range or (None, None)
    if rent_lo is None and rent_hi is None and credit_lo is None and credit_hi is None:
        return True
    for credit, rent in ends or []:
        rent_ok = _in_range(rent, rent_lo, rent_hi) if (rent_lo is not None or rent_hi is not None) else True
        credit_ok = _in_range(credit, credit_lo, credit_hi) if (credit_lo is not None or credit_hi is not None) else True
        if rent_ok and credit_ok:
            return True
    return False


def download_image(url: str) -> bytes | None:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": net.DIVAR_HEADERS["User-Agent"]})
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "image" not in ct and "octet" not in ct:
                if not any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
                    logger.warning(f"Unexpected content-type {ct} for {url}")
                    return None
            return r.content
        except Exception as e:
            logger.warning(f"download_image attempt {attempt+1}/3 for {url}: {e}")
            if attempt < 2:
                time.sleep(1 + random.uniform(0, 1))
    return None
