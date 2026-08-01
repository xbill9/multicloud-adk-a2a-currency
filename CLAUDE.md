# Working notes for this repo

## Ground rule: deploy, then document

Do not write article or results content for a path that has not been deployed
and exercised end to end. Code-complete plus a green local suite is not a
result. This project's own history is the argument: in the six-edge predecessor
series, the last build was code-complete with a passing suite for a day, and
deploying it surfaced six defects — five of which no local test could have
caught.

The README's "Not done: **Nothing is deployed**" is the honest state. Keep it
honest until it changes.

## Cross-cloud auth: the plan

Every callee here consumes external OIDC — AWS IAM OIDC providers, Entra
Federated Identity Credentials, AgentCore `CUSTOM_JWT`. The asymmetry is
whether the *calling* runtime can mint an OIDC token.

**Run the coordinator on Cloud Run.** It is the only runtime proven to mint
workload OIDC tokens with an arbitrary audience, which makes every outbound leg
a candidate for keyless federation:

| Leg | Mechanism | Status |
|---|---|---|
| GCP → AWS | metadata mint → STS `AssumeRoleWithWebIdentity` → SigV4 | proven in `adk-bedrock-a2a-currency` |
| GCP → Azure | Entra Federated Identity Credential on `accounts.google.com` | proposed, not yet deployed |
| GCP → GCP | Google ID token, `roles/run.invoker` | trivial |

Target: **no long-lived secrets anywhere in the mesh.** Hosting the coordinator
on AgentCore or Foundry instead gives up that property, because neither
runtime's OIDC-minting ability has been confirmed.

### Constraints that cost real time before

- **Audience alone is not authorization.** Audience is caller-chosen, so an
  audience-only condition proves only that *some* identity in that IdP minted
  the token. Always also pin the subject, using the immutable numeric ID rather
  than an email, which can be freed and re-bound.
- **AWS federates with `accounts.google.com` natively.** Creating an explicit
  IAM OIDC provider for it *breaks* federation (`InvalidIdentityToken`). For
  Entra you must create one. Opposite rules, same-looking task.
- **The IAM condition keys do not mean what they are named.**
  `accounts.google.com:oaud` is the token's `aud`; `accounts.google.com:aud` is
  the token's `azp` (a number). Putting an audience string in `:aud` can never
  match.
- **`format=full`** on the GCP metadata mint, or Google trims the token and
  omits the `email` claim.
- **Foundry's incoming A2A accepts Entra and only Entra** — no custom issuer.
- **Diagnostic:** `InvalidIdentityToken` means the token could not be validated
  at all; `AccessDenied` means your trust conditions did not match. That
  distinction separates a provider-setup bug from a condition bug.

### Where auth belongs in this codebase

`coordinator/participants.py` currently has no credential concept —
`QuoteSource` is just `convert()`. Add a single shared auth adapter behind that
interface (`credentials_for(peer) -> auth`) **before** wiring the first
deployed peer, with three implementations behind one shape.

**Log the raw provider response at every auth boundary.** This is worth more
than the federation work itself. In the predecessor series nothing cost more
time than unreadable auth errors: an adapter that reported only an HTTP status
and discarded the STS body, and an error string that travelled back as a tool
result and got paraphrased by the model into "an issue with the web identity
token." Raise *and* log; the raised message is not an observable.

## Topology

The N-way median consensus in `coordinator/consensus.py` is the right design —
keep it. A primary/verifier pair reintroduces a privileged source whose failure
is unrecoverable; the median means one divergent cloud cannot move the answer.

If the coordinator and the GCP agent both run in GCP, that leg is an **in-cloud
hop, not cross-cloud**. Label it as such in the matrix rather than letting it
pad the interop claim.

## Evidence from the predecessor series

Six directed edges between Bedrock AgentCore, Microsoft Foundry, and Google ADK,
all deployed and measured as of 2026-07-31. Summary report:
[xbill9/cross-cloud-a2a-rollup](https://github.com/xbill9/cross-cloud-a2a-rollup).

Relevant measured results:

- The A2A leg tracks the *remote's model and runtime*, not the protocol or the
  distance: 1.7–2.1 s to a Cloud Run container, 18.8–25.1 s to either hosted
  agent runtime. Expect the same shape here once agents are hosted — the
  current single-digit-millisecond matrix numbers are local, direct-brain.
- Verified/consensus latency ≈ max(legs), not the sum, when calls are issued
  concurrently.
- Open question nobody has answered: on ADK → AgentCore, scoping
  `bedrock-agentcore:InvokeAgentRuntime` to `runtime/<id>` and `runtime/<id>/*`
  was denied 403 on the agent-card fetch; only `Resource: "*"` worked. If this
  repo deploys against AgentCore, that is unfinished business.
