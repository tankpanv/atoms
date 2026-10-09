"""Small typed configuration surface for generated services.

Only platform-injected values are read here. Secrets never belong in frontend
code, source files, or checked-in env files.
"""
from dataclasses import dataclass
import os


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Atoms application")
    environment: str = os.getenv("APP_ENV", "development")
    debug: bool = _bool("APP_DEBUG")
    cors_origins: str = os.getenv("APP_CORS_ORIGINS", "")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}


settings = Settings()
