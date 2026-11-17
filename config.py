from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent

class Settings(BaseSettings):
    APP_NAME: str = "FastAPI"
    ALLOW_ORIGIN: str = "http://localhost:3000"
    DB_HOST: str = "host.docker.internal"
    DB_PORT: str = 5432
    DB_NAME: str = "mydb"
    DB_USER: str = "myuser"
    DB_PASSWORD: str = "mypassword"


    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True
    )

settings = Settings()