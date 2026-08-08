"""One credential seam for every outbound leg of the mesh.

The mesh is asymmetric in exactly one way that matters: every callee here
consumes *external* OIDC -- AWS IAM OIDC providers, Entra Federated Identity
Credentials, AgentCore ``CUSTOM_JWT`` -- but only some *callers* can mint an
OIDC token. Cloud Run can, with an arbitrary audience, which is why the
coordinator runs there and why all three legs below start from the same
metadata mint. See ``docs/DEPLOYMENT_PLAN.md``.

Three mechanisms, one shape::

    GCP -> GCP    Google ID token           Authorization: Bearer <jwt>
    GCP -> AWS    STS AssumeRoleWithWebIdentity -> SigV4 signature
    GCP -> Azure  Entra FIC token exchange  Authorization: Bearer <jwt>

``httpx.Auth`` is the shape because it is the only one that spans both: a
bearer header and a signature over the request body are the same object to
httpx, and all three vendor client SDKs accept an ``httpx.AsyncClient``. That
also means the **agent-card fetch is authenticated by the same credential as
the call** -- discovery is privileged, and a card fetch that 403s while the
call would have succeeded is the single most confusing failure in this space.

No vendor SDK is imported here. The coordinator calls three clouds; making it
depend on three clouds' auth libraries to do so would be the wrong trade. It
is httpx plus the standard library.

Logging
-------
Every provider response is logged at its boundary, in full, on failure. This
is deliberate and it is worth more than the federation work itself: in the
predecessor series nothing cost more time than auth errors that could not be
read -- an adapter that reported an HTTP status and discarded the STS body,
and an error that travelled back as a *tool result* and got paraphrased by the
model into "an issue with the web identity token". A raised message is not an
observable. Raise and log.

Successful responses are logged without their token material.
"""

import hashlib
import hmac
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import httpx

from coordinator.errors import AdapterError, FailureKind

log = logging.getLogger("coordinator.auth")

#: Refresh this far before actual expiry, so a token cannot die in flight.
_EXPIRY_SKEW = timedelta(seconds=60)

#: Audience Entra requires on the assertion presented to a Federated Identity
#: Credential. Not configurable: Entra rejects anything else.
ENTRA_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"

_METADATA_IDENTITY_PATH = (
    "/computeMetadata/v1/instance/service-accounts/default/identity"
)
_STS_NS = "{https://sts.amazonaws.com/doc/2011-06-15/}"


def _auth_error(boundary: str, detail: str) -> AdapterError:
    return AdapterError(FailureKind.AUTHENTICATION, f"{boundary}: {detail}")


def _log_provider_response(boundary: str, response: httpx.Response) -> None:
    """Log what the identity provider actually said.

    On failure the body is logged whole and unparsed. Truncating it is how you
    lose the one line that names the condition that did not match.
    """
    if response.is_success:
        log.info("%s -> %s %s", boundary, response.status_code, response.reason_phrase)
        return
    log.error(
        "%s -> %s %s\nrequest: %s %s\nbody: %s",
        boundary,
        response.status_code,
        response.reason_phrase,
        response.request.method,
        response.request.url,
        response.text,
    )


@dataclass(frozen=True)
class _CachedToken:
    value: str
    expires_at: datetime

    @property
    def usable(self) -> bool:
        return datetime.now(UTC) + _EXPIRY_SKEW < self.expires_at


class WorkloadIdentity:
    """Mints Google OIDC tokens for an arbitrary audience from the metadata server.

    Every leg starts here, including the two that end up somewhere other than
    Google: the AWS leg presents this token to STS and the Azure leg presents
    it to Entra, because both federate against ``accounts.google.com`` and the
    token's ``sub`` is the service account's immutable numeric ID.

    ``format=full`` is mandatory. Without it Google trims the token and omits
    the ``email`` claim, and the trust conditions that read it stop matching --
    with no error that says so.
    """

    def __init__(
        self,
        *,
        metadata_host: str | None = None,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        host = metadata_host or os.getenv("GCE_METADATA_HOST", "metadata.google.internal")
        self._base_url = f"http://{host}"
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: dict[str, _CachedToken] = {}

    async def id_token(self, audience: str) -> str:
        cached = self._cache.get(audience)
        if cached is not None and cached.usable:
            return cached.value

        boundary = f"gcp metadata mint (audience={audience})"
        url = f"{self._base_url}{_METADATA_IDENTITY_PATH}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.get(
                    url,
                    params={"audience": audience, "format": "full"},
                    headers={"Metadata-Flavor": "Google"},
                )
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(
                boundary,
                f"cannot reach the metadata server at {url}: {exc}. The coordinator "
                "must run on a Google runtime (Cloud Run, GCE) to mint workload "
                "OIDC tokens; there is no local equivalent.",
            ) from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, f"{response.status_code}: {response.text}")

        token = response.text.strip()
        if not token:
            raise _auth_error(boundary, "metadata server returned an empty token")

        self._cache[audience] = _CachedToken(token, _jwt_expiry(token))
        return token


