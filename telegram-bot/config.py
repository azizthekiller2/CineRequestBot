import os
import logging

logger = logging.getLogger(__name__)

def _safe_int(v, default=0):
    try:
        return int(v) if v and str(v).strip() else default
    except (ValueError, AttributeError):
        return default

API_ID      = _safe_int(os.environ.get("API_ID", ""))
API_HASH    = os.environ.get("API_HASH", "")
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
OWNER_ID    = _safe_int(os.environ.get("OWNER_ID", ""))
LOG_CHANNEL = _safe_int(os.environ.get("LOG_CHANNEL", ""))

MONGO_URI = os.environ.get("MONGO_URI", "")

CHANNEL        = os.environ.get("UPDATES_CHANNEL", "")
BACKUP_CHANNEL = os.environ.get("BACKUP_CHANNEL", "")
_rc = os.environ.get("RESULTS_CHANNEL", "0")
try:
    RESULTS_CHANNEL = int(_rc)          # numeric ID e.g. -1004413455841
except ValueError:
    RESULTS_CHANNEL = _rc               # @username e.g. @MyResultsChannel

SEARCH_REPLY_TTL = int(os.environ.get("SEARCH_REPLY_TTL", 600))  # 10 mins default
WELCOME_TTL      = 120
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", os.environ.get("PORT", 5000)))
PORT        = HEALTH_PORT

SESSION = os.environ.get("SESSION", "")
