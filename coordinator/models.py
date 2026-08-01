"""Domain models for the multi-cloud currency mesh.

Generalized from the pairwise (primary/verifier) model used by the two-cloud
benchmarks: a run now fans out to an arbitrary set of named participants and
reaches consensus across all of them, so adding a fourth cloud is a config
change rather than a model change.
"""

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class ConversionRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    source_currency: str
    target_currencies: list[str] = Field(min_length=1)

    @field_validator("source_currency")
    @classmethod
    def normalize_source(cls, value: str) -> str:
        return _iso_code(value)

    @field_validator("target_currencies")
    @classmethod
    def normalize_targets(cls, values: list[str]) -> list[str]:
        normalized = [_iso_code(value) for value in values]
        if len(normalized) != len(set(normalized)):
            raise ValueError("target currencies must be unique")
        return normalized


class ConversionQuote(BaseModel):
    source: str
    source_currency: str
    target_currency: str
    amount: Decimal = Field(gt=0)
    rate: Decimal = Field(gt=0)
    converted_amount: Decimal = Field(gt=0)
    observed_at: datetime
    latency_ms: float = Field(ge=0)

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        """Treat a timestamp without an offset as UTC so age math cannot crash."""
        return value if value.tzinfo else value.replace(tzinfo=UTC)


class ConsensusResult(BaseModel):
    """Agreement across every participant that answered for one target currency.

    ``consensus_amount`` is the median rather than the mean so a single
    divergent participant cannot drag the agreed value once three or more
    clouds respond -- the property that makes an N-way mesh worth more than a
    pair.
    """

    target_currency: str
    quotes: list[ConversionQuote] = Field(default_factory=list)
    consensus_amount: Decimal | None = None
    consensus_rate: Decimal | None = None
    max_relative_spread: Decimal | None = None
    agreed: bool | None = None
    outliers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def responded(self) -> list[str]:
        return [quote.source for quote in self.quotes]


class MeshRun(BaseModel):
    """Stable result envelope for one fan-out across the participant set."""

    request: ConversionRequest
    participants: list[str] = Field(default_factory=list)
    #: Participant name -> auth mode actually used on that leg. Recorded in the
    #: run rather than inferred from config afterwards: "which legs were
    #: keyless" is a claim the artifact has to be able to back on its own.
    auth_modes: dict[str, str] = Field(default_factory=dict)
    results: list[ConsensusResult] = Field(default_factory=list)
    failures: dict[str, str] = Field(default_factory=dict)
    elapsed_ms: float = Field(ge=0)

    @property
    def succeeded(self) -> bool:
        """True when at least one target actually reached a consensus value.

        Not ``bool(self.results)``: there is always one result per requested
        target, populated or not, so that test passed even when every cloud
        failed. Harmless while this only drove a CLI exit code on a laptop;
        wrong once a Cloud Run job's exit status is the health signal, where it
        reported a totally failed run as green.
        """
        return any(result.consensus_amount is not None for result in self.results)

    @property
    def verified(self) -> bool:
        """True when every target reached agreement across at least two clouds."""
        return bool(self.results) and all(result.agreed for result in self.results)


def _iso_code(value: str) -> str:
    result = value.strip().upper()
    if len(result) != 3 or not result.isalpha():
        raise ValueError(f"invalid ISO 4217-style currency code: {value!r}")
    return result
