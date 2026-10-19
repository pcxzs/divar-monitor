"""Divar `/s/...` link handling.

Two jobs:
  1. Parse a link the admin pastes into the API-ready search spec that
     `divar.py` posts to api.divar.ir (the link *is* the source of truth —
     the backend calls are built straight from it).
  2. Read and rewrite individual filters inside that link, so the Telegram
     admin panel can change e.g. the max floor without the admin ever
     touching a URL.
"""

import hashlib
import re
from urllib.parse import parse_qsl, quote, unquote, unquote_plus, urlparse, urlsplit, urlunsplit

from . import log

logger = log.get(__name__)

DIVAR_API_URL = "https://api.divar.ir/v8/postlist/w/search"
DIVAR_POST_DETAIL_URL = "https://api.divar.ir/v8/posts-v2/web/{token}"

# Minimal city slug -> Divar city id map. Fallback is ["1"] (Tehran).
CITY_IDS = {
    "tehran": "1", "karaj": "2", "mashhad": "3", "isfahan": "4", "shiraz": "5",
    "tabriz": "6", "ahvaz": "7", "qom": "8", "kermanshah": "9", "rasht": "10",
}

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


# ======================== URL QUERY EDITING ========================

# Params that describe the map/geometry. They come as a set from the Divar UI
# and are meaningless to edit one at a time, so the panel hides them.
STRUCTURAL_PARAMS = {
    "bbox", "map_bbox", "map_place_hash", "multi_geo_polygon_encoded",
    "map_place_hash_encoded", "camera", "place_ids", "zoom",
}


def get_param(url: str, key: str) -> str | None:
    """Decoded value of one query param, or None when absent."""
    for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if k == key:
            return v
    return None


def all_params(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlsplit(url).query, keep_blank_values=True)


def set_param(url: str, key: str, value: str | None) -> str:
    """Return `url` with `key` set to `value` (None deletes it).

    Edits the query string surgically so every untouched param keeps its exact
    original encoding — re-encoding the whole query would risk mangling the
    polygon blob Divar is picky about.
    """
    u = urlsplit(url)
    parts = [p for p in u.query.split("&") if p]
    out, done = [], False
    for p in parts:
        raw_key = p.split("=", 1)[0]
        if unquote_plus(raw_key) == key:
            if value is not None and not done:
                out.append(f"{quote(key, safe='')}={quote(str(value), safe='')}")
                done = True
            continue
        out.append(p)
    if value is not None and not done:
        out.append(f"{quote(key, safe='')}={quote(str(value), safe='')}")
    return urlunsplit((u.scheme, u.netloc, u.path, "&".join(out), u.fragment))


# ======================== FILTER REGISTRY ========================

RANGE, BOOL, MULTI, TEXT = "range", "bool", "multi", "text"

ROOM_OPTIONS = ["بدون اتاق", "یک", "دو", "سه", "چهار", "بیشتر"]

# Verified against the live API: a filter Divar honours is echoed back in the
# response's `search_data.form_data.data` and moves `map_data.post_count`.
# `probe_filters()` re-runs that check for anything added by hand.
FILTER_SPECS: dict[str, dict] = {
    "rent":              dict(type=RANGE, label="Rent (اجاره)",        emoji="💰", money=True),
    "credit":            dict(type=RANGE, label="Deposit (ودیعه)",     emoji="🏦", money=True),
    "price":             dict(type=RANGE, label="Price (قیمت)",        emoji="🏷", money=True),
    "size":              dict(type=RANGE, label="Area (متراژ)",        emoji="📐", unit="m²",
                              presets=[50, 60, 70, 80, 100, 150]),
    "floor":             dict(type=RANGE, label="Floor (طبقه)",        emoji="🏢",
                              presets=[1, 2, 3, 4, 5, 6]),
    "building-age":      dict(type=RANGE, label="Building age (سن بنا)", emoji="🧱", unit="yr",
                              presets=[0, 5, 10, 15, 20, 30]),
    "construction-year": dict(type=RANGE, label="Built year (سال ساخت)", emoji="📅", addable=False),
    "rooms":             dict(type=MULTI, label="Rooms (اتاق)",        emoji="🛏", options=ROOM_OPTIONS),
    "elevator":          dict(type=BOOL,  label="Elevator (آسانسور)",  emoji="🛗"),
    "parking":           dict(type=BOOL,  label="Parking (پارکینگ)",   emoji="🚗"),
    "warehouse":         dict(type=BOOL,  label="Storage (انباری)",    emoji="📦"),
    "balcony":           dict(type=BOOL,  label="Balcony (بالکن)",     emoji="🌇"),
    "has-photo":         dict(type=BOOL,  label="Has photo (عکس‌دار)", emoji="🖼"),
    "has-video":         dict(type=BOOL,  label="Has video (ویدیو)",   emoji="🎬"),
    "q":                 dict(type=TEXT,  label="Keyword (متن)",       emoji="🔤"),
    "districts":         dict(type=TEXT,  label="Districts (محله)",    emoji="🗺", addable=False),
    "business-type":     dict(type=TEXT,  label="Lister type",         emoji="🏢", addable=False),
    "sort":              dict(type=TEXT,  label="Sort order",          emoji="↕️", addable=False),
}

