#!/usr/bin/env bash
# Deploy the AWS leg: the Strands/a2a-sdk agent on Bedrock AgentCore Runtime,
# plus the federated role the GCP-hosted master assumes to reach it.
#
#   ./infra/deploy_aws.sh deploy        # ECR, ARM64 image, runtime, roles
#   ./infra/deploy_aws.sh trust-policy  # print the rendered trust policy
#   ./infra/deploy_aws.sh env           # env vars to add to the GCP master
#   ./infra/deploy_aws.sh verify        # negative controls -- run these
#   ./infra/deploy_aws.sh scope-test    # answers open question 2
#   ./infra/deploy_aws.sh url
#   ./infra/deploy_aws.sh destroy
#
# AgentCore Runtime, not Lambda. An agent runtime is where an AWS agent goes,
# and hosting this one on generic compute would make the mesh two agent
# runtimes and a function -- which concedes the premise the article rests on,
# and breaks comparability with the predecessor series' 18.8-25.1s
# hosted-runtime measurements.
#
# The A2A protocol contract (port 9000, root path, ARM64, GET /ping) is
# satisfied by infra/Dockerfile.aws and agents/serving.py unmodified.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-us-west-2}"
RUNTIME="${RUNTIME:-currency_aws}"
ECR_REPO="${ECR_REPO:-currency-mesh}"
ROLE_EXEC="${ROLE_EXEC:-currency-aws-agentcore-exec}"
ROLE_FEDERATED="${ROLE_FEDERATED:-currency-aws-federated}"
# Overridable so the script works before a fresh login picks up the docker
# group: DOCKER="sudo -u $USER -g docker docker" ./infra/deploy_aws.sh deploy
DOCKER="${DOCKER:-docker}"

# Caller-chosen, and matched by the trust policy's :oaud condition. It is not
# authorization; the :sub condition is what binds this to one identity.
AUDIENCE="${AWS_A2A_AUDIENCE:-sts.amazonaws.com}"

GCP_PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
MASTER_SA="${COORDINATOR_SA:-currency-coordinator@${GCP_PROJECT}.iam.gserviceaccount.com}"

account_id() { aws sts get-caller-identity --query Account --output text; }

runtime_arn() {
  aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${RUNTIME}'].agentRuntimeArn | [0]" \
    --output text
}

# AgentCore mounts the A2A server under a path built from the URL-encoded ARN.
# The card lives beneath the same path, which is why discovery is privileged
# here in exactly the way it is on Cloud Run: one resource, one authorization.
runtime_url() {
  local arn escaped
  arn="$(runtime_arn)"
  [[ "$arn" == "None" || -z "$arn" ]] && { echo "runtime not deployed" >&2; exit 1; }
  escaped="$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""))' "$arn")"
  echo "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${escaped}/invocations/"
}

master_sa_unique_id() {
  local id
  id="$(gcloud iam service-accounts describe "$MASTER_SA" \
        --format='value(uniqueId)' 2>/dev/null || true)"
  if [[ -z "$id" ]]; then
    echo "cannot read the numeric unique ID of ${MASTER_SA}." >&2
    echo "The trust policy must pin :sub to that number, never to the email --" >&2
    echo "an email can be freed and re-bound to a different principal." >&2
    exit 1
  fi
  echo "$id"
}

render_trust_policy() {
  AUDIENCE="$AUDIENCE" MASTER_SA_UNIQUE_ID="$(master_sa_unique_id)" \
    python3 - "$REPO/infra/aws-trust-policy.json" <<'PY'
import json, os, sys

with open(sys.argv[1]) as handle:
    policy = json.load(handle)

# The _comment block documents the traps for a human reader; IAM rejects it.
policy.pop("_comment", None)
condition = policy["Statement"][0]["Condition"]["StringEquals"]
condition["accounts.google.com:oaud"] = os.environ["AUDIENCE"]
condition["accounts.google.com:sub"] = os.environ["MASTER_SA_UNIQUE_ID"]
print(json.dumps(policy, indent=2))
PY
}

