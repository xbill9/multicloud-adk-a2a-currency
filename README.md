# Three-cloud A2A currency mesh

A proof of concept that links **native agents from Google Cloud, AWS, and
Azure** over A2A v1.0 — each built with its own vendor's agent framework, each
serving A2A through its own vendor's stack — and makes them answer one
question together.

It exists to answer a question the two-cloud version could not: **does A2A
actually interoperate between vendors, or does every pair need a workaround?**

Short answer: locally, all nine client/server pairs work — two only after
working around defects that neither vendor's own tests would catch. Deployed on
all three vendors' hosting, it is **8/9**: against the hosted GCP agent, **ADK's
own client cannot reach ADK's own server**, because `to_a2a()` advertises the
container's bind address and `RemoteA2aAgent` believes it. Both halves pass
Google's tests, because locally those two addresses are the same.

Everything crossing a cloud boundary here is authenticated with **no stored
secret**, and each of the three legs has a negative control behind it rather
than a self-reported mode.

One caveat the numbers below carry throughout: the coordinator runs on Cloud
Run, so **the GCP leg is an in-cloud hop, not a cross-cloud one**. Two of the
three legs cross a vendor boundary; the third is Cloud Run reaching Cloud Run,
and the matrix now marks it rather than letting it pad the score.

See [`docs/INTEROP.md`](docs/INTEROP.md) and
[`docs/DEPLOYMENT_PLAN.md`](docs/DEPLOYMENT_PLAN.md).

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
a2a-sdk          ok 163ms          ok 9ms            ok 9ms
agent-framework  ok 135ms          ok 7ms            ok 8ms
google-adk       ok 922ms          ok 8ms            ok 8ms

9/9 attempted cells succeeded
```

Latencies are loopback, direct-brain, single runs on one machine — they order
the stacks and nothing more. An earlier revision of this table recorded
69/31/864ms for the `gcp` column on different hardware and an older ADK.

When `CURRENCY_COORDINATOR_CLOUD` is set, the same table marks the column that
never left that cloud and reports the interop score separately from the raw
one:

```console
$ CURRENCY_COORDINATOR_CLOUD=gcp python3 -m matrix.runner --client a2a-sdk

client \ server  gcp*              aws               azure
----------------------------------------------------------------------
a2a-sdk         ok 153ms          ok 7ms            ok 8ms

3/3 attempted cells succeeded
  of which 2 crossed a cloud boundary and 1 did not
* in-cloud hop: gcp shares the coordinator's cloud, so these cells do not
  support the interop claim
```

`deploy_gcp.sh` sets that variable to `gcp` on both jobs, so a hosted run marks
the column. Unset — the local mesh — every leg is loopback, the distinction
does not arise, and the table reads exactly as it always did.

Confirmed hosted on 2026-08-08 — the deployed matrix job prints:

```console
client \ server  gcp*              aws               azure
a2a-sdk          ok 992ms          ok 1328ms         ok 538ms
agent-framework  ok 504ms          ok 994ms          ok 570ms
google-adk       transport         ok 5953ms         ok 471ms

8/9 attempted cells succeeded
  of which 6 crossed a cloud boundary and 2 did not
```

Six of the eight passing cells crossed a vendor boundary. The other two are
Cloud Run reaching Cloud Run, and no longer count toward the interop claim.

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
           coordinator/cli.py  (Cloud Run job, us-central1)
                        |
        +---------------+---------------+
        | A2A v1.0      | A2A v1.0      | A2A v1.0
        | ID token      | SigV4         | Entra token
        | IN-CLOUD HOP  | cross-cloud   | cross-cloud
        v               v               v
  Google Cloud      AWS              Azure
  ADK LlmAgent      Strands Agent    Agent Framework Agent
  Gemini            Bedrock Nova     Foundry model
  served by         served by        served by
  to_a2a()          a2a-sdk routes   A2AExecutor
  on               on                on
  Cloud Run        AgentCore Runtime Container Apps
  us-central1      us-west-2         westus2
```

Each credential is minted from the coordinator's own workload identity: a
Google ID token for Cloud Run, an STS `AssumeRoleWithWebIdentity` exchange into
SigV4 for AgentCore, and an Entra Federated Identity Credential exchange for
Container Apps. Three clouds, three mechanisms, **no stored secret** — the
coordinator's host is the only thing that makes that possible.

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

## Deployed

All three agents also run on their own vendor's hosting, reached from one
coordinator with **no long-lived secret anywhere in the mesh**. Deploy each
cloud with its own script, then wire the coordinator to all three:

