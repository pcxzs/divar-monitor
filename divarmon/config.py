"""Config file handling: defaults, migration, atomic save, thread-safe access.

Everything the operator can change lives in config.json so the Telegram admin
panel can rewrite it at runtime without a restart.
"""

import copy
import json
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import log

logger = log.get(__name__)

DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_STATE_FILE = "listings_state.json"

DEFAULT_CHECK_INTERVAL = 900          # full sweep of every search, in seconds
MIN_SEARCH_GAP = 20                   # floor for the per-search stagger slot
DEFAULT_PAGE_FETCH_DELAY = 0.8
DEFAULT_SEARCH_DELAY = 3.0
DEFAULT_MAX_PAGES = 5                 # sort_date puts fresh listings first
DEFAULT_MAX_RETRIES = 5


def _env_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ.get(name, fallback))
    except (TypeError, ValueError):
        return fallback


def default_config() -> dict:
    return {
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        # Who may use the Telegram admin panel. Numeric Telegram user IDs.
        "admin_ids": (
            [os.environ["TELEGRAM_ADMIN_ID"]] if os.environ.get("TELEGRAM_ADMIN_ID") else []
        ),
        # Who receives listing alerts (users, groups or channels).
        "telegram_chat_ids": (
            [os.environ["TELEGRAM_CHAT_ID"]] if os.environ.get("TELEGRAM_CHAT_ID") else []
        ),
        "proxy": "",
        "check_interval": _env_int("CHECK_INTERVAL", DEFAULT_CHECK_INTERVAL),
        "page_fetch_delay": DEFAULT_PAGE_FETCH_DELAY,
        "search_delay": DEFAULT_SEARCH_DELAY,
        "max_pages_per_search": _env_int("MAX_PAGES", DEFAULT_MAX_PAGES),
        "max_retries": DEFAULT_MAX_RETRIES,
        "notify_updates": True,        # alert when a tracked listing really changes
        # A نردبان bump moves sort_date without changing anything. With this on,
        # such a listing is opened once to confirm nothing else moved before we
        # stay quiet; off means bumps are ignored outright (cheaper, blinder).
        "deep_update_check": True,
        "max_deep_checks": 40,         # per scan, caps the extra detail requests
        "detail_fetch_delay": 0.35,
        "paused": False,               # global kill switch, toggled from Telegram
        "pending_requests": {},        # /ask requests awaiting an admin decision
        "searches": [],
    }


def new_search_id() -> str:
    return secrets.token_hex(4)


def mask_proxy(url: str) -> str:
    """Hide proxy credentials for logging."""
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = f"{parts.username}:***@{parts.hostname}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        pass
    return url


def apply_proxy(cfg: dict) -> dict:
    """Single place for proxy config: config.json `proxy` wins, else env.

    Exported to the environment so both `requests` (Divar) and telebot honor it.
    """
    proxy = ((cfg.get("proxy") or "") or os.environ.get("HTTPS_PROXY", "")
             or os.environ.get("https_proxy", "") or os.environ.get("HTTP_PROXY", "")
             or os.environ.get("http_proxy", "")).strip()
    cfg["proxy"] = proxy
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if proxy:
            os.environ[var] = proxy
        else:
            os.environ.pop(var, None)
    if proxy:
        logger.info(f"Using proxy {mask_proxy(proxy)}")
    return cfg


