"""Small dependency-free MCP stdio server for the deterministic rate fixture.

The server implements the JSON-RPC methods needed by an MCP client to discover
and call ``convert_currency``. Protocol SDK adapters can replace this transport
without changing the domain provider.
"""

import asyncio
import json
import os
import sys
from decimal import Decimal, InvalidOperation

from coordinator.models import ConversionRequest
from coordinator.providers import FrankfurterRateProvider, RateProvider, StaticRateProvider

PROTOCOL_VERSION = "2024-11-05"

TOOL = {
    "name": "convert_currency",
    "description": "Convert money using deterministic local fixture rates (not live rates).",
    "inputSchema": {
        "type": "object",
        "properties": {
            "amount": {"type": "string"},
            "source_currency": {"type": "string"},
            "target_currency": {"type": "string"},
        },
        "required": ["amount", "source_currency", "target_currency"],
        "additionalProperties": False,
    },
}

#: The same rate lookup under the name and signature the shared INSTRUCTION
#: asks for, which is also what the AWS and Azure agents register as a native
#: tool. Without this the GCP leg was the only cloud whose tool contract did
#: not match the prompt driving it: Gemini duly emitted
#: `get_exchange_rate(source_currency=..., target_currency=...)` and ADK
#: rejected it as UNEXPECTED_TOOL_CALL. `convert_currency` stays because
#: `coordinator/mcp_stdio.py` calls it by name.
RATE_TOOL = {
    "name": "get_exchange_rate",
    "description": (
        "Look up the exchange rate between two currencies using deterministic "
        "local fixture rates (not live rates)."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "currency_from": {"type": "string"},
            "currency_to": {"type": "string"},
        },
        "required": ["currency_from", "currency_to"],
        "additionalProperties": False,
    },
}


async def dispatch(message: dict, provider: RateProvider) -> dict | None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "currency-rate-fixture", "version": "0.1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [TOOL, RATE_TOOL]}
        elif method == "tools/call":
            params = message.get("params", {})
            name = params.get("name")
            if name not in (TOOL["name"], RATE_TOOL["name"]):
                raise ValueError(f"unknown tool: {name}")
            arguments = params.get("arguments", {})
            if name == RATE_TOOL["name"]:
                # A rate lookup, so the amount is irrelevant; 1 makes
                # converted_amount equal the rate, which is what the caller
                # asked for and costs no separate code path.
                request = ConversionRequest(
                    amount=Decimal(1),
                    source_currency=arguments["currency_from"],
                    target_currencies=[arguments["currency_to"]],
                )
            else:
                request = ConversionRequest(
                    amount=Decimal(arguments["amount"]),
                    source_currency=arguments["source_currency"],
                    target_currencies=[arguments["target_currency"]],
                )
            try:
                rate, observed_at = await provider.get_rate(
                    request.source_currency, request.target_currencies[0]
                )
            except ValueError as exc:
                # Tool-execution failure: reported in-band per the MCP spec, not
                # as a JSON-RPC error, so clients can surface it to the model.
                result = {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                }
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            payload = {
                "amount": str(request.amount),
                "converted_amount": str(request.amount * rate),
                "rate": str(rate),
                "source_currency": request.source_currency,
                "target_currency": request.target_currencies[0],
                "observed_at": observed_at.isoformat(),
                "provider": getattr(provider, "name", "deterministic-fixture"),
            }
            result = {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
                "isError": False,
            }
        else:
            return _error(request_id, -32601, f"method not found: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except (KeyError, ValueError, InvalidOperation) as exc:
        return _error(request_id, -32602, f"invalid parameters: {exc}")


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def select_provider() -> RateProvider:
    if os.getenv("CURRENCY_RATE_PROVIDER", "fixture").lower() == "frankfurter":
        return FrankfurterRateProvider()
    return StaticRateProvider()


def serve() -> None:
    provider = select_provider()
    while line := sys.stdin.readline():
        try:
            message = json.loads(line)
            response = asyncio.run(dispatch(message, provider))
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, f"parse error: {exc.msg}")
        if response is not None:
            print(json.dumps(response), flush=True)


def main() -> None:
    serve()


if __name__ == "__main__":
    main()
