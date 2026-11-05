#!/usr/bin/env python3
"""
MotoGP Grabber - Automated Session Downloader
===============================================

Searches for MotoGP, Moto2, and Moto3 sessions via Prowlarr and sends them to SABnzbd.

Commands:
    backfill  - Find and grab missing sessions for a season
    scheduled - Check for recently aired sessions (used by scheduler)
    status    - Show status and upcoming sessions
    clear     - Clear grabber state

Usage:
    python3 motogp_grabber.py backfill --season 2025 --dry-run
    python3 motogp_grabber.py backfill --season 2025
    python3 motogp_grabber.py scheduled
    python3 motogp_grabber.py status
"""

import os
import sys
import json
import re
import argparse
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Import from common module - including centralized logging
from motogp_common import (
    setup_logging, get_logger,
    SessionType, RaceClass,
    build_prefix, parse_prefix, get_session_from_element,
    load_calendar, get_round_by_number, has_session_times,
    identify_round, classify_session, detect_race_class,
    filter_releases, parse_release_title,
    DEFAULT_DESIRED_SESSIONS, CORE_SESSIONS,
    get_desired_sessions,
    SESSION_TO_CALENDAR_KEY, CALENDAR_KEY_TO_SESSION, CALENDAR_KEY_TO_DAY,
    get_config_dir, get_library_path,
    is_round_active, round_runs_session, get_round_status,
)
from activity_emitter import emit_activity, ActivityStage, mark_error

# Import config
from utils.config_loader import (
    get_prowlarr_url, get_prowlarr_api_key,
    get_sabnzbd_url, get_sabnzbd_api_key,
    get_motogp_library_path,
    get_allowed_groups, get_required_quality,
    get_max_grabs_per_run, get_search_delay_hours,
    get_current_season,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

SABNZBD_CATEGORY = "motogp"
CONFIG_DIR = get_config_dir()
STATE_FILE = CONFIG_DIR / "grabber_state.json"

# Sessions that have calendar times (can be scheduled)
SCHEDULABLE_SESSIONS = set(SESSION_TO_CALENDAR_KEY.keys())

# =============================================================================
# LOGGING SETUP
# =============================================================================

# Module logger - will be configured by init_logging()
logger = get_logger("grabber")


def init_logging(verbose: bool = False):
    """Initialize logging for grabber script."""
    setup_logging(debug_mode=verbose)
    logger.info("MotoGP Grabber initialized")


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

class GrabberState:
    """Manages persistent state for the grabber."""

    def __init__(self, state_file: Path = STATE_FILE):
        self.state_file = state_file
        self.state = self._load()

    def _load(self) -> dict:
        """Load state from file."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load state file: {e}")

        return {
            "searched": {},
            "grabbed": {},
            "failed": {},
            "last_run": None,
            "last_scheduled_check": None,
        }

    def save(self):
        """Save state to file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state["last_run"] = datetime.now().isoformat()

        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)

    @staticmethod
    def make_session_key(season: int, round_num: int, session_type: SessionType) -> str:
        """Create unique key for a session."""
        return f"{season}_R{round_num:02d}_E{int(session_type):02d}"

    def was_searched(self, session_key: str, max_age_days: int = 7) -> bool:
        """Check if session was recently searched."""
        if session_key not in self.state["searched"]:
            return False

        searched_time = datetime.fromisoformat(self.state["searched"][session_key])
        age = datetime.now() - searched_time
        return age.days < max_age_days

    def was_grabbed(self, session_key: str) -> bool:
        """Check if session was already grabbed."""
        return session_key in self.state["grabbed"]

    def mark_searched(self, session_key: str):
        """Mark session as searched."""
        self.state["searched"][session_key] = datetime.now().isoformat()

    def mark_grabbed(self, session_key: str, release_title: str, nzo_id: Optional[str] = None):
        """Mark session as grabbed."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "release": release_title,
        }
        if nzo_id:
            entry["nzo_id"] = nzo_id
        self.state["grabbed"][session_key] = entry

    def mark_failed(self, session_key: str, reason: str):
        """Mark session search as failed."""
        self.state["failed"][session_key] = {
            "timestamp": datetime.now().isoformat(),
            "reason": reason
        }

    def clear_session(self, session_key: str):
        """Clear all state for a session."""
        self.state["searched"].pop(session_key, None)
        self.state["grabbed"].pop(session_key, None)
        self.state["failed"].pop(session_key, None)

    def update_scheduled_check(self):
        """Update last scheduled check timestamp."""
        self.state["last_scheduled_check"] = datetime.now().isoformat()


# =============================================================================
# CALENDAR UTILITIES
# =============================================================================

def get_session_times(round_info: dict, session_type: SessionType) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Get GMT (start, end) for a session from the calendar.

    Both come from the same source so callers such as "upcoming sessions" do not
    have to guess the start time. Returns (None, None) when the round does not
    run the session or has no time for it.
    """
    calendar_key = SESSION_TO_CALENDAR_KEY.get(session_type)
    if not calendar_key:
        return (None, None)

    day = CALENDAR_KEY_TO_DAY.get(calendar_key)
    if not day:
        return (None, None)

    sessions = round_info.get("sessions", {})
    day_sessions = sessions.get(day, {})
    session_info = day_sessions.get(calendar_key)

    if not session_info:
        return (None, None)

    gmt_time = session_info.get("gmt")
    if not gmt_time:
        return (None, None)

    weekend_start = round_info.get("weekend_start")
    race_date = round_info.get("date")

    if not weekend_start or not race_date:
        return (None, None)

    try:
        weekend_start_dt = datetime.strptime(weekend_start, "%Y-%m-%d")
        race_date_dt = datetime.strptime(race_date, "%Y-%m-%d")

        if day == "friday":
            session_date = weekend_start_dt
        elif day == "saturday":
            session_date = weekend_start_dt + timedelta(days=1)
        elif day == "sunday":
            session_date = race_date_dt
        else:
            return (None, None)

        gmt_day = session_info.get("gmt_day")
        if gmt_day:
            day_offsets = {
                "thursday": -1 if day == "friday" else 0,
                "friday": -1 if day == "saturday" else 0,
                "saturday": -1 if day == "sunday" else 0,
            }
            session_date += timedelta(days=day_offsets.get(gmt_day, 0))

        hour, minute = map(int, gmt_time.split(":"))
        start_dt = session_date.replace(hour=hour, minute=minute)

        duration = session_info.get("duration_mins", 45)
        return (start_dt, start_dt + timedelta(minutes=duration))

    except Exception as e:
        logger.debug(f"Error parsing session datetime: {e}")
        return (None, None)


