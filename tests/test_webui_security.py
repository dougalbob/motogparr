"""
Regression tests for the WebUI security issues found during the v2.4.1 review.

These are deliberately written as *characterisation* tests: they pin the current,
known-bad behaviour so the planned fix in AGENTS.md §7 PR 2 cannot land silently
and so the threat is executable documentation rather than a paragraph.

When the auth work is done, the assertions marked ``BUG`` invert - a passing
``BUG`` test means the vulnerability is still present. Update or replace them in
the same commit that fixes the issue.

References:
    AGENTS.md  §6 finding A  POST /app-config destroys unknown config keys
    AGENTS.md  §6 finding B  Access-Control-Allow-Origin: * on every response
    AGENTS.md  §5            no authentication on any route
"""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

# motogp_webui computes module-level paths from CONFIG_DIR at import time, and
# importing it emits an activity event into CONFIG_DIR. Point that at a throwaway
# directory *before* the import so the test never touches a real /config and
# leaves nothing behind in the working tree.
_CONFIG_DIR = tempfile.mkdtemp(prefix="motogp-webui-test-")
os.environ["CONFIG_DIR"] = _CONFIG_DIR

import motogp_webui  # noqa: E402


SETTINGS = {
    "prowlarr": {"url": "http://prowlarr:9696", "api_key": "prowlarr-key"},
    "sabnzbd": {"url": "http://sab:8080", "api_key": "sab-key"},
    "plex": {"url": "http://plex:32400", "token": "plex-token", "library_id": "16"},
    "library": {"motogp_path": "/media/MotoGP", "downloads_path": "/downloads"},
    "grabber": {"max_grabs_per_run": 22, "allowed_groups": ["motogp"]},
    "uiadminpassword": "hunter2",
    "timezone": "Europe/London",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client whose settings.yml lives in tmp_path."""
    import utils.config_loader as config_loader

    settings_file = tmp_path / "settings.yml"
    settings_file.write_text(yaml.safe_dump(SETTINGS), encoding="utf-8")

    monkeypatch.setattr(motogp_webui, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_loader, "_settings_cache", None)

    motogp_webui.app.config.update(TESTING=True)
    with motogp_webui.app.test_client() as test_client:
        yield test_client, settings_file

    # The loader caches settings globally; leaving tmp_path in the cache would
    # leak a deleted directory into the next test.
    config_loader._settings_cache = None


# ---------------------------------------------------------------------------
# Finding A: POST /app-config rebuilds settings.yml from a fixed dict
# ---------------------------------------------------------------------------

def test_app_config_post_drops_keys_it_does_not_know(client):
    """
    BUG: saving App Config from the UI silently deletes every key that is not in
    the route's six-section literal - including uiadminpassword and all of
    grabber.*. Guarded here because it is invisible data loss, not an error.
    """
    test_client, settings_file = client

    # Exactly what templates/settings.html sends: the six sections, nothing else.
    posted = {
        "prowlarr": SETTINGS["prowlarr"],
        "sabnzbd": SETTINGS["sabnzbd"],
        "plex": SETTINGS["plex"],
        "library": SETTINGS["library"],
        "discord": {},
        "timezone": "Europe/London",
    }
    response = test_client.post("/app-config", json=posted)
    assert response.status_code == 200

    saved = yaml.safe_load(settings_file.read_text(encoding="utf-8"))

    # The dangerous part: keys the client never mentioned are gone.
    assert "uiadminpassword" not in saved, (
        "uiadminpassword survived - finding A may have been fixed; invert this test"
    )
    assert "grabber" not in saved, (
        "grabber section survived - finding A may have been fixed; invert this test"
    )


def test_app_config_get_exposes_api_keys(client):
    """
    BUG: GET /app-config returns the Plex token and both API keys verbatim, to
    any caller. This is the concrete reason the missing auth matters.
    """
    test_client, _ = client
    payload = test_client.get("/app-config").get_json()

    assert payload["plex"]["token"] == "plex-token"
    assert payload["prowlarr"]["api_key"] == "prowlarr-key"
    assert payload["sabnzbd"]["api_key"] == "sab-key"


# ---------------------------------------------------------------------------
# Finding B: CORS wildcard
# ---------------------------------------------------------------------------

def test_every_response_has_wildcard_cors(client):
    """
    BUG: Access-Control-Allow-Origin: * is set on all responses, so any page in
    a browser that can reach this host can read the secrets above cross-origin.
    """
    test_client, _ = client

    for path in ("/app-config", "/manifest.json", "/flags/gb.svg"):
        response = test_client.get(path)
        assert response.headers.get("Access-Control-Allow-Origin") == "*", (
            f"CORS wildcard missing on {path} - finding B may have been fixed"
        )


# ---------------------------------------------------------------------------
# Finding: no route requires authentication (the headline issue)
# ---------------------------------------------------------------------------

# Routes that are static assets or PWA plumbing and can reasonably stay public
# once auth lands. Kept explicit so the guard test below cannot be satisfied by
# accident. Anything not listed here and not in this set must become protected.
PUBLIC_ROUTES = {
    "/assets/<path:filename>",
    "/flags/<path:filename>",
    "/manifest.json",
    "/sw.js",
    "/static/<path:filename>",
}


def _route_cases():
    """(rule, method) for every non-static route, probed with its own method."""
    cases = []
    for rule in motogp_webui.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        if rule.rule in PUBLIC_ROUTES:
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            cases.append((rule.rule, method))
    return cases


def _sample_path(rule):
    """Concrete example URL for a rule containing a converter."""
    return rule.replace("<path:filename>", "x").replace("<int:", "<").split("<")[0] or "/"


@pytest.mark.parametrize("rule,method", sorted(_route_cases()))
def test_route_is_reachable_without_credentials(client, rule, method):
    """
    BUG: every application route answers an anonymous request. Enumerated from
    url_map rather than hard-coded, so a newly added route is covered
    automatically - this is the test that should flip to "rejected" when auth
    is implemented, and it will fail for any route added without a decision.

    A 4xx coming from the *view* (400 for a missing JSON body, 404 for an
    unreachable backend) still proves the request was served without
    credentials, so those count as reachable. 401/403 would mean auth landed.
    """
    test_client, _ = client
    response = test_client.open(_sample_path(rule), method=method)

    assert response.status_code not in (401, 403), (
        f"{rule} [{method}] now rejects anonymous requests - auth has landed; "
        "update this test to assert rejection instead"
    )
    assert response.status_code != 405, (
        f"{rule} [{method}] returned 405, so the probe never reached the view"
    )


def test_healthcheck_endpoint_is_not_auth_gated():
    """
    Guard against a future auth implementation locking out the Docker
    HEALTHCHECK, which would make every container report unhealthy. The probe
    hits '/' but with no assertion, so the auth work must keep it reachable.
    """
    import utils.healthcheck as healthcheck

    source = Path(healthcheck.__file__).read_text(encoding="utf-8")
    assert "Cf-Access" not in source and "AUTH_" not in source, (
        "healthcheck should stay dependency-free and unauthenticated; "
        "give it a dedicated bypass instead"
    )
