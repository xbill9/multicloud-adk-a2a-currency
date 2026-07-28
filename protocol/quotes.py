"""Prompt construction and reply parsing, shared by all three A2A client stacks.

Keeping these pure and transport-free is what makes the interop matrix
meaningful: when a 3x3 cell fails, the failure is in that SDK's wire handling,
not in three subtly different JSON parsers. It also keeps parsing testable
without a network or credentials.
"""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionQuote, ConversionRequest

_PROMPT_TEMPLATE = (
    "Convert {amount} {source} to the following currencies: {targets}. "
    "Use your exchange-rate tool for each target. Reply with exactly one JSON "
    'object per target of the form {{"source_currency": "<ISO code>", '
    '"target_currency": "<ISO code>", "rate": <decimal>, '
    '"converted_amount": <decimal>}} and no other text.'
)


def build_prompt(request: ConversionRequest) -> str:
    return _PROMPT_TEMPLATE.format(
        amount=request.amount,
        source=request.source_currency,
        targets=", ".join(request.target_currencies),
    )


def extract_json_objects(text: str) -> list[dict]:
    """Pull every top-level JSON object out of free-form model text."""
    decoder = json.JSONDecoder(parse_float=Decimal, parse_int=Decimal)
    objects: list[dict] = []
    index = 0
    while (start := text.find("{", index)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        index = start + max(end - start, 1)
    return objects


def parse_quotes(
    text: str,
    request: ConversionRequest,
    *,
    source: str,
    latency_ms: float,
    observed_at: datetime,
) -> list[ConversionQuote]:
    """Map a remote agent's JSON reply onto one quote per requested target."""
    candidates: dict[str, dict] = {}
    for obj in extract_json_objects(text):
        target = str(obj.get("target_currency", "")).strip().upper()
        if target:
            candidates[target] = obj

    quotes: list[ConversionQuote] = []
    for target in request.target_currencies:
        obj = candidates.get(target)
        if obj is None:
            raise AdapterError(
                FailureKind.PROTOCOL,
                f"remote agent reply is missing a quote for {target}: {text[:200]!r}",
            )
        try:
            rate = Decimal(str(obj["rate"]))
            converted = Decimal(str(obj.get("converted_amount", request.amount * rate)))
            quotes.append(
                ConversionQuote(
                    source=source,
                    source_currency=request.source_currency,
                    target_currency=target,
                    amount=request.amount,
                    rate=rate,
                    converted_amount=converted,
                    observed_at=observed_at,
                    latency_ms=latency_ms,
                )
            )
        except (KeyError, InvalidOperation, ValidationError) as exc:
            raise AdapterError(
                FailureKind.PROTOCOL,
                f"remote agent quote for {target} is malformed: {exc}",
            ) from exc
    return quotes