def get_session_end_time(round_info: dict, session_type: SessionType) -> Optional[datetime]:
    """Get GMT end time for a session from calendar."""
    return get_session_times(round_info, session_type)[1]


def get_session_start_time(round_info: dict, session_type: SessionType) -> Optional[datetime]:
    """Get GMT start time for a session from calendar."""
    return get_session_times(round_info, session_type)[0]


def get_sessions_ready_for_search(calendar: dict, search_delay_hours: int = None) -> List[dict]:
    """
    Find sessions that have finished and are ready for search.
    Only considers the current race weekend and schedulable sessions.
    """
    ready = []
    now = datetime.now()
    today = now.date()

    # Use config value if not overridden
    if search_delay_hours is None:
        search_delay_hours = get_search_delay_hours()

    logger.debug(f"Checking for ready sessions. Current time: {now}, Today: {today}")
    logger.debug(f"Schedulable sessions: {[s.full_display_name for s in SCHEDULABLE_SESSIONS]}")

    rounds_checked = 0
    rounds_in_window = 0

    for round_info in calendar.get("rounds", []):
        round_num = round_info.get("round", 0)
        venue = round_info.get("venue", "Unknown")
        rounds_checked += 1

        # A cancelled/postponed round must never be searched for, even if its
        # old dates still fall inside the active window.
        if not is_round_active(round_info):
            logger.info(
                f"Round {round_num} {venue}: status is "
                f"'{get_round_status(round_info)}', skipping"
            )
            continue

        # Only consider current race weekend
        race_date_str = round_info.get("date")
        weekend_start_str = round_info.get("weekend_start")

        if not race_date_str:
            logger.debug(f"Round {round_num} {venue}: No race date, skipping")
            continue

        try:
            race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            if weekend_start_str:
                weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            else:
                weekend_start = race_date - timedelta(days=2)

            # Only process rounds within the active window
            # From 1 day before weekend start to 2 days after race
            window_start = weekend_start - timedelta(days=1)
            window_end = race_date + timedelta(days=2)

            if not (window_start <= today <= window_end):
                logger.debug(
                    f"Round {round_num} {venue}: Outside window "
                    f"({window_start} to {window_end}), skipping"
                )
                continue

            rounds_in_window += 1
            logger.info(
                f"📅 Round {round_num} {venue} is within active window "
                f"({window_start} to {window_end})"
            )

        except ValueError as e:
            logger.debug(f"Round {round_num}: Date parse error: {e}")
            continue

        # Use config delay (not calendar override)
        delay = search_delay_hours
        logger.debug(f"Round {round_num} {venue}: Using search delay of {delay} hours")

        # Only iterate over sessions that have calendar times
        desired = get_desired_sessions()

        for session_type in SCHEDULABLE_SESSIONS:
            if not desired.get(session_type, False):
                logger.debug(f"  {session_type.full_display_name}: Not in desired sessions, skipping")
                continue

            # The weekend format decides which sessions exist (e.g. no MotoGP
            # warm-up from 2027, Sprint Qualifying added).
            if not round_runs_session(round_info, session_type, calendar):
                logger.debug(
                    f"  {session_type.full_display_name}: Not run at this round, skipping"
                )
                continue

            end_time = get_session_end_time(round_info, session_type)

            if not end_time:
                logger.debug(f"  {session_type.full_display_name}: Could not determine end time, skipping")
                continue

            search_after = end_time + timedelta(hours=delay)

            logger.debug(
                f"  {session_type.full_display_name}: Ended {end_time}, "
                f"search after {search_after}, now {now}"
            )

            if now >= search_after:
                logger.info(f"  ✅ {session_type.full_display_name}: READY for search (ended {end_time})")
                ready.append({
                    "round": round_num,
                    "round_info": round_info,
                    "session_type": session_type,
                    "ended_at": end_time,
                    "search_after": search_after,
                })
            else:
                time_until = search_after - now
                logger.debug(
                    f"  {session_type.full_display_name}: Not ready yet, "
                    f"{time_until} until search window"
                )

    logger.info(
        f"📊 Checked {rounds_checked} rounds, {rounds_in_window} in active window, "
        f"{len(ready)} sessions ready"
    )

    return ready


def get_upcoming_sessions(calendar: dict, hours_ahead: int = 48) -> List[dict]:
    """Find sessions happening in the next X hours."""
    upcoming = []
    now = datetime.now()
    cutoff = now + timedelta(hours=hours_ahead)

    # Only iterate over sessions that have calendar times
    desired = get_desired_sessions()

    for round_info in calendar.get("rounds", []):
        round_num = round_info.get("round", 0)
        venue = round_info.get("venue", "Unknown")

        if not is_round_active(round_info):
            continue

        for session_type in SCHEDULABLE_SESSIONS:
            if not desired.get(session_type, False):
                continue

            if not round_runs_session(round_info, session_type, calendar):
                continue

            start_time, end_time = get_session_times(round_info, session_type)
            if not start_time or not end_time:
                continue

            if now < start_time <= cutoff:
                upcoming.append({
                    "round": round_num,
                    "venue": venue,
                    "session_type": session_type,
                    "starts_at": start_time,
                    "ends_at": end_time,
                })

    upcoming.sort(key=lambda x: x["starts_at"])
    return upcoming


# =============================================================================
# LIBRARY SCANNING
# =============================================================================

