#!/usr/bin/env python3
"""
Why did that session not download? (Candidate C)
================================================

Today a session that fails is recorded with a string: ``"No results"``,
``"Grab failed"``, or ``"Processor returned False"``. Those are different
problems wearing the same label, and the difference is the whole diagnosis:

* Prowlarr is down or its API key is wrong - **the machine is broken**, and no
  amount of waiting will help;
* Prowlarr answered but the indexer rejected the request (401/403) - a
  different broken thing, in a specific place;
* no candidates came back - **nothing has been published yet**, waiting is
  correct, nothing is wrong;
* candidates came back and every one was rejected by the filter - the rules may
  be wrong, which is what the mismatch dialog exists for;
* candidates were filtered, accepted, and SABnzbd refused the submission - the
  downloader is broken;
* SABnzbd accepted it and the job died - the release itself is bad.

Before this module the first two collapsed into "No results" and looked
identical to "the release has not aired yet", which is the failure mode
AGENTS.md calls out repeatedly: the machine going quiet while everything looks
normal.

The stages are a closed vocabulary in one place so the grabber, the WebUI and
the tests cannot disagree about the wording. :func:`summarise` produces the one
line the user reads; :func:`sanitise` guarantees no API key or credential-bearing
URL reaches it - the Download tab is authenticated, but a reason string is
copied into support reports and logs, and this codebase has already leaked a
Prowlarr key once (AGENTS.md §7a).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------
#: Named stages, in pipeline order. Each is a distinct actor to go and look at.
STAGE_SEARCH_SETUP = "search_setup"        # Prowlarr not configured / unreachable
STAGE_SEARCH_INDEXER = "search_indexer"    # indexer refused the request
STAGE_NO_CANDIDATES = "no_candidates"      # search worked, nothing published
STAGE_REJECTED = "rejected"                # candidates found, all filtered out
STAGE_BLOCKED = "blocked"                  # every candidate marked as a mismatch
STAGE_SAB_SUBMIT = "sab_submit"            # SABnzbd refused / unreachable
STAGE_DOWNLOAD = "download"                # accepted by SAB, then died
STAGE_PROCESS = "process"                  # sorter/tagger failed

STAGES = (
    STAGE_SEARCH_SETUP,
    STAGE_SEARCH_INDEXER,
    STAGE_NO_CANDIDATES,
    STAGE_REJECTED,
    STAGE_BLOCKED,
    STAGE_SAB_SUBMIT,
    STAGE_DOWNLOAD,
    STAGE_PROCESS,
)

#: Which stages mean "something is broken" rather than "wait". This is the
#: distinction the UI needs: a retry is pointless for the former until a human
#: fixes it, and mandatory for the latter.
ACTIONABLE = {
    STAGE_SEARCH_SETUP,
    STAGE_SEARCH_INDEXER,
    STAGE_SAB_SUBMIT,
    STAGE_DOWNLOAD,
    STAGE_PROCESS,
}

LABELS = {
    STAGE_SEARCH_SETUP: "Search service",
    STAGE_SEARCH_INDEXER: "Indexer",
    STAGE_NO_CANDIDATES: "Not published yet",
    STAGE_REJECTED: "Rejected by filter",
    STAGE_BLOCKED: "Blocked as mismatch",
    STAGE_SAB_SUBMIT: "Downloader",
    STAGE_DOWNLOAD: "Download",
    STAGE_PROCESS: "Processing",
}

#: One short instruction per stage. The point of the field is to tell the user
#: what to *do*, not to restate the failure.
HINTS = {
    STAGE_SEARCH_SETUP: "Check the Prowlarr URL and API key in Admin settings.",
    STAGE_SEARCH_INDEXER: "Check the indexer in Prowlarr. The scheduler keeps retrying.",
    STAGE_NO_CANDIDATES: "Nothing published yet. The scheduler retries automatically.",
    STAGE_REJECTED: "If a shown release was the right one, mark it in the mismatch dialog.",
    STAGE_BLOCKED: "Every candidate is marked as wrong. Unblock one if that was a mistake.",
    STAGE_SAB_SUBMIT: "Check the SABnzbd URL, API key and that it is not paused.",
    STAGE_DOWNLOAD: "The release may be bad. Clear it and download the session again.",
    STAGE_PROCESS: "Check the watcher and sorter logs; the folder is still in downloads.",
}

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

#: ``scheme://user:pass@host:port/path?query`` - keep only the origin, so the
#: reader can tell *which* service was unreachable without the path, query or
#: anything embedded in them. Prowlarr's own search URL carries ``apikey=...``
#: in the query string, and a requests exception renders the whole URL.
_URL_RE = re.compile(r"\b(https?)://([^/\s?#]+)([^\s]*)?", re.IGNORECASE)

#: ``apikey=...`` / ``X-Plex-Token=...`` style parameters, including ones that
#: appear without a scheme (already-redacted URLs keep their query).
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|token|x-plex-token|x-api-key|password|secret)"
    r"(\s*[=:]\s*)([^\s&,;'\"\)]+)"
)

#: Anything that looks like a long opaque credential standing on its own.
_BARE_KEY_RE = re.compile(r"\b[A-Za-z0-9]{32}\b")


def sanitise(text: Any, secrets: Optional[Iterable[str]] = None) -> str:
    """
    Remove credentials and credential-bearing URLs from a message.

    Three passes, because one is not enough in practice:

    1. every *configured* secret value is replaced verbatim - this catches the
       case where the key appears with no recognisable parameter name;
    2. ``scheme://host:port`` is kept and everything after it is dropped, which
       removes ``/1/api?t=search&apikey=...`` while still saying where;
    3. named credential parameters anywhere else, and any bare 32-character
       alphanumeric token (the shape of a Prowlarr/SABnzbd key).

    The result is what goes into ``grabber_state.json`` and therefore into the
    WebUI and any "Copy report" the user makes, so it must be safe on its own.
    """
    if text is None:
        return ""
    message = str(text)

    for secret in secrets or []:
        if secret and len(str(secret)) >= 6:
            message = message.replace(str(secret), "***")

    message = _URL_RE.sub(lambda m: f"{m.group(1)}://{m.group(2)}", message)
    message = _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", message)
    message = _BARE_KEY_RE.sub("***", message)
    return message


# ---------------------------------------------------------------------------
# Building a record
# ---------------------------------------------------------------------------

def make(stage: str, detail: str = "", *, secrets: Optional[Iterable[str]] = None,
         now: Optional[datetime] = None, **extra: Any) -> Dict[str, Any]:
    """
    One structured failure record, ready to store in ``grabber_state.json``.

    ``reason`` stays a plain sentence so every existing consumer - the Download
    tab warning, ``/failed-sessions``, the cleanup utility's expectations -
    keeps working unchanged. The new fields are additive: ``stage`` says which
    actor failed, ``hint`` says what to do, ``actionable`` says whether waiting
    can possibly help.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown failure stage: {stage!r}")

    cleaned = sanitise(detail, secrets)
    record: Dict[str, Any] = {
        "timestamp": (now or datetime.now()).isoformat(),
        "stage": stage,
        "stage_label": LABELS[stage],
        "reason": summarise(stage, cleaned),
        "detail": cleaned,
        "hint": HINTS[stage],
        "actionable": stage in ACTIONABLE,
    }
    for key, value in extra.items():
        if value is not None:
            record[key] = sanitise(value, secrets) if isinstance(value, str) else value
    return record


