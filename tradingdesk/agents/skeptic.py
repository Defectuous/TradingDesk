"""
Skeptic gate. Phase 2 ships a stub that always passes; phase 6 replaces it with
the adversarial Claude call.

A Skeptic only returns a Verdict ("pass" or "reject"). The Manager hands it a
deep copy of the candidate, so it can't modify the real one.
"""
from __future__ import annotations

from typing import Protocol

from tradingdesk.models import Agent, Candidate, Verdict


class Skeptic(Protocol):
    def review(self, candidate: Candidate) -> Verdict: ...


class StubSkeptic:
    """Always passes. Placeholder until phase 6."""

    def review(self, candidate: Candidate) -> Verdict:
        return Verdict(agent=Agent.SKEPTIC, decision="pass", reasons=("stub Skeptic (phase 2): no review yet",))
