"""Transport-free tests of the shared client behaviour.

Every stack inherits timing, error mapping, and parsing from A2AQuoteClient,
so exercising the base against a stubbed ``_send`` covers all three without a
network, credentials, or vendor SDKs.
"""

from decimal import Decimal

import httpx
import pytest

from clients import CLIENT_STACKS, load_client
from clients.base import A2AQuoteClient
from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionRequest

REPLY = (
    '{"source_currency": "USD", "target_currency": "EUR", '
    '"rate": 0.92, "converted_amount": 92.0}\n'
    '{"source_currency": "USD", "target_currency": "GBP", '
    '"rate": 0.79, "converted_amount": 79.0}'
)


class StubClient(A2AQuoteClient):
    stack = "stub"

    def __init__(self, reply=REPLY, *, raises=None, **kwargs):
        super().__init__("http://stub.invalid", **kwargs)
        self._reply = reply
        self._raises = raises

    async def _send(self, prompt: str) -> str:
        self.last_prompt = prompt
        if self._raises:
            raise self._raises
        return self._reply


def request(*targets: str) -> ConversionRequest:
    return ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=list(targets)
    )


async def test_parses_one_quote_per_target():
    quotes = await StubClient().convert(request("EUR", "GBP"))

    assert [quote.target_currency for quote in quotes] == ["EUR", "GBP"]
    assert quotes[0].converted_amount == Decimal("92.0")
    assert quotes[0].latency_ms >= 0


async def test_prompt_names_every_target():
    client = StubClient()
    await client.convert(request("EUR", "GBP"))

    assert "EUR, GBP" in client.last_prompt
    assert "100" in client.last_prompt


async def test_prose_around_the_json_is_tolerated():
    reply = f"Sure! Here are the rates:\n{REPLY}\nLet me know if you need more."
    quotes = await StubClient(reply).convert(request("EUR", "GBP"))

    assert len(quotes) == 2


async def test_missing_target_is_a_protocol_failure():
    with pytest.raises(AdapterError) as exc:
        await StubClient().convert(request("EUR", "JPY"))

    assert exc.value.kind is FailureKind.PROTOCOL
    assert "JPY" in str(exc.value)


async def test_http_401_maps_to_authentication():
    failure = httpx.HTTPStatusError(
        "denied",
        request=httpx.Request("POST", "http://stub.invalid"),
        response=httpx.Response(401),
    )
    with pytest.raises(AdapterError) as exc:
        await StubClient(raises=failure).convert(request("EUR"))

    assert exc.value.kind is FailureKind.AUTHENTICATION


async def test_http_503_maps_to_transport():
    failure = httpx.HTTPStatusError(
        "unavailable",
        request=httpx.Request("POST", "http://stub.invalid"),
        response=httpx.Response(503),
    )
    with pytest.raises(AdapterError) as exc:
        await StubClient(raises=failure).convert(request("EUR"))

    assert exc.value.kind is FailureKind.TRANSPORT


async def test_connection_refused_maps_to_transport():
    with pytest.raises(AdapterError) as exc:
        await StubClient(raises=httpx.ConnectError("refused")).convert(request("EUR"))

    assert exc.value.kind is FailureKind.TRANSPORT


async def test_unknown_sdk_exception_maps_to_protocol_with_its_type():
    with pytest.raises(AdapterError) as exc:
        await StubClient(raises=RuntimeError("card is malformed")).convert(request("EUR"))

    assert exc.value.kind is FailureKind.PROTOCOL
    assert "RuntimeError" in str(exc.value)


def test_registry_rejects_unknown_stack():
    with pytest.raises(ValueError, match="unknown client stack"):
        load_client("langchain", "http://stub.invalid")


def test_every_declared_stack_is_constructible_or_reports_a_missing_sdk():
    """A stack whose SDK is absent must raise ImportError, not ValueError."""
    for stack in CLIENT_STACKS:
        try:
            client = load_client(stack, "http://stub.invalid")
        except ImportError:
            continue
        assert client.stack == stack
        assert client.endpoint == "http://stub.invalid"
