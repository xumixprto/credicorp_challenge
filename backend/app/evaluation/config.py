from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from shared.settings import PROJECT_ROOT


class EvaluationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: Path = Path("data/evaluations")
    judge_model: str = "gpt-4.1-mini"
    k: int = Field(default=5, ge=1, le=100)
    batch_size: int = Field(default=3, ge=1, le=20)
    max_concurrency: int = Field(default=2, ge=1, le=8)
    max_input_tokens: int = Field(default=24000, ge=1000)
    max_output_tokens: int = Field(default=8000, ge=256)
    timeout_seconds: float = Field(default=90, gt=0)
    max_retries: int = Field(default=1, ge=0, le=3)


def load_evaluation_config(path: Path) -> EvaluationConfig:
    with (PROJECT_ROOT / path).open(encoding="utf-8") as handle:
        return EvaluationConfig.model_validate((yaml.safe_load(handle) or {}).get("evaluation", {}))
