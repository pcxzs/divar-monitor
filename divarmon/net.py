"""HTTP helpers: Divar headers, retries, backoff, block handling."""

import random
import time

import requests

from . import log

logger = log.get(__name__)

MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0
DIVAR_BLOCK_SLEEP = 120

DIVAR_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:154.0) Gecko/20100101 Firefox/154.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://divar.ir/",
    "Origin": "https://divar.ir",
    "X-Screen-Size": "1920x963",
    "X-Standard-Divar-Error": "true",
    "X-Render-Type": "CSR",
}


def sleep_with_jitter(base: float, attempt: int, max_retries: int = MAX_RETRIES):
    delay = base * (2 ** attempt) + random.uniform(0, 1)
    logger.info(f"Retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})...")
    time.sleep(delay)


def is_divar_blocked(data: dict) -> bool:
    for w in data.get("widget_list", []) + data.get("list_widgets", []):
        if w.get("widget_type") == "BLOCKING_VIEW":
            return True
    return False


def _is_retryable_status(status: int) -> bool:
    return status in (429, 500, 502, 503, 504)


def post_with_retry(url: str, payload: dict, headers: dict, timeout: int = 30,
                    max_retries: int = MAX_RETRIES):
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if _is_retryable_status(resp.status_code):
                retry_after = int(resp.headers.get("Retry-After", "0") or "0")
                delay = retry_after if retry_after else RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"Divar HTTP {resp.status_code}, sleeping {delay:.1f}s...")
                time.sleep(delay + random.uniform(0, 1))
                continue
            if resp.status_code == 403:
                logger.warning(f"Divar 403 (blocked), sleeping {DIVAR_BLOCK_SLEEP}s...")
                time.sleep(DIVAR_BLOCK_SLEEP)
                continue
            resp.raise_for_status()
            try:
                if is_divar_blocked(resp.json()):
                    logger.warning(f"Divar blocking view detected, sleeping {DIVAR_BLOCK_SLEEP}s...")
                    time.sleep(DIVAR_BLOCK_SLEEP)
                    continue
            except Exception:
                pass
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            logger.warning(f"Network error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                sleep_with_jitter(RETRY_BASE_DELAY, attempt, max_retries)
            continue
        except requests.exceptions.RequestException as e:
            last_exc = e
            resp = getattr(e, "response", None)
            if resp is not None and 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                raise
            logger.warning(f"Request error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                sleep_with_jitter(RETRY_BASE_DELAY, attempt, max_retries)
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("POST retries exhausted")


def get_with_retry(url: str, headers: dict, timeout: int = 15, max_retries: int = MAX_RETRIES):
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if _is_retryable_status(resp.status_code):
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(f"GET HTTP {resp.status_code}, sleeping {delay:.1f}s...")
                time.sleep(delay + random.uniform(0, 1))
                continue
            if resp.status_code == 403:
                logger.warning(f"GET 403 blocked, sleeping {DIVAR_BLOCK_SLEEP}s...")
                time.sleep(DIVAR_BLOCK_SLEEP)
                continue
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            logger.warning(f"GET network error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                sleep_with_jitter(RETRY_BASE_DELAY, attempt, max_retries)
            continue
        except requests.exceptions.RequestException as e:
            last_exc = e
            logger.warning(f"GET error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                sleep_with_jitter(RETRY_BASE_DELAY, attempt, max_retries)
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("GET retries exhausted")
