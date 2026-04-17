"""Production config — 12-Factor: tất cả từ environment variables."""
import logging
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    environment: str = "development"
    debug: bool = False

    # App
    app_name: str = "Production AI Agent"
    app_version: str = "1.0.0"

    # LLM
    openai_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # Security
    agent_api_key: str = "dev-key-change-me"
    jwt_secret: str = "dev-jwt-secret"
    allowed_origins: str = "*"

    # Rate limiting
    rate_limit_per_minute: int = 3

    # Answer length limit (words)
    max_answer_words: int = 80

    # Budget
    monthly_budget_usd: float = 10.0
    daily_budget_usd: float = 5.0

    # Storage
    redis_url: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    def get_allowed_origins(self) -> list[str]:
        return self.allowed_origins.split(",")

    def validate_production(self):
        logger = logging.getLogger(__name__)
        if self.environment == "production":
            if self.agent_api_key == "dev-key-change-me":
                raise ValueError("AGENT_API_KEY must be set in production!")
            if self.jwt_secret == "dev-jwt-secret":
                raise ValueError("JWT_SECRET must be set in production!")
        if not self.openai_api_key:
            logger.warning("OPENAI_API_KEY not set — using mock LLM")
        return self


settings = Settings().validate_production()
