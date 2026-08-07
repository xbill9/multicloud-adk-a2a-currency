#!/usr/bin/env bash
# Deploy the Azure leg: the Agent Framework A2AExecutor agent on Container
# Apps, plus the Entra app registration and Federated Identity Credential the
# GCP-hosted master exchanges its Google token against.
#
#   ./infra/deploy_azure.sh deploy     # RG, ACR, env, container app
#   ./infra/deploy_azure.sh fic        # Entra app registration + FIC
#   ./infra/deploy_azure.sh auth       # enforce Entra in front of the ingress
#   ./infra/deploy_azure.sh scale 1    # warm it for a measurement run
#   ./infra/deploy_azure.sh scale 0    # back to scale-to-zero, the steady state
#   ./infra/deploy_azure.sh env        # env vars to add to the GCP master
#   ./infra/deploy_azure.sh verify     # negative controls -- run these
#   ./infra/deploy_azure.sh url
#   ./infra/deploy_azure.sh destroy
#
# `fic` and `auth` are two halves of one story and neither is sufficient. The
# FIC decides who can *obtain* a token for this app; `auth` decides whether the
# app *demands* one. Ship only the first and the leg reports `entra-fic` while
# answering anyone who asks -- a claim about the caller, dressed as a control.
#
# This is the unproven leg. GCP->GCP was trivial and GCP->AWS was a port of a
# mechanism already working elsewhere; nothing here has ever been exercised
# against a real Entra tenant. See docs/DEPLOYMENT_PLAN.md step 3.3.
#
# The trap to watch for, mirrored from the AWS side: AWS federates with Google
# *natively* and creating an explicit OIDC provider breaks it, whereas Entra
# requires the Federated Identity Credential to be created explicitly. Opposite
# rules, identical-looking task.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCATION="${LOCATION:-westus2}"
RG="${RG:-currency-mesh-rg}"
ACR="${ACR_NAME:-currencymeshacr}"
ENVIRONMENT="${ENVIRONMENT:-currency-mesh-env}"
APP="${APP:-currency-azure}"
APP_REG="${APP_REG:-currency-mesh-master}"
IMAGE_TAG="${IMAGE_TAG:-azure-agent}"
DOCKER="${DOCKER:-docker}"

# Scale-to-zero is the steady state for this mesh: it is a demonstrator, not a
# service, and paying for an idle replica on three clouds to make a latency
# table look tidier would be paying to mislead. The cost is a ~20s cold start
# on every call, which is the single largest number in every deployed table
# here and is configuration rather than Container Apps being slow.
#
# This was hard-coded to 1 while the deployed app sat at 0, so the scripts
# disagreed with the cloud and the cold starts read as a property of Azure.
# Set MIN_REPLICAS=1 to warm it for a measurement run; `$0 scale 0` to undo.
MIN_REPLICAS="${MIN_REPLICAS:-0}"
MAX_REPLICAS="${MAX_REPLICAS:-2}"

GCP_PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
MASTER_SA="${COORDINATOR_SA:-currency-coordinator@${GCP_PROJECT}.iam.gserviceaccount.com}"

app_url() {
  local fqdn
  fqdn="$(az containerapp show -n "$APP" -g "$RG" \
          --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null)"
  [[ -z "$fqdn" ]] && { echo "container app not deployed" >&2; exit 1; }
  echo "https://${fqdn}"
}

# The FIC pins the *numeric* unique ID, never the email: an email can be freed
# and re-bound to a different principal, so a subject condition written against
# one is a condition that can be inherited.
master_sa_unique_id() {
  local id
  id="$(gcloud iam service-accounts describe "$MASTER_SA" --format='value(uniqueId)' 2>/dev/null || true)"
  [[ -z "$id" ]] && { echo "cannot read the numeric unique ID of ${MASTER_SA}" >&2; exit 1; }
  echo "$id"
}

ensure_infra() {
  az group create -n "$RG" -l "$LOCATION" -o none
  az acr show -n "$ACR" -g "$RG" -o none 2>/dev/null || \
    az acr create -n "$ACR" -g "$RG" --sku Basic --admin-enabled true -o none
  az containerapp env show -n "$ENVIRONMENT" -g "$RG" -o none 2>/dev/null || {
    echo "creating container app environment (this takes a few minutes)"
    az containerapp env create -n "$ENVIRONMENT" -g "$RG" -l "$LOCATION" -o none
  }
}

