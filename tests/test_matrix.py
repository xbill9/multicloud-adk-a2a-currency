from decimal import Decimal

import pytest

from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionRequest
from matrix.model import Cell, MatrixReport
from matrix.runner import (
    COORDINATOR_CLOUD_ENV,
    Server,
    coordinator_cloud,
    hop_kind,
    probe,
    render_table,
)

SERVER = Server("azure", "Azure", "agent-framework A2AExecutor", "http://127.0.0.1:10003")
GCP_SERVER = Server("gcp", "Google Cloud", "adk to_a2a", "http://127.0.0.1:10001")


def cell(client_stack: str, server: str, ok: bool, **kwargs) -> Cell:
    return Cell(
        client_stack=client_stack,
        server=server,
        server_cloud=server.upper(),
        server_stack="stack",
        ok=ok,
        **kwargs,
    )


def request() -> ConversionRequest:
    return ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=["EUR"]
    )


def test_report_preserves_declaration_order():
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[
            cell("a2a-sdk", "gcp", True),
            cell("a2a-sdk", "aws", True),
            cell("agent-framework", "gcp", True),
        ],
    )
    assert report.client_stacks == ["a2a-sdk", "agent-framework"]
    assert report.servers == ["gcp", "aws"]


def test_missing_sdk_is_excluded_from_the_success_rate():
    """An uninstalled client SDK is not a protocol failure and must not read as one."""
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[
            cell("a2a-sdk", "gcp", True),
            cell("google-adk", "gcp", False, failure_kind="sdk-missing"),
        ],
    )
    assert len(report.attempted) == 1
    table = render_table(report)
    assert "1/1 attempted cells succeeded" in table
    assert "skipped (SDK not installed): google-adk" in table


def test_table_reports_failures_with_detail():
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[cell("a2a-sdk", "azure", False, failure_kind="protocol", detail="empty reply")],
    )
    table = render_table(report)
    assert "0/1 attempted cells succeeded" in table
    assert "a2a-sdk -> azure: empty reply" in table


def test_lookup_of_an_absent_cell_is_none():
    report = MatrixReport(request_summary="x", model_mode="direct", cells=[])
    assert report.cell("a2a-sdk", "gcp") is None


async def test_probe_records_adapter_failure_kind(monkeypatch):
    class Failing:
        async def convert(self, request):
            raise AdapterError(FailureKind.TRANSPORT, "connection refused")

    monkeypatch.setattr("matrix.runner.load_client", lambda *a, **k: Failing())
    result = await probe("a2a-sdk", SERVER, request(), timeout_s=1)

    assert result.ok is False
    assert result.failure_kind == "transport"
    assert "refused" in result.detail


async def test_probe_records_uninstalled_sdk_without_raising(monkeypatch):
    def missing(*args, **kwargs):
        raise ImportError("No module named 'strands'")

    monkeypatch.setattr("matrix.runner.load_client", missing)
    result = await probe("a2a-sdk", SERVER, request(), timeout_s=1)

    assert result.failure_kind == "sdk-missing"


async def test_probe_catches_exceptions_a_client_failed_to_map(monkeypatch):
    """A vendor SDK can raise outside our error mapping; the matrix must survive it."""

    class Exploding:
        async def convert(self, request):
            raise KeyError("supported_interfaces")

    monkeypatch.setattr("matrix.runner.load_client", lambda *a, **k: Exploding())
    result = await probe("a2a-sdk", SERVER, request(), timeout_s=1)

    assert result.failure_kind == "unmapped"
    assert "KeyError" in result.detail


@pytest.mark.parametrize("bad", ["", "not-a-stack"])
def test_render_handles_empty_report(bad):
    report = MatrixReport(request_summary=bad, model_mode="direct", cells=[])
    assert "0/0 attempted cells succeeded" in render_table(report)


def test_local_mesh_classifies_every_leg_as_local(monkeypatch):
    """Unset means loopback: nothing is claimed about crossing a boundary."""
    monkeypatch.delenv(COORDINATOR_CLOUD_ENV, raising=False)
    assert coordinator_cloud() is None
    assert hop_kind(GCP_SERVER, None) == "local"
    assert hop_kind(SERVER, None) == "local"


@pytest.mark.parametrize("value", ["gcp", "GCP", "  gcp  "])
def test_coordinator_cloud_is_normalised(monkeypatch, value):
    monkeypatch.setenv(COORDINATOR_CLOUD_ENV, value)
    assert coordinator_cloud() == "gcp"


def test_blank_coordinator_cloud_is_not_a_cloud_named_empty(monkeypatch):
    monkeypatch.setenv(COORDINATOR_CLOUD_ENV, "   ")
    assert coordinator_cloud() is None


def test_hop_kind_separates_the_coordinators_own_cloud(monkeypatch):
    assert hop_kind(GCP_SERVER, "gcp") == "in-cloud"
    assert hop_kind(SERVER, "gcp") == "cross-cloud"


@pytest.mark.asyncio
async def test_probe_records_the_hop_on_a_failed_cell(monkeypatch):
    """A denied in-cloud cell must still be labelled, or the footnote loses it."""

    def deny(*a, **k):
        raise AdapterError(FailureKind.AUTHENTICATION, "denied")

    monkeypatch.setattr("matrix.runner.credentials_for", deny)
    result = await probe("a2a-sdk", GCP_SERVER, request(), timeout_s=1, hop="in-cloud")

    assert result.ok is False
    assert result.hop == "in-cloud"


