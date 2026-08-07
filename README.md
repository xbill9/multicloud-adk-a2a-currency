# Three-cloud A2A currency mesh

A proof of concept that links **native agents from Google Cloud, AWS, and
Azure** over A2A v1.0 — each built with its own vendor's agent framework, each
serving A2A through its own vendor's stack — and makes them answer one
question together.

It exists to answer a question the two-cloud version could not: **does A2A
actually interoperate between vendors, or does every pair need a workaround?**

Short answer: locally, all nine client/server pairs work — two only after
working around defects that neither vendor's own tests would catch. Deployed,
that number drops: against the hosted GCP agent, **ADK's own client cannot
reach ADK's own server**, because `to_a2a()` advertises the container's bind
address and `RemoteA2aAgent` believes it. Both halves pass Google's tests,
because locally those two addresses are the same.

See [`docs/INTEROP.md`](docs/INTEROP.md).

## The demo

One command, four acts — the last two are the point:

```console
$ ./infra/demo.sh

1. Three clouds, one question       three vendors' frameworks, one answer
2. The interop matrix               3 client SDKs x 3 serving stacks
3. A cloud goes offline             the median degrades instead of failing
4. A cloud lies                     the median holds; the outlier is named
```

Any demo can show three green ticks. The claim this project makes is about
what happens when a participant is *wrong*, which is why acts 3 and 4 exist:

```console
100 USD = 92 EUR @ 0.92 [3/3 clouds, DISAGREED]
    gcp                  92 (229ms)
    aws               124.2 (25ms)
    azure                92 (24ms)
  warning: 1 of 3 clouds disagree by more than 0.50% (aws)
```

## The matrix

```console
$ ./infra/run_mesh.sh start
$ python3 -m matrix.runner

A2A interop matrix  (100 USD -> EUR, GBP, brain=direct)

client \ server  gcp               aws               azure
-----------------------------------------------------------------------
a2a-sdk          ok 220ms          ok 12ms           ok 15ms
agent-framework  ok 301ms          ok 21ms           ok 16ms
google-adk       ok 1831ms         ok 14ms           ok 12ms

9/9 attempted cells succeeded
```

Latencies are loopback, direct-brain, single runs on one machine — they order
the stacks and nothing more. An earlier revision of this table recorded
69/31/864ms for the `gcp` column on different hardware and an older ADK.

Three client SDKs × three natively-served agents. Every cell is one real A2A
call; a failed cell records which layer broke (`transport`, `protocol`,
`timeout`, `authentication`, `provider`) rather than just failing — the
classification walks the vendor SDK's exception chain, because every stack here
wraps the real cause in a type of its own.

## The mesh

```console
$ python3 -m coordinator.cli 100 USD EUR JPY

participants: gcp, aws, azure

100 USD = 92 EUR @ 0.92 [3/3 clouds, agreed]
    gcp                  92 (433ms)
    aws                  92 (27ms)
    azure                92 (31ms)
100 USD = 15000 JPY @ 150 [3/3 clouds, agreed]
    gcp               15000 (433ms)
    aws               15000 (27ms)
    azure             15000 (31ms)

elapsed 434ms
```

Consensus is the **median**, not a primary-plus-verifier pair, so a single
divergent cloud cannot move the agreed value once three respond — the property
that makes a third cloud worth adding. Clouds that time out or return garbage
degrade the quorum instead of failing the run.

## Architecture

```text
              coordinator/cli.py  (any machine)
                        |
        +---------------+---------------+
        | A2A v1.0      | A2A v1.0      | A2A v1.0
        v               v               v
  Google Cloud      AWS              Azure
  ADK LlmAgent      Strands Agent    Agent Framework Agent
  Gemini            Bedrock Nova     Foundry model
  served by         served by        served by
  to_a2a()          a2a-sdk routes   A2AExecutor
```

The three serving stacks are genuinely different code paths — that is what the
matrix measures. The client side is symmetric: any of the three client SDKs can
drive the whole mesh (`--client agent-framework`).

| Layer | Module |
|---|---|
| N-way consensus | `coordinator/consensus.py`, `coordinator/mesh.py` |
| Cloud-agnostic participant interface | `coordinator/participants.py` |
| Shared prompt + reply parsing | `protocol/quotes.py` |
| Three client stacks | `clients/` |
| Three native agents | `agents/{gcp,aws,azure}/server.py` |
| Interop matrix | `matrix/` |

## Two brains

Every agent runs one of two ways, set by `CURRENCY_MODEL_MODE`:

- **`direct`** (default) — answers deterministically from a rate provider. No
  model, no credentials, no upstream. A failed matrix cell is then
  unambiguously a protocol failure, never a model that wandered off-format or
  an expired key. The vendor's *serving* stack stays in the path in both
  modes, which is what the matrix is actually testing.
