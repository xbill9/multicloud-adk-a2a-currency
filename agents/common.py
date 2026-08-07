"""Shared pieces of every cloud's native currency agent.

Each cloud contributes its own *serving* stack -- that is what the interop
matrix measures -- but they all answer the same question in the same format,
and they all support two brains:

``CURRENCY_MODEL_MODE=direct``
    Answer deterministically from a RateProvider, no model and no credentials.
    Lets the matrix isolate protocol behaviour: a failed cell is unambiguously
    the wire, never a model that wandered off-format or a missing API key.

``CURRENCY_MODEL_MODE=llm``
    Answer with the cloud's native model through its native agent framework.
    The real configuration; also the one that can fail on formatting.
"""

import logging
import os
import re
from decimal import Decimal, InvalidOperation

from coordinator.models import ConversionRequest
from coordinator.providers import (
    FrankfurterRateProvider,
    RateProvider,
    ScaledRateProvider,
    StaticRateProvider,
)

INSTRUCTION = (
    "You are a specialized assistant for currency conversions. "
    "Your sole purpose is to use the 'get_exchange_rate' tool to answer questions about "
    "currency exchange rates. "
    "When asked to convert an amount, call the tool for each requested target currency, then "
    "reply with exactly one JSON object per line of the form "
    '{"source_currency": "<ISO code>", "target_currency": "<ISO code>", '
    '"rate": <decimal>, "converted_amount": <decimal>} '
    "and no other text. "
    "If the user asks about anything other than currency conversion or exchange rates, "
    "politely state that you cannot help with that topic."
)

DESCRIPTION = "An agent that answers currency conversion questions with structured quotes"

_PROMPT_RE = re.compile(
    r"convert\s+([0-9]+(?:\.[0-9]+)?)\s+([A-Za-z]{3})\s+"
    r"to the following currencies:\s*([A-Za-z,\s]+?)\s*\.",
    re.IGNORECASE,
)


def model_mode() -> str:
    """``direct`` for the credential-free protocol probe, ``llm`` for the real thing."""
    return os.getenv("CURRENCY_MODEL_MODE", "direct").strip().lower()


def rate_provider() -> RateProvider:
    """Fixture rates by default; Frankfurter only when explicitly asked for.

    The mesh deliberately does not default to a live upstream. When every
    cloud reads the same exchange-rate API they agree by construction, which
    makes consensus vacuous and folds upstream latency and rate limits into
    numbers that are supposed to measure A2A.
    """
    if os.getenv("CURRENCY_RATE_PROVIDER", "fixture").strip().lower() == "frankfurter":
        provider = FrankfurterRateProvider()
    else:
        provider = StaticRateProvider()

    # Fault injection, opt-in and loud: makes this agent return a divergent
    # rate so the median can be shown holding rather than asserted. Never set
    # in a measurement run -- ./infra/demo.sh act 4 is what it is for.
    scale = os.getenv("CURRENCY_RATE_SCALE", "").strip()
    if scale:
        logging.getLogger("agents").warning(
            "CURRENCY_RATE_SCALE=%s -- this agent is deliberately returning "
            "divergent rates. Not a valid measurement configuration.",
            scale,
        )
        provider = ScaledRateProvider(provider, Decimal(scale))
    return provider


def parse_conversion_prompt(text: str) -> ConversionRequest | None:
    """Recover the structured request from the benchmark's prompt template.

    Only used by ``direct`` mode, where there is no model to do the reading.
    Both sides of this prompt live in this repo, so a strict template match is
    appropriate; anything else returns None and the agent declines.
    """
    match = _PROMPT_RE.search(text)
    if not match:
        return None
    amount, source, targets = match.groups()
    try:
        return ConversionRequest(
            amount=Decimal(amount),
            source_currency=source,
            target_currencies=[part.strip() for part in targets.split(",") if part.strip()],
        )
    except (InvalidOperation, ValueError):
        return None


async def deterministic_reply(text: str, provider: RateProvider | None = None) -> str:
    """Answer a conversion prompt from the rate provider, in the wire format."""
    request = parse_conversion_prompt(text)
    if request is None:
        return "I can only help with currency conversion and exchange rates."

    provider = provider or rate_provider()
    lines: list[str] = []
    for target in request.target_currencies:
        try:
            rate, _ = await provider.get_rate(request.source_currency, target)
        except ValueError as exc:
            return f"I cannot convert to {target}: {exc}"
        lines.append(
            f'{{"source_currency": "{request.source_currency}", '
            f'"target_currency": "{target}", "rate": {rate}, '
            f'"converted_amount": {request.amount * rate}}}'
        )
    return "\n".join(lines)


def public_url(default_port: int) -> str:
    """The URL this agent advertises on its card.

    Deliberately explicit. An agent behind Cloud Run, AgentCore, or Foundry is
    reached at a hostname it cannot infer from its own socket, and a card that
    advertises the bind address is unreachable to every remote client -- the
    first interop bug this project hit.
    """
    if url := os.getenv("PUBLIC_URL"):
        return url.rstrip("/")
    host = os.getenv("HOST", "127.0.0.1")
    port = os.getenv("PORT", str(default_port))
    return f"http://{host}:{port}"
