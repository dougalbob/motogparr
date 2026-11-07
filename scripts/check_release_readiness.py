#!/usr/bin/env python3
"""
Release preparation check (Candidate B)
=======================================

A MotoGParr release is a *sequence*, not a single action:

    1. update unraid/motogparr.xml's TemplateURL to the candidate tag, merge it
    2. tag the merged commit  vX.Y.Z
    3. the tag builds and publishes  :X.Y.Z, :X.Y and :latest
    4. release.yml generates notes from merged PRs and opens the notes PR

During v2.11.0 step 2 was attempted before step 1 had merged. The template test
caught the mismatch - correctly - and a follow-up PR was needed before a
working release could be rebuilt. That is a sequencing hazard, not a reason to
weaken the test: the same check run *before* tagging turns a failed release
into a failed PR.

This script is that pre-flight. It is written to be run three ways:

    python scripts/check_release_readiness.py --version v2.12.0
    (by .github/workflows/release-prep.yml on every PR to main, using the
     version already pinned in the Unraid template)
    (by hand before pushing a tag)

It checks the things that are cheap locally and have actually gone wrong:

* the version is ``vX.Y.Z`` - anything else ships images without ``:X.Y``
  (``V.2.2.1`` did exactly that);
* ``unraid/motogparr.xml``'s ``TemplateURL`` pins **that** tag, so the file a
  user's Unraid "Apply" fetches is the release they are running;
* every ``COPY`` source in the Dockerfile exists, because a renamed module that
  is still copied builds a broken image that only fails at runtime;
* the tag is not already on the remote - if it is, the release has started and
  its image was built from a tree that this pre-flight cannot vouch for.

What it deliberately does **not** do: create or move a tag, publish a release,
or "fix" the template. It reports and exits non-zero. A check that repairs its
own input cannot catch the mistake it exists to catch.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = Path("unraid/motogparr.xml")
DOCKERFILE = Path("Dockerfile")

VERSION_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
TEMPLATE_URL_RE = re.compile(
    r"raw\.githubusercontent\.com/dougalbob/motogparr/([^/]+)/unraid/motogparr\.xml"
)


class Report:
    """Collects check results so every problem is reported before exiting."""

    def __init__(self) -> None:
        self.failures: List[str] = []
        self.warnings: List[str] = []
        self.notes: List[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.failures

    def render(self) -> str:
        lines = []
        for message in self.notes:
            lines.append(f"  - {message}")
        for message in self.warnings:
            lines.append(f"  warning: {message}")
        for message in self.failures:
            lines.append(f"  FAILED: {message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Individual checks (pure enough to unit test)
# ---------------------------------------------------------------------------

def check_version(version: str, report: Report) -> bool:
    """``vX.Y.Z`` only: the container workflow derives :X.Y and :latest from it."""
    if VERSION_RE.match(version or ""):
        return True
    report.fail(
        f"version {version!r} is not vX.Y.Z. The container workflow derives "
        ":MAJOR.MINOR and :latest from semver tags, so any other format ships "
        "an image with missing tags (V.2.2.1 did exactly that)."
    )
    return False


def template_pin(template_text: str) -> Optional[str]:
    """The ref ``TemplateURL`` pins, or None when the URL is not the expected one."""
    match = TEMPLATE_URL_RE.search(template_text)
    return match.group(1) if match else None


def check_template_pin(template_text: str, version: str, report: Report) -> bool:
    """
    The template's pinned ref must be the version being tagged.

    Pinning is what stops Unraid "Apply" replacing a user's edited template with
    whatever main happens to be (decision b, v2.5.0). The cost is that the pin
    must move *before* the tag exists, which is the step that was skipped.
    """
    ref = template_pin(template_text)
    if ref is None:
        report.fail(
            f"{TEMPLATE} has no TemplateURL for "
            "raw.githubusercontent.com/dougalbob/motogparr/<ref>/unraid/motogparr.xml"
        )
        return False
    if ref != version:
        report.fail(
            f"{TEMPLATE} pins {ref} but you are preparing {version}. "
            f"Bump TemplateURL to {version} and merge that PR first, then tag "
            "the merged commit. Tagging first is what forced a rebuild during "
            "v2.11.0."
        )
        return False
    report.note(f"Unraid template pins {ref}")
    return True


def dockerfile_copy_sources(dockerfile_text: str) -> List[str]:
    """Paths the image copies in, ignoring ``--flag`` options."""
    sources = []
    for line in dockerfile_text.splitlines():
        match = re.match(r"^COPY\s+(?:--\S+\s+)*(.+?)\s+\S+\s*$", line)
        if match:
            sources.append(match.group(1).strip())
    return sources


def check_copy_sources(repo_root: Path, report: Report) -> bool:
    """
    Every ``COPY`` source must exist.

    The Dockerfile copies an explicit list rather than the tree, so a renamed
    module leaves a stale line behind. Combined with an ignored build context
    or a typo, that is an image that builds and then fails at import time.
    """
    dockerfile = repo_root / DOCKERFILE
    if not dockerfile.is_file():
        report.fail(f"{DOCKERFILE} is missing")
        return False

    missing = [
        source
        for source in dockerfile_copy_sources(dockerfile.read_text(encoding="utf-8"))
        if not (repo_root / source).exists()
    ]
    if missing:
        report.fail(f"{DOCKERFILE} COPYs paths that do not exist: {missing}")
        return False
    report.note("Every Dockerfile COPY source exists")
    return True


def remote_tags(repo_root: Path) -> Optional[List[str]]:
    """Version tags on origin, or None when git/the remote is unavailable."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", "origin"],
            cwd=repo_root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    tags = []
    for line in result.stdout.splitlines():
        if "refs/tags/" not in line:
            continue
        tag = line.split("refs/tags/", 1)[1].strip()
        if VERSION_RE.match(tag):
            tags.append(tag)
    return tags


