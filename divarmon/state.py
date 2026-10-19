"""Seen-listing state: tokens, price history, new/updated classification."""

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import log
from .search_url import FA_DIGITS

logger = log.get(__name__)

LEGACY_SEEN_FILE = "seen_listings.json"
NO_PRICE_MARKERS = ("ذکر نشده", "توافقی", "رایگان", "مجانی", "تماس")
FA_DIGIT_MAP = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa(value) -> str:
    return str(value).translate(FA_DIGIT_MAP)


# ======================== BIDI HELPERS ========================

# Every alert line opens with an emoji, which carries no direction of its own,
# so a client picks the line's direction from whatever strong character follows
# — Latin text or a bare number flips the whole line to LTR. RLM pins it to RTL.
RLM = "\u200f"
# First-strong isolate: a value that starts with Latin text or a slash keeps its
# own internal direction without reordering the Persian around it.
FSI, PDI = "\u2068", "\u2069"
# Reading runs right-to-left, so "old ⬅️ new" puts the old value on the right and
# points at the new one on the left. A ➡️ here would point back at the old value.
ARROW = "⬅️"


def rtl(line: str) -> str:
    """Pin a line to right-to-left rendering."""
    return f"{RLM}{line}"


def iso(value) -> str:
    """Isolate an embedded value from the bidi run around it."""
    return f"{FSI}{value}{PDI}"


_BIDI_MARKS = str.maketrans("", "", RLM + FSI + PDI)


def plain(line: str) -> str:
    """Drop the bidi marks again — for logs, which no one reads right-to-left."""
    return line.translate(_BIDI_MARKS)


# ======================== PRICE HELPERS ========================

