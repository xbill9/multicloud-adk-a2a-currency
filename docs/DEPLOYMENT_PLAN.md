# Deployment plan

Current state: three native agents, a 3×3 interop matrix passing 9/9, N-way
median consensus, 45 tests — and **nothing deployed**. Everything measured so
far is local with `direct` brains.

Work in this order. Each step's output is what makes the next step meaningful.

## 1. Decide hosting, because it decides the auth bill

The coordinator is the only component that mints credentials, so its host
determines how much identity work exists:

| Coordinator host | Outbound legs | Long-lived secrets |
|---|---|---|
| **Cloud Run** | GCP→AWS, GCP→Azure, GCP→GCP | potentially **zero** |
| AgentCore | AWS→Azure, AWS→GCP | ≥1; AWS→Azure likely unavoidable |
| Foundry | Azure→AWS, Azure→GCP | 1–2, both unproven |

**Decision: Cloud Run.** It is the only runtime proven to mint workload OIDC
tokens with an arbitrary audience, which is what made GCP→AWS keyless in
`adk-bedrock-a2a-currency`.

## 2. Put the auth seam in before the first deployed peer — **done, unexercised**

`coordinator/auth.py`: `credentials_for(peer, endpoint) -> httpx.Auth | None`,
re-exported from `coordinator/participants.py` because that is the interface it
hangs off.

`httpx.Auth` is the shape, because it is the only one that spans both a bearer
header and a signature over the request body — and all three vendor client SDKs
accept an `httpx.AsyncClient`, so one seam covers all three matrix rows. The
credential is attached to the *client*, not the request, which means **the
agent-card fetch carries it too**. Discovery is privileged on all three clouds
and a card fetch that 403s while the call would have succeeded surfaces as a
protocol error, nowhere near auth.

No vendor SDK is imported: httpx plus the standard library, including the SigV4
signer. The coordinator reaches three clouds; making it carry three clouds' auth
libraries to do so would be the wrong trade.

Configuration is per-peer and environmental, so the same image runs the local
mesh (every peer `none`) and the deployed mesh without a code change — which is
what keeps the local matrix a protocol instrument rather than an identity test:

```
GCP_A2A_AUTH=google-id-token   [GCP_A2A_AUDIENCE=<defaults to service root>]
AWS_A2A_AUTH=aws-sigv4         AWS_A2A_ROLE_ARN=…  AWS_A2A_REGION=…
                               [AWS_A2A_AUDIENCE=sts.amazonaws.com]
                               [AWS_A2A_SIGNING_SERVICE=bedrock-agentcore]
AZURE_A2A_AUTH=entra-fic       AZURE_A2A_TENANT_ID=…  AZURE_A2A_CLIENT_ID=…
                               [AZURE_A2A_SCOPE=<client-id>/.default]
```

The mode actually used is recorded per leg in `MeshRun.auth_modes` and per cell
in the matrix report, rather than inferred from config afterwards — "which legs
were keyless" is a claim the artifact has to be able to back on its own, and a
leg that silently fell back to an unauthenticated call must not be mistakable
for a federated one.

**Raw provider response logged at every boundary**, whole and unparsed, on
failure. Both discriminators are carried through to the caller: STS
`InvalidIdentityToken` arrives with the "no IAM OIDC provider for
accounts.google.com / check `format=full`" hint, `AccessDenied` with the
`:oaud` vs `:aud` one, and Entra failures surface their AADSTS code.

**Status: 24 hermetic tests, no cloud.** They assert the mint sends
`format=full`, that the signature covers the body, that the signing key matches
AWS's published vector, and that each denial names the right layer. What they
cannot assert is that any real provider accepts any of it — **not one token has
been minted against a real endpoint.** Code-complete with a green suite is not
a result; that is what step 3 is for.

## 3. Deploy one leg at a time, cheapest-proof-first

1. **GCP→GCP** — ID token, `roles/run.invoker`. Proves the seam works with the
   least moving parts. **Done, 2026-07-31.** `./infra/deploy_gcp.sh` — see
   "What the first leg actually proved" below.
2. **GCP→AWS** — metadata mint → STS `AssumeRoleWithWebIdentity` → SigV4.
   Mechanism already proven elsewhere; port it rather than reinvent.
   **Done, 2026-08-02.** `./infra/deploy_aws.sh` — AgentCore Runtime
   `currency_aws`, `us-west-2`, SigV4 (no `--authorizer-configuration`), role
   `currency-aws-federated` trusting `accounts.google.com`.
3. **GCP→Azure** — Entra Federated Identity Credential trusting
   `accounts.google.com`, subject = the SA's unique ID. This is the unproven
   one and the one worth writing up. **Done, 2026-08-02.**
   `./infra/deploy_azure.sh` — Container App `currency-azure`, `westus2`, FIC
   subject `101913873674028276612`, plus the enforcement half described below.

All three legs were then exercised together, keyless, on 2026-08-07: see "The
whole mesh, deployed" and "What the controls actually proved".

