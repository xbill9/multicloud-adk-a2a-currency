# A2A interop findings

Every finding here came from a real call between two vendors' stacks, not from
reading specifications. Run `python -m matrix.runner` to reproduce.

## The matrix

Nine directed calls: three client SDKs against three natively-served agents.
Latencies are local, direct-brain (no model), and measure protocol overhead
only — single runs on one machine, 2026-08-07. They order the stacks and
nothing more; earlier revisions of this table recorded 69/31/864ms for the
`gcp` column on different hardware and an older ADK.

| client \ server | GCP (ADK `to_a2a`) | AWS (a2a-sdk routes) | Azure (AF `A2AExecutor`) |
|---|---|---|---|
| `a2a-sdk` | ok 163ms | ok 9ms | ok 9ms \* |
| `agent-framework` `A2AAgent` | ok 135ms | ok 7ms | ok 8ms |
| `google-adk` `RemoteA2aAgent` | ok 922ms | ok 8ms | ok 8ms |

\* only after the fix in finding 2.

All nine cells pass — but two of them only after working around a defect, and
neither defect was visible from either vendor's own documentation or tests.

## The same matrix, deployed (2026-08-07)

All three agents now run on their own vendor's hosting, called from the
coordinator as a Cloud Run job in `us-central1`. **This is the first table here
where the calls actually cross cloud boundaries**, and it is the one that
counts; every other latency in this document is loopback.

| client \ server | GCP `us-central1` | AWS `us-west-2` | Azure `westus2` |
|---|---|---|---|
| `a2a-sdk` | ok 15484ms | ok 1288ms | ok 1542ms |
| `agent-framework` | ok 477ms | ok 5700ms | ok 360ms |
| `google-adk` | **transport** | ok 5913ms | ok 433ms |

Re-run with Azure held warm at `minReplicas: 1`, same 8/9:

| client \ server | GCP `us-central1` | AWS `us-west-2` | Azure `westus2` (warm) |
|---|---|---|---|
| `a2a-sdk` | ok 15966ms | ok 1052ms | ok 2070ms |
| `agent-framework` | ok 490ms | ok 5678ms | ok 414ms |
| `google-adk` | **transport** | ok 864ms | ok 637ms |

### Five warm runs, 2026-08-08 — now with a distribution

Everything above is a single execution per cell. Five consecutive warm runs,
all 8/9, min–max across the five (the `gcp` column is marked because the
coordinator runs there; see the in-cloud-hop note below):

| client \ server | GCP* `us-central1` | AWS `us-west-2` | Azure `westus2` |
|---|---|---|---|
| `a2a-sdk` | ok 928–1139ms | ok 989–1328ms | ok 441–538ms |
| `agent-framework` | ok 399–504ms | ok 851–1033ms | ok 370–570ms |
| `google-adk` | **transport** ×5 | ok 5926–6037ms | ok 432–531ms |

Cold is a different regime and is not mixed in: a cold Azure leg measured
23378ms in the same session's consensus run, against 441–570ms warm here.

**8/9. The one red cell is finding 2, and it is still ADK's own client against
ADK's own server.** That is the same failure the single-column deployed run
found on 2026-07-31, now reproduced with the other two clouds beside it as
controls: the two servers that advertise a `PUBLIC_URL` are reachable from all
three client stacks, and the one that advertises its bind address is not
reachable from the one client that routes by card.

Read the latencies with care, and preferably not at all:

- **Every service scales to zero**, so the first call into a column pays a cold
  start and the rest do not. `a2a-sdk` is simply the first row the runner
  executes; the ~15.5s in its GCP cell is a container starting, not a stack
  being slow, and `agent-framework` hits the same server ~480ms later. That
  number is stable across both runs, so it is the cold start, reproducibly.
- **Warming Azure moves its column from ~24s cold to 414–2070ms** and changes
  nothing else. That is the whole of the Azure "slowness" seen in earlier
  tables: `minReplicas`, not Container Apps.
