from datetime import UTC, datetime
from decimal import Decimal

import pytest

from coordinator.consensus import median, reach_consensus
from coordinator.models import ConversionQuote


def quote(source: str, converted: str, *, target: str = "EUR", rate: str | None = None):
    return ConversionQuote(
        source=source,
        source_currency="USD",
        target_currency=target,
        amount=Decimal(100),
        rate=Decimal(rate or converted) / Decimal(100),
        converted_amount=Decimal(converted),
        observed_at=datetime.now(UTC),
        latency_ms=1.0,
    )


def test_median_odd_and_even():
    assert median([Decimal(3), Decimal(1), Decimal(2)]) == Decimal(2)
    assert median([Decimal(1), Decimal(3)]) == Decimal(2)


def test_median_rejects_empty():
    with pytest.raises(ValueError):
        median([])


def test_three_clouds_in_agreement():
    result = reach_consensus(
        "EUR",
        [quote("gcp", "92.00"), quote("aws", "92.01"), quote("azure", "91.99")],
    )
    assert result.agreed is True
    assert result.outliers == []
    assert result.consensus_amount == Decimal("92.00")
    assert result.warnings == []


def test_single_outlier_does_not_move_the_median():
    """The property that makes three clouds worth more than two."""
    result = reach_consensus(
        "EUR",
        [quote("gcp", "92.00"), quote("aws", "92.00"), quote("azure", "150.00")],
    )
    assert result.agreed is False
    assert result.outliers == ["azure"]
    assert result.consensus_amount == Decimal("92.00")
    assert "azure" in result.warnings[0]


def test_two_clouds_disagreeing_flags_both():
    """With no majority, neither cloud can be called the outlier."""
    result = reach_consensus("EUR", [quote("gcp", "92.00"), quote("aws", "150.00")])
    assert result.agreed is False
    assert sorted(result.outliers) == ["aws", "gcp"]


def test_single_response_is_unverified_not_agreed():
    result = reach_consensus("EUR", [quote("gcp", "92.00")])
    assert result.agreed is None
    assert result.consensus_amount == Decimal("92.00")
    assert "unverified" in result.warnings[0]


def test_no_response_yields_empty_result():
    result = reach_consensus("EUR", [])
    assert result.agreed is None
    assert result.consensus_amount is None
    assert result.quotes == []


def test_spread_within_tolerance_still_agrees():
    result = reach_consensus(
        "EUR",
        [quote("gcp", "92.00"), quote("aws", "92.20"), quote("azure", "92.10")],
    )
    assert result.agreed is True
    assert result.max_relative_spread < Decimal("0.005")


def test_mismatched_conversions_are_rejected():
    with pytest.raises(ValueError, match="same conversion"):
        reach_consensus("EUR", [quote("gcp", "92.00"), quote("aws", "79.00", target="GBP")])
