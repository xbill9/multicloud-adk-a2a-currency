#!/usr/bin/env bash
# Deploy the GCP leg: the ADK agent as an authenticated Cloud Run service, and
# the coordinator as a Cloud Run job that reaches it with a workload OIDC token.
#
#   ./infra/deploy_gcp.sh deploy    # build, deploy service + job, wire IAM
#   ./infra/deploy_gcp.sh wire      # fold the AWS and Azure legs into the job
#   ./infra/deploy_gcp.sh run       # execute the coordinator job, tail its log
#   ./infra/deploy_gcp.sh matrix    # deploy + run the 3x3 matrix, all servers
#   ./infra/deploy_gcp.sh verify    # negative controls -- run these
#   ./infra/deploy_gcp.sh url
#   ./infra/deploy_gcp.sh destroy
#
# The coordinator runs *on Cloud Run* rather than locally because that is the
# whole point: only a Google runtime can mint a workload OIDC token for an
# arbitrary audience, and there is no local equivalent. A laptop cannot
# exercise this path at all.
#
# `deploy` gives you a one-cloud mesh. `wire` is what makes it three, and it is
# a separate verb because it depends on the other two clouds already existing:
# it reads their endpoints and identifiers back out of them rather than keeping
# a second copy here to drift.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
REPO_NAME="${REPO_NAME:-currency-mesh}"
SERVICE="${SERVICE:-currency-gcp}"
JOB="${JOB:-currency-coordinator}"
MATRIX_JOB="${MATRIX_JOB:-currency-matrix}"
COORDINATOR_SA="${COORDINATOR_SA:-currency-coordinator@${PROJECT}.iam.gserviceaccount.com}"
#: No --cloud flag, so coordinator.cli defaults to all three participants.
THREE_CLOUD_ARGS="-m,coordinator.cli,100,USD,EUR,JPY"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO_NAME}/currency-mesh:latest"

service_url() {
  gcloud run services describe "$SERVICE" \
    --region "$REGION" --project "$PROJECT" --format='value(status.url)'
}

build() {
  gcloud artifacts repositories describe "$REPO_NAME" \
    --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "$REPO_NAME" \
      --repository-format=docker --location "$REGION" --project "$PROJECT" \
      --description="Three-cloud A2A currency mesh"

  gcloud builds submit "$REPO" \
    --tag "$IMAGE" --project "$PROJECT" \
    --gcs-source-staging-dir "gs://${PROJECT}_cloudbuild/source"
}

deploy() {
  build

  # --no-allow-unauthenticated is the point of the exercise: the service
  # rejects anything without a valid Google ID token whose audience is this
  # service's own URL.
  gcloud run deploy "$SERVICE" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT" \
    --no-allow-unauthenticated \
    --port 8080 \
    --set-env-vars CURRENCY_MODEL_MODE=direct,HOST=0.0.0.0 \
    --min-instances 0 --max-instances 2 \
    --quiet

  local url
  url="$(service_url)"
  echo "service: $url"

  # Audience alone is not authorization -- it is caller-chosen. This IAM
  # binding is what actually authorizes the call; the token only proves who
  # is asking.
  gcloud run services add-iam-policy-binding "$SERVICE" \
    --region "$REGION" --project "$PROJECT" \
    --member "serviceAccount:${COORDINATOR_SA}" \
    --role roles/run.invoker --quiet >/dev/null
  echo "granted roles/run.invoker to ${COORDINATOR_SA}"

  gcloud run jobs deploy "$JOB" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT" \
    --service-account "$COORDINATOR_SA" \
    --set-env-vars "GCP_A2A_ENDPOINT=${url},GCP_A2A_AUTH=google-id-token,CURRENCY_COORDINATOR_CLOUD=gcp" \
    --command python \
    --args="-m,coordinator.cli,100,USD,EUR,JPY,--cloud,gcp" \
    --max-retries 0 --task-timeout 300s \
    --quiet
}

