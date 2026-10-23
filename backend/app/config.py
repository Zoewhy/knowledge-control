from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "alg-qa-agent"
    app_env: str = "development"
    api_prefix: str = "/api/v1"
    secret_key: str = "change-me"
    database_url: str = "sqlite:///./alg_qa.db"
    redis_url: str = "redis://localhost:6379/0"
    elasticsearch_url: str = "http://localhost:9200"
    bailian_api_key: str = ""
    bailian_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    bailian_chat_model: str = "qwen-plus"
    bailian_embedding_model: str = "text-embedding-v3"
    bailian_embedding_dimensions: int = 1024
    llm_mock_mode: bool = True
    enable_elasticsearch: bool = False
    cors_origins: List[str] = ["http://localhost:5173", "http://localhost:8080"]
    retrieval_timeout_seconds: float = 2.5
    retrieval_max_workers: int = 5
    wiki_fallback_score: float = 0.42
    agent_execution_mode: str = "sync"
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_base_url: str = "https://open.feishu.cn/open-apis"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value):
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
