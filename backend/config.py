from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    extraction_provider: str = "gemini"
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    extraction_model: str = "claude-sonnet-4-20250514"

    oauth_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    oauth_client_port: int = 8765
    database_path: str = str(_ROOT / "household.db")
    host: str = "127.0.0.1"
    port: int = 8000

    swiggy_mcp_url: str = "https://mcp.swiggy.com/im"
    swiggy_auth_authorize_url: str = "https://mcp.swiggy.com/auth/authorize"
    swiggy_auth_token_url: str = "https://mcp.swiggy.com/auth/token"
    swiggy_resource_metadata_url: str = (
        "https://mcp.swiggy.com/im/.well-known/oauth-protected-resource"
    )

    household_id: str = "default"
    senders: list[str] = ["Alice", "Bob", "Priya", "Sam"]
    debug_pipeline: bool = True

    smart_product_picker: bool = True
    poll_open_seconds: int = 90

    telegram_bot_token: str = ""
    public_base_url: str = "http://127.0.0.1:8000"

    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v25.0"
    # Comma-separated phone:name — e.g. 91XXXXXXXXXX:Mom,91XXXXXXXXXX:Dad
    whatsapp_phone_map: str = ""

    admin_username: str = "admin"
    admin_password: str = "changeme"
    admin_session_secret: str = "change-me-admin-session-secret"


settings = Settings()


def _strip(s: str) -> str:
    return s.strip()


# Meta tokens often get accidental spaces when pasted into .env
settings.whatsapp_access_token = _strip(settings.whatsapp_access_token)
settings.whatsapp_phone_number_id = _strip(settings.whatsapp_phone_number_id)
settings.whatsapp_verify_token = _strip(settings.whatsapp_verify_token)


def gemini_configured() -> bool:
    key = settings.gemini_api_key.strip()
    return bool(key) and key != "paste-your-gemini-key-here"
