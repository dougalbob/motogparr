"""Browser writes must come from the app's own pages (Slice 5, light).

Cloudflare + the auth guard prove *who* sent a request; the Origin guard
proves a write came from a page this app served, so a malicious site in
another tab cannot trigger downloads with the user's session. Origin is
checked first, Referer is the fallback, and no header at all means a
non-browser client (curl/scripts), which auth - not this guard - governs.
CSP is report-only: the header is served, nothing is enforced yet.
"""

import pytest

import motogp_webui

HOST = "http://localhost"
EVIL = "https://evil.example.com"


@pytest.fixture
def client():
    motogp_webui.jobs.finish()
    motogp_webui.app.config.update(TESTING=True)
    with motogp_webui.app.test_client() as test_client:
        yield test_client
    motogp_webui.jobs.finish()


def _invalid_run(client, **headers):
    """A write that would be a 400 if it reaches the route at all."""
    return client.post("/run", json={"action": "bogus"}, headers=headers)


def test_cross_origin_write_is_rejected(client):
    response = _invalid_run(client, Origin=EVIL)
    assert response.status_code == 403
    assert response.get_json()["error"] == "cross_origin_write_rejected"


def test_same_origin_write_reaches_the_route(client):
    response = _invalid_run(client, Origin=HOST)
    assert response.status_code == 400  # route validation, not the guard


def test_write_without_origin_headers_reaches_the_route(client):
    """Non-browser clients send neither header; auth governs them, not this."""
    response = _invalid_run(client)
    assert response.status_code == 400


def test_referer_fallback_allows_same_host_and_rejects_cross_host(client):
    assert _invalid_run(client, Referer=HOST + "/library").status_code == 400
    assert _invalid_run(client, Referer=EVIL + "/page").status_code == 403


def test_origin_takes_precedence_over_referer(client):
    response = _invalid_run(client, Origin=EVIL, Referer=HOST + "/library")
    assert response.status_code == 403


def test_cross_origin_read_is_allowed(client):
    """/status is a GET: it changes nothing, and pages poll it cross-checks-free."""
    response = client.get("/status", headers={"Origin": EVIL})
    assert response.status_code == 200


def test_public_health_endpoint_stays_public(client):
    """The Docker HEALTHCHECK probe carries no identity and must keep working."""
    response = client.get("/healthz", headers={"Origin": EVIL})
    assert response.status_code == 200


def test_origin_with_different_scheme_but_same_host_is_allowed(client):
    """Cloudflare terminates TLS: browser says https, the app sees http."""
    response = _invalid_run(client, Origin="https://localhost")
    assert response.status_code == 400


def test_csp_is_report_only(client):
    response = client.get("/status")
    policy = response.headers.get("Content-Security-Policy-Report-Only")
    assert policy is not None
    assert "default-src 'self'" in policy
    # Enforcing header must NOT be set yet: inline scripts/styles are load-
    # bearing in the templates, so enforcing now would break the UI.
    assert response.headers.get("Content-Security-Policy") is None


def test_existing_security_headers_survive(client):
    response = client.get("/status")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "no-referrer"
