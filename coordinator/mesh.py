"""Fan out one conversion request across every participating cloud.

All participants are called concurrently and independently: one cloud timing
out or returning malformed JSON degrades the consensus to the remaining
clouds rather than failing the run. That is the behaviour the mesh exists to
demonstrate, so failures are recorded per participant rather than raised.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter

from coordinator.consensus import DEFAULT_TOLERANCE, reach_consensus
from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionQuote, ConversionRequest, MeshRun
from coordinator.participants import Participant


class CurrencyMesh:
    def __init__(
        self,
        participants: list[Participant],
        *,
        tolerance: Decimal = DEFAULT_TOLERANCE,
        stale_after_seconds: int = 86_400,
        timeout_seconds: float = 60,
    ) -> None:
        if not participants:
            raise ValueError("a mesh needs at least one participant")
        self._participants = participants
        self._tolerance = tolerance
        self._stale_after_seconds = stale_after_seconds
        self._timeout_seconds = timeout_seconds

    async def run(self, request: ConversionRequest) -> MeshRun:
        started = perf_counter()
        failures: dict[str, str] = {}

        gathered = await asyncio.gather(
            *(self._call(participant, request, failures) for participant in self._participants)
        )

        by_target: dict[str, list[ConversionQuote]] = {
            target: [] for target in request.target_currencies
        }
        for quotes in gathered:
            for quote in quotes:
                if quote.target_currency in by_target:
                    by_target[quote.target_currency].append(quote)

        results = [
            reach_consensus(target, by_target[target], tolerance=self._tolerance)
            for target in request.target_currencies
        ]
        for result in results:
            self._append_staleness_warning(result)

        return MeshRun(
            request=request,
            participants=[participant.name for participant in self._participants],
            results=results,
            failures=failures,
            elapsed_ms=(perf_counter() - started) * 1000,
        )

    async def _call(
        self,
        participant: Participant,
        request: ConversionRequest,
        failures: dict[str, str],
    ) -> list[ConversionQuote]:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await participant.source.convert(request)
        except TimeoutError:
            failures[participant.name] = AdapterError(
                FailureKind.TIMEOUT, f"exceeded {self._timeout_seconds}s"
            ).safe_message()
        except AdapterError as exc:
            failures[participant.name] = exc.safe_message()
        except Exception as exc:  # noqa: BLE001 - adapter boundary converts SDK failures
            failures[participant.name] = AdapterError(FailureKind.PROTOCOL, str(exc)).safe_message()
        return []

    def _append_staleness_warning(self, result) -> None:
        now = datetime.now(UTC)
        stale = [
            quote.source
            for quote in result.quotes
            if (now - quote.observed_at).total_seconds() > self._stale_after_seconds
        ]
        if stale:
            result.warnings.append(
                f"stale rates from {', '.join(stale)}; check the observation timestamps"
            )
