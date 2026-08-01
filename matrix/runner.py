"""Fill the 3x3 A2A interop matrix: every client stack against every cloud's agent.

The question this answers is not "does A2A work" -- each vendor demonstrates
that against its own agent -- but "does vendor X's client actually talk to
vendor Y's server". Every cell is one real A2A call, so a cell can fail on
transport, on the agent card, on the task lifecycle, or on the reply format,
and the recorded FailureKind says which.

    python -m matrix.runner                       # all cells, local mesh
    python -m matrix.runner --json report.json
    python -m matrix.runner --client a2a-sdk      # one row
"""

import argparse
import asyncio
import os
from dataclasses import dataclass
from decimal import Decimal

from clients import CLIENT_STACKS, load_client
from coordinator.auth import auth_mode, credentials_for
from coordinator.errors import AdapterError
from coordinator.models import ConversionRequest
from matrix.model import Cell, MatrixReport


@dataclass(frozen=True)
class Server:
    """One cloud's agent endpoint, plus how it serves A2A."""

    name: str
    cloud: str
    stack: str
    endpoint: str


DEFAULT_SERVERS = (
    Server("gcp", "Google Cloud", "adk to_a2a", os.getenv("GCP_A2A_ENDPOINT", "http://127.0.0.1:10001")),
    Server("aws", "AWS", "a2a-sdk routes", os.getenv("AWS_A2A_ENDPOINT", "http://127.0.0.1:10002")),
    Server("azure", "Azure", "agent-framework A2AExecutor", os.getenv("AZURE_A2A_ENDPOINT", "http://127.0.0.1:10003")),
)


async def probe(
    stack: str,
    server: Server,
    request: ConversionRequest,
    *,
    timeout_s: float,
) -> Cell:
    """Run one directed A2A call and classify whatever comes back."""
    try:
        auth = credentials_for(server.name, server.endpoint)
    except AdapterError as exc:
        # Misconfigured credentials are a cell failure, not a crash: one
        # unconfigured cloud must not stop the other six cells from running.
        return Cell(
            client_stack=stack,
            server=server.name,
            server_cloud=server.cloud,
            server_stack=server.stack,
            ok=False,
            failure_kind=exc.kind.value,
            detail=str(exc)[:300],
        )

    base = dict(
        client_stack=stack,
        server=server.name,
        server_cloud=server.cloud,
        server_stack=server.stack,
        auth=auth_mode(auth),
    )
    try:
        client = load_client(stack, server.endpoint, timeout_s=timeout_s, auth=auth)
    except ImportError as exc:
        return Cell(**base, ok=False, failure_kind="sdk-missing", detail=str(exc))

    try:
        quotes = await client.convert(request)
    except AdapterError as exc:
        return Cell(**base, ok=False, failure_kind=exc.kind.value, detail=str(exc)[:300])
    except Exception as exc:  # noqa: BLE001 - a client may fail outside its own mapping
        return Cell(
            **base,
            ok=False,
            failure_kind="unmapped",
            detail=f"{type(exc).__name__}: {exc}"[:300],
        )

    return Cell(
        **base,
        ok=True,
        latency_ms=round(max(quote.latency_ms for quote in quotes), 1),
        quotes=len(quotes),
        converted_amount=quotes[0].converted_amount,
    )


async def run_matrix(
    servers: tuple[Server, ...],
    stacks: tuple[str, ...],
    request: ConversionRequest,
    *,
    timeout_s: float = 120.0,
) -> MatrixReport:
    """Probe every cell.

    Cells run sequentially on purpose: these are latency measurements, and
    three SDKs hammering one uvicorn worker concurrently would measure
    contention instead of protocol overhead.
    """
    cells: list[Cell] = []
    for stack in stacks:
        for server in servers:
            cells.append(await probe(stack, server, request, timeout_s=timeout_s))
    return MatrixReport(
        request_summary=(
            f"{request.amount} {request.source_currency} -> "
            f"{', '.join(request.target_currencies)}"
        ),
        model_mode=os.getenv("CURRENCY_MODEL_MODE", "direct"),
        cells=cells,
    )


def render_table(report: MatrixReport) -> str:
    servers = report.servers
    width = max([len(stack) for stack in report.client_stacks] + [14])
    columns = [max(len(server), 16) for server in servers]

    lines = [
        f"A2A interop matrix  ({report.request_summary}, brain={report.model_mode})",
        "",
        "client \\ server".ljust(width)
        + "  "
        + "  ".join(server.ljust(col) for server, col in zip(servers, columns)),
        "-" * (width + 2 + sum(col + 2 for col in columns)),
    ]
    for stack in report.client_stacks:
        row = [stack.ljust(width)]
        for server, col in zip(servers, columns):
            cell = report.cell(stack, server)
            if cell is None:
                text = "?"
            elif cell.ok:
                text = f"ok {cell.latency_ms:.0f}ms" if cell.latency_ms is not None else "ok"
            else:
                text = cell.symbol
            row.append(text.ljust(col))
        lines.append("  ".join(row))

    attempted = report.attempted
    passed = [cell for cell in attempted if cell.ok]
    lines += ["", f"{len(passed)}/{len(attempted)} attempted cells succeeded"]

    # Only shown once something is deployed; against the local mesh every leg
    # is "none" and the table reads as it always did.
    authed = {cell.server: cell.auth for cell in report.cells if cell.auth != "none"}
    if authed:
        lines.append(
            "auth: " + ", ".join(f"{server}={mode}" for server, mode in sorted(authed.items()))
        )

    skipped = [cell for cell in report.cells if cell.failure_kind == "sdk-missing"]
    if skipped:
        stacks = sorted({cell.client_stack for cell in skipped})
        lines.append(f"skipped (SDK not installed): {', '.join(stacks)}")

    failures = [cell for cell in attempted if not cell.ok]
    if failures:
        lines += ["", "failures:"]
        for cell in failures:
            lines.append(f"  {cell.client_stack} -> {cell.server}: {cell.detail}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fill the A2A client x server interop matrix")
    parser.add_argument("--amount", default="100")
    parser.add_argument("--source", default="USD")
    parser.add_argument("--targets", nargs="+", default=["EUR", "GBP"])
    parser.add_argument(
        "--client",
        action="append",
        choices=list(CLIENT_STACKS),
        help="restrict to one client stack (repeatable); default is all three",
    )
    parser.add_argument(
        "--server",
        action="append",
        choices=[server.name for server in DEFAULT_SERVERS],
        help="restrict to one server (repeatable); default is all three",
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--json", dest="json_path", help="also write the full report as JSON")
    return parser


async def _run(args) -> int:
    request = ConversionRequest(
        amount=Decimal(args.amount),
        source_currency=args.source,
        target_currencies=args.targets,
    )
    servers = tuple(
        server for server in DEFAULT_SERVERS if not args.server or server.name in args.server
    )
    stacks = tuple(stack for stack in CLIENT_STACKS if not args.client or stack in args.client)

    report = await run_matrix(servers, stacks, request, timeout_s=args.timeout_seconds)
    print(render_table(report))

    if args.json_path:
        with open(args.json_path, "w") as handle:
            handle.write(report.model_dump_json(indent=2))
        print(f"\nwrote {args.json_path}")

    return 0 if all(cell.ok for cell in report.attempted) else 1


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
