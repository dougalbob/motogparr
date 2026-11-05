#!/usr/bin/env python3
"""
Container permission check
==========================

The container drops privileges to PUID:PGID (default 99:100, which is what Unraid
calls nobody:users) before the app starts. Dropping privileges is the easy part;
the failure it produces is the problem.

A wrong owner does not look like a permissions problem. It looks like:

    * the WebUI saving settings and the values silently reverting,
    * downloads that "complete" but never get sorted,
    * `PermissionError` in a log line at 3am, in a subprocess, after a grab,
    * a container that starts perfectly and does nothing all weekend.

None of those say "your appdata folder belongs to root". So this module checks
the folders the app must write to, *before* it starts doing work, and says
exactly which one is wrong and which command fixes it.

Two design points:

  * It only checks paths the app actually writes. /media is usually writable
    only where the library lives; demanding write access to the mount root would
    produce false failures on read-only media shares.
  * It reports, it does not fix. Changing ownership of a user's media tree
    uninvited is not a startup script's business.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

#: Directories that must be writable, relative to CONFIG_DIR.
#: Everything the app persists - settings, state, calendars, logs - lives here.
#:
#: These are created if missing: /config belongs to the app. Library and
#: downloads paths deliberately are NOT created - those are the user's data
#: locations, and inventing them would hide a wrong volume mapping rather than
#: reveal it.
CONFIG_WRITABLE_ITEMS = ("", "logs")


@dataclass
class PathCheck:
    path: str
    label: str
    exists: bool
    writable: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.exists and self.writable

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "label": self.label,
            "exists": self.exists,
            "writable": self.writable,
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass
class PermissionReport:
    uid: int
    gid: int
    is_root: bool
    checks: List[PathCheck]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> List[PathCheck]:
        return [check for check in self.checks if not check.ok]

    def hint(self) -> str:
        """The command that fixes the usual cause, or '' when nothing is wrong."""
        if self.ok:
            return ""
        targets = " ".join(f"'{c.path}'" for c in self.failures)
        return (
            f"This is almost always ownership. The container runs as "
            f"{self.uid}:{self.gid}, so those folders must be owned (or writable) "
            f"by it. On Unraid the usual fix is, on the host:\n"
            f"       chown -R {self.uid}:{self.gid} {targets}\n"
            f"     or set PUID/PGID on the container to the owner of those "
            f"folders (Unraid shares are normally 99:100)."
        )

    def as_dict(self) -> dict:
        return {
            "uid": self.uid,
            "gid": self.gid,
            "is_root": self.is_root,
            "ok": self.ok,
            "checks": [check.as_dict() for check in self.checks],
            "failures": [check.path for check in self.failures],
            "hint": self.hint(),
        }


def _writable(path: str) -> bool:
    """
    Real write test: create and remove a file.

    os.access(W_OK) answers for the *real* uid in some situations and can be
    wrong where ACLs or read-only mounts are involved. Creating a file is the
    question the app is actually asking.
    """
    probe = os.path.join(path, ".motogparr-write-test")
    try:
        with open(probe, "w") as handle:
            handle.write("")
        os.unlink(probe)
        return True
    except Exception:  # noqa: BLE001 - any failure means "not writable"
        return False


def check_permissions(
    config_dir: Optional[str] = None,
    library_path: Optional[str] = None,
    downloads_path: Optional[str] = None,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
) -> PermissionReport:
    """
    Check that the app can write where it must.

    library_path and downloads_path are only checked when they exist - a missing
    downloads folder is routine on a fresh install and is already reported
    separately. What matters here is *existing but unwritable*, which is the
    ownership case.
    """
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid

    if config_dir is None:
        config_dir = os.environ.get("CONFIG_DIR", "/config")

    checks: List[PathCheck] = []

    for item in CONFIG_WRITABLE_ITEMS:
        path = os.path.join(config_dir, item) if item else config_dir
        label = f"config ({item or 'root'})"

        # /config belongs to the app, so its own subfolders are created here
        # rather than reported missing. On a fresh install logs/ does not exist
        # yet, and treating that as a failure would refuse to start every new
        # user - a first-run bug that only shows up on an empty appdata folder.
        if item and not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass  # reported below as not writable, with the reason

        exists = os.path.isdir(path)
        writable = _writable(path) if exists else False
        detail = ""
        if not exists:
            detail = "could not be created"
        elif not writable:
            detail = f"exists but is not writable by {uid}:{gid}"
        checks.append(PathCheck(path, label, exists, writable, detail))

    for path, label in ((library_path, "library"), (downloads_path, "downloads")):
        if not path:
            continue
        exists = os.path.isdir(path)
        if not exists:
            # Missing paths are the path check's business, not this one's.
            continue
        writable = _writable(path)
        checks.append(PathCheck(
            path, label, True, writable,
            "" if writable else f"exists but is not writable by {uid}:{gid}",
        ))

    return PermissionReport(uid=uid, gid=gid, is_root=(uid == 0), checks=checks)


def main() -> int:
    """python -m utils.permissions - exit 1 when a required folder is unwritable."""
    from utils.config_loader import get_downloads_path, get_motogp_library_path

    report = check_permissions(
        library_path=get_motogp_library_path(),
        downloads_path=get_downloads_path(),
    )

    print(f"Running as uid={report.uid} gid={report.gid}" + (" (root)" if report.is_root else ""))
    for check in report.checks:
        state = "ok" if check.ok else ("MISSING" if not check.exists else "NOT WRITABLE")
        print(f"  {check.label:<14} {check.path}  [{state}]")

    if not report.ok:
        print()
        print(report.hint())
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
