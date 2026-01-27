#!/usr/bin/env python3
"""
MotoGP Common Module
=====================

Single source of truth for MotoGP session definitions, pattern matching,
calendar functions, and shared utilities.

This module is imported by:
    - motogp_grabber.py
    - motogp_sort.py
    - motogp_plex_tagger.py
    - motogp_scheduler.py

Architecture follows the F1 common module pattern with adaptations for
MotoGP's three-class structure (MotoGP, Moto2, Moto3).
"""

import os
import sys
import re
import json
import argparse
import logging
from enum import IntEnum
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# =============================================================================
# PATH CONFIGURATION
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()

# Config directory - use CONFIG_DIR env var in container, otherwise local
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", str(SCRIPT_DIR / "config")))

# Import config after defining SCRIPT_DIR
try:
    from utils.config_loader import get_motogp_library_path, get_downloads_path
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False


def get_library_path() -> str:
    """Get MotoGP library path from config or default."""
    if _CONFIG_AVAILABLE:
        return get_motogp_library_path()
    # Fallback for direct execution
    if os.path.exists("/media"):
        return "/media/MotoGP"
    elif os.path.exists("/mnt/user/data/media"):
        return "/mnt/user/data/media/Motorssport/MotoGP"
    return "/media/MotoGP"


def get_config_dir() -> Path:
    """Get config directory path."""
    return CONFIG_DIR


# Legacy compatibility
MOTOGP_LIBRARY_PATH = get_library_path()
BASE_PATH = "/data" if os.path.exists("/data") else "/mnt/user/data"

# Asset directories
ASSETS_DIR = SCRIPT_DIR / "assets"

CLASS_ASSET_DIRS = {
    "motogp": {
        "fanart": ASSETS_DIR / "motogp_fanart",
        "poster": ASSETS_DIR / "motogp_posters",
    },
    "moto2": {
        "fanart": ASSETS_DIR / "moto2_fanart",
        "poster": ASSETS_DIR / "moto2_posters",
    },
    "moto3": {
        "fanart": ASSETS_DIR / "moto3_fanart",
        "poster": ASSETS_DIR / "moto3_posters",
    },
}

# =============================================================================
# RACE CLASS ENUM
# =============================================================================

class RaceClass(IntEnum):
    """
    MotoGP race classes.
    Values are used for sorting (lower = earlier in weekend order).
    """
    MOTO3 = 1
    MOTO2 = 2
    MOTOGP = 3
    
    @property
    def display_name(self) -> str:
        """Human-readable display name."""
        return RACE_CLASS_DISPLAY_NAMES.get(self, self.name)
    
    @property
    def folder_key(self) -> str:
        """Key used in folder names and calendar."""
        return self.name.lower()
    
    @classmethod
    def from_string(cls, text: str) -> Optional['RaceClass']:
        """Parse race class from text."""
        text_lower = text.lower().replace(" ", "").replace("-", "")
        if "moto3" in text_lower:
            return cls.MOTO3
        elif "moto2" in text_lower:
            return cls.MOTO2
        elif "motogp" in text_lower:
            return cls.MOTOGP
        return None

RACE_CLASS_DISPLAY_NAMES = {
    RaceClass.MOTO3: "Moto3",
    RaceClass.MOTO2: "Moto2",
    RaceClass.MOTOGP: "MotoGP",
}

# =============================================================================
# SESSION TYPE ENUM
# =============================================================================

