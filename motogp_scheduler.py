#!/usr/bin/env python3
"""
MotoGP Scheduler - Always-On Session Monitor with Download Watcher
====================================================================

Runs continuously inside a Docker container:
1. Monitors for race weekends and triggers grabber for missing sessions
2. Watches download folder for completed downloads and triggers sorter

Usage:
    python3 motogp_scheduler.py
    python3 motogp_scheduler.py --no-watcher   # Disable download watcher
    python3 motogp_scheduler.py --watcher-only # Only run watcher
"""

import os
import sys
import time
import logging
import signal
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Set

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from motogp_common import load_calendar, has_session_times, get_config_dir
from motogp_watcher import create_watcher

from utils.config_loader import (
    get_motogp_library_path,
    get_downloads_path,
    get_current_season,
    get_search_delay_hours,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    "race_weekend_interval_minutes": 60,
    "off_weekend_interval_minutes": 360,
    "post_race_check_days": 2,
    "quiet_hours": {
        "enabled": True,
        "start": 1,
        "end": 6,
        "interval_minutes": 120,
    },
    "grabber_script": SCRIPT_DIR / "motogp_grabber.py",
    "sort_script": SCRIPT_DIR / "motogp_sort.py",
    "python_path": "/opt/venv/bin/python3",
    "log_level": "INFO",
}

CONFIG_DIR = get_config_dir()

# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logging():
    """Configure logging."""
    log_dir = os.path.join(get_motogp_library_path(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    log_file = os.path.join(log_dir, "scheduler.log")
    
    # Rotate log if too large
    if os.path.exists(log_file) and os.path.getsize(log_file) > 10 * 1024 * 1024:
        backup = log_file + ".old"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(log_file, backup)
    
    log_level = getattr(logging, CONFIG["log_level"], logging.INFO)
    
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    return log_file

# =============================================================================
# DOWNLOAD PROCESSOR
# =============================================================================

def process_download(folder_path: str) -> bool:
    """
    Process a completed download folder.
    
    Called by the watcher when a new download is ready.
    """
    logging.info(f"📂 Processing download: {os.path.basename(folder_path)}")
    
    try:
        result = subprocess.run(
            [
                CONFIG["python_path"],
                str(CONFIG["sort_script"]),
                "--source", folder_path
            ],
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )
        
        if result.returncode == 0:
            logging.info(f"✅ Sort completed: {os.path.basename(folder_path)}")
            return True
        else:
            logging.error(f"❌ Sort failed: {result.stderr[:500] if result.stderr else 'Unknown error'}")
            return False
    
    except subprocess.TimeoutExpired:
        logging.error(f"❌ Sort timed out for: {folder_path}")
        return False
    except Exception as e:
        logging.error(f"❌ Error processing download: {e}")
        return False

# =============================================================================
# RACE WEEKEND DETECTION
# =============================================================================

def get_current_race_weekend(calendar: dict) -> Optional[dict]:
    """Check if we're currently in a race weekend window."""
    now = datetime.now()
    today = now.date()
    
    for round_info in calendar.get("rounds", []):
        race_date_str = round_info.get("date")
        weekend_start_str = round_info.get("weekend_start")
        
        if not race_date_str:
            continue
        
        try:
            race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            
            if weekend_start_str:
                weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            else:
                weekend_start = race_date - timedelta(days=2)
            
            window_start = weekend_start - timedelta(days=1)
            window_end = race_date + timedelta(days=CONFIG["post_race_check_days"])
            
            if window_start <= today <= window_end:
                return {
                    "round": round_info.get("round"),
                    "venue": round_info.get("venue"),
                    "weekend_start": weekend_start,
                    "race_date": race_date,
                    "window_start": window_start,
                    "window_end": window_end,
                    "round_info": round_info,
                }
        
        except ValueError:
            continue
    
    return None

def get_next_race_weekend(calendar: dict) -> Optional[dict]:
    """Get the next upcoming race weekend."""
    now = datetime.now()
    today = now.date()
    
    next_round = None
    next_date = None
    
    for round_info in calendar.get("rounds", []):
        race_date_str = round_info.get("date")
        weekend_start_str = round_info.get("weekend_start")
        
        if not race_date_str:
            continue
        
        try:
            race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            
            if weekend_start_str:
                weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
            else:
                weekend_start = race_date - timedelta(days=2)
            
            window_start = weekend_start - timedelta(days=1)
            
            if window_start > today:
                if next_date is None or window_start < next_date:
                    next_date = window_start
                    next_round = {
                        "round": round_info.get("round"),
                        "venue": round_info.get("venue"),
                        "weekend_start": weekend_start,
                        "race_date": race_date,
                        "window_start": window_start,
                    }
        
        except ValueError:
            continue
    
    return next_round

# =============================================================================
# SESSION STATUS
# =============================================================================

def has_sessions_ended(round_info: dict, search_delay_hours: int) -> bool:
    """Check if any sessions have ended (plus delay) for this round."""
    from motogp_common import DEFAULT_DESIRED_SESSIONS, SESSION_TO_CALENDAR_KEY, CALENDAR_KEY_TO_DAY
    
    now = datetime.now()
    sessions = round_info.get("sessions", {})
    
    if not sessions:
        weekend_start_str = round_info.get("weekend_start")
        if weekend_start_str:
            weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d")
            first_searchable = weekend_start.replace(hour=14)
            return now >= first_searchable
        return False
    
    weekend_start = round_info.get("weekend_start")
    race_date = round_info.get("date")
    
    if not weekend_start or not race_date:
        return False
    
    try:
        weekend_start_dt = datetime.strptime(weekend_start, "%Y-%m-%d")
        race_date_dt = datetime.strptime(race_date, "%Y-%m-%d")
    except ValueError:
        return False
    
    for session_type, wanted in DEFAULT_DESIRED_SESSIONS.items():
        if not wanted:
            continue
        
        calendar_key = SESSION_TO_CALENDAR_KEY.get(session_type)
        if not calendar_key:
            continue
        
        day = CALENDAR_KEY_TO_DAY.get(calendar_key)
        if not day:
            continue
        
        day_sessions = sessions.get(day, {})
        session_info = day_sessions.get(calendar_key)
        if not session_info:
            continue
        
        gmt_time = session_info.get("gmt")
        if not gmt_time:
            continue
        
        try:
            if day == "friday":
                session_date = weekend_start_dt
            elif day == "saturday":
                session_date = weekend_start_dt + timedelta(days=1)
            elif day == "sunday":
                session_date = race_date_dt
            else:
                continue
            
            gmt_day = session_info.get("gmt_day")
            if gmt_day:
                day_offsets = {
                    "thursday": -1 if day == "friday" else 0,
                    "friday": -1 if day == "saturday" else 0,
                    "saturday": -1 if day == "sunday" else 0,
                }
                session_date += timedelta(days=day_offsets.get(gmt_day, 0))
            
            hour, minute = map(int, gmt_time.split(":"))
            session_start = session_date.replace(hour=hour, minute=minute)
            
            duration = session_info.get("duration_mins", 45)
            session_end = session_start + timedelta(minutes=duration)
            search_after = session_end + timedelta(hours=search_delay_hours)
            
            if now >= search_after:
                return True
        
        except Exception:
            continue
    
    return False

# =============================================================================
# GRABBER EXECUTION
# =============================================================================

def run_grabber_scheduled() -> dict:
    """Run the grabber in scheduled mode."""
    logging.info("🚀 Running grabber in scheduled mode...")
    
    try:
        result = subprocess.run(
            [
                CONFIG["python_path"],
                str(CONFIG["grabber_script"]),
                "scheduled"
            ],
            capture_output=True,
            text=True,
            timeout=1800
        )
        
        grabbed_count = 0
        
        for line in result.stdout.split('\n'):
            if 'Releases grabbed:' in line or 'Grabbed:' in line:
                try:
                    grabbed_count = int(line.split(':')[-1].strip())
                except ValueError:
                    pass
        
        if result.returncode == 0:
            logging.info(f"✅ Grabber completed: {grabbed_count} grabbed")
            return {"success": True, "grabbed": grabbed_count}
        else:
            logging.error(f"❌ Grabber failed with code {result.returncode}")
            return {"success": False, "grabbed": 0}
    
    except subprocess.TimeoutExpired:
        logging.error("❌ Grabber timed out")
        return {"success": False, "grabbed": 0}
    except Exception as e:
        logging.error(f"❌ Failed to run grabber: {e}")
        return {"success": False, "grabbed": 0}

# =============================================================================
# SCHEDULER STATE
# =============================================================================

class SchedulerState:
    """Track scheduler state."""
    
    def __init__(self):
        self.running = True
        self.last_check: Optional[datetime] = None
        self.last_grabber_run: Optional[datetime] = None
        self.current_weekend: Optional[dict] = None
        self.consecutive_no_grabs = 0
        self.total_grabbed_this_weekend = 0
    
    def reset_for_new_weekend(self, weekend: dict):
        """Reset state for a new race weekend."""
        if self.current_weekend and self.current_weekend.get("round") != weekend.get("round"):
            logging.info(f"🏁 New race weekend: Round {weekend['round']} {weekend['venue']}")
            self.consecutive_no_grabs = 0
            self.total_grabbed_this_weekend = 0
        self.current_weekend = weekend

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def format_timedelta(td: timedelta) -> str:
    """Format timedelta as human readable string."""
    total_seconds = int(td.total_seconds())
    
    if total_seconds < 0:
        return "now"
    
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    
    if days > 0:
        return f"{days}d {hours}h"
    elif hours > 0:
        return f"{hours}h {minutes}m"
    else:
        return f"{minutes}m"

def is_quiet_hours() -> bool:
    """Check if we're in quiet hours."""
    if not CONFIG["quiet_hours"]["enabled"]:
        return False
    
    hour = datetime.now().hour
    start = CONFIG["quiet_hours"]["start"]
    end = CONFIG["quiet_hours"]["end"]
    
    if start < end:
        return start <= hour < end
    else:
        return hour >= start or hour < end

def get_sleep_interval(in_race_weekend: bool) -> int:
    """Get sleep interval in seconds."""
    if is_quiet_hours():
        return CONFIG["quiet_hours"]["interval_minutes"] * 60
    
    if in_race_weekend:
        return CONFIG["race_weekend_interval_minutes"] * 60
    else:
        return CONFIG["off_weekend_interval_minutes"] * 60

# =============================================================================
# MAIN SCHEDULER LOOP
# =============================================================================

def main():
    """Main scheduler loop."""
    import argparse
    
    parser = argparse.ArgumentParser(description="MotoGP Scheduler")
    parser.add_argument("--no-watcher", action="store_true", help="Disable download watcher")
    parser.add_argument("--watcher-only", action="store_true", help="Only run watcher")
    args = parser.parse_args()
    
    log_file = setup_logging()
    
    state = SchedulerState()
    watcher = None
    
    # Handle graceful shutdown
    def signal_handler(signum, frame):
        logging.info("🛑 Shutdown signal received")
        state.running = False
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Startup banner
    season = get_current_season()
    
    logging.info("=" * 60)
    logging.info("  MOTOGPARR SCHEDULER STARTED")
    logging.info("=" * 60)
    logging.info(f"  Season:              {season}")
    logging.info(f"  Race weekend check:  Every {CONFIG['race_weekend_interval_minutes']} minutes")
    logging.info(f"  Off-weekend check:   Every {CONFIG['off_weekend_interval_minutes']} minutes")
    logging.info(f"  Search delay:        {get_search_delay_hours()} hours")
    logging.info(f"  Log file:            {log_file}")
    
    # Start watcher
    if not args.no_watcher:
        downloads_path = get_downloads_path()
        logging.info(f"  Watch directory:     {downloads_path}")
        
        watcher = create_watcher(
            watch_dir=downloads_path,
            processor=process_download,
            config_dir=str(CONFIG_DIR)
        )
        watcher.start()
        logging.info("  👁️ Download watcher: Started")
    else:
        logging.info("  👁️ Download watcher: Disabled")
    
    logging.info("=" * 60)
    
    # Watcher-only mode
    if args.watcher_only:
        logging.info("Running in watcher-only mode")
        try:
            while state.running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            if watcher:
                watcher.stop()
        return 0
    
    # Load calendar
    calendar = load_calendar(season)
    if not calendar:
        logging.error(f"❌ Could not load calendar for {season}")
        if watcher:
            watcher.stop()
        return 1
    
    logging.info(f"📅 Loaded calendar: {len(calendar.get('rounds', []))} rounds")
    
    # Check current state
    current_weekend = get_current_race_weekend(calendar)
    if current_weekend:
        logging.info(f"🏁 Currently in race weekend: Round {current_weekend['round']} {current_weekend['venue']}")
        state.reset_for_new_weekend(current_weekend)
    else:
        next_weekend = get_next_race_weekend(calendar)
        if next_weekend:
            until = datetime.combine(next_weekend['window_start'], datetime.min.time()) - datetime.now()
            logging.info(f"⏳ Next race weekend: Round {next_weekend['round']} {next_weekend['venue']} in {format_timedelta(until)}")
    
    # Main loop
    while state.running:
        try:
            now = datetime.now()
            state.last_check = now
            
            # Reload calendar
            calendar = load_calendar(season)
            if not calendar:
                time.sleep(60)
                continue
            
            # Check if we're in a race weekend
            current_weekend = get_current_race_weekend(calendar)
            in_race_weekend = current_weekend is not None
            
            if in_race_weekend:
                state.reset_for_new_weekend(current_weekend)
                round_info = current_weekend["round_info"]
                
                if has_sessions_ended(round_info, get_search_delay_hours()):
                    logging.info(f"🔍 Checking for new releases (Round {current_weekend['round']} {current_weekend['venue']})")
                    
                    result = run_grabber_scheduled()
                    state.last_grabber_run = datetime.now()
                    
                    if result["grabbed"] > 0:
                        state.total_grabbed_this_weekend += result["grabbed"]
                        state.consecutive_no_grabs = 0
                    else:
                        state.consecutive_no_grabs += 1
            else:
                next_weekend = get_next_race_weekend(calendar)
                if next_weekend:
                    until = datetime.combine(next_weekend['window_start'], datetime.min.time()) - now
                    if until.total_seconds() > 0:
                        logging.info(f"💤 Off-weekend. Next: Round {next_weekend['round']} in {format_timedelta(until)}")
            
            # Sleep
            sleep_seconds = get_sleep_interval(in_race_weekend)
            
            sleep_chunks = sleep_seconds // 30
            for i in range(sleep_chunks):
                if not state.running:
                    break
                time.sleep(30)
            
            remainder = sleep_seconds % 30
            if remainder > 0 and state.running:
                time.sleep(remainder)
        
        except KeyboardInterrupt:
            state.running = False
        except Exception as e:
            logging.error(f"❌ Error in scheduler loop: {e}", exc_info=True)
            time.sleep(60)
    
    # Cleanup
    if watcher:
        watcher.stop()
    
    logging.info("🛑 Scheduler stopped gracefully")
    return 0


if __name__ == "__main__":
    sys.exit(main())