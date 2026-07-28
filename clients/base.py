"""Shared timing, error mapping, and parsing for every A2A client stack.

Subclasses implement exactly one method -- ``_send``, prompt in and reply text
out -- so the only difference between matrix rows is the vendor SDK's wire
handling. Everything downstream of the reply text is identical by
construction.
"""

import asyncio
from datetime import UTC, datetime
from time import perf_counter

import httpx

from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionQuote, ConversionRequest
from protocol.quotes import build_prompt, parse_quotes


class A2AQuoteClient:
    #: Identifier for this SDK in the interop matrix (one row per stack).
    stack = "unknown"

    def __init__(
        self,
        endpoint: str,
        *,
        source: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._source = source or self.stack
        self._timeout_s = timeout_s

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def _send(self, prompt: str) -> str:
        """Send one text message over A2A and return the concatenated reply."""
        raise NotImplementedError

    async def convert(self, request: ConversionRequest) -> list[ConversionQuote]:
        started = perf_counter()
        try:
            response_text = await asyncio.wait_for(
                self._send(build_prompt(request)), timeout=self._timeout_s
            )
        except TimeoutError as exc:
            raise AdapterError(
                FailureKind.TIMEOUT,
                f"A2A agent at {self._endpoint} exceeded {self._timeout_s}s",
            ) from exc
        except httpx.HTTPStatusError as exc:
            kind = (
                FailureKind.AUTHENTICATION
                if exc.response.status_code in (401, 403)
                else FailureKind.TRANSPORT
            )
            raise AdapterError(kind, f"A2A endpoint returned {exc.response.status_code}") from exc
        except (httpx.TransportError, ConnectionError, OSError) as exc:
            raise AdapterError(
                FailureKind.TRANSPORT,
                f"cannot reach A2A endpoint {self._endpoint}: {exc}",
            ) from exc
        except AdapterError:
            raise
        except Exception as exc:  # vendor SDKs raise protocol errors of varied types
            raise AdapterError(
                FailureKind.PROTOCOL,
                f"A2A protocol failure from {self._endpoint}: {type(exc).__name__}: {exc}",
            ) from exc

        latency_ms = (perf_counter() - started) * 1000
        return parse_quotes(
            response_text,
            request,
            source=self._source,
            latency_ms=latency_ms,
            observed_at=datetime.now(UTC),
        )