class SessionType(IntEnum):
    """
    MotoGP session types with chronological ordering.
    
    Values represent the chronological order within a race weekend.
    The enum encodes both session type AND class for proper ordering.
    
    Naming convention: {CLASS}_{SESSION}
    """
    # Pre-event (MotoGP only)
    MOTOGP_PRESS_CONFERENCE = 1
    
    # Friday - Free Practice 1
    MOTO3_FP1 = 2
    MOTOGP_FP1 = 3
    MOTO2_FP1 = 4
    
    # Friday - Practice
    MOTO3_PRACTICE = 5
    MOTOGP_PRACTICE = 6
    MOTO2_PRACTICE = 7
    
    # Saturday - Free Practice 2
    MOTO3_FP2 = 8
    MOTOGP_FP2 = 9
    MOTO2_FP2 = 10
    
    # Saturday - Qualifying (Moto3)
    MOTO3_Q1 = 11
    MOTO3_Q2 = 12
    
    # Saturday - Qualifying (MotoGP)
    MOTOGP_Q1 = 13
    MOTOGP_Q2 = 14
    
    # Saturday - Sprint (MotoGP only)
    MOTOGP_SPRINT = 15
    
    # Saturday - Qualifying (Moto2, after Sprint)
    MOTO2_Q1 = 16
    MOTO2_Q2 = 17
    
    # Sunday - Warm Up (MotoGP only)
    MOTOGP_WARMUP = 18
    
    # Sunday - Races
    MOTO3_RACE = 19
    MOTO2_RACE = 20
    MOTOGP_RACE = 21
    
    # Post-race (MotoGP only)
    MOTOGP_CHEQUERED_FLAG = 22
    
    # Unknown/fallback
    UNKNOWN = 99
    
    @property
    def display_name(self) -> str:
        """Human-readable session name (without class prefix)."""
        return SESSION_DISPLAY_NAMES.get(self, "Unknown")
    
    @property
    def full_display_name(self) -> str:
        """Full display name including class (e.g., 'MotoGP Race')."""
        race_class = self.race_class
        if race_class:
            return f"{race_class.display_name} {self.display_name}"
        return self.display_name
    
    @property
    def race_class(self) -> Optional[RaceClass]:
        """Get the race class for this session."""
        return SESSION_RACE_CLASS.get(self)
    
    @property
    def is_core_session(self) -> bool:
        """Check if this is a core (commonly wanted) session."""
        return self in CORE_SESSIONS
    
    @property
    def is_feature_race(self) -> bool:
        """Check if this is a feature race session."""
        return self in FEATURE_RACE_SESSIONS
    
    @property
    def calendar_key(self) -> Optional[str]:
        """Get the calendar session key (e.g., 'motogp_race')."""
        return SESSION_TO_CALENDAR_KEY.get(self)

# Session display names (session type only, without class)
SESSION_DISPLAY_NAMES = {
    SessionType.MOTOGP_PRESS_CONFERENCE: "Pre-event Press Conference",
    SessionType.MOTO3_FP1: "Free Practice 1",
    SessionType.MOTOGP_FP1: "Free Practice 1",
    SessionType.MOTO2_FP1: "Free Practice 1",
    SessionType.MOTO3_PRACTICE: "Practice",
    SessionType.MOTOGP_PRACTICE: "Practice",
    SessionType.MOTO2_PRACTICE: "Practice",
    SessionType.MOTO3_FP2: "Free Practice 2",
    SessionType.MOTOGP_FP2: "Free Practice 2",
    SessionType.MOTO2_FP2: "Free Practice 2",
    SessionType.MOTO3_Q1: "Qualifying 1",
    SessionType.MOTO3_Q2: "Qualifying 2",
    SessionType.MOTOGP_Q1: "Qualifying 1",
    SessionType.MOTOGP_Q2: "Qualifying 2",
    SessionType.MOTOGP_SPRINT: "Sprint",
    SessionType.MOTO2_Q1: "Qualifying 1",
    SessionType.MOTO2_Q2: "Qualifying 2",
    SessionType.MOTOGP_WARMUP: "Warm Up",
    SessionType.MOTO3_RACE: "Race",
    SessionType.MOTO2_RACE: "Race",
    SessionType.MOTOGP_RACE: "Race",
    SessionType.MOTOGP_CHEQUERED_FLAG: "Chequered Flag",
    SessionType.UNKNOWN: "Unknown",
}

# Map session types to their race class
SESSION_RACE_CLASS = {
    SessionType.MOTOGP_PRESS_CONFERENCE: RaceClass.MOTOGP,
    SessionType.MOTO3_FP1: RaceClass.MOTO3,
    SessionType.MOTOGP_FP1: RaceClass.MOTOGP,
    SessionType.MOTO2_FP1: RaceClass.MOTO2,
    SessionType.MOTO3_PRACTICE: RaceClass.MOTO3,
    SessionType.MOTOGP_PRACTICE: RaceClass.MOTOGP,
    SessionType.MOTO2_PRACTICE: RaceClass.MOTO2,
    SessionType.MOTO3_FP2: RaceClass.MOTO3,
    SessionType.MOTOGP_FP2: RaceClass.MOTOGP,
    SessionType.MOTO2_FP2: RaceClass.MOTO2,
    SessionType.MOTO3_Q1: RaceClass.MOTO3,
    SessionType.MOTO3_Q2: RaceClass.MOTO3,
    SessionType.MOTOGP_Q1: RaceClass.MOTOGP,
    SessionType.MOTOGP_Q2: RaceClass.MOTOGP,
    SessionType.MOTOGP_SPRINT: RaceClass.MOTOGP,
    SessionType.MOTO2_Q1: RaceClass.MOTO2,
    SessionType.MOTO2_Q2: RaceClass.MOTO2,
    SessionType.MOTOGP_WARMUP: RaceClass.MOTOGP,
    SessionType.MOTO3_RACE: RaceClass.MOTO3,
    SessionType.MOTO2_RACE: RaceClass.MOTO2,
    SessionType.MOTOGP_RACE: RaceClass.MOTOGP,
    SessionType.MOTOGP_CHEQUERED_FLAG: RaceClass.MOTOGP,
}

