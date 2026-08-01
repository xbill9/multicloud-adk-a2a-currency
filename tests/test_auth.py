"""Cloud-free tests of the cross-cloud credential seam.

Every provider is a stubbed transport, so this suite asserts the things that
actually went wrong in the predecessor series -- the shape of the mint request,
which condition a denial names, whether the error survives the trip back --
without an account, a network, or a vendor SDK.
"""

import base64
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from coordinator.auth import (
    ENTRA_EXCHANGE_AUDIENCE,
    AwsSigV4Auth,
    EntraFederatedAuth,
    GoogleIdTokenAuth,
    WorkloadIdentity,
    _signing_key,
    auth_mode,
    credentials_for,
)
from coordinator.errors import AdapterError, FailureKind


def jwt(expires_in_seconds: int = 3600, **claims) -> str:
    """A structurally valid unsigned JWT. Nothing here verifies signatures."""
    payload = {"exp": int((datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).timestamp())}
    payload.update(claims)
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def identity_returning(token: str, *, record: list | None = None) -> WorkloadIdentity:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(200, text=token)

    return WorkloadIdentity(transport=transport(handler))


# --------------------------------------------------------------------------
# The metadata mint -- every leg starts here
# --------------------------------------------------------------------------


async def test_mint_requests_format_full():
    """Without format=full Google trims the token and drops the email claim.

    There is no error when this is wrong; the trust conditions simply stop
    matching. Asserting the query parameter is the only place it can be caught.
    """
    seen: list[httpx.Request] = []
    identity = identity_returning(jwt(), record=seen)

    await identity.id_token("https://currency.example.run.app")

    assert seen[0].url.params["format"] == "full"
    assert seen[0].url.params["audience"] == "https://currency.example.run.app"
    assert seen[0].headers["Metadata-Flavor"] == "Google"


async def test_mint_is_cached_per_audience():
    seen: list[httpx.Request] = []
    identity = identity_returning(jwt(), record=seen)

    await identity.id_token("aud-one")
    await identity.id_token("aud-one")
    await identity.id_token("aud-two")

    assert [request.url.params["audience"] for request in seen] == ["aud-one", "aud-two"]


async def test_near_expiry_token_is_reminted():
    """A token inside the skew window must not be handed out; it can die in flight."""
    seen: list[httpx.Request] = []
    identity = identity_returning(jwt(expires_in_seconds=30), record=seen)

    await identity.id_token("aud")
    await identity.id_token("aud")

    assert len(seen) == 2


async def test_unreachable_metadata_server_says_why():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    identity = WorkloadIdentity(transport=transport(handler))

    with pytest.raises(AdapterError) as exc:
        await identity.id_token("aud")

    assert exc.value.kind is FailureKind.AUTHENTICATION
    # The actionable part: this is not a bug, it is the coordinator running
    # somewhere that cannot mint.
    assert "must run on a Google runtime" in str(exc.value)


async def test_empty_token_is_an_auth_failure_not_an_empty_bearer():
    identity = WorkloadIdentity(transport=transport(lambda request: httpx.Response(200, text="")))

    with pytest.raises(AdapterError) as exc:
        await identity.id_token("aud")

    assert exc.value.kind is FailureKind.AUTHENTICATION


# --------------------------------------------------------------------------
# GCP -> GCP
# --------------------------------------------------------------------------


async def test_google_id_token_is_attached_as_a_bearer():
    token = jwt()
    auth = GoogleIdTokenAuth("https://peer.run.app", identity=identity_returning(token))
    request = httpx.Request("POST", "https://peer.run.app/a2a")

    async for signed in auth.async_auth_flow(request):
        assert signed.headers["Authorization"] == f"Bearer {token}"


async def test_card_fetch_carries_the_same_credential():
    """Discovery is privileged. A card fetch that 403s while the call would
    have succeeded surfaces as a protocol error, nowhere near auth."""
    from clients.base import A2AQuoteClient

    auth = GoogleIdTokenAuth("https://peer.run.app", identity=identity_returning(jwt()))
    client = A2AQuoteClient("https://peer.run.app", auth=auth)

    async with client._http_client() as httpx_client:
        assert httpx_client.auth is auth


# --------------------------------------------------------------------------
# GCP -> AWS
# --------------------------------------------------------------------------

STS_OK = """<AssumeRoleWithWebIdentityResponse
  xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleWithWebIdentityResult>
    <Credentials>
      <AccessKeyId>ASIAEXAMPLE</AccessKeyId>
      <SecretAccessKey>wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY</SecretAccessKey>
      <SessionToken>FQoEXAMPLEtoken</SessionToken>
      <Expiration>2099-01-01T00:00:00Z</Expiration>
    </Credentials>
  </AssumeRoleWithWebIdentityResult>
</AssumeRoleWithWebIdentityResponse>"""


