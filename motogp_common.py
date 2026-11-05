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
from datetime import datetime
from enum import IntEnum
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# =============================================================================
# CENTRALIZED LOGGING
# =============================================================================

import logging
from logging.handlers import RotatingFileHandler
from typing import Optional
import threading

# Global lock for thread-safe logging setup
_logging_lock = threading.Lock()
_logging_initialized = False


class LogConfig:
    """Logging configuration singleton."""
    
    # Default paths - overridden by setup_logging()
    log_dir: Path = Path("/config/logs")
    
    # Log levels
    console_level: int = logging.INFO
    file_level: int = logging.DEBUG
    
    # File settings
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5
    
    # Debug mode
    debug_mode: bool = False


def setup_logging(
    name: str = "motogp",
    log_dir: Optional[Path] = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    debug_mode: bool = False,
    force: bool = False
) -> logging.Logger:
    """
    Centralized logging setup for all MotoGP scripts.
    
    Call this once at the start of each script. Subsequent calls
    return the existing logger unless force=True.
    
    Args:
        name: Logger name (usually "motogp")
        log_dir: Directory for log files (default: /config/logs)
        console_level: Console output level
        file_level: File output level  
        debug_mode: Enable verbose debug logging
        force: Force reconfiguration even if already set up
    
    Returns:
        Configured root logger for the motogp hierarchy
    
    Usage:
        from motogp_common import setup_logging
        
        logger = setup_logging(debug_mode=True)
        # or for module-specific logger:
        logger = logging.getLogger("motogp.watcher")
    """
    global _logging_initialized
    
    with _logging_lock:
        # Return existing logger if already configured
        if _logging_initialized and not force:
            return logging.getLogger("motogp")
        
        # Determine log directory
        if log_dir is None:
            # Try config dir first, fall back to script dir
            config_log_dir = CONFIG_DIR / "logs"
            if CONFIG_DIR.exists() or str(CONFIG_DIR).startswith("/config"):
                log_dir = config_log_dir
            else:
                log_dir = SCRIPT_DIR / "logs"
        
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Update config
        LogConfig.log_dir = log_dir
        LogConfig.console_level = console_level
        LogConfig.file_level = file_level
        LogConfig.debug_mode = debug_mode
        
        # Get or create the motogp root logger
        logger = logging.getLogger("motogp")
        logger.setLevel(logging.DEBUG)
        
        # Clear existing handlers
        logger.handlers.clear()

        # Prevent propagation to root logger (avoids double logging)
        logger.propagate = False        

        # Formatter with module identification
        detailed_formatter = logging.Formatter(        
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        # Simple formatter for console
        console_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S"
        )
        
        # Console handler
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if debug_mode else console_level)
        console.setFormatter(console_formatter)
        logger.addHandler(console)
        
        # Main log file (rotating)
        main_log = log_dir / "motogp.log"
        try:
            file_handler = RotatingFileHandler(
                main_log,
                maxBytes=LogConfig.max_bytes,
                backupCount=LogConfig.backup_count,
                encoding="utf-8"
            )
            file_handler.setLevel(file_level)
            file_handler.setFormatter(detailed_formatter)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.warning(f"Could not create log file {main_log}: {e}")
        
        # Debug log (separate, more verbose) - only in debug mode
        if debug_mode:
            debug_log = log_dir / "motogp_debug.log"
            try:
                debug_formatter = logging.Formatter(
                    "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )
                debug_handler = RotatingFileHandler(
                    debug_log,
                    maxBytes=50 * 1024 * 1024,  # 50MB for debug
                    backupCount=3,
                    encoding="utf-8"
                )
                debug_handler.setLevel(logging.DEBUG)
                debug_handler.setFormatter(debug_formatter)
                logger.addHandler(debug_handler)
            except Exception as e:
                logger.warning(f"Could not create debug log file: {e}")
        
        _logging_initialized = True
        
        # Log startup
        logger.info(f"Logging initialized: {log_dir}")
        if debug_mode:
            logger.debug("Debug mode enabled")
        
        return logger


def get_logger(name: str = None) -> logging.Logger:
    """
    Get a logger in the motogp hierarchy.
    
    Args:
        name: Optional sub-logger name (e.g., "watcher", "sort")
              If None, returns the root motogp logger
    
    Returns:
        Logger instance
    
    Usage:
        logger = get_logger("watcher")  # Returns motogp.watcher logger
        logger = get_logger()           # Returns motogp logger
    """
    if name:
        return logging.getLogger(f"motogp.{name}")
    return logging.getLogger("motogp")


# =============================================================================
# LEGACY LOGGING COMPATIBILITY
# =============================================================================

def setup_legacy_logging(
    log_prefix: str,
    log_dir: Optional[str] = None,
    dry_run: bool = False
) -> str:
    """
    Legacy logging setup for backwards compatibility.
    
    Creates timestamped log files like before, but also logs to
    the centralized motogp.log.
    
    Args:
        log_prefix: Prefix for log file (e.g., "grabber", "sort")
        log_dir: Directory for logs (default: /config/logs)
        dry_run: If True, adds DRY-RUN notice
    
    Returns:
        Path to the timestamped log file
    """
    from datetime import datetime
    
    # Set up centralized logging first
    setup_logging()
    
    # Determine log directory
    if log_dir is None:
        log_dir = str(LogConfig.log_dir)
    
    os.makedirs(log_dir, exist_ok=True)
    
    # Create timestamped log file
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"{log_prefix}_{timestamp}.log")
    
    # Add file handler for this specific run
    logger = logging.getLogger("motogp")
    
    run_handler = logging.FileHandler(log_file, encoding="utf-8")
    run_handler.setLevel(logging.DEBUG)
    run_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(run_handler)
    
    # Cleanup old logs (keep last 20)
    try:
        log_files = sorted([
            f for f in os.listdir(log_dir)
            if f.startswith(f"{log_prefix}_") and f.endswith(".log")
        ], reverse=True)
        for old_log in log_files[20:]:
            os.remove(os.path.join(log_dir, old_log))
    except Exception:
        pass
    
    logger.info(f"Log file: {log_file}")
    if dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    return log_file

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
    """
    Get the MotoGP library path.

    Configuration wins. When running without the config loader (direct
    execution of a script) fall back to MOTOGP_LIBRARY_PATH, then a neutral
    default - never a guessed share spelling, because a plausible-looking wrong
    path is far worse than an obvious one.
    """
    if _CONFIG_AVAILABLE:
        return get_motogp_library_path()
    return os.environ.get("MOTOGP_LIBRARY_PATH", "/media/MotoGP")


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
    
    # =========================================================================
    # Appended AFTER the historical values on purpose.
    #
    # The integer is baked into every library folder name as "E<NN>"
    # (yyRNNEee), so changing an existing value would orphan files that are
    # already on disk and in Plex. Only ever add new members here.
    #
    # These cover the 2027 MotoGP weekend format. Session slots other than
    # Saturday free practice are shared with the 2026 sessions above because
    # the release names are the same ("Practice", "Qualifying", "Race"):
    #
    #   Friday      FP1 45m, FP2 30m, Sprint Qualifying 15m  (NEW)
    #   Saturday    FP3 20m                                   (NEW)
    #               GP Q1/Q2, Sprint
    #   Sunday      Race (warm-up removed)
    # =========================================================================
    MOTOGP_FP3 = 23
    MOTOGP_SPRINT_QUALIFYING = 24
    
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

    @property
    def motogp_session_name(self) -> Optional[str]:
        """
        Name used by the weekend format definitions in the calendar.

        MotoGP weekends are described as a list of session names, e.g.
        ["fp1", "fp2", "sprint_qualifying", "fp3", "qualifying", "sprint",
        "race"] for 2027. This maps the session type onto that vocabulary so the
        grabber can tell whether a session will actually be run.
        """
        return SESSION_TO_MOTOGP_SESSION_NAME.get(self)

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
    SessionType.MOTOGP_FP3: "Free Practice 3",
    SessionType.MOTOGP_SPRINT_QUALIFYING: "Sprint Qualifying",
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
    SessionType.MOTOGP_FP3: RaceClass.MOTOGP,
    SessionType.MOTOGP_SPRINT_QUALIFYING: RaceClass.MOTOGP,
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
    SessionType.MOTOGP_PRESS_CONFERENCE: False,
    SessionType.MOTO3_FP1: False,
    SessionType.MOTOGP_FP1: True,
    SessionType.MOTO2_FP1: False,
    SessionType.MOTO3_PRACTICE: False,
    SessionType.MOTOGP_PRACTICE: True,
    SessionType.MOTO2_PRACTICE: True,
    SessionType.MOTO3_FP2: False,
    SessionType.MOTOGP_FP2: True,
    SessionType.MOTO2_FP2: False,
    SessionType.MOTOGP_FP3: True,
    SessionType.MOTOGP_SPRINT_QUALIFYING: True,
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
    SessionType.MOTOGP_CHEQUERED_FLAG: False,
}

