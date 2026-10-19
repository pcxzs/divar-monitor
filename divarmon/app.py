"""Wiring: config -> bot -> admin panel -> staggered scan loop."""

import argparse
import atexit
import os
import signal
import sys
import threading
import time

import telebot

from . import log
from .admin import AdminPanel, esc, fmt_duration
from .config import (DEFAULT_CONFIG_FILE, DEFAULT_STATE_FILE, ConfigStore,
                     apply_proxy, mask_proxy)
from .monitor import Monitor
from .notify import notify_text
from .search_url import auto_name, parse_divar_search_url
from .state import StateStore

logger = log.get(__name__)


class BotHolder:
    """Indirection so the monitor always uses the live bot object."""

    def __init__(self, bot):
        self.bot = bot


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Divar rental watchdog with a Telegram admin panel")
    ap.add_argument("--config", default=DEFAULT_CONFIG_FILE, help="config file path (default: config.json)")
    ap.add_argument("--state", default=DEFAULT_STATE_FILE, help="state file path (default: listings_state.json)")
    ap.add_argument("--add-url", metavar="URL", help="add a Divar search link and exit")
    ap.add_argument("--name", default="", help="name for --add-url")
    ap.add_argument("--list-searches", action="store_true", help="list saved searches and exit")
    ap.add_argument("--check-once", action="store_true", help="run one scan of every search and exit")
    ap.add_argument("--seed", action="store_true", help="with --check-once: record listings without alerting")
    ap.add_argument("--prune-days", type=int, default=0, metavar="N",
                    help="drop listings not seen for N days, then continue")
    ap.add_argument("--non-interactive", action="store_true", help="accepted for compatibility; ignored")
    return ap.parse_args(argv)


def _list_searches(cfg: ConfigStore):
    searches = cfg.searches()
    if not searches:
        print("  (no saved searches yet)")
        return
    for i, s in enumerate(searches, 1):
        status = "enabled " if s.get("enabled", True) else "disabled"
        url = s.get("url", "")
        print(f"  {i}. [{status}] {s.get('name', '')}  (id {s.get('id')})")
        print(f"     {url if len(url) <= 100 else url[:97] + '...'}")


def _start_polling(bot: telebot.TeleBot):
    """Never die: proxy failures and outages are retried forever with backoff."""
    def loop():
        failures = 0
        while True:
            try:
                logger.info("Telegram polling started")
                bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
                failures = 0
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                failures += 1
                delay = min(10 + 10 * failures, 60)
                logger.warning(f"Telegram polling {type(e).__name__} (#{failures}), "
                               f"retry in {delay}s: {str(e)[:160]}")
                time.sleep(delay)

    thread = threading.Thread(target=loop, daemon=True, name="telegram-polling")
    thread.start()
    return thread


