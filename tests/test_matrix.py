from decimal import Decimal

import pytest

from coordinator.errors import AdapterError, FailureKind
from coordinator.models import ConversionRequest
from matrix.model import Cell, MatrixReport
from matrix.runner import Server, probe, render_table

SERVER = Server("azure", "Azure", "agent-framework A2AExecutor", "http://127.0.0.1:10003")


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
