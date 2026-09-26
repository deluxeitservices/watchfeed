import os


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./watchfeed.db")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings:
    DEMO_MODE = _bool("WATCHFEED_DEMO", False)
    # Demo must never read the project's live database, even if DATABASE_URL is set.
    DATABASE_URL = ("sqlite:///./watchfeed_demo.db" if DEMO_MODE
                    else _database_url())

    # WAHA (WhatsApp HTTP API) bridge
    WAHA_URL = os.getenv("WAHA_URL", "http://waha:3000").rstrip("/")
    WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
    WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
    # URL WAHA uses to reach this app (inside docker network)
    APP_INTERNAL_URL = os.getenv("APP_INTERNAL_URL", "http://app:8000").rstrip("/")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

    # AI extraction
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "4"))
    MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "12000"))

    # Access
    STAFF_USER = os.getenv("STAFF_USER", "staff")
    STAFF_PASSWORD = os.getenv("STAFF_PASSWORD", "")
    ADMIN_USER = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

    # Behaviour
    BASE_CURRENCY = os.getenv("BASE_CURRENCY", "GBP").upper()
    AUTO_ENABLE_NEW_GROUPS = _bool("AUTO_ENABLE_NEW_GROUPS", False)
    DUPLICATE_WINDOW_DAYS = int(os.getenv("DUPLICATE_WINDOW_DAYS", "14"))
    WORKER_ENABLED = _bool("WORKER_ENABLED", True)
    FX_URL = os.getenv("FX_URL", "https://api.frankfurter.app/latest?from=GBP")


settings = Settings()
