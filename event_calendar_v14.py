"""
═══════════════════════════════════════════════════════════════════════════
  event_calendar_v14.py — Known market-moving event days (IV-crush risk)
═══════════════════════════════════════════════════════════════════════════

  WHY
    Long premium gets destroyed by IV crush around scheduled events even when
    direction is right. The day-context gate penalises trading when it's an
    event day AND VIX is elevated. This module answers "is today an event day".

  HOW IT WORKS
    • A hardcoded EVENTS dict you keep updated (RBI/Fed/Budget/CPI/expiry-week).
    • is_event_day(date) -> (bool, label).
    • Dates are easy to maintain: add the next quarter's known dates.

  MAINTENANCE
    RBI MPC, US FOMC, India CPI, Union Budget, and major US data are scheduled
    well in advance. Update EVENTS each quarter from the official calendars:
      • RBI MPC:  rbi.org.in (Monetary Policy Committee meeting schedule)
      • FOMC:     federalreserve.gov (FOMC meeting calendar)
      • India CPI/IIP: mospi.gov.in
    If a date is missing, the bot simply treats it as a normal day — the VIX
    spike guard still provides a live safety net.
═══════════════════════════════════════════════════════════════════════════
"""

import datetime

# ── Known high-impact event dates. Format: "YYYY-MM-DD": "label" ───────────
# NOTE: maintained manually each quarter. These are EXAMPLES for the current
# window — verify against official sources before relying on them live.
EVENTS = {
    # RBI Monetary Policy Committee (rate decision — big Nifty/VIX mover)
    "2026-06-05": "RBI MPC rate decision",
    "2026-08-06": "RBI MPC rate decision",
    # US FOMC (overnight gap risk for next-day India open)
    "2026-06-17": "US FOMC decision",
    "2026-07-29": "US FOMC decision",
    # India CPI inflation print
    "2026-06-12": "India CPI",
    "2026-07-13": "India CPI",
    # US CPI (drives global risk / IV)
    "2026-06-10": "US CPI",
    # Union Budget (if applicable in window)
    # "2026-02-01": "Union Budget",
}


def is_event_day(today: datetime.date | None = None):
    """Return (is_event: bool, label: str). Pure lookup, no network."""
    today = today or datetime.date.today()
    key = today.strftime("%Y-%m-%d")
    if key in EVENTS:
        return True, EVENTS[key]
    return False, ""


def is_event_tomorrow(today: datetime.date | None = None):
    """Useful to avoid holding overnight into an event. (bool, label)."""
    today = today or datetime.date.today()
    nxt = (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if nxt in EVENTS:
        return True, EVENTS[nxt]
    return False, ""


def upcoming_events(today: datetime.date | None = None, days: int = 7):
    """List events within the next `days` days. For logging/alerts."""
    today = today or datetime.date.today()
    out = []
    for k, label in sorted(EVENTS.items()):
        try:
            d = datetime.datetime.strptime(k, "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= days:
            out.append((k, label, delta))
    return out