# Core sessions (typically wanted for collection)
CORE_SESSIONS: Set[SessionType] = {
    SessionType.MOTO3_PRACTICE,
    SessionType.MOTOGP_PRACTICE,
    SessionType.MOTO2_PRACTICE,
    SessionType.MOTO3_Q1,
    SessionType.MOTO3_Q2,
    SessionType.MOTOGP_Q1,
    SessionType.MOTOGP_Q2,
    SessionType.MOTO2_Q1,
    SessionType.MOTO2_Q2,
    SessionType.MOTOGP_SPRINT,
    SessionType.MOTO3_RACE,
    SessionType.MOTO2_RACE,
    SessionType.MOTOGP_RACE,
}

# Feature race sessions (for special genre tagging)
FEATURE_RACE_SESSIONS: Set[SessionType] = {
    SessionType.MOTO3_RACE,
    SessionType.MOTO2_RACE,
    SessionType.MOTOGP_RACE,
}

# Default desired sessions configuration
DEFAULT_DESIRED_SESSIONS: Dict[SessionType, bool] = {
    SessionType.MOTOGP_PRESS_CONFERENCE: True,
    SessionType.MOTO3_FP1: True,
    SessionType.MOTOGP_FP1: True,
    SessionType.MOTO2_FP1: True,
    SessionType.MOTO3_PRACTICE: True,
    SessionType.MOTOGP_PRACTICE: True,
    SessionType.MOTO2_PRACTICE: True,
    SessionType.MOTO3_FP2: True,
    SessionType.MOTOGP_FP2: True,
    SessionType.MOTO2_FP2: True,
    SessionType.MOTO3_Q1: True,
    SessionType.MOTO3_Q2: True,
    SessionType.MOTOGP_Q1: True,
    SessionType.MOTOGP_Q2: True,
    SessionType.MOTOGP_SPRINT: True,
    SessionType.MOTO2_Q1: True,
    SessionType.MOTO2_Q2: True,
    SessionType.MOTOGP_WARMUP: False,
    SessionType.MOTO3_RACE: True,
    SessionType.MOTO2_RACE: True,
    SessionType.MOTOGP_RACE: True,
    SessionType.MOTOGP_CHEQUERED_FLAG: True,
}

# =============================================================================
# CALENDAR KEY MAPPINGS
# =============================================================================

# Map calendar session keys to SessionType
CALENDAR_KEY_TO_SESSION: Dict[str, SessionType] = {
    "moto3_fp1": SessionType.MOTO3_FP1,
    "motogp_fp1": SessionType.MOTOGP_FP1,
    "moto2_fp1": SessionType.MOTO2_FP1,
    "moto3_practice": SessionType.MOTO3_PRACTICE,
    "motogp_practice": SessionType.MOTOGP_PRACTICE,
    "moto2_practice": SessionType.MOTO2_PRACTICE,
    "moto3_fp2": SessionType.MOTO3_FP2,
    "motogp_fp2": SessionType.MOTOGP_FP2,
    "moto2_fp2": SessionType.MOTO2_FP2,
    "moto3_qualifying": SessionType.MOTO3_Q2,
    "motogp_qualifying": SessionType.MOTOGP_Q2,
    "moto2_qualifying": SessionType.MOTO2_Q2,
    "motogp_sprint": SessionType.MOTOGP_SPRINT,
    "motogp_warmup": SessionType.MOTOGP_WARMUP,
    "moto3_race": SessionType.MOTO3_RACE,
    "moto2_race": SessionType.MOTO2_RACE,
    "motogp_race": SessionType.MOTOGP_RACE,
}

# Reverse mapping
SESSION_TO_CALENDAR_KEY: Dict[SessionType, str] = {
    SessionType.MOTO3_FP1: "moto3_fp1",
    SessionType.MOTOGP_FP1: "motogp_fp1",
    SessionType.MOTO2_FP1: "moto2_fp1",
    SessionType.MOTO3_PRACTICE: "moto3_practice",
    SessionType.MOTOGP_PRACTICE: "motogp_practice",
    SessionType.MOTO2_PRACTICE: "moto2_practice",
    SessionType.MOTO3_FP2: "moto3_fp2",
    SessionType.MOTOGP_FP2: "motogp_fp2",
    SessionType.MOTO2_FP2: "moto2_fp2",
    SessionType.MOTO3_Q1: "moto3_qualifying",
    SessionType.MOTO3_Q2: "moto3_qualifying",
    SessionType.MOTOGP_Q1: "motogp_qualifying",
    SessionType.MOTOGP_Q2: "motogp_qualifying",
    SessionType.MOTOGP_SPRINT: "motogp_sprint",
    SessionType.MOTO2_Q1: "moto2_qualifying",
    SessionType.MOTO2_Q2: "moto2_qualifying",
    SessionType.MOTOGP_WARMUP: "motogp_warmup",
    SessionType.MOTO3_RACE: "moto3_race",
    SessionType.MOTO2_RACE: "moto2_race",
    SessionType.MOTOGP_RACE: "motogp_race",
}

