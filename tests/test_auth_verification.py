"""
Tests for Cloudflare Access JWT verification (utils/auth.py).

Every test signs a real RS256 token with a real RSA key and verifies it against a
locally served key set, so the signature path is exercised rather than mocked.
No network is involved: <team>.cloudflareaccess.com is not reachable from the
sandbox or from most CI, and a test that needed it would be the reason nobody
runs the suite.

The cases mirror how an attacker or a misconfiguration actually fails:

    forged signature  - someone who can reach port 5679 sends their own token
    wrong audience    - a token minted for a different Access application
    wrong issuer      - a token from another Cloudflare team
    expired           - a replayed token
    no email claim    - a service token, where an email allowlist cannot apply
    not on allowlist  - authenticated by Cloudflare but not assigned here
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK

from utils import auth as auth_module
from utils.auth import AuthConfig, AuthError, AccessVerifier, load_config

ISSUER = "https://example-team.cloudflareaccess.com"
AUDIENCE = "0123456789abcdef0123456789abcdef"
EMAIL = "me@example.com"


# ---------------------------------------------------------------------------
# Fixtures: one RSA keypair per test (fast enough at 2048 bits, and isolation
# matters more than the few milliseconds)
# ---------------------------------------------------------------------------

@pytest.fixture
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


class FakeJWKS:
    """Minimal stand-in for jwt.PyJWKClient."""

    def __init__(self, public_key, kid="test-kid"):
        self._jwk = PyJWK.from_dict(
            _jwk_dict(public_key, kid), algorithm="RS256"
        )
        self.kid = kid
        self.refresh_calls = 0
        self.fail = False

    def get_signing_key_from_jwt(self, token):
        if self.fail:
            raise jwt.PyJWKClientError("unknown kid")
        return self._jwk

    def refresh(self):
        self.refresh_calls += 1


def _b64(public_key) -> str:
    import base64

    numbers = public_key.public_numbers()

    def enc(value: int, size: int) -> str:
        raw = value.to_bytes(size, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return enc(numbers.n, 256)


def _jwk_dict(public_key, kid="test-kid"):
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": _b64(public_key),
        "e": "AQAB",
    }


def make_token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "email": EMAIL,
        "iat": now - 10,
        "exp": now + 600,
        "sub": "user-id",
        "type": "app",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-kid"})


def make_verifier(public_key, **kwargs) -> AccessVerifier:
    config = AuthConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        allowed_emails=(EMAIL,),
        certs_url=ISSUER + "/cdn-cgi/access/certs",
    )
    return AccessVerifier(config, jwks=FakeJWKS(public_key), **kwargs)


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------

def test_no_auth_variables_means_auth_is_off():
    assert load_config({}) is None


def test_full_configuration_is_accepted():
    config = load_config({
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_EMAILS": EMAIL,
    })
    assert config.issuer == ISSUER
    assert config.audience == AUDIENCE
    assert config.allowed_emails == (EMAIL,)


@pytest.mark.parametrize("missing", ["AUTH_ISSUER", "AUTH_AUDIENCE", "AUTH_ALLOWED_EMAILS"])
def test_partial_configuration_is_rejected(missing):
    """
    A half-filled config must not fall back to "no auth": that would turn a typo
    into an open door. It raises instead.
    """
    env = {
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_EMAILS": EMAIL,
    }
    env.pop(missing)

    with pytest.raises(AuthError) as excinfo:
        load_config(env)
    assert missing in str(excinfo.value)


def test_issuer_must_be_https():
    with pytest.raises(AuthError) as excinfo:
        load_config({
            "AUTH_ISSUER": "http://example-team.cloudflareaccess.com",
            "AUTH_AUDIENCE": AUDIENCE,
            "AUTH_ALLOWED_EMAILS": EMAIL,
        })
    assert "https" in str(excinfo.value)


def test_certs_url_defaults_to_the_issuer_and_can_be_overridden():
    default = load_config({
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_EMAILS": EMAIL,
    })
    assert default.certs_url == ISSUER + "/cdn-cgi/access/certs"

    override = load_config({
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_EMAILS": EMAIL,
        "AUTH_CERTS_URL": "https://internal.example/certs",
    })
    assert override.certs_url == "https://internal.example/certs"


def test_allowlist_handles_lists_and_case():
    config = load_config({
        "AUTH_ISSUER": ISSUER,
        "AUTH_AUDIENCE": AUDIENCE,
        "AUTH_ALLOWED_EMAILS": " Me@Example.com , other@example.com ",
    })
    assert config.allowed_emails == ("Me@Example.com", "other@example.com")
    assert config.is_allowed("me@example.com") is True
    assert config.is_allowed("ME@EXAMPLE.COM") is True
    assert config.is_allowed("someone@else.com") is False
    assert config.is_allowed("") is False


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_token_is_accepted(keypair):
    private, public = keypair
    identity = make_verifier(public).verify(make_token(private))
    assert identity.email == EMAIL
    assert identity.display_name == "me"


# ---------------------------------------------------------------------------
# Rejections - the reason this module exists
# ---------------------------------------------------------------------------

def test_missing_header_is_rejected(keypair):
    _, public = keypair
    with pytest.raises(AuthError) as excinfo:
        make_verifier(public).verify(None)
    assert "Cf-Access-Jwt-Assertion" in str(excinfo.value)


def test_token_signed_by_a_different_key_is_rejected(keypair):
    """The forgery case: reach the port directly, mint your own token."""
    _, public = keypair
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(AuthError):
        make_verifier(public).verify(make_token(attacker_key))


def test_unsigned_token_is_rejected(keypair):
    """`alg: none` must never be honoured."""
    _, public = keypair
    now = int(time.time())
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "email": EMAIL, "exp": now + 600},
        key="",
        algorithm="none",
        headers={"kid": "test-kid"},
    )
    with pytest.raises(AuthError):
        make_verifier(public).verify(unsigned)


def test_expired_token_is_rejected(keypair):
    private, public = keypair
    now = int(time.time())
    token = make_token(private, iat=now - 3600, exp=now - 1800)
    with pytest.raises(AuthError) as excinfo:
        make_verifier(public).verify(token)
    assert "expired" in str(excinfo.value).lower()


def test_token_within_leeway_is_accepted(keypair):
    """Clock skew between Cloudflare and the container is a few seconds."""
    private, public = keypair
    now = int(time.time())
    token = make_token(private, exp=now - 5)
    assert make_verifier(public).verify(token).email == EMAIL


def test_wrong_audience_is_rejected(keypair):
    private, public = keypair
    with pytest.raises(AuthError) as excinfo:
        make_verifier(public).verify(make_token(private, aud="a-different-application"))
    assert "audience" in str(excinfo.value).lower()


def test_wrong_issuer_is_rejected(keypair):
    private, public = keypair
    with pytest.raises(AuthError) as excinfo:
        make_verifier(public).verify(
            make_token(private, iss="https://someone-else.cloudflareaccess.com")
        )
    assert "issuer" in str(excinfo.value).lower()


def test_token_without_an_email_is_rejected(keypair):
    """
    Service tokens are valid JWTs with no email claim. The allowlist cannot be
    applied to them, so they are denied rather than silently admitted.
    """
    private, public = keypair
    with pytest.raises(AuthError) as excinfo:
        make_verifier(public).verify(make_token(private, email=None))
    assert "email" in str(excinfo.value).lower()


def test_verified_email_that_is_not_on_the_allowlist_is_denied(keypair):
    """
    Cloudflare's email policy is the outer gate; this is the inner one. A valid
    token for an unlisted (but real) address must still be refused - that is the
    "no self-sign-up" requirement.
    """
    private, public = keypair
    token = make_token(private, email="stranger@example.com")
    with pytest.raises(AuthError) as excinfo:
        make_verifier(public).verify(token)
    assert "not assigned" in str(excinfo.value)
    # The attempted address must not be echoed into logs.
    assert "stranger@example.com" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------

def test_unknown_key_id_triggers_a_refetch(keypair):
    private, public = keypair
    verifier = make_verifier(public)
    jwks = verifier._key_client

    jwks.fail = True
    with pytest.raises(AuthError):
        verifier.verify(make_token(private))
    assert jwks.refresh_calls >= 1, "a rotated key must force a key-set refetch"


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------

def test_assertion_header_lookup_is_case_insensitive():
    headers = [("Host", "x"), ("cf-access-jwt-assertion", "token-value")]
    assert auth_module.assertion_from_headers(headers) == "token-value"


def test_blank_assertion_header_is_treated_as_absent():
    assert auth_module.assertion_from_headers([("Cf-Access-Jwt-Assertion", "  ")]) is None
