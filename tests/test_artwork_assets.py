"""Artwork is shared in the image, but Plex receives independent JPEGs.

Issue #25: every venue/class placeholder was an identical ~2 MB poster and
~1 MB fanart. Git deduplicated the blobs, but Docker copied all 138 files into
the image. The calendar filenames remain stable; only their source files are
now relative links to two masters. Custom artwork can replace a link with a
real JPEG without changing the calendar or the overlay code.
"""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from motogp_common import CLASS_ASSET_DIRS, SessionType

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
MASTERS = {
    "poster": ASSETS / "motogpPoster.jpg",
    "fanart": ASSETS / "motogpFanart.jpg",
}


def _digest(path):
    return hashlib.sha256(path.read_bytes()).digest()


def test_unused_2025_external_artwork_is_gone():
    """The old pre-rendered images had no runtime callers and dominated the image."""
    assert not (ASSETS / "external" / "2025").exists()


def test_every_calendar_asset_resolves_inside_the_image():
    """A dangling or escaping link would silently leave Plex without artwork."""
    referenced = set()
    for calendar_path in sorted((ROOT / "config").glob("motogp_*.json")):
        calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
        for round_info in calendar["rounds"]:
            for class_key, artwork in round_info.get("assets", {}).items():
                for kind, filename in artwork.items():
                    source = CLASS_ASSET_DIRS[class_key][kind] / filename
                    assert source.is_file(), f"{calendar_path.name}: missing {source}"
                    if source.is_symlink():
                        assert not source.readlink().is_absolute(), source
                        assert source.resolve().is_relative_to(ASSETS), source
                    referenced.add(source)
    assert referenced, "no calendar artwork was checked"


@pytest.mark.parametrize("class_key", ("motogp", "moto2", "moto3"))
@pytest.mark.parametrize("kind", ("poster", "fanart"))
def test_placeholders_are_links_not_duplicate_jpegs(class_key, kind):
    """Keep future venue art possible, but never reintroduce copied placeholders."""
    master = MASTERS[kind]
    directory = CLASS_ASSET_DIRS[class_key][kind]
    files = sorted(directory.glob("*.jpg"))
    assert files, f"no {kind} for {class_key}"
    for source in files:
        assert source.is_file(), f"broken link: {source}"
        if source.is_symlink():
            assert source.readlink() == Path("..") / master.name, source
            assert source.samefile(master), source
        else:
            # A real, venue-specific replacement is fine. An unchanged copy of
            # the shared placeholder would inflate the image again.
            assert _digest(source) != _digest(master), f"copied placeholder: {source}"


@pytest.mark.parametrize("session", (
    SessionType.MOTOGP_RACE, SessionType.MOTO2_RACE, SessionType.MOTO3_RACE,
))
def test_sorter_writes_real_artwork_without_modifying_shared_source(tmp_path, monkeypatch, session):
    """Plex has its own mount: it must receive files, not links into /app/assets."""
    import motogp_sort as sorter

    monkeypatch.setattr(sorter, "DRY_RUN", False)
    round_info = json.loads((ROOT / "config" / "motogp_2026.json").read_text())["rounds"][0]
    original = {kind: _digest(path) for kind, path in MASTERS.items()}

    sorter.apply_all_artwork(str(tmp_path), round_info, session)

    for name in ("poster.jpg", "fanart.jpg", "thumb.jpg"):
        output = tmp_path / name
        assert output.is_file() and not output.is_symlink(), name
        with Image.open(output) as image:
            image.verify()
    assert {kind: _digest(path) for kind, path in MASTERS.items()} == original
