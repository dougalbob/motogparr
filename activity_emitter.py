#!/usr/bin/env python3
"""
Activity Emitter Module for MotoGParr
=====================================

Provides a simple API for all scripts to emit activity events.
Events are stored in a shared JSON file and read by the WebUI.

Usage:
    from activity_emitter import emit_activity, ActivityStage
    
    emit_activity(
        episode_key="2025_R01_E05",
        episode_name="Moto3 Free Practice 1",
        stage=ActivityStage.SEARCHING,
        details="Round 1 Thailand",
        round_info={"round": 1, "venue": "Thailand"}
    )
"""

import json
import fcntl
import os
from pathlib import Path
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List, Any

# Configuration
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
ACTIVITY_FILE = CONFIG_DIR / "activity_events.json"
MAX_COMPLETED_HISTORY = 10


class ActivityStage(Enum):
    """Stages in the download lifecycle."""
    SEARCHING = "searching"
    CANDIDATES_FOUND = "candidates_found"
    DOWNLOADING = "downloading"
    DOWNLOAD_COMPLETE = "download_complete"
    PROCESSING = "processing"
    METADATA = "metadata"
    COMPLETE = "complete"
    ERROR = "error"


# Stage display configuration
STAGE_CONFIG = {
    ActivityStage.SEARCHING: {"icon": "🔍", "color": "#52aad8", "label": "Searching"},
    ActivityStage.CANDIDATES_FOUND: {"icon": "📋", "color": "#52aad8", "label": "Found"},
    ActivityStage.DOWNLOADING: {"icon": "⬇️", "color": "#f39c12", "label": "Downloading"},
    ActivityStage.DOWNLOAD_COMPLETE: {"icon": "📦", "color": "#f1c40f", "label": "Downloaded"},
    ActivityStage.PROCESSING: {"icon": "⚙️", "color": "#f1c40f", "label": "Processing"},
    ActivityStage.METADATA: {"icon": "🎬", "color": "#f1c40f", "label": "Metadata"},
    ActivityStage.COMPLETE: {"icon": "✅", "color": "#27ae60", "label": "Complete"},
    ActivityStage.ERROR: {"icon": "❌", "color": "#e74c3c", "label": "Error"},
}


def _load_activity_data() -> Dict[str, Any]:
    """Load activity data from JSON file."""
    if not ACTIVITY_FILE.exists():
        return {"events": {}, "completed_history": []}
    
    try:
        with open(ACTIVITY_FILE, 'r') as f:
            data = json.load(f)
            # Ensure required keys exist
            if "events" not in data:
                data["events"] = {}
            if "completed_history" not in data:
                data["completed_history"] = []
            return data
    except (json.JSONDecodeError, IOError):
        return {"events": {}, "completed_history": []}


def _save_activity_data(data: Dict[str, Any]) -> None:
    """Save activity data to JSON file with file locking."""
    # Ensure config directory exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    # Write to temp file first, then rename (atomic operation)
    temp_file = ACTIVITY_FILE.with_suffix('.json.tmp')
    
    try:
        with open(temp_file, 'w') as f:
            # Acquire exclusive lock
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2, default=str)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        # Atomic rename
        temp_file.rename(ACTIVITY_FILE)
    except IOError as e:
        print(f"Error saving activity data: {e}")
        if temp_file.exists():
            temp_file.unlink()


def emit_activity(
    episode_key: str,
    episode_name: str,
    stage: ActivityStage,
    details: Optional[str] = None,
    round_info: Optional[Dict] = None,
    progress: Optional[int] = None,
    error_message: Optional[str] = None,
    release_name: Optional[str] = None,
    nzo_id: Optional[str] = None
) -> None:
    """
    Emit an activity event for the feed.
    
    Args:
        episode_key: Unique identifier (e.g., "2025_R01_E05")
        episode_name: Human-readable name (e.g., "Moto3 Free Practice 1")
        stage: Current stage in the lifecycle
        details: Optional status details
        round_info: Optional dict with round/venue info
        progress: Optional download progress (0-100)
        error_message: Optional error message if stage is ERROR
        release_name: Optional release/filename being processed
        nzo_id: Optional SABnzbd job ID for tracking downloads
    """
    data = _load_activity_data()
    
    # Build event object
    stage_config = STAGE_CONFIG.get(stage, {})
    
    event = {
        "id": episode_key,
        "name": episode_name,
        "stage": stage.value,
        "stage_icon": stage_config.get("icon", "❓"),
        "stage_color": stage_config.get("color", "#666"),
        "stage_label": stage_config.get("label", stage.value),
        "details": details,
        "timestamp": datetime.now().isoformat(),
        "round": round_info.get("round") if round_info else None,
        "venue": round_info.get("venue") if round_info else None,
        "progress": progress,
        "error_message": error_message,
        "release_name": release_name,
        "nzo_id": nzo_id,
    }
    
    # Handle completion - move to history
    if stage in (ActivityStage.COMPLETE, ActivityStage.ERROR):
        # Remove from active events if present
        if episode_key in data["events"]:
            del data["events"][episode_key]
        
        # Add to completed history
        data["completed_history"].insert(0, event)
        
        # Trim history to max size
        data["completed_history"] = data["completed_history"][:MAX_COMPLETED_HISTORY]
    else:
        # Update or create in active events
        data["events"][episode_key] = event
    
    _save_activity_data(data)


