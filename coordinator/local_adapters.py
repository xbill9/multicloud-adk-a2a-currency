import asyncio
from decimal import Decimal
from time import perf_counter

from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionQuote, ConversionRequest
from coordinator.providers import RateProvider


class DeterministicCurrencyAdapter:
    """SDK-independent adapter used to exercise MCP/A2A orchestration locally."""

    def __init__(
        self,
        provider: RateProvider,
        *,
        source: str,
        rate_multiplier: Decimal = Decimal(1),
        delay_ms: float = 0,
        failure: AdapterError | None = None,
    ) -> None:
        self._provider = provider
        self._source = source
        self._rate_multiplier = rate_multiplier
        self._delay_ms = delay_ms
        self._failure = failure

    async def convert(self, request: ConversionRequest) -> list[ConversionQuote]:
        started = perf_counter()
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000)
        if self._failure:
            raise self._failure

        quotes: list[ConversionQuote] = []
        for target in request.target_currencies:
            try:
                rate, observed_at = await self._provider.get_rate(request.source_currency, target)
            except ValueError as exc:
                raise AdapterError(FailureKind.PROVIDER, str(exc)) from exc
            rate *= self._rate_multiplier
            quotes.append(
                ConversionQuote(
                    source=self._source,
                    source_currency=request.source_currency,
                    target_currency=target,
                    amount=request.amount,
                    rate=rate,
                    converted_amount=request.amount * rate,
                    observed_at=observed_at,
                    latency_ms=(perf_counter() - started) * 1000,
                )
            )
        return quotes
