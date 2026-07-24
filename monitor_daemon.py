"""Backward-compatible launcher for WorkMonitor V2.

Use ``python main.py`` for the new entry point.  This module remains so an
existing shortcut that points to ``monitor_daemon.py`` continues to work.
"""

from main import run_app


if __name__ == "__main__":
    run_app()