def summarise(stage: str, detail: str = "") -> str:
    """
    The one line the Download tab shows.

    It always names the stage, because "Grab failed" alone told the user nothing
    about whether to wait or to fix something, and that ambiguity is the defect
    this exists to remove.
    """
    label = LABELS.get(stage, stage)
    detail = (detail or "").strip().rstrip(".")
    if not detail:
        return label
    return f"{label}: {detail}"


# ---------------------------------------------------------------------------
# Search diagnostics -> cause
# ---------------------------------------------------------------------------

def explain_search(diagnostics: Optional[dict]) -> Dict[str, Any]:
    """
    Turn one search's counters into the stage that explains it.

    ``diagnostics`` is filled by ``motogp_grabber.search_for_session`` and has
    the shape::

        {"indexers": 2, "indexers_failed": 1, "candidates": 0,
         "filtered": 0, "excluded": 0, "setup_error": None,
         "errors": ["Prowlarr: ..."]}

    The order of the checks is the order the pipeline narrows in, and it is
    deliberately not "whatever is non-zero first": a setup error outranks an
    empty result, because "Prowlarr is unreachable" and "nothing is published"
    produce the same empty candidate list but demand opposite responses.

    It never guesses: a search that produced candidates and filtered them all
    out is ``REJECTED`` even when the count is one, and an ambiguous shape
    (no diagnostics at all) reports ``NO_CANDIDATES`` rather than inventing a
    cause.
    """
    diag = diagnostics or {}

    setup_error = diag.get("setup_error")
    if setup_error:
        return _explain(STAGE_SEARCH_SETUP, setup_error)

    indexers = int(diag.get("indexers") or 0)
    failed = int(diag.get("indexers_failed") or 0)
    errors: List[str] = list(diag.get("errors") or [])
    candidates = int(diag.get("candidates") or 0)
    filtered = int(diag.get("filtered") or 0)

    # Every indexer errored: the search never really happened. Reporting "no
    # releases yet" here would be a lie that hides a broken connection.
    if indexers and failed >= indexers and candidates == 0:
        detail = errors[0] if errors else f"all {indexers} indexer(s) failed"
        return _explain(STAGE_SEARCH_INDEXER, detail, indexers=indexers)

    if candidates == 0:
        # Some indexers failing while others answer is worth saying: results
        # are incomplete, which is not the same as "nothing published".
        detail = ""
        if failed:
            detail = f"{failed} of {indexers} indexer(s) failed"
        return _explain(STAGE_NO_CANDIDATES, detail, indexers=indexers,
                        indexers_failed=failed)

    if filtered == 0:
        # Everything found was blocked as a marked mismatch - a deliberate,
        # user-created dead end, and it must not read as "the rules rejected it".
        if diag.get("all_blocked"):
            return _explain(
                STAGE_BLOCKED,
                f"all {candidates} candidate(s) are marked as wrong",
                candidates=candidates,
            )
        return _explain(
            STAGE_REJECTED,
            f"{candidates} candidate(s) found, none matched the session rules",
            candidates=candidates,
        )

    return {"stage": "", "detail": "", "actionable": False}