# The three-cloud env, assembled from the sibling scripts rather than restated
# here. Each cloud's identifiers live in exactly one place -- the script that
# created them -- so a redeployed AgentCore runtime (whose URL contains its own
# ARN) or a recreated app registration cannot leave a stale copy behind.
peer_env() {
  echo "GCP_A2A_ENDPOINT=$(service_url)"
  echo "GCP_A2A_AUTH=google-id-token"
  # Both jobs run on Cloud Run, so the gcp leg never leaves Google Cloud. The
  # matrix marks that column rather than counting it toward the interop claim;
  # unset (the local mesh) means the distinction does not arise.
  echo "CURRENCY_COORDINATOR_CLOUD=gcp"
  # An empty *value* is the dangerous case, not empty output. A sibling that
  # cannot resolve its endpoint used to emit `AWS_A2A_ENDPOINT=` and exit 0;
  # the old guard only checked that *some* assignment came back, so the empty
  # one sailed through and got pushed to the live job. The coordinator then
  # degrades over the dead leg and exits 0 too. Refuse instead.
  #
  # stderr is deliberately no longer swallowed: the sibling script explains
  # *why* it could not resolve, and that message is the entire diagnosis.
  local script block
  for script in deploy_aws deploy_azure; do
    if ! block="$("$REPO/infra/${script}.sh" env)"; then
      echo "error: ${script}.sh env failed; refusing to wire a partial mesh." >&2
      echo "       set ALLOW_PARTIAL_MESH=1 to wire the legs that do resolve." >&2
      [[ "${ALLOW_PARTIAL_MESH:-}" == "1" ]] || return 1
      continue
    fi
    block="$(printf '%s\n' "$block" | grep -E '^[A-Z][A-Z0-9_]*=' || true)"
    if printf '%s\n' "$block" | grep -qE '^[A-Z][A-Z0-9_]*=$'; then
      echo "error: ${script}.sh env resolved these to nothing:" >&2
      printf '%s\n' "$block" | grep -E '^[A-Z][A-Z0-9_]*=$' | sed 's/^/         /' >&2
      echo "       refusing to wire a leg the coordinator cannot reach." >&2
      echo "       set ALLOW_PARTIAL_MESH=1 to wire it anyway." >&2
      [[ "${ALLOW_PARTIAL_MESH:-}" == "1" ]] || return 1
    fi
    [[ -n "$block" ]] && printf '%s\n' "$block"
  done
}

wire() {
  local vars
  # ^@^ makes @ the separator: gcloud splits --set-env-vars on commas by
  # default, and a percent-encoded ARN is exactly the kind of value that
  # eventually contains one.
  vars="$(peer_env | paste -sd'@' -)"
  [[ -z "$vars" ]] && { echo "nothing to wire" >&2; exit 1; }

  # The args matter as much as the env. `deploy` pins --cloud gcp because at
  # that point one cloud is all there is; wiring is precisely the step that
  # stops being true, and leaving the flag behind produced a run that reported
  # "1/1 clouds, unverified" and exited 0 with three legs correctly configured
  # underneath it -- the env said three clouds and the args said one.
  # (`verify` also passes --args, but to `jobs execute`, which is an
  # execution-scoped override and leaves the job spec alone.)
  gcloud run jobs update "$JOB" \
    --region "$REGION" --project "$PROJECT" \
    --set-env-vars "^@^${vars}" \
    --args="$THREE_CLOUD_ARGS" --quiet >/dev/null
  echo "coordinator job wired:"
  peer_env | sed 's/^/  /'
  echo "  args: ${THREE_CLOUD_ARGS//,/ }"
}

run() {
  gcloud run jobs execute "$JOB" \
    --region "$REGION" --project "$PROJECT" --wait --quiet
}

# The matrix job carries the same env as the coordinator, because the whole
# point of the 3x3 grid is that every client stack drives every server through
# the same credential seam. A matrix wired to fewer peers than the coordinator
# measures a different mesh than the one the consensus run measured.
matrix() {
  local vars
  vars="$(peer_env | paste -sd'@' -)"
  gcloud run jobs deploy "$MATRIX_JOB" \
    --image "$IMAGE" \
    --region "$REGION" --project "$PROJECT" \
    --service-account "$COORDINATOR_SA" \
    --set-env-vars "^@^${vars}" \
    --command python \
    --args="-m,matrix.runner" \
    --max-retries 0 --task-timeout 600s \
    --quiet >/dev/null
  gcloud run jobs execute "$MATRIX_JOB" \
    --region "$REGION" --project "$PROJECT" --wait --quiet
}