## What the first leg actually proved (2026-07-31)

Deployed in `aisprint-491218` / `us-central1`:

| Resource | What it is |
|---|---|
| service `currency-gcp` | the ADK agent, `--no-allow-unauthenticated` |
| job `currency-coordinator` | the CLI, SA `currency-coordinator@`, `GCP_A2A_AUTH=google-id-token` |
| job `currency-matrix` | the matrix, one server column |

The coordinator runs **as a Cloud Run job**, not locally, and that is not a
convenience. A user credential cannot mint an arbitrary-audience ID token at
all — `gcloud auth print-identity-token --audiences=<url>` fails outright with
*"Invalid account type for `--audiences`. Requires valid service account."* The
laptop cannot exercise this path. That is the sharpest available statement of
why the coordinator's host is the decision that sets the whole auth bill.

**Positive:** `100 USD = 92 EUR`, one cloud, `643ms`. Auth mode reported as
`google-id-token` in the run envelope.

**Negative controls** — an authenticated leg is unproven without them:

| probe | result |
|---|---|
| no token | **403** on both `/health` and the agent card |
| workload token, deliberately wrong audience | **401** on the card fetch, exit 1 |
| user token, audience = gcloud's own OAuth client ID | **200** |

That last row is worth keeping. Cloud Run accepted an ID token whose audience
was `32555940559.apps.googleusercontent.com` — not this service's URL — because
for an interactive user principal it is IAM that authorizes, and the audience
check is not what closes the door. It is the same lesson as the AWS `:oaud`
trap from the other side: **audience is caller-chosen, so audience alone is
never authorization.** The binding is what authorizes.

**Latency.** 643–731ms coordinator→agent, both in `us-central1`, against 66ms
for the identical code locally. The predecessor series predicted 1.7–2.1s to a
Cloud Run container; this is well inside that. Note this is an **in-cloud hop**
— both ends are GCP — so it belongs in the matrix labelled as such and must not
pad the interop claim.

**Two defects, neither catchable locally**, both found within minutes of the
first authenticated call by code with a green 69-test suite: a 401 misfiled as
a protocol failure, and a totally failed run exiting 0. Written up under "Found
by deploying" in `docs/INTEROP.md`, along with the confirmation that finding 2
reproduces — and that the client it breaks is ADK's own.

This is the project's thesis reproducing on schedule. Code-complete plus a
green suite was, again, not a result.

## The whole mesh, deployed (2026-08-07)

Three clouds, three vendors' hosting, one coordinator, no stored secret:

```console
$ ./infra/deploy_gcp.sh run

participants: gcp (google-id-token), aws (aws-sigv4), azure (entra-fic)
100 USD = 92 EUR @ 0.92 [3/3 clouds, agreed]
    gcp                  92 (15520ms)
    aws                  92 (1116ms)
    azure                92 (24127ms)
100 USD = 15000 JPY @ 150 [3/3 clouds, agreed]
elapsed 25012ms
```

Everything scales to zero, so that run is three simultaneous cold starts. The
warm shape, from 2026-08-03: `gcp 1006ms, aws 902ms, azure 476ms, elapsed
1770ms`. **Consensus latency is ≈ max(legs), not their sum** — 1770 against a
1006ms slowest leg — which confirms the coordinator issues all three
concurrently rather than assuming it. Cold, the same relation holds: 25012
against 24127.

The Azure leg is cold on *every* run an hour apart — 24127ms, then 21848ms —
because the deployed Container App sits at `minReplicas: 0` while
`deploy_azure.sh` writes `--min-replicas 1`. That drift is the whole
explanation for the slowest column in both tables, and it is configuration, not
Container Apps being twenty seconds slower than the other two clouds. Either
fix the deployed app or stop quoting cold Azure numbers; do not do neither.