build_and_push() {
  local account image
  account="$(account_id)"
  image="${account}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:aws-agent"

  # Everything here goes to stderr except the final echo. The caller does
  # image="$(build_and_push)", so a single stray stdout line -- docker login's
  # "Login Succeeded", every push layer -- ends up concatenated into the image
  # URI and is then passed to create-agent-runtime as a containerUri. That
  # fails in a way that names neither docker nor the capture.
  {
    aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$REGION" \
      >/dev/null 2>&1 || \
      aws ecr create-repository --repository-name "$ECR_REPO" --region "$REGION" >/dev/null

    aws ecr get-login-password --region "$REGION" \
      | $DOCKER login --username AWS --password-stdin \
          "${account}.dkr.ecr.${REGION}.amazonaws.com"

    # ARM64 is required by AgentCore, not preferred. On an x86 host this needs
    # buildx plus qemu (binfmt_misc must be mounted); a silently-amd64 image is
    # accepted by ECR and then fails at runtime with an exec-format error
    # naming neither the architecture nor the image.
    $DOCKER buildx build --platform linux/arm64 --load \
      -f "$REPO/infra/Dockerfile.aws" -t "$image" "$REPO"
    $DOCKER push "$image"
  } >&2

  echo "$image"
}

ensure_exec_role() {
  aws iam get-role --role-name "$ROLE_EXEC" >/dev/null 2>&1 && return 0
  local account; account="$(account_id)"

  aws iam create-role --role-name "$ROLE_EXEC" \
    --assume-role-policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Principal\": {\"Service\": \"bedrock-agentcore.amazonaws.com\"},
        \"Action\": \"sts:AssumeRole\",
        \"Condition\": {\"StringEquals\": {\"aws:SourceAccount\": \"${account}\"}}
      }]
    }" >/dev/null

  aws iam put-role-policy --role-name "$ROLE_EXEC" \
    --policy-name agentcore-runtime \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [
        {
          \"Effect\": \"Allow\",
          \"Action\": [
            \"ecr:GetAuthorizationToken\",
            \"ecr:BatchGetImage\",
            \"ecr:GetDownloadUrlForLayer\"
          ],
          \"Resource\": \"*\"
        },
        {
          \"Effect\": \"Allow\",
          \"Action\": [
            \"logs:CreateLogGroup\",
            \"logs:CreateLogStream\",
            \"logs:PutLogEvents\"
          ],
          \"Resource\": \"arn:aws:logs:${REGION}:${account}:*\"
        }
      ]
    }"
  echo "waiting for ${ROLE_EXEC} to propagate"
  sleep 15
}

ensure_federated_role() {
  local policy account arn
  policy="$(render_trust_policy)"
  account="$(account_id)"

  if aws iam get-role --role-name "$ROLE_FEDERATED" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$ROLE_FEDERATED" \
      --policy-document "$policy"
  else
    aws iam create-role --role-name "$ROLE_FEDERATED" \
      --description "Assumed by the GCP-hosted master via AssumeRoleWithWebIdentity" \
      --assume-role-policy-document "$policy" >/dev/null
    echo "waiting for ${ROLE_FEDERATED} to propagate"
    sleep 15
  fi

  arn="$(runtime_arn)"

  # Scoped to this runtime and its children -- OPEN QUESTION 2, and it is now
  # ANSWERED (2026-08-07, verified against the deployed policy).
  #
  # The predecessor read was that the resource scope was too narrow: in
  # adk-bedrock-a2a-currency, runtime/<id> and runtime/<id>/* were denied 403 on
  # the *agent-card fetch* while the invoke worked, and only Resource:"*"
  # succeeded. That diagnosis was wrong. The card fetch is a **separate IAM
  # action** -- bedrock-agentcore:GetAgentCard -- and granting it against the
  # same two narrow resources is what fixes it. Widening the resource "worked"
  # because a wildcard resource on a wildcard-adjacent action set happens to
  # cover the action nobody had named.
  #
  # So this mesh ships a scoped policy, and Resource:"*" is NOT required. If it
  # ever were, that would have to be disclosed rather than glossed -- but it is
  # not, and the reason the earlier project believed otherwise is that a missing
  # *action* and a too-narrow *resource* produce the same 403, with data-plane
  # denials absent from CloudTrail by default to make it near-invisible.
  aws iam put-role-policy --role-name "$ROLE_FEDERATED" \
    --policy-name invoke-currency-agent \
    --policy-document "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Effect\": \"Allow\",
        \"Action\": [
          \"bedrock-agentcore:InvokeAgentRuntime\",
          \"bedrock-agentcore:GetAgentCard\"
        ],
        \"Resource\": [\"${arn}\", \"${arn}/*\"]
      }]
    }"
  echo "granted InvokeAgentRuntime + GetAgentCard scoped to ${arn} and ${arn}/*"
}

