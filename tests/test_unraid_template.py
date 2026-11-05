"""
Tests for the Unraid container template.

Unraid drives a container from an XML template rather than a compose file, so a
broken or half-filled template is the difference between "install and configure
in the WebUI" and "the app starts but every path is wrong". These tests pin the
contract that matters: the template parses, the ports and volumes the app
actually needs are present, and every variable it sets is one the app reads.
"""

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "unraid" / "motogparr.xml"


@pytest.fixture(scope="module")
def template():
    return ET.parse(TEMPLATE).getroot()


def configs(root, kind=None):
    entries = root.findall("Config")
    if kind:
        entries = [c for c in entries if c.get("Type") == kind]
    return entries


def test_template_parses_and_is_unraid_v2(template):
    assert template.tag == "Container"
    assert template.get("version") == "2"


def test_repository_and_icon_are_pinned_to_this_project(template):
    assert template.findtext("Repository") == "ghcr.io/dougalbob/motogparr:latest"
    assert "flat/v2" not in (template.findtext("Icon") or "")
    icon = template.findtext("Icon")
    assert icon and icon.startswith("https://raw.githubusercontent.com/dougalbob/motogparr/")


def test_webui_port_matches_the_port_the_app_binds(template):
    """motogp_webui.py binds 5679; a mismatched template port hides the UI."""
    webui = template.findtext("WebUI")
    assert "[PORT:5679]" in webui

    ports = configs(template, "Port")
    assert [p.get("Target") for p in ports] == ["5679"]


def test_required_volumes_present(template):
    targets = {c.get("Target") for c in configs(template, "Path")}
    assert {"/config", "/media", "/downloads"} <= targets


def test_config_folder_is_persistent(template):
    config_dir = next(c for c in configs(template, "Path") if c.get("Target") == "/config")
    assert config_dir.get("Required") == "true"
    assert config_dir.text.startswith("/mnt/user/")


def test_variables_the_app_actually_reads(template):
    """
    Every Variable must be one the app reads - a typo here fails silently.
    The authoritative list is ENV_SPEC, not a copy of it, so this cannot rot.
    """
    from utils.config_loader import all_known_env_vars

    targets = {c.get("Target") for c in configs(template, "Variable")}
    unknown = targets - all_known_env_vars()
    assert not unknown, f"template sets variables nothing reads: {unknown}"


def test_secrets_are_masked(template):
    for name in ("PLEX_TOKEN", "PROWLARR_API_KEY", "SABNZBD_API_KEY", "DISCORD_WEBHOOK_URL"):
        entry = next(c for c in configs(template, "Variable") if c.get("Target") == name)
        assert entry.get("Mask") == "true", f"{name} is displayed in plain text"


def test_api_settings_are_optional(template):
    """
    Prowlarr/SABnzbd/Plex credentials are editable in the WebUI, so the template
    must not make them required - otherwise a fresh install cannot be saved.
    """
    for name in ("PROWLARR_URL", "SABNZBD_URL", "PLEX_URL", "PLEX_TOKEN"):
        entry = next(c for c in configs(template, "Variable") if c.get("Target") == name)
        assert entry.get("Required") == "false"


def test_calendar_update_variable_is_documented(template):
    """The whole point of the option is discoverable behaviour, so the default
    must match entrypoint.sh and the description must explain the risk."""
    entry = next(
        c for c in configs(template, "Variable")
        if c.get("Target") == "MOTOGPARR_CALENDAR_UPDATE"
    )
    assert entry.text == "auto"
    assert "auto" in entry.get("Description")
    assert "warn" in entry.get("Description")

    entrypoint = (REPO_ROOT / "entrypoint.sh").read_text()
    assert 'MOTOGPARR_CALENDAR_UPDATE:-auto' in entrypoint