build_and_push() {
  local image="${ACR}.azurecr.io/currency-azure:${IMAGE_TAG}"
  # ACR build runs server-side: no local docker, no architecture guessing, and
  # it works on a machine that cannot build linux/amd64 natively.
  {
    az acr build --registry "$ACR" --resource-group "$RG" \
      --image "currency-azure:${IMAGE_TAG}" \
      --file "$REPO/infra/Dockerfile.azure" "$REPO"
  } >&2
  echo "$image"
}

deploy() {
  ensure_infra
  local image url pw
  image="$(build_and_push)"
  pw="$(az acr credential show -n "$ACR" --query 'passwords[0].value' -o tsv)"

  if az containerapp show -n "$APP" -g "$RG" -o none 2>/dev/null; then
    az containerapp update -n "$APP" -g "$RG" --image "$image" -o none
  else
    az containerapp create -n "$APP" -g "$RG" \
      --environment "$ENVIRONMENT" \
      --image "$image" \
      --registry-server "${ACR}.azurecr.io" \
      --registry-username "$ACR" --registry-password "$pw" \
      --target-port 8080 --ingress external \
      --min-replicas "$MIN_REPLICAS" --max-replicas "$MAX_REPLICAS" \
      --env-vars CURRENCY_MODEL_MODE=direct HOST=0.0.0.0 PORT=8080 -o none
  fi

  url="$(app_url)"
  # Two-phase, as on both other clouds: the card must advertise an ingress FQDN
  # that does not exist until the app does.
  az containerapp update -n "$APP" -g "$RG" \
    --set-env-vars "PUBLIC_URL=${url}" CURRENCY_MODEL_MODE=direct HOST=0.0.0.0 PORT=8080 -o none

  echo
  echo "container app : $url"
  echo
  echo "Next: ./infra/deploy_azure.sh fic"
}

# The Entra half. An app registration whose Federated Identity Credential
# trusts Google's issuer, so the master exchanges a Google-minted token for an
# Entra token with no secret anywhere.
fic() {
  local app_id sub
  app_id="$(az ad app list --display-name "$APP_REG" --query '[0].appId' -o tsv 2>/dev/null)"
  if [[ -z "$app_id" ]]; then
    app_id="$(az ad app create --display-name "$APP_REG" --query appId -o tsv)"
    echo "created app registration $APP_REG ($app_id)"
    az ad sp create --id "$app_id" -o none 2>/dev/null || true
  else
    echo "app registration exists: $app_id"
  fi

  sub="$(master_sa_unique_id)"

  # issuer  : https://accounts.google.com -- for Entra this MUST be created
  #           explicitly, the opposite of the AWS rule
  # subject : the SA's numeric unique ID, never its email
  # audience: api://AzureADTokenExchange -- Entra rejects anything else
  local body
  body="$(python3 -c '
import json,sys
print(json.dumps({
  "name": "gcp-master",
  "issuer": "https://accounts.google.com",
  "subject": sys.argv[1],
  "audiences": ["api://AzureADTokenExchange"],
  "description": "GCP-hosted coordinator, keyless"
}))' "$sub")"

  az ad app federated-credential create --id "$app_id" --parameters "$body" -o none 2>/dev/null \
    && echo "created FIC pinning subject=$sub" \
    || echo "FIC already exists (subject should be $sub) -- verify with: az ad app federated-credential list --id $app_id"

  echo
  echo "tenant  : $(az account show --query tenantId -o tsv)"
  echo "clientId: $app_id"
}

# The enforcement half. Container Apps' built-in auth validates the token at
# the ingress, before the request reaches the container, so the agent stays
# credential-free and identical to the one that runs locally -- the same
# property Cloud Run gives the GCP leg and IAM gives the AWS one.
auth_enforce() {
  local app_id tenant
  app_id="$(az ad app list --display-name "$APP_REG" --query '[0].appId' -o tsv 2>/dev/null)"
  [[ -z "$app_id" ]] && { echo "no app registration; run: $0 fic" >&2; exit 1; }
  tenant="$(az account show --query tenantId -o tsv)"

  # Pin both. The issuer alone would accept any app in the tenant; the audience
  # alone would accept a token minted for this app by a different issuer. And
  # neither says *who* -- that is the FIC's subject condition, one layer up.
  az containerapp auth microsoft update -n "$APP" -g "$RG" \
    --client-id "$app_id" \
    --issuer "https://sts.windows.net/${tenant}/" \
    --allowed-audiences "$app_id" \
    --yes -o none

  # Return401, never the default RedirectToLoginPage. This is an API: a 302 to
  # an interactive sign-in page is a 200-with-HTML to an A2A client, which then
  # reports a parse error and sends you looking for a protocol bug.
  az containerapp auth update -n "$APP" -g "$RG" \
    --enabled true --unauthenticated-client-action Return401 -o none

  echo "ingress now rejects unauthenticated callers with 401"
  echo "issuer  : https://sts.windows.net/${tenant}/"
  echo "audience: ${app_id}"
}