def test_in_cloud_cells_are_marked_and_excluded_from_the_interop_count():
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[
            cell("a2a-sdk", "gcp", True, hop="in-cloud"),
            cell("a2a-sdk", "aws", True, hop="cross-cloud"),
            cell("a2a-sdk", "azure", True, hop="cross-cloud"),
        ],
    )
    assert report.in_cloud_servers == ["gcp"]

    table = render_table(report)
    assert "3/3 attempted cells succeeded" in table
    assert "of which 2 crossed a cloud boundary and 1 did not" in table
    assert "gcp*" in table
    assert "* in-cloud hop: gcp" in table
    # The columns that did cross must not be marked.
    assert "aws*" not in table
    assert "azure*" not in table


def test_brain_label_comes_from_the_servers_not_the_runner(monkeypatch):
    """The regression: the runner is a different container once deployed.

    Reading CURRENCY_MODEL_MODE here produced a table that said brain=direct
    while every agent in it was running a model.
    """
    monkeypatch.setenv("CURRENCY_MODEL_MODE", "direct")
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[
            cell("a2a-sdk", "gcp", True, server_brain="llm"),
            cell("a2a-sdk", "aws", True, server_brain="llm"),
            cell("a2a-sdk", "azure", True, server_brain="llm"),
        ],
    )
    assert report.brain_summary == "llm"
    assert "brain=llm" in render_table(report)


def test_a_mixed_mesh_is_not_summarised_as_one_word():
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[
            cell("a2a-sdk", "gcp", True, server_brain="llm"),
            cell("a2a-sdk", "aws", True, server_brain="direct"),
        ],
    )
    assert report.brain_summary == "mixed (gcp=llm, aws=direct)"


def test_one_unreachable_server_does_not_get_a_confident_label():
    """'unknown' must not be averaged away into the others' answer."""
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[
            cell("a2a-sdk", "gcp", True, server_brain="llm"),
            cell("a2a-sdk", "aws", False, server_brain="unknown"),
        ],
    )
    assert report.brain_summary == "mixed (gcp=llm, aws=unknown)"


def test_brain_defaults_to_unknown_rather_than_direct():
    """A cell nobody asked must not read as a deliberate 'direct'."""
    assert cell("a2a-sdk", "gcp", True).server_brain == "unknown"


@pytest.mark.asyncio
async def test_server_brain_reads_the_health_endpoint():
    import httpx

    from matrix import runner as runner_module

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/health")
        return httpx.Response(200, json={"status": "ok", "agent": "x", "brain": "llm"})

    original = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    runner_module.httpx.AsyncClient = fake_client
    try:
        assert await runner_module.server_brain(GCP_SERVER) == "llm"
    finally:
        runner_module.httpx.AsyncClient = original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"status": "ok"},
        {"status": "ok", "brain": ""},
        {"status": "ok", "brain": 7},
    ],
)
async def test_a_health_reply_without_a_usable_brain_is_unknown(response):
    import httpx

    from matrix import runner as runner_module

    original = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, json=response)
        )
        return original(*args, **kwargs)

    runner_module.httpx.AsyncClient = fake_client
    try:
        assert await runner_module.server_brain(GCP_SERVER) == "unknown"
    finally:
        runner_module.httpx.AsyncClient = original


@pytest.mark.asyncio
async def test_an_unreachable_agent_is_unknown_not_a_crash():
    """The label must never fail the run: it is a label."""
    from matrix.runner import server_brain

    unreachable = Server("gcp", "Google Cloud", "adk to_a2a", "http://127.0.0.1:9")
    assert await server_brain(unreachable, timeout_s=1.0) == "unknown"


def test_local_report_says_nothing_about_boundaries():
    """The local matrix reads exactly as it always did -- no footnote, no stars."""
    report = MatrixReport(
        request_summary="100 USD -> EUR",
        model_mode="direct",
        cells=[cell("a2a-sdk", "gcp", True), cell("a2a-sdk", "aws", True)],
    )
    table = render_table(report)
    assert "2/2 attempted cells succeeded" in table
    assert "*" not in table
    assert "crossed a cloud boundary" not in table


def test_a_missing_version_header_is_not_read_as_a_0_3_client():
    """AgentCore does not forward A2A-Version; absent must not mean 0.3.

    a2a-sdk defaults a missing header to 0.3 and then rejects it as
    unsupported, so the same code passed on Cloud Run and Container Apps and
    failed behind AgentCore with
    "A2A version '0.3' is not supported by this handler".
    """
    from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, VERSION_HEADER
    from starlette.testclient import TestClient

    from agents.serving import build_agent_card, build_app, direct_executor

    card = build_agent_card(name="currency_agent", url="http://testserver/")
    seen: dict[str, str] = {}

    app = build_app(direct_executor(), card)

    async def echo(request):
        from starlette.responses import JSONResponse

        seen["version"] = request.headers.get(VERSION_HEADER, "<absent>")
        return JSONResponse({"v": seen["version"]})

    app.router.add_route("/echo-version", echo, methods=["GET"])

    with TestClient(app) as client:
        # No A2A-Version header, exactly as AgentCore delivers it.
        body = client.get("/echo-version").json()

    assert body["v"] == PROTOCOL_VERSION_CURRENT


def test_an_explicit_old_version_is_still_rejected():
    """The middleware fills a gap; it must not overwrite a real client claim."""
    from a2a.utils.constants import VERSION_HEADER
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient

    from agents.serving import build_agent_card, build_app, direct_executor

    app = build_app(
        direct_executor(), build_agent_card(name="currency_agent", url="http://testserver/")
    )

    async def echo(request):
        return JSONResponse({"v": request.headers.get(VERSION_HEADER, "<absent>")})

    app.router.add_route("/echo-version", echo, methods=["GET"])

    with TestClient(app) as client:
        body = client.get("/echo-version", headers={VERSION_HEADER: "0.3"}).json()

    assert body["v"] == "0.3"
