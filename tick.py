#!/usr/bin/env python3
"""Cron entry point: closes eligible Topics/Polls without a live web request.

Meant for hosts (like TigerTech shared hosting) where a background
thread inside the web process can't be relied on to keep running, so
Tick() is instead driven by a periodic cron job calling this script.
Prints nothing when nothing closes, per good cron etiquette; prints
one line per closed Topic when something does, so a MAILTO'd cron
digest is actually informative.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import config  # noqa: F401  (optional; sets os.environ — see config.example.py)
except ImportError:
    pass

import persistence
import tick_runner

DB_PATH = os.environ.get("MEETINGHOUSE_DB", os.path.join(os.path.dirname(__file__), "emeetinghouse.db"))


def main():
    conn = persistence.Connect(DB_PATH)
    house = persistence.LoadHouse(conn)
    closed = tick_runner.RunTick(house, conn, dt.datetime.utcnow())
    for topic in closed:
        print(f"{topic.subsection.value}: '{topic.title}' closed {topic.result.value}")


if __name__ == "__main__":
    main()
