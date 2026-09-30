from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parent.parent / ".env", extra="ignore")

    DATABASE_URL: str
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    PAYSTACK_SECRET_KEY: str = ""
    PAYSTACK_PUBLIC_KEY: str = ""
    PAYSTACK_BASE_URL: str = "https://api.paystack.co"
    PLATFORM_WITHDRAWAL_FEE_PERCENT: float = 2.5
    BITNOB_CLIENT_ID: str = ""
    BITNOB_CLIENT_SECRET: str = ""
    # Signs Bitnob's card webhooks (x-bitnob-signature, HMAC-SHA512 of the raw
    # body). Bitnob's docs only say "your secret key"; when this is empty the
    # client secret is used, which is how other integrations have it working.
    BITNOB_WEBHOOK_SECRET: str = ""
    # Private half (base64 of the PKCS8 PEM) of the RSA key whose public half is
    # registered in Bitnob's dashboard (Settings -> Controls -> Card encryption
    # key). Decrypts GET /api/cards/{id}/secure. Empty = card details can't be shown.
    BITNOB_CARD_PRIVATE_KEY_B64: str = ""
    DEMO_GHS_USD_RATE: float = 15.5
    LOG_LEVEL: str = "INFO"  # app modules; httpx/apscheduler are kept at WARNING (see main.py)
    USMS_BASE_URL: str = "https://webapp.usmsgh.com"
    USMS_SENDER_ID: str = ""
    USMS_TOKEN: str = ""



settings = Settings()
