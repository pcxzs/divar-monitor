"""Logging setup shared by every module."""

import logging
import re
from logging.handlers import RotatingFileHandler

LOG_FILE = "divar_watcher.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 3

# A Telegram bot token is "<bot id>:<secret>". requests puts the full API URL
# (https://api.telegram.org/bot<token>/sendMessage) into connection errors, so
# without this every proxy blip would write the token into the log.
BOT_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")

_configured = False


class RedactingFormatter(logging.Formatter):
    """Masks bot tokens in the final line, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return BOT_TOKEN_RE.sub("<bot-token>", super().format(record))


def setup(log_file: str = LOG_FILE, level: int = logging.INFO) -> None:
    """Console + rotating file logging. Safe to call more than once."""
    global _configured
    if _configured:
        return
    fmt = RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s")
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