def _jwt_expiry(token: str) -> datetime:
    """Read ``exp`` out of a JWT without verifying it.

    We minted this token seconds ago from a trusted local endpoint; the only
    question is when to mint the next one. A token we cannot parse is treated
    as short-lived rather than rejected.
    """
    import base64
    import json

    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromtimestamp(int(claims["exp"]), UTC)
    except Exception:  # noqa: BLE001 - an unparseable token just refreshes sooner
        log.warning("could not read exp from the minted token; caching for 5 minutes")
        return datetime.now(UTC) + timedelta(minutes=5)


class GoogleIdTokenAuth(httpx.Auth):
    """GCP -> GCP. The trivial leg: an ID token, and ``roles/run.invoker``.

    The audience is the receiving Cloud Run service's URL, which is what that
    service's own validator checks. Nothing here is federation; it is the
    control case that proves the seam works before the interesting legs use it.
    """

    def __init__(self, audience: str, *, identity: WorkloadIdentity | None = None) -> None:
        self._audience = audience
        self._identity = identity or WorkloadIdentity()

    @property
    def mode(self) -> str:
        return "google-id-token"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        token = await self._identity.id_token(self._audience)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class EntraFederatedAuth(httpx.Auth):
    """GCP -> Azure. A Google token becomes an Entra token, keylessly.

    The Federated Identity Credential on the app registration must have
    issuer ``https://accounts.google.com``, audience ``api://AzureADTokenExchange``,
    and subject = the service account's **numeric unique ID**. Not its email:
    an email can be freed and re-bound to a different principal, so a subject
    condition written against one is a condition that can be inherited.

    Note the asymmetry with AWS, which is the trap: AWS federates with Google
    natively and creating an explicit IAM OIDC provider for ``accounts.google.com``
    *breaks* it, whereas for Entra you must create the credential explicitly.
    Opposite rules, identical-looking task.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        scope: str,
        identity: WorkloadIdentity | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._scope = scope
        self._identity = identity or WorkloadIdentity()
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _CachedToken | None = None

    @property
    def mode(self) -> str:
        return "entra-fic"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._access_token()}"
        yield request

    async def _access_token(self) -> str:
        if self._cache is not None and self._cache.usable:
            return self._cache.value

        assertion = await self._identity.id_token(ENTRA_EXCHANGE_AUDIENCE)
        boundary = f"entra token exchange (tenant={self._tenant_id}, client={self._client_id})"
        url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "scope": self._scope,
            "client_assertion_type": (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ),
            "client_assertion": assertion,
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


def _entra_detail(response: httpx.Response) -> str:
    """Surface Entra's AADSTS code, which is the part that names the mismatch."""
    try:
        payload = response.json()
    except ValueError:
        return f"{response.status_code}: {response.text}"
    code = payload.get("error", "unknown_error")
    description = payload.get("error_description", response.text)
    return f"{response.status_code} {code}: {description}"


