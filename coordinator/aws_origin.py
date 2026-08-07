"""The outbound legs of an **AWS-rooted** master.

``coordinator.auth`` implements the legs that start from Google, and every one
of them begins at the same metadata mint. This module implements the two that
start from AWS, and the reason it is a separate file is that they do not share
that root -- they start from an AWS role, which changes what is possible:

    AWS -> GCP    signed GetCallerIdentity -> GCP STS -> impersonate -> ID token
    AWS -> Azure  client secret                            **not keyless**

That asymmetry is the experiment in ``docs/DEPLOYMENT_PLAN.md`` step 5. Holding
the three agents fixed and moving the master is what makes it attributable, and
this module is the half of the seam that the AWS-rooted run needs.

Why AWS -> GCP is keyless and AWS -> Azure is not
-------------------------------------------------
Google's Workload Identity Federation accepts an **AWS-shaped** subject token:
a SigV4-signed ``GetCallerIdentity`` request, serialised and handed over
unsent. Google replays it against AWS STS to learn who signed it. No JWT is
involved, so this leg does **not** depend on whether an AWS runtime can mint
OIDC -- which is the open question that blocks so much else here.

Entra has no equivalent. Its Federated Identity Credential wants a JWT
assertion from an issuer with OIDC discovery, and an ECS task role or Lambda
execution role is not one. Outside EKS/IRSA or Cognito there is nothing for AWS
to present, so this leg falls back to a client secret and the mesh stops being
secretless. That is a measured boundary, not an implementation shortcut, and
``EntraClientSecretAuth`` is loud about it for exactly that reason.
"""

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx

from coordinator.auth import (
    _auth_error,
    _AwsCredentials,
    _CachedToken,
    _entra_detail,
    _log_provider_response,
    _sign_request,
)
from coordinator.errors import AdapterError, FailureKind

log = logging.getLogger("coordinator.aws_origin")

#: Google requires this header on the GetCallerIdentity subject token, naming
#: the provider it is destined for, and requires it inside the signature.
_TARGET_RESOURCE_HEADER = "x-goog-cloud-target-resource"

_GOOGLE_STS_URL = "https://sts.googleapis.com/v1/token"
_IAM_CREDENTIALS_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateIdToken"
)
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: ECS task roles are served here; the relative URI arrives in the environment.
_ECS_CREDENTIALS_HOST = "http://169.254.170.2"