deploy() {
  local image account arn url
  image="$(build_and_push)"
  account="$(account_id)"
  ensure_exec_role

  arn="$(runtime_arn)"
  if [[ "$arn" == "None" || -z "$arn" ]]; then
    aws bedrock-agentcore-control create-agent-runtime --region "$REGION" \
      --agent-runtime-name "$RUNTIME" \
      --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
      --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
      --network-configuration 'networkMode=PUBLIC' \
      --protocol-configuration 'serverProtocol=A2A' >/dev/null
    # --authorizer-configuration is OMITTED, which is what selects SigV4.
    # It is a tagged union whose only member is customJWTAuthorizer, so an
    # empty '{}' is rejected with "Must set one of the following keys for
    # tagged union structure authorizerConfiguration" -- an error that reads
    # like the field is required when in fact it must be absent.
  else
    aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
      --agent-runtime-id "$(basename "$arn")" \
      --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
      --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
      --network-configuration 'networkMode=PUBLIC' \
      --protocol-configuration 'serverProtocol=A2A' >/dev/null
  fi

  arn="$(runtime_arn)"
  url="$(runtime_url)"

  # Two-phase by necessity: the card must advertise the invocations URL, which
  # is derived from an ARN that does not exist until the runtime does.
  aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
    --agent-runtime-id "$(basename "$arn")" \
    --agent-runtime-artifact "containerConfiguration={containerUri=${image}}" \
    --role-arn "arn:aws:iam::${account}:role/${ROLE_EXEC}" \
    --network-configuration 'networkMode=PUBLIC' \
    --protocol-configuration 'serverProtocol=A2A' \
    --environment-variables "PUBLIC_URL=${url},CURRENCY_MODEL_MODE=direct,HOST=0.0.0.0,PORT=9000" \
    >/dev/null

  ensure_federated_role

  echo
  echo "runtime arn : $arn"
  echo "runtime url : $url"
  echo
  echo "Now run: ./infra/deploy_aws.sh verify"
}

env_block() {
  cat <<EOF
# Add to the GCP master (Cloud Run job) to reach this agent:
AWS_A2A_ENDPOINT=$(runtime_url)
AWS_A2A_AUTH=aws-sigv4
AWS_A2A_ROLE_ARN=$(aws iam get-role --role-name "$ROLE_FEDERATED" --query Role.Arn --output text)
AWS_A2A_REGION=${REGION}
AWS_A2A_AUDIENCE=${AUDIENCE}
# bedrock-agentcore is already the default in coordinator/auth.py, and is what
# adds the mandatory X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header --
# inside the signature, so it cannot be stripped or swapped in transit.
AWS_A2A_SIGNING_SERVICE=bedrock-agentcore
EOF
}

verify() {
  local url
  url="$(runtime_url)"

  echo "an authenticated leg is unproven without negative controls."
  echo

  echo "1. no signature -> expect 403 (SigV4 runtimes return ACCESS_DENIED,"
  echo "   with no WWW-Authenticate header -- unlike an OAuth-configured one,"
  echo "   which 401s and advertises its authorization server)"
  curl -s -o /dev/null -w '   %{http_code}\n' -X POST "$url"

  echo "2. unsigned agent-card fetch -> expect 403"
  curl -s -o /dev/null -w '   %{http_code}\n' "${url}.well-known/agent-card.json"

  echo "3. signed but with no session header -> AgentCore requires it on every"
  echo "   request; this is the control for that, not for auth"
  echo "   (run from the master; a laptop cannot mint the token this role accepts)"

  echo
  echo "The positive control is the GCP master itself -- it is the only"
  echo "principal that can mint the token this role's trust policy accepts."
}