# Map calendar keys to their day in the weekend
CALENDAR_KEY_TO_DAY: Dict[str, str] = {
    "moto3_fp1": "friday",
    "motogp_fp1": "friday",
    "moto2_fp1": "friday",
    "moto3_practice": "friday",
    "motogp_practice": "friday",
    "moto2_practice": "friday",
    "moto3_fp2": "saturday",
    "motogp_fp2": "saturday",
    "moto2_fp2": "saturday",
    "moto3_qualifying": "saturday",
    "motogp_qualifying": "saturday",
    "motogp_sprint": "saturday",
    "moto2_qualifying": "saturday",
    "motogp_warmup": "sunday",
    "moto3_race": "sunday",
    "moto2_race": "sunday",
    "motogp_race": "sunday",
}

# =============================================================================
# SESSION PATTERN MATCHING
# =============================================================================

@dataclass
class SessionPattern:
    """Pattern for matching session types from release titles."""
    session_type: SessionType
    must_have: List[str]
    must_not_have: List[str]
    priority: int = 0

# Session patterns ordered by priority (most specific first)
SESSION_PATTERNS: List[SessionPattern] = [
    # === PRE-EVENT ===
    SessionPattern(
        session_type=SessionType.MOTOGP_PRESS_CONFERENCE,
        must_have=["pre event", "pre-event", "press conference", "pre.event", "gear up", "gear.up"],
        must_not_have=[],
        priority=100,
    ),
    
    # === CHEQUERED FLAG ===
    SessionPattern(
        session_type=SessionType.MOTOGP_CHEQUERED_FLAG,
        must_have=["chequered flag", "checkered flag", "chequered.flag", "checkered.flag"],
        must_not_have=[],
        priority=100,
    ),
    
    # === SPRINT ===
    SessionPattern(
        session_type=SessionType.MOTOGP_SPRINT,
        must_have=["sprint"],
        must_not_have=["qualifying"],
        priority=95,
    ),
    
    # === WARM UP ===
    SessionPattern(
        session_type=SessionType.MOTOGP_WARMUP,
        must_have=["warm up", "warm-up", "warmup", "warming up", "warm.up", "warming.up"],
        must_not_have=[],
        priority=95,
    ),
    
    # === FREE PRACTICE 1 ===
    SessionPattern(
        session_type=SessionType.MOTOGP_FP1,
        must_have=["fp1", "free practice 1", "free practice one", 
                   "free.practice.1", "free.practice.one"],
        must_not_have=[],
        priority=90,
    ),
    
    # === FREE PRACTICE 2 ===
    SessionPattern(
        session_type=SessionType.MOTOGP_FP2,
        must_have=["fp2", "free practice 2", "free practice two", 
                   "free.practice.2", "free.practice.two"],
        must_not_have=[],
        priority=90,
    ),
    
    # === QUALIFYING 1 ===
    SessionPattern(
        session_type=SessionType.MOTOGP_Q1,
        must_have=["q1", "qualifying 1", "qualifying one", "qualifying.1", "qualifying.one"],
        must_not_have=[],
        priority=88,
    ),
    
    # === QUALIFYING 2 ===
    SessionPattern(
        session_type=SessionType.MOTOGP_Q2,
        must_have=["q2", "qualifying 2", "qualifying two", "qualifying.2", "qualifying.two"],
        must_not_have=[],
        priority=88,
    ),
    
    # === QUALIFYING (combined) ===
    SessionPattern(
        session_type=SessionType.MOTOGP_Q2,
        must_have=["qualifying", "qualifing"],
        must_not_have=["q1", "q2", "qualifying 1", "qualifying 2", 
                       "qualifying.1", "qualifying.2", "qualifying one", "qualifying two"],
        priority=85,
    ),
    
    # === PRACTICE ===
    SessionPattern(
        session_type=SessionType.MOTOGP_PRACTICE,
        must_have=["practice"],
        must_not_have=["free practice", "free.practice", "fp1", "fp2", "fp 1", "fp 2"],
        priority=82,
    ),
    
    # === RACE ===
    SessionPattern(
        session_type=SessionType.MOTOGP_RACE,
        must_have=["race"],
        must_not_have=["sprint", "sprint race"],
        priority=75,
    ),
]