- **`llm`** — the cloud's native model through its native framework: Gemini
  via ADK, Nova via Strands, a Foundry deployment via Agent Framework.
  Requires that cloud's credentials.

## On Frankfurter

The two-cloud predecessors pulled live rates from the Frankfurter API on every
leg. That is available here (`CURRENCY_RATE_PROVIDER=frankfurter`,
`--rates frankfurter`) but is **not** the default, for two reasons:

1. When every cloud reads the same upstream they agree by construction. The
   earlier run recorded the ADK agent returning `1.1367` and the MCP tool
   returning `1.1367` — a real measurement of nothing. Consensus across
   correlated sources is vacuous.
2. It folds upstream HTTP latency, rate limits, and outages into numbers meant
   to measure A2A. A red cell should never mean "Frankfurter throttled us".

Disagreement is therefore tested by **fault injection** rather than by hoping
three models diverge, at two levels: `tests/test_mesh.py` perturbs a quote
after the fact, and `CURRENCY_RATE_SCALE_<AGENT>` skews a *running* agent so
the median can be watched holding (act 4 of `./infra/demo.sh`). Live rates
remain useful as an end-to-end validation pass, which is what they are kept for.

## Setup

Requires Python 3.13 and `uv`.

```bash
uv pip install --system \
  "a2a-sdk[http-server]" google-adk \
  agent-framework-a2a agent-framework-core \
  pydantic httpx uvicorn pytest pytest-asyncio
uv pip install --system -e .
```

Latest of everything, no virtualenv — see `CLAUDE.md`. `google-adk` 2.4.0 could
not serve A2A v1.0 (finding 4 in `docs/INTEROP.md`), but that is a fact about
2.4.0: retested 2026-08-02 on `google-adk` 2.6.1 + `a2a-sdk` 1.1.2 and it
serves.

`strands-agents` is needed only for the AWS agent's `llm` mode; every other
path runs without it.

## Run

```bash
./infra/run_mesh.sh start        # three agents on :10001 :10002 :10003
./infra/run_mesh.sh status
python3 -m matrix.runner --json report.json
python3 -m coordinator.cli 100 USD EUR GBP
./infra/run_mesh.sh stop
```

The GCP leg can also be run deployed and authenticated. The coordinator runs as
a Cloud Run job rather than locally because a user credential cannot mint an
arbitrary-audience ID token at all — there is no laptop equivalent of this path.

```bash
./infra/deploy_gcp.sh deploy     # service + coordinator job + roles/run.invoker
./infra/deploy_gcp.sh run        # execute the job
./infra/deploy_gcp.sh destroy
```

Tests are hermetic by default; the live suite skips itself unless the mesh is
up.

```bash
python3 -m pytest tests/ -q     # 92 passed, 11 skipped
```

## Status

Done and verified:

- N-way median consensus with per-participant failure isolation, replacing the
  pairwise primary/verifier model.
- Three native agents, each on its own vendor's A2A serving stack.
- Three client stacks behind one interface, sharing one parser.
- The full 3×3 matrix passing locally, with two real interop defects found,
  diagnosed, and documented.
- 71 tests, including all nine cells as assertions and a
  cloud-goes-offline degradation case.
- One credential seam for all three legs (`coordinator/auth.py`): Google ID
  token, STS `AssumeRoleWithWebIdentity` → SigV4, and Entra federated exchange
  behind a single `httpx.Auth`, attached to the client so the agent-card fetch
  is authenticated too. Peers default to unauthenticated, so the local matrix
  stays a protocol instrument.
- **The GCP leg is deployed and authenticated** (`./infra/deploy_gcp.sh`): the
  ADK agent as a Cloud Run service with `--no-allow-unauthenticated`, reached
  by the coordinator running as a Cloud Run job with a workload OIDC token.
  Proven in both directions — 643ms with the right audience, 401 on the card
  fetch with the wrong one, 403 with no token. Deploying it immediately found
  two defects a green test suite had not, and confirmed interop finding 2
  against a real hosted card — where the client it breaks turns out to be
  ADK's own.

Not done:

- **Two of three agents are still local.** AWS (AgentCore) and Azure (Foundry)
  are undeployed, so no measurement here crosses a cloud boundary — the one
  deployed hop is coordinator and agent both in GCP, an in-cloud hop.
- **Two of three auth legs have never seen a real provider.** The AWS STS and
  Entra implementations are hermetically tested only; no token has been
  exchanged with either. Treat them as code, not as results.
- `llm` mode is implemented but has been exercised for none of the three
  clouds; all measurements here are direct-brain.
- No token or cost accounting, and no warm/cold latency distributions.