def _normalize(cfg: dict) -> dict:
    """Fill in defaults, migrate old shapes, give every search a stable id."""
    base = default_config()
    for k, v in cfg.items():
        base[k] = v
    cfg = base

    # Legacy single-admin key.
    legacy_admin = cfg.pop("admin_id", None)
    if legacy_admin and not cfg.get("admin_ids"):
        cfg["admin_ids"] = [legacy_admin]
    cfg["admin_ids"] = [str(a).strip() for a in (cfg.get("admin_ids") or []) if str(a).strip()]
    cfg["telegram_chat_ids"] = [str(c).strip() for c in (cfg.get("telegram_chat_ids") or []) if str(c).strip()]

    norm, seen_ids = [], set()
    for s in cfg.get("searches", []):
        if isinstance(s, str):           # allow bare URL strings
            s = {"url": s}
        if not isinstance(s, dict) or not s.get("url"):
            continue
        sid = str(s.get("id") or "")
        if not sid or sid in seen_ids:
            sid = new_search_id()
            while sid in seen_ids:
                sid = new_search_id()
        s["id"] = sid
        seen_ids.add(sid)
        s.setdefault("enabled", True)
        s.setdefault("name", "")
        s.setdefault("max_pages", None)       # None = inherit global
        s.setdefault("notify_updates", None)  # None = inherit global
        if not s["name"]:
            s["name"] = f"search {len(norm) + 1}"
        norm.append(s)
    cfg["searches"] = norm
    return cfg


class ConfigStore:
    """Thread-safe config holder. Telegram handlers and the scan loop share it."""

    def __init__(self, path: str = DEFAULT_CONFIG_FILE, state_file: str = DEFAULT_STATE_FILE):
        self.path = path
        self._lock = threading.RLock()
        self.data = _normalize(self._read(path))
        self.data["_state_file"] = state_file

    # ---------- persistence ----------

    @staticmethod
    def _read(path: str) -> dict:
        p = Path(path)
        if not p.exists():
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            logger.info(f"Loaded config from {path} ({len(loaded.get('searches', []))} searches)")
            return loaded
        except Exception as e:
            raise SystemExit(f"Config file {path} is corrupt: {e}")

    def save(self) -> None:
        with self._lock:
            payload = {k: v for k, v in self.data.items() if not k.startswith("_")}
            try:
                tmp = Path(f"{self.path}.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                tmp.replace(self.path)
            except Exception as e:
                logger.error(f"Failed to save config: {e}")

    # ---------- access ----------

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def get(self, key: str, default=None):
        with self._lock:
            val = self.data.get(key, default)
            return copy.deepcopy(val) if isinstance(val, (dict, list)) else val

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    def is_admin(self, user_id) -> bool:
        with self._lock:
            admins = self.data.get("admin_ids") or []
            return str(user_id) in admins

    def has_admins(self) -> bool:
        return bool(self.get("admin_ids"))

    # ---------- searches ----------

    def searches(self) -> list:
        with self._lock:
            return copy.deepcopy(self.data.get("searches", []))

    def enabled_searches(self) -> list:
        return [s for s in self.searches() if s.get("enabled", True)]

    def find(self, sid: str) -> dict | None:
        with self._lock:
            for s in self.data.get("searches", []):
                if s.get("id") == sid:
                    return copy.deepcopy(s)
        return None

    def add_search(self, url: str, name: str, enabled: bool = True) -> dict:
        with self._lock:
            search = {
                "id": new_search_id(),
                "name": name,
                "url": url,
                "enabled": enabled,
                "max_pages": None,
                "notify_updates": None,
            }
            self.data.setdefault("searches", []).append(search)
            self.save()
            return copy.deepcopy(search)

    def update_search(self, sid: str, **fields) -> dict | None:
        with self._lock:
            for s in self.data.get("searches", []):
                if s.get("id") == sid:
                    s.update(fields)
                    self.save()
                    return copy.deepcopy(s)
        return None

    def remove_search(self, sid: str) -> dict | None:
        with self._lock:
            searches = self.data.get("searches", [])
            for i, s in enumerate(searches):
                if s.get("id") == sid:
                    removed = searches.pop(i)
                    self.save()
                    return removed
        return None

    # ---------- effective per-search settings ----------

    def max_pages_for(self, search: dict) -> int:
        return int(search.get("max_pages") or self.get("max_pages_per_search", DEFAULT_MAX_PAGES))

    def notify_updates_for(self, search: dict) -> bool:
        val = search.get("notify_updates")
        if val is None:
            return bool(self.get("notify_updates", True))
        return bool(val)
