"""X3: the effort every model call is pinned at — per call type, from `effort.yaml`.

Until 2026-10-30 nothing in this repo set an effort: the API used each model's default and the
Claude CLI used whatever the logged-in user had last chosen with `/effort`, so a change to the
owner's personal settings changed how hard the M17 managers thought. Every path that can send an
effort now reads it from here, and refuses a model this file does not place, rather than letting the
provider or the user's settings decide.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from analyst.llm.client import LLMError
from dataplatform.config import LlmEffort

__all__ = ["EFFORT_PATH", "EffortPolicy", "effort_for", "load_effort_policy"]

#: The versioned policy (see the file's header).
EFFORT_PATH: Final[Path] = Path(__file__).with_name("effort.yaml")


class EffortPolicy(BaseModel):
    """Call types and their efforts, and the call type each model serves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    call_types: dict[str, LlmEffort] = Field(min_length=1)
    models: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _every_model_has_a_known_call_type(self) -> EffortPolicy:
        unknown = {m: t for m, t in self.models.items() if t not in self.call_types}
        if unknown:
            raise ValueError(f"models name call types the policy does not define: {unknown}")
        return self

    def call_type(self, model: str) -> str:
        """The call type ``model`` serves, or an `LLMError` naming the file to add it to."""
        try:
            return self.models[model]
        except KeyError:
            raise LLMError(
                f"model {model!r} has no call type in {EFFORT_PATH.name}, so no effort can be "
                "pinned for it; add it there rather than let the provider or the user's settings "
                "choose"
            ) from None

    def effort_for(self, model: str) -> LlmEffort:
        """The effort a call to ``model`` is sent at."""
        return self.call_types[self.call_type(model)]


@lru_cache(maxsize=1)
def load_effort_policy(path: Path = EFFORT_PATH) -> EffortPolicy:
    """Parse and validate the policy, once per process. A schema break raises."""
    with path.open(encoding="utf-8") as handle:
        return EffortPolicy.model_validate(yaml.safe_load(handle))


def effort_for(model: str) -> LlmEffort:
    """The pinned effort for ``model`` under the checked-in policy."""
    return load_effort_policy().effort_for(model)