def scan_library_for_season(season: int) -> Dict[int, Set[SessionType]]:
    """Scan library to find existing sessions for a season."""
    season_path = os.path.join(get_motogp_library_path(), str(season))

    if not os.path.exists(season_path):
        logger.warning(f"Season folder not found: {season_path}")
        return {}

    existing: Dict[int, Set[SessionType]] = {}

    logger.debug(f"Scanning library: {season_path}")

    for round_folder in os.listdir(season_path):
        round_path = os.path.join(season_path, round_folder)

        if not os.path.isdir(round_path):
            continue

        round_match = re.match(r'Round\s*(\d+)', round_folder)
        if not round_match:
            continue

        round_num = int(round_match.group(1))
        existing[round_num] = set()

        for session_folder in os.listdir(round_path):
            session_path = os.path.join(round_path, session_folder)

            if not os.path.isdir(session_path):
                continue

            parsed = parse_prefix(session_folder)
            if parsed:
                folder_year, folder_round, session_type = parsed

                if folder_year == season and folder_round == round_num:
                    existing[round_num].add(session_type)
                    logger.debug(f"Library: Round {round_num} has {session_type.full_display_name}")

    total_sessions = sum(len(s) for s in existing.values())
    logger.info(f"📚 Library scan: Found {total_sessions} sessions across {len(existing)} rounds")

    return existing


def identify_missing_sessions(
    season: int,
    calendar: dict,
    existing: Dict[int, Set[SessionType]],
    round_filter: Optional[int] = None,
    only_past: bool = True
) -> List[dict]:
    """Identify missing desired sessions."""
    missing = []
    today = datetime.now().date()

    for round_entry in calendar.get("rounds", []):
        round_num = round_entry.get("round", 0)

        if round_filter and round_num != round_filter:
            continue

        # Never hunt for sessions belonging to a cancelled/postponed round.
        if not is_round_active(round_entry):
            logger.info(
                f"Round {round_num} {round_entry.get('venue', '?')}: status is "
                f"'{get_round_status(round_entry)}', not looking for sessions"
            )
            continue

        if only_past:
            round_date_str = round_entry.get("date", "")
            if round_date_str:
                try:
                    round_date = datetime.strptime(round_date_str, "%Y-%m-%d").date()
                    if round_date > today:
                        continue
                except ValueError:
                    pass

        existing_for_round = existing.get(round_num, set())

        for session_type, wanted in get_desired_sessions().items():
            if not wanted:
                continue

            # Skip sessions this weekend format does not run.
            if not round_runs_session(round_entry, session_type, calendar):
                continue

            if session_type not in existing_for_round:
                missing.append({
                    "round": round_num,
                    "round_info": round_entry,
                    "session_type": session_type,
                })

    return missing


# =============================================================================
# PROWLARR SEARCH
# =============================================================================

def get_indexers() -> List[dict]:
    """Get list of enabled Prowlarr indexers."""
    prowlarr_url = get_prowlarr_url()
    prowlarr_api_key = get_prowlarr_api_key()

    if not prowlarr_url or not prowlarr_api_key:
        logger.error("Prowlarr not configured")
        return []

    try:
        url = f"{prowlarr_url}/api/v1/indexer"
        headers = {"X-Api-Key": prowlarr_api_key}

        response = requests.get(url, headers=headers, timeout=10)

        if response.status_code == 200:
            indexers = response.json()
            return [i for i in indexers if i.get('enable', False)]
        return []
    except Exception as e:
        logger.error(f"Error getting indexers: {e}")
        return []


def parse_newznab_xml(xml_text: str, indexer_name: str = "Unknown") -> List[dict]:
    """Parse Newznab XML response."""
    releases = []

    try:
        if isinstance(xml_text, bytes):
            xml_text = xml_text.decode('utf-8', errors='replace')

        root = ET.fromstring(xml_text)

        for item in root.findall('.//item'):
            release = {'indexer': indexer_name}

            title_elem = item.find('title')
            if title_elem is not None and title_elem.text:
                release['title'] = title_elem.text.strip()

            guid_elem = item.find('guid')
            if guid_elem is not None and guid_elem.text:
                release['guid'] = guid_elem.text.strip()

            link_elem = item.find('link')
            if link_elem is not None and link_elem.text:
                release['link'] = link_elem.text.strip()

            enclosure = item.find('enclosure')
            if enclosure is not None:
                try:
                    release['size'] = int(enclosure.get('length', 0))
                except ValueError:
                    release['size'] = 0
                release['downloadUrl'] = enclosure.get('url', '')

            if release.get('title'):
                releases.append(release)

    except ET.ParseError as e:
        logger.debug(f"XML parse error: {e}")
    except Exception as e:
        logger.debug(f"Error parsing XML: {e}")

    return releases


def search_indexer(query: str, indexer_id: int, indexer_name: str) -> List[dict]:
    """Search a specific indexer via Prowlarr."""
    prowlarr_url = get_prowlarr_url()
    prowlarr_api_key = get_prowlarr_api_key()

    try:
        url = f"{prowlarr_url}/{indexer_id}/api"
        params = {
            "t": "search",
            "q": query,
            "apikey": prowlarr_api_key,
            "limit": 100,
        }

        response = requests.get(url, params=params, timeout=60)

        if response.status_code == 200:
            return parse_newznab_xml(response.text, indexer_name)
        return []
    except Exception as e:
        logger.debug(f"Error searching {indexer_name}: {e}")
        return []


def build_search_queries(round_info: dict, session_type: SessionType, season: int) -> List[str]:
    """Build search queries for a session."""
    queries = []

    venue = round_info.get("venue", "")
    aliases = round_info.get("aliases", [])
    round_num = round_info.get("round", 0)

    race_class = session_type.race_class
    class_search = race_class.display_name if race_class else "MotoGP"

    # Venue-based queries
    queries.append(f"{class_search} {venue} {season}")
    queries.append(f"{class_search} {season} {venue}")

    # Alias-based queries
    for alias in aliases[:3]:
        if len(alias) > 2 and alias.lower() not in ['uk', 'gp', 'tt']:
            queries.append(f"{class_search} {season} {alias}")

    # Round-based queries. The calendar may carry alternative round numbers
    # for the same event (see "round_aliases"), which happens when the
    # championship is renumbered mid-season after a postponement.
    for alias in [round_num] + list(round_info.get("round_aliases") or []):
        if not alias:
            continue
        queries.append(f"{class_search} {season} Round{alias:02d}")
        queries.append(f"{class_search} {season} Round{alias}")
        queries.append(f"{class_search} {season} Round {alias}")

    # Remove duplicates
    seen = set()
    unique_queries = []
    for q in queries:
        q_lower = q.lower()
        if q_lower not in seen:
            seen.add(q_lower)
            unique_queries.append(q)

    return unique_queries