- ~~**`agent-framework` → AWS is reproducibly ~5.7s.**~~ **Withdrawn
  2026-08-08 — it was never about the client.** See below.
- These were single runs per cell. The five-run table above now has a
  distribution behind it; the two tables above that do not.

### The ~5.7s AWS cell was an AgentCore session cold start

The claim above said "something in that client/AgentCore pairing costs a fixed
~4.5s". That was wrong, and the shape of the evidence should have given it
away: across the two runs the slow cell **moved** — `agent-framework` in one,
`google-adk` in the other, both in a third. A fixed per-client cost does not
move. Something per-*call* does.

`coordinator/auth.py:741` mints the AgentCore session header as
`os.getenv("AWS_A2A_SESSION_ID") or str(uuid.uuid4())`, and `credentials_for()`
runs once per cell. So each of the three AWS cells opened a **different**
AgentCore session, and AgentCore gives each session its own microVM. The ~5.9s
is that microVM starting. It lands on whichever cell draws cold capacity, which
is why it wandered between clients.

Pinning one session id across all three cells removes it, and releasing the pin
brings it straight back. The two conditions were interleaved in time, so this
is not warming drift:

| `google-adk` → AWS | runs | measured |
|---|---|---|
| fresh UUID per cell (default) | 5 | 5953, 5970, 5926, 5984, 6037ms |
| `AWS_A2A_SESSION_ID` pinned | 2 | 710, 704ms |

The whole AWS column drops with the pin — `a2a-sdk` 815–1147ms,
`agent-framework` 621–622ms — because none of the three cells is paying for a
new microVM any more.

Two things follow. **The matrix's AWS column was measuring session cold starts,
not client stacks**, and any comparison drawn from it between the three clients
was reading noise. And **the mesh pays this in production too**: every
`credentials_for()` call mints a fresh session, so a consensus run opens a new
AgentCore session each time. Those runs measured 1073–1207ms on the AWS leg, so
they usually draw warm capacity — but nothing guarantees it, and a ~6s leg
would silently become the `max(legs)` that sets the whole run's elapsed time.
Set `AWS_A2A_SESSION_ID` to make the leg predictable; leave it unset only if
you actually want per-call session isolation.

What is *not* established: why cold capacity falls to one cell rather than
another, or how many warm sessions AgentCore keeps. Only the cause of the
latency is proven here, not the scheduler's policy.

The predecessor series' prediction holds in shape: 0.4–1.5s to warm containers,
and nothing here approaches the 18.8–25.1s it measured against hosted *model*
runtimes, because every agent in this table is `direct`-brain.

## Finding 1: a completed Task carries the answer in a different field per vendor

The headline result. Both ADK and Agent Framework return a `Task` in
`TASK_STATE_COMPLETED`, and both are spec-conformant. They disagree about
where the reply goes:

- **ADK** attaches it as an **artifact** (`task.artifacts[].parts[].text`).
- **Agent Framework's `A2AExecutor`** drives the full task lifecycle
  (`submit` → `start_work` → `complete`) and leaves the reply as a
  `ROLE_AGENT` message in **`task.history`**, with `artifacts` empty.

So the obvious client — read `task.artifacts`, which is what the two-cloud
version of this project shipped — works perfectly against Google and returns
an **empty string** against Microsoft. Not an error, not a timeout: a
successful call with no content, which then fails downstream as a parse error
pointing at the wrong layer.

`clients/a2a_sdk.py::_task_texts` reads artifacts, `status.message`, and
agent-role history entries, in that order.

The lesson generalizes past currency quotes: **"the call succeeded" and "you
received the answer" are different claims in A2A**, and a client written
against one vendor's server will pass its own tests while silently dropping
another vendor's replies.

## Finding 2: ADK's `to_a2a()` advertises its bind address

`to_a2a(agent, host, port)` writes `host:port` straight into the agent card's
`supportedInterfaces[].url`. On Cloud Run the process binds `0.0.0.0:8080`, so
the deployed card advertises an address no client can route to. The live
Cloud Run agent from the earlier two-cloud work still shows it:

