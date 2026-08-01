#!/usr/bin/env bash
# Deploy the GCP leg: the ADK agent as an authenticated Cloud Run service, and
# the coordinator as a Cloud Run job that reaches it with a workload OIDC token.
#
#   ./infra/deploy_gcp.sh deploy    # build, deploy service + job, wire IAM
#   ./infra/deploy_gcp.sh run       # execute the coordinator job, tail its log
#   ./infra/deploy_gcp.sh url
#   ./infra/deploy_gcp.sh destroy
#
# The coordinator runs *on Cloud Run* rather than locally because that is the
# whole point: only a Google runtime can mint a workload OIDC token for an
# arbitrary audience, and there is no local equivalent. A laptop cannot
# exercise this path at all.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
REPO_NAME="${REPO_NAME:-currency-mesh}"
SERVICE="${SERVICE:-currency-gcp}"
JOB="${JOB:-currency-coordinator}"
COORDINATOR_SA="${COORDINATOR_SA:-currency-coordinator@${PROJECT}.iam.gserviceaccount.com}"
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
    --set-env-vars "GCP_A2A_ENDPOINT=${url},GCP_A2A_AUTH=google-id-token" \
    --command python \
    --args="-m,coordinator.cli,100,USD,EUR,JPY,--cloud,gcp" \
    --max-retries 0 --task-timeout 300s \
    --quiet
}

run() {
  gcloud run jobs execute "$JOB" \
    --region "$REGION" --project "$PROJECT" --wait --quiet
}

destroy() {
  gcloud run jobs delete "$JOB" --region "$REGION" --project "$PROJECT" --quiet || true
  gcloud run services delete "$SERVICE" --region "$REGION" --project "$PROJECT" --quiet || true
}

case "${1:-deploy}" in
  build) build ;;
  deploy) deploy ;;
  run) run ;;
  url) service_url ;;
  destroy) destroy ;;
  *) echo "usage: $0 {build|deploy|run|url|destroy}" >&2; exit 2 ;;
esac
