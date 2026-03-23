from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_url: str = Field(...)
    deploy_environment: str = Field(...)
    auth_algorithm: str = Field(...)
    auth_secret_key: str = Field(...)
    auth_access_token_expire_minutes: int = Field(...)
    sql_admin_username: str = Field(...)
    sql_admin_password: str = Field(...)
    sql_admin_secret_key: str = Field(...)
    neo4j_db_url: str = Field(...)
    neo4j_db_username: str = Field(...)
    neo4j_db_password: str = Field(...)
    redis_host: str = Field(...)
    redis_port: str = Field(...)
    nostr_transfer_from_relay: str = Field(...)
    nostr_transfer_to_relay: str = Field(...)
    nostr_transfer_to_relay2: str | None = Field(None)
    nostr_upload_ta_events_relay: str = Field(...)
    nostr_upload_ta_events_relay_public_url: str = Field(...)
    cutoff_of_valid_graperank_scores: float = Field(...)
    perform_nostr_full_sync: bool = Field(...)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