def parse_price_number(text: str | None) -> int | None:
    if not text:
        return None
    for marker in NO_PRICE_MARKERS:
        if marker in text:
            return None
    digits = re.sub(r"[^\d]", "", text.translate(FA_DIGITS))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def fmt_num(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def build_price_change(old: dict, new_listing: dict) -> dict | None:
    """Compare stored record vs fresh listing. None if nothing changed."""
    old_dep_raw = old.get("deposit_raw", "") or ""
    old_pri_raw = old.get("price_raw", "") or ""
    new_dep_raw = new_listing.get("deposit", "") or ""
    new_pri_raw = new_listing.get("price", "") or ""
    if old_dep_raw == new_dep_raw and old_pri_raw == new_pri_raw:
        return None
    new_dep_num = parse_price_number(new_dep_raw)
    new_pri_num = parse_price_number(new_pri_raw)

    def diff_line(label, o_raw, n_raw, o_num, n_num):
        if o_raw == n_raw:
            return None
        line = rtl(f"{label}: {iso(o_raw or '—')} {ARROW} {iso(n_raw or '—')}")
        if o_num and n_num and o_num != n_num:
            delta = n_num - o_num
            pct = delta / abs(o_num) * 100
            sign = "+" if delta > 0 else ""
            arrow = "🔺" if delta > 0 else "🔻"
            line += f"\n  {arrow} {sign}{fmt_num(delta)} ({sign}{pct:.1f}٪)"
        return line

    lines = []
    for args in (("ودیعه", old_dep_raw, new_dep_raw, old.get("deposit_num"), new_dep_num),
                 ("اجاره", old_pri_raw, new_pri_raw, old.get("price_num"), new_pri_num)):
        ln = diff_line(*args)
        if ln:
            lines.append(ln)
    return {
        "lines": lines,
        "deposit_old_raw": old_dep_raw, "deposit_new_raw": new_dep_raw,
        "price_old_raw": old_pri_raw, "price_new_raw": new_pri_raw,
        "deposit_old_num": old.get("deposit_num"), "deposit_new_num": new_dep_num,
        "price_old_num": old.get("price_num"), "price_new_num": new_pri_num,
    }


# ======================== SPEC FINGERPRINT ========================

# Features we track for "did anything real change?". Divar reports each one as
# available true/false, so a missing key means the listing never mentioned it.
TRACKED_FEATURES = ("پارکینگ", "آسانسور", "انباری", "بالکن")

SPEC_LABELS = {
    "size": ("📐", "متراژ"),
    "rooms": ("🛏", "اتاق"),
    "floor": ("🏢", "طبقه"),
    "year": ("🏗", "ساخت"),
}
FEATURE_ICONS = {"پارکینگ": "🚗", "آسانسور": "🛗", "انباری": "📦", "بالکن": "🌇"}


def spec_from_detail(detail: dict) -> dict:
    """The subset of a listing that counts as a real change when it moves."""
    features = detail.get("features") or {}
    spec = {
        "size": (detail.get("size") or "").strip(),
        "rooms": (detail.get("rooms") or "").strip(),
        "floor": (detail.get("floor") or "").strip(),
        "year": (detail.get("year") or "").strip(),
        # RENT_SLIDER ends: a moved slider is a real price change even when
        # the list-view text is identical.
        "credit_base": detail.get("credit_num"),
        "rent_base": detail.get("rent_num"),
        "credit_alt": detail.get("credit_alt_num"),
        "rent_alt": detail.get("rent_alt_num"),
        "convertible": bool(detail.get("convertible")),
    }
    for feat in TRACKED_FEATURES:
        if feat in features:
            spec[feat] = bool(features[feat])
    return spec


def diff_spec(old: dict | None, new: dict | None) -> list:
    """Human-readable lines for every spec field that moved. Empty = no change.

    An unknown side (a record saved before specs were tracked) yields no lines,
    so upgrading never produces a burst of 'changed' alerts.
    """
    if not old or not new:
        return []
    lines = []
    for key, (icon, label) in SPEC_LABELS.items():
        o, n = (old.get(key) or ""), (new.get(key) or "")
        if o and n and o != n:
            lines.append(rtl(f"{icon} {label}: {iso(o)} {ARROW} {iso(n)}"))
    for feat in TRACKED_FEATURES:
        o, n = old.get(feat), new.get(feat)
        if o is None or n is None or o == n:
            continue
        icon = FEATURE_ICONS.get(feat, "•")
        lines.append(rtl(f"{icon} {feat}: {'دارد' if o else 'ندارد'} {ARROW} "
                         f"{'دارد' if n else 'ندارد'}"))
    lines.extend(diff_slider(old, new))
    return lines


def _fmt_slider_num(n) -> str:
    return "—" if n is None else f"{n:,}"


def diff_slider(old: dict | None, new: dict | None) -> list:
    """Lines for a moved رهن/اجاره slider. Empty = no slider change.

    Unknown sides (records saved before slider tracking) yield no lines, so
    upgrading never produces a burst of 'changed' alerts.
    """
    if not old or not new:
        return []
    lines = []
    o_base = (old.get("credit_base"), old.get("rent_base"))
    n_base = (new.get("credit_base"), new.get("rent_base"))
    if all(v is not None for v in (*o_base, *n_base)) and o_base != n_base:
        lines.append(rtl(f"💰 ودیعه {_fmt_slider_num(o_base[0])} {ARROW} {_fmt_slider_num(n_base[0])} · "
                         f"اجاره {_fmt_slider_num(o_base[1])} {ARROW} {_fmt_slider_num(n_base[1])}"))
    o_alt = (old.get("credit_alt"), old.get("rent_alt"))
    n_alt = (new.get("credit_alt"), new.get("rent_alt"))
    if o_alt != n_alt and any(v is not None for v in (*o_alt, *n_alt)):
        # One side unknown (pre-slider record) -> stay quiet, just adopt it.
        if all(v is not None for v in (*o_alt,)) or all(v is not None for v in (*n_alt,)):
            if not (o_alt[0] is None and o_alt[1] is None and (n_alt[0] is not None or n_alt[1] is not None)
                    and old.get("convertible") is None):
                lines.append(rtl(f"🔄 تبدیل: ودیعه {_fmt_slider_num(o_alt[0])} {ARROW} {_fmt_slider_num(n_alt[0])} · "
                                 f"اجاره {_fmt_slider_num(o_alt[1])} {ARROW} {_fmt_slider_num(n_alt[1])}"))
    return lines


def diff_images(old_sig: str | None, new_sig: str | None) -> str | None:
    """Line describing a photo-set change, or None."""
    if not old_sig or not new_sig or old_sig == new_sig:
        return None
    try:
        old_n, new_n = int(old_sig.split(":")[0]), int(new_sig.split(":")[0])
    except (ValueError, IndexError):
        return rtl("🖼 عکس‌ها تغییر کرد")
    if new_n > old_n:
        return rtl(f"🖼 عکس جدید ({to_fa(old_n)} {ARROW} {to_fa(new_n)})")
    if new_n < old_n:
        return rtl(f"🖼 حذف عکس ({to_fa(old_n)} {ARROW} {to_fa(new_n)})")
    return rtl("🖼 عکس‌ها عوض شد")


def diff_title(old_title: str, new_title: str) -> str | None:
    old_title, new_title = (old_title or "").strip(), (new_title or "").strip()
    if not old_title or not new_title or old_title == new_title:
        return None
    # The caption headline already carries the new title, so repeating it here
    # printed the same sentence twice; only the superseded one adds anything.
    return rtl(f"📝 عنوان قبلی: {iso(old_title)}")


# ======================== RECORDS ========================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_record(listing: dict, search_name: str, detail: dict | None = None) -> dict:
    from .divar import image_signature
    detail = detail or {}
    return {
        "spec": spec_from_detail(detail) if detail else None,
        "image_sig": image_signature(detail.get("images") or []) if detail else None,
        "image_count": listing.get("image_count"),
        "title": listing.get("title", ""),
        "price_raw": listing.get("price", ""),
        "deposit_raw": listing.get("deposit", ""),
        "price_num": parse_price_number(listing.get("price", "")),
        "deposit_num": parse_price_number(listing.get("deposit", "")),
        "credit_base": detail.get("credit_num"),
        "rent_base": detail.get("rent_num"),
        "credit_alt": detail.get("credit_alt_num"),
        "rent_alt": detail.get("rent_alt_num"),
        "convertible": bool(detail.get("convertible", False)),
        "sort_date": listing.get("sort_date", "") or "",
        "last_notified_sort_date": listing.get("sort_date", "") or "",
        "first_seen": _now(),
        "last_seen": _now(),
        "search": search_name,
        "latitude": detail.get("latitude"),
        "longitude": detail.get("longitude"),
    }


def update_record_in_place(rec: dict, listing: dict, search_name: str,
                           notified_sort_date: str | None = None, detail: dict | None = None):
    rec["title"] = listing.get("title", rec.get("title", ""))
    rec["price_raw"] = listing.get("price", "")
    rec["deposit_raw"] = listing.get("deposit", "")
    rec["price_num"] = parse_price_number(listing.get("price", ""))
    rec["deposit_num"] = parse_price_number(listing.get("deposit", ""))
    rec["sort_date"] = listing.get("sort_date", "") or ""
    if listing.get("image_count") is not None:
        rec["image_count"] = listing.get("image_count")
    if notified_sort_date is not None:
        rec["last_notified_sort_date"] = notified_sort_date
    rec["last_seen"] = _now()
    rec["search"] = search_name
    if detail:
        from .divar import image_signature
        rec["spec"] = spec_from_detail(detail)
        rec["image_sig"] = image_signature(detail.get("images") or [])
        rec["credit_base"] = detail.get("credit_num")
        rec["rent_base"] = detail.get("rent_num")
        rec["credit_alt"] = detail.get("credit_alt_num")
        rec["rent_alt"] = detail.get("rent_alt_num")
        rec["convertible"] = bool(detail.get("convertible", False))
        if detail.get("latitude") is not None:
            rec["latitude"] = detail.get("latitude")
            rec["longitude"] = detail.get("longitude")


# ======================== STORE ========================

class StateStore:
    """{token: record} with an atomic, thread-safe save."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.tokens: dict = {}
        self.load()

    def __len__(self) -> int:
        with self._lock:
            return len(self.tokens)

    def get(self, token: str) -> dict | None:
        with self._lock:
            return self.tokens.get(token)

    def put(self, token: str, record: dict):
        with self._lock:
            self.tokens[token] = record

    def load(self):
        p = Path(self.path)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.tokens = data.get("tokens", {}) if isinstance(data, dict) else {}
                logger.info(f"Loaded {len(self.tokens)} tracked tokens from {self.path}")
            except Exception as e:
                logger.warning(f"Corrupt {self.path}: {e}, backing up and starting fresh")
                try:
                    p.rename(f"{self.path}.corrupt.{int(time.time())}")
                except Exception:
                    pass
                self.tokens = {}
        self._migrate_legacy()

    def _migrate_legacy(self):
        legacy = Path(LEGACY_SEEN_FILE)
        if not legacy.exists() or str(legacy.resolve()) == str(Path(self.path).resolve()):
            return
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                ldata = json.load(f)
            added = 0
            for t in (ldata.get("tokens", []) if isinstance(ldata, dict) else []):
                if t not in self.tokens:
                    self.tokens[t] = {"title": "", "price_raw": "", "deposit_raw": "",
                                      "price_num": None, "deposit_num": None,
                                      "sort_date": "", "last_notified_sort_date": "",
                                      "first_seen": _now(), "last_seen": _now(), "search": "legacy"}
                    added += 1
            if added:
                logger.info(f"Migrated {added} legacy tokens from {LEGACY_SEEN_FILE}")
                self.save()
        except Exception as e:
            logger.warning(f"Legacy migration failed: {e}")

    def save(self):
        with self._lock:
            try:
                tmp = Path(f"{self.path}.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"tokens": self.tokens, "updated": _now()}, f, ensure_ascii=False)
                tmp.replace(self.path)
            except Exception as e:
                logger.error(f"Failed to save state: {e}")

    def prune(self, max_age_days: int) -> int:
        """Drop records not seen for `max_age_days`. Keeps the file from growing forever."""
        if not max_age_days:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
        removed = 0
        with self._lock:
            for token in list(self.tokens):
                try:
                    seen = datetime.fromisoformat(self.tokens[token].get("last_seen", "")).timestamp()
                except (ValueError, TypeError):
                    continue
                if seen < cutoff:
                    del self.tokens[token]
                    removed += 1
            if removed:
                self.save()
        return removed

    def stats_by_search(self) -> dict:
        counts: dict = {}
        with self._lock:
            for rec in self.tokens.values():
                counts[rec.get("search", "?")] = counts.get(rec.get("search", "?"), 0) + 1
        return counts


# ======================== CLASSIFICATION ========================

def classify(current: list, store: StateStore, search_name: str):
    """Split fresh listings into new / edited / bumped / unchanged.

    * **new** — a token never tracked before.
    * **edited** — something visible in the search results moved: price,
      deposit, title or the photo set. Always worth an alert.
    * **bumped** — only `sort_date` moved. That is what نردبان (a paid bump)
      does, and it is *not* a change: the listing is identical. These are
      handed back separately so the caller can optionally open the listing and
      confirm nothing else changed before staying quiet.
    * **unchanged** — nothing moved at all.

    Legacy stubs (migrated tokens with no prices and no sort_date) are filled
    silently so the migration never produces a wall of alerts.

    Returns (new_list, edited, bumped, unchanged_count, seeded_count) where
    `edited` is [(listing, price_change, cheap_lines)].
    """
    new_list, edited, bumped, unchanged, seeded = [], [], [], 0, 0
    for listing in current:
        token = listing.get("token", "")
        rec = store.get(token)
        if rec is None:
            new_list.append(listing)
            continue
        if not rec.get("price_raw") and not rec.get("deposit_raw") and not rec.get("last_notified_sort_date"):
            update_record_in_place(rec, listing, search_name,
                                   notified_sort_date=listing.get("sort_date", "") or "")
            seeded += 1
            unchanged += 1
            continue

        price_change = build_price_change(rec, listing)
        cheap_lines = []
        title_line = diff_title(rec.get("title", ""), listing.get("title", ""))
        if title_line:
            cheap_lines.append(title_line)
        # The list view already exposes the photo count, so an added photo is
        # visible without opening the listing.
        old_count, new_count = rec.get("image_count"), listing.get("image_count")
        if isinstance(old_count, int) and isinstance(new_count, int) and old_count != new_count:
            cheap_lines.append(rtl(f"🖼 عکس ({to_fa(old_count)} {ARROW} {to_fa(new_count)})"))

        sort_changed = (bool(listing.get("sort_date"))
                        and listing.get("sort_date") != rec.get("last_notified_sort_date"))

        if price_change is not None or cheap_lines:
            edited.append((listing, price_change, cheap_lines))
        elif sort_changed:
            bumped.append(listing)
        else:
            unchanged += 1

        # Refresh what we saw now, but keep last_notified until the alert lands.
        rec["last_seen"] = _now()
        rec["sort_date"] = listing.get("sort_date", "") or ""
        rec["image_count"] = new_count if isinstance(new_count, int) else rec.get("image_count")
        if price_change is None and not cheap_lines:
            rec["title"] = listing.get("title", rec.get("title", ""))
    return new_list, edited, bumped, unchanged, seeded


def deep_change_lines(rec: dict, listing: dict, detail: dict) -> list:
    """What really changed on a bumped listing, by comparing the stored
    fingerprint with a freshly fetched detail page. Empty list = only نردبان."""
    from .divar import image_signature
    lines = diff_spec(rec.get("spec"), spec_from_detail(detail))
    img = diff_images(rec.get("image_sig"), image_signature(detail.get("images") or []))
    if img:
        lines.append(img)
    return lines
