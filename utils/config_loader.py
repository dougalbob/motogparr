#!/usr/bin/env python3
"""
MotoGParr Configuration Loader
===============================

Loads settings with priority:
1. Environment variables
2. settings.yml (for easy configuration)
3. Defaults

Environment variables are the canonical way to configure a container, because
they are what a launcher (Unraid's template, docker run, compose) can actually
set. Every variable the app understands is declared once in ENV_SPEC below, and
that table is the single source of truth: the loader reads it, the Unraid
template is checked against it in the tests, and the docs are generated from it.

An environment variable that is set but EMPTY counts as "not set", so a template
can ship a blank field without silently overriding settings.yml.

Usage:
    from utils.config_loader import get_prowlarr_url
    
    prowlarr_url = get_prowlarr_url()
    # or
    from utils.config_loader import get
    prowlarr_url = get("prowlarr", "url")
"""

import os
import yaml
from pathlib import Path
from typing import Any, Optional, Dict

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
SETTINGS_FILE = CONFIG_DIR / "settings.yml"

_settings_cache: Optional[Dict] = None


# =============================================================================
# ENVIRONMENT VARIABLE REGISTRY
# =============================================================================
#
# The single source of truth for "which variables does this app read?".
#
# Every entry maps an environment variable name to the settings.yml location it
# overrides. Nested keys are dotted ("path_mapping.container"). If a variable is
# missing from this table, the app does not read it - which is exactly the bug
# that made PLEX_PATH_CONTAINER / PLEX_PATH_PLEX documented but dead.
#
# Keys:
#   section     settings.yml section it overrides
#   key         settings.yml key (dotted for nested), or None for a top-level
#               setting that has no yaml equivalent (e.g. TZ)
#   cast        callable applied to the raw string
#   secret      never echo the value in logs / debug output
#   description one line, used by `python -m utils.config_loader --env-help`