def _build_episode_key(season: int, round_num: int, session_type: SessionType) -> str:
    """Build episode key for activity tracking."""
    return f"{season}_R{round_num:02d}_E{int(session_type):02d}"


def search_for_session(
    round_info: dict,
    session_type: SessionType,
    season: int,
    indexers: List[dict],
    calendar: dict = None
) -> List[dict]:
    """Search all indexers for a session."""
    queries = build_search_queries(round_info, session_type, season)

    all_results = []
    seen_guids = set()

    round_num = round_info.get("round", "?")
    venue = round_info.get("venue", "Unknown")

    logger.info(f"🔍 Searching: Round {round_num} {venue} - {session_type.full_display_name}")

    for indexer in indexers:
        idx_id = indexer.get('id')
        idx_name = indexer.get('name', 'Unknown')

        for query in queries[:5]:
            results = search_indexer(query, idx_id, idx_name)

            for release in results:
                guid = release.get('guid', '')
                if guid and guid not in seen_guids:
                    seen_guids.add(guid)
                    all_results.append(release)

    logger.info(
        f"📋 Found {len(all_results)} candidates for "
        f"Round {round_num} {venue} - {session_type.full_display_name}"
    )

    # Filter using common module function
    filtered = filter_releases(
        all_results,
        session_type,
        season,
        round_info,
        get_required_quality(),
        get_allowed_groups(),
        calendar
    )

    logger.info(f"🎯 After filtering: {len(filtered)} matching releases")

    return filtered


# =============================================================================
# SABNZBD INTEGRATION
# =============================================================================

def test_sabnzbd_connection() -> bool:
    """Test SABnzbd connection."""
    sabnzbd_url = get_sabnzbd_url()
    sabnzbd_api_key = get_sabnzbd_api_key()

    if not sabnzbd_url or not sabnzbd_api_key:
        logger.error("SABnzbd not configured")
        return False

    try:
        params = {
            "mode": "version",
            "apikey": sabnzbd_api_key,
            "output": "json"
        }

        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=10)

        if response.status_code == 200:
            result = response.json()
            version = result.get('version', 'Unknown')
            logger.info(f"SABnzbd connected: v{version}")
            return True
        return False
    except Exception as e:
        logger.error(f"SABnzbd connection failed: {e}")
        return False


def grab_release(release: dict, session_info: dict, season: int) -> Optional[str]:
    """Send release to SABnzbd with enriched name.

    Returns the nzo_id string on success (empty string if SABnzbd did not
    provide one), or None on failure. Callers should check ``result is not
    None`` to determine success.
    """
    sabnzbd_url = get_sabnzbd_url()
    sabnzbd_api_key = get_sabnzbd_api_key()

    download_url = release.get('downloadUrl') or release.get('link')

    if not download_url:
        logger.error("No download URL available")
        return None

    # Build enriched name with prefix
    round_num = session_info.get("round", 0)
    round_info = session_info.get("round_info", {})
    session_type = session_info.get("session_type")
    venue = round_info.get("venue", "Unknown")

    prefix = build_prefix(season, round_num, session_type)
    enriched_name = f"{prefix} {venue} {session_type.full_display_name}"

    try:
        params = {
            "mode": "addurl",
            "name": download_url,
            "nzbname": enriched_name,
            "cat": SABNZBD_CATEGORY,
            "apikey": sabnzbd_api_key,
            "output": "json"
        }

        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=30)

        if response.status_code == 200:
            try:
                result = response.json()
            except Exception as parse_err:
                logger.error(f"SABnzbd response parse error: {parse_err}")
                return None

            if result.get('status', False):
                # Extract nzo_id from response (SABnzbd returns a list)
                nzo_ids = result.get('nzo_ids') or []
                nzo_id = nzo_ids[0] if isinstance(nzo_ids, list) and nzo_ids else None

                release_title = release.get('title', 'Unknown')
                release_score = release.get('_score', 0)
                logger.info(f"✅ Grabbed: {enriched_name}")
                logger.info(f"   📦 Release: {release_title[:70]}")
                logger.info(f"   📊 Score: {release_score}")
                if nzo_id:
                    logger.info(f"   🆔 nzo_id: {nzo_id}")
                return nzo_id if nzo_id else ""
            else:
                logger.error(f"SABnzbd error: {result.get('error', 'Unknown')}")
        return None
    except Exception as e:
        logger.error(f"Grab error: {e}")
        return None


def get_sabnzbd_job_status(nzo_id: str) -> str:
    """Check the status of a SABnzbd job by nzo_id.

    Returns one of: 'inprogress', 'completed', 'failed', 'unknown'.
    Checks the queue first, then history.
    """
    sabnzbd_url = get_sabnzbd_url()
    sabnzbd_api_key = get_sabnzbd_api_key()

    if not sabnzbd_url or not sabnzbd_api_key or not nzo_id:
        return "unknown"

    # Check active queue
    try:
        params = {
            "mode": "queue",
            "nzo_ids": nzo_id,
            "apikey": sabnzbd_api_key,
            "output": "json",
        }
        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=10)
        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                data = {}
            queue = data.get("queue") or {}
            slots = queue.get("slots") or []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                if slot.get("nzo_id") == nzo_id:
                    status = (slot.get("status") or "").lower()
                    if status in ("downloading", "queued", "grabbing", "verifying",
                                  "repairing", "extracting", "moving", "running"):
                        return "inprogress"
                    if status == "failed":
                        return "failed"
                    return "inprogress"
    except Exception as e:
        logger.debug(f"SABnzbd queue check error for {nzo_id}: {e}")

    # Check history
    try:
        params = {
            "mode": "history",
            "nzo_ids": nzo_id,
            "apikey": sabnzbd_api_key,
            "output": "json",
        }
        response = requests.get(f"{sabnzbd_url}/api", params=params, timeout=10)
        if response.status_code == 200:
            try:
                data = response.json()
            except Exception:
                data = {}
            history = data.get("history") or {}
            slots = history.get("slots") or []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                if slot.get("nzo_id") == nzo_id:
                    status = (slot.get("status") or "").lower()
                    if status == "completed":
                        return "completed"
                    if status in ("failed", "bad"):
                        return "failed"
                    return "unknown"
    except Exception as e:
        logger.debug(f"SABnzbd history check error for {nzo_id}: {e}")

    return "unknown"


