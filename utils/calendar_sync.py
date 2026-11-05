#!/usr/bin/env python3
"""
Calendar sync helper
====================

The calendars in /config are on a persistent volume, so they survive image
updates. That is intentional - you may have edited them - but it also means a
new image can ship a corrected calendar that the running container never sees.

This module compares the calendar inside the image with the copy in /config and
reports what changed, so entrypoint.sh can either refresh it or warn loudly.

The important question is "is this the same season?". Two calendars for the same
season can differ because a round was postponed (which is exactly what happened
with Qatar in 2026). They are NOT the same season if the venues do not match -
that would mean the files are for different championships and replacing one with
the other would be wrong.

Usage:
    python3 utils/calendar_sync.py --image /app/config/motogp_2026.json \
                                   --config /config/motogp_2026.json

    python3 utils/calendar_sync.py ... --json      # machine readable
    python3 utils/calendar_sync.py ... --apply     # copy image -> config (.bak kept)
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_venue(calendar: dict) -> dict:
    """
    Index rounds by venue.

    Venue is the stable key: when a championship is renumbered, a round keeps
    its venue but changes number and date. Comparing by round number instead
    would report every single round as changed, which hides the real change.
    """
    return {
        entry.get("venue"): entry
        for entry in calendar.get("rounds", [])
        if entry.get("venue")
    }


def compare(image_path: Path, config_path: Path) -> dict:
    """
    Compare the image calendar with the one in config.

    Returns a dict with:
        same_season    - both files describe the same championship season
        identical      - byte-for-byte identical content
        change_summary - short human readable description of the differences
        differences    - structured detail
    """
    image = _load(image_path)
    config = _load(config_path)

    image_rounds = _by_venue(image)
    config_rounds = _by_venue(config)

    # "Same season" means the same set of venues. A rescheduled race keeps its
    # venue, so a postponed round still counts as the same season - which is
    # exactly the case that should be updated automatically.
    same_season = (
        image.get("season") == config.get("season")
        and set(image_rounds) == set(config_rounds)
    )

    added = [v for v in image_rounds if v not in config_rounds]
    removed = [v for v in config_rounds if v not in image_rounds]
    common = [v for v in image_rounds if v in config_rounds]

    renumbered = [
        v for v in common
        if image_rounds[v].get("round") != config_rounds[v].get("round")
    ]
    moved = [
        v for v in common
        if image_rounds[v].get("date") != config_rounds[v].get("date")
    ]
    status_changes = [
        v for v in common
        if image_rounds[v].get("status", "scheduled")
        != config_rounds[v].get("status", "scheduled")
    ]

    differences = []
    for venue in removed:
        differences.append(f"{venue} removed from the calendar")
    for venue in added:
        entry = image_rounds[venue]
        differences.append(
            f"{venue} added as round {entry.get('round')} on {entry.get('date')}"
        )
    for venue in moved:
        differences.append(
            f"{venue} moved: {config_rounds[venue].get('date')} -> "
            f"{image_rounds[venue].get('date')}"
        )
    for venue in status_changes:
        differences.append(
            f"{venue} status {config_rounds[venue].get('status', 'scheduled')} -> "
            f"{image_rounds[venue].get('status', 'scheduled')}"
        )
    if renumbered:
        example = renumbered[0]
        differences.append(
            f"{len(renumbered)} rounds renumbered "
            f"(e.g. {example}: round {config_rounds[example].get('round')} -> "
            f"{image_rounds[example].get('round')})"
        )

    if not differences:
        differences.append("same rounds and dates; session times or artwork differ")

    # A short summary for the startup log, detail stays in "differences".
    summary_parts = []
    if removed:
        summary_parts.append(f"{len(removed)} round(s) removed")
    if added:
        summary_parts.append(f"{len(added)} round(s) added")
    if moved:
        preview = ", ".join(
            f"{v} {config_rounds[v].get('date')} -> {image_rounds[v].get('date')}"
            for v in moved[:2]
        )
        more = f" (+{len(moved) - 2} more)" if len(moved) > 2 else ""
        summary_parts.append(f"dates changed: {preview}{more}")
    if renumbered:
        summary_parts.append(f"{len(renumbered)} rounds renumbered")
    if status_changes:
        summary_parts.append(f"status changed: {', '.join(status_changes[:3])}")
    if not summary_parts:
        summary_parts.append("same rounds and dates; session times or artwork differ")

    return {
        "season": image.get("season"),
        "identical": image_path.read_bytes() == config_path.read_bytes(),
        "same_season": bool(same_season),
        "rounds_in_image": len(image_rounds),
        "rounds_in_config": len(config_rounds),
        "change_summary": "; ".join(summary_parts),
        "differences": differences,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare image and config calendars")
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--apply", action="store_true",
                        help="copy the image calendar over the config copy (.bak kept)")
    args = parser.parse_args()

    for path in (args.image, args.config):
        if not path.is_file():
            print(f"❌ Not found: {path}", file=sys.stderr)
            return 2

    result = compare(args.image, args.config)

    if args.apply:
        if not result["same_season"]:
            print("❌ Refusing to overwrite: files do not describe the same season",
                  file=sys.stderr)
            return 1
        backup = args.config.with_suffix(args.config.suffix + ".bak")
        shutil.copy2(args.config, backup)
        shutil.copy2(args.image, args.config)
        result["applied"] = True
        result["backup"] = str(backup)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    label = "identical" if result["identical"] else result["change_summary"]
    print(f"season {result['season']}: {label}")
    if not result["same_season"]:
        print("⚠️  Not the same season - venues differ, do not auto-replace")
    if result.get("applied"):
        print(f"✅ Updated; previous copy saved as {result['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
