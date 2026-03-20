#!/usr/bin/env python3
"""
MotoGP Grabber Web UI
======================
Web interface for MotoGP grabber - refactored with templates.
"""

import os
import sys
import subprocess
import threading
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from flask import (
    Flask, render_template, request, jsonify, 
    send_from_directory, Response
)

# Add paths for imports
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from motogp_common import (
    SessionType,
    parse_prefix,
    load_calendar,
    DEFAULT_DESIRED_SESSIONS,
    get_config_dir,
)

from utils.config_loader import (
    get_motogp_library_path,
    validate,
    reload_settings,
    save_settings,
    _load_yaml,
    get_prowlarr_url,
    get_prowlarr_api_key,
    get_sabnzbd_url,
    get_sabnzbd_api_key,
    get_plex_url,
    get_plex_token,
)

# =============================================================================
# CONFIGURATION
# =============================================================================

PYTHON_PATH = "/opt/venv/bin/python3"
GRABBER_SCRIPT = SCRIPT_DIR / "motogp_grabber.py"
OVERLAY_SCRIPT = SCRIPT_DIR / "motogp_overlay_utility.py"
CONFIG_DIR = get_config_dir()
WEBUI_CONFIG_FILE = CONFIG_DIR / "webui_settings.json"
LOG_FILE = CONFIG_DIR / "logs" / "motogp.log"
MOTOGP_LIBRARY_PATH = get_motogp_library_path()

SEASONS = [2024, 2025, 2026]

SESSION_GROUPS = {
    "MotoGP": [
        "MOTOGP_PRESS_CONFERENCE", "MOTOGP_FP1", "MOTOGP_PRACTICE",
        "MOTOGP_FP2", "MOTOGP_Q1", "MOTOGP_Q2", "MOTOGP_SPRINT",
        "MOTOGP_WARMUP", "MOTOGP_RACE", "MOTOGP_CHEQUERED_FLAG",
    ],
    "Moto2": [
        "MOTO2_FP1", "MOTO2_PRACTICE", "MOTO2_FP2",
        "MOTO2_Q1", "MOTO2_Q2", "MOTO2_RACE",
    ],
    "Moto3": [
        "MOTO3_FP1", "MOTO3_PRACTICE", "MOTO3_FP2",
        "MOTO3_Q1", "MOTO3_Q2", "MOTO3_RACE",
    ],
}

# Build SESSION_TYPES from SessionType enum
SESSION_TYPES = {}
for session_type in SessionType:
    if session_type == SessionType.UNKNOWN:
        continue
    SESSION_TYPES[session_type.name] = {
        "name": session_type.full_display_name,
        "default": DEFAULT_DESIRED_SESSIONS.get(session_type, False)
    }

from grab_stats import get_stats_summary
from activity_emitter import get_all_activity, clear_all_activity

app = Flask(__name__)
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response
# Store command output
command_output = {
    "running": False,
    "command": "",
    "output": "",
    "timestamp": None,
    "batch_total": 0,
    "batch_current": 0
}


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def load_calendar_data(year: int) -> dict:
    """Load calendar for a given year."""
    return load_calendar(year) or {"rounds": []}


def get_rounds_for_season(year: int) -> list:
    """Get list of rounds with venue names for a season."""
    calendar = load_calendar_data(year)
    return [
        {"number": r.get("round"), "venue": r.get("venue"), "date": r.get("date")}
        for r in calendar.get("rounds", [])
    ]