# Offered under "add a filter" in the panel, in this order. Divar supports
# these on the search link whether or not the pasted URL happened to use them.
ADDABLE_ORDER = ["rent", "credit", "price", "size", "rooms", "floor", "building-age",
                 "parking", "elevator", "warehouse", "balcony", "has-photo",
                 "has-video", "q"]
ADDABLE_ORDER += [k for k, v in FILTER_SPECS.items()
                  if v.get("addable", True) and k not in ADDABLE_ORDER]

VALID_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")


def parse_custom_filter(text: str) -> tuple[str, str | None]:
    """'parking=true' / 'floor: 0-3' / 'balcony' -> (key, value).

    Raises ValueError when the key could not be a Divar query param.
    """
    raw = (text or "").strip().lstrip("?&")
    for sep in ("=", ":"):
        if sep in raw:
            key, _, value = raw.partition(sep)
            key, value = key.strip(), value.strip()
            break
    else:
        key, value = raw, "true"
    if not VALID_KEY_RE.match(key):
        raise ValueError(f"'{key}' is not a valid filter name")
    if key in STRUCTURAL_PARAMS:
        raise ValueError(f"'{key}' describes the map area — paste a new link to change that")
    return key, (value or None)


def spec_for(key: str) -> dict:
    return FILTER_SPECS.get(key) or dict(type=TEXT, label=key, emoji="🔧", addable=False)


def filter_token(key: str) -> str:
    """Stable 6-char id for a filter key (Telegram callback_data is 64 bytes)."""
    return hashlib.blake2s(key.encode("utf-8"), digest_size=3).hexdigest()


def key_from_token(url: str, token: str) -> str | None:
    """Reverse `filter_token` against the keys this URL could possibly carry."""
    candidates = {k for k, _ in all_params(url)} | set(FILTER_SPECS)
    for key in candidates:
        if filter_token(key) == token:
            return key
    return None


def editable_params(url: str) -> list[str]:
    """Query params the panel shows for this search, known ones first."""
    present = [k for k, _ in all_params(url) if k not in STRUCTURAL_PARAMS]
    known = [k for k in FILTER_SPECS if k in present]
    other = [k for k in present if k not in FILTER_SPECS]
    seen, ordered = set(), []
    for k in known + other:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    return ordered


# ======================== VALUE FORMATTING / PARSING ========================

def parse_range(value: str | None) -> tuple[str | None, str | None]:
    """'min-max' (either side may be empty) -> (min|None, max|None)."""
    if not value:
        return None, None
    if "-" not in value:
        v = value.strip()
        return (v or None, v or None)
    left, _, right = value.partition("-")
    return (left.strip() or None), (right.strip() or None)


def join_range(minimum: str | None, maximum: str | None) -> str | None:
    if not minimum and not maximum:
        return None
    return f"{minimum or ''}-{maximum or ''}"