ENV_SPEC: Dict[str, Dict[str, Any]] = {
    # --- services -----------------------------------------------------------
    "PROWLARR_URL": {
        "section": "prowlarr", "key": "url", "cast": str,
        "description": "Prowlarr base URL, e.g. http://192.168.1.100:9696",
    },
    "PROWLARR_API_KEY": {
        "section": "prowlarr", "key": "api_key", "cast": str, "secret": True,
        "description": "Prowlarr API key",
    },
    "SABNZBD_URL": {
        "section": "sabnzbd", "key": "url", "cast": str,
        "description": "SABnzbd base URL, e.g. http://192.168.1.100:8080",
    },
    "SABNZBD_API_KEY": {
        "section": "sabnzbd", "key": "api_key", "cast": str, "secret": True,
        "description": "SABnzbd API key",
    },
    "PLEX_URL": {
        "section": "plex", "key": "url", "cast": str,
        "description": "Plex base URL, e.g. http://192.168.1.100:32400",
    },
    "PLEX_TOKEN": {
        "section": "plex", "key": "token", "cast": str, "secret": True,
        "description": "Plex token",
    },
    "MOTOGP_PLEX_LIBRARY_ID": {
        "section": "plex", "key": "library_id", "cast": str,
        "description": "Plex library section id (the number in /library/sections/<id>/)",
    },
    "MOTOGP_PLEX_LIBRARY": {
        "section": "plex", "key": "library_name", "cast": str,
        "description": "Plex library display name",
    },

    # --- paths --------------------------------------------------------------
    "MOTOGP_LIBRARY_PATH": {
        "section": "library", "key": "motogp_path", "cast": str,
        "description": "MotoGP library path INSIDE the container",
    },
    "MOTOGP_DOWNLOADS_PATH": {
        "section": "library", "key": "downloads_path", "cast": str,
        "description": "Completed-downloads path INSIDE the container",
    },

    # --- Plex path translation ---------------------------------------------
    # Plex reports paths as its own container sees them. Without this mapping
    # the tagger compares two different strings and silently never matches,
    # which looks like "tagging stopped working" after any path change.
    "PLEX_PATH_CONTAINER": {
        "section": "plex", "key": "path_mapping.container", "cast": str,
        "description": "Path prefix as this container sees it, e.g. /media",
    },
    "PLEX_PATH_PLEX": {
        "section": "plex", "key": "path_mapping.plex", "cast": str,
        "description": "The same prefix as Plex sees it, e.g. /data/media",
    },

    # --- misc ---------------------------------------------------------------
    "DISCORD_WEBHOOK_URL": {
        "section": "discord", "key": "webhook_url", "cast": str, "secret": True,
        "description": "Discord webhook URL for notifications (optional)",
    },
    "TZ": {
        "section": "timezone", "key": None, "cast": str,
        "description": "Timezone, e.g. Europe/London",
    },

    # --- authentication -----------------------------------------------------
    # Cloudflare Access verification. These live in ENV_SPEC because that table
    # is the single source of truth for "what does the app read", and the Unraid
    # template is checked against it. settings.yml is deliberately NOT an option:
    # an allowlist in a file the UI can rewrite is not a security control.
    "AUTH_ISSUER": {
        "section": "auth", "key": "issuer", "cast": str,
        "description": "Cloudflare Access issuer, e.g. https://<team>.cloudflareaccess.com",
    },
    "AUTH_AUDIENCE": {
        "section": "auth", "key": "audience", "cast": str,
        "description": "Access application AUD tag the JWT must be issued for",
    },
    "AUTH_ALLOWED_EMAILS": {
        "section": "auth", "key": "allowed_emails", "cast": str, "secret": True,
        "description": "Comma-separated emails allowed in (mirrors the CF policy)",
    },
    "AUTH_CERTS_URL": {
        "section": "auth", "key": "certs_url", "cast": str,
        "description": "Optional override for the JWKS URL (defaults to $AUTH_ISSUER/cdn-cgi/access/certs)",
    },
    "AUTH_MODE": {
        "section": "auth", "key": "mode", "cast": str,
        "description": "cloudflare (default when AUTH_ISSUER is set) or none",
    },
    "MOTOGPARR_COOKIE_SECURE": {
        "section": "auth", "key": "cookie_secure", "cast": str,
        "description": "1 = mark the session cookie Secure (only when served over https)",
    },
}


#: Variables read by entrypoint.sh rather than by the Python code. Kept separate
#: so "the app reads this" stays literally true for ENV_SPEC.
ENTRYPOINT_ENV: Dict[str, str] = {
    "MOTOGPARR_CALENDAR_UPDATE":
        "auto | warn | off - what to do when the /config calendars differ",
    "MOTOGPARR_SKIP_PATH_CHECK":
        "1 = start even when a configured path does not exist",
}

#: Informational only: set by the Dockerfile / build workflow, never a setting.
INFORMATIONAL_ENV = ("MOTOGPARR_VERSION",)


def all_known_env_vars() -> "set[str]":
    """Every variable this project understands, from any of the three sources."""
    return set(ENV_SPEC) | set(ENTRYPOINT_ENV) | set(INFORMATIONAL_ENV)


def _lookup_env_key(section: str, key: Optional[str]) -> Optional[str]:
    """Find the environment variable that overrides (section, key), if any."""
    for name, spec in ENV_SPEC.items():
        if spec["section"] == section and spec["key"] == key:
            return name
    return None


def _load_yaml() -> Dict:
    """Load settings from YAML file."""
    global _settings_cache
    
    if _settings_cache is not None:
        return _settings_cache
    
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, 'r') as f:
                _settings_cache = yaml.safe_load(f) or {}
                return _settings_cache
        except Exception as e:
            print(f"Warning: Could not load settings.yml: {e}")
    
    _settings_cache = {}
    return _settings_cache


def reload_settings():
    """Force reload of settings from disk."""
    global _settings_cache
    _settings_cache = None
    return _load_yaml()