def _remap_session_for_class(session_type: SessionType, race_class: RaceClass) -> SessionType:
    """Remap a generic session type to the class-specific version."""
    session_base_map = {
        # FP1
        (SessionType.MOTOGP_FP1, RaceClass.MOTO3): SessionType.MOTO3_FP1,
        (SessionType.MOTOGP_FP1, RaceClass.MOTO2): SessionType.MOTO2_FP1,
        (SessionType.MOTOGP_FP1, RaceClass.MOTOGP): SessionType.MOTOGP_FP1,
        # FP2
        (SessionType.MOTOGP_FP2, RaceClass.MOTO3): SessionType.MOTO3_FP2,
        (SessionType.MOTOGP_FP2, RaceClass.MOTO2): SessionType.MOTO2_FP2,
        (SessionType.MOTOGP_FP2, RaceClass.MOTOGP): SessionType.MOTOGP_FP2,
        # Practice
        (SessionType.MOTOGP_PRACTICE, RaceClass.MOTO3): SessionType.MOTO3_PRACTICE,
        (SessionType.MOTOGP_PRACTICE, RaceClass.MOTO2): SessionType.MOTO2_PRACTICE,
        (SessionType.MOTOGP_PRACTICE, RaceClass.MOTOGP): SessionType.MOTOGP_PRACTICE,
        # Q1
        (SessionType.MOTOGP_Q1, RaceClass.MOTO3): SessionType.MOTO3_Q1,
        (SessionType.MOTOGP_Q1, RaceClass.MOTO2): SessionType.MOTO2_Q1,
        (SessionType.MOTOGP_Q1, RaceClass.MOTOGP): SessionType.MOTOGP_Q1,
        # Q2
        (SessionType.MOTOGP_Q2, RaceClass.MOTO3): SessionType.MOTO3_Q2,
        (SessionType.MOTOGP_Q2, RaceClass.MOTO2): SessionType.MOTO2_Q2,
        (SessionType.MOTOGP_Q2, RaceClass.MOTOGP): SessionType.MOTOGP_Q2,
        # Race
        (SessionType.MOTOGP_RACE, RaceClass.MOTO3): SessionType.MOTO3_RACE,
        (SessionType.MOTOGP_RACE, RaceClass.MOTO2): SessionType.MOTO2_RACE,
        (SessionType.MOTOGP_RACE, RaceClass.MOTOGP): SessionType.MOTOGP_RACE,
    }
    
    return session_base_map.get((session_type, race_class), session_type)


def detect_race_class(text: str) -> RaceClass:
    """Detect race class from text."""
    text_lower = text.lower()
    text_normalized = text_lower.replace(".", " ").replace("-", " ")
    
    # Sprint is always MotoGP
    if "sprint" in text_normalized:
        return RaceClass.MOTOGP
    
    # Warm Up is always MotoGP
    if any(term in text_normalized for term in ["warm up", "warmup", "warming up"]):
        return RaceClass.MOTOGP
    
    # Chequered Flag is MotoGP class
    if "chequered" in text_normalized and "flag" in text_normalized:
        return RaceClass.MOTOGP
    
    # Pre-event press is MotoGP class
    if "pre" in text_normalized and "event" in text_normalized:
        return RaceClass.MOTOGP
    
    # Check explicit class mentions (order matters: moto3/moto2 before motogp)
    if re.search(r'moto\s*3', text_normalized):
        return RaceClass.MOTO3
    elif re.search(r'moto\s*2', text_normalized):
        return RaceClass.MOTO2
    elif re.search(r'moto\s*gp', text_normalized):
        return RaceClass.MOTOGP
    
    # Default to MotoGP
    return RaceClass.MOTOGP


def classify_session(text: str, race_class: RaceClass = None) -> SessionType:
    """Classify session type from text."""
    text_lower = text.lower()
    text_normalized = text_lower.replace(".", " ").replace("-", " ")
    
    if race_class is None:
        race_class = detect_race_class(text)
    
    sorted_patterns = sorted(SESSION_PATTERNS, key=lambda p: p.priority, reverse=True)
    
    for pattern in sorted_patterns:
        has_required = any(term in text_normalized for term in pattern.must_have)
        
        if not has_required:
            continue
        
        has_excluded = any(term in text_normalized for term in pattern.must_not_have)
        
        if has_excluded:
            continue
        
        return _remap_session_for_class(pattern.session_type, race_class)
    
    # Fallback: assume Race
    return _remap_session_for_class(SessionType.MOTOGP_RACE, race_class)


def get_session_from_element(element_num: int) -> Optional[SessionType]:
    """Get SessionType from element number in prefix."""
    for session_type in SessionType:
        if int(session_type) == element_num:
            return session_type
    return None

