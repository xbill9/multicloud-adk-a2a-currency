# Article plan

This repo has findings sets with genuinely different audiences. Splitting them
keeps each argument tight, and avoids one article where the identity material
buries the protocol material or vice versa.

Nothing gets written before the thing it describes is deployed and measured.
See `CLAUDE.md`.

## Tone and scope — read this first

The subject is strategy: cross-cloud auth, deployment topology, and the
scaffolding that supports both. Other material should generally stay out.

The repo's own working notes are deliberately exhaustive about small failures,
because that is what working notes are for. An article is not working notes. A
reader who came for "how do I make an agent in one cloud call an agent in
another without a stored secret" does not need to be walked through a missing
package or a build-cache miss, and printing that material has two costs: it
crowds out the findings, and it invites the reader to conclude the project is
mostly a catalogue of ordinary friction.

**In scope**

- Auth *strategy*: which runtime can mint, and how that single fact determines
  the entire topology and the secret count.
- Deployment *strategy*: what belongs where and why, what the choice costs, and
  what it forecloses.
- Scaffolding: the structures that made the rest tractable — one credential
  seam, one participant interface, the matrix as an instrument, negative
  controls, deploy verbs that are reproducible from the repo.
- Findings that a single-vendor test suite structurally cannot produce.

**Out of scope — do not write these up, do not reference them**

- Package and dependency management: missing extras, version pins, transitive
  incompatibilities, install layering. Version *skew as a protocol
  phenomenon* stays; the `pip` mechanics do not.
- Repository hygiene of every kind: ignore files, what was or was not
  committed, recovered work, test counts, lost coverage. None of it belongs in
  an article and some of it actively misleads about what the project is.
- Build and toolchain friction: emulation, architectures, cache behaviour,
  local-versus-container environment differences.
- Ordinary configuration mistakes: an env var set in the wrong place, a flag
  left behind, a role missing a permission. These are how software is built,
  not what was learned.
- The author's process — what was tried, in what order, what went wrong on the
  way. State the finding, not the search.

**A rough test.** Before including anything, ask: *would this have happened on
a single-cloud project in any language?* If so, it is probably ordinary
development friction and can come out.

**The one deliberate exception** is a failure whose *shape* is the point — the
recurring pattern where a broken thing reports success. A whole cloud
configured as an empty string and exiting 0, a run reporting `1/1 clouds` while
three were wired, an agent serving with no tools behind a healthy `/health`.
The pattern says something about observability in distributed systems that the
individual bugs do not, so the pattern is worth including and the bugs are
not.

---

## Article A — Auth: "three clouds, zero secrets"

**Audience:** platform and infrastructure engineers. People who will have to
make an agent in one cloud call an agent in another and are about to reach for
a service-account key.

**Thesis:** cross-cloud agent auth is an identity project, not a protocol
problem — and with the coordinator on the right runtime, a three-cloud mesh can
run with **no long-lived secrets at all**.

**Spine:**

1. The asymmetry that decides everything: every callee here consumes external
   OIDC (AWS IAM OIDC providers, Entra Federated Identity Credentials,
   AgentCore `CUSTOM_JWT`); only some *callers* can mint an OIDC token. That
   single fact picks your coordinator's host.
2. Why the coordinator runs on Cloud Run, with the alternatives costed:
   AgentCore or Foundry as host gives up the secretless property because
   neither runtime's minting ability is confirmed.
3. The three legs and their mechanisms — GCP→AWS (STS
   `AssumeRoleWithWebIdentity` → SigV4), GCP→Azure (Entra FIC), GCP→GCP (ID
   token, `roles/run.invoker`).
4. The traps, each of which looks like correct configuration:
   - `accounts.google.com:oaud` is the token's `aud`; `:aud` is its `azp`
   - AWS federates with Google natively, so creating an explicit IAM OIDC
     provider *breaks* it — but for Entra you must create one
   - audience is caller-chosen, so audience alone is not authorization
   - `format=full` on the metadata mint
   - Foundry's incoming A2A takes Entra and only Entra
5. Diagnostics, and why this is the real cost: `InvalidIdentityToken` vs
   `AccessDenied` as the fastest discriminator; data-plane denials missing from
   CloudTrail; and the corollary that an error returned as a *tool result* gets
   paraphrased by the model in the middle, so raised messages are not
   observables — log at the boundary.
6. What it costs in latency, measured.