```console
$ curl -s https://currency-adk-a2a-...run.app/.well-known/agent-card.json
{"supportedInterfaces":[{"url":"http://127.0.0.1:8080","protocolBinding":"JSONRPC"}], ...}
```

Clients that route by card URL — including the `a2a-sdk` reference client —
are unreachable against it without rewriting the interfaces after resolution
(`clients/a2a_sdk.py`). The AWS and Azure agents here take a `PUBLIC_URL`
environment variable and advertise that instead, which is the behaviour ADK is
missing rather than anything clever.

This does **not** reproduce on a local mesh, where bind address and dial
address coincide. It needs a deployment, or a deliberate mismatch, which is
why it survived into production in the first place.

### Confirmed on this repo's own deployment (2026-07-31)

The GCP agent is now on Cloud Run, and it reproduces exactly:

```console
$ curl -sH "Authorization: Bearer $(gcloud auth print-identity-token)" \
    https://currency-gcp-...run.app/.well-known/agent-card.json
{"url": null,
 "additionalInterfaces": [{"url": "http://0.0.0.0:8080", "protocolBinding": "JSONRPC", ...}]}
```

A public HTTPS endpoint advertising unroutable plaintext `http://0.0.0.0:8080`.
Hosted it is strictly worse than the two-cloud sighting above: `127.0.0.1` at
least resolves, and the scheme downgrade is new.

**Which clients survive it is the opposite of what finding 3 predicts.** Same
deployed server, one matrix column:

| client | local | deployed |
|---|---|---|
| `a2a-sdk` | ok 69ms | **ok 1027ms** — rewrites the interfaces after resolution |
| `agent-framework` `A2AAgent` | ok 31ms | **ok 424ms** — never routes by card, so the bad card is inert |
| `google-adk` `RemoteA2aAgent` | ok 864ms | **fails** — routes by card, dials `0.0.0.0:8080` |

`agent-framework` cannot *express* the workaround (finding 3) and does not need
it, because it dials the URL it was constructed with. The stack that fails is
**ADK's own client against ADK's own server** — `to_a2a()` writes the bind
address and `RemoteA2aAgent` honours it, so the one pairing that is entirely
one vendor's code is the one that cannot complete a hop. Both halves ship
green in Google's own tests, because locally the two addresses coincide.

## Finding 5: ADK's client masks the connection error with an `AttributeError`

Chased down from finding 2's deployed failure. Having dialled `0.0.0.0:8080`
and failed, `RemoteA2aAgent` does not report that:

```
google-adk -> gcp: A2A protocol failure from https://currency-gcp-...run.app:
  AttributeError: 'A2AClientError' object has no attribute 'status_code'
```

The error handler assumes any `A2AClientError` carries `.status_code`, which a
transport-layer failure does not. So the reported error names neither the
address it could not reach nor the reason — the actual cause (`All connection
attempts failed`) only appears on a separate log line, and the exception that
propagates is from the error handler, not the error.

Two defects compounding is what makes this expensive: the first sends the
client to an unroutable address, and the second removes the evidence of where
it went. A reader of the exception alone would look for a protocol mismatch.

This is the general form of the trap this project keeps hitting — **an error
that is reported at the wrong layer costs more than the failure it describes**.
See also the 401 misfiled as a protocol failure under "found by deploying"
below.

## Finding 3: the three client SDKs are not the same kind of object

Ergonomics, not correctness, but it shapes what you can fix:

- **`agent-framework` `A2AAgent`** — `A2AAgent(name, url)`, `await .run(prompt)`,
  read `.text`. Two lines. Card resolution and transport selection are
  internal, which also means **there is no seam to patch when a server
  advertises a bad card** — this stack cannot express the finding-2 workaround.
- **`a2a-sdk`** — resolve the card, mutate it, build a client, iterate typed
  protobuf chunks, close it. Verbose, and the only stack low-level enough to
  work around both findings above.
