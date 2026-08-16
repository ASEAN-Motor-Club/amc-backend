"""Structured LLM response model for name moderation.

Mirrors amc-peripheral's `ai_models.py` pattern: a Pydantic v2 model passed as
`response_format` to `client.beta.chat.completions.parse(...)`, which returns an
instance of this model from `choices[0].message.parsed`. The strict field types
(let openai/pydantic raise on malformed) plus bounded enums keep the auto-rename
decision deterministic.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# Allowed moderation action outcomes the LLM may return.
RecommendedAction = Literal["rename", "none", "manual_review"]


class NameVerdict(BaseModel):
    """Judgement for a single display name by the LLM (Stage B)."""

    name: str
    is_violation: bool
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    categories: List[str] = Field(default_factory=list)
    reason: str = ""
    # The LLM proposes the clean replacement when is_violation is True.
    suggested_name: Optional[str] = None
    # "rename" means the caller may auto-apply; "manual_review" routes to a
    # human; "none" means no action.
    recommended_action: RecommendedAction = "none"