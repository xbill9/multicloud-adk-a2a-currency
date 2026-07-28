"""MCP stdio client adapter fulfilling ``ExchangeRateTool`` over JSON-RPC.

This exercises the full MCP loop locally: spawn the stdio server, initialize,
and call ``convert_currency`` once per target. Protocol SDK clients can replace
this transport without changing the coordinator.
"""

import asyncio
import json
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from time import perf_counter

from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionQuote, ConversionRequest

DEFAULT_COMMAND: Sequence[str] = (sys.executable, "-m", "mcp_server.server")


class McpStdioExchangeRateTool:
    def __init__(
        self,
        command: Sequence[str] = DEFAULT_COMMAND,
        *,
        source: str = "mcp-stdio",
    ) -> None:
        self._command = tuple(command)
        self._source = source

    async def convert(self, request: ConversionRequest) -> list[ConversionQuote]:
        started = perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AdapterError(FailureKind.TRANSPORT, f"cannot start MCP server: {exc}") from exc

        try:
            await self._rpc(
                process,
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "currency-coordinator", "version": "0.1.0"},
                },
            )
            await self._notify(process, "notifications/initialized")
            quotes: list[ConversionQuote] = []
            for call_id, target in enumerate(request.target_currencies, start=2):
                result = await self._rpc(
                    process,
                    call_id,
                    "tools/call",
                    {
                        "name": "convert_currency",
                        "arguments": {
                            "amount": str(request.amount),
                            "source_currency": request.source_currency,
                            "target_currency": target,
                        },
                    },
                )
                quotes.append(self._quote(result, started))
            return quotes
        finally:
            if process.stdin:
                process.stdin.close()
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def _rpc(
        self, process: asyncio.subprocess.Process, request_id: int, method: str, params: dict
    ) -> dict:
        assert process.stdin and process.stdout
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line:
            raise AdapterError(FailureKind.TRANSPORT, "MCP server closed the stream")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdapterError(
                FailureKind.PROTOCOL, f"invalid JSON-RPC response: {exc.msg}"
            ) from exc
        if "error" in response:
            error = response["error"]
            raise AdapterError(
                FailureKind.PROTOCOL, f"MCP error {error.get('code')}: {error.get('message')}"
            )
        result = response.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            text = content[0].get("text", "tool failed") if content else "tool failed"
            raise AdapterError(FailureKind.PROVIDER, text)
        return result

    async def _notify(self, process: asyncio.subprocess.Process, method: str) -> None:
        assert process.stdin
        message = {"jsonrpc": "2.0", "method": method}
        process.stdin.write((json.dumps(message) + "\n").encode())
        await process.stdin.drain()

    def _quote(self, result: dict, started: float) -> ConversionQuote:
        payload = result.get("structuredContent")
        try:
            return ConversionQuote(
                source=f"{self._source}:{payload['provider']}",
                source_currency=payload["source_currency"],
                target_currency=payload["target_currency"],
                amount=Decimal(payload["amount"]),
                rate=Decimal(payload["rate"]),
                converted_amount=Decimal(payload["converted_amount"]),
                observed_at=payload["observed_at"],
                latency_ms=(perf_counter() - started) * 1000,
            )
        except (TypeError, KeyError, InvalidOperation, ValueError) as exc:
            raise AdapterError(FailureKind.PROTOCOL, f"malformed tool result: {exc!r}") from exc
