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

import httpx

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

#: Set on the deployed jobs to the cloud the coordinator itself runs in. A cell
#: whose server matches never leaves that cloud, and a 3x3 that silently counts
#: such a cell toward the interop claim is inflating it -- the coordinator runs
#: on Cloud Run, so the gcp column is Cloud Run reaching Cloud Run. Unset for
#: the local mesh, where every endpoint is loopback and the question is moot.
COORDINATOR_CLOUD_ENV = "CURRENCY_COORDINATOR_CLOUD"


def coordinator_cloud() -> str | None:
    """Which cloud the matrix is being run *from*, or None when run locally."""
    return os.getenv(COORDINATOR_CLOUD_ENV, "").strip().lower() or None


def hop_kind(server: Server, from_cloud: str | None) -> str:
    """Classify one leg as local, in-cloud, or genuinely cross-cloud."""
    if from_cloud is None:
        return "local"
    return "in-cloud" if server.name.lower() == from_cloud else "cross-cloud"


async def server_brain(server: Server, *, timeout_s: float = 45.0) -> str:
    """Ask the agent which brain it is running.

    Carries the peer's credential, because /health sits behind the same
    privileged ingress as everything else -- Cloud Run's IAM, Container Apps'
    Entra enforcement, AgentCore's SigV4. Probing it unauthenticated returns
    403 and every deployed column would be labelled "unknown", which is the
    one situation this label exists for.

    Best effort otherwise by design: this is a label, and a server that will
    not answer must degrade to "unknown" rather than fail a cell or, worse,
    inherit the runner's own CURRENCY_MODEL_MODE -- which is what it used to
    do, and how a run against three `llm` agents came to print brain=direct.

    The timeout is generous because every service here scales to zero and this
    is the *first* request of a run: at 10s it reported Azure as "unknown"
    purely because a cold Container App takes ~20s to answer, which is an
    honest label of the wrong thing.
    """
    try:
        auth = credentials_for(server.name, server.endpoint)
    except AdapterError:
        auth = None

    base = server.endpoint.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout_s, auth=auth) as client:
        # /health first: it is the direct answer where the path is reachable.
        try:
            response = await client.get(f"{base}/health")
            if response.is_success:
                brain = response.json().get("brain")
                if isinstance(brain, str) and brain:
                    return brain
        except (httpx.HTTPError, ValueError):
            pass

        # Then the agent card, for stacks that do not expose arbitrary paths.
        # AgentCore serves only its own /invocations and /ping contract, so
        # /health cannot be fetched there however the agent is configured.
        try:
            response = await client.get(f"{base}/.well-known/agent-card.json")
            if not response.is_success:
                return "unknown"
            for skill in response.json().get("skills") or []:
                for tag in skill.get("tags") or []:
                    if isinstance(tag, str) and tag.startswith("brain:"):
                        return tag.split(":", 1)[1] or "unknown"
        except (httpx.HTTPError, ValueError, AttributeError):
            return "unknown"
    return "unknown"


async def probe(
    stack: str,
    server: Server,
    request: ConversionRequest,
    *,
    timeout_s: float,
    hop: str = "local",
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
            hop=hop,
            ok=False,
            failure_kind=exc.kind.value,
            detail=str(exc)[:300],
        )

    base = dict(  # noqa: C408 - keyword form reads better than a literal here
        client_stack=stack,
        server=server.name,
        server_cloud=server.cloud,
        server_stack=server.stack,
        auth=auth_mode(auth),
        hop=hop,
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
    from_cloud = coordinator_cloud()
    # Asked once per server rather than per cell: it is a property of the
    # agent, not of the client dialling it.
    brains = {server.name: await server_brain(server) for server in servers}
    cells: list[Cell] = []
    for stack in stacks:
        for server in servers:
            cell = await probe(
                stack,
                server,
                request,
                timeout_s=timeout_s,
                hop=hop_kind(server, from_cloud),
            )
            cells.append(cell.model_copy(update={"server_brain": brains[server.name]}))
    return MatrixReport(
        request_summary=(
            f"{request.amount} {request.source_currency} -> "
            f"{', '.join(request.target_currencies)}"
        ),
        # Retained for compatibility with existing reports, but no longer the
        # thing that gets printed: it describes the runner, not the mesh.
        model_mode=os.getenv("CURRENCY_MODEL_MODE", "direct"),
        cells=cells,
    )


def render_table(report: MatrixReport) -> str:
    servers = report.servers
    in_cloud = set(report.in_cloud_servers)
    width = max([len(stack) for stack in report.client_stacks] + [14])
    headers = [f"{server}*" if server in in_cloud else server for server in servers]
    columns = [max(len(header), 16) for header in headers]

    lines = [
        f"A2A interop matrix  ({report.request_summary}, brain={report.brain_summary})",
        "",
        "client \\ server".ljust(width)
        + "  "
        + "  ".join(header.ljust(col) for header, col in zip(headers, columns)),
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

    # The interop claim rests on the cells that actually crossed a vendor
    # boundary, so say how many did. Without this the gcp column pads the
    # score with Cloud Run reaching Cloud Run.
    if in_cloud:
        crossed = [cell for cell in passed if cell.hop == "cross-cloud"]
        inside = [cell for cell in passed if cell.hop == "in-cloud"]
        lines.append(
            f"  of which {len(crossed)} crossed a cloud boundary and "
            f"{len(inside)} did not"
        )
        lines.append(
            "* in-cloud hop: " + ", ".join(sorted(in_cloud)) + " shares the "
            "coordinator's cloud, so these cells do not support the interop claim"
        )

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
        # Blocking write in an async function, deliberately: this is the last
        # statement of a CLI entrypoint with nothing else in flight, and
        # pulling in an async file library to satisfy the linter would be a
        # dependency bought for one line.
        with open(args.json_path, "w") as handle:  # noqa: ASYNC230
            handle.write(report.model_dump_json(indent=2))
        print(f"\nwrote {args.json_path}")

    return 0 if all(cell.ok for cell in report.attempted) else 1


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
