"""The single interface every cloud plugs into.

The two-cloud benchmarks had two protocols, ``ExchangeRateTool`` and
``RemoteCurrencyAgent``, with identical shapes -- an artifact of MCP and A2A
being wired in one at a time. A mesh has no such asymmetry: a local MCP tool
and a remote agent on another continent are both just named quote sources.
"""

from dataclasses import dataclass
from typing import Protocol

from coordinator.models import ConversionQuote, ConversionRequest


class QuoteSource(Protocol):
    async def convert(self, request: ConversionRequest) -> list[ConversionQuote]: ...


@dataclass(frozen=True)
class Participant:
    """A named quote source, plus the metadata the matrix report needs."""

    name: str
    source: QuoteSource
    cloud: str = "local"
    stack: str = "in-process"

    def __str__(self) -> str:
        return self.name
