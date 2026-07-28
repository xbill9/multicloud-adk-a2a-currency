"""End-to-end tests against the local mesh.

Skipped unless all three agents are up (``./infra/run_mesh.sh start``), so the
default test run stays hermetic and credential-free.
"""

from decimal import Decimal

import httpx
import pytest

from clients import CLIENT_STACKS, load_client
from coordinator.mesh import CurrencyMesh
from coordinator.models import ConversionRequest
from coordinator.participants import Participant

ENDPOINTS = {
    "gcp": "http://127.0.0.1:10001",
    "aws": "http://127.0.0.1:10002",
    "azure": "http://127.0.0.1:10003",
}


def _up(endpoint: str) -> bool:
    try:
        return httpx.get(f"{endpoint}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not all(_up(endpoint) for endpoint in ENDPOINTS.values()),
    reason="local mesh is not running; start it with ./infra/run_mesh.sh start",
)


def request(*targets: str) -> ConversionRequest:
    return ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=list(targets)
    )


@pytest.mark.parametrize("stack", CLIENT_STACKS)
@pytest.mark.parametrize("cloud", list(ENDPOINTS))
async def test_every_client_reaches_every_cloud(stack, cloud):
    """The interop matrix as an assertion: all nine cells must round-trip."""
    try:
        client = load_client(stack, ENDPOINTS[cloud], timeout_s=60)
    except ImportError:
        pytest.skip(f"{stack} SDK is not installed")

    quotes = await client.convert(request("EUR", "GBP"))

    assert [quote.target_currency for quote in quotes] == ["EUR", "GBP"]
    assert quotes[0].converted_amount == Decimal(92)


async def test_three_clouds_reach_consensus():
    participants = [
        Participant(
            name=cloud,
            source=load_client("a2a-sdk", endpoint, source=cloud, timeout_s=60),
            cloud=cloud,
        )
        for cloud, endpoint in ENDPOINTS.items()
    ]
    run = await CurrencyMesh(participants, timeout_seconds=60).run(request("EUR"))

    assert run.verified
    assert run.failures == {}
    assert len(run.results[0].quotes) == 3
    assert run.results[0].consensus_amount == Decimal(92)


async def test_mesh_survives_one_unreachable_cloud():
    participants = [
        Participant(
            name="gcp",
            source=load_client("a2a-sdk", ENDPOINTS["gcp"], source="gcp", timeout_s=60),
        ),
        Participant(
            name="aws",
            source=load_client("a2a-sdk", ENDPOINTS["aws"], source="aws", timeout_s=60),
        ),
        Participant(
            name="offline",
            source=load_client("a2a-sdk", "http://127.0.0.1:9", source="offline", timeout_s=5),
        ),
    ]
    run = await CurrencyMesh(participants, timeout_seconds=15).run(request("EUR"))

    assert run.verified, "two healthy clouds should still reach consensus"
    assert "offline" in run.failures
    assert len(run.results[0].quotes) == 2
