# Strengthening this project

**Nothing in this document has been done.** It is a list of work that would
make the project's claims harder to dismiss, written down on 2026-08-09 after a
critical read of what the repo actually demonstrates versus what it says it
demonstrates. Everything here is a proposal; treat any claim below as unproven
until it appears in `docs/INTEROP.md` with a measurement behind it.

## Why this list exists

The project's defensible contribution is narrower than the README's framing.
The architecture is not novel — cross-cloud workload identity federation,
median consensus, and multi-cloud agent demos all exist. What is unusual is the
**off-diagonal**: running every vendor's client against every other vendor's
server. Vendors test their client against their own server, so interop defects
live precisely where nobody looks:

- **`to_a2a()` advertises the container's bind address**, so ADK's own client
  cannot reach ADK's own server once hosted — and both halves pass Google's
  tests. This is the finding, and it is genuinely off-diagonal.
- **AgentCore strips the `A2A-Version` header**, so `a2a-sdk` assumes `0.3` and
  rejects a request its own handler cannot serve. This is a *confirmation*, not
  a discovery — see the correction at the end of this document. The predecessor
  series had already identified the mechanism; what is new is that it
  reproduced on AgentCore with two control clouds forwarding the same header
  untouched, plus a fix.

Everything below exists to protect that pair from the objections that would
otherwise sink them — and to keep the second one described accurately, since
overstating it is the fastest way to lose the first.

## Two structural facts, verified 2026-08-09

These are the sharpest objections available to a hostile reader, and both are
true. They were checked in the code, not assumed.

**The client axis is one transport with three façades.** All three "independent
client stacks" resolve to the same library: `agent-framework-a2a` requires
`a2a-sdk>=1.0.0,<2` and `google-adk` requires `a2a-sdk>=0.3.4,<2`. There is one
wire implementation under all three.

**The server axis is two stacks, not three.** `agents/aws/server.py` and
`agents/azure/server.py` both call `build_app` from `agents/serving.py` — the
same Starlette app, the same `create_jsonrpc_routes`, the same card builder —
differing only in executor. Only the GCP leg, on ADK's `to_a2a()`, is a
genuinely separate serving stack. The proof is that the
`_AssumeCurrentProtocolVersion` middleware added to `serving.py` for the AWS
header bug applies identically to Azure.

So "3 client SDKs × 3 serving stacks" is closer to *three façades over one
transport × two HTTP stacks*. Nine cells is a presentation, not nine
independent experiments.

**This should be led with, not hidden.** Shared implementation on both ends
ought to make interop trivial. It did not: a platform still stripped a header
and a framework still advertised an unreachable address. That is a sharper
result than nine green cells, and it is only available to someone who states
the dependency honestly.

## The work, in priority order

### 1. A non-Python client

The single highest-value addition. A TypeScript or Go A2A client would make the
client axis genuinely independent for the first time; today it is decorative.

*Buys:* converts the strongest structural objection into a measured result.
Every cell it fills is a real second implementation of the protocol talking to
these servers.

*Costs:* a new client adapter behind the existing `clients/` interface, plus
whatever runtime it needs in the matrix image. The interface is already the
right shape — `QuoteSource.convert()` — so the seam exists.

*Done when:* the matrix has a fourth row from a non-Python stack, and its
results are reported separately from the three that share `a2a-sdk`.

### 2. Differentiate the Azure serving stack, or relabel the axis

Two options, and either is acceptable; the current state is not.

- Serve Azure through Agent Framework's own A2A server rather than
  `agents/serving.py`, making the server axis genuinely three-way; or
- Relabel the axis honestly as two serving stacks with three executors, and
  stop implying otherwise in the README and `docs/INTEROP.md`.

*Buys:* removes a claim a reviewer can falsify in one `grep`.

*Done when:* either the two servers no longer share `build_app`, or every
document describing the matrix says two.

### 3. File both findings upstream

Report the ADK bind-address card behaviour and the AgentCore header stripping to
their vendors.

*Buys:* the largest credibility gain per unit of effort. External
acknowledgement converts "one person's observation on one account" into a
verified defect. A vendor's own issue tracker is the independent verification
this project otherwise lacks entirely — the harness, the agents, and the
analysis currently share an author, so a systematic error would be invisible
from the inside.

*Done when:* both have issue links recorded in `docs/INTEROP.md` beside the
findings.

### 4. Move the header finding off N=1