def _yaml_lookup(settings: Dict, section: str, key: Optional[str]) -> Any:
    """Read a possibly-nested key out of the loaded settings.yml."""
    if key is None:
        return settings.get(section)

    node: Any = settings.get(section)
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def get_with_source(
    section: str, key: Optional[str], default: Any = None
) -> "tuple[Any, str]":
    """
    Resolve a setting and report where the value came from.

    Priority: environment variable > settings.yml > default.

    Returns:
        (value, source) where source is "env", "settings.yml" or "default".
        Knowing the source matters for paths: a container whose template
        overrides a path will not behave like settings.yml says it should.
    """
    env_name = _lookup_env_key(section, key)

    if env_name:
        env_value = os.environ.get(env_name)
        if env_value:  # empty string means "not set", so templates can ship blanks
            cast = ENV_SPEC[env_name].get("cast", str)
            return cast(env_value), "env"

    value = _yaml_lookup(_load_yaml(), section, key)
    if value is not None and value != "":
        return value, "settings.yml"

    return default, "default"


def get(section: str, key: Optional[str], default: Any = None) -> Any:
    """
    Get a setting value.

    Args:
        section: Section in YAML (e.g., 'prowlarr', 'plex')
        key: Key within section (e.g., 'url', 'api_key'). Dotted for nested
             keys (e.g. 'path_mapping.container'). Use None for a standalone
             setting such as the timezone.
        default: Default value if not found

    Returns:
        Setting value
    """
    return get_with_source(section, key, default)[0]


