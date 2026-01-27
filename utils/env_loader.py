#!/usr/bin/env python3
"""
Environment Variable Loader (Legacy Compatibility)
===================================================

Provides backwards compatibility with old .env-based configuration.
New code should use config_loader.py instead.
"""

import os
from pathlib import Path
from typing import Optional

_env_loaded = False


def load_env(env_file: str = None) -> bool:
    """
    Load environment variables from .env file.
    
    Args:
        env_file: Path to .env file. If None, searches common locations.
    
    Returns:
        True if file was loaded, False otherwise.
    """
    global _env_loaded
    
    if _env_loaded:
        return True
    
    # Search paths
    search_paths = []
    
    if env_file:
        search_paths.append(Path(env_file))
    
    # Add common locations
    script_dir = Path(__file__).parent.parent
    search_paths.extend([
        script_dir / ".env",
        script_dir.parent / ".env",
        Path("/config/.env"),
        Path.home() / ".env",
    ])
    
    for path in search_paths:
        if path.exists():
            try:
                with open(path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, _, value = line.partition('=')
                            key = key.strip()
                            value = value.strip().strip('"').strip("'")
                            if key and key not in os.environ:
                                os.environ[key] = value
                _env_loaded = True
                return True
            except Exception:
                continue
    
    return False


def get_env(key: str, default: str = None, required: bool = False) -> Optional[str]:
    """
    Get environment variable value.
    
    Args:
        key: Environment variable name
        default: Default value if not found
        required: If True, raises error when not found
    
    Returns:
        Environment variable value or default
    
    Raises:
        ValueError: If required=True and variable not found
    """
    value = os.environ.get(key)
    
    if value is None or value == "":
        if required:
            raise ValueError(f"Required environment variable not set: {key}")
        return default
    
    return value