- **`google-adk` `RemoteA2aAgent`** — a `BaseAgent` meant to sit inside an
  agent tree. Using it as a plain client means standing up a `Runner`, an
  `InMemorySessionService`, and a session per request. It is also ~30x slower
  than the other two against the same server (864ms vs 31ms/69ms), and still
  emits `[EXPERIMENTAL]` warnings on every call.

## Finding 4: `google-adk` 2.4.0 cannot serve A2A v1.0

`google-adk` 2.4.0 imports `a2a.server.apps.A2AStarletteApplication`, which
`a2a-sdk` 1.x removed. Installing current `a2a-sdk` alongside it produces a
`ModuleNotFoundError` at import of `to_a2a`. The first working pair was
**`google-adk` 2.5.0 + `a2a-sdk` 1.1.2**, and this repo pinned both.

**The pins are gone, and how they were removed is the more useful finding.**
Retested 2026-08-02: `to_a2a` imports and serves on `google-adk` 2.6.1, the
suite passes, and both pins came out of `pyproject.toml`, both Dockerfiles and
the README. `a2a-sdk==1.1.2` turned out never to have been load-bearing at all
— 1.1.2 was simply the latest release when the pin was written, so it had been
copied across four files as though it were a finding.

The conclusion originally drawn here, that "latest of each is not yet a safe
assumption", was true on a date and then read as a law. That is precisely how a
pin outlives the defect that justified it. Write the *measured failure* into the
comment, never the general warning — and re-test whenever the area is touched,
because a pin nobody re-tests is indistinguishable from rot.

## Found by deploying, not by testing

Both of these were caught within minutes of the first authenticated Cloud Run
call, by code that had a green 69-test suite. Neither was reachable locally.

**A 401 classified as a protocol failure.** `a2a-sdk` wraps the card fetch's
`httpx.HTTPStatusError` in its own `AgentCardResolutionError`, exposing the
original only as `__cause__`. `clients/base.py` caught the httpx type directly,
so a genuine auth denial was filed as `protocol` — the matrix pointing at the
wrong layer, which is the most expensive wrong answer this instrument can give.
Fixed by walking the exception chain; the message now names the failing URL,
because a 401 on `/.well-known/agent-card.json` (discovery is privileged) is a
different fix from a 401 on the message endpoint.

**A totally failed run exiting 0.** `MeshRun.succeeded` was `bool(results)`,
and there is always one result envelope per requested target whether or not
anything filled it. Harmless while it only drove a CLI exit code on a laptop.
Wrong the moment a Cloud Run job's exit status became the health signal: the
run where every participant 401'd reported green.

**A whole cloud wired in as an empty string (2026-08-08).** An expired AWS
session made `deploy_aws.sh env` emit `AWS_A2A_ENDPOINT=` and
`AWS_A2A_ROLE_ARN=` — and exit **0**. `deploy_gcp.sh wire` accepted them and
pushed both blanks into the live coordinator job, reporting success.

The mechanism is worth stating exactly, because it is invisible on inspection.
`env_block()` built the block as a heredoc with `$(runtime_url)` inline. A
command substitution is a subshell, so `runtime_url`'s `exit 1` terminated only
that subshell; `set -euo pipefail` never saw a failure, the heredoc printed the
name with nothing after the `=`, and the function returned 0. The guard in
`peer_env()` then checked that *some* assignment came back — which one had —
rather than that it had a value.

Three layers of silent success stacked over one absent cloud: the AWS script,
the wire step, and then the mesh itself, whose median absorbs a dead leg and
exits 0 by design. That last one is correct behaviour at runtime and exactly
wrong at deploy time, which is the distinction the fix draws.

Both ends now refuse. `env_block()` resolves into variables, validates them,
and returns non-zero; `peer_env()` rejects any empty-valued assignment, no
longer swallows the sibling's stderr (the explanation was being discarded), and
offers `ALLOW_PARTIAL_MESH=1` for the case where two clouds really is what you
want. `runtime_url()`'s message named only "runtime not deployed", which sent
this session hunting for a deleted runtime that was in fact `READY`; it now
names both causes and how to tell them apart.