**Must not over-claim:** whether AgentCore Runtime can mint a workload OIDC
token is unresolved. If it cannot, "secretless" is a property of *this*
topology, not of cross-cloud agents generally. Say so.

---

## Article B — Interop: "nine client/server pairs"

**Audience:** people building on ADK, Strands, or Agent Framework who assume
"speaks A2A" means "interoperates."

**Thesis:** "speaks A2A" does not mean "interoperates," and the defects live
exactly where no vendor looks — because each vendor tests its client against
its own server, and nobody owns two.

**Spine:**

1. The 3×3 matrix as the instrument: three client SDKs × three natively-served
   agents, every cell a real A2A call, failures typed by layer rather than just
   red. **Lead with the honest dependency**: all three client stacks resolve to
   the same `a2a-sdk` underneath, and two of the three servers share a serving
   stack. That makes the result stronger, not weaker — shared implementation on
   both ends should make interop trivial, and it did not.
2. The finding that carries the article, and it is off-diagonal:
   **ADK's `to_a2a()` advertises its bind address**, so ADK's own client cannot
   reach ADK's own server once hosted, and both halves pass Google's tests
   because locally the two addresses are identical. This is the clearest
   example in the series of a defect a vendor's own suite cannot produce.
3. **Header stripping, stated honestly as a confirmation rather than a
   discovery.** The predecessor series already identified a proxy silently
   removing `A2A-Version`, and already understood that the server then defaults
   a *missing* header to 0.3 and makes a transport bug look like an old client.
   That was written into this plan before the current mesh existed. What is new
   here is narrower and should be claimed as such: the mechanism reproduced on
   **AgentCore specifically**, with two control clouds forwarding the same
   header untouched, and a fix — assume the current version when the header is
   absent, since absent is not evidence of an old client. A prediction that
   later reproduces with controls beside it is a good result; it is not a
   discovery, and calling it one would be the first thing a reader checks.
4. Supporting findings from the series: a completed Task carries the answer in
   a different field per vendor; the three client SDKs are not the same kind of
   object. Version skew as a *protocol* phenomenon only.
5. **Latency is easy to misattribute, and this project got it wrong first.**
   An AgentCore session id maps to a microVM, so a per-*call* cold start
   presented as a fixed per-*client* cost. The retraction belongs in the
   article: the evidence to falsify it was on the page all along, because the
   slow cell moved between clients and a fixed per-client cost cannot move.
6. Why N-way median consensus beats a primary/verifier pair: a privileged
   source is an unrecoverable single failure; a median means one divergent
   cloud cannot move the answer. Backed by the fault-injection tests.
7. Why the rates are deterministic by default — consensus across correlated
   sources measures nothing.

**Must not over-claim:** the current matrix numbers are local and
`direct`-brain. They are protocol measurements, not performance results.

---

## Article C — Deployment and scaffolding: "what makes the second one cheap"

**Audience:** the engineer who has one agent working on one cloud and has been
asked to make it three. The person who will otherwise spend their time on the
same problems this project spent its time on.

**Thesis:** in a cross-cloud mesh the expensive decisions are made before any
protocol is spoken — where the coordinator runs, what the credential seam looks
like, and whether a failure can be told apart from a success. Get those three
right and adding a cloud is a day; get them wrong and every cloud is a project.

**Spine:**

1. **Hosting decides the auth bill.** The same table as Article A, from the
   other side: the coordinator's runtime is not a deployment detail, it is the
   choice that determines how many long-lived secrets the whole system needs.
   Cross-reference A rather than re-arguing it.
2. **One credential seam, three mechanisms.** `coordinator/auth.py` puts a
   Google ID token, an STS `AssumeRoleWithWebIdentity` → SigV4 exchange, and an
   Entra federated exchange behind a single `httpx.Auth`, attached to the client
   so the agent-card fetch carries the same credential as the call. The general
   claim: build the seam before the first deployed peer, not after the third.
3. **One participant interface.** `QuoteSource.convert()` and the client
   registry mean a cloud is an implementation, not a branch. This is what makes
   the matrix expressible at all.
4. **The instrument is the deliverable.** A 3×N grid where each cell is a real
   call and each failure is typed by layer — transport, protocol, timeout,
   authentication, provider — rather than merely red. Typing the failure is the
   difference between "the mesh is broken" and "discovery is 403ing on one leg."
5. **Negative controls, and why a green run proves less than it looks.** The
   median degrades on purpose, so a three-cloud run with one credential removed
   still reaches quorum and exits 0 — which reads as "no denial" and is not.
   Every leg is therefore probed alone. This generalises: any system with
   graceful degradation needs its controls scoped to one component, or the
   degradation hides the very failure you are testing for.