def get_next_race_info() -> dict:
    """Get information about the next race weekend."""
    today = datetime.now().date()
    
    for year in [today.year, today.year + 1]:
        calendar = load_calendar_data(year)
        
        for r in calendar.get("rounds", []):
            race_date_str = r.get("date")
            weekend_start_str = r.get("weekend_start")
            
            if not race_date_str:
                continue
            
            try:
                race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
                weekend_start = (
                    datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
                    if weekend_start_str
                    else race_date - timedelta(days=2)
                )
                weekend_end = race_date + timedelta(days=1)
                
                # Format date as DD-MM-YYYY
                formatted_date = race_date.strftime("%d-%m-%Y")
                
                if weekend_start <= today <= weekend_end:
                    return {
                        "round": r.get("round"),
                        "venue": r.get("venue"),
                        "date": formatted_date,
                        "flag": r.get("flag", ""),
                        "status": "now",
                        "countdown": "Race Weekend Now!",
                        "days": 0
                    }
                
                if weekend_start > today:
                    days_until = (weekend_start - today).days
                    
                    if days_until == 1:
                        countdown = "Tomorrow!"
                    elif days_until < 7:
                        countdown = f"{days_until} days"
                    elif days_until < 14:
                        countdown = f"{days_until} days (next week)"
                    else:
                        weeks = days_until // 7
                        countdown = f"{days_until} days (~{weeks} weeks)"
                    
                    return {
                        "round": r.get("round"),
                        "venue": r.get("venue"),
                        "date": formatted_date,
                        "flag": r.get("flag", ""),
                        "status": "upcoming",
                        "countdown": countdown,
                        "days": days_until
                    }
            except ValueError:
                continue
    
    return {
        "round": None,
        "venue": "No upcoming races",
        "date": None,
        "flag": "",
        "status": "none",
        "countdown": "Season ended",
        "days": None
    }



def load_webui_settings():
    """Load WebUI settings from config file."""
    defaults = {
        "max_grabs_per_run": 22,
        "sessions": {k: v["default"] for k, v in SESSION_TYPES.items()}
    }
    
    if WEBUI_CONFIG_FILE.exists():
        try:
            with open(WEBUI_CONFIG_FILE, 'r') as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
    
    return defaults


def save_webui_settings(settings):
    """Save WebUI settings to config file."""
    WEBUI_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WEBUI_CONFIG_FILE, 'w') as f:
        json.dump(settings, f, indent=2)


def update_settings_yml(settings):
    """Update settings.yml with max_grabs_per_run."""
    try:
        current = _load_yaml()
        if "grabber" not in current:
            current["grabber"] = {}
        current["grabber"]["max_grabs_per_run"] = settings.get("max_grabs_per_run", 5)
        save_settings(current)
        reload_settings()
        return True
    except Exception as e:
        print(f"Error updating settings.yml: {e}")
        return False


# =============================================================================
# LIBRARY SCANNING
# =============================================================================

def scan_library_for_season(year: int) -> dict:
    """Scan the library to find existing sessions for a season."""
    season_path = os.path.join(MOTOGP_LIBRARY_PATH, str(year))
    
    if not os.path.exists(season_path):
        return {}
    
    existing = {}
    
    for round_folder in os.listdir(season_path):
        round_path = os.path.join(season_path, round_folder)
        
        if not os.path.isdir(round_path):
            continue
        
        round_match = re.match(r'Round\s*(\d+)', round_folder)
        if not round_match:
            continue
        
        round_num = int(round_match.group(1))
        existing[round_num] = {}
        
        for session_folder in os.listdir(round_path):
            session_path = os.path.join(round_path, session_folder)
            
            if not os.path.isdir(session_path):
                continue
            
            parsed = parse_prefix(session_folder)
            if parsed:
                folder_year, folder_round, session_type = parsed
                if folder_year == year and folder_round == round_num:
                    existing[round_num][session_type] = session_path
    
    return existing


def get_library_status(year: int, round_filter: int = None) -> dict:
    """Get library status for display."""
    calendar = load_calendar_data(year)
    existing = scan_library_for_season(year)
    settings = load_webui_settings()
    
    # Build desired sessions set from settings
    desired_set = set()
    for key, enabled in settings.get("sessions", {}).items():
        if enabled:
            try:
                desired_set.add(SessionType[key])
            except KeyError:
                pass
    
    if not desired_set:
        desired_set = {st for st, enabled in DEFAULT_DESIRED_SESSIONS.items() if enabled}
    
    result = {
        "rounds": [], 
        "total_grabbed": 0, 
        "total_desired": 0,
        "past_rounds_grabbed": 0,
        "past_rounds_desired": 0,
        "total_in_library": 0
    }
    today = datetime.now().date()
    
    # Count total files in library (all sessions, not just desired)
    for round_num, sessions in existing.items():
        result["total_in_library"] += len(sessions)
    
    for r in calendar.get("rounds", []):
        round_num = r.get("round")
        
        if round_filter and round_num != round_filter:
            continue
        
        # Check if future round (don't skip, just flag it)
        race_date_str = r.get("date", "")
        is_future = False
        if race_date_str:
            try:
                is_future = datetime.strptime(race_date_str, "%Y-%m-%d").date() > today
            except ValueError:
                pass
        
        round_existing = existing.get(round_num, {})
        sessions = []
        grabbed_count = 0
        desired_count = 0
        
        for session_type in sorted(desired_set, key=lambda x: int(x)):
            exists = session_type in round_existing
            sessions.append({
                "type": session_type,
                "name": session_type.full_display_name,
                "exists": exists,
            })
            desired_count += 1
            if exists:
                grabbed_count += 1
        
        result["rounds"].append({
            "number": round_num,
            "venue": r.get("venue", "Unknown"),
            "date": r.get("date", ""),
            "is_future": is_future,
            "sessions": sessions,
            "grabbed_count": grabbed_count,
            "desired_count": desired_count,
        })
        
        result["total_grabbed"] += grabbed_count
        result["total_desired"] += desired_count
        
        # Only count past rounds for the "preferred" stats
        if not is_future:
            result["past_rounds_grabbed"] += grabbed_count
            result["past_rounds_desired"] += desired_count
    
    return result