def update_download_progress(
    episode_key: str,
    progress: int,
    details: Optional[str] = None
) -> None:
    """
    Update download progress for an existing event.
    
    Args:
        episode_key: The episode to update
        progress: Download progress (0-100)
        details: Optional updated details string
    """
    data = _load_activity_data()
    
    if episode_key in data["events"]:
        data["events"][episode_key]["progress"] = progress
        data["events"][episode_key]["timestamp"] = datetime.now().isoformat()
        if details:
            data["events"][episode_key]["details"] = details
        _save_activity_data(data)


def mark_complete(
    episode_key: str,
    details: Optional[str] = "Ready in Plex"
) -> None:
    """
    Mark an episode as complete and move to history.
    
    Args:
        episode_key: The episode to mark complete
        details: Completion message
    """
    data = _load_activity_data()
    
    if episode_key in data["events"]:
        event = data["events"][episode_key]
        # Update to complete stage
        emit_activity(
            episode_key=episode_key,
            episode_name=event.get("name", "Unknown"),
            stage=ActivityStage.COMPLETE,
            details=details,
            round_info={"round": event.get("round"), "venue": event.get("venue")}
        )


def mark_error(
    episode_key: str,
    error_message: str,
    episode_name: Optional[str] = None
) -> None:
    """
    Mark an episode as failed and move to history.
    
    Args:
        episode_key: The episode that failed
        error_message: Description of the error
        episode_name: Episode name (optional, will try to get from existing event)
    """
    data = _load_activity_data()
    
    # Try to get existing event info
    existing = data["events"].get(episode_key, {})
    name = episode_name or existing.get("name", "Unknown Episode")
    round_num = existing.get("round")
    venue = existing.get("venue")
    
    emit_activity(
        episode_key=episode_key,
        episode_name=name,
        stage=ActivityStage.ERROR,
        details="Failed",
        error_message=error_message,
        round_info={"round": round_num, "venue": venue} if round_num else None
    )


def clear_episode(episode_key: str) -> None:
    """
    Remove an episode from active events without adding to history.
    
    Args:
        episode_key: The episode to remove
    """
    data = _load_activity_data()
    
    if episode_key in data["events"]:
        del data["events"][episode_key]
        _save_activity_data(data)


def get_active_events() -> Dict[str, Any]:
    """
    Get all currently active events.
    
    Returns:
        Dict of active events keyed by episode_key
    """
    data = _load_activity_data()
    return data.get("events", {})


def get_completed_history(count: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Get recent completed/error events.
    
    Args:
        count: Max number of events to return (default: all up to MAX_COMPLETED_HISTORY)
    
    Returns:
        List of completed events, most recent first
    """
    data = _load_activity_data()
    history = data.get("completed_history", [])
    
    if count:
        return history[:count]
    return history


def get_all_activity() -> Dict[str, Any]:
    """
    Get all activity data for WebUI display.
    
    Returns:
        Dict with 'active' (list) and 'history' (list) keys
    """
    data = _load_activity_data()
    
    # Convert events dict to sorted list (most recent first)
    active_list = sorted(
        data.get("events", {}).values(),
        key=lambda x: x.get("timestamp", ""),
        reverse=True
    )
    
    return {
        "active": active_list,
        "history": data.get("completed_history", [])
    }


def clear_all_activity() -> None:
    """Clear all activity data. Use with caution."""
    _save_activity_data({"events": {}, "completed_history": []})


# For testing
if __name__ == "__main__":
    print("Testing activity_emitter...")
    
    # Test emit
    emit_activity(
        episode_key="2025_R01_E05",
        episode_name="Moto3 Free Practice 1",
        stage=ActivityStage.SEARCHING,
        details="Round 1 Thailand",
        round_info={"round": 1, "venue": "Thailand"}
    )
    print("✅ Emitted SEARCHING event")
    
    # Test get
    activity = get_all_activity()
    print(f"✅ Active events: {len(activity['active'])}")
    print(f"✅ History events: {len(activity['history'])}")
    
    # Show event
    if activity['active']:
        print(f"   Latest: {activity['active'][0]}")
    
    print("\n✅ activity_emitter module is working!")
