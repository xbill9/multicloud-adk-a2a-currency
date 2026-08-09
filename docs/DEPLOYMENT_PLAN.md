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
pad the interop claim. The runner now does it: `CURRENCY_COORDINATOR_CLOUD=gcp`
is set on both jobs by `deploy_gcp.sh`, and the marker counts the cross-cloud
cells separately rather than reporting a bare 9/9. Verified against the local
mesh with the variable set; **the jobs have not been redeployed or re-run since
the change**, so no hosted table has yet been printed with the marker on it.

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

Everything scales to zero, so that run is three simultaneous cold starts.

**Warm, with Azure temporarily at `minReplicas: 1`** (2026-08-07, three
consecutive runs, all `3/3 clouds, agreed`):

| run | gcp | aws | azure | max(legs) | sum(legs) | elapsed |
|---|---|---|---|---|---|---|
| 1 | 1327 | 1344 | 485 | 1344 | 3156 | **2494** |
| 2 | 1394 | 1028 | 511 | 1394 | 2933 | **2258** |
| 3 | 1138 | 1116 | 1532 | 1532 | 3786 | **2511** |

**The legs are issued concurrently — elapsed is nowhere near the sum.** But the
sharper claim, "elapsed ≈ max(legs)", is not quite what the numbers say: there
is a consistent ~1s floor above the slowest leg (979–1117ms across all three,
and ~760ms in the 2026-08-03 run). That is the coordinator's own fixed cost —
job container start, three agent-card fetches, three credential mints — and it
is *not* included in any per-leg figure, because those are timed around the
conversion call. So:

> elapsed ≈ max(legs) + ~1s fixed, and emphatically not sum(legs)

Quote it that way. "≈ max(legs)" alone would predict 1344ms for run 1 and be
wrong by 85%, and the fixed cost is the part that would grow with a fourth
cloud only if the mints were serialised — which is worth knowing and is not
measured here.

Cold, the same shape holds: 25012 elapsed against a 24127ms slowest leg.

The Azure leg was cold on *every* run an hour apart — 24127ms, 21848ms,
26847ms — because the deployed Container App sat at `minReplicas: 0` while
`deploy_azure.sh` wrote `--min-replicas 1`. That drift is the whole explanation
for the slowest column in both tables. It is configuration, not Container Apps
being twenty seconds slower than the other two clouds, and a reader comparing
the three columns would have concluded otherwise.

**Scale-to-zero is the intended steady state** for all three clouds — this is a
demonstrator, not a service, and paying for idle replicas to make a latency
table look tidier is paying to mislead. So the fix is not to pin a replica; it
is to stop the scripts and the cloud from disagreeing about which state they
are in. `MIN_REPLICAS` now defaults to `0`, matching intent, and
`./infra/deploy_azure.sh scale <n>` moves between the two without a rebuild —
because the reason the last drift survived is that nobody was going to redeploy
an app to change one integer back.

Any warm number recorded here must say so. Cold and warm figures in one column
is the same error as the drift, one layer up.