class AwsWorkloadCredentials:
    """Resolves the AWS role credentials of the runtime the master is running on.

    Three sources, in the order the AWS SDKs themselves try them:

    1. ``AWS_CONTAINER_CREDENTIALS_FULL_URI`` -- ECS with a full endpoint.
    2. ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` -- ordinary ECS task role.
    3. ``AWS_ACCESS_KEY_ID`` and friends -- Lambda, and local testing.

    IMDS (``169.254.169.254``) is deliberately not implemented. The master runs
    on ECS or Lambda by design -- see the scoping note in step 5 -- and a silent
    IMDS fallback is how a run that should have failed loudly instead picks up
    an EC2 instance profile nobody meant to grant it.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _AwsCredentials | None = None

    async def credentials(self) -> _AwsCredentials:
        if self._cache is not None and self._cache.usable:
            return self._cache
        self._cache = await self._resolve()
        return self._cache

    async def _resolve(self) -> _AwsCredentials:
        full_uri = os.getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        relative_uri = os.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")

        if full_uri:
            return await self._from_container_endpoint(full_uri)
        if relative_uri:
            return await self._from_container_endpoint(f"{_ECS_CREDENTIALS_HOST}{relative_uri}")

        key_id = os.getenv("AWS_ACCESS_KEY_ID")
        secret = os.getenv("AWS_SECRET_ACCESS_KEY")
        if key_id and secret:
            # Lambda populates these from the execution role and rotates them;
            # a session token is absent only for long-lived user keys, which
            # this path should never be handed.
            token = os.getenv("AWS_SESSION_TOKEN")
            if not token:
                log.warning(
                    "AWS_SESSION_TOKEN is unset: these look like long-lived user keys "
                    "rather than a role. The master is supposed to run under a task or "
                    "execution role."
                )
            return _AwsCredentials(
                access_key_id=key_id,
                secret_access_key=secret,
                session_token=token or "",
                # Env credentials carry no expiry we can read; re-resolving is
                # cheap and reading a stale key is not.
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

        raise _auth_error(
            "aws workload credentials",
            "no AWS role credentials found. Expected one of "
            "AWS_CONTAINER_CREDENTIALS_FULL_URI, AWS_CONTAINER_CREDENTIALS_RELATIVE_URI "
            "(ECS), or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (Lambda). The master "
            "must run on an AWS runtime with a role attached; IMDS is not consulted.",
        )

    async def _from_container_endpoint(self, url: str) -> _AwsCredentials:
        boundary = f"aws container credentials ({url})"
        headers = {}
        # ECS agent v1.4+ requires this when a full URI is used.
        auth_token = os.getenv("AWS_CONTAINER_AUTHORIZATION_TOKEN")
        if auth_token:
            headers["Authorization"] = auth_token

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach the credentials endpoint: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, f"{response.status_code}: {response.text}")

        payload = response.json()
        try:
            return _AwsCredentials(
                access_key_id=payload["AccessKeyId"],
                secret_access_key=payload["SecretAccessKey"],
                session_token=payload["Token"],
                expires_at=datetime.fromisoformat(payload["Expiration"]),
            )
        except KeyError as exc:
            raise _auth_error(
                boundary, f"credentials response is missing {exc.args[0]}: {response.text}"
            ) from exc


class GcpFederatedIdTokenAuth(httpx.Auth):
    """AWS -> GCP. Keyless, and notably without minting a JWT anywhere.

    Four steps, of which only the last two leave the process::

        1. resolve the AWS role credentials
        2. build and sign a GetCallerIdentity request -- and do not send it
        3. hand that to Google STS, which replays it against AWS to learn the caller
        4. impersonate the target service account for an ID token

    Step 2 is the part worth understanding: the *subject token* is a signed HTTP
    request, serialised as JSON and never issued by us. Google issues it. That
    is why this leg works from a runtime that cannot mint OIDC at all, and it is
    the asymmetry that makes AWS -> GCP cheap while AWS -> Azure is not.

    Step 4 exists because Cloud Run validates an **ID token** whose audience is
    its own service URL, and the STS exchange yields an *access* token. The
    federated principal therefore needs ``roles/iam.serviceAccountTokenCreator``
    on the service account it impersonates -- a grant that is easy to forget and
    which denies with a 403 naming the *service account*, not the pool.

    Cost, relative to the GCP-rooted equivalent: that leg is one hop to the
    metadata server. This is two network round trips before the call, plus a
    local signature.
    """

    def __init__(
        self,
        *,
        audience: str,
        pool_provider: str,
        service_account: str,
        region: str,
        credentials: AwsWorkloadCredentials | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._audience = audience
        self._pool_provider = pool_provider
        self._service_account = service_account
        self._region = region
        self._credentials = credentials or AwsWorkloadCredentials(transport=transport)
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _CachedToken | None = None

    @property
    def mode(self) -> str:
        return "gcp-wif-aws"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._id_token()}"
        yield request

    async def _id_token(self) -> str:
        if self._cache is not None and self._cache.usable:
            return self._cache.value

        federated = await self._federated_access_token()
        token = await self._impersonated_id_token(federated)
        # generateIdToken returns no expiry; the ID tokens it mints last an
        # hour, and the skew in _CachedToken keeps us clear of the edge.
        self._cache = _CachedToken(token, datetime.now(UTC) + timedelta(seconds=3600))
        return token

    async def _federated_access_token(self) -> str:
        subject_token = await self._subject_token()
        boundary = f"google sts token exchange (provider={self._pool_provider})"
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": self._pool_provider,
            "scope": _CLOUD_PLATFORM_SCOPE,
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(_GOOGLE_STS_URL, data=form)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {_GOOGLE_STS_URL}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, _google_sts_detail(response))

        token = response.json().get("access_token")
        if not token:
            raise _auth_error(boundary, f"no access_token in the response: {response.text}")
        return token

    async def _subject_token(self) -> str:
        """Build the signed-but-unsent ``GetCallerIdentity`` Google will replay.

        The serialisation is Google's, not AWS's: a JSON object of url, method
        and a *list* of key/value header pairs, URL-encoded whole. The header
        list must include the signed ``x-goog-cloud-target-resource``, or the
        exchange is refused with a message that does not mention it.
        """
        credentials = await self._credentials.credentials()
        url = (
            f"https://sts.{self._region}.amazonaws.com/"
            "?Action=GetCallerIdentity&Version=2011-06-15"
        )
        request = httpx.Request("POST", url)
        request.headers[_TARGET_RESOURCE_HEADER] = self._pool_provider
        _sign_request(
            request,
            credentials=credentials,
            region=self._region,
            service="sts",
            now=datetime.now(UTC),
            extra_signed_headers=(_TARGET_RESOURCE_HEADER,),
        )

        payload = {
            "url": url,
            "method": "POST",
            "headers": [
                {"key": name, "value": value}
                for name, value in request.headers.items()
                # httpx adds these; they are not part of what was signed and
                # Google rejects a header list that disagrees with the signature.
                if name.lower() not in ("accept", "accept-encoding", "connection")
            ],
        }
        return quote(json.dumps(payload))

    async def _impersonated_id_token(self, federated_token: str) -> str:
        boundary = f"iamcredentials generateIdToken (sa={self._service_account})"
        url = _IAM_CREDENTIALS_URL.format(sa=self._service_account)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {federated_token}"},
                    json={"audience": self._audience, "includeEmail": True},
                )
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {url}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, _impersonation_detail(response, self._service_account))

        token = response.json().get("token")
        if not token:
            raise _auth_error(boundary, f"no token in the response: {response.text}")
        return token


def _google_sts_detail(response: httpx.Response) -> str:
    """Name Google's STS error, with the discriminator this leg actually needs.

    The mirror of ``_sts_detail`` in ``coordinator.auth``: ``invalid_grant``
    means the subject token itself was not accepted, ``invalid_request`` or
    ``permission_denied`` means it was read and the pool's attribute condition
    rejected the principal. Provider-setup bug versus condition bug, again.
    """
    try:
        payload = response.json()
    except ValueError:
        return f"{response.status_code}: {response.text}"

    code = payload.get("error", "unknown_error")
    description = payload.get("error_description", response.text)
    hint = {
        "invalid_grant": (
            "the GetCallerIdentity subject token was rejected outright -- check that "
            "the provider's AWS account ID matches the role actually in use, and that "
            f"{_TARGET_RESOURCE_HEADER} was inside the signature rather than merely present"
        ),
        "invalid_request": (
            "the token was read but the request was malformed -- most often the "
            "subject_token is not URL-encoded, or the header list disagrees with "
            "what the signature covered"
        ),
        "permission_denied": (
            "the caller was identified and the pool's attribute condition did not "
            "match it -- check attribute.aws_role against the assumed-role ARN, "
            "which is not the same string as the role ARN you granted"
        ),
    }.get(code)
    detail = f"{response.status_code} {code}: {description}"
    return f"{detail} [{hint}]" if hint else detail


def _impersonation_detail(response: httpx.Response, service_account: str) -> str:
    """A 403 here names the service account, not the federation, and misleads."""
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message", response.text)
    except ValueError:
        message = response.text

    if response.status_code in (401, 403):
        return (
            f"{response.status_code}: {message} [the federated principal needs "
            f"roles/iam.serviceAccountTokenCreator on {service_account}. This denial "
            "is about impersonation, not about the workload identity pool -- the STS "
            "exchange before it already succeeded.]"
        )
    return f"{response.status_code}: {message}"


class EntraClientSecretAuth(httpx.Auth):
    """AWS -> Azure. **This leg is not keyless, and that is the finding.**

    Entra's Federated Identity Credential needs a JWT assertion from an issuer
    it can discover. An ECS task role and a Lambda execution role are not OIDC
    issuers, and AWS will not mint a token for an arbitrary audience outside
    EKS/IRSA or Cognito. There is nothing to federate *with*, so an AWS-rooted
    master falls back to a client secret.

    This class exists to make the boundary measurable rather than to make the
    mesh work. It logs a warning on construction because a silent fallback is
    exactly how "we deployed a secretless mesh" gets written about a mesh with a
    secret in it -- and ``MeshRun.auth_modes`` reports ``entra-client-secret``,
    which must never be mistaken for ``entra-fic``.

    If AWS ever gains an OIDC issuer for ordinary compute, this whole class is
    deleted and replaced by the FIC path in ``coordinator.auth``.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        scope: str,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _CachedToken | None = None
        log.warning(
            "AWS -> Azure is using a client secret (client_id=%s). This leg is NOT "
            "keyless: Entra requires a JWT assertion and no AWS runtime role can mint "
            "one. Any claim of a secretless mesh must exclude it.",
            client_id,
        )

    @property
    def mode(self) -> str:
        return "entra-client-secret"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._access_token()}"
        yield request

    async def _access_token(self) -> str:
        if self._cache is not None and self._cache.usable:
            return self._cache.value

        boundary = f"entra client credentials (tenant={self._tenant_id}, client={self._client_id})"
        url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": self._scope,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(url, data=form)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {url}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, _entra_detail(response))

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise _auth_error(boundary, f"no access_token in the response: {response.text}")

        expires_in = int(payload.get("expires_in", 3600))
        self._cache = _CachedToken(token, datetime.now(UTC) + timedelta(seconds=expires_in))
        return token