# =============================================================================
# PREFIX HANDLING
# =============================================================================

def build_prefix(season: int, round_num: int, session_type: SessionType) -> str:
    """
    Build the YYRREE prefix for a session.
    
    Format: YYRREE
        YY = 2-digit year
        RR = 2-digit round number
        EE = 2-digit session/element number
    
    Example: 25R06E21 = 2025, Round 6, MotoGP Race (element 21)
    """
    year_short = season % 100
    element_num = int(session_type)
    return f"{year_short:02d}R{round_num:02d}E{element_num:02d}"


def parse_prefix(text: str) -> Optional[Tuple[int, int, SessionType]]:
    """Parse a YYRREE prefix from text."""
    match = re.match(r'(\d{2})R(\d{2})E(\d{2})', text)
    if not match:
        return None
    
    year_short = int(match.group(1))
    round_num = int(match.group(2))
    element_num = int(match.group(3))
    
    year = 2000 + year_short
    session_type = get_session_from_element(element_num)
    
    if session_type is None:
        return None
    
    return (year, round_num, session_type)

# =============================================================================
# CALENDAR FUNCTIONS
# =============================================================================

def load_calendar(season: int) -> Optional[dict]:
    """Load the MotoGP calendar for a given season."""
    calendar_file = CONFIG_DIR / f"motogp_{season}.json"
    
    if not calendar_file.exists():
        return None
    
    try:
        with open(calendar_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def get_round_by_number(calendar: dict, round_num: int) -> Optional[dict]:
    """Get round info by round number."""
    if not calendar:
        return None
    
    for round_entry in calendar.get("rounds", []):
        if round_entry.get("round") == round_num:
            return round_entry
    
    return None


def extract_round_number(text: str) -> Optional[int]:
    """Extract round number from text."""
    text_lower = text.lower()
    
    number_words = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
        'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
        'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
        'nineteen': 19, 'twenty': 20, 'twentyone': 21, 'twentytwo': 22,
    }
    
    match = re.search(r'round\s*(\d+)', text_lower)
    if match:
        return int(match.group(1))
    
    match = re.search(r'\br(\d{1,2})\b', text_lower)
    if match:
        return int(match.group(1))
    
    match = re.search(r'round\s*([a-z]+)', text_lower)
    if match and match.group(1) in number_words:
        return number_words[match.group(1)]
    
    return None


def match_venue(text: str, calendar: dict) -> Optional[dict]:
    """Match text against venue names and aliases in calendar."""
    if not calendar:
        return None
    
    text_lower = text.lower()
    text_normalized = text_lower.replace(".", " ").replace("-", " ")
    tokens = set(text_normalized.split())
    
    # Phase 0: Extract "Grand Prix of X" pattern
    gp_match = re.search(r'grand\s*prix\s*of\s*(\w+)', text_normalized)
    if gp_match:
        gp_location = gp_match.group(1).lower()
        for round_entry in calendar.get("rounds", []):
            venue = round_entry.get("venue", "").lower()
            aliases = [a.lower() for a in round_entry.get("aliases", [])]
            
            if gp_location == venue or gp_location in aliases:
                return round_entry
            
            if gp_location in venue or venue in gp_location:
                return round_entry
            for alias in aliases:
                if gp_location in alias or alias in gp_location:
                    return round_entry
    
    # Phase 1: Multi-word alias matching
    for round_entry in calendar.get("rounds", []):
        venue = round_entry.get("venue", "").lower()
        aliases = [a.lower() for a in round_entry.get("aliases", [])]
        
        all_candidates = [venue] + aliases
        
        for candidate in all_candidates:
            if " " in candidate and candidate in text_normalized:
                return round_entry
    
    # Phase 2: Single-word matching
    for round_entry in calendar.get("rounds", []):
        venue = round_entry.get("venue", "").lower()
        aliases = [a.lower() for a in round_entry.get("aliases", [])]
        
        all_candidates = [venue] + aliases
        
        for candidate in all_candidates:
            if " " not in candidate and candidate in tokens:
                return round_entry
    
    return None


def identify_round(text: str, calendar: dict) -> Optional[dict]:
    """Identify round from text."""
    if not calendar:
        return None
    
    round_num = extract_round_number(text)
    if round_num:
        round_info = get_round_by_number(calendar, round_num)
        if round_info:
            return round_info
    
    return match_venue(text, calendar)


def has_session_times(calendar: dict) -> bool:
    """Check if calendar has detailed session times (2026+ format)."""
    if not calendar:
        return False
    
    rounds = calendar.get("rounds", [])
    if not rounds:
        return False
    
    return "sessions" in rounds[0]

