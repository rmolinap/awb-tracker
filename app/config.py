from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    tracker_timeout_seconds: int = 30
    enable_screenshots: bool = False
    screenshot_dir: str = "./screenshots"
    playwright_headless: bool = True
    playwright_slowmo_ms: int = 0
    oxylabs_enabled: bool = False
    oxylabs_username: str = ""
    oxylabs_password: str = ""
    oxylabs_endpoint: str = "https://realtime.oxylabs.io/v1/queries"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
