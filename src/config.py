from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Azure AD — client credentials flow
    azure_tenant_id: str
    azure_client_id: str
    azure_client_secret: str

    # Public URL of this service (set to https://<app>.railway.app/webhook)
    notification_url: str = ""

    # Shared secret returned in every Graph notification for validation
    webhook_client_state: str = "truegraph-v1"

    # CPA service on Railway
    cpa_base_url: str
    cpa_api_key: str = ""
    cpa_tenant_id: str = "default"

    # Railway Postgres (DATABASE_URL injected automatically)
    database_url: str

    # Operating mode: shadow | warn | enforce
    mode: str = "shadow"

    # p_authentic below this threshold is flagged
    flag_threshold: float = 0.55

    @property
    def async_db_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


settings = Settings()
