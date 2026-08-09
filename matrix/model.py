"""Result types for the client-stack x server-agent interop matrix."""

from decimal import Decimal

from pydantic import BaseModel, Field


class Cell(BaseModel):
    """One directed A2A call: a client SDK dialling one cloud's agent."""

    client_stack: str
    server: str
    server_cloud: str
    server_stack: str
    #: Auth mode used to reach this server; "none" against the local mesh.
    auth: str = "none"
    #: Whether this call left the coordinator's own cloud. "local" is the
    #: loopback mesh, where the question does not arise; "in-cloud" is a hop
    #: that never crossed a vendor boundary and must not be counted toward the
    #: interop claim the way a "cross-cloud" cell can.
    hop: str = "local"
    #: The brain this *server* reported on /health -- "direct", "llm", or
    #: "unknown" when it could not be asked. Never the runner's own setting:
    #: the runner is a different container once deployed, and reading its
    #: CURRENCY_MODEL_MODE produced a table that said direct while measuring
    #: three llm agents.
    server_brain: str = "unknown"
    ok: bool
    #: FailureKind value, or "sdk-missing" when the client SDK is not installed.
    failure_kind: str | None = None
    detail: str | None = None
    latency_ms: float | None = None
    quotes: int = 0
    converted_amount: Decimal | None = None

    @property
    def symbol(self) -> str:
        if self.ok:
            return "ok"
        if self.failure_kind == "sdk-missing":
            return "-"
        return self.failure_kind or "fail"


class MatrixReport(BaseModel):
    request_summary: str
    model_mode: str
    cells: list[Cell] = Field(default_factory=list)

    @property
    def client_stacks(self) -> list[str]:
        seen: list[str] = []
        for cell in self.cells:
            if cell.client_stack not in seen:
                seen.append(cell.client_stack)
        return seen

    @property
    def servers(self) -> list[str]:
        seen: list[str] = []
        for cell in self.cells:
            if cell.server not in seen:
                seen.append(cell.server)
        return seen

    def cell(self, client_stack: str, server: str) -> Cell | None:
        for candidate in self.cells:
            if candidate.client_stack == client_stack and candidate.server == server:
                return candidate
        return None

    @property
    def attempted(self) -> list[Cell]:
        """Cells that actually ran, i.e. excluding uninstalled client SDKs."""
        return [cell for cell in self.cells if cell.failure_kind != "sdk-missing"]

    @property
    def brains(self) -> dict[str, str]:
        """What each server said its brain was, in column order."""
        found: dict[str, str] = {}
        for cell in self.cells:
            found.setdefault(cell.server, cell.server_brain)
        return found

    @property
    def brain_summary(self) -> str:
        """One honest phrase for the header.

        A single value only when every server agreed and none was unknown --
        otherwise the per-server breakdown, because a mixed mesh cannot be
        described by one word and a table that tries is the bug this replaced.
        """
        brains = self.brains
        if not brains:
            return "unknown"
        distinct = set(brains.values())
        if len(distinct) == 1 and "unknown" not in distinct:
            return distinct.pop()
        detail = ", ".join(f"{server}={brain}" for server, brain in brains.items())
        return f"mixed ({detail})"

    @property
    def in_cloud_servers(self) -> list[str]:
        """Servers sharing the coordinator's cloud, so reaching them crosses nothing."""
        seen: list[str] = []
        for cell in self.cells:
            if cell.hop == "in-cloud" and cell.server not in seen:
                seen.append(cell.server)
        return seen