def get_desired_sessions() -> Dict[SessionType, bool]:
    """
    Get desired sessions from WebUI config, falling back to defaults.
    
    The WebUI saves session preferences to webui_settings.json.
    This function loads those preferences at runtime.
    
    Returns:
        Dict mapping SessionType to whether it's desired (True/False)
    """
    webui_config = CONFIG_DIR / "webui_settings.json"
    
    if webui_config.exists():
        try:
            with open(webui_config, 'r') as f:
                config = json.load(f)
            
            sessions_config = config.get("sessions", {})
            
            if sessions_config:
                result = {}
                for session_type in SessionType:
                    if session_type == SessionType.UNKNOWN:
                        continue
                    # WebUI uses session type name as key (e.g., "MOTOGP_RACE")
                    key = session_type.name
                    # Fall back to default if not specified
                    default = DEFAULT_DESIRED_SESSIONS.get(session_type, False)
                    result[session_type] = sessions_config.get(key, default)
                
                # Log what we loaded (debug level)
                enabled_count = sum(1 for v in result.values() if v)
                logging.getLogger("motogp.common").debug(
                    f"Loaded desired sessions from config: {enabled_count} enabled"
                )
                return result
        
        except Exception as e:
            logging.getLogger("motogp.common").warning(
                f"Could not load webui_settings.json: {e}, using defaults"
            )
    
    # Fall back to defaults
    return DEFAULT_DESIRED_SESSIONS.copy()

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
    "motogp_fp3": SessionType.MOTOGP_FP3,
    "motogp_sprint_qualifying": SessionType.MOTOGP_SPRINT_QUALIFYING,
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
    SessionType.MOTOGP_FP3: "motogp_fp3",
    SessionType.MOTOGP_SPRINT_QUALIFYING: "motogp_sprint_qualifying",
    SessionType.MOTOGP_SPRINT: "motogp_sprint",
    SessionType.MOTO2_Q1: "moto2_qualifying",
    SessionType.MOTO2_Q2: "moto2_qualifying",
    SessionType.MOTOGP_WARMUP: "motogp_warmup",
    SessionType.MOTO3_RACE: "moto3_race",
    SessionType.MOTO2_RACE: "moto2_race",
    SessionType.MOTOGP_RACE: "motogp_race",
}

