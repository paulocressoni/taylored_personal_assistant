"""pydantic-settings (reads .env once)"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for the application."""

    # Configuration for pydantic-settings
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # DeepSeek API key for LLM client
    deepseek_api_key: str = ""

    # Check if DeepSeek API key is available
    @property
    def has_deepseek(self) -> bool:
        return bool(self.deepseek_api_key)


# Create an instance of the settings
settings = Settings()