# Negative controls, run where the credentials actually are. Everything here
# uses execution-time env overrides rather than a redeploy, so the job that
# proves the denial is bit-for-bit the job that proves the success -- otherwise
# the control tests a configuration nothing else ever runs.
#
# A control that "fails" is a control that passed. Read the exit codes below as
# the assertions they are.
# Every probe is restricted to ONE cloud with --cloud. That is not tidiness: the
# mesh is a median and degrades on purpose, so a three-cloud run with one leg's
# credential removed still reaches quorum on the other two and exits 0. Read as
# a control, that exit code says "the denial was absorbed" while looking exactly
# like "there was no denial". One cloud per probe is what makes the exit code
# mean something.
probe() {
  local label cloud expect; label="$1"; cloud="$2"; expect="$3"; shift 3
  local rc=0
  echo
  echo "--- ${label}"
  gcloud run jobs execute "$JOB" \
    --region "$REGION" --project "$PROJECT" --wait --quiet \
    --args="-m,coordinator.cli,100,USD,EUR,--cloud,${cloud}" \
    ${1+--update-env-vars "$*"} >/dev/null 2>&1 || rc=$?

  if [[ "$expect" == "deny" ]]; then
    [[ "$rc" -ne 0 ]] \
      && echo "    exit ${rc} -- denied, as required" \
      || echo "    exit 0 -- ANSWERED WITHOUT THE CREDENTIAL. This control failed:
    the leg's auth mode is a label, not a control."
  else
    [[ "$rc" -eq 0 ]] \
      && echo "    exit 0 -- answered, as required" \
      || echo "    exit ${rc} -- the POSITIVE control failed; the leg is broken
    independently of auth, and the denials above prove nothing until it is fixed."
  fi
}

verify() {
  local url; url="$(service_url)"

  echo "1. unauthenticated, from here -- no Google token at all"
  echo "   /health    -> $(curl -s -o /dev/null -w '%{http_code}' -m 25 "${url}/health")   (expect 403)"
  echo "   agent card -> $(curl -s -o /dev/null -w '%{http_code}' -m 25 "${url}/.well-known/agent-card.json")   (expect 403)"
  echo
  echo "2. positive controls -- each leg alone, credentials as deployed."
  echo "   These come first: a denial only means something once you know the"
  echo "   leg answers at all."

  probe "GCP leg, as deployed"   gcp   allow
  probe "AWS leg, as deployed"   aws   allow
  probe "Azure leg, as deployed" azure allow

  echo
  echo
  echo "3. negative controls -- each leg alone, credential removed."

  probe "GCP leg, auth mode forced to none"   gcp   deny GCP_A2A_AUTH=none
  probe "AWS leg, auth mode forced to none"   aws   deny AWS_A2A_AUTH=none
  probe "Azure leg, auth mode forced to none" azure deny AZURE_A2A_AUTH=none

  # Audience is caller-chosen, so this proves less than it looks like it does --
  # see the note in docs/DEPLOYMENT_PLAN.md about the user token Cloud Run
  # accepted with gcloud's own client ID as its audience. It is still worth
  # running: it separates "the token was rejected" from "no token was sent".
  probe "GCP leg, right identity, wrong audience" gcp deny \
    GCP_A2A_AUDIENCE=https://not-this-service.example.com
}

destroy() {
  gcloud run jobs delete "$MATRIX_JOB" --region "$REGION" --project "$PROJECT" --quiet || true
  gcloud run jobs delete "$JOB" --region "$REGION" --project "$PROJECT" --quiet || true
  gcloud run services delete "$SERVICE" --region "$REGION" --project "$PROJECT" --quiet || true
}

case "${1:-deploy}" in
  build) build ;;
  deploy) deploy ;;
  wire) wire ;;
  run) run ;;
  matrix) matrix ;;
  verify) verify ;;
  url) service_url ;;
  destroy) destroy ;;
  *) echo "usage: $0 {build|deploy|wire|run|matrix|verify|url|destroy}" >&2; exit 2 ;;
esac
