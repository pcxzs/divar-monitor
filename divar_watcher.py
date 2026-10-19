#!/usr/bin/env python3
"""Divar.ir rental watchdog — Telegram alerts with a Telegram admin panel.

Run it:
    python divar_watcher.py                 # watch + admin panel
    python divar_watcher.py --check-once    # single scan (cron/systemd oneshot)
    python divar_watcher.py --add-url URL   # add a search link from the shell
    python divar_watcher.py --list-searches

Everything else is configured from Telegram: send /admin to the bot.
"""

import sys

from divarmon.app import main

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nExiting.", flush=True)
        sys.exit(0)
