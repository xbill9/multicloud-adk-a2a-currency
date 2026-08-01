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
