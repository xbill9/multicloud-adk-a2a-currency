"""Run one conversion across all three clouds and report the consensus.

    currency-mesh 100 USD EUR GBP
    currency-mesh 100 USD EUR --client agent-framework --json

Each cloud is reached over A2A through whichever client stack is selected, so
this is also the end-to-end demonstration that the three native agents --
Google ADK, Strands on Bedrock, Agent Framework on Foundry -- answer one
question together.
"""

import argparse
import asyncio
import os
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from clients import CLIENT_STACKS, load_client
from coordinator.local_adapters import DeterministicCurrencyAdapter
from coordinator.mesh import CurrencyMesh
from coordinator.models import ConversionRequest
from coordinator.participants import Participant, auth_mode, credentials_for
from coordinator.providers import FrankfurterRateProvider, StaticRateProvider

CLOUD_ENDPOINTS = {
    "gcp": ("GCP_A2A_ENDPOINT", "http://127.0.0.1:10001"),
    "aws": ("AWS_A2A_ENDPOINT", "http://127.0.0.1:10002"),
    "azure": ("AZURE_A2A_ENDPOINT", "http://127.0.0.1:10003"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a currency conversion across three clouds")
    parser.add_argument("amount", help="positive decimal amount")
    parser.add_argument("source_currency")
    parser.add_argument("target_currencies", nargs="+")
    parser.add_argument(
        "--cloud",
        action="append",
        choices=list(CLOUD_ENDPOINTS),
        help="restrict to one cloud (repeatable); default is all three",
    )
    parser.add_argument(
        "--client",
        choices=list(CLIENT_STACKS),
        default="a2a-sdk",
        help="A2A client stack used to reach every cloud",
    )
    parser.add_argument(
        "--local-reference",
        action="store_true",
        help="add an in-process fixture participant as a fourth opinion",
    )
    parser.add_argument(
        "--rates",
        choices=["fixture", "frankfurter"],
        default="fixture",
        help="rate source for the local reference participant only",
    )
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def build_participants(args) -> list[Participant]:
    clouds = args.cloud or list(CLOUD_ENDPOINTS)
    participants: list[Participant] = []
    for cloud in clouds:
        env_var, default = CLOUD_ENDPOINTS[cloud]
        endpoint = os.getenv(env_var, default)
        auth = credentials_for(cloud, endpoint)
        participants.append(
            Participant(
                name=cloud,
                source=load_client(
                    args.client,
                    endpoint,
                    source=cloud,
                    timeout_s=args.timeout_seconds,
                    auth=auth,
                ),
                cloud=cloud,
                stack=args.client,
                auth=auth_mode(auth),
            )
        )
    if args.local_reference:
        provider = (
            FrankfurterRateProvider() if args.rates == "frankfurter" else StaticRateProvider()
        )
        participants.append(
            Participant(
                name="local",
                source=DeterministicCurrencyAdapter(provider, source="local"),
                cloud="local",
                stack="in-process",
            )
        )
    return participants


def _fmt(value: Decimal) -> str:
    """Trim trailing zeros without letting Decimal switch to scientific notation."""
    return f"{value.normalize():f}"


def render(run) -> str:
    # Annotate only the authenticated legs, so the all-local run reads exactly
    # as it did before the auth seam existed.
    named = [
        f"{name} ({mode})" if (mode := run.auth_modes.get(name, "none")) != "none" else name
        for name in run.participants
    ]
    lines = [f"participants: {', '.join(named)}", ""]
    for result in run.results:
        if result.consensus_amount is None:
            lines.append(f"{result.target_currency}: no cloud answered")
        else:
            verdict = {True: "agreed", False: "DISAGREED", None: "unverified"}[result.agreed]
            lines.append(
                f"{_fmt(run.request.amount)} {run.request.source_currency} = "
                f"{_fmt(result.consensus_amount)} {result.target_currency} "
                f"@ {_fmt(result.consensus_rate)} "
                f"[{len(result.quotes)}/{len(run.participants)} clouds, {verdict}]"
            )
            for quote in result.quotes:
                lines.append(
                    f"    {quote.source:<8} {_fmt(quote.converted_amount):>14} "
                    f"({quote.latency_ms:.0f}ms)"
                )
        for warning in result.warnings:
            lines.append(f"  warning: {warning}")
    for name, failure in run.failures.items():
        lines.append(f"  {name} failure: {failure}")
    lines.append("")
    lines.append(f"elapsed {run.elapsed_ms:.0f}ms")
    return "\n".join(lines)


async def _run(args) -> int:
    try:
        request = ConversionRequest(
            amount=Decimal(args.amount),
            source_currency=args.source_currency,
            target_currencies=args.target_currencies,
        )
    except (InvalidOperation, ValidationError, ValueError) as exc:
        print(f"invalid request: {exc}")
        return 2

    mesh = CurrencyMesh(build_participants(args), timeout_seconds=args.timeout_seconds)
    run = await mesh.run(request)
    print(run.model_dump_json(indent=2) if args.as_json else render(run))
    return 0 if run.succeeded else 1


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
