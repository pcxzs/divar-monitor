"""Scan engine and the staggered scheduler.

`check_interval` is the period in which *every* search is visited once. The
loop spreads the searches evenly across it: 15 min with 3 links means one
search is scanned every 5 minutes, and each individual link is still revisited
every 15 minutes. Adding or disabling a link from Telegram re-spaces the
remaining ones on the next tick — no restart needed.
"""

import threading
import time
from dataclasses import dataclass, field

from . import geo, log, net, notify
from .config import MIN_SEARCH_GAP
from .divar import (fetch_listings, fetch_post_detail, format_time_ago,
                   matches_price_ranges, slider_ends)
from .search_url import (parse_divar_search_url, price_ranges_from_url,
                         strip_price_filters)
from .state import (classify, deep_change_lines, make_record, plain,
                    update_record_in_place)

logger = log.get(__name__)

ALERT_PAUSE = 1.2          # between alerts, to stay under Telegram rate limits
IDLE_WAIT = 20.0           # nothing enabled / paused: how long before re-checking
EMPTY_CYCLE_LIMIT = 3      # consecutive empty scans before we assume a block


@dataclass
class SearchRuntime:
    last_run: float = 0.0
    last_duration: float = 0.0
    last_total: int = 0
    last_new: int = 0
    last_updated: int = 0
    last_error: str = ""
    runs: int = 0
    total_new: int = 0
    total_updated: int = 0
    scanning: bool = False


@dataclass
class ScanStats:
    total: int = 0
    new: int = 0
    updated: int = 0
    seeded: int = 0
    skipped_geo: int = 0
    filtered_price: int = 0  # slider-aware rent/credit gate (both ends checked)
    bumps_ignored: int = 0     # نردبان only — listing identical, stayed quiet
    error: str = ""
    duration: float = 0.0
    samples: list = field(default_factory=list)   # titles of what was alerted