# Warm the leg for a measurement run, or put it back. Separate from `deploy` so
# that returning to scale-to-zero costs one command and does not go through a
# rebuild -- the reason the last drift survived is that nobody was going to
# redeploy an app just to change one integer back.
scale() {
  local n="${1:?usage: $0 scale <min-replicas>}"
  az containerapp update -n "$APP" -g "$RG" \
    --min-replicas "$n" --max-replicas "$MAX_REPLICAS" -o none
  az containerapp show -n "$APP" -g "$RG" \
    --query '{min:properties.template.scale.minReplicas,
              max:properties.template.scale.maxReplicas,
              revision:properties.latestRevisionName}' -o json
  [[ "$n" -gt 0 ]] && cat <<'EOF'

Warm. This is a MEASUREMENT state, not the steady state -- it bills for an idle
replica. Latencies recorded now are warm-path numbers and must be labelled as
such; do not mix them into a table alongside cold ones. Put it back with:

  ./infra/deploy_azure.sh scale 0
EOF
  return 0
}

env_block() {
  local app_id
  app_id="$(az ad app list --display-name "$APP_REG" --query '[0].appId' -o tsv)"
  cat <<EOF
# Add to the GCP master (Cloud Run job) to reach this agent:
AZURE_A2A_ENDPOINT=$(app_url)
AZURE_A2A_AUTH=entra-fic
AZURE_A2A_TENANT_ID=$(az account show --query tenantId -o tsv)
AZURE_A2A_CLIENT_ID=${app_id}
# Defaults to <client-id>/.default; set explicitly only if the API exposes a
# different scope.
EOF
}

verify() {
  local url health card fic sub expected
  url="$(app_url)"

  echo "an authenticated leg is unproven without negative controls."
  echo

  health="$(curl -s -o /dev/null -w '%{http_code}' -m 25 "${url}/health")"
  card="$(curl -s -o /dev/null -w '%{http_code}' -m 25 "${url}/.well-known/agent-card.json")"
  echo "1. no token, /health                  -> ${health}   (expect 401)"
  echo "2. no token, agent card               -> ${card}   (expect 401; discovery"
  echo "   is privileged here exactly as on the other two clouds)"

  echo "3. enforcement config actually stored:"
  az containerapp auth show -n "$APP" -g "$RG" \
    --query '{action:globalValidation.unauthenticatedClientAction,
              issuer:identityProviders.azureActiveDirectory.registration.openIdIssuer,
              audiences:identityProviders.azureActiveDirectory.validation.allowedAudiences}' \
    -o json 2>/dev/null | sed 's/^/   /'

  # The binding, which is the part neither a status code nor the audience list
  # can show: only one principal in the world can obtain a token for that
  # audience, and this is where that is written down.
  echo "4. FIC subject vs the master SA's numeric unique ID:"
  fic="$(az ad app federated-credential list \
          --id "$(az ad app list --display-name "$APP_REG" --query '[0].appId' -o tsv)" \
          --query '[0].subject' -o tsv 2>/dev/null || true)"
  expected="$(master_sa_unique_id)"
  sub="${fic:-<none>}"
  echo "   FIC subject : ${sub}"
  echo "   master SA   : ${expected}"
  [[ "$sub" == "$expected" ]] \
    && echo "   bound to the one principal that can mint the assertion" \
    || echo "   MISMATCH -- the audience check is then the only control, and it is not one"

  echo
  echo "The positive control is the GCP master: ./infra/deploy_gcp.sh verify"
}

destroy() {
  az containerapp delete -n "$APP" -g "$RG" --yes -o none 2>/dev/null || true
  az group delete -n "$RG" --yes --no-wait -o none 2>/dev/null || true
  local app_id
  app_id="$(az ad app list --display-name "$APP_REG" --query '[0].appId' -o tsv 2>/dev/null)"
  [[ -n "$app_id" ]] && az ad app delete --id "$app_id" -o none 2>/dev/null || true
}

case "${1:-deploy}" in
  deploy) deploy ;;
  fic) fic ;;
  auth) auth_enforce ;;
  scale) shift; scale "${1:-0}" ;;
  env) env_block ;;
  verify) verify ;;
  url) app_url ;;
  destroy) destroy ;;
  *) echo "usage: $0 {deploy|fic|auth|scale <n>|env|verify|url|destroy}" >&2; exit 2 ;;
esac
