"""Application configuration loaded from environment variables."""
import logging
import os
import sys


class Settings:
    def __init__(self) -> None:
        self.wahoo_client_id: str = os.environ.get("WAHOO_CLIENT_ID", "")
        self.wahoo_client_secret: str = os.environ.get("WAHOO_CLIENT_SECRET", "")
        self.wahoo_redirect_uri: str = os.environ.get("WAHOO_REDIRECT_URI", "")
        self.wahoo_webhook_token: str = os.environ.get("WAHOO_WEBHOOK_TOKEN", "")
        # AI provider: "anthropic" (default) o "openai"
        self.ai_provider: str = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()
        self.anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
        self.openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
        self.openai_model: str = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.app_name: str = os.environ.get("APP_NAME", "OpenFit")
        # Optional app-level login password (decoupled from Wahoo OAuth)
        self.app_password: str = os.environ.get("APP_PASSWORD", "")
        self.app_secret_key: str = os.environ.get("APP_SECRET_KEY", "")
        self.app_base_url: str = os.environ.get("APP_BASE_URL", "http://localhost:8080")
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.db_path: str = os.environ.get("DB_PATH", "/data/wahoo.db")
        self.fit_dir: str = os.environ.get("FIT_DIR", "/data/fits")
        # Refresh the Wahoo access token if it expires within this many minutes
        self.token_refresh_margin_min: int = int(os.environ.get("TOKEN_REFRESH_MARGIN_MIN", "10"))
        self.anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        # Google Health API (ex Fitbit) — opzionale, arricchisce nuoto/camminate
        self.google_client_id: str = os.environ.get("GOOGLE_CLIENT_ID", "")
        self.google_client_secret: str = os.environ.get("GOOGLE_CLIENT_SECRET", "")

    @property
    def ai_model(self) -> str:
        """Model name of the active AI provider."""
        return self.openai_model if self.ai_provider == "openai" else self.anthropic_model

    @property
    def ai_api_key(self) -> str:
        return self.openai_api_key if self.ai_provider == "openai" else self.anthropic_api_key

    def validate(self) -> list[str]:
        missing = []
        ai_key = "openai_api_key" if self.ai_provider == "openai" else "anthropic_api_key"
        for name in ("wahoo_client_id", "wahoo_client_secret", "wahoo_redirect_uri",
                     "wahoo_webhook_token", ai_key, "app_secret_key"):
            if not getattr(self, name):
                missing.append(name.upper())
        return missing


settings = Settings()


def setup_logging() -> None:
    """Structured-ish logging to stdout, compatible with `docker logs`."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    if settings.log_level != "DEBUG":
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