def get_int(section: str, key: str, default: int = 0) -> int:
    """Get setting as integer."""
    value = get(section, key)
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def get_bool(section: str, key: str, default: bool = False) -> bool:
    """Get setting as boolean."""
    value = get(section, key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return default


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def get_prowlarr_url() -> Optional[str]:
    return get("prowlarr", "url")


def get_prowlarr_api_key() -> Optional[str]:
    return get("prowlarr", "api_key")


def get_sabnzbd_url() -> Optional[str]:
    return get("sabnzbd", "url")


def get_sabnzbd_api_key() -> Optional[str]:
    return get("sabnzbd", "api_key")


def get_plex_url() -> Optional[str]:
    return get("plex", "url")


def get_plex_token() -> Optional[str]:
    return get("plex", "token")


def get_plex_library_id() -> Optional[str]:
    return get("plex", "library_id")


def get_plex_library_name() -> str:
    return get("plex", "library_name", "Motorsport - MotoGP")


def get_motogp_library_path() -> str:
    return get("library", "motogp_path", "/media/MotoGP")


def get_downloads_path() -> str:
    return get("library", "downloads_path", "/downloads/complete/motogp")


def get_discord_webhook() -> Optional[str]:
    return get("discord", "webhook_url")


def get_timezone() -> str:
    """Timezone name. TZ wins (Docker sets it), then settings.yml, then UK."""
    return get("timezone", None, "Europe/London")


def get_plex_path_mapping() -> dict:
    """
    Get the Plex path mapping used to translate container paths.

    Read per-key rather than as one dict, so each side can come from either
    PLEX_PATH_CONTAINER / PLEX_PATH_PLEX or settings.yml:

        plex:
          path_mapping:
            container: /media
            plex: /data/media
    """
    container = get("plex", "path_mapping.container")
    plex = get("plex", "path_mapping.plex")
    return {
        "container": str(container) if container else None,
        "plex": str(plex) if plex else None,
    }


def plex_path_mapping_complete() -> bool:
    """True when both sides of the mapping are set, i.e. translation can work."""
    mapping = get_plex_path_mapping()
    return bool(mapping["container"] and mapping["plex"])


def translate_path_for_plex(container_path: str) -> str:
    """Translate a container path to what Plex expects."""
    mapping = get_plex_path_mapping()
    container_prefix = mapping.get("container")
    plex_prefix = mapping.get("plex")
    
    if container_prefix and plex_prefix and container_path.startswith(container_prefix):
        return container_path.replace(container_prefix, plex_prefix, 1)
    
    return container_path


# =============================================================================
# GRABBER-SPECIFIC SETTINGS
# =============================================================================

def get_allowed_groups() -> list:
    """Get list of allowed release groups."""
    groups = get("grabber", "allowed_groups")
    if isinstance(groups, list):
        return groups
    return ["VERUM", "MWR", "BILLIE"]


def get_required_quality() -> str:
    """Get required video quality."""
    return get("grabber", "required_quality", "1080p")


def get_allowed_groups() -> list:
    """
    Release groups the grabber will accept.

    Read from config but never written by the UI before v2.4.2, so it was
    effectively a hidden setting: present in settings.yml, invisible everywhere
    else. Accepts a comma-separated string so ALLOWED_GROUPS-style env config
    works too.
    """
    value = get("grabber", "allowed_groups")
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def get_required_quality() -> str:
    return get("grabber", "required_quality", "1080p")


def get_max_grabs_per_run() -> int:
    """Get maximum grabs per run."""
    return get_int("grabber", "max_grabs_per_run", 22)


def get_search_delay_hours() -> int:
    """Get hours to wait after session before searching."""
    return get_int("grabber", "search_delay_hours", 1)


def get_current_season() -> int:
    """Get current season year."""
    return get_int("grabber", "current_season", 2026)


# =============================================================================
# VALIDATION
# =============================================================================

def validate() -> Dict[str, Any]:
    """
    Validate that required settings are configured.
    
    Returns:
        Dict with 'valid' bool, 'missing' list, and 'configured' list
    """
    required = [
        ("prowlarr", "url", "Prowlarr URL"),
        ("prowlarr", "api_key", "Prowlarr API Key"),
        ("sabnzbd", "url", "SABnzbd URL"),
        ("sabnzbd", "api_key", "SABnzbd API Key"),
        ("plex", "url", "Plex URL"),
        ("plex", "token", "Plex Token"),
    ]
    
    optional = [
        ("plex", "library_id", "Plex Library ID"),
        ("plex", "library_name", "Plex Library Name"),
        ("library", "motogp_path", "MotoGP Library Path"),
        ("library", "downloads_path", "Downloads Path"),
        ("discord", "webhook_url", "Discord Webhook"),
    ]
    
    missing = []
    configured = []
    
    for section, key, name in required:
        value = get(section, key)
        if value:
            configured.append(name)
        else:
            missing.append(name)
    
    for section, key, name in optional:
        value = get(section, key)
        if value:
            configured.append(name)
    
    return {
        "valid": len(missing) == 0,
        "missing": missing,
        "configured": configured,
        "settings_file": str(SETTINGS_FILE),
        "settings_file_exists": SETTINGS_FILE.exists(),
    }


def get_all_settings() -> Dict[str, Any]:
    """Get all current settings (for display/debugging)."""
    return {
        "prowlarr": {
            "url": get_prowlarr_url(),
            "api_key": "***" if get_prowlarr_api_key() else None,
        },
        "sabnzbd": {
            "url": get_sabnzbd_url(),
            "api_key": "***" if get_sabnzbd_api_key() else None,
        },
        "plex": {
            "url": get_plex_url(),
            "token": "***" if get_plex_token() else None,
            "library_id": get_plex_library_id(),
            "library_name": get_plex_library_name(),
            "path_mapping": get_plex_path_mapping(),
        },
        "library": {
            "motogp_path": get_motogp_library_path(),
            "downloads_path": get_downloads_path(),
        },
        "grabber": {
            "allowed_groups": get_allowed_groups(),
            "required_quality": get_required_quality(),
            "max_grabs_per_run": get_max_grabs_per_run(),
            "search_delay_hours": get_search_delay_hours(),
            "current_season": get_current_season(),
        },
        "discord": {
            "webhook_url": "***" if get_discord_webhook() else None,
        },
        "timezone": get_timezone(),
    }


# =============================================================================
# STARTUP DIAGNOSTICS
# =============================================================================

#: Settings whose value is a path inside the container. A wrong one of these
#: looks identical to "the share is not mounted": the app starts happily and
#: quietly does nothing.
PATH_SETTINGS = [
    ("library", "motogp_path", "Library path"),
    ("library", "downloads_path", "Downloads path"),
]


def describe_setting(section: str, key: Optional[str], label: str) -> Dict[str, Any]:
    """Resolve one setting and say where it came from and whether it exists."""
    value, source = get_with_source(section, key)
    info: Dict[str, Any] = {
        "label": label,
        "section": section,
        "key": key,
        "value": value,
        "source": source,
        "env_var": _lookup_env_key(section, key),
        "exists": None,
        "warning": None,
    }
    if value:
        info["exists"] = Path(str(value)).exists()
        if not info["exists"]:
            info["warning"] = (
                f"{label} does not exist inside the container: {value}"
            )
    else:
        info["warning"] = f"{label} is not configured"
    return info


def startup_report() -> Dict[str, Any]:
    """
    Everything the entrypoint should print before starting work.

    The point is that a misconfigured path is loud at startup instead of
    silently producing wrong results hours later.
    """
    paths = [
        describe_setting(section, key, label)
        for section, key, label in PATH_SETTINGS
    ]

    mapping = get_plex_path_mapping()
    mapping_complete = plex_path_mapping_complete()

    warnings = [p["warning"] for p in paths if p["warning"]]

    if not mapping_complete and paths[0]["value"]:
        warnings.append(
            "Plex path mapping is incomplete - Plex will not be told about new "
            "files by exact path. Set plex.path_mapping (container/plex) in "
            "settings.yml, or PLEX_PATH_CONTAINER / PLEX_PATH_PLEX."
        )

    return {
        "paths": paths,
        "plex_path_mapping": mapping,
        "plex_path_mapping_complete": mapping_complete,
        "settings_file": str(SETTINGS_FILE),
        "settings_file_exists": SETTINGS_FILE.exists(),
        "warnings": warnings,
    }


# =============================================================================
# SETTINGS FILE MANAGEMENT
# =============================================================================

def create_template() -> bool:
    """Create settings.yml template if it doesn't exist."""
    if SETTINGS_FILE.exists():
        return False
    
    template = """# MotoGParr Configuration
# ========================
# Edit these values to match your setup.
# Environment variables take priority over these settings.
#
# IMPORTANT: Path Configuration
# -----------------------------
# Paths in this file are CONTAINER paths, not host paths.
# They must match your Docker volume mounts.
#
# Example volume mounts (docker run):
#   -v /mnt/user/data/media:/media
#   -v /mnt/user/data/downloads:/downloads
#   -v /mnt/user/appdata/motogparr:/config
#
# To find your Plex library ID:
#   1. Open Plex web interface
#   2. Go to your MotoGP library
#   3. Look at the URL - it contains: /library/sections/X/
#   4. X is your library_id

prowlarr:
  url: ""          # e.g., http://192.168.1.100:9696
  api_key: ""

sabnzbd:
  url: ""          # e.g., http://192.168.1.100:8080
  api_key: ""

plex:
  url: ""          # e.g., http://192.168.1.100:32400
  token: ""        # See: https://support.plex.tv/articles/204059436
  library_id: ""   # Plex library section ID (see instructions above)
  library_name: "Motorsport - MotoGP"
  path_mapping:    # Both required if Plex sees the files under a different path
    container: ""  # Path prefix as THIS container sees it, e.g. /media
    plex: ""       # The same prefix as PLEX sees it, e.g. /data/media

library:
  motogp_path: "/media/MotoGP"
  downloads_path: "/downloads/complete/motogp"

grabber:
  allowed_groups: ["VERUM", "MWR"]
  required_quality: "1080p"
  max_grabs_per_run: 22
  search_delay_hours: 4
  current_season: 2026

discord:
  webhook_url: ""  # Optional: for notifications

timezone: "Europe/London"
"""
    
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, 'w') as f:
            f.write(template)
        return True
    except Exception as e:
        print(f"Error creating settings template: {e}")
        return False