def _range_bound_to_int(raw: str | None) -> int | None:
    """One side of a `rent`/`credit` URL range -> int Toman (or None)."""
    if raw is None:
        return None
    t = str(raw).strip().translate(FA_DIGITS).replace("٬", "").replace(",", "").replace(" ", "")
    if not t:
        return None
    try:
        return int(float(t))
    except (TypeError, ValueError):
        return None


def price_ranges_from_url(url: str) -> dict:
    """Desired (rent, credit) ranges from a saved link, as ints.

    Returns {"rent": (lo|None, hi|None), "credit": (lo|None, hi|None)}.
    Missing filter -> (None, None) = unconstrained.
    """
    out = {}
    for key in ("rent", "credit"):
        lo_s, hi_s = parse_range(get_param(url, key))
        out[key] = (_range_bound_to_int(lo_s), _range_bound_to_int(hi_s))
    return out


def strip_price_filters(spec: dict) -> dict:
    """Copy of a parsed search spec without server-side rent/credit filters.

    Divar's search API filters convertible (قابل تبدیل) listings on their
    *base* RENT_SLIDER end only, so a listing whose converted end matches
    the user's ranges would never be returned. The scan therefore fetches
    the wider candidate set with these filters removed and re-applies them
    locally (see divar.matches_price_ranges) against *both* slider ends.
    The saved URL is untouched — this only affects the API payload.
    """
    import copy
    dup = copy.deepcopy(spec)
    form_data = dup.get("form_data")
    if isinstance(form_data, dict):
        form_data.pop("rent", None)
        form_data.pop("credit", None)
    return dup


