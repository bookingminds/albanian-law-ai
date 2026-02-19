"""Application configuration loaded from environment variables."""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class Settings(BaseSettings):
    # OpenAI
    OPENAI_API_KEY: str = ""

    # Models
    LLM_MODEL: str = "gpt-4o-mini"
    EMBEDDING_MODEL: str = "text-embedding-3-small"

    # Chunking
    CHUNK_SIZE: int = 900
    CHUNK_OVERLAP: int = 150

    # RAG — retrieval
    TOP_K_RESULTS: int = 10
    SIMILARITY_THRESHOLD: float = 0.55  # cosine distance gate per-chunk

    # Confidence gate — minimum similarity for the BEST chunk
    # If the top chunk's similarity < this value, refuse to answer.
    # For text-embedding-3-small on Albanian legal text, typical relevant
    # similarities are 0.45–0.65.  Set higher for stricter gating.
    CONFIDENCE_MIN_SIMILARITY: float = 0.35

    # Hybrid search (per-query defaults — overridden by multi-query)
    HYBRID_VECTOR_WEIGHT: float = 0.6   # weight for vector results in RRF
    HYBRID_KEYWORD_WEIGHT: float = 0.4  # weight for keyword results in RRF
    HYBRID_FETCH_K: int = 40            # fetch per method per single query
    HYBRID_FINAL_K: int = 8             # return per single query

    # Accuracy-first multi-query search
    MQ_FETCH_K: int = 150               # fetch per method per variant (wide recall)
    MQ_FINAL_K: int = 40                # final chunks after merge+rerank
    MQ_STITCH_WINDOW: int = 2           # neighbor chunks ±2 for context
    MQ_COVERAGE_MAX_PASSES: int = 3     # max coverage-check loops
    MQ_COVERAGE_EXTRA_K: int = 10       # chunks per gap-fill query

    # Caching
    EMBEDDING_CACHE_SIZE: int = 512     # max cached embeddings (higher for multi-query)
    SEARCH_CACHE_TTL: int = 300         # search result cache TTL in seconds

    # Supabase Auth
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # Legacy JWT (used as fallback if Supabase not configured)
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_DAYS: int = 7
    ADMIN_EMAIL: str = ""

    # Free trial
    TRIAL_DAYS: int = 1  # 1-day free trial
    MAX_SIGNUPS_PER_IP_24H: int = 2
    BLOCK_DISPOSABLE_EMAILS: bool = True

    # Google Play Billing
    GOOGLE_PLAY_PACKAGE_NAME: str = "com.zagrid.albanianlawai"
    GOOGLE_PLAY_PRODUCT_ID: str = "law_ai_monthly"
    SUBSCRIPTION_PRICE_EUR: float = 4.99

    SERVER_URL: str = "http://localhost:8000"
    FRONTEND_URL: str = "http://localhost:8000"

    # Paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    UPLOAD_DIR: Path = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads")))
    DATA_DIR: Path = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
    DB_PATH: Path = Path(os.environ.get("DB_PATH", str(BASE_DIR / "albanian_law.db")))

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

settings.UPLOAD_DIR.mkdir(exist_ok=True)
settings.DATA_DIR.mkdir(exist_ok=True)