def newest_tag() -> str:
    """Newest version tag in the repo, so the TemplateURL cannot silently rot.

    Tags are fetched from the remote (a shallow checkout may not carry them
    locally), which is why this shells out to git rather than reading a file.
    """
    refs = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", "origin"],
        capture_output=True, text=True, check=True,
    ).stdout
    version_re = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
    tags = []
    for line in refs.splitlines():
        if "refs/tags/" not in line or line.endswith("^{}"):
            continue
        tag = line.split("refs/tags/", 1)[1]
        if version_re.match(tag):
            tags.append(tag)
    assert tags, "no version tags found in the repo"
    return sorted(
        tags,
        key=lambda t: tuple(int(p) for p in version_re.match(t).groups()),
    )[-1]


def test_template_url_is_pinned_to_the_latest_release_tag(template):
    """TemplateURL must pin a release tag, never float on 'main'.

    Pinning (decision b in the handoff) means pressing "Apply" in the Unraid
    GUI cannot silently replace a user's locally edited template with whatever
    main happens to be. The trade-off: old installs do not receive template
    updates until the user re-points it, so the release checklist must remind
    the releaser to bump TemplateURL on every release.
    """
    url = template.findtext("TemplateURL")
    assert "raw.githubusercontent.com/dougalbob/motogparr/" in url, url
    # The segment after the repo name is the pinned ref (a tag, never main).
    ref = url.split("raw.githubusercontent.com/dougalbob/motogparr/", 1)[1].split(
        "/", 1
    )[0]
    assert ref != "main", f"TemplateURL must not float on main: {url}"
    assert ref.startswith("v"), f"TemplateURL must pin a version tag: {url}"
    assert ref == newest_tag(), (
        f"TemplateURL pins {ref} but the newest tag is {newest_tag()}; "
        "bump TemplateURL on every release"
    )
    # The file must exist at the path the URL advertises, or Unraid 404s.
    assert (REPO_ROOT / "unraid" / "motogparr.xml").is_file()


def test_every_config_has_unique_target_and_required_attributes(template):
    """Each <Config> must carry a unique Target plus Type/Display/Default so
    Unraid can render the Add Container form unambiguously."""
    entries = configs(template)
    targets = [c.get("Target") for c in entries]
    assert all(targets), "every Config needs a Target"
    assert len(targets) == len(set(targets)), f"duplicate Target values: {targets}"
    for c in entries:
        assert c.get("Type"), f"Config {c.get('Target')!r} missing Type"
        assert c.get("Display") is not None, (
            f"Config {c.get('Target')!r} missing Display"
        )
        assert c.get("Default") is not None, (
            f"Config {c.get('Target')!r} missing Default"
        )


def test_puid_pgid_umask_and_auth_targets_present(template):
    """PUID/PGID/UMASK and every AUTH_* variable must be offered by the
    template (they are runtime configuration the user may need to set)."""
    targets = {c.get("Target") for c in configs(template)}
    for name in ("PUID", "PGID", "UMASK"):
        assert name in targets, f"{name} is not in the Unraid template"
    # Every AUTH_* the app reads must have a template entry.
    from utils.config_loader import ENV_SPEC

    auth_vars = [name for name in ENV_SPEC if name.startswith("AUTH_")]
    assert auth_vars, "ENV_SPEC should define AUTH_* variables"
    for name in auth_vars:
        assert name in targets, f"{name} is not in the Unraid template"


def test_new_variables_are_not_required(template):
    """Newly added optional variables must not be Required="true", or they
    would block the Add Container form for existing users."""
    newly_optional = ("MOTOGPARR_HOST_MAP",)
    for name in newly_optional:
        entry = next(c for c in configs(template) if c.get("Target") == name)
        assert entry.get("Required") != "true", f"{name} must not be required"


def test_icon_file_referenced_by_url_exists(template):
    url = template.findtext("Icon")
    name = url.rsplit("/", 1)[-1]
    assert (REPO_ROOT / "assets" / name).is_file(), f"missing asset for Icon URL: {name}"
