"""N-way agreement over quotes from independent clouds.

Replaces the pairwise ``compare_quotes`` of the two-cloud benchmarks. The
pairwise version measured the verifier against the primary, which silently
privileged whichever adapter was wired first. With three or more participants
there is no privileged answer, so consensus is the median and every
participant is scored against it symmetrically.
"""

from decimal import Decimal

from coordinator.models import ConsensusResult, ConversionQuote

DEFAULT_TOLERANCE = Decimal("0.005")


def median(values: list[Decimal]) -> Decimal:
    """Median of a non-empty list, averaging the middle pair when even."""
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def reach_consensus(
    target_currency: str,
    quotes: list[ConversionQuote],
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> ConsensusResult:
    """Fold every participant's quote for one target into a single verdict."""
    if not quotes:
        return ConsensusResult(
            target_currency=target_currency,
            warnings=["no participant returned a quote for this target"],
        )

    mismatched = [
        quote.source
        for quote in quotes
        if quote.target_currency != target_currency
        or quote.source_currency != quotes[0].source_currency
        or quote.amount != quotes[0].amount
    ]
    if mismatched:
        raise ValueError(f"quotes do not describe the same conversion: {mismatched}")

    amounts = [quote.converted_amount for quote in quotes]
    consensus_amount = median(amounts)
    consensus_rate = median([quote.rate for quote in quotes])

    if len(quotes) == 1:
        return ConsensusResult(
            target_currency=target_currency,
            quotes=quotes,
            consensus_amount=consensus_amount,
            consensus_rate=consensus_rate,
            agreed=None,
            warnings=[
                (
                    "only one cloud responded; independent verification unavailable, "
                    "result is unverified"
                )
            ],
        )

    spread = (max(amounts) - min(amounts)) / consensus_amount if consensus_amount else Decimal(0)
    outliers = [
        quote.source
        for quote in quotes
        if consensus_amount
        and abs(quote.converted_amount - consensus_amount) / consensus_amount > tolerance
    ]
    agreed = not outliers

    warnings: list[str] = []
    if not agreed:
        warnings.append(
            f"{len(outliers)} of {len(quotes)} clouds disagree by more than "
            f"{tolerance:.2%} ({', '.join(outliers)}); do not treat this as a final quote"
        )

    return ConsensusResult(
        target_currency=target_currency,
        quotes=quotes,
        consensus_amount=consensus_amount,
        consensus_rate=consensus_rate,
        max_relative_spread=spread,
        agreed=agreed,
        outliers=outliers,
        warnings=warnings,
    )