# =============================================================================
# BROADCASTER PREFERENCES
# =============================================================================

PREFERRED_BROADCASTERS = {
    "tnt": 25,
    "bt sport": 20,
    "bt.sport": 20,
    "btsport": 20,
}

# =============================================================================
# RELEASE PARSING AND FILTERING
# =============================================================================

@dataclass
class ReleaseInfo:
    """Parsed information from a release title."""
    title: str
    year: Optional[int] = None
    round_num: Optional[int] = None
    venue: Optional[str] = None
    session_type: SessionType = SessionType.UNKNOWN
    race_class: Optional[RaceClass] = None
    quality: Optional[str] = None
    group: Optional[str] = None
    broadcaster: Optional[str] = None


def parse_release_title(title: str, calendar: dict = None) -> ReleaseInfo:
    """Parse a release title to extract metadata."""
    info = ReleaseInfo(title=title)
    
    title_lower = title.lower()
    title_normalized = title_lower.replace(".", " ").replace("-", " ")
    
    # Extract year
    year_match = re.search(r'\b(20\d{2})\b', title)
    if year_match:
        info.year = int(year_match.group(1))
    
    # Extract quality
    quality_match = re.search(r'\b(2160p|1080p|1080i|720p|480p)\b', title_lower)
    if quality_match:
        info.quality = quality_match.group(1)
    
    # Extract release group
    group_patterns = [
        r'-(\w+)$',
        r'\[(\w+)\]',
    ]
    for pattern in group_patterns:
        group_match = re.search(pattern, title)
        if group_match:
            info.group = group_match.group(1)
            break
    
    # Extract broadcaster
    for broadcaster in PREFERRED_BROADCASTERS.keys():
        if broadcaster in title_normalized:
            info.broadcaster = broadcaster
            break
    
    # Detect race class
    info.race_class = detect_race_class(title)
    
    # Classify session
    info.session_type = classify_session(title, info.race_class)
    
    # Try to identify round
    if calendar:
        round_info = identify_round(title, calendar)
        if round_info:
            info.round_num = round_info.get("round")
            info.venue = round_info.get("venue")
    else:
        info.round_num = extract_round_number(title)
    
    return info


def filter_releases(
    releases: List[dict],
    target_session: SessionType,
    season: int,
    round_info: dict,
    required_quality: str = "1080p",
    allowed_groups: List[str] = None,
    calendar: dict = None
) -> List[dict]:
    """Filter and score releases for a target session."""
    if allowed_groups is None:
        allowed_groups = ["VERUM", "MWR"]
    
    venue = round_info.get("venue", "").lower()
    aliases = [a.lower() for a in round_info.get("aliases", [])]
    all_locations = [venue] + aliases
    round_num = round_info.get("round", 0)
    
    target_class = target_session.race_class
    target_session_name = target_session.display_name
    
    scored = []
    excluded_count = 0
    
    # Terms to exclude globally
    excluded_terms = [
        "full event", "full.event", "fullevent",
        "weekend", "race weekend",
        "complete weekend", "complete.weekend",
        "all sessions", "all.sessions",
    ]
    
    for release in releases:
        title = release.get('title', '')
        title_lower = title.lower()
        title_normalized = title_lower.replace('.', ' ').replace('-', ' ')
        
        # GLOBAL EXCLUSIONS
        if any(term in title_normalized for term in excluded_terms):
            excluded_count += 1
            continue
        
        # Must match class
        detected_class = detect_race_class(title)
        if detected_class != target_class:
            continue
        
        # Must match location or round number
        location_match = any(loc in title_lower for loc in all_locations)
        if not location_match:
            round_patterns = [
                f"round{round_num:02d}",
                f"round{round_num}",
                f"round.{round_num:02d}",
                f"round.{round_num}",
                f"round {round_num}",
            ]
            if not any(p in title_lower or p in title_normalized for p in round_patterns):
                continue
        
        # Must have required quality
        quality_base = required_quality.lower().rstrip('pi')
        if quality_base not in title_lower:
            continue
        
        # Must be from allowed release group
        group_match = any(g.lower() in title_lower for g in allowed_groups)
        if not group_match:
            continue
        
        # Must match year
        if str(season) not in title:
            continue
        
        # SESSION MATCHING
        detected_session = classify_session(title, detected_class)
        
        session_match = False
        is_combined_quali = False
        
        if detected_session == target_session:
            session_match = True
        elif target_session_name in ["Qualifying 1", "Qualifying 2"]:
            target_is_q1 = target_session_name == "Qualifying 1"
            target_is_q2 = target_session_name == "Qualifying 2"
            
            q1_patterns = ["q1", "qualifying 1", "qualifying.1", "qualifying one", "qualifying.one"]
            q2_patterns = ["q2", "qualifying 2", "qualifying.2", "qualifying two", "qualifying.two"]
            
            has_q1 = any(p in title_normalized for p in q1_patterns)
            has_q2 = any(p in title_normalized for p in q2_patterns)
            
            if target_is_q1 and has_q1:
                session_match = True
            elif target_is_q2 and has_q2:
                session_match = True
            elif not has_q1 and not has_q2:
                if "qualifying" in title_normalized or "qualifing" in title_normalized:
                    session_match = True
                    is_combined_quali = True
                    release['_combined_quali'] = True
        
        if not session_match:
            continue
        
        # Scoring
        score = 100
        if detected_session == target_session:
            score += 25
        elif is_combined_quali:
            score += 10
        
        broadcaster_score = 0
        for broadcaster, bonus in PREFERRED_BROADCASTERS.items():
            if broadcaster in title_normalized:
                broadcaster_score = bonus
                release['_broadcaster'] = broadcaster
                break
        score += broadcaster_score
        
        if "verum" in title_lower:
            score += 20
            release['_group'] = 'VERUM'
        elif "mwr" in title_lower:
            score += 20
            release['_group'] = 'MWR'
        
        if "1080p" in title_lower:
            score += 10
        elif "2160p" in title_lower:
            score += 5
        
        release['_score'] = score
        release['_broadcaster_score'] = broadcaster_score
        scored.append(release)
    
    if excluded_count > 0:
        logging.debug(f"Excluded {excluded_count} full event/weekend releases")
    
    scored.sort(key=lambda x: x.get('_score', 0), reverse=True)
    
    return scored

