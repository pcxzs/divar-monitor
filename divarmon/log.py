"""Logging setup shared by every module."""

import logging
from logging.handlers import RotatingFileHandler

LOG_FILE = "divar_watcher.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3

_configured = False


def setup(log_file: str = LOG_FILE, level: int = logging.INFO) -> None:
    """Console + rotating file logging. Safe to call more than once."""
    global _configured
    if _configured:
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        # The previous build wrote an unbounded log that reached 30 MB.
        fileh = RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES,
                                    backupCount=LOG_BACKUPS, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except Exception as e:  # read-only dir, etc. — console logging still works
        root.warning(f"File logging disabled: {e}")

    # Telebot dumps a full traceback on every polling hiccup (proxy blip,
    # brief outage). Our polling loop logs a one-line warning and retries.
    logging.getLogger("TeleBot").setLevel(logging.CRITICAL)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _configured = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