The deployed 3×3 matrix is in [`INTEROP.md`](INTEROP.md#the-same-matrix-deployed-2026-08-07):
**8/9**, the single red cell being finding 2, still ADK's own client against
ADK's own server.

## Full revalidation before publication (2026-08-09)

Everything re-run end to end against the deployed mesh, after the `llm` work
and the return to `direct`.

| Check | Result |
|---|---|
| Three-cloud consensus, 3 runs | `3/3 clouds, agreed` each time |
| Warm elapsed vs slowest leg | +729ms, +843ms (cold Azure run: +898ms) |
| Unauthenticated `curl`, `/health` and card | **403** both |
| Each leg alone, as deployed | answered (3/3) |
| Each leg alone, credential removed | denied (3/3) |
| Right identity, wrong audience | denied |
| Hosted matrix | 8/9, 6 cross-cloud + 2 in-cloud, one red cell = finding 2 |
| Stored credentials, all three clouds | none (see below) |
| Suite / lint | 96 passed, 11 skipped / clean |

**Two defects found by revalidating, both mine, both from working outside the
scripts.**

*The AWS leg was returning 400 on every agent-card fetch.* Setting
`CURRENCY_MODEL_MODE` back to `direct` with a direct `update-agent-runtime`
call replaced the runtime's configuration rather than merging into it, dropping
`protocolConfiguration: serverProtocol=A2A`, which the deploy script passes on
every call. The runtime stayed `READY` and its health check passed while no A2A
request could succeed. Restored, verified, and a reason to route every change
through the script.

*The Azure app held a stored secret.* Image pull used the ACR admin password,
kept as a secret in the app's own configuration. Not on any agent-to-agent
path, but enough to falsify "no stored secrets" as written. Now pulls with the
app's managed identity (`AcrPull` on the registry) and the secret is deleted;
`deploy_azure.sh` does this as part of `deploy`. Audited afterwards: the AWS
runtime environment holds five plain values, the Cloud Run service and job
reference no secrets, the Container App's secret list is empty, and there is no
key vault in the resource group.

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

## Open question 2, answered: it was never the resource scope

The predecessor series left this open, and it was the reason to expect this
repo would have to ship `Resource: "*"` and disclose it. It does not.

Read back off the live role, 2026-08-07:

```json
{ "Action": ["bedrock-agentcore:InvokeAgentRuntime",
             "bedrock-agentcore:GetAgentCard"],
  "Resource": ["arn:aws:bedrock-agentcore:us-west-2:…:runtime/currency_aws-9c5IMB2L1X",
               "arn:aws:bedrock-agentcore:us-west-2:…:runtime/currency_aws-9c5IMB2L1X/*"] }
```

Scoped to one runtime, and all three client stacks reach it — agent-card fetch
included. **The predecessor's diagnosis was wrong.** In
`adk-bedrock-a2a-currency` the card fetch 403'd under a policy scoped to
`runtime/<id>` and `runtime/<id>/*`, and widening `Resource` to `"*"` fixed it,
so the resource scope took the blame. The actual cause is that **the card fetch
is a separate IAM action**, `bedrock-agentcore:GetAgentCard`, which that policy
never granted at any scope. Widening the resource worked by accident.

Tested rather than inferred. Removing *only* `GetAgentCard`, leaving the two
narrow resources untouched, and running the AWS leg alone:

```
aws failure: authentication: A2A endpoint returned 403 for
  https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/…/.well-known/agent-card.json:
  {"message":"User: arn:aws:sts::…:assumed-role/currency-aws-federated/currency-mesh-coordinator
   is not authorized to perform: bedrock-agentcore:GetAgentCard on resource:
   arn:aws:bedrock-agentcore:us-west-2:…:runtime/currency_aws-9c5IMB2L1X
   because no identity-based policy allows the bedrock-agentcore:GetAgentCard action"}
```

Restoring the action restores the leg. So the resource scope was never
implicated, and the invoke keeps working throughout — only discovery breaks.

**And the answer was in the response the whole time.** AWS names the missing
action, the resource, and the assumed-role principal, in one sentence. The
predecessor series could not see it for a reason recorded in this repo's own
`CLAUDE.md`: its adapter reported an HTTP status and discarded the provider
body, and the surviving error string was paraphrased by a model into "an issue
with the web identity token." An entirely diagnostic 403 arrived and was thrown
away, and a year of "only `Resource: '*'` works" followed from what was left.

That is the finding worth carrying forward — not the IAM trivia. **The cost was
never the denial; it was the discarded body.** `coordinator/auth.py` logs the
raw provider response at every auth boundary specifically so this cannot happen
here, and this is the first time that decision has paid out.

Two corollaries:

- A missing action and a too-narrow resource both surface as 403, and
  AgentCore's data-plane denials skip CloudTrail by default — so the audit
  trail does not distinguish them either. The response body is the only place
  the difference is written down.
- **The fix that works is not evidence for the theory that motivated it.**
  Widening `Resource` did resolve the predecessor's 403; it just did not
  resolve it for the stated reason.

Worth stating plainly because it changes a deliverable: this mesh reaches
AgentCore under least privilege, and the earlier project's "only `Resource:
'*'` worked" should be treated as retired rather than repeated.

Two more things the same read-back settles:

- **No IAM OIDC provider for `accounts.google.com` exists in the account** —
  the trust policy's principal is `Federated: accounts.google.com` natively.
  That is the rule from `CLAUDE.md` holding in practice: creating one here
  would *break* federation, which is the exact opposite of the Entra side.
- **The runtime's `authorizerConfiguration` is `null`**, which is what selects
  SigV4. It is a tagged union whose only member is `customJWTAuthorizer`, so
  an empty `{}` is rejected with an error that reads as though the field were
  required when in fact it must be absent.

## 4. Re-run the matrix hosted, and expect it to move — done

Local cells are 7–922 ms because everything is loopback and `direct`-brain.
Hosted, they are 360 ms–15.5 s, and **that is not a regression**: the A2A leg
tracks the remote's model and runtime, not the protocol, and every service here
scales to zero so the first call into each column pays a cold start. The
predecessor's 1.7–2.1 s to a Cloud Run container is the right comparison for the
warm cells; its 18.8–25.1 s figure is for hosted *model* runtimes and nothing
here has a model in the path.

Consensus latency is emphatically not the sum, so the coordinator is issuing all
three concurrently — verified, not assumed. But it is not ≈ max(legs) either:
there is a ~1s coordinator floor above the slowest leg. The figure to quote is
**elapsed ≈ max(legs) + ~1s fixed**; see the warm table above for the three runs
it rests on.

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
2. ~~**What ARN shape does AgentCore authorise `InvokeAgentRuntime` against?**~~
   **Answered, 2026-08-07 — the question was aimed at the wrong field.**
   `runtime/<id>` and `runtime/<id>/*` are sufficient; `Resource: "*"` is not
   required and this mesh does not ship it. The predecessor's card-fetch 403
   was a missing *action*, `bedrock-agentcore:GetAgentCard`, not a too-narrow
   *resource*. See "Open question 2, answered" above. The inherited unfinished
   business is closed, and `deploy_aws.sh` now grants both actions.
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