# OPEN QUESTION 2, answered 2026-08-07. Kept as a verb because the answer is a
# claim about a live policy, and a claim about a live policy should be
# re-checkable rather than remembered.
scope_test() {
  echo "OPEN QUESTION 2: does a scoped policy permit the agent-card fetch, or"
  echo "is Resource:\"*\" required?"
  echo
  echo "ANSWERED: scoped works. Resource:\"*\" is NOT required. The predecessor"
  echo "diagnosis -- too-narrow resource -- was wrong; the card fetch is a"
  echo "separate ACTION, bedrock-agentcore:GetAgentCard, against the same two"
  echo "narrow resources. A missing action and a too-narrow resource both"
  echo "produce 403, which is why widening the resource looked like the fix."
  echo
  echo "The deployed policy, live:"
  aws iam get-role-policy --role-name "$ROLE_FEDERATED" \
    --policy-name invoke-currency-agent \
    --query 'PolicyDocument.Statement[0].{Action:Action,Resource:Resource}' \
    --output json 2>/dev/null | sed 's/^/  /'
  echo
  echo "If Resource ever reads \"*\" here, this mesh is shipping a broad policy"
  echo "and that must be disclosed, not glossed."
  echo
  echo "Note: data-plane denials do not reach CloudTrail by default, so a 403"
  echo "from either cause is near-invisible until you enable them."
}

destroy() {
  local arn; arn="$(runtime_arn)"
  if [[ "$arn" != "None" && -n "$arn" ]]; then
    aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" \
      --agent-runtime-id "$(basename "$arn")" >/dev/null 2>&1 || true
  fi
  aws iam delete-role-policy --role-name "$ROLE_FEDERATED" --policy-name invoke-currency-agent 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE_FEDERATED" 2>/dev/null || true
  aws iam delete-role-policy --role-name "$ROLE_EXEC" --policy-name agentcore-runtime 2>/dev/null || true
  aws iam delete-role --role-name "$ROLE_EXEC" 2>/dev/null || true
}

# Roles without the image, so IAM can be reviewed before anything is built and
# so a propagation failure is not mistaken for a build failure. ensure_federated
# needs the runtime ARN for its resource scope, so it is skipped until the
# runtime exists -- run `deploy` after this to complete it.
roles_only() {
  ensure_exec_role
  local arn; arn="$(runtime_arn)"
  if [[ "$arn" == "None" || -z "$arn" ]]; then
    echo "runtime not created yet; federated role will be scoped by 'deploy'."
    echo "creating it now with the trust policy only, no invoke permission:"
    local policy; policy="$(render_trust_policy)"
    if aws iam get-role --role-name "$ROLE_FEDERATED" >/dev/null 2>&1; then
      aws iam update-assume-role-policy --role-name "$ROLE_FEDERATED" --policy-document "$policy"
      echo "updated trust policy on ${ROLE_FEDERATED}"
    else
      aws iam create-role --role-name "$ROLE_FEDERATED" \
        --description "Assumed by the GCP-hosted master via AssumeRoleWithWebIdentity" \
        --assume-role-policy-document "$policy" >/dev/null
      echo "created ${ROLE_FEDERATED}"
    fi
    render_trust_policy
  else
    ensure_federated_role
  fi
}

case "${1:-deploy}" in
  deploy) deploy ;;
  roles) roles_only ;;
  trust-policy) render_trust_policy ;;
  env) env_block ;;
  verify) verify ;;
  scope-test) scope_test ;;
  url) runtime_url ;;
  destroy) destroy ;;
  *) echo "usage: $0 {deploy|roles|trust-policy|env|verify|scope-test|url|destroy}" >&2; exit 2 ;;
esac