def reconcile_grabbed_jobs(state: "GrabberState") -> int:
    """Check SABnzbd status for all grabbed jobs that have an nzo_id.

    If SABnzbd reports a job as failed, move the session to failed state and
    clear the grabbed entry so the session can be retried on the next run.

    Returns the number of sessions that were reset to allow retry.
    """
    grabbed = state.state.get("grabbed", {})
    retried = 0

    for session_key, entry in list(grabbed.items()):
        if not isinstance(entry, dict):
            continue
        nzo_id = entry.get("nzo_id")
        if not nzo_id:
            continue

        job_status = get_sabnzbd_job_status(nzo_id)
        logger.debug(f"SABnzbd job {nzo_id} ({session_key}): {job_status}")

        if job_status == "failed":
            release = entry.get("release", "Unknown")
            logger.warning(
                f"SABnzbd job failed for {session_key} ({release}), "
                f"clearing for retry"
            )
            state.state["failed"][session_key] = {
                "timestamp": datetime.now().isoformat(),
                "reason": f"SABnzbd reported failure (nzo_id={nzo_id})",
            }
            del state.state["grabbed"][session_key]
            retried += 1

    return retried


# =============================================================================
# DISPLAY HELPERS
# =============================================================================

def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable."""
    if not size_bytes:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def format_time_until(dt: datetime) -> str:
    """Format time remaining until datetime."""
    delta = dt - datetime.now()
    if delta.total_seconds() < 0:
        return "now"

    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    if hours > 24:
        return f"{hours // 24}d {hours % 24}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_time_ago(dt: datetime) -> str:
    """Format time since datetime."""
    delta = datetime.now() - dt
    if delta.total_seconds() < 0:
        return "not yet"

    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    if hours > 24:
        return f"{hours // 24}d {hours % 24}h ago"
    elif hours > 0:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"


def print_missing_summary(missing: List[dict]):
    """Print summary of missing sessions."""
    print("\n" + "=" * 70)
    print("MISSING SESSIONS")
    print("=" * 70)

    if not missing:
        print("\n  ✅ No missing sessions found!")
        return

    by_round = {}
    for item in missing:
        round_num = item['round']
        if round_num not in by_round:
            by_round[round_num] = {
                'venue': item['round_info'].get('venue', 'Unknown'),
                'sessions': []
            }
        by_round[round_num]['sessions'].append(item['session_type'].full_display_name)

    for round_num in sorted(by_round.keys()):
        info = by_round[round_num]
        print(f"\n  Round {round_num} - {info['venue']}:")
        for session in sorted(info['sessions']):
            print(f"    ❌ {session}")

    print(f"\n  Total missing: {len(missing)} sessions")
    print("=" * 70)


def print_search_results(results: List[dict], limit: int = 5):
    """Print search results."""
    if not results:
        print("    No matching results found")
        return

    for i, r in enumerate(results[:limit], 1):
        title = r.get('title', 'Unknown')
        size = format_size(r.get('size', 0))
        indexer = r.get('indexer', 'Unknown')
        score = r.get('_score', 0)
        broadcaster = r.get('_broadcaster', '')
        group = r.get('_group', '')

        print(f"    [{i}] {title[:60]}{'...' if len(title) > 60 else ''}")

        info_parts = [indexer, size, f"Score: {score}"]
        if broadcaster:
            info_parts.append(f"📺 {broadcaster.upper()}")
        if group:
            info_parts.append(f"🏷️ {group}")

        print(f"        {' | '.join(info_parts)}")


# =============================================================================
# QUALIFYING HELPERS
# =============================================================================

def _get_other_quali_session_key(
    season: int,
    round_num: int,
    session_type: SessionType
) -> Optional[str]:
    """Get the session key for the other qualifying session (Q1↔Q2)."""
    race_class = session_type.race_class
    session_name = session_type.display_name

    if session_name == "Qualifying 1":
        q2_map = {
            RaceClass.MOTO3: SessionType.MOTO3_Q2,
            RaceClass.MOTO2: SessionType.MOTO2_Q2,
            RaceClass.MOTOGP: SessionType.MOTOGP_Q2,
        }
        other_session = q2_map.get(race_class)
    elif session_name == "Qualifying 2":
        q1_map = {
            RaceClass.MOTO3: SessionType.MOTO3_Q1,
            RaceClass.MOTO2: SessionType.MOTO2_Q1,
            RaceClass.MOTOGP: SessionType.MOTOGP_Q1,
        }
        other_session = q1_map.get(race_class)
    else:
        return None

    if other_session:
        return GrabberState.make_session_key(season, round_num, other_session)
    return None


# =============================================================================
# MAIN COMMANDS
# =============================================================================

def cmd_backfill(args):
    """Backfill missing sessions for a season."""
    season = args.season
    dry_run = args.dry_run
    round_filter = args.round
    force = args.force

    print(f"\n{'=' * 60}")
    print(f"  MOTOGP GRABBER - BACKFILL MODE")
    print(f"{'=' * 60}")
    print(f"\n  Season:     {season}")
    print(f"  Dry Run:    {dry_run}")
    print(f"  Max Grabs:  {get_max_grabs_per_run()}")
    if round_filter:
        print(f"  Round:      {round_filter} only")
    print(f"  Time:       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    state = GrabberState()

    # Reconcile previously grabbed jobs against SABnzbd status
    if not dry_run:
        reset_count = reconcile_grabbed_jobs(state)
        if reset_count > 0:
            print(f"\n  ♻️  Reconciled {reset_count} failed SABnzbd job(s) — will retry")
            state.save()

    calendar = load_calendar(season)
    if not calendar:
        print(f"\n❌ Could not load calendar for {season}")
        return 1

    print(f"\n  Calendar:   {len(calendar.get('rounds', []))} rounds loaded")

    # Connection tests
    print(f"\n{'─' * 40}")
    print("CONNECTION TESTS")
    print(f"{'─' * 40}")

    try:
        indexers = get_indexers()
        if indexers:
            print(f"  ✅ Prowlarr: {len(indexers)} indexers")
        else:
            print("  ❌ Prowlarr: No indexers found")
            return 1
    except Exception as e:
        print(f"  ❌ Prowlarr: {e}")
        return 1

    if not dry_run:
        if not test_sabnzbd_connection():
            print("  ❌ SABnzbd: Connection failed")
            return 1
    else:
        print("  ⏭️  SABnzbd: Skipped (dry run)")

    # Scan library
    print(f"\n{'─' * 40}")
    print("SCANNING LIBRARY")
    print(f"{'─' * 40}")

    existing = scan_library_for_season(season)
    total_existing = sum(len(sessions) for sessions in existing.values())
    print(f"  Found {total_existing} existing sessions across {len(existing)} rounds")

    missing = identify_missing_sessions(season, calendar, existing, round_filter)

    # Filter by specific sessions if provided
    sessions_filter = getattr(args, 'sessions', None)
    if sessions_filter:
        session_nums = []
        for s in sessions_filter.split(','):
            s = s.strip().upper()
            if s.startswith('E'):
                s = s[1:]
            try:
                session_nums.append(int(s))
            except ValueError:
                pass

        if session_nums:
            missing = [m for m in missing if int(m['session_type']) in session_nums]
            print(f"\n  Filtered to {len(missing)} sessions: {sessions_filter}")

    print_missing_summary(missing)

    if not missing:
        state.save()
        return 0

    # Filter by state with 30-minute cooldown
    if not force:
        filtered_missing = []
        for item in missing:
            session_key = state.make_session_key(
                season, item['round'], item['session_type']
            )

            if state.was_grabbed(session_key):
                continue

            # 30-minute cooldown (matching f1arr behaviour)
            if session_key in state.state.get("searched", {}):
                try:
                    searched_time = datetime.fromisoformat(
                        state.state["searched"][session_key]
                    )
                    minutes_since = (datetime.now() - searched_time).total_seconds() / 60
                    if minutes_since < 30:
                        logger.debug(
                            f"Skipping {session_key}: searched {int(minutes_since)}m ago"
                        )
                        continue
                except (ValueError, KeyError):
                    pass  # Invalid timestamp, proceed with search

            filtered_missing.append(item)

        skipped = len(missing) - len(filtered_missing)
        if skipped > 0:
            print(f"\n  ⏭️  Skipping {skipped} recently searched sessions (within 30 mins)")
            print(f"     (use --force to search again)")

        missing = filtered_missing

    if not missing:
        print("\n  ℹ️  All missing sessions have been recently searched")
        state.save()
        return 0

    # Search and grab
    print(f"\n{'─' * 40}")
    print("SEARCHING & GRABBING")
    print(f"{'─' * 40}")

    grabs_this_run = 0
    grabbed_titles = set()
    sessions_satisfied_this_run = set()
    max_grabs = get_max_grabs_per_run()

    for item in missing:
        if grabs_this_run >= max_grabs:
            print(f"\n  ⚠️  Reached max grabs limit ({max_grabs})")
            break

        round_num = item['round']
        round_info = item['round_info']
        session_type = item['session_type']
        venue = round_info.get('venue', 'Unknown')

        session_key = state.make_session_key(season, round_num, session_type)

        if session_key in sessions_satisfied_this_run:
            print(f"\n  ⏭️  Round {round_num} {venue}: {session_type.full_display_name}")
            print(f"      Already covered by combined release grabbed earlier")
            continue

        logger.info(f"🔍 Searching: Round {round_num} {venue} - {session_type.full_display_name}")
        print(f"\n  🔍 Round {round_num} {venue}: {session_type.full_display_name}")

        # Emit SEARCHING activity
        episode_key = _build_episode_key(season, round_num, session_type)
        emit_activity(
            episode_key=episode_key,
            episode_name=session_type.full_display_name,
            stage=ActivityStage.SEARCHING,
            details=f"Round {round_num} {venue}",
            round_info={"round": round_num, "venue": venue}
        )

        results = search_for_session(round_info, session_type, season, indexers, calendar)

        state.mark_searched(session_key)

        if not results:
            print("    ❌ No matching releases found")
            logger.info(
                f"❌ No candidates: Round {round_num} {venue} - "
                f"{session_type.full_display_name}"
            )
            state.mark_failed(session_key, "No results")
            mark_error(
                episode_key=episode_key,
                error_message="No matching releases found"
            )
            continue

        available = [r for r in results if r.get('title', '').lower() not in grabbed_titles]

        if not available:
            print(f"    ⚠️  All results already grabbed this run")
            state.mark_failed(session_key, "All results already grabbed")
            continue

        logger.info(
            f"📋 Found {len(available)} candidates for "
            f"Round {round_num} {venue} - {session_type.full_display_name}"
        )
        print(f"    Found {len(available)} matching releases:")
        print_search_results(available, limit=5)

        # Emit CANDIDATES_FOUND activity
        emit_activity(
            episode_key=episode_key,
            episode_name=session_type.full_display_name,
            stage=ActivityStage.CANDIDATES_FOUND,
            details=f"Found {len(available)} candidates",
            round_info={"round": round_num, "venue": venue}
        )

        best = available[0]
        best_title = best.get('title', 'Unknown')
        is_combined = best.get('_combined_quali', False)

        if dry_run:
            print(f"\n    [DRY-RUN] Would grab: {best_title[:50]}")
            grabbed_titles.add(best_title.lower())
            grabs_this_run += 1

            if is_combined:
                sessions_satisfied_this_run.add(session_key)
                other_key = _get_other_quali_session_key(season, round_num, session_type)
                if other_key:
                    sessions_satisfied_this_run.add(other_key)
        else:
            logger.info(f"⬇️ Sending to SABnzbd: {best_title[:60]}")
            print(f"\n    ⬇️  Sending to SABnzbd...")

            # Emit DOWNLOADING activity
            emit_activity(
                episode_key=episode_key,
                episode_name=session_type.full_display_name,
                stage=ActivityStage.DOWNLOADING,
                details=f"Sending to SABnzbd",
                round_info={"round": round_num, "venue": venue},
                release_name=best_title[:80]
            )

            nzo_id = grab_release(best, item, season)
            if nzo_id is not None:
                print(f"    ✅ Grab successful!")
                state.mark_grabbed(session_key, best_title, nzo_id=nzo_id or None)
                grabbed_titles.add(best_title.lower())
                grabs_this_run += 1

                if is_combined:
                    sessions_satisfied_this_run.add(session_key)
                    other_key = _get_other_quali_session_key(season, round_num, session_type)
                    if other_key:
                        sessions_satisfied_this_run.add(other_key)
                        state.mark_grabbed(other_key, best_title + " (combined)")
            else:
                print(f"    ❌ Grab failed!")
                state.mark_failed(session_key, "Grab failed")
                mark_error(
                    episode_key=episode_key,
                    error_message="SABnzbd grab failed"
                )

    state.save()

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Sessions searched:  {len(missing)}")
    print(f"  Releases grabbed:   {grabs_this_run}")
    print(f"{'=' * 60}\n")

    return 0


def cmd_scheduled(args):
    """Run scheduled check for current season."""
    season = args.season or get_current_season()
    dry_run = args.dry_run
    force = args.force
    search_delay = getattr(args, 'search_delay', None)

    logger.info(f"Starting scheduled check for season {season}")
    if search_delay:
        logger.info(f"Using search delay override: {search_delay} hours")

    print(f"\n{'=' * 60}")
    print(f"  MOTOGP GRABBER - SCHEDULED MODE")
    print(f"{'=' * 60}")
    print(f"\n  Season:       {season}")
    print(f"  Dry Run:      {dry_run}")
    print(f"  Search Delay: {search_delay or get_search_delay_hours()} hours")
    print(f"  Time (Local): {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    state = GrabberState()

    # Reconcile previously grabbed jobs against SABnzbd status
    if not dry_run:
        reset_count = reconcile_grabbed_jobs(state)
        if reset_count > 0:
            print(f"\n  ♻️  Reconciled {reset_count} failed SABnzbd job(s) — will retry")
            state.save()

    calendar = load_calendar(season)
    if not calendar:
        print(f"\n❌ Could not load calendar for {season}")
        logger.error(f"Could not load calendar for {season}")
        return 1

    if not has_session_times(calendar):
        print(f"\n❌ Calendar for {season} doesn't have session times")
        print("   Use 'backfill' mode for older seasons")
        logger.error(f"Calendar for {season} doesn't have session times")
        return 1

    print(f"\n  Calendar: {len(calendar.get('rounds', []))} rounds with session times")
    logger.info(f"Loaded calendar with {len(calendar.get('rounds', []))} rounds")

    # Get sessions ready for search
    logger.info("Checking for sessions ready to search...")
    ready = get_sessions_ready_for_search(calendar, search_delay_hours=search_delay)

    logger.info(f"get_sessions_ready_for_search returned {len(ready)} sessions")
    for r in ready:
        logger.info(
            f"  Ready: Round {r['round']} "
            f"{r['round_info'].get('venue', '?')} - "
            f"{r['session_type'].full_display_name}"
        )

    # Scan library for existing sessions
    logger.info("Scanning library for existing sessions...")
    existing = scan_library_for_season(season)

    # Filter out sessions already in library, already grabbed, or within rate limit
    filtered_ready = []
    for item in ready:
        round_num = item['round']
        session_type = item['session_type']
        venue = item['round_info'].get('venue', 'Unknown')

        # Skip if already in library
        if round_num in existing and session_type in existing[round_num]:
            logger.info(
                f"Skipping Round {round_num} {venue} "
                f"{session_type.full_display_name}: already in library"
            )
            continue

        session_key = state.make_session_key(season, round_num, session_type)

        # Skip if already grabbed (sent to downloader)
        if state.was_grabbed(session_key):
            logger.info(f"Skipping {session_key}: already grabbed (in state)")
            continue

        # Rate limit: don't search same session more than once per 30 minutes
        if not force and session_key in state.state.get("searched", {}):
            try:
                searched_time = datetime.fromisoformat(
                    state.state["searched"][session_key]
                )
                minutes_since = (datetime.now() - searched_time).total_seconds() / 60
                if minutes_since < 30:
                    logger.debug(
                        f"Skipping {session_key}: searched "
                        f"{int(minutes_since)}m ago (rate limit)"
                    )
                    continue
            except (ValueError, KeyError):
                pass  # Invalid timestamp, proceed with search

        logger.info(
            f"Will search for: Round {round_num} {venue} - "
            f"{session_type.full_display_name}"
        )
        filtered_ready.append(item)

    if not filtered_ready:
        print("\n  ℹ️  No new sessions to search for")
        logger.info("No new sessions to search for")
        state.update_scheduled_check()
        state.save()
        return 0

    print(f"\n  Ready sessions: {len(filtered_ready)}")

    # Connection tests
    print(f"\n{'─' * 40}")
    print("CONNECTION TESTS")
    print(f"{'─' * 40}")

    try:
        indexers = get_indexers()
        if indexers:
            print(f"  ✅ Prowlarr: {len(indexers)} indexers")
            logger.info(f"Prowlarr connected: {len(indexers)} indexers")
        else:
            print("  ❌ Prowlarr: No indexers found")
            logger.error("Prowlarr: No indexers found")
            return 1
    except Exception as e:
        print(f"  ❌ Prowlarr: {e}")
        logger.error(f"Prowlarr connection failed: {e}")
        return 1

    if not dry_run:
        if not test_sabnzbd_connection():
            print("  ❌ SABnzbd: Connection failed")
            return 1
    else:
        print("  ⏭️  SABnzbd: Skipped (dry run)")

    # Search and grab
    print(f"\n{'─' * 40}")
    print(f"SEARCHING & GRABBING (Season {season})")
    print(f"{'─' * 40}")

    grabs_this_run = 0
    max_grabs = get_max_grabs_per_run()

    for item in filtered_ready:
        if grabs_this_run >= max_grabs:
            print(f"\n  ⚠️  Reached max grabs limit ({max_grabs})")
            break

        round_num = item['round']
        round_info = item['round_info']
        session_type = item['session_type']
        venue = round_info.get('venue', 'Unknown')

        session_key = state.make_session_key(season, round_num, session_type)

        ended_ago = format_time_ago(item['ended_at'])
        print(f"\n  🔍 Round {round_num} {venue}: {session_type.full_display_name}")
        print(f"      Session ended: {ended_ago}")

        logger.info(
            f"Searching for Round {round_num} {venue} - "
            f"{session_type.full_display_name}"
        )

        results = search_for_session(round_info, session_type, season, indexers, calendar)

        state.mark_searched(session_key)

        if not results:
            print("    ❌ No matching releases found yet")
            logger.info(
                f"No matching releases for Round {round_num} {venue} - "
                f"{session_type.full_display_name}"
            )
            state.mark_failed(session_key, "No results")
            continue

        print(f"    Found {len(results)} matching releases:")
        print_search_results(results, limit=3)

        best = results[0]

        if dry_run:
            prefix = build_prefix(season, round_num, session_type)
            print(f"\n    [DRY-RUN] Would grab as: {prefix} {venue} {session_type.full_display_name}")
            grabs_this_run += 1
        else:
            print(f"\n    ⬇️  Grabbing: {best.get('title', 'Unknown')[:50]}")

            nzo_id = grab_release(best, item, season)
            if nzo_id is not None:
                state.mark_grabbed(
                    session_key,
                    best.get('title', 'Unknown'),
                    nzo_id=nzo_id or None
                )
                grabs_this_run += 1
            else:
                state.mark_failed(session_key, "Grab failed")

    state.update_scheduled_check()
    state.save()

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Sessions ready:    {len(filtered_ready)}")
    print(f"  Releases grabbed:  {grabs_this_run}")
    print(f"{'=' * 60}\n")

    logger.info(f"Scheduled check complete: {grabs_this_run} grabbed")

    return 0


def cmd_status(args):
    """Show status and upcoming sessions."""
    season = args.season or get_current_season()

    print(f"\n{'=' * 60}")
    print(f"  MOTOGP GRABBER - STATUS")
    print(f"{'=' * 60}")

    state = GrabberState()

    print(f"\n  Last run:             {state.state.get('last_run', 'Never')}")
    print(f"  Last scheduled check: {state.state.get('last_scheduled_check', 'Never')}")
    print(f"\n  Searched sessions:    {len(state.state.get('searched', {}))}")
    print(f"  Grabbed sessions:     {len(state.state.get('grabbed', {}))}")
    print(f"  Failed sessions:      {len(state.state.get('failed', {}))}")

    calendar = load_calendar(season)
    if calendar and has_session_times(calendar):
        upcoming = get_upcoming_sessions(calendar, hours_ahead=72)
        if upcoming:
            print(f"\n  Upcoming sessions (next 72h):")
            for s in upcoming[:5]:
                starts = s['starts_at'].strftime("%a %H:%M")
                print(f"    ⏰ {starts} - {s['session_type'].full_display_name}")

    print(f"\n{'=' * 60}\n")
    return 0


def cmd_clear(args):
    """Clear grabber state."""
    state = GrabberState()

    if args.all:
        state.state = {
            "searched": {},
            "grabbed": {},
            "failed": {},
            "last_run": None,
            "last_scheduled_check": None,
        }
        print("✅ All state cleared")
    elif args.session:
        state.clear_session(args.session)
        print(f"✅ Cleared state for: {args.session}")
    elif args.searched:
        state.state["searched"] = {}
        print("✅ Cleared searched state")
    elif args.failed:
        state.state["failed"] = {}
        print("✅ Cleared failed state")

    state.save()
    return 0


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MotoGP Grabber - Automated Session Downloader",
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Backfill
    backfill_p = subparsers.add_parser('backfill', help='Find and grab missing sessions')
    backfill_p.add_argument('--season', type=int, required=True, help='Season year')
    backfill_p.add_argument('--round', type=int, help='Specific round number')
    backfill_p.add_argument('--dry-run', action='store_true', help='Preview only')
    backfill_p.add_argument('--force', action='store_true', help='Ignore search history')
    backfill_p.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    backfill_p.add_argument('--sessions', type=str, help='Comma-separated session numbers (e.g., E09,E11,E12)')

    # Scheduled
    scheduled_p = subparsers.add_parser('scheduled', help='Check for recently aired sessions')
    scheduled_p.add_argument('--season', type=int, help='Season year')
    scheduled_p.add_argument('--dry-run', action='store_true', help='Preview only')
    scheduled_p.add_argument('--force', action='store_true', help='Ignore search history')
    scheduled_p.add_argument('--verbose', '-v', action='store_true', help='Verbose output')
    scheduled_p.add_argument('--search-delay', type=int, help='Hours to wait after session ends before searching')

    # Status
    status_p = subparsers.add_parser('status', help='Show status and upcoming')
    status_p.add_argument('--season', type=int, help='Season year')

    # Clear
    clear_p = subparsers.add_parser('clear', help='Clear grabber state')
    clear_p.add_argument('--all', action='store_true', help='Clear all state')
    clear_p.add_argument('--searched', action='store_true', help='Clear searched only')
    clear_p.add_argument('--failed', action='store_true', help='Clear failed only')
    clear_p.add_argument('--session', type=str, help='Clear specific session key')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    verbose = getattr(args, 'verbose', False)
    init_logging(verbose)

    if args.command == 'backfill':
        return cmd_backfill(args)
    elif args.command == 'scheduled':
        return cmd_scheduled(args)
    elif args.command == 'status':
        return cmd_status(args)
    elif args.command == 'clear':
        return cmd_clear(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())