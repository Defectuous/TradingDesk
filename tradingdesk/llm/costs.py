"""LLM usage records and cost estimates (USD) from the pricing table in config.yaml."""
from __future__ import annotations

from datetime import datetime

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from tradingdesk.sessions import now_et
from tradingdesk.settings import ModelPrice


class LLMUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent: str
    model: str
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
    cache_read_tokens: int = Field(0, ge=0)
    cache_write_tokens: int = Field(0, ge=0)
    local: bool = False
    ticker: str | None = None
    candidate_id: str | None = None
    at: datetime = Field(default_factory=now_et)
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


def estimate_cost(usage: LLMUsage, pricing: dict[str, ModelPrice]) -> float:
    """USD cost of one call. Local models are free. Unknown Claude models are
    costed at the most expensive known model, so the spend cap errs on the safe side."""
    if usage.local:
        return 0.0
    price = pricing.get(usage.model)
    if price is None:
        if not pricing:
            raise ValueError("llm.pricing is empty in config.yaml")
        price = max(pricing.values(), key=lambda p: p.output)
        logger.warning("No price for model {!r}; costing at the most expensive known rate", usage.model)
    per = 1_000_000
    cost = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / per
    return round(cost, 6)


def with_cost(usage: LLMUsage, pricing: dict[str, ModelPrice]) -> LLMUsage:
    return usage.model_copy(update={"cost_usd": estimate_cost(usage, pricing)})
