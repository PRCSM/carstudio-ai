from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_base_url: str = "http://localhost:8000"
    frontend_origin: str = "http://localhost:3000"

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/carstudio"
    redis_url: str = "redis://localhost:6379/0"

    supabase_url: str = ""
    supabase_service_role_key: str = ""
    storage_originals_bucket: str = "car-originals"
    storage_outputs_bucket: str = "car-results"
    signed_url_ttl_seconds: int = 900

    # Provider fallback chain, tried left to right.
    image_provider_chain: str = "fal_flux_kontext,gemini_free,local_composite"
    fal_key: str = ""
    fal_model: str = "fal-ai/flux-pro/kontext"  # verify current model path on fal.ai
    gemini_paid_api_key: str = ""
    gemini_free_api_key: str = ""
    gemini_image_model: str = "gemini-2.5-flash-image"  # verify current ID in AI Studio
    gemini_vision_model: str = "gemini-2.5-flash"  # verify current ID in AI Studio
    vision_enabled: bool = True
    # Free-tier providers may use inputs for training; only allowed when the customer's
    # consent version covers it.
    free_tier_consent_versions: str = ""

    # Rough per-call prices (USD) used for budget accounting; keep in sync with provider pages.
    price_fal_flux_kontext: float = 0.04
    price_gemini_paid: float = 0.039
    price_gemini_vision: float = 0.002

    n8n_internal_api_token: str = ""
    n8n_callback_url: str = ""
    n8n_callback_hmac_secret: str = ""
    customer_hash_salt: str = "change-me"
    admin_api_token: str = ""

    sentry_dsn: str = ""
    max_upload_mb: int = 15
    max_image_pixels: int = 40_000_000
    min_image_side: int = 512
    max_generation_attempts: int = 3
    daily_anonymous_quota: int = 2
    daily_customer_quota: int = 10
    monthly_spend_limit_usd: float = 5.0
    image_retention_days: int = 30
    styles: str = "white_studio,dark_luxury,showroom"
    provider_circuit_seconds: int = 300

    cors_origins: list[str] = Field(default_factory=list)

    @property
    def chain(self) -> list[str]:
        return [p.strip() for p in self.image_provider_chain.split(",") if p.strip()]

    @property
    def style_list(self) -> list[str]:
        return [s.strip() for s in self.styles.split(",") if s.strip()]

    @property
    def free_tier_consents(self) -> set[str]:
        return {v.strip() for v in self.free_tier_consent_versions.split(",") if v.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
