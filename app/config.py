"""Environment configuration. Everything else (bot token, admin IDs, prices...) lives in the DB and is edited from the web panel."""
import os
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


SECRET_KEY = _env("SECRET_KEY")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")

_db = _env("DATABASE_URL", "sqlite+aiosqlite:///./data.db")
if _db.startswith("postgres://"):
    _db = "postgresql+asyncpg://" + _db[len("postgres://"):]
elif _db.startswith("postgresql://"):
    _db = "postgresql+asyncpg://" + _db[len("postgresql://"):]
DATABASE_URL = _db

EITAA_API_BASE = _env("EITAA_API_BASE", "https://eitaayar.ir/api").rstrip("/")
EITAA_PROXY = _env("EITAA_PROXY") or None

RUBIKA_API_BASE = _env("RUBIKA_API_BASE", "https://botapi.rubika.ir/v3").rstrip("/")
RUBIKA_PROXY = _env("RUBIKA_PROXY") or None

# optional fallbacks, the panel values win
ENV_BOT_TOKEN = _env("BOT_TOKEN")
ENV_RUBIKA_TOKEN = _env("RUBIKA_BOT_TOKEN")
ENV_ADMIN_IDS = _env("ADMIN_IDS")

TZ = ZoneInfo("Asia/Tehran")

MISSING: list[str] = []
if len(SECRET_KEY) < 16:
    MISSING.append("SECRET_KEY (حداقل ۱۶ کاراکتر)")
if len(ADMIN_PASSWORD) < 6:
    MISSING.append("ADMIN_PASSWORD (حداقل ۶ کاراکتر)")