def _require(peer: str, mode: str, name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"{peer} is configured with {name.rsplit('_', 1)[0]}"
            f"_A2A_AUTH={mode} but {name} is unset",
        )
    return value


def build(peer: str, mode: str, endpoint: str) -> httpx.Auth:
    """Construct an AWS-rooted credential from the per-peer environment.

    Called by ``coordinator.auth.credentials_for``; the registry stays there so
    that one function still answers "how does this leg authenticate", whichever
    cloud the master happens to be running in.
    """
    prefix = peer.upper()

    if mode == "gcp-wif-aws":
        return GcpFederatedIdTokenAuth(
            audience=os.getenv(f"{prefix}_A2A_AUDIENCE") or _service_root(endpoint),
            pool_provider=_require(peer, mode, f"{prefix}_A2A_POOL_PROVIDER"),
            service_account=_require(peer, mode, f"{prefix}_A2A_SERVICE_ACCOUNT"),
            region=_require(peer, mode, f"{prefix}_A2A_REGION"),
        )

    client_id = _require(peer, mode, f"{prefix}_A2A_CLIENT_ID")
    return EntraClientSecretAuth(
        tenant_id=_require(peer, mode, f"{prefix}_A2A_TENANT_ID"),
        client_id=client_id,
        client_secret=_require(peer, mode, f"{prefix}_A2A_CLIENT_SECRET"),
        scope=os.getenv(f"{prefix}_A2A_SCOPE") or f"{client_id}/.default",
    )


def _service_root(endpoint: str) -> str:
    from coordinator.auth import _service_root as root

    return root(endpoint)
