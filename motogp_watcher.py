#!/usr/bin/env python3
"""
MotoGP Watcher - Monitor download folder for completed MotoGP downloads
=========================================================================

Watches /downloads/complete/motogp/ for new folders and triggers sorting.
Runs as part of the scheduler, eliminating SABnzbd post-processing dependency.

This replaces the SABnzbd post-script approach with a clean folder watcher.
"""

import os
import sys
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Set, Dict, Optional, Callable

logger = logging.getLogger(__name__)


class DownloadWatcher:
    """
    Watches a directory for new completed downloads.
    
    Features:
    - Detects new folders appearing in watch directory
    - Waits for folder to be "stable" (no changes for X seconds)
    - Calls processor function when ready
    - Tracks processed folders to avoid reprocessing
    - Thread-safe for running alongside scheduler
    """
    
    def __init__(
        self,
        watch_dir: str,
        processor: Callable[[str], bool],
        stable_seconds: int = 15,
        check_interval: int = 10,
        processed_state_file: Optional[str] = None
    ):
        """
        Initialize the watcher.
        
        Args:
            watch_dir: Directory to monitor for new folders
            processor: Function to call with folder path when ready
            stable_seconds: Seconds folder must be unchanged before processing
            check_interval: Seconds between directory scans
            processed_state_file: Optional file to persist processed folders
        """
        self.watch_dir = Path(watch_dir)
        self.processor = processor
        self.stable_seconds = stable_seconds
        self.check_interval = check_interval
        self.processed_state_file = processed_state_file
        
        # State tracking
        self.processed: Set[str] = set()
        self.pending: Dict[str, datetime] = {}  # folder -> first_seen_time
        self.folder_sizes: Dict[str, int] = {}  # folder -> last_known_size
        
        # Thread control
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Load previously processed if state file exists
        self._load_processed_state()
    
    def _load_processed_state(self):
        """Load processed folders from state file."""
        if self.processed_state_file and os.path.exists(self.processed_state_file):
            try:
                with open(self.processed_state_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.processed.add(line)
                logger.debug(f"Loaded {len(self.processed)} processed folders from state")
            except Exception as e:
                logger.warning(f"Could not load processed state: {e}")
    
    def _save_processed_state(self):
        """Save processed folders to state file."""
        if self.processed_state_file:
            try:
                os.makedirs(os.path.dirname(self.processed_state_file), exist_ok=True)
                with open(self.processed_state_file, 'w') as f:
                    for folder in sorted(self.processed):
                        f.write(folder + '\n')
            except Exception as e:
                logger.warning(f"Could not save processed state: {e}")
    
    def _get_folder_size(self, folder_path: Path) -> int:
        """Get total size of all files in folder."""
        total = 0
        try:
            for item in folder_path.rglob('*'):
                if item.is_file():
                    total += item.stat().st_size
        except Exception:
            pass
        return total
    
    def _is_folder_stable(self, folder_path: Path) -> bool:
        """Check if folder has stopped changing (download complete)."""
        folder_str = str(folder_path)
        current_size = self._get_folder_size(folder_path)
        
        # First time seeing this folder
        if folder_str not in self.folder_sizes:
            self.folder_sizes[folder_str] = current_size
            self.pending[folder_str] = datetime.now()
            return False
        
        # Size changed - reset timer
        if current_size != self.folder_sizes[folder_str]:
            self.folder_sizes[folder_str] = current_size
            self.pending[folder_str] = datetime.now()
            return False
        
        # Size unchanged - check if stable long enough
        first_seen = self.pending.get(folder_str, datetime.now())
        stable_time = (datetime.now() - first_seen).total_seconds()
        
        return stable_time >= self.stable_seconds
    
    def _has_media_files(self, folder_path: Path) -> bool:
        """Check if folder contains media files."""
        media_extensions = {'.mkv', '.mp4', '.avi', '.m4v', '.wmv', '.ts', '.m2ts'}
        
        try:
            for item in folder_path.rglob('*'):
                if item.is_file() and item.suffix.lower() in media_extensions:
                    return True
        except Exception:
            pass
        
        return False
    
    def _scan_directory(self):
        """Scan watch directory for new folders to process."""
        if not self.watch_dir.exists():
            logger.debug(f"Watch directory does not exist: {self.watch_dir}")
            return
        
        try:
            for item in self.watch_dir.iterdir():
                if not item.is_dir():
                    continue
                
                folder_name = item.name
                folder_str = str(item)
                
                # Skip already processed
                if folder_name in self.processed:
                    continue
                
                # Skip hidden/temp folders
                if folder_name.startswith('.') or folder_name.startswith('_'):
                    continue
                
                # Check if has media files
                if not self._has_media_files(item):
                    logger.debug(f"No media files in {folder_name}, skipping")
                    continue
                
                # Check if stable (download complete)
                if not self._is_folder_stable(item):
                    if folder_str in self.pending:
                        age = (datetime.now() - self.pending[folder_str]).total_seconds()
                        logger.debug(f"Waiting for {folder_name} to stabilize ({age:.0f}s)")
                    continue
                
                # Ready to process
                logger.info(f"📂 New download ready: {folder_name}")
                
                try:
                    success = self.processor(folder_str)
                    
                    if success:
                        logger.info(f"✅ Successfully processed: {folder_name}")
                        self.processed.add(folder_name)
                        self._save_processed_state()
                    else:
                        logger.warning(f"⚠️ Processing returned False: {folder_name}")
                        # Still mark as processed to avoid infinite retries
                        self.processed.add(folder_name)
                        self._save_processed_state()
                
                except Exception as e:
                    logger.error(f"❌ Error processing {folder_name}: {e}")
                
                # Clean up pending state
                self.pending.pop(folder_str, None)
                self.folder_sizes.pop(folder_str, None)
        
        except Exception as e:
            logger.error(f"Error scanning directory: {e}")
    
    def _run_loop(self):
        """Main watcher loop."""
        logger.info(f"👁️ Watcher started: {self.watch_dir}")
        logger.info(f"   Stable time: {self.stable_seconds}s, Check interval: {self.check_interval}s")
        
        while self._running:
            self._scan_directory()
            
            # Sleep in small chunks for responsive shutdown
            for _ in range(self.check_interval):
                if not self._running:
                    break
                time.sleep(1)
        
        logger.info("👁️ Watcher stopped")
    
    def start(self):
        """Start the watcher in a background thread."""
        if self._running:
            logger.warning("Watcher already running")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop the watcher."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
    
    def run_once(self):
        """Run a single scan (for testing or manual trigger)."""
        self._scan_directory()
    
    def clear_processed(self, folder_name: str = None):
        """Clear processed state for reprocessing."""
        if folder_name:
            self.processed.discard(folder_name)
        else:
            self.processed.clear()
        self._save_processed_state()


def create_watcher(
    watch_dir: str,
    processor: Callable[[str], bool],
    config_dir: str = None
) -> DownloadWatcher:
    """
    Factory function to create a configured watcher.
    
    Args:
        watch_dir: Directory to watch
        processor: Function to process folders
        config_dir: Directory for state file (optional)
    
    Returns:
        Configured DownloadWatcher instance
    """
    state_file = None
    if config_dir:
        state_file = os.path.join(config_dir, "watcher_processed.txt")
    
    return DownloadWatcher(
        watch_dir=watch_dir,
        processor=processor,
        stable_seconds=15,
        check_interval=10,
        processed_state_file=state_file
    )


# =============================================================================
# STANDALONE TESTING
# =============================================================================

if __name__ == "__main__":
    import argparse
    
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    
    parser = argparse.ArgumentParser(description="MotoGP Download Watcher")
    parser.add_argument("--watch-dir", "-w", default="/downloads/complete/motogp",
                        help="Directory to watch")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Don't actually process, just log")
    args = parser.parse_args()
    
    def test_processor(folder_path: str) -> bool:
        logger.info(f"Would process: {folder_path}")
        return True
    
    watcher = create_watcher(
        watch_dir=args.watch_dir,
        processor=test_processor,
    )
    
    logger.info("Running single scan...")
    watcher.run_once()
    logger.info("Done.")