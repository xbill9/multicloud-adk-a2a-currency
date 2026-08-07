"""Shared timing, error mapping, and parsing for every A2A client stack.

Subclasses implement exactly one method -- ``_send``, prompt in and reply text
out -- so the only difference between matrix rows is the vendor SDK's wire
handling. Everything downstream of the reply text is identical by
construction.
"""

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

import httpx

from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionQuote, ConversionRequest
from protocol.quotes import build_prompt, parse_quotes

log = logging.getLogger("clients")


def _classify(exc: BaseException, endpoint: str) -> AdapterError | None:
    """Map one exception to a typed failure, or ``None`` if unrecognisable.

    Only failures this can name are anything other than ``protocol``. Every
    entry below exists because a real cause was once misfiled:

    - a 401 on the card fetch, reported as ``protocol`` (found by deploying)
    - a missing vendor SDK, reported as ``protocol`` (found by building)
    - a refused connection, reported as ``protocol`` (found by running)

    The URL on a status failure is the part worth keeping: a 401 on
    ``/.well-known/agent-card.json`` means discovery is privileged and the
    credential never reached it, which is a different fix from a 401 on the
    message endpoint.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        kind = FailureKind.AUTHENTICATION if status in (401, 403) else FailureKind.TRANSPORT
        detail = f"A2A endpoint returned {status} for {exc.request.url}"
        # The body is the discriminator and it is free. AWS says "signature we
        # calculated does not match" for a signing bug and "not authorized to
        # perform" for a policy bug -- the same 403, opposite afternoons. The
        # rule this project keeps relearning is that the raised message is not
        # an observable, so carry the provider's own words back with it.
        if status in (401, 403):
            try:
                body = exc.response.text.strip()
            except Exception:  # noqa: BLE001 - a streamed body may be gone
                body = ""
            if body:
                log.error("%s\nbody: %s", detail, body)
                detail = f"{detail}: {body[:400]}"
        return AdapterError(kind, detail)

    # Checked before TransportError, of which it is a subclass.
    if isinstance(exc, httpx.TimeoutException):
        return AdapterError(FailureKind.TIMEOUT, f"A2A endpoint {endpoint} timed out: {exc}")

    if isinstance(exc, httpx.TransportError | ConnectionError | OSError):
        return AdapterError(
            FailureKind.TRANSPORT, f"cannot reach A2A endpoint {endpoint}: {exc}"
        )

    # A missing dependency is this process's problem, not the remote's. Filing
    # it as `protocol` makes an environment fault indistinguishable from an
    # interop finding, which is the one thing this instrument must never do.
    if isinstance(exc, ImportError):
        return AdapterError(
            FailureKind.PROVIDER,
            f"client stack is not installed correctly: {type(exc).__name__}: {exc}",
        )

    return None


def _classify_chain(exc: BaseException, endpoint: str) -> AdapterError | None:
    """Walk ``__cause__``/``__context__`` for the first cause we can name.

    Vendor SDKs wrap transport and HTTP failures in their own types --
    ``a2a-sdk`` funnels every card-fetch failure through
    ``AgentCardResolutionError`` regardless of cause -- so the real kind is
    only ever visible further down the chain.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        classified = _classify(current, endpoint)
        if classified is not None:
            return classified
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


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
        except AdapterError:
            raise
        except Exception as exc:
            # One classifier for both the direct and the wrapped case. Every
            # vendor SDK here wraps failures in its own type, so classifying
            # only what we catch directly means classifying almost nothing:
            # `protocol` becomes the default rather than a diagnosis, and a red
            # cell stops naming its layer -- the single most expensive kind of
            # wrong answer this instrument can give.
            classified = _classify_chain(exc, self._endpoint)
            if classified is not None:
                raise classified from exc
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
