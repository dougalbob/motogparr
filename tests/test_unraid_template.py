"""
Tests for the Unraid container template.

Unraid drives a container from an XML template rather than a compose file, so a
broken or half-filled template is the difference between "install and configure
in the WebUI" and "the app starts but every path is wrong". These tests pin the
contract that matters: the template parses, the ports and volumes the app
actually needs are present, and every variable it sets is one the app reads.
"""

import re
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
    """Every Variable must be one the app reads - a typo here fails silently."""
    known = {
        "MOTOGP_LIBRARY_PATH",
        "MOTOGP_DOWNLOADS_PATH",
        "TZ",
        "MOTOGPARR_CALENDAR_UPDATE",
        "PROWLARR_URL",
        "PROWLARR_API_KEY",
        "SABNZBD_URL",
        "SABNZBD_API_KEY",
        "PLEX_URL",
        "PLEX_TOKEN",
        "MOTOGP_PLEX_LIBRARY_ID",
        "MOTOGP_PLEX_LIBRARY",
        "DISCORD_WEBHOOK_URL",
    }
    targets = {c.get("Target") for c in configs(template, "Variable")}
    assert targets <= known, f"template sets variables the app never reads: {targets - known}"


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


def test_template_url_points_at_this_file(template):
    url = template.findtext("TemplateURL")
    assert url.endswith("/unraid/motogparr.xml"), url
    # The file must exist at the path the URL advertises, or Unraid 404s.
    assert (REPO_ROOT / "unraid" / "motogparr.xml").is_file()


def test_icon_file_referenced_by_url_exists(template):
    url = template.findtext("Icon")
    name = url.rsplit("/", 1)[-1]
    assert (REPO_ROOT / "assets" / name).is_file(), f"missing asset for Icon URL: {name}"
