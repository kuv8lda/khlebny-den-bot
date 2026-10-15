from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr  # токен Telegram от @BotFather
    log_level: str = "INFO"
    fsm_db_path: str = "data/fsm.sqlite3"
    orders_db_path: str = ""  # пусто: заказы лежат в том же файле, что и FSM

    # Панель orders-admin-panel. Пустой panel_api_key выключает интеграцию.
    panel_base_url: str = "http://127.0.0.1:5000"
    panel_api_key: SecretStr = SecretStr("")
    panel_timeout_seconds: float = 5
    catalog_ttl_seconds: float = 60

    @field_validator("panel_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def orders_path(self) -> str:
        return self.orders_db_path or self.fsm_db_path

    @property
    def panel_enabled(self) -> bool:
        return bool(self.panel_api_key.get_secret_value().strip())


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