The deployed 3×3 matrix is in [`INTEROP.md`](INTEROP.md#the-same-matrix-deployed-2026-08-07):
**8/9**, the single red cell being finding 2, still ADK's own client against
ADK's own server.

## What the controls actually proved (2026-08-07)

`./infra/deploy_gcp.sh verify`. Every probe runs one leg alone — the mesh
degrades on purpose, so a three-cloud run with one credential removed still
reaches quorum and exits 0, which reads as "no denial" and is not.

| probe | leg | result |
|---|---|---|
| unauthenticated `curl`, `/health` and card | gcp | **403** |
| as deployed | gcp | answered |
| as deployed | aws | answered |
| as deployed | azure | answered |
| `GCP_A2A_AUTH=none` | gcp | **denied** |
| `AWS_A2A_AUTH=none` | aws | **denied** |
| `AZURE_A2A_AUTH=none` | azure | **denied** |
| right identity, `GCP_A2A_AUDIENCE` pointed elsewhere | gcp | **denied** |

Seven for seven. Three positive controls first, because a denial means nothing
until you know the leg answers at all — and the positive controls run through
the same job, same image, same env, with one variable changed by an
execution-time override rather than a redeploy. A control that tests a
configuration nothing else ever runs is not a control.

**This is the claim the mesh could not previously make.** Before these, the run
envelope's `gcp (google-id-token), aws (aws-sigv4), azure (entra-fic)` recorded
only that a credential had been *sent*. Two of the three endpoints could have
been answering anyone, and on Azure that was briefly true — see "The Azure
trap" above.

The wrong-audience row still proves less than it appears to. Audience is
caller-chosen, and Cloud Run has already been observed accepting a user token
whose audience was gcloud's own OAuth client ID. What it does separate is "the
token was rejected" from "no token was sent", which is worth having.

Not proven by any of this: that the AWS role's trust policy is *scoped* the way
`deploy_aws.sh` writes it. The role was deployed with `Resource` limited to the
runtime ARN and `ARN/*`, and every AWS cell including the card fetch succeeds —
which would answer open question 2 in the affirmative and contradict the
predecessor finding. But the deployed policy has not been read back since, and
until it is, the possibility that it was widened to `"*"` during the 2026-08-02
session is open. `aws iam get-role-policy --role-name currency-aws-federated
--policy-name invoke-currency-agent` settles it in one command.

## 4. Re-run the matrix hosted, and expect it to move

Current cells are 7–864 ms because everything is local and `direct`-brain. The
predecessor series measured 1.7–2.1 s to a Cloud Run container and 18.8–25.1 s
to either hosted agent runtime. **That is not a regression** and the article
must not read like one — the A2A leg tracks the remote's model and runtime, not
the protocol.

Consensus latency should land at ≈ max(legs), not the sum, provided the
coordinator issues all three concurrently. Verify that rather than assume it.

## Keep the two axes separate

There are two orthogonal axes — local↔hosted and `direct`↔`llm` — so four
combinations, and only one is the headline.

**Keep `direct` as the matrix brain even after deploying.** The 3×3 grid is a
protocol instrument; its value is that a red cell is unambiguously a protocol
failure. Run `llm` only for the consensus demo, where model divergence is the
point. Otherwise a throttled Bedrock call turns an interop cell red and costs
an evening debugging A2A that is not broken.

## Open questions

1. **Can AgentCore Runtime mint a workload OIDC token?** If yes, a fully
   secretless mesh is reachable from any host and the result generalizes. If
   no, "zero secrets" is a property of the Cloud-Run-hosted topology
   specifically, and the article must scope the claim that way. Unresolved.
2. **What ARN shape does AgentCore authorise `InvokeAgentRuntime` against?**
   Scoping to `runtime/<id>` and `runtime/<id>/*` was denied 403 on the
   agent-card fetch in `adk-bedrock-a2a-currency`; only `Resource: "*"` worked,
   and data-plane denials do not reach CloudTrail by default. Any deployment
   here that touches AgentCore inherits this as unfinished business — and
   shipping `Resource: "*"` must be disclosed, not glossed.
3. ~~**Does Entra FIC match Google tokens on `sub` or `azp`?**~~ **Answered,
   2026-08-02.** `sub`, and it means what it says. The FIC's `subject` is the
   coordinator SA's numeric unique ID (`101913873674028276612`) and the
   exchange succeeds; there is no Entra equivalent of the AWS `:aud`/`azp`
   trap. Its `audiences` field is likewise literal, but is not a choice —
   `api://AzureADTokenExchange` is the only value Entra accepts there.

   The trap on this side turned out to be somewhere else entirely: see below.

## The Azure trap: a FIC is half a control

The AWS and GCP legs are authenticated by the thing that receives the call —
IAM authorizes `InvokeAgentRuntime`, Cloud Run authorizes `run.invoker`. On
Container Apps the ingress is **public by default**, and nothing about creating
a Federated Identity Credential changes that.

So the first version of this leg had a working FIC, a coordinator presenting a
correctly-exchanged Entra token, `entra-fic` printed in the run envelope — and
an endpoint that would have answered a stranger with curl just as happily. The
deploy script said so in its own `verify` output and it was still easy to read
the green run as proof of an identity story it was not testing.

The two halves are separate and both are load-bearing:

- **`deploy_azure.sh fic`** decides *who can obtain* a token for this app. It is
  where the binding lives: subject = one numeric principal.
- **`deploy_azure.sh auth`** decides whether the app *demands* one. Container
  Apps' built-in auth, issuer and audience both pinned,
  `unauthenticatedClientAction: Return401`.

`Return401` rather than the default `RedirectToLoginPage` matters more than it
looks: a 302 to an interactive sign-in page arrives at an A2A client as a 200
carrying HTML, which it reports as a parse failure. That is this project's
recurring trap again — **an error reported at the wrong layer** — and it would
have sent a reader looking for a protocol bug in a leg whose only problem was
that it was not logged in.

The generalisation, which is the part worth keeping: **an auth mode reported by
the caller is a claim about the caller.** It says a credential was sent, never
that one was required. Only a negative control can tell those apart, which is
why `./infra/deploy_gcp.sh verify` exists and why every probe in it isolates a
single leg.
