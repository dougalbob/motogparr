"""
Tests for the settings.yml schema (utils/settings_schema.py).

The schema exists to break silence, not to be strict: a misspelled key is
currently ignored by the loader, which falls back to a default and behaves as if
the line were never written. Every test here is about reporting that clearly,
and - just as importantly - about not reporting things that are fine, because a
validator that cries wolf gets ignored within a week.
"""

from pathlib import Path

import pytest
import yaml

from utils.settings_schema import (
    SETTINGS,
    validate_file,
    validate_settings,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def issues(settings):
    return validate_settings(settings).issues


def kinds(settings):
    return [(i.path, i.kind) for i in issues(settings)]


# ---------------------------------------------------------------------------
# The shipped files must be clean
# ---------------------------------------------------------------------------

def test_the_example_settings_file_passes():
    report = validate_file(REPO_ROOT / "settings.example.yaml")
    assert report.ok, report.as_dict()["messages"]


def test_the_generated_template_passes(tmp_path):
    """First-run users get this file; it must be valid by construction."""
    import utils.config_loader as config_loader

    original_dir, original_file = config_loader.CONFIG_DIR, config_loader.SETTINGS_FILE
    try:
        config_loader.CONFIG_DIR = tmp_path
        config_loader.SETTINGS_FILE = tmp_path / "settings.yml"
        assert config_loader.create_template() is True
    finally:
        config_loader.CONFIG_DIR, config_loader.SETTINGS_FILE = original_dir, original_file

    report = validate_file(tmp_path / "settings.yml")
    assert report.ok, report.as_dict()["messages"]


def test_a_minimal_file_is_valid():
    assert validate_settings({}).ok
    assert validate_settings({"timezone": "Europe/London"}).ok


def test_the_nested_path_mapping_is_not_an_unknown_block():
    """path_mapping itself is not a setting; its children are."""
    report = validate_settings({
        "plex": {"path_mapping": {"container": "/media", "plex": "/data/media"}}
    })
    assert report.ok, report.as_dict()["messages"]


# ---------------------------------------------------------------------------
# Unknown keys - the whole point
# ---------------------------------------------------------------------------

def test_a_misspelled_key_is_reported_with_a_suggestion():
    found = issues({"prowlarr": {"api_kye": "x"}})
    assert len(found) == 1
    assert found[0].path == "prowlarr.api_kye"
    assert found[0].kind == "unknown-key"
    assert "prowlarr.api_key" in found[0].message


def test_a_misspelled_section_is_reported_with_a_suggestion():
    found = issues({"librari": {"motogp_path": "/media/x"}})
    assert found[0].path == "librari"
    assert "library" in found[0].message


def test_an_unknown_nested_key_names_the_leaf():
    found = issues({"plex": {"path_mapping": {"plexx": "/data/media"}}})
    assert [i.path for i in found] == ["plex.path_mapping.plexx"]


def test_a_completely_unknown_block_is_reported_once():
    found = issues({"plex": {"nonsense": {"a": 1, "b": 2}}})
    assert [i.path for i in found] == ["plex.nonsense"]


def test_the_admin_password_key_is_reported_as_unknown():
    """
    uiadminpassword is no longer read by anything. Silently ignoring it would
    leave a password sitting in a file that has no effect.
    """
    found = issues({"uiadminpassword": "hunter2"})
    assert found and found[0].kind == "unknown-key"


def test_authentication_in_settings_yml_is_explained():
    found = issues({"auth": {"issuer": "https://x.cloudflareaccess.com"}})
    assert len(found) == 1
    assert "AUTH_* environment variables" in found[0].message


# ---------------------------------------------------------------------------
# Types and values
# ---------------------------------------------------------------------------

def test_a_string_where_a_number_is_expected_is_reported():
    found = issues({"grabber": {"max_grabs_per_run": "22"}})
    assert found[0].kind == "wrong-type"
    assert "positive whole number" in found[0].message


def test_a_negative_or_zero_number_is_reported():
    assert issues({"grabber": {"max_grabs_per_run": 0}})[0].kind == "wrong-type"
    assert issues({"grabber": {"max_grabs_per_run": -1}})[0].kind == "wrong-type"


def test_an_unknown_quality_is_reported():
    found = issues({"grabber": {"required_quality": "1440p"}})
    assert found[0].kind == "bad-value"
    assert "720p" in found[0].message


def test_allowed_groups_accepts_a_list_or_a_comma_string():
    assert validate_settings({"grabber": {"allowed_groups": ["VERUM", "MWR"]}}).ok
    assert validate_settings({"grabber": {"allowed_groups": "VERUM,MWR"}}).ok


def test_an_empty_value_is_not_a_type_error():
    """`webhook_url:` with nothing after it means 'unset' everywhere else."""
    assert validate_settings({"discord": {"webhook_url": None}}).ok


def test_a_numeric_library_id_is_accepted():
    """`library_id: 16` is how everyone writes it; failing that would be noise."""
    assert validate_settings({"plex": {"library_id": 16}}).ok
    assert validate_settings({"plex": {"library_id": "16"}}).ok


def test_a_section_that_is_not_a_mapping_is_reported():
    found = issues({"plex": "http://plex:32400"})
    assert found[0].kind == "wrong-type"


# ---------------------------------------------------------------------------
# Robustness and reporting
# ---------------------------------------------------------------------------

def test_malformed_yaml_is_reported_rather_than_raising(tmp_path):
    broken = tmp_path / "settings.yml"
    broken.write_text("prowlarr:\n  url: [unclosed\n")
    report = validate_file(broken)
    assert not report.ok
    assert "could not parse YAML" in report.issues[0].message


def test_a_missing_file_is_not_an_error(tmp_path):
    assert validate_file(tmp_path / "nope.yml").ok


def test_validate_reports_schema_issues_to_the_webui(tmp_path, monkeypatch):
    """
    The WebUI shows validate() output in the Settings tab, so schema problems
    have to travel through it rather than only existing in a CLI.
    """
    import utils.config_loader as config_loader

    settings = tmp_path / "settings.yml"
    settings.write_text(yaml.safe_dump({"prowlarr": {"api_kye": "oops"}}))
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", settings)
    monkeypatch.setattr(config_loader, "_settings_cache", None)

    result = config_loader.validate()
    assert result["schema"]["ok"] is False
    assert any("api_kye" in m for m in result["schema"]["messages"])
    # A schema problem must not make the app unusable.
    assert "schema" in result and "valid" in result


# ---------------------------------------------------------------------------
# The schema must not drift away from the code
# ---------------------------------------------------------------------------

def test_every_schema_entry_maps_to_a_getter():
    """
    A schema that lists keys nothing reads becomes a second, lying source of
    truth - the exact problem it exists to prevent.

    The check is that a loader getter exists for each section/key, by naming
    convention (get_<section>_<key>, or get_<key> for the grabber). It cannot be
    done by asking for the value, because an unset key legitimately resolves to
    its default - which is how the first version of this test managed to flag
    all 18 entries as unread.
    """
    import utils.config_loader as config_loader

    missing = []
    for setting in SETTINGS:
        if setting.key is None:  # a top-level scalar such as timezone
            assert hasattr(config_loader, "get_timezone")
            continue

        leaf = setting.key.split(".")[-1]
        candidates = [
            f"get_{leaf}",                          # get_required_quality
            f"get_{setting.section}_{leaf}",        # get_grabber_max_grabs_per_run
            f"get_{leaf.replace('motogp_', '')}",   # library.motogp_path
            "get_plex_path_mapping",                # plex.path_mapping.*
        ]
        # Some getters are named for the concept rather than the key:
        aliases = {
            ("library", "motogp_path"): "get_motogp_library_path",
            ("discord", "webhook_url"): "get_discord_webhook",
        }
        name = aliases.get((setting.section, setting.key))
        if name and hasattr(config_loader, name):
            continue
        if not any(hasattr(config_loader, candidate) for candidate in candidates):
            missing.append(f"{setting.section}.{setting.key}")

    assert not missing, f"schema documents settings with no getter: {missing}"


@pytest.mark.parametrize("section,key", [
    ("grabber", "max_grabs_per_run"),
    ("grabber", "search_delay_hours"),
    ("grabber", "current_season"),
    ("grabber", "required_quality"),
    ("grabber", "allowed_groups"),
])
def test_grabber_settings_have_getters(section, key):
    import utils.config_loader as config_loader

    getter = "get_" + key
    assert hasattr(config_loader, getter), f"missing {getter}()"


# ---------------------------------------------------------------------------
# Credential provenance
# ---------------------------------------------------------------------------

def test_credential_sources_prefers_env_and_reports_it(tmp_path, monkeypatch):
    """
    The trap this exists for: a key typed into the WebUI is saved to
    settings.yml and looks like it took effect, while an environment variable in
    the container template quietly wins. Reporting the source turns a confusing
    authentication failure into an obvious one.
    """
    import utils.config_loader as config_loader

    settings = tmp_path / "settings.yml"
    settings.write_text(yaml.safe_dump({
        "prowlarr": {"api_key": "typed-in-the-ui"},
        "plex": {"token": "from-settings"},
    }))
    monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_loader, "SETTINGS_FILE", settings)
    monkeypatch.setattr(config_loader, "_settings_cache", None)
    monkeypatch.setenv("PROWLARR_API_KEY", "from-the-template")

    sources = config_loader.credential_sources()

    assert sources["prowlarr_api_key"] == "env"
    assert sources["plex_token"] == "settings.yml"
    # Only the source, never the value.
    assert "from-the-template" not in str(sources)
