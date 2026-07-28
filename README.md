# Three-cloud A2A currency mesh

A proof of concept that links **native agents from Google Cloud, AWS, and
Azure** over A2A v1.0 — each built with its own vendor's agent framework, each
serving A2A through its own vendor's stack — and makes them answer one
question together.

It exists to answer a question the two-cloud version could not: **does A2A
actually interoperate between vendors, or does every pair need a workaround?**

Short answer: all nine client/server pairs work, and two of them only after
working around defects that neither vendor's own tests would catch. See
[`docs/INTEROP.md`](docs/INTEROP.md).

## The matrix

```console
$ ./infra/run_mesh.sh start
$ python -m matrix.runner

A2A interop matrix  (100 USD -> EUR, GBP, brain=direct)

client \ server  gcp               aws               azure
-----------------------------------------------------------------------
a2a-sdk          ok 69ms           ok 9ms            ok 10ms
agent-framework  ok 31ms           ok 7ms            ok 8ms
google-adk       ok 864ms          ok 10ms           ok 11ms

9/9 attempted cells succeeded
```

Three client SDKs × three natively-served agents. Every cell is one real A2A
call; a failed cell records which layer broke (`transport`, `protocol`,
`timeout`, `authentication`) rather than just failing.

## The mesh

```console
$ python -m coordinator.cli 100 USD EUR JPY

participants: gcp, aws, azure

100 USD = 92 EUR @ 0.92 [3/3 clouds, agreed]
    gcp                  92 (77ms)
    aws                  92 (14ms)
    azure                92 (14ms)
100 USD = 15000 JPY @ 150 [3/3 clouds, agreed]
    gcp               15000 (77ms)
    aws               15000 (14ms)
    azure             15000 (14ms)

elapsed 77ms
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

Disagreement is therefore tested by **fault injection** — perturbing one
participant's rate and asserting it is named as the outlier
(`tests/test_mesh.py`) — rather than by hoping three models diverge. Live rates
remain useful as an end-to-end validation pass, which is what they are kept for.

## Setup

Requires Python 3.13 and `uv`.

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python \
  "a2a-sdk[http-server]==1.1.2" "google-adk==2.5.0" \
  agent-framework-a2a agent-framework-core \
  "pydantic>=2.10" "httpx>=0.28" "uvicorn>=0.30" "pytest>=8.3" "pytest-asyncio>=0.25"
uv pip install --python .venv/bin/python -e .
```

Those two version pins matter: `google-adk` 2.4.0 cannot serve A2A v1.0 —
see finding 4 in `docs/INTEROP.md`.

`strands-agents` is needed only for the AWS agent's `llm` mode; every other
path runs without it.

## Run

```bash
./infra/run_mesh.sh start        # three agents on :10001 :10002 :10003
./infra/run_mesh.sh status
python -m matrix.runner --json report.json
python -m coordinator.cli 100 USD EUR GBP
./infra/run_mesh.sh stop
```

Tests are hermetic by default; the live suite skips itself unless the mesh is
up.

```bash
.venv/bin/python -m pytest tests/ -q     # 45 passed
```

## Status

Done and verified:

- N-way median consensus with per-participant failure isolation, replacing the
  pairwise primary/verifier model.
- Three native agents, each on its own vendor's A2A serving stack.
- Three client stacks behind one interface, sharing one parser.
- The full 3×3 matrix passing locally, with two real interop defects found,
  diagnosed, and documented.
- 45 tests, including all nine cells as assertions and a
  cloud-goes-offline degradation case.

Not done:

- **Nothing is deployed.** All three agents are local. Cloud Run, AgentCore
  Runtime, and Foundry hosting are the obvious next step, and finding 2 only
  reproduces once they are.
- `llm` mode is implemented but has been exercised for none of the three
  clouds; all measurements here are direct-brain.
- No token or cost accounting, and no warm/cold latency distributions.