def _explain(stage: str, detail: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "stage": stage,
        "stage_label": LABELS[stage],
        "detail": detail,
        "hint": HINTS[stage],
        "actionable": stage in ACTIONABLE,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


# ---------------------------------------------------------------------------
# Reading stored records (WebUI side)
# ---------------------------------------------------------------------------

def describe(entry: Any) -> Dict[str, Any]:
    """
    Normalise a stored ``failed`` entry for display.

    Old entries are a bare ``{"timestamp", "reason"}``: they are shown with
    their reason and no stage, rather than being assigned one. Guessing that
    "No results" meant ``no_candidates`` would relabel a historic Prowlarr
    outage as "nothing published", which is the exact confusion this removes.
    """
    if not isinstance(entry, dict):
        return {
            "reason": "Unknown failure",
            "stage": "",
            "stage_label": "",
            "hint": "",
            "actionable": False,
            "timestamp": "",
        }

    stage = entry.get("stage") if entry.get("stage") in STAGES else ""
    return {
        "reason": entry.get("reason") or "Unknown failure",
        "detail": entry.get("detail", ""),
        "stage": stage,
        "stage_label": entry.get("stage_label") or (LABELS[stage] if stage else ""),
        "hint": entry.get("hint") or (HINTS[stage] if stage else ""),
        "actionable": bool(entry.get("actionable", stage in ACTIONABLE)),
        "timestamp": entry.get("timestamp", ""),
    }