This is the project's thesis a third time. No local test could have produced
it: it needs a real expired credential, a real sibling script, and a real
deployment target.

## `llm` mode had never run, and three defects explain why (2026-08-09)

All found on a laptop, before spending anything on a cloud. The pattern is the
one this document keeps recording: each failure reported success somewhere.

**GCP could not have worked.** `_llm_agent()` connected with
`StreamableHTTPConnectionParams` to `http://127.0.0.1:8081/mcp`, but
`mcp_server/server.py` is a stdio JSON-RPC server and there is no HTTP MCP
server anywhere in this repo. Nothing has ever listened on that port.

**And it failed as a warning.** ADK's `_MCP_GRACEFUL_ERROR_HANDLING` downgrades
`Failed to create MCP session: All connection attempts failed` to a `WARNING`,
so the agent started, answered `/health` with **200**, and served `llm` mode
with zero tools registered. An agent advertising a brain it does not have.

**The prompt named a tool that did not exist.** The shared `INSTRUCTION` in
`agents/common.py` says to use `get_exchange_rate`, which is exactly what the
AWS and Azure agents register natively. The MCP server offered only
`convert_currency`, with a different signature. Gemini emitted
`get_exchange_rate(...)` as instructed and ADK rejected it as
`UNEXPECTED_TOOL_CALL` — the prompt and the tool contract disagreeing, with
nothing in either component able to see the other.

Fixed by pointing GCP at `StdioConnectionParams` running
`python -m mcp_server.server` (the transport that exists, keeping the leg
genuinely "over MCP"), and by adding `get_exchange_rate(currency_from,
currency_to)` to the MCP server beside `convert_currency` — which stays,
because `coordinator/mcp_stdio.py` calls it by name.

| cloud | brain | result |
|---|---|---|
| GCP | Gemini via ADK + MCP | `0.92` EUR, `150.0` JPY, no MCP warnings |
| AWS | Nova via Strands | `0.92` EUR, `0.79` GBP, 1839ms |
| Azure | Foundry | **cannot run** — no Foundry project exists |

Also worth recording: ADK does not use application-default credentials for
Gemini. It wants `GOOGLE_GENAI_USE_VERTEXAI=true` with
`GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION`, or an API key; with neither it
returns "No API key was provided" **inside a task body with HTTP 200**, which
is its own instance of the pattern above.

None of this is deployed. `Dockerfile.aws` still omits `strands-agents`
deliberately, so the hosted AWS agent cannot serve `llm` mode as built, and no
hosted `llm` measurement exists.

## What is deliberately not claimed

- Latencies in the nine-cell matrix are local and direct-brain. They measure
  protocol and framework overhead, not cloud-to-cloud network time, and not
  model time.
- The rate values agree trivially: every agent reads the same fixture table.
  That is the point — see the note on Frankfurter in the README. Numeric
  consensus is exercised by fault injection, not by hoping three models
  disagree.
- **The GCP column is an in-cloud hop.** Coordinator and agent are both in
  `us-central1`, so that column measures Cloud Run to Cloud Run and must not be
  counted toward the interop claim the way the AWS and Azure columns can. This
  is no longer left to the reader: `matrix/runner.py` classifies every cell as
  `local`, `in-cloud` or `cross-cloud` from `CURRENCY_COORDINATOR_CLOUD`, marks
  the column `gcp*`, and prints the cross-cloud count on its own line. A hosted
  9/9 is six cells of interop and three of Cloud Run talking to itself. The
  marker is exercised locally only so far — the jobs have not been re-run since.
- **The deployed latencies are single executions**, cold or warm depending only
  on where in the run order a cell fell. There is no distribution behind any of
  them, and they should not be read as a comparison between clouds.
- **Nothing here measures a model.** Every agent is `direct`-brain, which is the
  point — a red cell is a protocol failure and never a model that wandered
  off-format. It also means these numbers say nothing about what an `llm`-mode
  mesh would cost, and the predecessor series measured 18.8–25.1s for that.