```bash
./infra/deploy_aws.sh   deploy   # AgentCore Runtime + the federated role
./infra/deploy_azure.sh deploy   # Container App
./infra/deploy_azure.sh fic      # Entra app registration + FIC on Google's issuer
./infra/deploy_azure.sh auth     # make the ingress demand it

./infra/deploy_gcp.sh deploy     # ADK service + coordinator job + roles/run.invoker
./infra/deploy_gcp.sh wire       # fold the AWS and Azure legs into the job
./infra/deploy_gcp.sh run        # 3-cloud consensus, from the cloud
./infra/deploy_gcp.sh matrix     # the 3x3, every client against every hosted server
./infra/deploy_gcp.sh verify     # the negative controls
```

The coordinator runs *as a Cloud Run job* rather than locally, and that is not
a convenience: a user credential cannot mint an arbitrary-audience ID token at
all, so there is no laptop equivalent of this path. Its host is what sets the
whole auth bill — see [`docs/DEPLOYMENT_PLAN.md`](docs/DEPLOYMENT_PLAN.md).

`verify` is the part worth running twice. Every leg is probed alone, because
the mesh degrades on purpose: a three-cloud run with one credential removed
still reaches quorum on the other two and exits 0, which reads as "no denial"
and is not.

Tests are hermetic by default; the live suite skips itself unless the mesh is
up.

```bash
python3 -m pytest tests/ -q     # 85 passed, 11 skipped
```

## Status

Done and verified:

- N-way median consensus with per-participant failure isolation, replacing the
  pairwise primary/verifier model.
- Three native agents, each on its own vendor's A2A serving stack.
- Three client stacks behind one interface, sharing one parser.
- The full 3×3 matrix passing locally, with two real interop defects found,
  diagnosed, and documented.
- 96 tests, including all nine cells as assertions and a
  cloud-goes-offline degradation case.
- One credential seam for all three legs (`coordinator/auth.py`): Google ID
  token, STS `AssumeRoleWithWebIdentity` → SigV4, and Entra federated exchange
  behind a single `httpx.Auth`, attached to the client so the agent-card fetch
  is authenticated too. Peers default to unauthenticated, so the local matrix
  stays a protocol instrument.
- **The token-refresh branches are covered, and covering them found a latent
  crash.** A provider expiry with no UTC offset parsed cleanly and then raised
  `TypeError: can't compare offset-naive and offset-aware datetimes` on the
  *next* call, inside `usable` — a crash at a line with nothing to do with the
  cause. An unparseable one raised `ValueError`, which is not an `AdapterError`
  and so travelled back unmapped rather than as a named auth failure. Both are
  now one `_parse_expiry` helper shared by the STS and ECS paths, which had
  duplicated the same two gaps. Real AWS always sends a `Z`, so this never
  fired in production — it is a trap removed, not an outage explained.
- **All three agents are deployed on their own vendor's hosting** — Cloud Run,
  AgentCore Runtime, Container Apps — and answer one question together from a
  Cloud Run coordinator: `3/3 clouds, agreed`, and consensus latency at
  **max(legs) + ~1s** — emphatically not their sum, so the legs are concurrent,
  but not bare max(legs) either: the ~1s is the coordinator's own fixed cost,
  which no per-leg figure includes. Seven warm runs across two days now sit
  behind that: 2258–2511ms on 2026-08-07 and 1953–2169ms on 2026-08-08, with
  the gap over `max(legs)` at 880–984ms on the second day's four.
