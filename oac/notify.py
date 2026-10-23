"""Completion notifications: terminal bell and optional desktop notification."""
from __future__ import annotations

import shutil
import subprocess
import sys


def _bell() -> None:
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _desktop(message: str) -> bool:
    if shutil.which("notify-send"):
        command = ["notify-send", "-a", "oac", "oac", message]
    elif shutil.which("osascript"):
        command = ["osascript", "-e", f'display notification "{message}" with title "oac"']
    elif shutil.which("termux-notification"):
        command = ["termux-notification", "--title", "oac", "--content", message]
    else:
        return False
    try:
        subprocess.run(command, check=False, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def notify_done(mode: str, message: str = "Task finished") -> None:
    """Fire the configured notification: off | bell | desktop | all."""
    if mode in {"bell", "all"}:
        _bell()
    if mode in {"desktop", "all"}:
        _desktop(message)
