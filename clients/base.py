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


def _wrapped_status_error(exc: BaseException) -> httpx.HTTPStatusError | None:
    """Find an HTTP status failure anywhere in the exception chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, httpx.HTTPStatusError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _status_error(exc: httpx.HTTPStatusError) -> AdapterError:
    """Classify an HTTP status, naming the URL that produced it.

    The URL is the part worth keeping: a 401 on ``/.well-known/agent-card.json``
    means discovery is privileged and the credential never reached it, which is
    a different fix from a 401 on the message endpoint.
    """
    status = exc.response.status_code
    kind = FailureKind.AUTHENTICATION if status in (401, 403) else FailureKind.TRANSPORT
    return AdapterError(kind, f"A2A endpoint returned {status} for {exc.request.url}")


class A2AQuoteClient:
    #: Identifier for this SDK in the interop matrix (one row per stack).
    stack = "unknown"

    def __init__(
        self,
        endpoint: str,
        *,
        source: str | None = None,
        timeout_s: float = 120.0,
        auth: httpx.Auth | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._source = source or self.stack
        self._timeout_s = timeout_s
        self._auth = auth

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def auth(self) -> httpx.Auth | None:
        return self._auth

    def _http_client(self, **kwargs) -> httpx.AsyncClient:
        """Build the transport every stack talks through.

        The credential is attached to the *client*, not to a single request,
        so the agent-card fetch carries it too. Discovery is privileged on all
        three clouds, and a card fetch that 403s while the call itself would
        have succeeded is the most misleading failure in this space -- it
        surfaces as a protocol or transport error, nowhere near auth.
        """
        return httpx.AsyncClient(timeout=self._timeout_s, auth=self._auth, **kwargs)

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
            raise _status_error(exc) from exc
        except (httpx.TransportError, ConnectionError, OSError) as exc:
            raise AdapterError(
                FailureKind.TRANSPORT,
                f"cannot reach A2A endpoint {self._endpoint}: {exc}",
            ) from exc
        except AdapterError:
            raise
        except Exception as exc:  # vendor SDKs raise protocol errors of varied types
            # A vendor SDK that wraps the HTTP failure in its own type would
            # otherwise land here and be filed as "protocol". Observed against
            # a 401 on the card fetch: a2a-sdk raises AgentCardResolutionError
            # with the httpx.HTTPStatusError only as __cause__, so an auth
            # failure classifies as a protocol failure and the matrix reports
            # the wrong layer -- which is the single most expensive kind of
            # wrong answer this instrument can give.
            wrapped = _wrapped_status_error(exc)
            if wrapped is not None:
                raise _status_error(wrapped) from exc
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
