"""Environment-based configuration for Forecast Studio Backend."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_origins(raw: str | None) -> List[str]:
    if not raw:
        return [
            "http://localhost:5500",
            "http://127.0.0.1:5500",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("ENVIRONMENT", "development").strip().lower()
    cors_origins: List[str] = field(default_factory=lambda: _parse_origins(os.getenv("CORS_ORIGINS")))
    firebase_project_id: str = os.getenv("FIREBASE_PROJECT_ID", "dashboard-forecast-demand").strip()
    firebase_storage_bucket: str = os.getenv(
        "FIREBASE_STORAGE_BUCKET",
        "dashboard-forecast-demand.firebasestorage.app",
    ).strip()
    firebase_service_account_json: str = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    google_application_credentials: str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    require_auth: bool = _parse_bool(os.getenv("REQUIRE_AUTH"), True)
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "25"))
    max_concurrent_jobs: int = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2")))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


settings = Settings()