def update_settings(updates: Dict) -> bool:
    """
    Merge ``updates`` into settings.yml instead of replacing the file.

    This exists because replacing the file was silently destructive: the WebUI
    used to rebuild the whole document from the four sections it knew about, so
    every save from the UI deleted anything else in the file - the admin
    password, the entire grabber section, and any key added since. Nobody
    noticed because nothing failed; the settings just quietly reverted.

    Rules:
      * nested dicts are merged key by key, so a partial ``plex`` payload does
        not drop ``plex.library_name``;
      * a key explicitly set to ``None`` is deleted (that is how the caller says
        "remove this"), an omitted key is left alone;
      * an empty string is kept, because "clear this field" is a real intent and
        the env-var layer already treats empty as unset.
    """
    global _settings_cache

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        current = dict(_load_yaml() or {})
        merged = _deep_merge(current, updates)

        # Write via a temporary file so a crash mid-write cannot leave a
        # truncated settings.yml behind - the file is small, the cost is nil.
        tmp_path = SETTINGS_FILE.with_suffix(".yml.tmp")
        with open(tmp_path, "w") as f:
            yaml.dump(merged, f, default_flow_style=False, sort_keys=False)
        tmp_path.replace(SETTINGS_FILE)

        _settings_cache = merged
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False


def _deep_merge(base: Dict, updates: Dict) -> Dict:
    """Recursively merge updates into a copy of base. See update_settings."""
    result = dict(base)
    for key, value in updates.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def save_settings(settings: Dict) -> bool:
    """
    Replace settings.yml wholesale.

    Kept for callers that genuinely own the whole document (the template
    creator). Prefer :func:`update_settings`, which cannot lose keys it was not
    told about.
    """
    global _settings_cache

    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, 'w') as f:
            yaml.dump(settings, f, default_flow_style=False, sort_keys=False)
        _settings_cache = settings
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False