6. **Reproducible-from-the-repo deployment.** Verbs rather than runbooks:
   `deploy`, `wire`, `verify`, `foundry`. The identifiers for each cloud live in
   exactly one place — the script that created them — so a redeployed runtime
   whose URL contains its own ARN cannot leave a stale copy behind.
7. **The failure shape worth generalising:** broken things that report success.
   Present it as a design claim — in a distributed mesh the exit code is not the
   signal, and anything that can degrade must say what it degraded to.

**Must not over-claim:** this is one topology deployed once by one operator. The
scaffolding claims are architectural arguments supported by an example, not
measurements. Do not dress them as findings.

**Tone note:** this is the article most at risk of turning into a changelog.
Every item above is a *decision and its consequence*. If a paragraph is
describing something that went wrong rather than something that was decided,
it belongs in the repo, not here.

---

## Drafted

`docs/article-cross-cloud-auth.md` is a first draft merging **A** and **C** —
auth strategy, deployment strategy and scaffolding for one audience, since they
share a reader and a spine. Article **B**, the interop findings, is not written
and should stay separate.

## Splitting the overlap deliberately

Three items could plausibly land in either article. Assign once, reference
across, do not write twice:

| Item | Goes in | Why |
|---|---|---|
| `to_a2a()` advertises bind address | **B** | it is a card/discovery defect; auth is incidental |
| Version skew, all three mechanisms | **B** | protocol behavior |
| Agent-card fetch requiring a token | **A** | the point is that discovery is privileged |
| Remote runtime dominating latency | **B** | with a pointer from A |

Each article should link the other once, in the first two paragraphs, so a
reader who came for the wrong one leaves with the right one.

## Prior art — surveyed 2026-08-09

A basic web search, not a systematic review, but enough to place the work.

**Third-party cross-vendor A2A interop testing: none found.** The nearest
things are adjacent rather than overlapping:

| Work | What it does | Why it is not this |
|---|---|---|
| [`a2aproject/a2a-tck`](https://github.com/a2aproject/a2a-tck) | Official TCK. Validates one implementation against the spec across gRPC/JSON-RPC/HTTP+JSON, with MUST/SHOULD/MAY filtering | One system under test, against the *spec*, run locally (`--sut-host http://localhost:9999`). Not vendor-A-client against vendor-B-server, and not deployed |
| [`a2aproject/a2a-inspector`](https://github.com/a2aproject/a2a-inspector) | Validation tooling for a single agent | Single-agent validation, not a pairing |
| [A2A issue #1755](https://github.com/a2aproject/A2A/issues/1755) | Field study: 50 agents advertising A2A. 100% advertised, **0% answered a correct `tasks/send`** | Reachability and endpoint conformance at breadth. Says nothing about whether two working implementations interoperate |

The A2A roadmap lists expanded testing and tooling as *planned*, which is
consistent with a conformance suite existing and an interop matrix not.

**The real prior art is this author's own predecessor series**, and it should be
cited generously rather than tiptoed around:
[xbill9/cross-cloud-a2a-rollup](https://github.com/xbill9/cross-cloud-a2a-rollup),
plus the published write-ups of the six directed edges. That series already
covered protocol versions, dependency extras, identity federation, header
forwarding and URL shapes. Anyone assessing novelty will find it immediately,
so claim the delta and only the delta.

**What is actually new here, against that series:**

- **N×M rather than pairwise.** The series measured six directed edges one at a
  time. This runs every client stack against every server stack, and reaches
  three clouds *simultaneously* from one coordinator.
- **Three-way median consensus** with per-participant failure isolation,
  replacing a primary/verifier pair — and the argument for why a privileged
  source is the wrong shape.
- **Keyless on all three legs at once**, with negative controls per leg rather
  than a self-reported mode.
- **The instrument framing itself**: typed failures, the in-cloud hop excluded
  from the interop count, warm/cold labelled, and a published retraction.

Article A is the natural sequel to the series' identity section; Article B to
its version-skew section; Article C is the one with no predecessor.

**Framing consequence.** With no third-party cross-vendor interop work found,
the honest claim is *"the first cross-vendor A2A interop matrix I can find"* —
hedged that way, with the survey shown. That is both stronger and safer than an
unqualified first, which one counterexample would destroy along with the
reader's trust in everything else.
