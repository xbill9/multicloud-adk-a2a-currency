from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol


class RateProvider(Protocol):
    async def get_rate(self, source_currency: str, target_currency: str) -> tuple[Decimal, datetime]:
        """Return target units per source unit and its observation time."""


class StaticRateProvider:
    """Deterministic cross-rate provider for local development and tests.

    Values are currency units per USD. They are fixtures, not live financial data.
    """

    name = "deterministic-fixture"

    DEFAULT_UNITS_PER_USD: Mapping[str, Decimal] = {
        "USD": Decimal(1),
        "CAD": Decimal("1.35"),
        "EUR": Decimal("0.92"),
        "GBP": Decimal("0.79"),
        "JPY": Decimal(150),
        "AUD": Decimal("1.52"),
        "CHF": Decimal("0.88"),
        "NZD": Decimal("1.64"),
    }

    def __init__(
        self,
        units_per_usd: Mapping[str, Decimal] | None = None,
        *,
        observed_at: datetime | None = None,
    ) -> None:
        self._rates = dict(units_per_usd or self.DEFAULT_UNITS_PER_USD)
        self._observed_at = observed_at or datetime.now(UTC)

    async def get_rate(self, source_currency: str, target_currency: str) -> tuple[Decimal, datetime]:
        try:
            source = self._rates[source_currency]
            target = self._rates[target_currency]
        except KeyError as exc:
            raise ValueError(f"unsupported currency: {exc.args[0]}") from exc
        return target / source, self._observed_at


class ScaledRateProvider:
    """Wraps a provider and multiplies its rate. Fault injection, nothing else.

    Exists so a *running agent* can be made to disagree, which the mesh-level
    fault-injection tests cannot do -- they perturb a quote after the fact.
    Demonstrating that the median holds requires a participant that actually
    returns a divergent number over the wire.

    The scale is applied to the computed rate rather than to the underlying
    table, because these rates are cross-rates: scaling every entry by k gives
    (k*target)/(k*source), which is the rate you started with.
    """

    def __init__(self, inner: RateProvider, scale: Decimal) -> None:
        self._inner = inner
        self._scale = scale

    @property
    def name(self) -> str:
        return f"{getattr(self._inner, 'name', 'unknown')}-scaled-{self._scale}"

    async def get_rate(self, source_currency: str, target_currency: str) -> tuple[Decimal, datetime]:
        rate, observed_at = await self._inner.get_rate(source_currency, target_currency)
        return rate * self._scale, observed_at


class FrankfurterRateProvider:
    """Live daily reference rates from the Frankfurter API.

    The same upstream source the benchmark ADK agent uses, so verified-mode
    comparisons against it measure protocol overhead rather than data skew.
    """

    name = "frankfurter-live"

    def __init__(self, base_url: str = "https://api.frankfurter.dev/v1") -> None:
        self._base_url = base_url

    async def get_rate(self, source_currency: str, target_currency: str) -> tuple[Decimal, datetime]:
        import httpx

        if source_currency == target_currency:
            return Decimal(1), datetime.now(UTC)
        try:
            # local_address pins the socket to IPv4; IPv6 to this host hangs in
            # some sandboxes (same workaround as the upstream currency-agent).
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
            async with httpx.AsyncClient(timeout=15, transport=transport) as client:
                response = await client.get(
                    f"{self._base_url}/latest",
                    params={"base": source_currency, "symbols": target_currency},
                )
                response.raise_for_status()
                payload = response.json()
            rate = Decimal(str(payload["rates"][target_currency]))
            observed_at = datetime.fromisoformat(payload["date"]).replace(tzinfo=UTC)
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"unsupported currency pair {source_currency}/{target_currency}: "
                f"HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, KeyError) as exc:
            raise ValueError(f"frankfurter request failed: {exc}") from exc
        return rate, observed_at