# Map MotoGP session types to weekend-format session names.
# Support class sessions are absent: only the MotoGP format varies by season.
# Note that MOTOGP_FP2 maps to "fp2" and MOTOGP_PRACTICE maps to "practice":
# in 2026 Friday runs FP1 + a one hour Practice, from 2027 Friday runs
# FP1 + a 30 minute FP2 + Sprint Qualifying, so the two names never collide
# within a single weekend.
SESSION_TO_MOTOGP_SESSION_NAME: Dict[SessionType, str] = {
    SessionType.MOTOGP_FP1: "fp1",
    SessionType.MOTOGP_PRACTICE: "practice",
    SessionType.MOTOGP_FP2: "fp2",
    SessionType.MOTOGP_FP3: "fp3",
    SessionType.MOTOGP_Q1: "qualifying",
    SessionType.MOTOGP_Q2: "qualifying",
    SessionType.MOTOGP_SPRINT_QUALIFYING: "sprint_qualifying",
    SessionType.MOTOGP_SPRINT: "sprint",
    SessionType.MOTOGP_WARMUP: "warmup",
    SessionType.MOTOGP_RACE: "race",
    SessionType.MOTOGP_CHEQUERED_FLAG: "chequered_flag",
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
    "motogp_fp3": "saturday",
    "motogp_sprint_qualifying": "friday",
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
    
    # === SPRINT QUALIFYING (2027+) ===
    # Must be tested before SPRINT, and SPRINT must exclude "qualifying",
    # otherwise "Sprint Qualifying" would be read as the Sprint race.
    SessionPattern(
        session_type=SessionType.MOTOGP_SPRINT_QUALIFYING,
        must_have=["sprint qualifying", "sprint.qualifying", "sprint quali",
                   "sprint shootout", "sprint qualifying"],
        must_not_have=[],
        priority=97,
    ),
    
    # === SPRINT ===
    SessionPattern(
        session_type=SessionType.MOTOGP_SPRINT,
        must_have=["sprint"],
        must_not_have=["qualifying", "quali", "qualifing", "shootout"],
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
    
    # === FREE PRACTICE 3 (2027+) ===
    SessionPattern(
        session_type=SessionType.MOTOGP_FP3,
        must_have=["fp3", "free practice 3", "free practice three",
                   "free.practice.3", "free.practice.three"],
        must_not_have=[],
        priority=91,
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


def matches_qualifying_part(text: str, part: int) -> bool:
    """
    Does this text refer to Q1 or Q2 specifically?

    Uses word boundaries so "Qualifying 1080p" is not read as "Qualifying 1".
    """
    text_normalized = text.lower().replace(".", " ").replace("-", " ")
    if part == 1:
        pattern = r"\bq1\b|\bqualifying\s*(?:1|one)\b(?!\d)"
    else:
        pattern = r"\bq2\b|\bqualifying\s*(?:2|two)\b(?!\d)"
    return re.search(pattern, text_normalized) is not None


def classify_session(text: str, race_class: RaceClass = None) -> SessionType:
    """Classify session type from text with explicit handling for practice sessions."""
    import logging
    import re
    logger = logging.getLogger("motogp.classify")
    
    text_lower = text.lower()
    text_normalized = text_lower.replace(".", " ").replace("-", " ")

    # Quality tokens are removed before matching. Without this, "Qualifying
    # 1080p" matches the "qualifying 1" pattern (and 2160p matches "2"),
    # which made plain qualifying releases look like Q1/Q2 parts.
    text_normalized = re.sub(
        r"\b(2160p|1080p|1080i|720p|480p|4k|uhd)\b", " ", text_normalized
    )

    logger.debug(f"=" * 50)
    logger.debug(f"CLASSIFY: '{text}'")
    logger.debug(f"NORMALIZED: '{text_normalized}'")

    if race_class is None:
        race_class = detect_race_class(text)
    logger.debug(f"RACE CLASS: {race_class.display_name}")

    # === SPRINT QUALIFYING (2027+) ===
    # Must be checked before the practice handling below, because the 2027
    # session is literally called "Sprint Qualifying" and must not be
    # classified as the Sprint race or as grand prix qualifying.
    if re.search(r'\bsprint\b', text_normalized) and re.search(
            r'\bqualif', text_normalized):
        logger.debug(f">>> SPRINT QUALIFYING MATCH")
        return SessionType.MOTOGP_SPRINT_QUALIFYING

    # === EXPLICIT PRACTICE SESSION HANDLING ===
    if "practice" in text_normalized:
        logger.debug(f">>> PRACTICE BLOCK ENTERED")

        # FP3 (2027+) - check before FP2/FP1 so "FP3" is not read as FP2.
        for pattern in [r'\bfp3\b', r'\bfp\s*3\b',
                        r'\bfree\s+practice\s+3\b',
                        r'\bfree\s+practice\s+three\b']:
            if re.search(pattern, text_normalized):
                logger.debug(f">>> FP3 MATCH: '{pattern}'")
                return SessionType.MOTOGP_FP3
        
        # Check for FP2 indicators using word boundaries
        fp2_patterns = [
            r'\bfp2\b',
            r'\bfp\s*2\b',
            r'\bfree\s+practice\s+2\b',
            r'\bfree\s+practice\s+two\b',
            r'\bpractice\s+two\b',
            r'\bpractice\s+2\b(?!\d)',  # practice 2 but not practice 2160p
        ]
        for pattern in fp2_patterns:
            if re.search(pattern, text_normalized):
                logger.debug(f">>> FP2 MATCH: '{pattern}'")
                if race_class == RaceClass.MOTO3:
                    return SessionType.MOTO3_FP2
                elif race_class == RaceClass.MOTO2:
                    return SessionType.MOTO2_FP2
                else:
                    return SessionType.MOTOGP_FP2

        # Check for FP1 indicators using word boundaries
        fp1_patterns = [
            r'\bfp1\b',
            r'\bfp\s*1\b',
            r'\bfree\s+practice\s+1\b',
            r'\bfree\s+practice\s+one\b',
            r'\bpractice\s+one\b',
            r'\bpractice\s+1\b(?!\d)',  # practice 1 but not practice 1080p
        ]
        for pattern in fp1_patterns:
            if re.search(pattern, text_normalized):
                logger.debug(f">>> FP1 MATCH: '{pattern}'")
                if race_class == RaceClass.MOTO3:
                    return SessionType.MOTO3_FP1
                elif race_class == RaceClass.MOTO2:
                    return SessionType.MOTO2_FP1
                else:
                    return SessionType.MOTOGP_FP1

        # Check for generic "Free Practice" (without number)
        if re.search(r'\bfree\s+practice\b', text_normalized):
            # Make sure it's not followed by a number
            if not re.search(r'\bfree\s+practice\s+(1|2|3|one|two|three)\b', text_normalized):
                logger.debug(f">>> FREE PRACTICE (generic) MATCH")
                if race_class == RaceClass.MOTO3:
                    return SessionType.MOTO3_FP1
                elif race_class == RaceClass.MOTO2:
                    return SessionType.MOTO2_FP1
                else:
                    return SessionType.MOTOGP_FP1

        # Standalone Practice - the actual Practice session
        logger.debug(f">>> STANDALONE PRACTICE - returning Practice session")
        if race_class == RaceClass.MOTO3:
            return SessionType.MOTO3_PRACTICE
        elif race_class == RaceClass.MOTO2:
            return SessionType.MOTO2_PRACTICE
        else:
            return SessionType.MOTOGP_PRACTICE

    # === REGULAR SESSION PATTERN MATCHING ===
    logger.debug(f">>> PATTERN MATCHING BLOCK")
    sorted_patterns = sorted(SESSION_PATTERNS, key=lambda p: p.priority, reverse=True)

    for pattern in sorted_patterns:
        has_required = any(term in text_normalized for term in pattern.must_have)
        if not has_required:
            continue

        has_excluded = any(term in text_normalized for term in pattern.must_not_have)
        if has_excluded:
            continue

        logger.debug(f">>> PATTERN MATCH: {pattern.session_type.name}")
        return _remap_session_for_class(pattern.session_type, race_class)

    # === FALLBACK ===
    logger.debug(f">>> FALLBACK")
    if "race" in text_normalized and "sprint" not in text_normalized:
        if race_class == RaceClass.MOTO3:
            return SessionType.MOTO3_RACE
        elif race_class == RaceClass.MOTO2:
            return SessionType.MOTO2_RACE
        else:
            return SessionType.MOTOGP_RACE

    if any(q in text_normalized for q in ["qualifying", "qualifing", "q1", "q2"]):
        if race_class == RaceClass.MOTO3:
            return SessionType.MOTO3_Q2
        elif race_class == RaceClass.MOTO2:
            return SessionType.MOTO2_Q2
        else:
            return SessionType.MOTOGP_Q2

    if race_class == RaceClass.MOTO3:
        return SessionType.MOTO3_RACE
    elif race_class == RaceClass.MOTO2:
        return SessionType.MOTO2_RACE
    else:
        return SessionType.MOTOGP_RACE
    
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
# ROUND STATUS AND WEEKEND FORMAT
# =============================================================================
#
# These helpers keep calendar *data* authoritative so a cancelled, postponed or
# rescheduled round, a MotoGP-only event, or a weekend format change never
# requires editing code again.

# Statuses that mean "this round is not happening as published".
INACTIVE_ROUND_STATUSES = {"cancelled", "canceled", "postponed", "replaced"}


def is_round_active(round_info: dict) -> bool:
    """Is this round still on the calendar (not cancelled/postponed/replaced)?"""
    if round_info is None:
        return False
    status = str(round_info.get("status", "scheduled")).lower()
    return status not in INACTIVE_ROUND_STATUSES


def get_round_status(round_info: dict) -> str:
    """Normalised round status, defaulting to 'scheduled'."""
    if not round_info:
        return "scheduled"
    return str(round_info.get("status", "scheduled")).lower()


def get_round_classes(round_info: dict, calendar: dict = None) -> List[str]:
    """Classes racing at this round, falling back to the calendar default."""
    if round_info and round_info.get("classes"):
        return list(round_info["classes"])
    if calendar and calendar.get("classes"):
        return list(calendar["classes"])
    return ["motogp", "moto2", "moto3"]


def get_weekend_format(round_info: dict, calendar: dict = None) -> dict:
    """
    Get the weekend format definition for a round.

    Looks up round_info["weekend_format"] in calendar["weekend_formats"].
    Returns {} for old style calendars that have no formats block.
    """
    if not round_info or not calendar:
        return {}
    formats = calendar.get("weekend_formats") or {}
    return formats.get(round_info.get("weekend_format", ""), {})


def get_round_motogp_sessions(round_info: dict, calendar: dict = None) -> Optional[List[str]]:
    """
    Which MotoGP sessions this round actually runs.

    Returns a list of session names such as ["fp1", "practice", "fp2",
    "qualifying", "sprint", "warmup", "race"], or None when the calendar does
    not declare them (meaning: assume everything the search rules find).
    """
    if not round_info:
        return None
    sessions = round_info.get("motogp_sessions")
    if sessions is not None:
        return list(sessions)

    fmt = get_weekend_format(round_info, calendar)
    if fmt:
        names = []
        for day_sessions in fmt.values():
            if isinstance(day_sessions, list):
                names.extend(s.get("session") for s in day_sessions if s.get("session"))
        return names

    return None


def round_runs_session(round_info: dict, session_type: SessionType,
                       calendar: dict = None) -> bool:
    """
    Does this round run the given session?

    Used to stop the grabber hunting for sessions that will never exist, e.g.
    a MotoGP warm-up from 2027 onwards.
    """
    if not is_round_active(round_info):
        return False

    session_class = session_type.race_class
    if session_class is not None:
        classes = get_round_classes(round_info, calendar)
        if session_class.folder_key not in classes:
            return False

    if session_type == SessionType.UNKNOWN:
        return False

    motogp_sessions = get_round_motogp_sessions(round_info, calendar)
    if motogp_sessions is None:
        return True

    if session_class != RaceClass.MOTOGP:
        # Only the MotoGP weekend format is described; the support classes
        # keep their usual sessions whenever they are on the bill.
        return True

    return session_type.motogp_session_name in motogp_sessions


def get_active_rounds(calendar: dict) -> List[dict]:
    """Rounds that are still scheduled, in championship order."""
    if not calendar:
        return []
    return [r for r in calendar.get("rounds", []) if is_round_active(r)]


def validate_calendar(calendar: dict) -> List[str]:
    """
    Sanity-check a calendar file. Returns a list of human readable problems.

    Catches the mistakes that are easy to make when hand editing a season after
    a cancellation or reschedule: duplicate or gappy round numbers, folders that
    disagree with the round number, bad dates, missing assets.
    """
    problems: List[str] = []
    if not calendar:
        return ["Calendar is empty or could not be parsed"]

    rounds = calendar.get("rounds", [])
    if not rounds:
        return ["Calendar has no rounds"]

    seen_numbers = set()
    seen_folders = set()
    previous_date = None

    for entry in rounds:
        round_num = entry.get("round")
        venue = entry.get("venue", "?")

        if round_num is None:
            problems.append(f"{venue}: missing 'round'")
            continue

        if round_num in seen_numbers:
            problems.append(f"Duplicate round number {round_num} ({venue})")
        seen_numbers.add(round_num)

        # Round numbers must be unique, but gaps are legal: a cancelled round
        # is normally removed from the calendar, and MotoGP has historically
        # renumbered, so we only warn about the pattern, never fail on it.

        folder = entry.get("folder", "")
        if folder in seen_folders:
            problems.append(f"{venue}: duplicate folder '{folder}'")
        seen_folders.add(folder)

        # The number in the folder must agree with the round. Padding is not
        # enforced here: legacy calendars use "Round 5 Spain" and renaming a
        # finished season's folders would churn the Plex library for nothing.
        if folder:
            match = re.match(r"Round\s*(\d+)\b", folder)
            if not match:
                problems.append(f"{venue}: folder '{folder}' is not 'Round <n> <venue>'")
            elif int(match.group(1)) != round_num:
                problems.append(
                    f"{venue}: folder '{folder}' says round {int(match.group(1))} "
                    f"but the entry is round {round_num}"
                )
            else:
                # Number agrees, so the venue in the folder should too. This is
                # what catches "Round 20 Malaysia" left behind after a round
                # moved: the folder belongs to a different race.
                rest = folder[match.end():].strip()
                if rest and rest.lower() != venue.lower():
                    problems.append(
                        f"{venue}: folder '{folder}' names '{rest}' instead of "
                        f"the venue '{venue}'"
                    )

        date = entry.get("date")
        if not date:
            problems.append(f"{venue}: missing 'date'")
        else:
            try:
                parsed = datetime.strptime(date, "%Y-%m-%d").date()
                if previous_date and parsed < previous_date:
                    problems.append(
                        f"{venue}: date {date} is earlier than the previous round"
                    )
                previous_date = parsed
            except (ValueError, TypeError):
                problems.append(f"{venue}: invalid 'date' {date!r}")

        if not entry.get("sessions") and has_session_times(calendar):
            problems.append(f"{venue}: no 'sessions' block (grabber cannot schedule)")

        if get_round_status(entry) in INACTIVE_ROUND_STATUSES:
            continue

    return problems


# =============================================================================
# BROADCASTER PREFERENCES
# =============================================================================

PREFERRED_BROADCASTERS = {
    "tnt": 40, #was 25
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
        allowed_groups = ["VERUM", "MWR", "BILLIE"]
    
    venue = round_info.get("venue", "").lower()
    aliases = [a.lower() for a in round_info.get("aliases", [])]
    all_locations = [venue] + aliases
    round_num = round_info.get("round", 0)
    
    target_class = target_session.race_class
    target_session_name = target_session.display_name
    
    scored = []
    
    # Terms to exclude globally
    excluded_terms = [
        "full event", "full.event", "fullevent",
        "weekend", "race weekend",
        "complete weekend", "complete.weekend",
        "all sessions", "all.sessions",
    ]
    
    # Get logger
    logger = logging.getLogger("motogp.filter")
    
    logger.debug(f"=" * 60)
    logger.debug(f"FILTER_RELEASES DEBUG")
    logger.debug(f"=" * 60)
    logger.debug(f"Target session: {target_session.full_display_name}")
    logger.debug(f"Target class: {target_class.display_name if target_class else 'None'}")
    logger.debug(f"Target session name: {target_session_name}")
    logger.debug(f"Season: {season}")
    logger.debug(f"Round: {round_num}")
    logger.debug(f"Venue: {venue}")
    logger.debug(f"All locations: {all_locations}")
    logger.debug(f"Required quality: {required_quality}")
    logger.debug(f"Allowed groups: {allowed_groups}")
    logger.debug(f"Total candidates: {len(releases)}")
    logger.debug(f"-" * 60)
    
    for idx, release in enumerate(releases):
        title = release.get('title', '')
        title_lower = title.lower()
        title_normalized = title_lower.replace('.', ' ').replace('-', ' ')
        
        logger.debug(f"")
        logger.debug(f"[{idx+1}/{len(releases)}] Evaluating: {title}")
        
        # GLOBAL EXCLUSIONS
        excluded_match = None
        for term in excluded_terms:
            if term in title_normalized:
                excluded_match = term
                break
        
        if excluded_match:
            logger.debug(f"  ❌ REJECTED: Contains excluded term '{excluded_match}'")
            continue
        
        # Must match class
        detected_class = detect_race_class(title)
        logger.debug(f"  Detected class: {detected_class.display_name}")
        
        if detected_class != target_class:
            logger.debug(f"  ❌ REJECTED: Class mismatch (want {target_class.display_name}, got {detected_class.display_name})")
            continue
        
        logger.debug(f"  ✓ Class matches: {detected_class.display_name}")
        
        # Must match location or round number
        location_match = False
        matched_location = None
        for loc in all_locations:
            if loc in title_lower:
                location_match = True
                matched_location = loc
                break
        
        round_match = False
        if not location_match:
            round_patterns = [
                f"round{round_num:02d}",
                f"round{round_num}",
                f"round.{round_num:02d}",
                f"round.{round_num}",
                f"round {round_num}",
                f"r{round_num:02d}",
                f"r{round_num}",
            ]
            for p in round_patterns:
                if p in title_lower or p in title_normalized:
                    round_match = True
                    logger.debug(f"  ✓ Round pattern matched: '{p}'")
                    break
        
        if location_match:
            logger.debug(f"  ✓ Location matched: '{matched_location}'")
        elif round_match:
            logger.debug(f"  ✓ Round number matched")
        else:
            logger.debug(f"  ❌ REJECTED: No location/round match")
            logger.debug(f"     Title lower: {title_lower}")
            logger.debug(f"     Looking for locations: {all_locations}")
            logger.debug(f"     Looking for round patterns: round{round_num}, r{round_num}, etc.")
            continue
        
        # Must have required quality
        quality_base = required_quality.lower().rstrip('pi')
        if quality_base not in title_lower:
            logger.debug(f"  ❌ REJECTED: Missing quality '{quality_base}'")
            continue
        
        logger.debug(f"  ✓ Quality matched: {quality_base}")
        
        # Must be from allowed release group
        group_match = False
        matched_group = None
        for g in allowed_groups:
            if g.lower() in title_lower:
                group_match = True
                matched_group = g
                break
        
        if not group_match:
            logger.debug(f"  ❌ REJECTED: No allowed group found (looking for {allowed_groups})")
            continue
        
        logger.debug(f"  ✓ Group matched: {matched_group}")
        
        # Must match year
        if str(season) not in title:
            logger.debug(f"  ❌ REJECTED: Year {season} not found in title")
            continue
        
        logger.debug(f"  ✓ Year matched: {season}")
        
        # SESSION MATCHING
        detected_session = classify_session(title, detected_class)
        logger.debug(f"  Detected session: {detected_session.display_name} (full: {detected_session.full_display_name})")
        
        session_match = False
        is_combined_quali = False
        
        if detected_session == target_session:
            session_match = True
            logger.debug(f"  ✓ Session exact match")
        elif target_session_name in ["Qualifying 1", "Qualifying 2"]:
            target_is_q1 = target_session_name == "Qualifying 1"
            target_is_q2 = target_session_name == "Qualifying 2"
            
            has_q1 = matches_qualifying_part(title, 1)
            has_q2 = matches_qualifying_part(title, 2)
            
            if target_is_q1 and has_q1:
                session_match = True
                logger.debug(f"  ✓ Q1 pattern matched")
            elif target_is_q2 and has_q2:
                session_match = True
                logger.debug(f"  ✓ Q2 pattern matched")
            elif not has_q1 and not has_q2:
                if "qualifying" in title_normalized or "qualifing" in title_normalized:
                    session_match = True
                    is_combined_quali = True
                    release['_combined_quali'] = True
                    logger.debug(f"  ✓ Combined qualifying matched")
        else:
            # For non-qualifying sessions, check if detected matches target
            logger.debug(f"  Comparing: detected={detected_session.name} vs target={target_session.name}")
        
        if not session_match:
            logger.debug(f"  ❌ REJECTED: Session mismatch")
            logger.debug(f"     Want: {target_session.full_display_name} ({target_session.name})")
            logger.debug(f"     Got:  {detected_session.full_display_name} ({detected_session.name})")
            continue
        
        # PASSED ALL FILTERS!
        logger.debug(f"  ✅ ACCEPTED!")
        
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
            score += 40
            release['_group'] = 'MWR'
        elif "billie" in title_lower:
            score += 40
            release['_group'] = 'BILLIE'
        
        if "1080p" in title_lower:
            score += 10
        elif "2160p" in title_lower:
            score += 5
        
        release['_score'] = score
        release['_broadcaster_score'] = broadcaster_score
        scored.append(release)
        
        logger.debug(f"  Score: {score}")
    
    logger.debug(f"")
    logger.debug(f"=" * 60)
    logger.debug(f"FILTER SUMMARY: {len(scored)}/{len(releases)} releases passed")
    logger.debug(f"=" * 60)
    
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