# =============================================================================
# MODULE TEST
# =============================================================================

def env_help() -> str:
    """Render every environment variable the project understands."""
    width = max(len(name) for name in all_known_env_vars())

    lines = ["Read by the Python code (override settings.yml):", ""]
    for name, spec in sorted(ENV_SPEC.items()):
        section = spec["section"]
        key = spec["key"]
        target = section if key is None else f"{section}.{key}"
        flags = " (secret)" if spec.get("secret") else ""
        lines.append(
            f"  {name.ljust(width)}  {target.ljust(28)} {spec['description']}{flags}"
        )

    lines += ["", "Read by entrypoint.sh at container start:", ""]
    for name, description in sorted(ENTRYPOINT_ENV.items()):
        lines.append(f"  {name.ljust(width)}  {'(container start)'.ljust(28)} {description}")

    lines += ["", "Informational (set by the image build):", ""]
    for name in INFORMATIONAL_ENV:
        lines.append(f"  {name.ljust(width)}  {'(reported at startup)'.ljust(28)} Which release is running")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if "--env-help" in sys.argv:
        print("Environment variables understood by MotoGParr:")
        print()
        print(env_help())
        raise SystemExit(0)

    print("=== MotoGParr Configuration ===")
    print(f"Settings file: {SETTINGS_FILE}")
    print(f"Exists: {SETTINGS_FILE.exists()}")
    print()

    validation = validate()
    if validation["valid"]:
        print("✅ All required settings configured")
    else:
        print("❌ Missing required settings:")
        for item in validation["missing"]:
            print(f"   - {item}")

    print()
    print("Paths:")
    report = startup_report()
    for entry in report["paths"]:
        state = "ok" if entry["exists"] else "MISSING"
        print(f"  {entry['label']}: {entry['value']}  [{entry['source']}, {state}]")
    print(f"  Plex path mapping: {report['plex_path_mapping']}")

    for warning in report["warnings"]:
        print(f"  ! {warning}")

    print()
    print("Configured:")
    for item in validation["configured"]:
        print(f"   ✓ {item}")