- **All three legs are keyless, and that is now a measured claim rather than a
  reported one.** Seven controls, 2026-08-07: each leg answers alone with its
  credential, each is denied alone without it, and the unauthenticated `curl`
  gets 403. Each probe isolates one leg, because the median absorbs a single
  denial and exits 0 — the failure mode that would have let three decorative
  auth modes look like three working ones. See
  [`docs/DEPLOYMENT_PLAN.md`](docs/DEPLOYMENT_PLAN.md#what-the-controls-actually-proved-2026-08-07).
- **The 3×3 matrix run hosted: 8/9**, the one red cell being interop finding 2
  — still ADK's own client against ADK's own server, now with two other clouds
  beside it as controls. Of those cells, only six cross a vendor boundary: the
  coordinator and the GCP agent are both on Cloud Run, and the matrix now marks
  that column instead of letting three in-cloud hops pad the interop score.
- Deploying found what a green suite had not, twice: two defects on the first
  GCP leg (a 401 misfiled as a protocol failure, a totally failed run exiting
  0), and on Azure a leg reporting `entra-fic` in front of a public ingress.
- **AgentCore is reached under least privilege**, closing the predecessor
  series' longest-standing open question. Its finding — that only
  `Resource: "*"` permitted the agent-card fetch — was a misdiagnosis: the card
  fetch is a separate IAM action (`bedrock-agentcore:GetAgentCard`), not a
  resource-scope problem. Confirmed by removing that one action and nothing
  else, which breaks discovery while the invoke keeps working. **AWS had been
  naming the missing action in the response body all along**; the earlier
  adapter kept the status code and threw the body away. That is why
  `coordinator/auth.py` logs the raw provider response at every auth boundary,
  and it is the first time that decision has paid for itself.

Not done:

- **Roughly 32 tests are gone.** The 2026-08-02 session left 92 passing; this
  repo has 60, because that work was recovered from a Cloud Build tarball and
  `.gcloudignore` excludes `tests/`. What they covered is unknown. The
  surviving 71 (60 + 11 skipped) passed; the suite is now 96 (85 + 11)
  after the in-cloud-hop and token-lifecycle work added twenty-five.
- **Nothing outstanding on AWS scoping** — this one moved to the done list:
  the deployed policy is scoped to one runtime ARN, `Resource: "*"` is not
  required, and the predecessor's contrary finding was a misdiagnosis. See
  below.
- ~~**Most hosted latencies are single runs.**~~ Done 2026-08-08: the matrix
  has five consecutive warm runs behind it and the consensus seven across two
  days, all labelled warm or cold. What that bought was not a tighter number
  but a **retracted one** — see the AWS session finding below. Cold remains a
  separate regime, not averaged in: the Azure leg measured 23378ms cold against
  441–570ms warm in the same session.
- **A documented anomaly turned out to be an artefact of the instrument.** The
  "`agent-framework` → AWS is reproducibly ~5.7s, unexplained" claim is
  withdrawn. Each matrix cell minted a fresh AgentCore session id
  (`coordinator/auth.py:741`), each session gets its own microVM, and the ~5.9s
  was that microVM starting — landing on whichever cell drew cold capacity,
  which is why the slow cell *moved between clients* across runs. Pinning
  `AWS_A2A_SESSION_ID` removes it (5926–6037ms → 704/710ms) and unpinning
  brings it back, interleaved so it is not warming drift. The mesh mints a
  fresh session per run too, so a consensus run can pay this without warning.
  See [`docs/INTEROP.md`](docs/INTEROP.md).
- **The AWS STS and Entra paths are proven end to end, but only on the happy
  path plus one denial each.** No token has ever expired in production: every
  deployed run is a Cloud Run job that lives a few seconds, so the refresh
  branches never execute there. They are now covered hermetically instead —
  the skew window, STS credentials that expire mid-run or arrive already
  expired, a short-lived Entra token, and an unreadable `exp` — because expiry
  is a clock question rather than a network one. What is still untested is a
  *real* aged token from either provider, and genuine clock skew between the
  coordinator's clock and theirs.
- **`llm` mode runs on all three clouds** (2026-08-09), having never produced
  an answer anywhere before. Gemini via ADK over MCP and Nova via Strands run
  locally; **Azure runs a model in production** — gpt-5-mini on AI Foundry,
  deployed via `./infra/deploy_azure.sh foundry` and passing 3/3 of its hosted
  matrix cells. Getting there needed code and infrastructure, not credentials:
  six defects, all in [`docs/INTEROP.md`](docs/INTEROP.md).
- **Only Azure's `llm` mode is deployed.** GCP and AWS were exercised on a
  laptop against their real models; neither has been rebuilt, and
  `Dockerfile.aws` still omits `strands-agents`, so the hosted AWS agent cannot
  serve `llm` mode as built. Every other hosted number here is direct-brain,
  and the mesh is now deliberately mixed — which the `brain=` label reports.
- ~~**The matrix's `brain=` label reads the wrong environment.**~~ Fixed
  2026-08-09. Every agent reports its own brain on `/health`, and the matrix
  asks each server rather than reading its own `CURRENCY_MODEL_MODE`. A mixed
  mesh is reported as one: `brain=mixed (gcp=direct, aws=unknown, azure=llm)`.
  The probe carries the peer's credential, because `/health` sits behind the
  same privileged ingress as everything else. **AWS reads `unknown`** — its
  endpoint ends in `/invocations/`, so there is no `/health` to fetch — and
  saying so beats the confidently wrong value it replaced.
- No token or cost accounting. Warm/cold is now labelled everywhere it is
  recorded, but only the consensus run has more than one sample behind it.
