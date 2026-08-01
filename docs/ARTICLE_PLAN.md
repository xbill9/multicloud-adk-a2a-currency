# Article plan: two articles, not one

This repo has two findings sets with genuinely different audiences. Splitting
them keeps each argument tight, and avoids one article where the identity
material buries the protocol material or vice versa.

Neither article gets written before the thing it describes is deployed and
measured. See `CLAUDE.md`.

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

**Thesis:** all nine client/server pairs work, but two only after working
around defects neither vendor's own tests would catch — because each vendor
tests its client against its own server.

**Spine:**

1. The 3×3 matrix as the instrument: three client SDKs × three natively-served
   agents, every cell a real A2A call, failures typed by layer rather than just
   red.
2. The four findings already in `INTEROP.md`:
   - a completed Task carries the answer in a different field per vendor
   - ADK's `to_a2a()` advertises its bind address
   - the three client SDKs are not the same kind of object
   - `google-adk` 2.4.0 cannot serve A2A v1.0
3. Version skew as the recurring theme, with the three distinct mechanisms
   observed across this series: a transitive dependency pin, a framework
   `[a2a]` extra, and a proxy silently stripping the `A2A-Version` header
   (where the server then defaults a *missing* header to v0.3, making a
   transport bug look like an old client).
4. Why N-way median consensus beats a primary/verifier pair: a privileged
   source is an unrecoverable single failure; a median means one divergent
   cloud cannot move the answer. Backed by the fault-injection tests.
5. Why the rates are deterministic by default — consensus across correlated
   sources measures nothing.

**Must not over-claim:** the current matrix numbers are local and
`direct`-brain. They are protocol measurements, not performance results.

---

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

## Prior art to reference

The six-edge predecessor series, all deployed and measured as of 2026-07-31:
[xbill9/cross-cloud-a2a-rollup](https://github.com/xbill9/cross-cloud-a2a-rollup).
Article A is the natural sequel to its identity section; Article B is the
natural sequel to its version-skew section.
