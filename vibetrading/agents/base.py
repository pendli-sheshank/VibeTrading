from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from vibetrading.core.enums import AgentType
from vibetrading.core.models import AgentOutput, Signal, Stock


class Agent(ABC):
    """Shared contract for data-collecting agents: Research's news/chat
    collectors and the Strategy Agent's technical-indicator step.

    Each call produces one AgentOutput for one stock at one point in time;
    the orchestrator persists it and the Strategy Agent later reads the
    latest output per AgentType to synthesize a Signal.
    """

    agent_type: AgentType
    run_interval_seconds: int = 300

    @abstractmethod
    async def analyze(self, stock: Stock, context: dict[str, Any]) -> AgentOutput:
        ...


class SynthesisAgent(ABC):
    """Contract for agents that combine other agents' outputs into a decision.

    Separate from Agent because a SynthesisAgent consumes AgentOutput objects
    rather than raw market/news/social data — the Strategy Agent (combining
    Research + Technical) is the primary implementation.
    """

    @abstractmethod
    async def synthesize(self, stock: Stock, outputs: list[AgentOutput]) -> Signal:
        ...
