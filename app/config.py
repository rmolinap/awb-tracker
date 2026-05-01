from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    tracker_timeout_seconds: int = 30
    enable_screenshots: bool = False
    screenshot_dir: str = "./screenshots"
    playwright_headless: bool = True
    playwright_slowmo_ms: int = 0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
