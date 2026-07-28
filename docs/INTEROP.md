# A2A interop findings

Every finding here came from a real call between two vendors' stacks, not from
reading specifications. Run `python -m matrix.runner` to reproduce.

## The matrix

Nine directed calls: three client SDKs against three natively-served agents.
Latencies are local, direct-brain (no model), and measure protocol overhead
only.

| client \ server | GCP (ADK `to_a2a`) | AWS (a2a-sdk routes) | Azure (AF `A2AExecutor`) |
|---|---|---|---|
| `a2a-sdk` 1.1.2 | ok 69ms | ok 9ms | ok 10ms \* |
| `agent-framework` `A2AAgent` | ok 31ms | ok 7ms | ok 8ms |
| `google-adk` `RemoteA2aAgent` | ok 864ms | ok 10ms | ok 11ms |

\* only after the fix in finding 2.

All nine cells pass — but two of them only after working around a defect, and
neither defect was visible from either vendor's own documentation or tests.

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
`ModuleNotFoundError` at import of `to_a2a`. The working pair is **`google-adk`
2.5.0 + `a2a-sdk` 1.1.2**, pinned in this repo's venv. A2A v1.0 is recent
enough that "latest of each" is not yet a safe assumption.

## What is deliberately not claimed

- Latencies are local and direct-brain. They measure protocol and framework
  overhead, not cloud-to-cloud network time, and not model time.
- The rate values agree trivially: every agent reads the same fixture table.
  That is the point — see the note on Frankfurter in the README. Numeric
  consensus is exercised by fault injection, not by hoping three models
  disagree.
- No cell has been run against a deployed agent yet. All three are local.
