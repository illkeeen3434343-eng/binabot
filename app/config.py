"""Configuration: everything comes from environment variables / the .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] not in "\"'" and " #" in v:        # inline comment
            v = v.split(" #", 1)[0].strip()
        os.environ.setdefault(k.strip(), v.strip("\"'"))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    bot_token: str = ""
    admin_ids: frozenset = frozenset()
    db_path: str = "data/binabot.sqlite3"
    log_dir: str = "logs"
    timezone: str = "Asia/Baku"
    map_path: str = str(ROOT / "bina_map.json")
    check_interval: int = 300
    request_min_interval: float = 3.0
    request_timeout: float = 30.0
    request_retries: int = 2
    concurrency: int = 1
    max_pages: int = 5
    seed_pages: int = 2
    new_max_age_days: float = 2.0
    ids_per_day: int = 2800
    rebaseline_hours: float = 24.0
    block_cooldown_min: int = 15
    block_cooldown_max_min: int = 120
    persisted_hash: str = "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073"
    unknown_params: str = "reject"
    queue_ttl_hours: float = 24.0
    queue_reset_at_midnight: bool = True
    verify_after_min: int = 60
    dispatch_interval: int = 15
    send_delay: float = 1.1
    report_hour: int = 9
    reminder_hour: int = 10
    default_sub_days: int = 30
    alert_cooldown_min: int = 30
    payment_info: str = ""
    telegram_api_base: str = ""
    check_first_delay: int = 5
    extra: dict = field(default_factory=dict)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    ids = frozenset(int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x)
    db = os.environ.get("DB_PATH", "data/binabot.sqlite3")
    logd = os.environ.get("LOG_DIR", "logs")
    return Config(
        bot_token=os.environ.get("BOT_TOKEN", "").strip(),
        admin_ids=ids,
        db_path=db if os.path.isabs(db) else str(ROOT / db),
        log_dir=logd if os.path.isabs(logd) else str(ROOT / logd),
        timezone=os.environ.get("TIMEZONE", "Asia/Baku"),
        map_path=os.environ.get("BINA_MAP", str(ROOT / "bina_map.json")),
        check_interval=_i("CHECK_INTERVAL_SEC", 300),
        request_min_interval=_f("REQUEST_MIN_INTERVAL_SEC", 3),
        request_timeout=_f("REQUEST_TIMEOUT_SEC", 30),
        request_retries=_i("REQUEST_RETRIES", 2),
        concurrency=max(1, _i("SCRAPE_CONCURRENCY", 1)),
        max_pages=max(1, _i("MAX_PAGES", 5)),
        seed_pages=max(1, _i("SEED_PAGES", 2)),
        new_max_age_days=_f("NEW_MAX_AGE_DAYS", 2),
        ids_per_day=_i("BINA_IDS_PER_DAY", 2800),
        rebaseline_hours=_f("REBASELINE_HOURS", 24),
        block_cooldown_min=_i("BLOCK_COOLDOWN_MIN", 15),
        persisted_hash=os.environ.get("BINA_PERSISTED_HASH", Config.persisted_hash),
        unknown_params=os.environ.get("UNKNOWN_PARAMS", "reject").lower(),
        queue_ttl_hours=_f("QUEUE_TTL_HOURS", 24),
        queue_reset_at_midnight=_b("QUEUE_RESET_AT_MIDNIGHT", True),
        verify_after_min=_i("VERIFY_AFTER_MIN", 60),
        report_hour=_i("REPORT_HOUR", 9),
        reminder_hour=_i("REMINDER_HOUR", 10),
        default_sub_days=_i("DEFAULT_SUB_DAYS", 30),
        payment_info=os.environ.get("PAYMENT_INFO", ""),
        telegram_api_base=os.environ.get("TELEGRAM_API_BASE", "").strip(),
        check_first_delay=_i("CHECK_FIRST_DELAY_SEC", 5),
    )
