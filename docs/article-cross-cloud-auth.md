# Three clouds, no stored secrets

## What it takes to let agents call each other across vendors

You have an agent on one cloud. Someone asks you to have it call an agent on
another. The reflex is to create a service account key, put it in a secret
manager, and move on. That works, and it is the decision you will be living
with for the rest of the project.

This is a write-up of a three-cloud agent mesh — Google ADK on Cloud Run,
Strands on Bedrock AgentCore, Microsoft Agent Framework on Container Apps — all
speaking A2A v1.0, coordinated from one place, with no long-lived credential
stored anywhere in the running system. The interesting part is not the protocol.
It is that almost every consequential decision was made before any protocol was
spoken.

## The asymmetry that decides everything

Cross-cloud agent auth looks like a protocol problem and is an identity
problem. The shape of it is this:

**Every callee in this mesh consumes external OIDC.** AWS IAM has OIDC identity
providers. Entra has Federated Identity Credentials. AgentCore Runtime accepts a
`CUSTOM_JWT`. Each of them will accept a token minted somewhere else, if you
configure the trust correctly.

**Only some callers can mint one.** A runtime that can produce a workload OIDC
token for an audience you choose can federate outward to anything on that list.
A runtime that cannot must fall back to a stored credential.

That single asymmetry determines your topology, because the coordinator — the
component that calls the others — is the only one that needs to mint.

| Coordinator host | Outbound legs | Long-lived secrets |
|---|---|---|
| Cloud Run | GCP→AWS, GCP→Azure, GCP→GCP | potentially zero |
| AgentCore | AWS→Azure, AWS→GCP | at least one |
| Foundry | Azure→AWS, Azure→GCP | one or two, both unproven |

Cloud Run is the choice here because its metadata server mints ID tokens for an
arbitrary audience, and that is exactly the input the other two clouds' trust
policies want. Whether AgentCore Runtime can do the same is, as far as this
project established, simply unconfirmed — so "secretless" is a property of
*this* topology, not a general claim about cross-cloud agents.

It also has a cost worth stating plainly. Putting the coordinator on Cloud Run
means the GCP leg is a hop that never leaves Google Cloud. Two of the three legs
cross a vendor boundary; the third does not, and counting it toward an interop
claim would inflate the result.

There is a second-order consequence that surprised me. A user credential cannot
mint an arbitrary-audience ID token at all, so there is no laptop equivalent of
this path. The coordinator cannot be run locally, even for debugging. Once you
choose federation, the only place the system works is the place it is deployed.

## Three mechanisms, one seam

The three legs use genuinely different machinery:

- **GCP → GCP** — a Google ID token whose audience is the receiving service's
  URL, authorized by `roles/run.invoker`.
- **GCP → AWS** — the same metadata mint, presented to STS
  `AssumeRoleWithWebIdentity`, exchanged for temporary credentials, used to sign
  the request with SigV4.
- **GCP → Azure** — the same metadata mint again, presented to Entra as a client
  assertion against a Federated Identity Credential, exchanged for an access
  token.

Three exchanges, three token formats, one of which is not a bearer token at all
but a signature over the request body. Putting them behind one interface is the
single decision that made the rest tractable.

The interface is `httpx.Auth`. It is the only shape that spans both cases: to
httpx, a bearer header and a request signature are the same kind of object. All
three vendor client SDKs accept an `httpx.AsyncClient`, so the credential
attaches once and every call through that client carries it.

Build this seam before the first deployed peer, not after the third. The
temptation is to get one leg working with inline code and generalise later, and
by the time you have three legs you have three error-handling styles and three
places where a token is cached.

**Discovery is privileged, and it is easy to miss.** An agent's card lives at
`/.well-known/agent-card.json` behind the same authorization as the agent
itself. If the credential attaches to the message call but not the card fetch,
you get a 403 during discovery — which surfaces as a protocol or transport
error, nowhere near auth, pointing at the wrong layer. Attaching auth to the
client rather than the request is what makes this correct by construction.

## The traps that cost real time

These are not configuration mistakes. Each one looks like correct configuration
and fails silently or misleadingly.

**Audience is caller-chosen, so audience alone is never authorization.** The
caller decides what audience to request. A trust policy that only checks the
audience proves that *some* identity in that IdP minted a token, which is not
the same as proving *this* identity did. Pin the subject as well — and pin it to
the immutable numeric ID, never the email, because an email can be released and
re-bound to a different principal.

**AWS and Azure have opposite rules for the same-looking task.** AWS federates
with `accounts.google.com` natively; creating an explicit IAM OIDC provider for
it *breaks* federation with `InvalidIdentityToken`. Entra requires you to create
the Federated Identity Credential explicitly. Same conceptual step, inverted
prerequisites, and the failure from getting it backwards names neither.

**The IAM condition keys do not mean what they are named.**
`accounts.google.com:oaud` is the token's `aud` claim.
`accounts.google.com:aud` is its `azp`, which is a number. Putting an audience
string in `:aud` produces a condition that can never match, and the denial does
not explain why.

**Ask for the full token.** The GCP metadata mint takes a `format` parameter,
and without `format=full` Google trims claims including `email`. Trust
conditions that read a trimmed claim stop matching, with no error saying so.