class AwsSigV4Auth(httpx.Auth):
    """GCP -> AWS. Metadata mint -> STS AssumeRoleWithWebIdentity -> SigV4.

    The mechanism proven in ``adk-bedrock-a2a-currency``; ported rather than
    reinvented. Two things about the IAM trust policy that cost real time and
    are invisible from here:

    - Do **not** create an IAM OIDC provider for ``accounts.google.com``. AWS
      federates with Google natively and adding one produces
      ``InvalidIdentityToken``.
    - The condition keys do not mean what they are named.
      ``accounts.google.com:oaud`` is the token's ``aud``;
      ``accounts.google.com:aud`` is its ``azp``, a number. An audience string
      in ``:aud`` can never match, and the denial does not say why.

    Audience alone is not authorization in any case -- it is caller-chosen, so
    it proves only that *some* Google identity minted the token. Pin ``:sub``
    to the service account's numeric ID as well.
    """

    #: SigV4 signs a hash of the body, so httpx must materialise it first.
    requires_request_body = True

    def __init__(
        self,
        *,
        role_arn: str,
        region: str,
        audience: str,
        service: str = "bedrock-agentcore",
        session_name: str = "currency-mesh-coordinator",
        extra_headers: dict[str, str] | None = None,
        identity: WorkloadIdentity | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._role_arn = role_arn
        self._region = region
        self._audience = audience
        self._service = service
        self._session_name = session_name
        self._extra_headers = extra_headers or {}
        self._identity = identity or WorkloadIdentity()
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _AwsCredentials | None = None

    @property
    def mode(self) -> str:
        return "aws-sigv4"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        credentials = await self._credentials()
        # Set before signing and named to the signer, so they fall inside the
        # signature. AgentCore's session header is required on every request
        # including the card fetch, and an unsigned x-amzn-* header is the kind
        # of thing a service may accept today and reject later.
        for name, value in self._extra_headers.items():
            request.headers[name] = value
        _sign_request(
            request,
            credentials=credentials,
            region=self._region,
            service=self._service,
            now=datetime.now(UTC),
            extra_signed_headers=tuple(self._extra_headers),
        )
        yield request

    async def _credentials(self) -> "_AwsCredentials":
        if self._cache is not None and self._cache.usable:
            return self._cache

        assertion = await self._identity.id_token(self._audience)
        boundary = f"aws sts AssumeRoleWithWebIdentity (role={self._role_arn})"
        url = f"https://sts.{self._region}.amazonaws.com/"
        form = {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": "2011-06-15",
            "RoleArn": self._role_arn,
            "RoleSessionName": self._session_name,
            "WebIdentityToken": assertion,
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
            raise _auth_error(boundary, _sts_detail(response))

        self._cache = _parse_sts_response(response.text, boundary)
        return self._cache


def _sts_detail(response: httpx.Response) -> str:
    """Name the STS error code, because it is the fastest discriminator there is.

    ``InvalidIdentityToken`` means the token could not be validated at all --
    a provider-setup bug. ``AccessDenied`` means it validated fine and your
    trust conditions did not match -- a condition bug. Those are different
    afternoons.
    """
    try:
        root = ElementTree.fromstring(response.text)
    except ElementTree.ParseError:
        return f"{response.status_code}: {response.text}"

    code = root.findtext(f".//{_STS_NS}Code") or root.findtext(".//Code")
    message = root.findtext(f".//{_STS_NS}Message") or root.findtext(".//Message")
    if code is None:
        return f"{response.status_code}: {response.text}"

    hint = {
        "InvalidIdentityToken": (
            "the token could not be validated at all -- check that no IAM OIDC "
            "provider exists for accounts.google.com (AWS federates with Google "
            "natively; an explicit provider breaks it) and that the mint used "
            "format=full"
        ),
        "AccessDenied": (
            "the token validated but the trust policy conditions did not match "
            "-- note that accounts.google.com:oaud is the token's aud while "
            "accounts.google.com:aud is its azp"
        ),
    }.get(code)
    detail = f"{response.status_code} {code}: {message}"
    return f"{detail} [{hint}]" if hint else detail


@dataclass(frozen=True)
class _AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: datetime

    @property
    def usable(self) -> bool:
        return datetime.now(UTC) + _EXPIRY_SKEW < self.expires_at


def _parse_expiry(value: str, boundary: str) -> datetime:
    """Read a provider's expiry timestamp as an aware UTC datetime.

    Two traps, both of which surface far from here if left alone. A value with
    no offset parses happily and then raises ``TypeError: can't compare
    offset-naive and offset-aware datetimes`` on the *next* call, inside
    ``usable`` -- a crash at a line that has nothing to do with the cause. And
    an unparseable value raises ``ValueError``, which is not an ``AdapterError``
    and so travels back as an unmapped exception rather than a named auth
    failure. Both providers document UTC, so a naive value is assumed UTC.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _auth_error(
            boundary, f"could not read the credential expiry {value!r}: {exc}"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_sts_response(body: str, boundary: str) -> _AwsCredentials:
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise _auth_error(boundary, f"unparseable STS response: {body}") from exc

    node = root.find(f".//{_STS_NS}Credentials")
    if node is None:
        raise _auth_error(boundary, f"no Credentials element in the STS response: {body}")

    def field(name: str) -> str:
        value = node.findtext(f"{_STS_NS}{name}")
        if not value:
            raise _auth_error(boundary, f"STS response is missing {name}: {body}")
        return value

    return _AwsCredentials(
        access_key_id=field("AccessKeyId"),
        secret_access_key=field("SecretAccessKey"),
        session_token=field("SessionToken"),
        expires_at=_parse_expiry(field("Expiration"), boundary),
    )


def _sign_request(
    request: httpx.Request,
    *,
    credentials: _AwsCredentials,
    region: str,
    service: str,
    now: datetime,
    extra_signed_headers: tuple[str, ...] = (),
) -> None:
    """Apply an AWS SigV4 signature to ``request`` in place.

    Implemented against the standard rather than pulled from botocore: the
    coordinator's whole point is that it reaches three clouds without carrying
    three clouds' SDKs.

    ``extra_signed_headers`` names headers already set on the request that must
    fall inside the signature. GCP's Workload Identity Federation needs this:
    it rejects a ``GetCallerIdentity`` subject token whose
    ``x-goog-cloud-target-resource`` header was not signed, because an unsigned
    one could be swapped for a different pool by anyone who replayed the token.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    url = request.url

    request.headers["host"] = url.netloc.decode("ascii")
    request.headers["x-amz-date"] = amz_date

    signed_names = ["host", "x-amz-date"]
    # Only temporary credentials carry a session token. Sending the header with
    # an empty value -- which is what the env-credentials path produces for
    # long-lived keys -- is signed faithfully and then rejected by AWS, with an
    # error about the signature rather than about the token.
    if credentials.session_token:
        request.headers["x-amz-security-token"] = credentials.session_token
        signed_names.append("x-amz-security-token")
    if "content-type" in request.headers:
        signed_names.append("content-type")
    signed_names.extend(name.lower() for name in extra_signed_headers)
    signed_names = sorted(set(signed_names))

    canonical_headers = "".join(
        f"{name}:{' '.join(request.headers[name].split())}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    payload_hash = hashlib.sha256(request.content or b"").hexdigest()

    # SigV4 requires each path segment to be URI-encoded *twice* for every
    # service except S3. url.path is percent-decoded, so encoding it once only
    # reproduces the raw path and the signature silently fails to match --
    # invisible on simple paths, fatal on AgentCore, whose invocations URL
    # embeds a percent-encoded ARN. Start from raw_path, which is already
    # encoded once, and encode it again.
    raw_path = url.raw_path.split(b"?", 1)[0].decode("ascii")
    canonical_request = "\n".join(
        [
            request.method,
            _canonical_path(raw_path),
            _canonical_query(url.query.decode("ascii")),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    signing_key = _signing_key(credentials.secret_access_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    request.headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _canonical_path(path: str) -> str:
    """Percent-encode the path, leaving ``/`` alone. An empty path signs as ``/``."""
    if not path:
        return "/"
    return quote(path, safe="/-._~")


def _canonical_query(query: str) -> str:
    """Sort query parameters by name then value, with each half re-encoded."""
    if not query:
        return ""
    pairs = []
    for item in query.split("&"):
        name, _, value = item.partition("=")
        pairs.append((_requote(name), _requote(value)))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _requote(value: str) -> str:
    from urllib.parse import unquote

    return quote(unquote(value), safe="-._~")


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    key = f"AWS4{secret}".encode()
    for part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

#: Auth modes a peer can be configured with, keyed by ``{PEER}_A2A_AUTH``.
#:
#: The first four are rooted in GCP and implemented above. The last two are
#: rooted in AWS and live in ``coordinator.aws_origin``; the registry below
#: dispatches to them so that one function still answers "how does this leg
#: authenticate", whichever cloud the master runs in.
AUTH_MODES = (
    "none",
    "google-id-token",
    "aws-sigv4",
    "entra-fic",
    "gcp-wif-aws",
    "entra-client-secret",
)

#: Modes that require no long-lived secret. ``entra-client-secret`` is the only
#: one that does, and the distinction is reported per leg rather than inferred,
#: because "which legs were keyless" is the claim this project has to back.
KEYLESS_MODES = frozenset(
    {"none", "google-id-token", "aws-sigv4", "entra-fic", "gcp-wif-aws"}
)

_AWS_ROOTED_MODES = frozenset({"gcp-wif-aws", "entra-client-secret"})


def _require(peer: str, mode: str, name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"{peer} is configured with {name.rsplit('_', 1)[0]}"
            f"_A2A_AUTH={mode} but {name} is unset",
        )
    return value


def credentials_for(
    peer: str,
    endpoint: str,
    *,
    identity: WorkloadIdentity | None = None,
) -> httpx.Auth | None:
    """Return the credential for one outbound leg, or ``None`` for an open peer.

    Configuration is per-peer and environmental, so the same coordinator image
    runs against the local mesh (every peer ``none``) and against the deployed
    mesh without a code change -- which is what keeps the local matrix a
    protocol instrument rather than an identity test.

        GCP_A2A_AUTH=google-id-token
        AWS_A2A_AUTH=aws-sigv4   AWS_A2A_ROLE_ARN=...  AWS_A2A_REGION=us-west-2
        AZURE_A2A_AUTH=entra-fic AZURE_A2A_TENANT_ID=... AZURE_A2A_CLIENT_ID=...

    With the master on AWS instead, the same peers are configured differently
    and nothing else in the mesh changes -- which is the point of step 5::

        GCP_A2A_AUTH=gcp-wif-aws GCP_A2A_POOL_PROVIDER=//iam.googleapis.com/...
                                 GCP_A2A_SERVICE_ACCOUNT=...  GCP_A2A_REGION=...
        AZURE_A2A_AUTH=entra-client-secret   AZURE_A2A_CLIENT_SECRET=...
    """
    prefix = peer.upper()
    mode = os.getenv(f"{prefix}_A2A_AUTH", "none").strip().lower()

    if mode == "none":
        return None
    if mode not in AUTH_MODES:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"unknown auth mode {mode!r} for peer {peer} (expected one of {AUTH_MODES})",
        )

    if mode in _AWS_ROOTED_MODES:
        # Imported here rather than at module scope: aws_origin imports the
        # signer and the cache types from this module, and a top-level import
        # would be circular.
        from coordinator import aws_origin

        return aws_origin.build(peer, mode, endpoint)

    identity = identity or WorkloadIdentity()

    if mode == "google-id-token":
        # Cloud Run validates the audience against its own service URL, so the
        # endpoint we dialled is the right default.
        audience = os.getenv(f"{prefix}_A2A_AUDIENCE") or _service_root(endpoint)
        return GoogleIdTokenAuth(audience, identity=identity)

    if mode == "aws-sigv4":
        service = os.getenv(f"{prefix}_A2A_SIGNING_SERVICE", "bedrock-agentcore")
        return AwsSigV4Auth(
            role_arn=_require(peer, mode, f"{prefix}_A2A_ROLE_ARN"),
            region=_require(peer, mode, f"{prefix}_A2A_REGION"),
            # Caller-chosen, and matched by the trust policy's :oaud condition.
            audience=os.getenv(f"{prefix}_A2A_AUDIENCE", "sts.amazonaws.com"),
            service=service,
            extra_headers=_agentcore_headers(prefix) if service == "bedrock-agentcore" else None,
            identity=identity,
        )

    return EntraFederatedAuth(
        tenant_id=_require(peer, mode, f"{prefix}_A2A_TENANT_ID"),
        client_id=_require(peer, mode, f"{prefix}_A2A_CLIENT_ID"),
        scope=os.getenv(f"{prefix}_A2A_SCOPE")
        or f"{_require(peer, mode, f'{prefix}_A2A_CLIENT_ID')}/.default",
        identity=identity,
    )


#: AgentCore Runtime isolates sessions on this header and requires it on every
#: request -- including the agent-card fetch, which is served from the same
#: ``/invocations/`` path. It must be at least 33 characters.
AGENTCORE_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def _agentcore_headers(prefix: str) -> dict[str, str]:
    """The session header AgentCore requires, stable for the life of the run.

    One session per coordinator process rather than per request: AgentCore
    keeps a runtime session alive per ID, and minting a fresh one on every call
    would create a session per quote and burn the session quota.
    """
    import uuid

    session_id = os.getenv(f"{prefix}_A2A_SESSION_ID") or str(uuid.uuid4())
    if len(session_id) < 33:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"{prefix}_A2A_SESSION_ID must be at least 33 characters "
            f"(AgentCore rejects shorter ones); got {len(session_id)}",
        )
    return {AGENTCORE_SESSION_HEADER: session_id}


def auth_mode(auth: httpx.Auth | None) -> str:
    """Name the mode on a credential, for the matrix report and the CLI header."""
    return getattr(auth, "mode", "none") if auth is not None else "none"


def is_keyless(mode: str) -> bool:
    """Whether a leg running in ``mode`` needs a long-lived secret.

    Reported per leg rather than inferred from the topology afterwards. The
    whole claim of this project is which legs were keyless, and a run that
    quietly used a client secret must not be summarised as though it had not.
    """
    return mode in KEYLESS_MODES


def _service_root(endpoint: str) -> str:
    """Scheme and host only -- Cloud Run's ID token audience is the service URL."""
    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return endpoint.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"