class Monitor:
    def __init__(self, store, state, bot_holder):
        """`bot_holder` is an object with a `.bot` attribute so the monitor
        keeps working across a Telegram reconnect."""
        self.cfg = store
        self.state = state
        self.bot_holder = bot_holder
        self.runtime: dict[str, SearchRuntime] = {}
        self.stop_event = threading.Event()
        self._scan_lock = threading.Lock()   # one Divar scan at a time
        self._runtime_lock = threading.Lock()
        self.empty_cycles = 0
        self.started_at = time.time()
        self.last_cycle_at = 0.0

    # ---------- runtime bookkeeping ----------

    def rt(self, sid: str) -> SearchRuntime:
        with self._runtime_lock:
            return self.runtime.setdefault(sid, SearchRuntime())

    @property
    def bot(self):
        return self.bot_holder.bot

    def slot_seconds(self, enabled_count: int | None = None) -> float:
        """Gap between two consecutive scans of *different* searches."""
        if enabled_count is None:
            enabled_count = len(self.cfg.enabled_searches())
        interval = float(self.cfg.get("check_interval", 900))
        if enabled_count <= 0:
            return interval
        return max(MIN_SEARCH_GAP, interval / enabled_count)

    def next_due(self, sid: str) -> float:
        """Unix time this search is expected to be scanned again."""
        rt = self.rt(sid)
        if not rt.last_run:
            return time.time()
        return rt.last_run + float(self.cfg.get("check_interval", 900))

    # ---------- one search ----------

    def scan_search(self, search: dict, seed_mode: bool = False,
                    alert: bool = True) -> ScanStats:
        """Fetch one saved link, diff against state, fire alerts."""
        stats = ScanStats()
        sid = search.get("id", "?")
        name = search.get("name", "?")
        started = time.time()
        rt = self.rt(sid)
        rt.scanning = True

        with self._scan_lock:
            try:
                stats = self._scan_locked(search, seed_mode, alert, stats)
            except Exception as e:
                logger.exception(f"[{name}] scan crashed: {e}")
                stats.error = f"{type(e).__name__}: {e}"
            finally:
                stats.duration = time.time() - started
                rt.scanning = False
                rt.last_run = time.time()
                rt.last_duration = stats.duration
                rt.last_total = stats.total
                rt.last_new = stats.new
                rt.last_updated = stats.updated
                rt.last_error = stats.error
                rt.runs += 1
                rt.total_new += stats.new
                rt.total_updated += stats.updated
                self.state.save()
        return stats

    def _price_ok(self, listing: dict, detail: dict,
                    rent_range: tuple | None, credit_range: tuple | None,
                    has_price_filter: bool) -> bool:
        """Slider-aware rent/credit gate: True if EITHER slider end has its
        credit in range AND its rent in range. Fail-open on unexpected errors
        so a parsing bug can never silently drop alerts."""
        if not has_price_filter:
            return True
        try:
            ends = slider_ends(listing, detail)
            return matches_price_ranges(ends, rent_range, credit_range)
        except Exception as e:
            logger.warning(f"price-gate check failed, fail-open: {e}")
            return True

    def _scan_locked(self, search: dict, seed_mode: bool, alert: bool, stats: ScanStats) -> ScanStats:
        name = search.get("name", "?")
        try:
            parsed = parse_divar_search_url(search["url"])
        except Exception as e:
            logger.error(f"[{name}] Bad URL, skipping: {e}")
            stats.error = f"bad link: {e}"
            return stats
        parsed["name"] = name

        # Slider-aware price handling: Divar filters convertibles on the base
        # slider end only, so the server query drops `rent`/`credit` and the
        # ranges from the saved link are re-applied locally against BOTH ends.
        try:
            ranges = price_ranges_from_url(search["url"])
        except Exception as e:
            logger.warning(f"[{name}] Could not parse price ranges, no price gate: {e}")
            ranges = {"rent": (None, None), "credit": (None, None)}
        rent_range = ranges.get("rent", (None, None))
        credit_range = ranges.get("credit", (None, None))
        has_price_filter = any(v is not None for v in (*rent_range, *credit_range))
        server_spec = strip_price_filters(parsed) if has_price_filter else parsed
        if has_price_filter:
            logger.info(f"[{name}] Price gate local (rent={rent_range}, credit={credit_range}); "
                        "server query widened for slider ends.")

        try:
            fence = geo.build_fence(parsed)
            if fence and fence.get("polygons"):
                logger.info(f"[{name}] Geo fence: {len(fence['polygons'])} polygon(s)")
        except Exception as e:
            logger.warning(f"[{name}] Geo fence build failed ({e}), alerting without geo check")
            fence = None

        max_retries = self.cfg.get("max_retries", net.MAX_RETRIES)
        try:
            current, total = fetch_listings(
                server_spec,
                page_fetch_delay=self.cfg.get("page_fetch_delay", 0.8),
                max_retries=max_retries,
                max_pages=self.cfg.max_pages_for(search),
            )
        except Exception as e:
            logger.error(f"[{name}] fetch failed: {e}")
            stats.error = f"fetch failed: {e}"
            return stats

        stats.total = len(current)
        logger.info(f"[{name}] Found {len(current)} listings (total on Divar: {total})")

        if seed_mode:
            for listing in current:
                self.state.put(listing["token"], make_record(listing, name))
            stats.seeded = len(current)
            self.state.save()
            return stats

        new_list, edited, bumped, _, seeded = classify(current, self.state, name)
        stats.seeded = seeded
        if not self.cfg.notify_updates_for(search):
            edited, bumped = [], []

        chat_ids = self.cfg.get("telegram_chat_ids", []) if alert else []

        for listing in new_list:
            token = listing["token"]
            detail = fetch_post_detail(token, max_retries=max_retries)
            if not geo.allowed(fence, detail.get("latitude"), detail.get("longitude")):
                logger.info(f"[{name}] NEW outside geo fence, alert skipped: {listing['title']} "
                            f"({detail.get('latitude')}, {detail.get('longitude')}) {listing['web_url']}")
                self.state.put(token, make_record(listing, name, detail))
                self.state.save()
                stats.skipped_geo += 1
                continue
            if not self._price_ok(listing, detail, rent_range, credit_range, has_price_filter):
                logger.info(f"[{name}] NEW outside price ranges (both slider ends checked), "
                            f"alert skipped: {listing['title']} {listing['web_url']}")
                self.state.put(token, make_record(listing, name, detail))
                self.state.save()
                stats.filtered_price += 1
                continue
            logger.info(f"[{name}] NEW: {listing['title']} - {listing['price']} "
                        f"({format_time_ago(listing.get('sort_date', ''))})")
            if chat_ids:
                try:
                    notify.send_listing_alert(self.bot, listing, chat_ids, kind="new",
                                              search_name=name, max_retries=max_retries, detail=detail)
                except Exception as e:
                    logger.error(f"send alert failed for {token}: {e}")
            self.state.put(token, make_record(listing, name, detail))
            self.state.save()
            stats.new += 1
            if len(stats.samples) < 5:
                stats.samples.append(f"🆕 {listing['title']} — {listing['price']}")
            if chat_ids:
                time.sleep(ALERT_PAUSE)

        for listing, price_change, cheap_lines in edited:
            token = listing["token"]
            detail = fetch_post_detail(token, max_retries=max_retries)
            rec = self.state.get(token)
            change_lines = list(cheap_lines)
            if rec is not None:
                # Fold in anything the search results could not show (floor,
                # rooms, parking, ...) so one message covers the whole edit.
                for line in deep_change_lines(rec, listing, detail):
                    if line not in change_lines:
                        change_lines.append(line)
            notified = listing.get("sort_date", "") or ""
            if not geo.allowed(fence, detail.get("latitude"), detail.get("longitude")):
                logger.info(f"[{name}] UPDATED outside geo fence, alert skipped: {listing['title']} "
                            f"({detail.get('latitude')}, {detail.get('longitude')}) {listing['web_url']}")
                self._store_update(token, listing, name, notified, detail)
                stats.skipped_geo += 1
                continue
            if not self._price_ok(listing, detail, rent_range, credit_range, has_price_filter):
                logger.info(f"[{name}] UPDATED outside price ranges (both slider ends checked), "
                            f"alert skipped: {listing['title']} {listing['web_url']}")
                self._store_update(token, listing, name, notified, detail)
                stats.filtered_price += 1
                continue
            logger.info(f"[{name}] UPDATED: {listing['title']} - {listing['price']} "
                        f"({'; '.join(plain(l) for l in change_lines) or 'price'})")
            self._alert_update(listing, detail, price_change, change_lines, chat_ids,
                               name, max_retries, notified, stats)

        self._check_bumped(bumped, fence, chat_ids, name, max_retries, stats,
                           rent_range, credit_range, has_price_filter)
        return stats

    def _alert_update(self, listing, detail, price_change, change_lines, chat_ids,
                      name, max_retries, notified, stats):
        token = listing["token"]
        if chat_ids:
            try:
                notify.send_listing_alert(self.bot, listing, chat_ids, kind="updated",
                                          price_change=price_change, search_name=name,
                                          max_retries=max_retries, detail=detail,
                                          change_lines=change_lines)
            except Exception as e:
                logger.error(f"send update alert failed for {token}: {e}")
        self._store_update(token, listing, name, notified, detail)
        stats.updated += 1
        if len(stats.samples) < 5:
            stats.samples.append(f"♻️ {listing['title']} — {listing['price']}")
        if chat_ids:
            time.sleep(ALERT_PAUSE)

    def _check_bumped(self, bumped, fence, chat_ids, name, max_retries, stats,
                        rent_range=None, credit_range=None, has_price_filter=False):
        """Listings whose only visible change is sort_date — i.e. نردبان.

        The bump itself is never worth a message. With deep_update_check on we
        open the listing once to confirm no spec or photo changed behind the
        bump; if something did, that is a real edit and gets an alert.
        """
        if not bumped:
            return
        deep = self.cfg.get("deep_update_check", True)
        budget = int(self.cfg.get("max_deep_checks", 40)) if deep else 0
        detail_delay = float(self.cfg.get("detail_fetch_delay", 0.35))

        for listing in bumped:
            token = listing["token"]
            rec = self.state.get(token)
            notified = listing.get("sort_date", "") or ""
            if rec is None:
                continue
            if budget <= 0:
                # Out of budget: record the bump so we do not re-check it every
                # cycle, and stay quiet.
                update_record_in_place(rec, listing, name, notified_sort_date=notified)
                stats.bumps_ignored += 1
                continue
            budget -= 1
            detail = fetch_post_detail(token, max_retries=max_retries)
            # A listing recorded by a seed (or by the pre-2.1 version) has no
            # fingerprint yet: this fetch establishes the baseline silently, and
            # the next bump can be compared against it.
            change_lines = deep_change_lines(rec, listing, detail) if rec.get("spec") else []
            if not change_lines:
                logger.info(f"[{name}] bump only (نردبان), no alert: {listing['title']}")
                update_record_in_place(rec, listing, name, notified_sort_date=notified,
                                       detail=detail)
                self.state.save()
                stats.bumps_ignored += 1
                time.sleep(detail_delay)
                continue
            if not geo.allowed(fence, detail.get("latitude"), detail.get("longitude")):
                update_record_in_place(rec, listing, name, notified_sort_date=notified,
                                       detail=detail)
                self.state.save()
                stats.skipped_geo += 1
                continue
            if not self._price_ok(listing, detail, rent_range, credit_range, has_price_filter):
                logger.info(f"[{name}] UPDATED (behind a bump) outside price ranges, "
                            f"no alert: {listing['title']}")
                update_record_in_place(rec, listing, name, notified_sort_date=notified,
                                       detail=detail)
                self.state.save()
                stats.filtered_price += 1
                continue
            logger.info(f"[{name}] UPDATED (behind a bump): {listing['title']} "
                        f"({'; '.join(plain(l) for l in change_lines)})")
            self._alert_update(listing, detail, None, change_lines, chat_ids,
                               name, max_retries, notified, stats)

    def _store_update(self, token, listing, name, notified, detail):
        rec = self.state.get(token)
        if rec is not None:
            update_record_in_place(rec, listing, name,
                                   notified_sort_date=notified or rec.get("sort_date", ""),
                                   detail=detail)
        else:
            self.state.put(token, make_record(listing, name, detail))
        self.state.save()

    # ---------- all searches at once (CLI --check-once) ----------

    def scan_all(self, seed_mode: bool = False, alert: bool = True) -> ScanStats:
        combined = ScanStats()
        searches = self.cfg.enabled_searches()
        for i, search in enumerate(searches):
            s = self.scan_search(search, seed_mode=seed_mode, alert=alert)
            combined.total += s.total
            combined.new += s.new
            combined.updated += s.updated
            combined.seeded += s.seeded
            combined.skipped_geo += s.skipped_geo
            combined.filtered_price += s.filtered_price
            combined.bumps_ignored += s.bumps_ignored
            if i < len(searches) - 1:
                time.sleep(self.cfg.get("search_delay", 3.0))
        return combined

    # ---------- the staggered loop ----------

    def _pick_next(self, enabled: list) -> dict:
        """The enabled search that has been waiting longest."""
        return min(enabled, key=lambda s: self.rt(s["id"]).last_run)

    def loop(self):
        logger.info("Scheduler started")
        while not self.stop_event.is_set():
            if self.cfg.get("paused", False):
                self.stop_event.wait(IDLE_WAIT)
                continue
            enabled = self.cfg.enabled_searches()
            if not enabled:
                self.stop_event.wait(IDLE_WAIT)
                continue

            slot = self.slot_seconds(len(enabled))
            search = self._pick_next(enabled)
            started = time.time()
            logger.info(f"[{search.get('name')}] scheduled scan "
                        f"(1 of {len(enabled)}, next slot in {slot:.0f}s)")
            try:
                stats = self.scan_search(search)
                self.last_cycle_at = time.time()
                logger.info(f"[{search.get('name')}] done: total={stats.total} new={stats.new} "
                            f"updated={stats.updated} bumps_ignored={stats.bumps_ignored} "
                            f"skipped_geo={stats.skipped_geo} filtered_price={stats.filtered_price} "
                            f"in {stats.duration:.1f}s")
                if stats.total == 0 and not stats.error:
                    self.empty_cycles += 1
                    logger.warning(f"Empty result scan {self.empty_cycles}/{EMPTY_CYCLE_LIMIT}")
                    if self.empty_cycles >= EMPTY_CYCLE_LIMIT:
                        logger.warning("Multiple empty scans, possible block - backing off")
                        self.stop_event.wait(net.DIVAR_BLOCK_SLEEP)
                        self.empty_cycles = 0
                else:
                    self.empty_cycles = 0
            except Exception as e:
                logger.error(f"Scheduler error (continuing): {type(e).__name__}: {e}")

            self._wait_slot(started)
        logger.info("Scheduler stopped")

    def _wait_slot(self, started: float):
        """Sleep until this search's slot is up, re-reading the config as we go
        so an interval change or a pause from Telegram lands within seconds."""
        while not self.stop_event.is_set():
            if self.cfg.get("paused", False):
                return
            deadline = started + self.slot_seconds()
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            self.stop_event.wait(min(5.0, remaining))

    def stop(self):
        self.stop_event.set()
