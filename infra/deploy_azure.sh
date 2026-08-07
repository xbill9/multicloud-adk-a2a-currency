#!/usr/bin/env bash
# Deploy the Azure leg: the Agent Framework A2AExecutor agent on Container
# Apps, plus the Entra app registration and Federated Identity Credential the
# GCP-hosted master exchanges its Google token against.
#
#   ./infra/deploy_azure.sh deploy     # RG, ACR, env, container app
#   ./infra/deploy_azure.sh fic        # Entra app registration + FIC
#   ./infra/deploy_azure.sh env        # env vars to add to the GCP master
#   ./infra/deploy_azure.sh verify     # negative controls -- run these
#   ./infra/deploy_azure.sh url
#   ./infra/deploy_azure.sh destroy
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
      --min-replicas 1 --max-replicas 2 \
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
  local url; url="$(app_url)"
  echo "1. health                -> $(curl -s -o /dev/null -w '%{http_code}' -m 15 "${url}/health")"
  echo "2. agent card            -> $(curl -s -o /dev/null -w '%{http_code}' -m 15 "${url}/.well-known/agent-card.json")"
  echo "3. card advertises       -> $(curl -s -m 15 "${url}/.well-known/agent-card.json" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("url") or [i.get("url") for i in d.get("additionalInterfaces",d.get("supportedInterfaces",[]))])' 2>/dev/null)"
  echo
  echo "NOTE: ingress is currently PUBLIC. Until Entra auth is enforced in front"
  echo "of it, this leg proves the agent and the serving stack, NOT the identity"
  echo "story -- and the mesh's keyless claim does not yet cover it."
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
  env) env_block ;;
  verify) verify ;;
  url) app_url ;;
  destroy) destroy ;;
  *) echo "usage: $0 {deploy|fic|env|verify|url|destroy}" >&2; exit 2 ;;
esac