def fmt_money(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1_000_000_000 and n % 1_000_000_000 == 0:
        return f"{n // 1_000_000_000}B ({n:,})"
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M ({n:,})"
    return f"{n:,}"


def parse_money_input(text: str) -> int | None:
    """Accept '40', '40m', '40 million', '40,000,000', '۴۰'."""
    t = (text or "").strip().translate(FA_DIGITS).lower()
    t = t.replace("٬", "").replace(",", "").replace(" ", "")
    mult = 1
    for suffix, factor in (("میلیارد", 1_000_000_000), ("b", 1_000_000_000),
                           ("میلیون", 1_000_000), ("m", 1_000_000),
                           ("هزار", 1_000), ("k", 1_000)):
        if t.endswith(suffix):
            t, mult = t[: -len(suffix)], factor
            break
    if not re.fullmatch(r"\d+(\.\d+)?", t or ""):
        return None
    value = int(float(t) * mult)
    # Bare small numbers on a money field mean millions ("rent 40" = 40M).
    if mult == 1 and value < 1000:
        value *= 1_000_000
    return value


def parse_int_input(text: str) -> int | None:
    t = (text or "").strip().translate(FA_DIGITS).replace(",", "")
    if not re.fullmatch(r"-?\d+", t or ""):
        return None
    return int(t)


def describe_value(key: str, value: str | None) -> str:
    """Human-readable rendering of one filter's current value."""
    spec = spec_for(key)
    kind = spec["type"]
    if value is None:
        return "any"
    if kind == BOOL:
        v = value.strip().lower()
        return "yes ✅" if v in ("true", "1", "yes") else "no ❌"
    if kind == MULTI:
        items = [i for i in re.split(r"[,|]", value) if i.strip()]
        return "، ".join(items) if items else "any"
    if kind == RANGE:
        lo, hi = parse_range(value)
        money = spec.get("money")
        unit = spec.get("unit", "")
        f = fmt_money if money else (lambda x: str(x))
        if lo and hi and lo == hi:
            return f"{f(lo)} {unit}".strip()
        if lo and hi:
            return f"{f(lo)} … {f(hi)} {unit}".strip()
        if hi:
            return f"≤ {f(hi)} {unit}".strip()
        if lo:
            return f"≥ {f(lo)} {unit}".strip()
        return "any"
    return value or "any"


def summarize(url: str, limit: int = 12) -> str:
    """Multi-line 'filters at a glance' block for the panel."""
    lines = []
    for key in editable_params(url)[:limit]:
        spec = spec_for(key)
        lines.append(f"{spec['emoji']} {spec['label']}: {describe_value(key, get_param(url, key))}")
    if not lines:
        lines.append("(no filters on this link)")
    if has_polygon(url):
        lines.append("🗺 Map area: custom polygon (from the Divar map)")
    elif get_param(url, "bbox"):
        lines.append("🗺 Map area: bounding box")
    return "\n".join(lines)


def has_polygon(url: str) -> bool:
    return bool(get_param(url, "multi_geo_polygon_encoded"))


# ======================== DIVAR SEARCH SPEC ========================

def _parse_number_range(value: str):
    """'min-max' (either side may be empty) -> (minimum|None, maximum|None)."""
    if "-" not in value:
        return None
    left, _, right = value.partition("-")
    left, right = left.strip(), right.strip()
    if left and not re.fullmatch(r"[\d۰-۹٠-٩٬,\s.]+", left):
        return None
    if right and not re.fullmatch(r"[\d۰-۹٠-٩٬,\s.]+", right):
        return None
    if not left and not right:
        return None
    return (left or None, right or None)


def _inferred_field(key: str, raw_values: list) -> dict:
    """Infer a Divar form_data field from raw query-string value(s)."""
    if key in ("sort",):
        return {"str": {"value": raw_values[0]}}
    if len(raw_values) == 1:
        v = raw_values[0]
        vl = v.lower()
        if vl in ("true", "false"):
            return {"boolean": {"value": vl == "true"}}
        nr = _parse_number_range(v)
        if nr is not None and key not in ("rooms", "q"):
            minimum, maximum = nr
            rng = {}
            if minimum:
                rng["minimum"] = minimum
            if maximum:
                rng["maximum"] = maximum
            return {"number_range": rng}
        if "," in v or "|" in v:
            sep = "," if "," in v else "|"
            items = [i.strip() for i in v.split(sep) if i.strip()]
            return {"repeated_string": {"value": items}}
        return {"str": {"value": v}}
    # repeated key (?a=x&a=y)
    items = []
    for v in raw_values:
        items.extend([i.strip() for i in v.split(",") if i.strip()] or [v])
    return {"repeated_string": {"value": items}}


def parse_divar_search_url(url: str) -> dict:
    """Parse a Divar /s/... (map) link into an API-ready search spec.

    Returns dict: city_slug, category_slug, api_category, city_ids,
    form_data (dict of Divar field objects), camera_bbox, place_hash, query_keys.
    Raises ValueError on an invalid URL.
    """
    url = url.strip().strip("\"'")
    if not url.startswith("http"):
        url = "https://" + url
    u = urlparse(url)
    if "divar.ir" not in (u.netloc or ""):
        raise ValueError("URL must be a divar.ir link")
    parts = [p for p in u.path.split("/") if p]
    # expected: s/<city>[/<category>...]
    city_slug, category_slug = "", ""
    if "s" in parts:
        i = parts.index("s")
        if len(parts) > i + 1:
            city_slug = unquote(parts[i + 1])
        if len(parts) > i + 2:
            category_slug = unquote("/".join(parts[i + 2:]))
    else:
        raise ValueError("Not a Divar search link (expected divar.ir/s/<city>/...)")
    # category slug like "rent-apartment" -> API "apartment-rent" (reverse dash parts)
    api_category = ""
    if category_slug:
        base = category_slug.split("?")[0].split("/")[0]
        chunks = base.split("-")
        api_category = "-".join(reversed(chunks)) if len(chunks) > 1 else base

    grouped: dict[str, list] = {}
    for k, v in parse_qsl(u.query, keep_blank_values=True):
        grouped.setdefault(k, []).append(v)

    form_data: dict = {}
    camera_bbox = None
    place_hash = None

    for key, values in grouped.items():
        lk = key.lower()
        if lk == "map_bbox":
            try:
                nums = [float(x) for x in values[0].split(",")]
                if len(nums) == 4:
                    lon_min, lat_min, lon_max, lat_max = nums
                    camera_bbox = {"min_latitude": lat_min, "min_longitude": lon_min,
                                   "max_latitude": lat_max, "max_longitude": lon_max}
            except Exception:
                pass
            continue
        if lk == "map_place_hash":
            place_hash = values[0]
            continue
        if lk == "bbox":
            try:
                nums = [float(x) for x in values[0].split(",")]
                if len(nums) == 4:
                    form_data["bbox"] = {"repeated_float": {"value": [{"value": n} for n in nums]}}
                    if camera_bbox is None:
                        lon_min, lat_min, lon_max, lat_max = nums
                        camera_bbox = {"min_latitude": lat_min, "min_longitude": lon_min,
                                       "max_latitude": lat_max, "max_longitude": lon_max}
            except Exception:
                pass
            continue
        if lk == "multi_geo_polygon_encoded":
            # Divar web strips trailing commas; sending them makes the server
            # silently drop the polygon and fall back to bbox-only filtering.
            cleaned = [v.strip().rstrip(",") for v in values]
            cleaned = [v for v in cleaned if v]
            if cleaned:
                form_data[key] = {"repeated_string": {"value": cleaned}}
            continue
        if lk in ("has-photo", "has_photo", "hasphoto"):
            form_data["has-photo"] = {"boolean": {"value": (values[0] or "").lower() in ("true", "1", "yes")}}
            continue
        if not values or all(v == "" for v in values):
            continue
        form_data[key] = _inferred_field(key, values)

    if api_category and "category" not in form_data:
        form_data["category"] = {"str": {"value": api_category}}
    if "sort" not in form_data:
        # sort_date surfaces both new AND recently-updated listings first
        form_data["sort"] = {"str": {"value": "sort_date"}}

    city_id = CITY_IDS.get(city_slug.lower(), "1" if city_slug.lower() in ("", "tehran") else None)
    city_ids = [city_id] if city_id else ["1"]

    if place_hash is None:
        place_hash = f"{city_ids[0]}||{api_category}||"

    if camera_bbox is None:                        # default camera on Tehran
        camera_bbox = {"min_latitude": 35.6787262, "min_longitude": 51.2615623,
                       "max_latitude": 35.8264809, "max_longitude": 51.505394}

    return {
        "url": url,
        "city_slug": city_slug,
        "category_slug": category_slug,
        "api_category": api_category,
        "city_ids": city_ids,
        "form_data": form_data,
        "camera_bbox": camera_bbox,
        "place_hash": place_hash,
        "query_keys": sorted(grouped.keys()),
    }


def build_api_payload(search_spec: dict, pagination_data: dict | None = None) -> dict:
    bbox = search_spec["camera_bbox"]
    payload = {
        "city_ids": search_spec.get("city_ids", ["1"]),
        "source_view": "MAP_DISCOVERY_MAP",
        "disable_recommendation": False,
        "map_state": {
            "camera_info": {
                "bbox": {
                    "min_latitude": bbox["min_latitude"],
                    "min_longitude": bbox["min_longitude"],
                    "max_latitude": bbox["max_latitude"],
                    "max_longitude": bbox["max_longitude"],
                },
                "place_hash": search_spec.get("place_hash", ""),
                "zoom": 11.76,
            },
            "page_state": "HALF_STATE",
        },
        "search_data": {
            "form_data": {"data": search_spec.get("form_data", {})},
            "server_payload": {
                "@type": "type.googleapis.com/widgets.SearchData.ServerPayload",
                "additional_form_data": {},
            },
        },
        "previous_place_ids": [],
    }
    if pagination_data:
        payload["pagination_data"] = pagination_data
    return payload


def auto_name(url: str, idx: int) -> str:
    try:
        parsed = parse_divar_search_url(url)
        city = parsed.get("city_slug") or "divar"
        cat = parsed.get("category_slug") or "search"
        return f"{city}/{cat} #{idx}"
    except Exception:
        return f"search #{idx}"


DIVAR_LINK_RE = re.compile(r"https?://(?:www\.)?divar\.ir/s/\S+", re.IGNORECASE)


def extract_link(text: str) -> str | None:
    """Pull the first Divar search link out of a Telegram message."""
    if not text:
        return None
    m = DIVAR_LINK_RE.search(text)
    if m:
        return m.group(0).strip().strip("'\"")
    t = text.strip()
    if "divar.ir/s/" in t and " " not in t:
        return t if t.startswith("http") else "https://" + t
    return None