def main(argv=None) -> int:
    args = parse_args(argv)
    log.setup()

    cfg = ConfigStore(args.config, state_file=args.state)
    with cfg.lock:
        apply_proxy(cfg.data)
    cfg.save()   # persist normalization (search ids, new defaults)

    if args.list_searches:
        _list_searches(cfg)
        return 0

    if args.add_url:
        try:
            parsed = parse_divar_search_url(args.add_url)
        except Exception as e:
            print(f"Could not parse URL: {e}", file=sys.stderr)
            return 1
        name = args.name or auto_name(args.add_url, len(cfg.searches()) + 1)
        search = cfg.add_search(args.add_url, name)
        print(f"Added '{name}' (id {search['id']}, city={parsed['city_slug']}, "
              f"category={parsed['api_category']}, filters={len(parsed['form_data'])}). "
              f"Total: {len(cfg.searches())}")
        return 0

    token = cfg.get("telegram_bot_token", "")
    if not token:
        logger.error("No telegram_bot_token in config.json (or TELEGRAM_BOT_TOKEN env). "
                     "Create a bot with @BotFather and put the token there.")
        return 1

    state = StateStore(args.state)
    if args.prune_days:
        removed = state.prune(args.prune_days)
        logger.info(f"Pruned {removed} listings older than {args.prune_days} days")

    bot = telebot.TeleBot(token, threaded=True)
    holder = BotHolder(bot)
    monitor = Monitor(cfg, state, holder)

    # ---- one-shot mode (cron/systemd oneshot) ----
    if args.check_once:
        if not cfg.enabled_searches():
            logger.error("No enabled searches. Add one with --add-url or from Telegram.")
            return 1
        seed = args.seed or not len(state)
        if seed:
            print("Seeding state (no alerts will be sent)...")
        stats = monitor.scan_all(seed_mode=seed)
        print(f"Done: total={stats.total} new={stats.new} updated={stats.updated} "
              f"seeded={stats.seeded} skipped_geo={stats.skipped_geo} "
              f"filtered_price={stats.filtered_price}")
        state.save()
        return 0

    # ---- long-running mode ----
    logger.info("=" * 52)
    logger.info("Divar.ir Rental Watchdog starting")
    logger.info("=" * 52)

    panel = AdminPanel(bot, cfg, state, monitor)
    panel.register()
    _start_polling(bot)

    if not cfg.has_admins():
        banner = (
            "\n" + "!" * 60 +
            f"\n  No admin configured yet.\n"
            f"  Open Telegram, message the bot and send:   /claim {panel.claim_code}\n"
            f"  (or put your numeric id in \"admin_ids\" in {args.config})\n" +
            "!" * 60 + "\n"
        )
        print(banner, flush=True)
        logger.warning("No admin configured — claim code printed to the console")

    searches = cfg.searches()
    enabled = cfg.enabled_searches()
    interval = cfg.get("check_interval", 900)
    logger.info(f"Tracking {len(state)} listings across {len(enabled)}/{len(searches)} enabled searches")
    logger.info(f"Interval {fmt_duration(interval)} split across {len(enabled)} searches "
                f"→ one scan every {fmt_duration(monitor.slot_seconds(len(enabled)))}")
    if cfg.get("proxy"):
        logger.info(f"Proxy: {mask_proxy(cfg.get('proxy'))}")

    # First run: record what is already listed instead of alerting on all of it.
    if enabled and not len(state):
        logger.info("First run — seeding state without alerts...")
        try:
            stats = monitor.scan_all(seed_mode=True)
            logger.info(f"Seeded {stats.seeded} listings")
        except Exception as e:
            logger.error(f"Seeding failed: {e} — will retry on the next scan")

    admin_ids = cfg.get("admin_ids", [])
    recipients = cfg.get("telegram_chat_ids", [])
    if recipients:
        names = esc(", ".join(s.get("name", "?") for s in enabled) or "none")
        notify_text(bot, recipients,
                    "🟢 <b>Divar Watchdog Started</b>\n\n"
                    f"🔎 Searches ({len(enabled)}): {names}\n"
                    f"⏱ Interval: {fmt_duration(interval)} "
                    f"(one scan every {fmt_duration(monitor.slot_seconds(len(enabled)))})\n"
                    f"📦 Tracked: {len(state)} listings\n"
                    f"👥 Recipients: {len(recipients)}",
                    parse_mode="HTML")
    for admin in admin_ids:
        if admin not in recipients:
            notify_text(bot, [admin], "🟢 Divar Watchdog started. Send /admin for the panel.")

    # ---- shutdown handling ----
    stopping = threading.Event()

    def _shutdown(signum, _frame):
        if stopping.is_set():
            print("\nForce quit.", flush=True)
            os._exit(130)
        stopping.set()
        logger.info(f"Signal {signum} received, saving state and exiting...")
        print("\nSaving state and exiting... (press Ctrl+C again to force quit)", flush=True)
        monitor.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except Exception:
            pass
    atexit.register(state.save)

    try:
        monitor.loop()
    except KeyboardInterrupt:
        monitor.stop()
    finally:
        state.save()
        cfg.save()
        logger.info("State saved, bye.")
    return 0