**Two error codes are worth more than any amount of logging.** From STS,
`InvalidIdentityToken` means the token could not be validated at all — a
provider-setup problem. `AccessDenied` means it validated fine and your
conditions did not match — a policy problem. Those are different afternoons, and
the distinction is the fastest diagnostic available.

That last point generalises into the one piece of engineering advice I would
carry to any similar project: **log the raw provider response at every auth
boundary.** In an agent system the error travels back as a tool result and can
be paraphrased by a model before a human sees it. A raised message is not an
observable. The federation work is straightforward; reading the failures is
where the time goes.

## Deployment strategy

**Hosting decides the auth bill.** This is the same claim as the opening
section, viewed from the deployment side: the coordinator's runtime is not a
deployment detail to settle later. It determines how many long-lived secrets the
entire system needs. Decide it first.

**Scale to zero, and label what that costs.** Every service here idles at zero
replicas. That is the right steady state for a demonstrator — paying for idle
capacity on three clouds to make a latency table look tidier is paying to
mislead — but it means the first call into any leg pays a cold start. Measured
here, a cold Azure leg took 23.4 seconds against 0.5 seconds warm. If a table
mixes those two regimes without saying which is which, every conclusion drawn
from it is wrong. Label warm and cold, or do not publish the number.

**Deployment belongs in the repository as verbs, not in a runbook.** `deploy`,
`wire`, `verify`. The identifiers for each cloud live in exactly one place — the
script that created them — and the other scripts read them back out rather than
keeping a second copy. A redeployed AgentCore runtime has a new ARN, and its
invocation URL contains that ARN, so any copy of it elsewhere is stale the
moment it is written down.

**Separate "deployed" from "wired".** Deploying one cloud gives a one-cloud
mesh. Wiring is the step that makes it three, and it depends on the other clouds
already existing. Keeping them as separate verbs makes the dependency explicit
instead of encoding it in the order someone happens to run things.

## Scaffolding

Four structures did most of the work.

**One credential seam.** Described above. `credentials_for(peer)` returns
something that knows how to authenticate to that peer, and callers do not know
which of the three mechanisms they are using.

**One participant interface.** A cloud is an implementation of a small protocol
— `convert()` — not a branch in the coordinator. Adding a cloud means adding an
implementation and an entry in a registry. This is what makes it possible to
express "every client against every server" as a loop rather than a rewrite.

**An instrument, not a demo.** The matrix runs every client stack against every
served agent and records, for each failure, *which layer broke*: transport,
protocol, timeout, authentication, provider. Typing the failure is the
difference between "the mesh is broken" and "discovery is returning 403 on one
leg". The classification has to walk the vendor SDK's exception chain, because
each stack wraps the original cause in a type of its own.

**Negative controls, scoped to one component at a time.** This is the piece I
would most encourage copying, because getting it wrong is invisible.

The mesh uses a median across three clouds and degrades on purpose: if one cloud
fails, the other two still reach quorum and the run exits successfully. That is
correct behaviour at runtime and it makes a naive control useless. Remove one
leg's credential from a three-cloud run and the run still exits 0 — which reads
as "no denial occurred" when what actually happened is "the denial was absorbed."

So every leg is probed alone. Seven probes: each leg answering with its
credential, each leg denied without it, and an unauthenticated request rejected
outright. Only then does the exit code mean anything.

The general form: **any system with graceful degradation needs its controls
scoped to a single component, or the degradation hides the failure you are
testing for.**

## What it costs

Four consecutive warm runs of the three-cloud consensus, min–max across the
four:

| | GCP (in-cloud) | AWS | Azure | elapsed |
|---|---|---|---|---|
| range | 1042–1200ms | 1073–1157ms | 474–587ms | 1953–2169ms |

Three further warm runs the previous day landed at 2258–2511ms elapsed, with
the same relationship between elapsed time and the slowest leg.

Elapsed time sits consistently about a second above the slowest single leg, and
far below the sum of all three. The legs are issued concurrently, so the sum was
never the right model — but neither is the slowest leg on its own. That extra
second is the coordinator's own fixed cost: container start, three agent-card
fetches, three credential mints. No per-leg figure contains it. An earlier
version of this claim quoted the slowest leg alone and was wrong by 85% on the
fastest run, which is the sort of error that only shows up once there is more
than one sample.

The federation itself is not expensive. Token mints and exchanges are a small
part of that fixed second. If the mesh feels slow, the cause is a cold start or
a model, not the identity work.

## What this does not show

One deployment, one account, one region pair, one operator, over a few days.
These are existence proofs — a thing worked, in a configuration, once. They are
not measurements of a population.

The mesh is keyless in operation, not in bootstrap: creating trust policies, app
registrations and federated credentials used ordinary operator credentials, as
provisioning always does. The claim is about the running system.

Token expiry and refresh are implemented and covered by tests, but no token has
expired in production — every run is a job that lives a few seconds, so the
refresh paths have not executed against a real provider.

And the third leg is an in-cloud hop, as described at the top. Two boundaries
crossed, not three.

## What I would tell someone starting this

Decide where the coordinator runs before anything else; it sets the secret count
for the whole system. Build the credential seam before the second cloud. Attach
auth to the client, not the request, so discovery is covered. Log the provider's
own words at every boundary, because you will spend more time reading auth
failures than writing auth code. And scope your controls to one component,
because a system designed to survive a failure will happily hide one from you.
