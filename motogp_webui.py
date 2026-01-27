#!/usr/bin/env python3
"""
MotoGP Grabber Web UI
======================

Web interface for MotoGP grabber with:
- Quick actions (status, scheduled checks)
- Backfill controls
- Overlay utility integration
- Library status viewer

Usage:
    python motogp_webui.py
    
Then open http://your-unraid-ip:5679 in a browser.
"""

import os
import sys
import subprocess
import threading
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, send_from_directory

# Add paths for imports
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from motogp_common import (
    SessionType,
    RaceClass,
    parse_prefix,
    load_calendar,
    get_round_by_number,
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

# Configuration
PYTHON_PATH = "/opt/venv/bin/python3"
GRABBER_SCRIPT = SCRIPT_DIR / "motogp_grabber.py"
OVERLAY_SCRIPT = SCRIPT_DIR / "motogp_overlay_utility.py"
CONFIG_DIR = get_config_dir()
WEBUI_CONFIG_FILE = CONFIG_DIR / "webui_settings.json"
MOTOGP_LIBRARY_PATH = get_motogp_library_path()

# Available seasons
SEASONS = [2024, 2025, 2026]

# Session types grouped by class
SESSION_GROUPS = {
    "MotoGP": [
        "MOTOGP_PRESS_CONFERENCE",
        "MOTOGP_FP1",
        "MOTOGP_PRACTICE",
        "MOTOGP_FP2",
        "MOTOGP_Q1",
        "MOTOGP_Q2",
        "MOTOGP_SPRINT",
        "MOTOGP_WARMUP",
        "MOTOGP_RACE",
        "MOTOGP_CHEQUERED_FLAG",
    ],
    "Moto2": [
        "MOTO2_FP1",
        "MOTO2_PRACTICE",
        "MOTO2_FP2",
        "MOTO2_Q1",
        "MOTO2_Q2",
        "MOTO2_RACE",
    ],
    "Moto3": [
        "MOTO3_FP1",
        "MOTO3_PRACTICE",
        "MOTO3_FP2",
        "MOTO3_Q1",
        "MOTO3_Q2",
        "MOTO3_RACE",
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

app = Flask(__name__)

# Store command output
command_output = {
    "running": False,
    "command": "",
    "output": "",
    "timestamp": None
}


def load_calendar_data(year: int) -> dict:
    """Load calendar for a given year."""
    return load_calendar(year) or {"rounds": []}


def get_rounds_for_season(year: int) -> list:
    """Get list of rounds with venue names for a season."""
    calendar = load_calendar_data(year)
    rounds = []
    for r in calendar.get("rounds", []):
        rounds.append({
            "number": r.get("round"),
            "venue": r.get("venue"),
            "date": r.get("date"),
        })
    return rounds


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
                
                if weekend_start_str:
                    weekend_start = datetime.strptime(weekend_start_str, "%Y-%m-%d").date()
                else:
                    weekend_start = race_date - timedelta(days=2)
                
                weekend_end = race_date + timedelta(days=1)
                
                if weekend_start <= today <= weekend_end:
                    return {
                        "round": r.get("round"),
                        "venue": r.get("venue"),
                        "date": race_date_str,
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
                        "date": race_date_str,
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

def update_grabber_config(settings):
    """Update motogp_common.py with new settings from WebUI."""
    common_path = SCRIPT_DIR / "motogp_common.py"
    
    try:
        with open(common_path, 'r') as f:
            content = f.read()
        
        # Build the new DESIRED_SESSIONS block
        sessions_block = "DEFAULT_DESIRED_SESSIONS: Dict[SessionType, bool] = {\n"
        for session_type in SessionType:
            if session_type == SessionType.UNKNOWN:
                continue
            key = session_type.name
            enabled = settings.get("sessions", {}).get(key, DEFAULT_DESIRED_SESSIONS.get(session_type, False))
            sessions_block += f"    SessionType.{key}: {enabled},\n"
        sessions_block += "}"
        
        # Replace the DEFAULT_DESIRED_SESSIONS block
        content = re.sub(
            r'DEFAULT_DESIRED_SESSIONS:\s*Dict\[SessionType,\s*bool\]\s*=\s*\{[^}]+\}',
            sessions_block,
            content,
            flags=re.DOTALL
        )
        
        with open(common_path, 'w') as f:
            f.write(content)
        
        return True
    except Exception as e:
        print(f"Error updating grabber config: {e}")
        return False


def update_settings_yml(settings):
    """Update settings.yml with max_grabs_per_run."""
    from utils.config_loader import _load_yaml, save_settings as save_yml
    
    try:
        current = _load_yaml()
        if "grabber" not in current:
            current["grabber"] = {}
        current["grabber"]["max_grabs_per_run"] = settings.get("max_grabs_per_run", 5)
        save_yml(current)
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
                session_type = SessionType[key]
                desired_set.add(session_type)
            except KeyError:
                pass
    
    # If no settings, use defaults
    if not desired_set:
        desired_set = {st for st, enabled in DEFAULT_DESIRED_SESSIONS.items() if enabled}
    
    result = {
        "rounds": [],
        "total_grabbed": 0,
        "total_desired": 0,
    }
    
    today = datetime.now().date()
    
    for r in calendar.get("rounds", []):
        round_num = r.get("round")
        
        if round_filter and round_num != round_filter:
            continue
        
        # Skip future rounds
        race_date_str = r.get("date", "")
        if race_date_str:
            try:
                race_date = datetime.strptime(race_date_str, "%Y-%m-%d").date()
                if race_date > today:
                    continue
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
            "sessions": sessions,
            "grabbed_count": grabbed_count,
            "desired_count": desired_count,
        })
        
        result["total_grabbed"] += grabbed_count
        result["total_desired"] += desired_count
    
    return result


# =============================================================================
# HTML TEMPLATE
# =============================================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>MotoGParr</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#ff6600">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background: #000000;
            color: #eee;
        }
        h1 {
            color: #ff6600;
            border-bottom: 2px solid #ff6600;
            padding-bottom: 10px;
        }
        h2 {
            color: #fff;
            margin-top: 30px;
            margin-bottom: 15px;
        }
        h3 {
            color: #ccc;
            margin: 15px 0 10px 0;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .card {
            background: #0a0a12;
            border: 1px solid #1a1a2e;
            border-radius: 8px;
            padding: 20px;
            margin: 15px 0;
        }
        .next-race {
            background: linear-gradient(135deg, #ff6600 0%, #cc5200 100%);
            border-radius: 8px;
            padding: 20px;
            margin: 15px 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
        }
        .next-race.race-now {
            background: linear-gradient(135deg, #27ae60 0%, #1e8449 100%);
            animation: pulse 2s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.8; }
        }
        .next-race-info { flex: 1; }
        .next-race-title {
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 1px;
            opacity: 0.9;
            margin-bottom: 5px;
        }
        .next-race-venue { font-size: 24px; font-weight: bold; }
        .next-race-round { font-size: 14px; opacity: 0.9; margin-top: 5px; }
        .next-race-countdown { text-align: right; }
        .countdown-number { font-size: 36px; font-weight: bold; }
        .countdown-label { font-size: 14px; opacity: 0.9; }
        .form-group { margin: 15px 0; }
        label { display: block; margin-bottom: 5px; font-weight: 500; }
        select, button, input[type="number"] {
            padding: 12px;
            border-radius: 5px;
            border: 1px solid #2a2a3e;
            font-size: 16px;
        }
        select, input[type="number"] {
            background: #141420;
            color: #fff;
            margin-bottom: 10px;
        }
        select:focus, input[type="number"]:focus {
            outline: none;
            border-color: #ff6600;
        }
        select { width: 100%; }
        input[type="number"] { width: 100px; text-align: center; }
        button {
            background: #ff6600;
            color: white;
            cursor: pointer;
            font-weight: bold;
            transition: background 0.2s;
            padding: 12px 24px;
            border: none;
        }
        button:hover { background: #ff8533; }
        button:disabled { background: #333; color: #666; cursor: not-allowed; }
        button.secondary { background: #1a1a2e; border: 1px solid #2a2a3e; }
        button.secondary:hover { background: #2a2a3e; }
        button.success { background: #27ae60; }
        button.success:hover { background: #2ecc71; }
        button.warning { background: #f39c12; color: #000; }
        button.warning:hover { background: #f1c40f; }
        button.small { padding: 6px 12px; font-size: 12px; }
        .output {
            background: #050508;
            border: 1px solid #1a1a2e;
            border-radius: 5px;
            padding: 15px;
            font-family: 'Monaco', 'Menlo', 'Consolas', monospace;
            font-size: 13px;
            white-space: pre-wrap;
            word-wrap: break-word;
            max-height: 400px;
            overflow-y: auto;
            margin-top: 15px;
            color: #0f0;
        }
        .status { padding: 10px 15px; border-radius: 5px; margin: 10px 0; display: inline-block; }
        .status.running { background: #f39c12; color: #000; }
        .status.ready { background: #27ae60; }
        .quick-buttons {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 10px;
            margin-top: 15px;
        }
        .quick-buttons button { width: 100%; }
        .checkbox-group { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
        .checkbox-group input[type="checkbox"] { width: 18px; height: 18px; cursor: pointer; accent-color: #ff6600; }
        .checkbox-group label { margin: 0; cursor: pointer; font-weight: normal; }
        .timestamp { font-size: 12px; color: #666; margin-top: 5px; }
        .sessions-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
        }
        .session-group {
            background: #0d0d14;
            border: 1px solid #1a1a2e;
            border-radius: 5px;
            padding: 15px;
        }
        .session-group h3 { margin-top: 0; color: #ff6600; }
        .inline-form { display: flex; align-items: center; gap: 15px; flex-wrap: wrap; }
        .inline-form label { margin: 0; }
        .save-notice {
            background: #27ae60;
            color: white;
            padding: 10px 15px;
            border-radius: 5px;
            margin: 10px 0;
            display: none;
        }
        .save-notice.show { display: block; }
        .tabs { display: flex; gap: 5px; margin-bottom: 0; }
        .tab {
            padding: 10px 20px;
            background: #0a0a12;
            border: 1px solid #1a1a2e;
            border-bottom: none;
            color: #888;
            cursor: pointer;
            border-radius: 5px 5px 0 0;
            font-size: 14px;
        }
        .tab:hover { color: #fff; }
        .tab.active { background: #0a0a12; color: #fff; border-color: #1a1a2e; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .button-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 15px; }
        
        /* Library status styles */
        .library-round {
            background: #0d0d14;
            border: 1px solid #1a1a2e;
            border-radius: 5px;
            padding: 15px;
            margin: 10px 0;
        }
        .library-round-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
            flex-wrap: wrap;
            gap: 10px;
        }
        .library-round-title { font-weight: bold; font-size: 16px; }
        .library-round-stats { color: #888; font-size: 14px; }
        .library-sessions {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 5px;
        }
        .library-session {
            padding: 5px 10px;
            border-radius: 3px;
            font-size: 14px;
        }
        .library-session.exists { background: rgba(39, 174, 96, 0.2); color: #27ae60; }
        .library-session.missing { background: rgba(231, 76, 60, 0.2); color: #e74c3c; }
        .library-summary {
            background: linear-gradient(135deg, #1a1a2e 0%, #0a0a12 100%);
            border-radius: 8px;
            padding: 20px;
            margin: 15px 0;
            text-align: center;
        }
        .library-summary-number { font-size: 48px; font-weight: bold; color: #ff6600; }
        .library-summary-label { font-size: 14px; color: #888; }
        .progress-bar {
            height: 8px;
            background: #1a1a2e;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
        }
        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #ff6600, #27ae60);
            transition: width 0.3s;
        }
        .library-filter { display: flex; gap: 15px; align-items: center; flex-wrap: wrap; margin-bottom: 20px; }
        .library-filter select { width: auto; min-width: 150px; }
        
        /* Sub-tabs for Settings */
        .sub-tabs {
            display: flex;
            gap: 5px;
            margin-bottom: 20px;
            border-bottom: 1px solid #1a1a2e;
            padding-bottom: 10px;
        }
        .sub-tab {
            padding: 8px 16px;
            background: transparent;
            border: 1px solid #2a2a3e;
            color: #888;
            cursor: pointer;
            border-radius: 20px;
            font-size: 13px;
            transition: all 0.2s;
        }
        .sub-tab:hover { color: #fff; border-color: #ff6600; }
        .sub-tab.active { background: #ff6600; color: #fff; border-color: #ff6600; }
        .sub-tab-content { display: none; }
        .sub-tab-content.active { display: block; }
        
        /* Config form styles */
        .config-section {
            background: #0d0d14;
            border: 1px solid #1a1a2e;
            border-radius: 5px;
            padding: 15px;
            margin-bottom: 15px;
        }
        .config-section h3 {
            margin-top: 0;
            color: #ff6600;
            border-bottom: 1px solid #1a1a2e;
            padding-bottom: 10px;
            margin-bottom: 15px;
        }
        .config-row {
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            margin-bottom: 15px;
        }
        .config-field {
            flex: 1;
            min-width: 250px;
        }
        .config-field label {
            display: block;
            margin-bottom: 5px;
            font-size: 13px;
            color: #aaa;
        }
        .config-field input[type="text"],
        .config-field input[type="password"] {
            width: 100%;
            padding: 10px;
            background: #141420;
            border: 1px solid #2a2a3e;
            border-radius: 5px;
            color: #fff;
            font-size: 14px;
        }
        .config-field input:focus {
            outline: none;
            border-color: #ff6600;
        }
        .config-field .input-with-toggle {
            position: relative;
        }
        .config-field .toggle-visibility {
            position: absolute;
            right: 10px;
            top: 50%;
            transform: translateY(-50%);
            background: none;
            border: none;
            color: #666;
            cursor: pointer;
            padding: 5px;
            font-size: 14px;
        }
        .config-field .toggle-visibility:hover { color: #fff; }
        .config-help {
            font-size: 11px;
            color: #666;
            margin-top: 5px;
        }
        .validation-status {
            padding: 10px 15px;
            border-radius: 5px;
            margin-bottom: 15px;
            font-size: 14px;
        }
        .validation-status.valid {
            background: rgba(39, 174, 96, 0.2);
            border: 1px solid #27ae60;
            color: #27ae60;
        }
        .validation-status.invalid {
            background: rgba(231, 76, 60, 0.2);
            border: 1px solid #e74c3c;
            color: #e74c3c;
        }
        
        /* Class badges */
        .class-badge {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: bold;
            margin-left: 5px;
        }
        .class-badge.motogp { background: #ff6600; color: #fff; }
        .class-badge.moto2 { background: #0066cc; color: #fff; }
        .class-badge.moto3 { background: #00cc66; color: #fff; }
    </style>
</head>
<body>
    <h1>🏍️ MotoGParr</h1>
    
    <!-- Next Race Countdown -->
    <div class="next-race {{ 'race-now' if next_race.status == 'now' }}">
        <div class="next-race-info">
            <div class="next-race-title">
                {% if next_race.status == 'now' %}🏁 Race Weekend In Progress
                {% else %}🏍️ Next Race Weekend{% endif %}
            </div>
            <div class="next-race-venue">{{ next_race.venue }}</div>
            {% if next_race.round %}
            <div class="next-race-round">Round {{ next_race.round }} • {{ next_race.date }}</div>
            {% endif %}
        </div>
        <div class="next-race-countdown">
            {% if next_race.status == 'now' %}
                <div class="countdown-number">🟢 LIVE</div>
                <div class="countdown-label">Race Weekend Now!</div>
            {% elif next_race.days is not none %}
                <div class="countdown-number">{{ next_race.days }}</div>
                <div class="countdown-label">{{ 'day' if next_race.days == 1 else 'days' }} until Friday</div>
            {% else %}
                <div class="countdown-label">{{ next_race.countdown }}</div>
            {% endif %}
        </div>
    </div>
    
    <div class="status {{ 'running' if status.running else 'ready' }}">
        {% if status.running %}⏳ Running: {{ status.command }}
        {% else %}✅ Ready{% endif %}
    </div>

    <!-- Tabs -->
    <div class="tabs">
        <button class="tab active" onclick="showTab('commands')">Commands</button>
        <button class="tab" onclick="showTab('library')">Library</button>
        <button class="tab" onclick="showTab('settings')">Settings</button>
    </div>

    <!-- Commands Tab -->
    <div id="commands-tab" class="tab-content active">
        <!-- Quick Actions -->
        <div class="card">
            <h2>Quick Actions</h2>
            <div class="quick-buttons">
                <button onclick="runCommand('status')" {{ 'disabled' if status.running }}>📊 Check Status</button>
                <button onclick="runCommand('scheduled')" {{ 'disabled' if status.running }}>🔍 Run Scheduled Check</button>
                <button onclick="runCommand('scheduled --dry-run')" class="secondary" {{ 'disabled' if status.running }}>👁️ Scheduled (Dry Run)</button>
                <button onclick="refreshOutput()" class="secondary">🔄 Refresh Output</button>
            </div>
        </div>

        <!-- Backfill & Overlay -->
        <div class="card">
            <h2>Backfill & Overlay Tools</h2>
            <div class="form-group">
                <label>Season</label>
                <select id="season" onchange="updateRounds()">
                    {% for year in seasons %}<option value="{{ year }}" {{ 'selected' if year == 2025 }}>{{ year }}</option>{% endfor %}
                </select>
            </div>
            <div class="form-group">
                <label>Round (optional)</label>
                <select id="round">
                    <option value="">All Rounds</option>
                </select>
            </div>
            <div class="form-group checkbox-group">
                <input type="checkbox" id="force">
                <label for="force">Force retry failed searches</label>
            </div>
            <div class="form-group checkbox-group">
                <input type="checkbox" id="dry-run">
                <label for="dry-run">Dry run (don't actually grab)</label>
            </div>
            
            <div class="button-row">
                <button onclick="runBackfill()" {{ 'disabled' if status.running }}>🚀 Run Backfill</button>
            </div>
            
            <hr style="border-color: #1a1a2e; margin: 20px 0;">
            
            <h3>Overlay Scaling</h3>
            <div class="inline-form">
                <label for="scale-factor">Scale Factor:</label>
                <input type="number" id="scale-factor" min="0.5" max="3.0" step="0.1" value="1.0">
                <span style="color: #666; font-size: 14px;">(0.5 - 3.0)</span>
            </div>
            <div class="button-row">
                <button onclick="runOverlay()" class="warning" {{ 'disabled' if status.running }}>🎨 Apply Overlays</button>
                <button onclick="runOverlay(true)" class="secondary" {{ 'disabled' if status.running }}>👁️ Overlay (Dry Run)</button>
            </div>
        </div>

        <!-- Output -->
        <div class="card">
            <h2>Output</h2>
            {% if status.timestamp %}<div class="timestamp">Last run: {{ status.timestamp }}</div>{% endif %}
            <div class="output" id="output">{{ status.output or 'No output yet. Run a command to see results.' }}</div>
        </div>
    </div>

    <!-- Library Tab -->
    <div id="library-tab" class="tab-content">
        <div class="card">
            <h2>Library Status</h2>
            
            <div class="library-filter">
                <label>Season:</label>
                <select id="library-season" onchange="loadLibraryStatus()">
                    {% for year in seasons %}<option value="{{ year }}" {{ 'selected' if year == 2025 }}>{{ year }}</option>{% endfor %}
                </select>
                <label>Round:</label>
                <select id="library-round" onchange="loadLibraryStatus()">
                    <option value="">All Rounds</option>
                </select>
                <button onclick="loadLibraryStatus()" class="secondary small">🔄 Refresh</button>
            </div>
            
            <div id="library-content">
                <p style="color: #666;">Select a season and click Refresh to view library status.</p>
            </div>
        </div>
    </div>

    <!-- Settings Tab -->
    <div id="settings-tab" class="tab-content">
        <div class="card">
            <!-- Sub-tabs -->
            <div class="sub-tabs">
                <button class="sub-tab active" onclick="showSubTab('grabbing')">Grabbing</button>
                <button class="sub-tab" onclick="showSubTab('app-config')">App Config</button>
            </div>
            
            <!-- Grabbing Sub-tab -->
            <div id="grabbing-subtab" class="sub-tab-content active">
                <div class="save-notice" id="save-notice">✅ Settings saved successfully!</div>
                
                <h2>Grabbing Settings</h2>
                <div class="form-group inline-form">
                    <label for="max-grabs">Max grabs per run:</label>
                    <input type="number" id="max-grabs" min="1" max="50" value="{{ settings.max_grabs_per_run }}">
                    <span style="color: #666; font-size: 14px;">Limits how many sessions are grabbed in one scheduled run</span>
                </div>
                
                <h2 style="margin-top: 30px;">Desired Sessions</h2>
                <p style="color: #666; margin-bottom: 20px;">Select which session types to search for and download.</p>
                <div class="sessions-grid">
                    {% for group_name, session_keys in session_groups.items() %}
                    <div class="session-group">
                        <h3>{{ group_name }}</h3>
                        {% for key in session_keys %}
                        {% if key in session_types %}
                        <div class="checkbox-group">
                            <input type="checkbox" id="session-{{ key }}" data-session="{{ key }}"
                                   {{ 'checked' if settings.sessions.get(key, session_types[key].default) }}>
                            <label for="session-{{ key }}">{{ session_types[key].name }}</label>
                        </div>
                        {% endif %}
                        {% endfor %}
                    </div>
                    {% endfor %}
                </div>
                <div style="margin-top: 20px; display: flex; gap: 10px; flex-wrap: wrap;">
                    <button onclick="saveSettings()" class="success">💾 Save Settings</button>
                    <button onclick="selectAll(true)" class="secondary">Select All</button>
                    <button onclick="selectAll(false)" class="secondary">Deselect All</button>
                    <button onclick="resetDefaults()" class="secondary">Reset Defaults</button>
                </div>
            </div>
            
            <!-- App Config Sub-tab -->
            <div id="app-config-subtab" class="sub-tab-content">
                <div class="save-notice" id="config-save-notice">✅ Configuration saved successfully!</div>
                <div class="validation-status" id="config-validation"></div>
                
                <h2>Application Configuration</h2>
                <p style="color: #666; margin-bottom: 20px;">Configure connections to Prowlarr, SABnzbd, and Plex.</p>
                
                <!-- Prowlarr -->
                <div class="config-section">
                    <h3>🔍 Prowlarr</h3>
                    <div class="config-row">
                        <div class="config-field">
                            <label for="cfg-prowlarr-url">URL</label>
                            <input type="text" id="cfg-prowlarr-url" placeholder="http://192.168.1.100:9696">
                            <div class="config-help">Prowlarr server URL including port</div>
                        </div>
                        <div class="config-field">
                            <label for="cfg-prowlarr-key">API Key</label>
                            <div class="input-with-toggle">
                                <input type="password" id="cfg-prowlarr-key" placeholder="API Key">
                                <button type="button" class="toggle-visibility" onclick="togglePassword('cfg-prowlarr-key')">👁️</button>
                            </div>
                            <div class="config-help">Found in Prowlarr → Settings → General</div>
                        </div>
                    </div>
                </div>
                
                <!-- SABnzbd -->
                <div class="config-section">
                    <h3>📥 SABnzbd</h3>
                    <div class="config-row">
                        <div class="config-field">
                            <label for="cfg-sabnzbd-url">URL</label>
                            <input type="text" id="cfg-sabnzbd-url" placeholder="http://192.168.1.100:8080">
                            <div class="config-help">SABnzbd server URL including port</div>
                        </div>
                        <div class="config-field">
                            <label for="cfg-sabnzbd-key">API Key</label>
                            <div class="input-with-toggle">
                                <input type="password" id="cfg-sabnzbd-key" placeholder="API Key">
                                <button type="button" class="toggle-visibility" onclick="togglePassword('cfg-sabnzbd-key')">👁️</button>
                            </div>
                            <div class="config-help">Found in SABnzbd → Config → General</div>
                        </div>
                    </div>
                </div>
                
                <!-- Plex -->
                <div class="config-section">
                    <h3>🎬 Plex</h3>
                    <div class="config-row">
                        <div class="config-field">
                            <label for="cfg-plex-url">URL</label>
                            <input type="text" id="cfg-plex-url" placeholder="http://192.168.1.100:32400">
                            <div class="config-help">Plex server URL including port</div>
                        </div>
                        <div class="config-field">
                            <label for="cfg-plex-token">Token</label>
                            <div class="input-with-toggle">
                                <input type="password" id="cfg-plex-token" placeholder="Plex Token">
                                <button type="button" class="toggle-visibility" onclick="togglePassword('cfg-plex-token')">👁️</button>
                            </div>
                            <div class="config-help"><a href="https://support.plex.tv/articles/204059436" target="_blank" style="color: #ff6600;">How to find your Plex token</a></div>
                        </div>
                    </div>
                    <div class="config-row">
                        <div class="config-field">
                            <label for="cfg-plex-library-id">Library ID</label>
                            <input type="text" id="cfg-plex-library-id" placeholder="16">
                            <div class="config-help">Section ID from Plex URL (e.g., /library/sections/<strong>16</strong>/)</div>
                        </div>
                        <div class="config-field">
                            <label for="cfg-plex-library-name">Library Name</label>
                            <input type="text" id="cfg-plex-library-name" placeholder="Motorsport - MotoGP">
                            <div class="config-help">Display name of your MotoGP library in Plex</div>
                        </div>
                    </div>
                </div>
                
                <!-- Library Paths -->
                <div class="config-section">
                    <h3>📂 Library Paths</h3>
                    <p style="color: #888; font-size: 13px; margin-bottom: 15px;">
                        Paths inside this container (must match your volume mounts).
                    </p>
                    <div class="config-row">
                        <div class="config-field">
                            <label for="cfg-motogp-path">MotoGP Library Path</label>
                            <input type="text" id="cfg-motogp-path" placeholder="/media/Motorssport/MotoGP">
                            <div class="config-help">Where MotoGP content is stored (inside container)</div>
                        </div>
                        <div class="config-field">
                            <label for="cfg-downloads-path">Downloads Path</label>
                            <input type="text" id="cfg-downloads-path" placeholder="/downloads/complete/motogp">
                            <div class="config-help">Where SABnzbd puts completed MotoGP downloads</div>
                        </div>
                    </div>
                </div>
                
                <!-- Discord -->
                <div class="config-section">
                    <h3>💬 Discord (Optional)</h3>
                    <div class="config-row">
                        <div class="config-field" style="flex: 2;">
                            <label for="cfg-discord-webhook">Webhook URL</label>
                            <div class="input-with-toggle">
                                <input type="password" id="cfg-discord-webhook" placeholder="https://discord.com/api/webhooks/...">
                                <button type="button" class="toggle-visibility" onclick="togglePassword('cfg-discord-webhook')">👁️</button>
                            </div>
                            <div class="config-help">Discord webhook for notifications (leave empty to disable)</div>
                        </div>
                    </div>
                </div>
                
                <!-- Timezone -->
                <div class="config-section">
                    <h3>🕐 Timezone</h3>
                    <div class="config-row">
                        <div class="config-field">
                            <label for="cfg-timezone">Timezone</label>
                            <input type="text" id="cfg-timezone" placeholder="Europe/London">
                            <div class="config-help">Container timezone (e.g., Europe/London, America/New_York)</div>
                        </div>
                    </div>
                </div>
                
                <div style="margin-top: 20px; display: flex; gap: 10px; flex-wrap: wrap;">
                    <button onclick="saveAppConfig()" class="success">💾 Save Configuration</button>
                    <button onclick="testConnections()" class="secondary">🔌 Test Connections</button>
                    <button onclick="loadAppConfig()" class="secondary">🔄 Reload</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        const calendarData = {{ calendar_data | tojson }};
        const isRunning = {{ 'true' if status.running else 'false' }};
        const sessionDefaults = {{ session_defaults | tojson }};
        
        function scrollOutputToBottom() {
            const output = document.getElementById('output');
            if (output) output.scrollTop = output.scrollHeight;
        }
        
        window.onload = function() {
            updateRounds();
            updateLibraryRounds();
            scrollOutputToBottom();
        };
        
        function showTab(tabName) {
            document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            document.getElementById(tabName + '-tab').classList.add('active');
            event.target.classList.add('active');
            
            if (tabName === 'library') {
                loadLibraryStatus();
            }
        }
        
        function updateRounds() {
            const season = document.getElementById('season').value;
            const roundSelect = document.getElementById('round');
            const rounds = calendarData[season] || [];
            
            roundSelect.innerHTML = '<option value="">All Rounds</option>';
            rounds.forEach(r => {
                roundSelect.innerHTML += `<option value="${r.number}">Round ${r.number} - ${r.venue}</option>`;
            });
        }
        
        function updateLibraryRounds() {
            const season = document.getElementById('library-season').value;
            const roundSelect = document.getElementById('library-round');
            const rounds = calendarData[season] || [];
            
            roundSelect.innerHTML = '<option value="">All Rounds</option>';
            rounds.forEach(r => {
                roundSelect.innerHTML += `<option value="${r.number}">Round ${r.number} - ${r.venue}</option>`;
            });
        }
        
        function runCommand(cmd) {
            fetch('/run', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({command: cmd})
            }).then(() => setTimeout(() => location.reload(), 500));
        }
        
        function runBackfill() {
            const season = document.getElementById('season').value;
            const round = document.getElementById('round').value;
            const force = document.getElementById('force').checked;
            const dryRun = document.getElementById('dry-run').checked;
            
            let cmd = `backfill --season ${season}`;
            if (round) cmd += ` --round ${round}`;
            if (force) cmd += ' --force';
            if (dryRun) cmd += ' --dry-run';
            
            runCommand(cmd);
        }
        
        function runOverlay(dryRun = false) {
            const season = document.getElementById('season').value;
            const round = document.getElementById('round').value;
            let scale = parseFloat(document.getElementById('scale-factor').value);
            
            if (isNaN(scale) || scale < 0.5) scale = 0.5;
            if (scale > 3.0) scale = 3.0;
            document.getElementById('scale-factor').value = scale;
            
            let cmd;
            if (round) {
                cmd = `overlay apply-round ${season} ${round} --scale ${scale}`;
            } else {
                cmd = `overlay apply-season ${season} --scale ${scale}`;
            }
            if (dryRun) cmd += ' --dry-run';
            
            runCommand(cmd);
        }
        
        function refreshOutput() { location.reload(); }
        
        function loadLibraryStatus() {
            const season = document.getElementById('library-season').value;
            const round = document.getElementById('library-round').value;
            updateLibraryRounds();
            
            fetch(`/library-status?season=${season}&round=${round}`)
                .then(r => r.json())
                .then(data => {
                    renderLibraryStatus(data);
                });
        }
        
        function renderLibraryStatus(data) {
            const container = document.getElementById('library-content');
            
            if (!data.rounds || data.rounds.length === 0) {
                container.innerHTML = '<p style="color: #666;">No past rounds found for this season.</p>';
                return;
            }
            
            const pct = data.total_desired > 0 ? Math.round(data.total_grabbed / data.total_desired * 100) : 0;
            
            let html = `
                <div class="library-summary">
                    <div class="library-summary-number">${data.total_grabbed} / ${data.total_desired}</div>
                    <div class="library-summary-label">Sessions in Library (${pct}%)</div>
                    <div class="progress-bar">
                        <div class="progress-bar-fill" style="width: ${pct}%"></div>
                    </div>
                </div>
            `;
            
            data.rounds.forEach(round => {
                const roundPct = round.desired_count > 0 ? Math.round(round.grabbed_count / round.desired_count * 100) : 0;
                const hasMissing = round.grabbed_count < round.desired_count;
                
                html += `
                    <div class="library-round">
                        <div class="library-round-header">
                            <div>
                                <span class="library-round-title">Round ${round.number} - ${round.venue}</span>
                                <span class="library-round-stats">${round.date}</span>
                            </div>
                            <div style="display: flex; align-items: center; gap: 10px;">
                                <span class="library-round-stats">${round.grabbed_count}/${round.desired_count} (${roundPct}%)</span>
                                ${hasMissing ? `<button class="small warning" ${isRunning ? "disabled" : ""} onclick="grabMissing(${round.number})">Grab Missing</button>` : ''}
                            </div>
                        </div>
                        <div class="library-sessions">
                `;
                
                round.sessions.forEach(session => {
                    const cls = session.exists ? 'exists' : 'missing';
                    const icon = session.exists ? '✅' : '❌';
                    html += `<div class="library-session ${cls}">${icon} ${session.name}</div>`;
                });
                
                html += '</div></div>';
            });
            
            container.innerHTML = html;
        }
        
        function grabMissing(roundNum) {
            const season = document.getElementById('library-season').value;
            runCommand(`backfill --season ${season} --round ${roundNum} --force`);
        }
        
        function saveSettings() {
            const sessions = {};
            document.querySelectorAll('[data-session]').forEach(cb => {
                sessions[cb.dataset.session] = cb.checked;
            });
            
            const settings = {
                max_grabs_per_run: parseInt(document.getElementById('max-grabs').value) || 22,
                sessions: sessions
            };
            
            fetch('/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(settings)
            }).then(r => r.json()).then(data => {
                const notice = document.getElementById('save-notice');
                notice.classList.add('show');
                setTimeout(() => notice.classList.remove('show'), 3000);
            });
        }
        
        function selectAll(checked) {
            document.querySelectorAll('[data-session]').forEach(cb => cb.checked = checked);
        }
        
        function resetDefaults() {
            document.querySelectorAll('[data-session]').forEach(cb => {
                cb.checked = sessionDefaults[cb.dataset.session] || false;
            });
            document.getElementById('max-grabs').value = 22;
        }
        
        function showSubTab(tabName) {
            document.querySelectorAll('.sub-tab-content').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
            document.getElementById(tabName + '-subtab').classList.add('active');
            event.target.classList.add('active');
            
            if (tabName === 'app-config') {
                loadAppConfig();
            }
        }
        
        function togglePassword(inputId) {
            const input = document.getElementById(inputId);
            input.type = input.type === 'password' ? 'text' : 'password';
        }
        
        function loadAppConfig() {
            fetch('/app-config')
                .then(r => r.json())
                .then(data => {
                    document.getElementById('cfg-prowlarr-url').value = data.prowlarr?.url || '';
                    document.getElementById('cfg-prowlarr-key').value = data.prowlarr?.api_key || '';
                    document.getElementById('cfg-sabnzbd-url').value = data.sabnzbd?.url || '';
                    document.getElementById('cfg-sabnzbd-key').value = data.sabnzbd?.api_key || '';
                    document.getElementById('cfg-plex-url').value = data.plex?.url || '';
                    document.getElementById('cfg-plex-token').value = data.plex?.token || '';
                    document.getElementById('cfg-plex-library-id').value = data.plex?.library_id || '';
                    document.getElementById('cfg-plex-library-name').value = data.plex?.library_name || '';
                    document.getElementById('cfg-motogp-path').value = data.library?.motogp_path || '';
                    document.getElementById('cfg-downloads-path').value = data.library?.downloads_path || '';
                    document.getElementById('cfg-discord-webhook').value = data.discord?.webhook_url || '';
                    document.getElementById('cfg-timezone').value = data.timezone || 'Europe/London';
                    
                    updateValidationStatus(data.validation);
                });
        }
        
        function updateValidationStatus(validation) {
            const el = document.getElementById('config-validation');
            if (!validation) {
                el.style.display = 'none';
                return;
            }
            
            el.style.display = 'block';
            if (validation.valid) {
                el.className = 'validation-status valid';
                el.innerHTML = '✅ All required settings configured';
            } else {
                el.className = 'validation-status invalid';
                el.innerHTML = '⚠️ Missing: ' + validation.missing.join(', ');
            }
        }
        
        function saveAppConfig() {
            const config = {
                prowlarr: {
                    url: document.getElementById('cfg-prowlarr-url').value,
                    api_key: document.getElementById('cfg-prowlarr-key').value
                },
                sabnzbd: {
                    url: document.getElementById('cfg-sabnzbd-url').value,
                    api_key: document.getElementById('cfg-sabnzbd-key').value
                },
                plex: {
                    url: document.getElementById('cfg-plex-url').value,
                    token: document.getElementById('cfg-plex-token').value,
                    library_id: document.getElementById('cfg-plex-library-id').value,
                    library_name: document.getElementById('cfg-plex-library-name').value
                },
                library: {
                    motogp_path: document.getElementById('cfg-motogp-path').value,
                    downloads_path: document.getElementById('cfg-downloads-path').value
                },
                discord: {
                    webhook_url: document.getElementById('cfg-discord-webhook').value
                },
                timezone: document.getElementById('cfg-timezone').value
            };
            
            fetch('/app-config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(config)
            })
            .then(r => r.json())
            .then(data => {
                const notice = document.getElementById('config-save-notice');
                notice.classList.add('show');
                setTimeout(() => notice.classList.remove('show'), 3000);
                
                if (data.validation) {
                    updateValidationStatus(data.validation);
                }
            });
        }
        
        function testConnections() {
            const btn = event.target;
            btn.disabled = true;
            btn.textContent = '⏳ Testing...';
            
            fetch('/test-connections')
                .then(r => r.json())
                .then(data => {
                    let msg = 'Connection Test Results:\\n\\n';
                    msg += `Prowlarr: ${data.prowlarr ? '✅ Connected' : '❌ Failed'}\\n`;
                    msg += `SABnzbd: ${data.sabnzbd ? '✅ Connected' : '❌ Failed'}\\n`;
                    msg += `Plex: ${data.plex ? '✅ Connected' : '❌ Failed'}\\n`;
                    
                    if (data.errors && data.errors.length > 0) {
                        msg += '\\nErrors:\\n' + data.errors.join('\\n');
                    }
                    
                    alert(msg);
                })
                .finally(() => {
                    btn.disabled = false;
                    btn.textContent = '🔌 Test Connections';
                });
        }
        
        {% if status.running %}
        setTimeout(() => location.reload(), 3000);
        {% endif %}
    </script>
</body>
</html>
"""


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
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
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
            round_info = None
            for r in calendar.get("rounds", []):
                if r.get("round") == int(round_num):
                    round_info = r
                    break
            
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
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
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
    
    calendar_data = {}
    for year in SEASONS:
        calendar_data[str(year)] = get_rounds_for_season(year)
    
    return render_template_string(
        HTML_TEMPLATE,
        status=command_output,
        seasons=SEASONS,
        settings=settings,
        session_types=SESSION_TYPES,
        session_groups=SESSION_GROUPS,
        session_defaults=session_defaults,
        next_race=next_race,
        calendar_data=calendar_data
    )


@app.route("/run", methods=["POST"])
def run():
    if command_output["running"]:
        return jsonify({"error": "Command already running"}), 400
    
    data = request.json
    cmd = data.get("command", "status")
    
    if cmd.startswith("overlay "):
        overlay_cmd = cmd[8:]
        thread = threading.Thread(target=run_overlay_command, args=(overlay_cmd,))
    else:
        thread = threading.Thread(target=run_grabber_command, args=(cmd,))
    
    thread.start()
    return jsonify({"status": "started", "command": cmd})


@app.route("/settings", methods=["GET", "POST"])
def settings_route():
    if request.method == "POST":
        new_settings = request.json
        save_webui_settings(new_settings)
        update_grabber_config(new_settings)
        update_settings_yml(new_settings)
        return jsonify({"status": "saved"})
    else:
        return jsonify(load_webui_settings())


@app.route("/status")
def status():
    return jsonify(command_output)


@app.route("/library-status")
def library_status():
    season = request.args.get("season", "2025", type=int)
    round_num = request.args.get("round", "", type=str)
    round_filter = int(round_num) if round_num else None
    
    data = get_library_status(season, round_filter)
    
    for round_data in data["rounds"]:
        for session in round_data["sessions"]:
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
        
        return jsonify({
            "status": "saved" if success else "error",
            "validation": validation
        })
    else:
        reload_settings()
        settings = _load_yaml()
        validation = validate()
        
        return jsonify({
            **settings,
            "validation": validation
        })


@app.route("/test-connections")
def test_connections():
    """Test connections to Prowlarr, SABnzbd, and Plex."""
    import requests as req
    
    results = {
        "prowlarr": False,
        "sabnzbd": False,
        "plex": False,
        "errors": []
    }
    
    try:
        url = get_prowlarr_url()
        key = get_prowlarr_api_key()
        if url and key:
            r = req.get(f"{url}/api/v1/health", headers={"X-Api-Key": key}, timeout=5)
            results["prowlarr"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Prowlarr: {str(e)}")
    
    try:
        url = get_sabnzbd_url()
        key = get_sabnzbd_api_key()
        if url and key:
            r = req.get(f"{url}/api?mode=version&apikey={key}&output=json", timeout=5)
            results["sabnzbd"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"SABnzbd: {str(e)}")
    
    try:
        url = get_plex_url()
        token = get_plex_token()
        if url and token:
            r = req.get(f"{url}/?X-Plex-Token={token}", timeout=5)
            results["plex"] = r.status_code == 200
    except Exception as e:
        results["errors"].append(f"Plex: {str(e)}")
    
    return jsonify(results)


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
        "theme_color": "#ff6600",
        "orientation": "any",
        "icons": [
            {
                "src": "/assets/motogp_icon.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable"
            }
        ]
    })


@app.route("/assets/<path:filename>")
def static_files(filename):
    return send_from_directory(str(SCRIPT_DIR / "assets"), filename)


if __name__ == "__main__":
    print("=" * 50)
    print("  MotoGParr Web UI")
    print("  Open http://your-server-ip:5679")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5679, debug=False)