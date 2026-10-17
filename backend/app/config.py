"""Settings loaded from the project-root .env.

Every path is resolved against PROJECT_ROOT (the candidate-package folder) and must stay inside it:
the copilot never reads or writes outside that folder.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/ -> project root
DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    project_root: Path = DEFAULT_PROJECT_ROOT

    database_url: str = "postgresql+psycopg://copilot:copilot@localhost:55432/copilot"
    test_database_url: str = "postgresql+psycopg://copilot:copilot@localhost:55432/copilot_test"

    openai_api_key: str = ""
    answer_model: str = "gpt-6.1-sol"
    answer_effort: str = "low"  # measured: halves generation time, no accuracy loss (README, Latency)
    answer_escalate_model: str = "gpt-6-astra"
    fast_model: str = "gpt-6-luna"
    fast_effort: str = "low"
    vision_model: str = "gpt-6-astra"
    judge_model: str = "gpt-6-astra"
    embed_model: str = "text-embedding-3-large"
    embed_dims: int = 3072
    reranker: str = "llm"
    # The curated conflict register (config/conflicts.yaml) holds business-judgment overrides. Off = general
    # rules only, to measure what generalises to documents nobody has curated.
    conflict_register: bool = True

    corpus_root: Path = Path("./corpus")
    users_file: Path = Path("./users.json")
    permissions_file: Path = Path("./permissions.json")
    questions_file: Path = Path("./eval/questions.json")
    var_dir: Path = Path("./var")
    reports_dir: Path = Path("./reports")
    config_dir: Path = Path("./config")

    top_k: int = 40
    context_k: int = 10
    coverage_threshold: float = 1.5

    wo_approver_role: str = Field(default="role-lead", pattern="^(any|role-lead)$")
    demo_auth: bool = True
    session_secret: str = "change-me-for-anything-but-a-local-demo"

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        root = self.project_root.resolve()
        self.project_root = root
        for name in (
            "corpus_root",
            "users_file",
            "permissions_file",
            "questions_file",
            "var_dir",
            "reports_dir",
            "config_dir",
        ):
            setattr(self, name, resolve_inside(root, getattr(self, name)))
        return self


def resolve_inside(root: Path, path: Path) -> Path:
    """Resolve `path` against `root` and refuse anything that escapes it."""
    resolved = (path if path.is_absolute() else root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{path} resolves outside the project folder {root}")
    return resolved


@lru_cache
def get_settings() -> Settings:
    return Settings()
