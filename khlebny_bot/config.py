from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_owner_ids(raw: str) -> frozenset[int]:
    """«1, 2 3» -> {1, 2, 3}. Пустая строка — владельцев нет."""
    parts = raw.replace(",", " ").split()
    try:
        return frozenset(int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("OWNER_TELEGRAM_IDS: ожидается список чисел через запятую") from exc


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
    catalog_push_token: SecretStr = SecretStr("")
    catalog_push_host: str = "127.0.0.1"
    catalog_push_port: int = Field(default=8081, ge=1, le=65535)
    events_poll_interval: float = Field(default=5, gt=0, allow_inf_nan=False)

    # Telegram ID владельцев через запятую. Пусто: уведомлений и команды /owner нет.
    owner_telegram_ids: str = ""

    @field_validator("catalog_push_token")
    @classmethod
    def _check_push_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if token and len(token.encode()) < 32:
            raise ValueError("CATALOG_PUSH_TOKEN must be at least 32 bytes")
        return value

    @field_validator("owner_telegram_ids")
    @classmethod
    def _check_owner_ids(cls, value: str) -> str:
        parse_owner_ids(value)
        return value

    @field_validator("panel_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def orders_path(self) -> str:
        return self.orders_db_path or self.fsm_db_path

    @property
    def owner_ids(self) -> frozenset[int]:
        return parse_owner_ids(self.owner_telegram_ids)

    @property
    def panel_enabled(self) -> bool:
        return bool(self.panel_api_key.get_secret_value().strip())


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
