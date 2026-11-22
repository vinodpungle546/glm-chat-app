#!/usr/bin/env python3
"""Turn relative dates in meeting notes ("tomorrow", "next Friday", "end of the week") into
actual dates, based on the meeting date. Output format: "20 Nov 2026".

Conventions (also stated in skills/meeting-notes/SKILL.md):
  "Friday", "this Friday", "by Friday", "on Friday"  → the next Friday after the meeting
                                                       (a week later if the meeting is on a Friday)
  "next Friday"  → the Friday of the following week (calendar weeks start on Monday)
  "end of (the) week" / "EOW"  → that week's Friday;  "end of (the) month" / "EOM" → last day
  "next week"  → Monday of the following week ("w/c 23 Nov 2026")
  "today", "EOD", "tonight" → the meeting date; "tomorrow"; "in 3 days"; "in 2 weeks"
Anything else ("TBD", "end of sprint", "Q1") is returned unchanged.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "a": 1, "an": 1}


def fmt(d: date) -> str:
    """A date as '21 Nov 2026'."""
    return f"{d.day} {calendar.month_abbr[d.month]} {d.year}"


def _weekday(word: str) -> int:
    """Weekday number (Monday = 0) for a weekday name or abbreviation such as 'Tue'."""
    word = word.lower()
    return next(i for i, w in enumerate(WEEKDAYS) if w.startswith(word[:3]))


def _next_weekday(base: date, wd: int) -> date:
    """The next date that falls on weekday `wd` after `base` (a week later when `base` already is that weekday)."""
    days = (wd - base.weekday()) % 7
    return base + timedelta(days=days or 7)


def resolve(text: str, meeting: date) -> str:
    """The date `text` refers to, formatted; unchanged when it isn't a recognisable date."""
    original = (text or "").strip()
    if not original:
        return original
    t = original.lower().strip(" .")
    t = re.sub(r"^(?:by|on|before|until|due|due by|no later than|latest by|for)\s+", "", t)
    t = re.sub(r"^the\s+(?=end of)", "", t)
    t = re.sub(r"\s+(?:eod|cob|end of day|morning|afternoon|evening)$", "", t) if not re.fullmatch(r"(eod|cob|end of day)", t) else t

    if t in ("today", "tonight", "eod", "cob", "end of day", "end of today", "this afternoon", "this evening"):
        return fmt(meeting)
    if t in ("tomorrow", "tmrw", "tomorrow morning", "tomorrow eod"):
        return fmt(meeting + timedelta(days=1))
    if t in ("day after tomorrow", "the day after tomorrow"):
        return fmt(meeting + timedelta(days=2))
    if t in ("end of week", "end of the week", "eow", "this week", "end of this week"):
        friday = meeting + timedelta(days=(4 - meeting.weekday()) % 7)
        return fmt(friday)
    if t in ("next week", "early next week", "beginning of next week"):
        return "w/c " + fmt(meeting + timedelta(days=7 - meeting.weekday()))
    if t in ("end of next week",):
        return fmt(meeting + timedelta(days=7 - meeting.weekday() + 4))
    if t in ("end of month", "end of the month", "eom", "end of this month"):
        last = calendar.monthrange(meeting.year, meeting.month)[1]
        return fmt(meeting.replace(day=last))
    if t in ("next month",):
        first = (meeting.replace(day=1) + timedelta(days=32)).replace(day=1)
        return calendar.month_name[first.month] + f" {first.year}"

    m = re.fullmatch(r"in (\d+|one|two|three|four|five|six|seven|a|an) (day|days|week|weeks)", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]
        return fmt(meeting + timedelta(days=n * (7 if m.group(2).startswith("week") else 1)))
    m = re.fullmatch(rf"next {_WD}", t)
    if m:
        wd = _weekday(m.group(1))
        start_next_week = meeting + timedelta(days=7 - meeting.weekday())
        return fmt(start_next_week + timedelta(days=wd))
    m = re.fullmatch(rf"(?:this |coming |this coming )?{_WD}", t)
    if m:
        return fmt(_next_weekday(meeting, _weekday(m.group(1))))

    # A time on its own ("14:00", "2 pm", "by 2 o'clock") is that time on the meeting day.
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|o'clock)?", t)
    if m and (m.group(2) or m.group(3)):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        elif m.group(3) == "o'clock" and hour < 8:
            hour += 12  # "two o'clock" in a working day
        if hour < 24 and minute < 60:
            return f"{fmt(meeting)}, {hour:02d}:{minute:02d}"

    # Absolute dates in other formats → the same format
    for pattern in (r"(\d{4})-(\d{1,2})-(\d{1,2})",):
        m = re.fullmatch(pattern, t)
        if m:
            try:
                return fmt(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                return original
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3,9})\.?,? ?(\d{4})?", t) or None
    month_first = re.fullmatch(r"([a-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?,? ?(\d{4})?", t)
    if m or month_first:
        day_s, mon_s, year_s = (m.group(1), m.group(2), m.group(3)) if m else (month_first.group(2), month_first.group(1), month_first.group(3))
        month = MONTHS.get(mon_s[:3])
        if month:
            year = int(year_s) if year_s else meeting.year
            try:
                d = date(year, month, int(day_s))
            except ValueError:
                return original
            if not year_s and d < meeting - timedelta(days=60):
                d = d.replace(year=year + 1)  # "5 Jan" said in November
            return fmt(d)
    return original


def meeting_date(value: str | None) -> date:
    """The meeting's date from an ISO timestamp (falls back to today)."""
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except (TypeError, ValueError):
        return date.today()