def sts_error(code: str, message: str = "denied") -> str:
    return (
        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        f"<Error><Type>Sender</Type><Code>{code}</Code>"
        f"<Message>{message}</Message></Error></ErrorResponse>"
    )


def sigv4_auth(sts_body: str = STS_OK, status: int = 200, record: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, text=sts_body)

    return AwsSigV4Auth(
        role_arn="arn:aws:iam::123456789012:role/currency-mesh",
        region="us-west-2",
        audience="sts.amazonaws.com",
        identity=identity_returning(jwt()),
        transport=transport(handler),
    )


async def test_sts_exchange_presents_the_google_token_as_the_web_identity():
    seen: list[httpx.Request] = []
    auth = sigv4_auth(record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://agentcore.example/a2a")):
        pass

    body = seen[0].content.decode()
    assert "Action=AssumeRoleWithWebIdentity" in body
    assert "WebIdentityToken=" in body


async def test_signed_request_carries_a_sigv4_authorization_header():
    auth = sigv4_auth()
    request = httpx.Request(
        "POST", "https://bedrock-agentcore.us-west-2.amazonaws.com/runtime/abc", json={"q": 1}
    )

    async for signed in auth.async_auth_flow(request):
        header = signed.headers["Authorization"]
        assert header.startswith("AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/")
        assert "/us-west-2/bedrock-agentcore/aws4_request" in header
        # The session token is both sent and signed; omitting it from
        # SignedHeaders is a signature mismatch that reads as a clock problem.
        assert "x-amz-security-token" in header
        assert signed.headers["x-amz-security-token"] == "FQoEXAMPLEtoken"


async def test_signature_covers_the_request_body():
    """requires_request_body is not decoration: two bodies must not share a signature."""
    first = sigv4_auth()
    second = sigv4_auth()
    url = "https://bedrock-agentcore.us-west-2.amazonaws.com/runtime/abc"

    signatures = []
    for auth, body in ((first, {"amount": 100}), (second, {"amount": 999})):
        async for signed in auth.async_auth_flow(httpx.Request("POST", url, json=body)):
            signatures.append(signed.headers["Authorization"].split("Signature=")[1])

    assert signatures[0] != signatures[1]


def test_signing_key_matches_the_published_aws_vector():
    """The derivation from AWS's own SigV4 documentation, verbatim."""
    key = _signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20150830", "us-east-1", "iam"
    )

    assert key.hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


async def test_invalid_identity_token_points_at_the_provider_not_the_conditions():
    """The fastest discriminator in this whole space, so it must reach the caller."""
    auth = sigv4_auth(sts_body=sts_error("InvalidIdentityToken"), status=403)

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://x.example/a2a")):
            pass

    message = str(exc.value)
    assert "InvalidIdentityToken" in message
    assert "IAM OIDC provider" in message
    assert "format=full" in message


async def test_access_denied_points_at_the_condition_keys():
    auth = sigv4_auth(sts_body=sts_error("AccessDenied"), status=403)

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://x.example/a2a")):
            pass

    message = str(exc.value)
    assert "AccessDenied" in message
    # The trap that costs an afternoon: :oaud is aud, :aud is azp.
    assert ":oaud" in message and ":aud" in message


async def test_sts_body_is_logged_whole_at_the_boundary(caplog):
    """A raised message is not an observable -- it can be paraphrased by a model
    in the middle. The body has to be in the log too."""
    auth = sigv4_auth(sts_body=sts_error("AccessDenied", "no matching condition"), status=403)

    with caplog.at_level(logging.ERROR, logger="coordinator.auth"), pytest.raises(AdapterError):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://x.example/a2a")):
            pass

    assert "no matching condition" in caplog.text


async def test_credentials_are_cached_across_calls():
    seen: list[httpx.Request] = []
    auth = sigv4_auth(record=seen)
    url = "https://bedrock-agentcore.us-west-2.amazonaws.com/runtime/abc"

    for _ in range(3):
        async for _signed in auth.async_auth_flow(httpx.Request("POST", url, json={})):
            pass

    assert len(seen) == 1


# --------------------------------------------------------------------------
# GCP -> Azure
# --------------------------------------------------------------------------


def entra_auth(payload: dict, status: int = 200, record: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json=payload)

    return EntraFederatedAuth(
        tenant_id="tenant-uuid",
        client_id="client-uuid",
        scope="api://currency/.default",
        identity=identity_returning(jwt(), record=record),
        transport=transport(handler),
    )


