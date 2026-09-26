import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_list(name: str, default: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    env: str = os.getenv("APP_ENV", "dev")
    warehouse_path: str = os.getenv("WAREHOUSE_PATH", str(ROOT / "data" / "warehouse" / "settlement.duckdb"))
    raw_dir: str = os.getenv("RAW_DIR", str(ROOT / "data" / "raw"))
    sql_dir: str = os.getenv("SQL_DIR", str(ROOT / "sql"))

    settlement_sla_minutes: int = int(os.getenv("SETTLEMENT_SLA_MINUTES", "30"))
    settlement_rate_threshold: float = float(os.getenv("SETTLEMENT_RATE_THRESHOLD", "95.0"))
    sla_rate_threshold: float = float(os.getenv("SLA_RATE_THRESHOLD", "90.0"))
    late_event_threshold_sec: int = int(os.getenv("LATE_EVENT_THRESHOLD_SEC", "300"))
    event_sla_threshold_sec: int = int(os.getenv("EVENT_SLA_THRESHOLD_SEC", "900"))
    amount_tolerance: float = float(os.getenv("AMOUNT_TOLERANCE", "0.01"))
    allowed_currencies: tuple[str, ...] = ("INR",)

    api_key: str = os.getenv("API_KEY", "dev-local-key-change-me")
    api_keys_enabled: bool = os.getenv("API_KEYS_ENABLED", "true").lower() == "true"
    pii_salt: str = os.getenv("PII_SALT", "dev-salt-change-me")
    cors_origins: list[str] = field(default_factory=lambda: _env_list("CORS_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080"))
    max_date_range_days: int = int(os.getenv("MAX_DATE_RANGE_DAYS", "366"))
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    def __post_init__(self) -> None:
        if self.env not in PROTECTED_ENVIRONMENTS:
            return
        problems = []
        if not self.api_keys_enabled:
            problems.append("API_KEYS_ENABLED must be true")
        if self.api_key in PLACEHOLDER_SECRETS or len(self.api_key) < 24:
            problems.append("API_KEY is missing, a placeholder, or shorter than 24 characters")
        if self.pii_salt in PLACEHOLDER_SECRETS or len(self.pii_salt) < 16:
            problems.append("PII_SALT is missing, a placeholder, or shorter than 16 characters")
        if problems:
            raise ValueError(f"Refusing to start in {self.env}: " + "; ".join(problems))


PROTECTED_ENVIRONMENTS = {"test", "prod"}
PLACEHOLDER_SECRETS = {"", "dev-local-key-change-me", "dev-salt-change-me", "replace-me-from-secrets-manager"}

settings = Settings()


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
