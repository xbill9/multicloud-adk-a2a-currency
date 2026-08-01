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
3. **GCP→Azure** — Entra Federated Identity Credential trusting
   `accounts.google.com`, subject = the SA's unique ID. This is the unproven
   one and the one worth writing up.

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
3. **Does Entra FIC match Google tokens on `sub` or `azp`?** The AWS side had
   exactly this trap (`:aud` silently meaning `azp`). Check before assuming the
   mirror works the obvious way.
