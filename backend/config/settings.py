"""Portable settings bound by the selected controller before importing task owners."""
import os
from desktop.umaa_public_context import endpoint
ADB_HOST="127.0.0.1"
ADB_PORT=int(endpoint().rpartition(":")[2])
ADB_ENDPOINT=endpoint()
ADB_PATH=os.environ.get("UMA_ADB_PATH", "")
SCREEN_WIDTH=1440
SCREEN_HEIGHT=2560
SCREENSHOT_DIR="runs/screenshots"
TAP_DELAY=.3
SWIPE_DELAY=.5
ACTION_DELAY=1.0
LONG_WAIT=3.0
