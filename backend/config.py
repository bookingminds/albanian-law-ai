"""Application configuration loaded from environment variables."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings(BaseSettings):
    # OpenAI
    OPENAI_API_KEY: str = ""

    # Models
    LLM_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Chunking
    CHUNK_SIZE: int = 1000
    CHUNK_OVERLAP: int = 200

    # RAG
    TOP_K_RESULTS: int = 5

    # Auth
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_DAYS: int = 7
    ADMIN_EMAIL: str = ""  # First user with this email becomes admin (or set in DB)

    # Free trial: starts at signup, full access for TRIAL_DAYS; after that, paywall on premium actions (e.g. chat)
    TRIAL_DAYS: int = 3
    MAX_SIGNUPS_PER_IP_24H: int = 2  # Max accounts per IP in last 24h
    BLOCK_DISPOSABLE_EMAILS: bool = True  # Block known disposable/temp email domains

    # Stripe (subscription €9.99/month)
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_ID: str = ""  # Price ID for €9.99/month recurring
    FRONTEND_URL: str = "http://localhost:8000"

    # PayPal (subscription €9.99/month)
    PAYPAL_CLIENT_ID: str = ""
    PAYPAL_CLIENT_SECRET: str = ""
    PAYPAL_MODE: str = "sandbox"  # sandbox | live
    PAYPAL_PLAN_ID: str = ""  # Plan ID from PayPal dashboard or API

    # Paths (override via env vars for production / Docker)
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    UPLOAD_DIR: Path = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads")))
    DATA_DIR: Path = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
    DB_PATH: Path = Path(os.environ.get("DB_PATH", str(BASE_DIR / "albanian_law.db")))

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

# Ensure directories exist
settings.UPLOAD_DIR.mkdir(exist_ok=True)
settings.DATA_DIR.mkdir(exist_ok=True)