def check_tag_is_new(version: str, tags: Optional[List[str]], report: Report) -> bool:
    """
    The tag must not already exist: the image for it would be built from a tree
    this pre-flight is not checking. Re-tagging to fix a check is exactly the
    "move the tag as a workaround" this is meant to prevent.
    """
    if tags is None:
        report.warn(
            "Could not read tags from origin (no git remote here). Skipping the "
            "'tag not already published' check."
        )
        return True
    if version in tags:
        report.fail(
            f"{version} is already on the remote. Its image was built from a "
            "commit this check did not see, so do not move the tag to repair a "
            "failure - cut a new patch version instead."
        )
        return False
    report.note(f"{version} is not published yet")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run(version: str, repo_root: Path = REPO_ROOT) -> Report:
    report = Report()

    if not check_version(version, report):
        return report  # the rest of the checks key off a valid tag

    template = repo_root / TEMPLATE
    if template.is_file():
        check_template_pin(template.read_text(encoding="utf-8"), version, report)
    else:
        report.fail(f"{TEMPLATE} is missing, so no Unraid install can find it")

    check_copy_sources(repo_root, report)
    check_tag_is_new(version, remote_tags(repo_root), report)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that a MotoGParr release can be tagged safely."
    )
    parser.add_argument(
        "--version", required=True,
        help="Candidate version, exactly as it will be tagged (e.g. v2.12.0)",
    )
    parser.add_argument(
        "--repo-root", default=str(REPO_ROOT),
        help="Repository to check (defaults to this script's checkout)",
    )
    args = parser.parse_args(argv)

    report = run(args.version, Path(args.repo_root))

    print(f"Release readiness for {args.version}")
    output = report.render()
    if output:
        print(output)
    if not report.ok:
        # The annotation is the only line that survives to a CI run summary
        # view in this sandbox, so the first failure goes in the title.
        print(
            f"::error title=Release not ready::{report.failures[0]}",
            file=sys.stderr,
        )
        return 1

    print("OK - safe to tag this commit.")
    print(
        "Next: merge this PR, tag the merged commit, confirm "
        f":{args.version.lstrip('v')}, :{'.'.join(args.version.lstrip('v').split('.')[:2])} "
        "and :latest are published, then merge the release-notes PR."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
