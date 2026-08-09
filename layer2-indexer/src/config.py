from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Memgraph
    memgraph_host: str = "localhost"
    memgraph_port: int = 7687

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "code_entities"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # Ollama
    ollama_url: str = "http://localhost:11434"
    embedding_model: str = "nomic-embed-text"  # nomic-embed-text v1.5 supports code
    embedding_dim: int = 768
    description_model: str = "qwen2.5-coder:1.5b"  # local LLM for summaries

    # Claude (for descriptions — Haiku is cheapest)
    anthropic_api_key: str = ""
    description_llm: str = "claude-haiku-4-5-20251001"  # cheapest Claude model

    # GitHub App
    github_app_id: str = ""
    github_app_private_key: str = ""    # PEM content (not file path)
    github_webhook_secret: str = ""

    # Local repo cache
    repo_cache_dir: str = "/tmp/repos"

    # Indexing behaviour
    max_file_size_bytes: int = 512_000  # skip files > 512KB
    large_file_line_threshold: int = 500  # use hierarchical summarization above this
    skip_descriptions: bool = False  # skip LLM description calls, use docstring/name fallback


settings = Settings()
