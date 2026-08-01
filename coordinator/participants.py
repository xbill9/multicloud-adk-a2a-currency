"""The single interface every cloud plugs into.

The two-cloud benchmarks had two protocols, ``ExchangeRateTool`` and
``RemoteCurrencyAgent``, with identical shapes -- an artifact of MCP and A2A
being wired in one at a time. A mesh has no such asymmetry: a local MCP tool
and a remote agent on another continent are both just named quote sources.

``QuoteSource`` deliberately says nothing about credentials. The credential is
a property of the *leg*, not of the conversion, and it is resolved once when
the source is constructed -- ``credentials_for(peer, endpoint)`` in
``coordinator.auth``, re-exported here because this is the interface it hangs
off. One adapter, three implementations, one shape; adding a fourth cloud
means adding a mode, not a code path.

The alternative is what the predecessor series did: three bespoke auth paths
retrofitted after the fact, one per repo, which is why its findings ended up
scattered across six of them.
"""

from dataclasses import dataclass
from typing import Protocol

from coordinator.auth import auth_mode, credentials_for
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
    #: How this leg authenticates: one of ``coordinator.auth.AUTH_MODES``.
    #: Reported rather than inferred, so a leg that silently fell back to an
    #: unauthenticated call cannot be mistaken for a federated one.
    auth: str = "none"

    def __str__(self) -> str:
        return self.name


__all__ = ["Participant", "QuoteSource", "auth_mode", "credentials_for"]