async def test_entra_exchange_uses_the_fixed_exchange_audience():
    """Entra rejects any assertion audience other than api://AzureADTokenExchange."""
    seen: list[httpx.Request] = []
    auth = entra_auth({"access_token": "entra-token", "expires_in": 3600}, record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://foundry.example/a2a")):
        pass

    mint = next(r for r in seen if "metadata" in str(r.url))
    assert mint.url.params["audience"] == ENTRA_EXCHANGE_AUDIENCE


async def test_entra_exchange_sends_a_client_assertion():
    seen: list[httpx.Request] = []
    auth = entra_auth({"access_token": "entra-token", "expires_in": 3600}, record=seen)

    async for signed in auth.async_auth_flow(httpx.Request("POST", "https://foundry.example/a2a")):
        assert signed.headers["Authorization"] == "Bearer entra-token"

    exchange = next(r for r in seen if "login.microsoftonline.com" in str(r.url))
    body = exchange.content.decode()
    assert "client_assertion_type=urn%3Aietf%3Aparams%3Aoauth%3Aclient-assertion-type%3Ajwt-bearer" in body
    assert "grant_type=client_credentials" in body


async def test_entra_error_surfaces_the_aadsts_code():
    auth = entra_auth(
        {"error": "invalid_client", "error_description": "AADSTS700213: no matching FIC"},
        status=401,
    )

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://foundry.example/a2a")):
            pass

    assert "AADSTS700213" in str(exc.value)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def test_peers_are_unauthenticated_by_default(monkeypatch):
    """The local mesh must stay an unauthenticated protocol instrument."""
    monkeypatch.delenv("GCP_A2A_AUTH", raising=False)

    assert credentials_for("gcp", "http://127.0.0.1:10001") is None
    assert auth_mode(None) == "none"


def test_google_id_token_audience_defaults_to_the_service_root(monkeypatch):
    monkeypatch.setenv("GCP_A2A_AUTH", "google-id-token")
    monkeypatch.delenv("GCP_A2A_AUDIENCE", raising=False)

    auth = credentials_for("gcp", "https://currency-gcp-abc.a.run.app/some/path")

    assert auth.mode == "google-id-token"
    assert auth._audience == "https://currency-gcp-abc.a.run.app"


def test_unknown_mode_is_rejected_rather_than_silently_unauthenticated(monkeypatch):
    monkeypatch.setenv("AWS_A2A_AUTH", "sigv4")

    with pytest.raises(AdapterError) as exc:
        credentials_for("aws", "https://agentcore.example")

    assert exc.value.kind is FailureKind.VALIDATION


def test_missing_required_setting_names_the_variable(monkeypatch):
    monkeypatch.setenv("AWS_A2A_AUTH", "aws-sigv4")
    monkeypatch.delenv("AWS_A2A_ROLE_ARN", raising=False)

    with pytest.raises(AdapterError) as exc:
        credentials_for("aws", "https://agentcore.example")

    assert "AWS_A2A_ROLE_ARN" in str(exc.value)


def test_entra_registry_builds_a_configured_credential(monkeypatch):
    monkeypatch.setenv("AZURE_A2A_AUTH", "entra-fic")
    monkeypatch.setenv("AZURE_A2A_TENANT_ID", "tenant-uuid")
    monkeypatch.setenv("AZURE_A2A_CLIENT_ID", "client-uuid")
    monkeypatch.delenv("AZURE_A2A_SCOPE", raising=False)

    auth = credentials_for("azure", "https://foundry.example")

    assert auth.mode == "entra-fic"
    assert auth._scope == "client-uuid/.default"


def test_a_misconfigured_peer_fails_its_cell_not_the_matrix(monkeypatch):
    """One unconfigured cloud must not stop the other six cells running."""
    import asyncio
    from decimal import Decimal

    from coordinator.models import ConversionRequest
    from matrix.runner import Server, probe

    monkeypatch.setenv("AWS_A2A_AUTH", "aws-sigv4")
    monkeypatch.delenv("AWS_A2A_ROLE_ARN", raising=False)

    cell = asyncio.run(
        probe(
            "a2a-sdk",
            Server("aws", "AWS", "a2a-sdk routes", "https://agentcore.example"),
            ConversionRequest(
                amount=Decimal(100), source_currency="USD", target_currencies=["EUR"]
            ),
            timeout_s=1.0,
        )
    )

    assert cell.ok is False
    assert cell.failure_kind == FailureKind.VALIDATION.value