# =============================================================================
# COMMAND RUNNERS
# =============================================================================

def run_grabber_command(args: str):
    """Run a grabber command in the background."""
    global command_output
    
    command_output["running"] = True
    command_output["command"] = f"motogp-grab {args}"
    command_output["output"] = "Starting...\n"
    command_output["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        cmd = [PYTHON_PATH, str(GRABBER_SCRIPT)] + args.split()
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        
        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            command_output["output"] = "".join(output_lines)
        
        process.wait()
        command_output["output"] = "".join(output_lines)
        
    except Exception as e:
        command_output["output"] += f"\nError: {e}"
    finally:
        command_output["running"] = False


def run_overlay_command(args: str):
    """Run an overlay command in the background."""
    global command_output
    
    command_output["running"] = True
    command_output["command"] = f"motogp-overlay {args}"
    command_output["output"] = "Starting overlay...\n"
    command_output["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        parts = args.split()
        
        if parts[0] == 'apply-season':
            year = parts[1]
            scale = "1.0"
            dry_run = False
            
            for i, p in enumerate(parts):
                if p == '--scale' and i + 1 < len(parts):
                    scale = parts[i + 1]
                if p == '--dry-run':
                    dry_run = True
            
            cmd = [PYTHON_PATH, str(OVERLAY_SCRIPT), "apply-season", year, "--scale", scale]
            if dry_run:
                cmd.append("--dry-run")
        
        elif parts[0] == 'apply-round':
            year = parts[1]
            round_num = parts[2]
            scale = "1.0"
            dry_run = False
            
            for i, p in enumerate(parts):
                if p == '--scale' and i + 1 < len(parts):
                    scale = parts[i + 1]
                if p == '--dry-run':
                    dry_run = True
            
            calendar = load_calendar_data(int(year))
            round_info = next(
                (r for r in calendar.get("rounds", []) if r.get("round") == int(round_num)),
                None
            )
            
            if round_info:
                venue = round_info.get("venue", "Unknown")
                round_path = os.path.join(MOTOGP_LIBRARY_PATH, year, f"Round {round_num} {venue}")
                cmd = [PYTHON_PATH, str(OVERLAY_SCRIPT), "apply-round", round_path, "--scale", scale]
                if dry_run:
                    cmd.append("--dry-run")
            else:
                command_output["output"] = f"Error: Round {round_num} not found in calendar"
                command_output["running"] = False
                return
        else:
            command_output["output"] = f"Error: Unknown overlay command: {parts[0]}"
            command_output["running"] = False
            return
        
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        
        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            command_output["output"] = "".join(output_lines)
        
        process.wait()
        command_output["output"] = "".join(output_lines)
        
    except Exception as e:
        command_output["output"] += f"\nError: {e}"
    finally:
        command_output["running"] = False


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/")
def index():
    settings = load_webui_settings()
    session_defaults = {k: v["default"] for k, v in SESSION_TYPES.items()}
    next_race = get_next_race_info()
    
    calendar_data = {str(year): get_rounds_for_season(year) for year in SEASONS}
    
    return render_template(
        "dashboard.html",
        status=command_output,
        seasons=SEASONS,
        settings=settings,
        session_types=SESSION_TYPES,
        session_groups=SESSION_GROUPS,
        session_defaults=session_defaults,
        next_race=next_race,
        calendar_data=calendar_data,
        active_tab="home"
    )


@app.route("/library")
def library():
    settings = load_webui_settings()
    next_race = get_next_race_info()
    calendar_data = {str(year): get_rounds_for_season(year) for year in SEASONS}
    
    # Build flag data per season
    flag_data = {}
    for year in SEASONS:
        flag_data[str(year)] = {}
        cal = load_calendar_data(year)
        for r in cal.get("rounds", []):
            flag_data[str(year)][r.get("round")] = r.get("flag", "")
    
    return render_template(
        "library.html",
        status=command_output,
        seasons=SEASONS,
        settings=settings,
        next_race=next_race,
        calendar_data=calendar_data,
        flag_data=flag_data,
        active_tab="download"
    )


@app.route("/settings-page")
def settings_page():
    settings = load_webui_settings()
    session_defaults = {k: v["default"] for k, v in SESSION_TYPES.items()}
    next_race = get_next_race_info()
    calendar_data = {str(year): get_rounds_for_season(year) for year in SEASONS}
    
    return render_template(
        "settings.html",
        status=command_output,
        seasons=SEASONS,
        settings=settings,
        session_types=SESSION_TYPES,
        session_groups=SESSION_GROUPS,
        session_defaults=session_defaults,
        next_race=next_race,
        calendar_data=calendar_data,
        active_tab="settings"
    )


@app.route("/logs")
def logs_page():
    next_race = get_next_race_info()
    
    return render_template(
        "logs.html",
        status=command_output,
        next_race=next_race,
        active_tab="logs"
    )


@app.route("/run", methods=["POST"])
def run():
    if command_output["running"]:
        return jsonify({"error": "Command already running"}), 400
    
    data = request.json
    cmd = data.get("command", "status")
    
    if cmd.startswith("overlay "):
        thread = threading.Thread(target=run_overlay_command, args=(cmd[8:],))
    else:
        thread = threading.Thread(target=run_grabber_command, args=(cmd,))
    
    thread.start()
    return jsonify({"status": "started", "command": cmd})


@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "POST":
        new_settings = request.json
        save_webui_settings(new_settings)
        update_settings_yml(new_settings)
        return jsonify({"status": "saved"})
    return jsonify(load_webui_settings())


@app.route("/status")
def status():
    return jsonify(command_output)

@app.route("/api/activity-events")
def api_activity_events():
    """Get all activity events for the activity feed."""
    try:
        activity = get_all_activity()
        return jsonify(activity)
    except Exception as e:
        return jsonify({"error": str(e), "active": [], "history": []}), 500


@app.route("/api/activity-events/clear", methods=["POST"])
def api_clear_activity():
    """Clear all activity events."""
    try:
        clear_all_activity()
        return jsonify({"status": "cleared"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route("/library-status")
def library_status():
    season = request.args.get("season", "2025", type=int)
    round_num = request.args.get("round", "", type=str)
    round_filter = int(round_num) if round_num else None
    
    data = get_library_status(season, round_filter)
    
    for round_data in data["rounds"]:
        for session in round_data["sessions"]:
            # Include episode number for session key building
            session["episode"] = int(session["type"])
            session["type"] = session["type"].name
    
    return jsonify(data)


@app.route("/app-config", methods=["GET", "POST"])
def app_config_route():
    """Get or save app configuration (settings.yml)."""
    if request.method == "POST":
        new_config = request.json
        
        settings = {
            "prowlarr": new_config.get("prowlarr", {}),
            "sabnzbd": new_config.get("sabnzbd", {}),
            "plex": new_config.get("plex", {}),
            "library": new_config.get("library", {}),
            "discord": new_config.get("discord", {}),
            "timezone": new_config.get("timezone", "Europe/London"),
        }
        
        success = save_settings(settings)
        reload_settings()
        validation = validate()
        
        return jsonify({"status": "saved" if success else "error", "validation": validation})
    
    reload_settings()
    settings = _load_yaml()
    validation = validate()
    return jsonify({**settings, "validation": validation})


@app.route("/test-connections")
def test_connections():
    """Test connections to Prowlarr, SABnzbd, and Plex."""
    import requests as req
    
    results = {"prowlarr": False, "sabnzbd": False, "plex": False, "errors": []}
    
    # Prowlarr
    try:
        url, key = get_prowlarr_url(), get_prowlarr_api_key()
        if url and key:
            r = req.get(f"{url}/api/v1/health", headers={"X-Api-Key": key}, timeout=5)
            results["prowlarr"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Prowlarr: {str(e)}")
    
    # SABnzbd
    try:
        url, key = get_sabnzbd_url(), get_sabnzbd_api_key()
        if url and key:
            r = req.get(f"{url}/api?mode=version&apikey={key}&output=json", timeout=5)
            results["sabnzbd"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"SABnzbd: {str(e)}")
    
    # Plex
    try:
        url, token = get_plex_url(), get_plex_token()
        if url and token:
            r = req.get(f"{url}/?X-Plex-Token={token}", timeout=5)
            results["plex"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Plex: {str(e)}")
    
    return jsonify(results)


@app.route("/logs/stream")
def stream_logs():
    """Stream log file using Server-Sent Events."""
    def generate():
        try:
            if not LOG_FILE.exists():
                yield f"data: Log file not found: {LOG_FILE}\n\n"
                return
            
            # Send last 100 lines initially
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                for line in lines[-100:]:
                    yield f"data: {line.rstrip()}\n\n"
            
            # Then tail the file
            with open(LOG_FILE, 'r') as f:
                f.seek(0, 2)  # Go to end
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {line.rstrip()}\n\n"
                    else:
                        import time
                        time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            yield f"data: Error reading log: {e}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')


@app.route("/logs/recent")
def recent_logs():
    """Get recent log lines."""
    lines = request.args.get("lines", 100, type=int)
    
    if not LOG_FILE.exists():
        return jsonify({"error": "Log file not found", "lines": []})
    
    try:
        with open(LOG_FILE, 'r') as f:
            all_lines = f.readlines()
            return jsonify({"lines": [l.rstrip() for l in all_lines[-lines:]]})
    except Exception as e:
        return jsonify({"error": str(e), "lines": []})




@app.route("/stats")
def stats():
    """Statistics page."""
    next_race = get_next_race_info()
    stats_data = get_stats_summary()
    
    # Calculate max values for chart scaling
    max_daily = max([d["count"] for d in stats_data.get("by_day", [])], default=1) or 1
    max_monthly = max([m["count"] for m in stats_data.get("by_month", [])], default=1) or 1
    
    return render_template(
        "stats.html",
        status=command_output,
        next_race=next_race,
        stats=stats_data,
        max_daily=max_daily,
        max_monthly=max_monthly,
        active_tab="stats"
    )



@app.route("/failed-sessions")
def failed_sessions():
    """Get recently failed session searches."""
    state_file = CONFIG_DIR / "grabber_state.json"
    failed = {}
    
    if state_file.exists():
        try:
            with open(state_file, 'r') as f:
                state = json.load(f)
                failed = state.get("failed", {})
        except Exception:
            pass
    
    return jsonify({"failed": failed})


@app.route("/download-selected", methods=["POST"])
def download_selected():
    """Download selected missing sessions."""
    data = request.json
    season = data.get("season")
    selections = data.get("selections", [])
    
    if not selections:
        return jsonify({"error": "No sessions selected"}), 400
    
    # Group by round for efficiency
    by_round = {}
    for sel in selections:
        round_num = sel.get("round")
        if round_num not in by_round:
            by_round[round_num] = []
        
        # Convert session type name to episode number
        session_name = sel.get("session", "")
        try:
            # SessionType is an IntEnum, so we can get its value
            session_type = SessionType[session_name]
            episode_num = f"E{int(session_type):02d}"
            by_round[round_num].append(episode_num)
        except (KeyError, ValueError) as e:
            # If conversion fails, skip this session
            print(f"Could not convert session: {session_name} - {e}")
            continue
    
    # Build commands with specific sessions
    commands = []
    for round_num, episodes in by_round.items():
        if episodes:  # Only if we have valid episodes
            sessions_str = ",".join(episodes)
            cmd = f"backfill --season {season} --round {round_num} --sessions {sessions_str} --force"
            commands.append(cmd)
    
    # Run commands sequentially in background, keeping running=True until all complete
    def run_all_commands(cmds, season, round_info):
        global command_output
        total = len(cmds)
        command_output["batch_total"] = total
        
        for i, (cmd, info) in enumerate(zip(cmds, round_info)):
            # Update command display with clear search status
            command_output["running"] = True
            command_output["batch_current"] = i + 1
            round_num = info["round"]
            episodes = info["episodes"]
            command_output["command"] = f"Searching ({i+1}/{total}): Season {season} R{round_num} {episodes}"
            command_output["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if i == 0:
                command_output["output"] = f"Starting batch download of {total} job(s)...\n\n"
            else:
                command_output["output"] += f"\n{'='*60}\n"
            
            command_output["output"] += f">>> Search {i+1}/{total}: Season {season} Round {round_num} - {episodes}\n\n"
            
            try:
                cmd_parts = [PYTHON_PATH, str(GRABBER_SCRIPT)] + cmd.split()
                process = subprocess.Popen(
                    cmd_parts, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1
                )
                
                for line in process.stdout:
                    command_output["output"] += line
                
                process.wait()
                
            except Exception as e:
                command_output["output"] += f"\nError: {e}\n"
        
        # Only set running=False after ALL commands complete
        command_output["running"] = False
        command_output["batch_total"] = 0
        command_output["batch_current"] = 0
        command_output["output"] += f"\n{'='*60}\n✅ All {total} searches completed.\n"
        command_output["output"] += f"\n{'='*60}\nAll {len(cmds)} commands completed.\n"
    
    # Build round info for status display
    round_info = []
    for round_num, episodes in by_round.items():
        if episodes:
            round_info.append({"round": round_num, "episodes": ",".join(episodes)})
    
    if commands:
        thread = threading.Thread(target=run_all_commands, args=(commands, season, round_info))
        thread.start()
    
    return jsonify({
        "status": "started",
        "rounds": list(by_round.keys()),
        "total_sessions": len(selections),
        "commands": commands
    })



@app.route("/activity")
def activity():
    """Activity feed page."""
    next_race = get_next_race_info()
    
    return render_template(
        "activity.html",
        status=command_output,
        next_race=next_race,
        active_tab="activity"
    )


@app.route("/activity/stream")
def activity_stream():
    """Stream activity events parsed from log file."""
    import re
    
    def parse_log_line(line):
        """Parse a log line into an activity event."""
        # Pattern: 2026-01-31 19:24:30 [INFO] motogp.grabber: message
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] ([\w.]+): (.+)', line)
        if not match:
            return None
        
        timestamp, level, source, message = match.groups()
        event = {'timestamp': timestamp, 'level': level, 'source': source, 'raw': message}
        
        # Parse specific event types
        if '🔍 Searching:' in message:
            # 🔍 Searching: Round 3 Americas - MotoGP Free Practice 2
            m = re.search(r'Round (\d+) (\w+) - (.+)', message)
            if m:
                event['stage'] = 'searching'
                event['round'] = m.group(1)
                event['venue'] = m.group(2)
                event['name'] = f"{m.group(2)} {m.group(3)}"  # "Americas MotoGP Free Practice 2"
                event['session_key'] = f"R{m.group(1)}_{m.group(3).replace(' ', '_')}"
        
        elif '📋 Found' in message:
            # 📋 Found 6 candidates for Round 4 Qatar - MotoGP Free Practice 2
            m = re.search(r'Found (\d+) candidates for Round (\d+) (\w+) - (.+)', message)
            if m:
                event['stage'] = 'found'
                event['candidates'] = int(m.group(1))
                event['round'] = m.group(2)
                event['venue'] = m.group(3)
                event['name'] = f"{m.group(3)} {m.group(4)}"  # "Qatar MotoGP Free Practice 2"
                event['session_key'] = f"R{m.group(2)}_{m.group(4).replace(' ', '_')}"
            else:
                # Fallback for old format without Round/Venue
                m = re.search(r'Found (\d+) candidates for (.+)', message)
                if m:
                    event['stage'] = 'found'
                    event['candidates'] = int(m.group(1))
                    event['name'] = m.group(2)
                    event['session_key'] = f"search_{m.group(2).replace(' ', '_')}"
        
        elif '⬇️ Sending to SABnzbd:' in message:
            # ⬇️ Sending to SABnzbd: MotoGP.2025.The.Americas...
            m = re.search(r'Sending to SABnzbd: (.+)', message)
            if m:
                event['stage'] = 'sending'
                event['release'] = m.group(1)
        
        elif '✅ Grabbed:' in message:
            # ✅ Grabbed: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Grabbed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'grabbed'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif '📦 Release:' in message:
            # 📦 Release: MotoGP.2025.The.Americas...
            m = re.search(r'Release: (.+)', message)
            if m:
                event['release'] = m.group(1)
        
        elif '📊 Score:' in message:
            # 📊 Score: 155
            m = re.search(r'Score: (\d+)', message)
            if m:
                event['score'] = int(m.group(1))
        
        elif '📂 New download ready:' in message:
            # 📂 New download ready: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'New download ready: (\S+) (.+)', message)
            if m:
                event['stage'] = 'ready'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif 'Processing:' in message and 'motogp.sort' in source:
            # Processing: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Processing: (\S+) (.+)', message)
            if m:
                event['stage'] = 'sorting'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif 'Applying Plex tags' in message:
            event['stage'] = 'tagging'
        
        elif 'Triggering Plex refresh' in message:
            event['stage'] = 'plex'
        
        elif '✅ Successfully processed:' in message:
            # ✅ Successfully processed: 25R03E09 Americas MotoGP Free Practice 2
            m = re.search(r'Successfully processed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'complete'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
        
        elif '✅ Sort completed:' in message:
            # ✅ Sort completed: 25R08E07 Aragon Moto2 Practice
            m = re.search(r'Sort completed: (\S+) (.+)', message)
            if m:
                event['stage'] = 'complete'
                event['session_key'] = m.group(1)
                event['name'] = m.group(2)
                
        elif '✅ Plex refresh triggered' in message:
            # This one doesn't have session info in the message
            # Skip it - let "Successfully processed" be the complete trigger
            return None

        elif '❌ No candidates:' in message:
            m = re.search(r'No candidates: Round (\d+) (.+?) - (.+)', message)
            if m:
                event['stage'] = 'error'
                event['round'] = m.group(1)
                event['venue'] = m.group(2)
                event['name'] = f"{m.group(2)} {m.group(3)}"
                event['session_key'] = f"R{m.group(1)}_{m.group(3).replace(' ', '_')}"
                event['detail'] = 'No matching releases found'
                event['candidates'] = 0
                event['error_message'] = 'Episode unavailable - no matching releases after filtering'

           
        elif '❌' in message or 'Error' in message or 'error' in message.lower():
            event['stage'] = 'error'
            event['detail'] = message
        
        return event if event.get('stage') or event.get('release') or event.get('score') else None
        
    def generate():
        try:
            log_file = CONFIG_DIR / "logs" / "motogp.log"
            
            if not log_file.exists():
                yield "data: " + json.dumps({"error": "Log file not found"}) + "\n\n"
                return
            
            # Send last 50 lines initially
            with open(log_file, 'r') as f:
                lines = f.readlines()[-50:]
                for line in lines:
                    event = parse_log_line(line.strip())
                    if event:
                        yield "data: " + json.dumps(event) + "\n\n"
            
            # Then tail the file
            with open(log_file, 'r') as f:
                f.seek(0, 2)  # Go to end
                while True:
                    line = f.readline()
                    if line:
                        event = parse_log_line(line.strip())
                        if event:
                            yield "data: " + json.dumps(event) + "\n\n"
                    else:
                        import time
                        time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            yield "data: " + json.dumps({"error": str(e)}) + "\n\n"
    
    return Response(generate(), mimetype='text/event-stream')


@app.route("/calendar")
def calendar():
    """Calendar view of race schedule."""
    year = request.args.get("year", 2025, type=int)
    next_race = get_next_race_info()
    
    # Get calendar data for all seasons
    calendar_data = {}
    for y in SEASONS:
        rounds = get_rounds_for_season(y)
        calendar_data[y] = rounds
    
    # Get library status for the selected year
    library_status = {}
    lib_data = get_library_status(year)
    for r in lib_data.get("rounds", []):
        library_status[r["number"]] = {
            "grabbed": r["grabbed_count"],
            "total": r["desired_count"],
            "sessions": [{"name": s["name"], "exists": s["exists"], "type": s["type"], "episode": s.get("episode", 0)} for s in r["sessions"]]
        }
    
    # Build flag data per season
    flag_data = {}
    for y in SEASONS:
        flag_data[y] = {}
        cal = load_calendar_data(y)
        for r in cal.get("rounds", []):
            flag_data[y][r.get("round")] = r.get("flag", "")
    
    return render_template(
        "calendar.html",
        status=command_output,
        next_race=next_race,
        year=year,
        calendar_data=calendar_data,
        library_status=library_status,
        flag_data=flag_data,
        active_tab="calendar"
    )


@app.route("/calendar-data")
def calendar_data_api():
    """Get calendar and library data for a specific year."""
    year = request.args.get("year", 2025, type=int)
    
    library_status = {}
    lib_data = get_library_status(year)
    for r in lib_data.get("rounds", []):
        library_status[r["number"]] = {
            "grabbed": r["grabbed_count"],
            "total": r["desired_count"],
            "sessions": [{"name": s["name"], "exists": s["exists"]} for s in r["sessions"]]
        }
    
    return jsonify({
        "year": year,
        "library_status": library_status
    })

@app.route("/api/sabnzbd/queue")
def sabnzbd_queue():
    """Get SABnzbd queue status for motogp category."""
    import requests
    
    try:
        # Load SABnzbd settings
        settings_file = CONFIG_DIR / "settings.yml"
        if not settings_file.exists():
            return jsonify({"error": "Settings file not found"}), 404
        
        with open(settings_file, 'r') as f:
            import yaml
            settings = yaml.safe_load(f)
        
        sab_config = settings.get('sabnzbd', {})
        sab_url = sab_config.get('url', '').rstrip('/')
        sab_api_key = sab_config.get('api_key', '')
        
        if not sab_url or not sab_api_key:
            return jsonify({"error": "SABnzbd not configured"}), 400
        
        # Call SABnzbd API
        api_url = f"{sab_url}/api"
        params = {
            'apikey': sab_api_key,
            'mode': 'queue',
            'output': 'json'
        }
        
        response = requests.get(api_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        queue = data.get('queue', {})
        slots = queue.get('slots', [])
        
        # Filter for motogp category
        motogp_downloads = []
        for slot in slots:
            if slot.get('cat', '').lower() == 'motogp':
                motogp_downloads.append({
                    'filename': slot.get('filename', 'Unknown'),
                    'size': slot.get('size', '0 MB'),
                    'sizeleft': slot.get('sizeleft', '0 MB'),
                    'percentage': slot.get('percentage', '0'),
                    'timeleft': slot.get('timeleft', 'Unknown'),
                    'status': slot.get('status', 'Unknown')
                })
        
        # Get total queue info
        result = {
            'motogp_downloads': motogp_downloads,
            'motogp_count': len(motogp_downloads),
            'total_size': queue.get('size', '0 MB'),
            'total_sizeleft': queue.get('sizeleft', '0 MB'),
            'speed': queue.get('speed', '0 KB/s'),
            'timeleft': queue.get('timeleft', 'Unknown'),
            'paused': queue.get('paused', False),
            'total_slots': len(slots)
        }
        
        return jsonify(result)
    
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Failed to connect to SABnzbd: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/flags/<path:filename>")
def flag_files(filename):
    """Serve flag SVG files."""
    return send_from_directory(str(SCRIPT_DIR / "assets" / "flag_assets_svg"), filename)


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "MotoGParr",
        "short_name": "MotoGParr",
        "description": "MotoGP Automated Recording & Retrieval",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#52aad8 ",
        "orientation": "any",
        "icons": [
            {
                "src": "/assets/icon-192x192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            },
            {
                "src": "/assets/icon-512x512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    })

@app.route('/verify-admin', methods=['POST'])
def verify_admin():
    """Verify admin password."""
    import yaml
    
    data = request.get_json()
    password = data.get("password", "") if data else ""
    
    try:
        settings_file = CONFIG_DIR / "settings.yml"
        if settings_file.exists():
            with open(settings_file, "r") as f:
                settings = yaml.safe_load(f)
                admin_password = settings.get("uiadminpassword", "")
                
                if admin_password and password == admin_password:
                    return jsonify({"valid": True})
    except Exception as e:
        print(f"Error verifying admin password: {e}")
    
    return jsonify({"valid": False})

@app.route("/sw.js")
def service_worker():
    return send_from_directory(str(SCRIPT_DIR / "static" / "js"), "sw.js", mimetype='application/javascript')


@app.route("/assets/<path:filename>")
def static_files(filename):
    return send_from_directory(str(SCRIPT_DIR / "assets"), filename)


if __name__ == "__main__":
    print("=" * 50)
    print("  MotoGParr Web UI")
    print("  Open http://your-server-ip:5679")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5679, debug=False)