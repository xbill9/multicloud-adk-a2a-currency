import asyncio
from decimal import Decimal

from coordinator.errors import AdapterError, FailureKind
from coordinator.local_adapters import DeterministicCurrencyAdapter
from coordinator.mesh import CurrencyMesh
from coordinator.models import ConversionRequest
from coordinator.participants import Participant
from coordinator.providers import StaticRateProvider


def participant(name: str, **kwargs) -> Participant:
    return Participant(
        name=name,
        source=DeterministicCurrencyAdapter(StaticRateProvider(), source=name, **kwargs),
        cloud=name,
    )


def request(*targets: str) -> ConversionRequest:
    return ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=list(targets)
    )


async def test_three_participants_agree():
    mesh = CurrencyMesh([participant("gcp"), participant("aws"), participant("azure")])
    run = await mesh.run(request("EUR", "GBP"))

    assert run.verified
    assert run.failures == {}
    assert len(run.results) == 2
    assert all(len(result.quotes) == 3 for result in run.results)


async def test_one_cloud_failing_degrades_to_the_rest():
    mesh = CurrencyMesh(
        [
            participant("gcp"),
            participant("aws"),
            Participant(
                name="azure",
                source=DeterministicCurrencyAdapter(
                    StaticRateProvider(),
                    source="azure",
                    failure=AdapterError(FailureKind.TRANSPORT, "connection refused"),
                ),
            ),
        ]
    )
    run = await mesh.run(request("EUR"))

    assert run.succeeded
    assert run.verified
    assert "azure" in run.failures
    assert "transport" in run.failures["azure"]
    assert len(run.results[0].quotes) == 2


async def test_perturbed_cloud_is_named_as_the_outlier():
    """Forced disagreement: only possible because clouds can differ upstream."""
    mesh = CurrencyMesh(
        [
            participant("gcp"),
            participant("aws"),
            participant("azure", rate_multiplier=Decimal("1.5")),
        ]
    )
    run = await mesh.run(request("EUR"))

    result = run.results[0]
    assert result.agreed is False
    assert result.outliers == ["azure"]


async def test_slow_cloud_times_out_without_failing_the_run():
    mesh = CurrencyMesh(
        [participant("gcp"), participant("aws", delay_ms=500)], timeout_seconds=0.05
    )
    run = await mesh.run(request("EUR"))

    assert run.succeeded
    assert "timeout" in run.failures["aws"]
    assert run.results[0].agreed is None


async def test_all_clouds_failing_yields_no_results():
    failing = Participant(
        name="gcp",
        source=DeterministicCurrencyAdapter(
            StaticRateProvider(),
            source="gcp",
            failure=AdapterError(FailureKind.PROTOCOL, "bad card"),
        ),
    )
    run = await CurrencyMesh([failing]).run(request("EUR"))

    assert run.results[0].quotes == []
    assert not run.verified


async def test_participants_are_called_concurrently():
    mesh = CurrencyMesh(
        [participant(name, delay_ms=150) for name in ("gcp", "aws", "azure")],
        timeout_seconds=5,
    )
    started = asyncio.get_running_loop().time()
    await mesh.run(request("EUR"))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.4, "fan-out should overlap, not serialize"


async def test_a_run_where_every_cloud_failed_is_not_a_success():
    """Regression from the first deploy: exit status is the job's health signal.

    ``succeeded`` used to be ``bool(results)``, and there is always one result
    per requested target whether or not anything filled it -- so a Cloud Run
    job whose every participant 401'd exited 0 and reported green.
    """
    mesh = CurrencyMesh([Participant(name="broken", source=_FailingSource())])

    run = await mesh.run(
        ConversionRequest(amount=Decimal(100), source_currency="USD", target_currencies=["EUR"])
    )

    assert run.results, "a result envelope per target is still expected"
    assert run.failures
    assert not run.succeeded


class _FailingSource:
    async def convert(self, request):
        raise AdapterError(FailureKind.AUTHENTICATION, "A2A endpoint returned 401")
