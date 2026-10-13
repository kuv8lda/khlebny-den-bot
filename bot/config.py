from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    log_level: str = "INFO"
    fsm_db_path: str = "data/fsm.sqlite3"
    orders_db_path: str = ""  # пусто: заказы лежат в том же файле, что и FSM

    @property
    def orders_path(self) -> str:
        return self.orders_db_path or self.fsm_db_path


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