Re-run the hosted matrix from a second AWS account, or a second region, or
both.

*Buys:* "AgentCore drops the header" is currently one configuration on one
date. A second environment makes it a property of the platform rather than of
this deployment.

*Done when:* the finding is recorded with two independent environments behind
it, or is explicitly narrowed to the one it was observed in.

### 5. Make deployed consensus non-vacuous

Every agent reads the same fixture table, so `3/3 clouds, agreed` is true by
construction. Disagreement is exercised only by fault injection in tests, which
means the *deployed* consensus result demonstrates nothing about consensus.

Give one cloud a genuinely independent rate source — a different provider, or
live rates on one leg only — so agreement is a measurement rather than an
identity.

*Buys:* the median is the reason a third cloud exists. Right now the deployed
mesh never tests it.

*Caveat:* this trades away the property that a red cell is unambiguously a
protocol failure. It should be a separate, clearly labelled run, not the
default.

## Framing changes, which cost nothing

**Publish a defect taxonomy and claim only the top tiers.** Of the defects found
by deploying, one is a protocol/discovery defect, one is platform-mediated, and
the rest are packaging and configuration — missing `strands-agents`, missing
`agent-framework-foundry`, missing `bedrock:InvokeModel`, `mcp` 2.0, blank env
vars, a `--cloud` flag left behind. Presented with equal weight, they invite a
reviewer to notice most are `pip install` problems and discount the entire set.
Separate them: *protocol* / *platform-mediated* / *packaging*, and rest the
thesis on the first two.

**Lead with the retraction.** Withdrawing the "`agent-framework` → AWS is
reproducibly ~5.7s, unexplained" claim — and stating that the evidence to
falsify it was already on the page, because the slow cell *moved between
clients* and a fixed per-client cost cannot move — is the most credible thing in
the repo. A project that publicly retracts its own finding earns the benefit of
the doubt on the ones it keeps.

**Stop implying the latency tables are comparative.** They are labelled, but
tables invite comparison regardless. The `llm` figures especially: single runs
against models that are not comparable to each other — Nova micro against a
reasoning model against Gemini Flash, one of them cross-region.

**Reframe the top-line claim.** Not "we built a three-cloud agent mesh" — that
is a demo, and demos are cheap. Rather: *here is a falsification apparatus for
interop claims, and it found defects that vendor test suites structurally
cannot.* That thesis survives every objection in this document.

## Not worth doing

- **More clouds.** A fourth vendor adds cells without addressing a single
  weakness above. Breadth is not the problem; independence is.
- **More `llm` measurement without a reason.** The models are not comparable,
  so more runs produce a tighter number that still means nothing. Repeat only
  to test a specific hypothesis.
- **Chasing the ADK-vs-ADK red cell.** It is interop finding 2, understood and
  documented. Fixing it would remove the project's clearest example of a defect
  that a vendor's own tests cannot see.

## The novelty caveat, now checked (2026-08-09)

The earlier draft of this document said someone should search for published
cross-vendor A2A conformance work before committing to a framing. That search
has been done — a basic one, not a systematic review — and the result is
recorded in `docs/ARTICLE_PLAN.md` under "Prior art".

Short version: **no third-party cross-vendor interop testing was found.** The
official `a2a-tck` validates one implementation against the *spec*, locally.
`a2a-inspector` validates a single agent. A2A issue #1755 measures reachability
across 50 advertised endpoints and found 0% answered a correct `tasks/send` —
breadth of availability, not depth of interop. The A2A roadmap lists expanded
testing as planned.

So items 1 and 2 above are **not** table stakes; the client-axis independence
problem is a real differentiator to fix rather than a box already ticked.

One correction this survey forced, and it matters more than the reassurance:
**the AgentCore header-stripping finding is not a discovery.** The predecessor
series had already identified a proxy stripping `A2A-Version`, and already
understood that the server defaults a missing header to 0.3 — it was written
into `docs/ARTICLE_PLAN.md` before this mesh existed. What is new is the
mechanism reproduced on AgentCore specifically, with two control clouds
forwarding the same header untouched, plus a fix. That is a confirmation with
controls, which is a good result and not the same thing. Any framing that
called it a discovery would be checked and found wanting.

The real prior art is this author's own predecessor series, which will be found
immediately by anyone assessing the work. Claim the delta and only the delta:
N×M rather than pairwise, three clouds reached simultaneously, three-way median
consensus, keyless on all three legs with per-leg controls, and the instrument
framing.
