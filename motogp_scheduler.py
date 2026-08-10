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
    python3 motogp_scheduler.py --debug        # Enable debug logging
"""

import os
import sys
import time
import signal
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Set

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Import centralized logging FIRST
from motogp_common import setup_logging, get_logger, load_calendar, has_session_times, get_config_dir
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
}

CONFIG_DIR = get_config_dir()

# =============================================================================
# DOWNLOAD PROCESSOR
# =============================================================================

def process_download(folder_path: str) -> bool:
    """
    Process a completed download folder.
    
    Called by the watcher when a new download is ready.
    """
    logger = get_logger("scheduler")
    logger.info(f"📂 Processing download: {os.path.basename(folder_path)}")
    
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
            logger.info(f"✅ Sort completed: {os.path.basename(folder_path)}")
            return True
        else:
            logger.error(f"❌ Sort failed (exit code {result.returncode})")
            if result.stderr:
                logger.error(f"   Error: {result.stderr[:500]}")
            if result.stdout:
                logger.debug(f"   Output: {result.stdout[-500:]}")
            return False
    
    except subprocess.TimeoutExpired:
        logger.error(f"❌ Sort timed out for: {folder_path}")
        return False
    except Exception as e:
        logger.error(f"❌ Error processing download: {e}")
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
# GRABBER EXECUTION
# =============================================================================

def run_grabber_scheduled() -> dict:
    """Run the grabber in scheduled mode."""
    logger = get_logger("scheduler")
    logger.info("🚀 Running grabber in scheduled mode...")
    
    # Build command with search delay from config
    cmd = [
        CONFIG["python_path"],
        str(CONFIG["grabber_script"]),
        "scheduled"
    ]
    
    # Add search delay from config
    search_delay = get_search_delay_hours()
    if search_delay is not None:
        cmd.extend(["--search-delay", str(search_delay)])
        logger.debug(f"Using search delay: {search_delay} hours")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800
        )
        
        grabbed_count = 0
        
        # Log the grabber output for visibility
        for line in result.stdout.split('\n'):
            line = line.strip()
            if not line:
                continue
            # Log important lines from grabber
            if any(marker in line for marker in ['✅', '❌', '📅', '📊', '🔍', '📋', '🎯', 'READY', 'Outside window', 'within active window']):
                logger.info(f"  {line}")
            elif 'Releases grabbed:' in line or 'Grabbed:' in line:
                logger.info(f"  {line}")
                try:
                    grabbed_count = int(line.split(':')[-1].strip())
                except ValueError:
                    pass
        
        if result.returncode == 0:
            logger.info(f"✅ Grabber completed: {grabbed_count} grabbed")
            return {"success": True, "grabbed": grabbed_count}
        else:
            logger.error(f"❌ Grabber failed with code {result.returncode}")
            if result.stderr:
                logger.error(f"   Error: {result.stderr[:500]}")
            return {"success": False, "grabbed": 0}
    
    except subprocess.TimeoutExpired:
        logger.error("❌ Grabber timed out")
        return {"success": False, "grabbed": 0}
    except Exception as e:
        logger.error(f"❌ Failed to run grabber: {e}")
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
        logger = get_logger("scheduler")
        if self.current_weekend and self.current_weekend.get("round") != weekend.get("round"):
            logger.info(f"🏁 New race weekend: Round {weekend['round']} {weekend['venue']}")
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
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(debug_mode=args.debug)
    logger = get_logger("scheduler")
    
    state = SchedulerState()
    watcher = None
    
    # Handle graceful shutdown
    def signal_handler(signum, frame):
        logger.info("🛑 Shutdown signal received")
        state.running = False
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Startup banner
    season = get_current_season()
    
    logger.info("=" * 60)
    logger.info("  MOTOGPARR SCHEDULER")
    logger.info("=" * 60)
    logger.info(f"  Season:              {season}")
    logger.info(f"  Race weekend check:  Every {CONFIG['race_weekend_interval_minutes']} minutes")
    logger.info(f"  Off-weekend check:   Every {CONFIG['off_weekend_interval_minutes']} minutes")
    logger.info(f"  Search delay:        {get_search_delay_hours()} hours")
    logger.info(f"  Watch directory:     {get_downloads_path()}")
    logger.info(f"  Library path:        {get_motogp_library_path()}")
    logger.info("=" * 60)
    
    # Start watcher
    if not args.no_watcher:
        logger.info("📂 Initializing download watcher...")
        downloads_path = get_downloads_path()
        
        watcher = create_watcher(
            watch_dir=downloads_path,
            processor=process_download,
            config_dir=str(CONFIG_DIR)
        )
        watcher.start()
        logger.info(f"👁️ Watching: {downloads_path}")
    else:
        logger.info("👁️ Download watcher: Disabled")
    
    # Watcher-only mode
    if args.watcher_only:
        logger.info("Running in watcher-only mode")
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
        logger.error(f"❌ Could not load calendar for {season}")
        if watcher:
            watcher.stop()
        return 1
    
    if not has_session_times(calendar):
        logger.warning(f"⚠️ Calendar doesn't have detailed session times")
    
    logger.info(f"📅 Loaded calendar: {len(calendar.get('rounds', []))} rounds")
    
    # Check current state
    current_weekend = get_current_race_weekend(calendar)
    if current_weekend:
        logger.info(f"🏁 Currently in race weekend: Round {current_weekend['round']} {current_weekend['venue']}")
        state.reset_for_new_weekend(current_weekend)
    else:
        next_weekend = get_next_race_weekend(calendar)
        if next_weekend:
            until = datetime.combine(next_weekend['window_start'], datetime.min.time()) - datetime.now()
            logger.info(f"⏳ Next race weekend: Round {next_weekend['round']} {next_weekend['venue']} in {format_timedelta(until)}")
    
    # Main loop
    while state.running:
        try:
            now = datetime.now()
            state.last_check = now
            
            # Reload calendar periodically
            calendar = load_calendar(season) or calendar
            
            # Check if we're in a race weekend
            current_weekend = get_current_race_weekend(calendar)
            in_race_weekend = current_weekend is not None
            
            if in_race_weekend:
                state.reset_for_new_weekend(current_weekend)
                
                # Always run grabber during race weekend - let it determine what's ready
                if has_session_times(calendar):
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
                        logger.info(f"💤 Next: Round {next_weekend['round']} {next_weekend['venue']} in {format_timedelta(until)}")
            
            # Sleep
            sleep_seconds = get_sleep_interval(in_race_weekend)
            
            # Sleep in chunks for responsive shutdown
            for _ in range(sleep_seconds // 30):
                if not state.running:
                    break
                time.sleep(30)
            
            if state.running:
                time.sleep(sleep_seconds % 30)
        
        except KeyboardInterrupt:
            state.running = False
        except Exception as e:
            logger.error(f"❌ Error in scheduler loop: {e}", exc_info=True)
            time.sleep(60)
    
    # Cleanup
    if watcher:
        watcher.stop()
    
    logger.info("🛑 Scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())