#!/usr/bin/env python3
"""calendar_event -- writes a standard .ics file into <repo>/knowledge/calendar/
(design spec lists Google Calendar MCP "when authenticated" as a future
destination; not wired up this session -- no verified auth path). RISK
inert: an .ics file executes nothing."""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import register

TYPE = "calendar_event"
RISK = "inert"
SCHEMA = {
    "required": ["title", "date"],
    "types": {"title": str, "date": str},
}
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CALENDAR_ROOT = REPO_ROOT / "knowledge" / "calendar"


def _slug(title):
    return re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-") or "event"


def target_path(payload):
    return CALENDAR_ROOT / f"{_slug(payload['title'])}.ics"


def describe(payload):
    time_part = f" {payload['time']}" if payload.get("time") else ""
    return f"Calendar event: {payload['title']} on {payload['date']}{time_part}"


def _build_ics(payload):
    dt = payload["date"].replace("-", "")
    time_part = (payload.get("time") or "000000").replace(":", "")[:6].ljust(6, "0")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//instagram-to-value//captured-event//EN",
        "BEGIN:VEVENT", f"UID:{_slug(payload['title'])}@instagram-to-value",
        f"DTSTAMP:{stamp}", f"DTSTART:{dt}T{time_part}", f"SUMMARY:{payload['title']}",
    ]
    if payload.get("location"):
        lines.append(f"LOCATION:{payload['location']}")
    if payload.get("description"):
        lines.append(f"DESCRIPTION:{payload['description']}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def preview(payload):
    return _build_ics(payload)


def collides(payload):
    target = target_path(payload)
    return target if target.exists() else None


def install(payload, target, context=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_build_ics(payload))
    return {"ok": True, "path": str(target), "command": None, "exit_code": None, "output": None, "error": None}


register(sys.modules[__name__])