# =============================================================================
# VENUE/COUNTRY MAPPING
# =============================================================================

VENUE_COUNTRY_MAP = {
    "Thailand": "Thailand",
    "Argentina": "Argentina",
    "Americas": "United States",
    "Qatar": "Qatar",
    "Spain": "Spain",
    "Jerez": "Spain",
    "France": "France",
    "Great Britain": "United Kingdom",
    "Aragon": "Spain",
    "Italy": "Italy",
    "Mugello": "Italy",
    "Misano": "Italy",
    "Netherlands": "Netherlands",
    "Assen": "Netherlands",
    "Germany": "Germany",
    "Sachsenring": "Germany",
    "Czech Republic": "Czech Republic",
    "Austria": "Austria",
    "Spielberg": "Austria",
    "Hungary": "Hungary",
    "Catalunya": "Spain",
    "Barcelona": "Spain",
    "San Marino": "San Marino",
    "Japan": "Japan",
    "Motegi": "Japan",
    "Indonesia": "Indonesia",
    "Mandalika": "Indonesia",
    "Australia": "Australia",
    "Phillip Island": "Australia",
    "Malaysia": "Malaysia",
    "Sepang": "Malaysia",
    "Portugal": "Portugal",
    "Portimao": "Portugal",
    "Valencia": "Spain",
    "Brazil": "Brazil",
}


def get_country_for_venue(venue: str) -> Optional[str]:
    """Get country name for a venue."""
    return VENUE_COUNTRY_MAP.get(venue)

# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MotoGP Common Module - Test utilities",
    )
    
    parser.add_argument('--test-inline', metavar='TITLE', help='Test single title')
    parser.add_argument('--season', type=int, help='Season year for testing')
    parser.add_argument('--list-sessions', action='store_true', help='List all session types')
    
    args = parser.parse_args()
    
    if args.list_sessions:
        print("\nSESSION TYPES\n" + "=" * 50)
        current_class = None
        for session in SessionType:
            if session == SessionType.UNKNOWN:
                continue
            race_class = session.race_class
            if race_class != current_class:
                current_class = race_class
                if race_class:
                    print(f"\n{race_class.display_name}:")
            core = "⭐" if session.is_core_session else "  "
            feature = "🏁" if session.is_feature_race else "  "
            print(f"  {core}{feature} E{int(session):02d} - {session.display_name}")
    
    elif args.test_inline:
        title = args.test_inline
        print(f"\nTitle: {title}")
        race_class = detect_race_class(title)
        session = classify_session(title, race_class)
        print(f"  Class:   {race_class.display_name}")
        print(f"  Session: {session.display_name}")
        print(f"  Full:    {session.full_display_name}")
        print(f"  Element: E{int(session):02d}")
        
        if args.season:
            calendar = load_calendar(args.season)
            if calendar:
                round_info = identify_round(title, calendar)
                if round_info:
                    print(f"  Round:   {round_info['round']} - {round_info['venue']}")
                    prefix = build_prefix(args.season, round_info['round'], session)
                    print(f"  Prefix:  {prefix}")
    else:
        parser.print_help()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())