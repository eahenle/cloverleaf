from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    workspace: Path = Field(default=Path("workspace"), validation_alias="CLOVERLEAF_WORKSPACE")
    main_file: str = Field(default="main.tex", validation_alias="CLOVERLEAF_MAIN_FILE")
    ai_provider: str = Field(default="codex", validation_alias="AI_PROVIDER")
    ai_model: str = Field(default="gpt-5.6-sol", validation_alias="AI_MODEL")
    ai_base_url: str = Field(default="", validation_alias="AI_BASE_URL")
    ai_api_key: str = Field(default="", validation_alias="AI_API_KEY")
    codex_bin: str = Field(default="", validation_alias="CLOVERLEAF_CODEX_BIN")
    project_state_file: str = Field(default="", validation_alias="CLOVERLEAF_PROJECT_STATE")

    @property
    def project_state_path(self) -> Path:
        if self.project_state_file.strip():
            configured = Path(self.project_state_file).expanduser()
            return (configured if configured.is_absolute() else Path.cwd() / configured).resolve()
        return self.workspace.parent / ".cloverleaf-project.json"

    @field_validator("workspace")
    @classmethod
    def resolve_workspace(cls, value: Path) -> Path:
        return (value if value.is_absolute() else Path.cwd() / value).resolve()

    @field_validator("main_file")
    @classmethod
    def validate_main_file(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not value.endswith(".tex"):
            raise ValueError("CLOVERLEAF_MAIN_FILE must be a relative .tex path")
        return path.as_posix()


@lru_cache
def get_settings() -> Settings:
    return Settings()
