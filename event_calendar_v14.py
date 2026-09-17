"""
═══════════════════════════════════════════════════════════════════════════
  event_calendar_v14.py — Known market-moving event days (IV-crush risk)
═══════════════════════════════════════════════════════════════════════════

  WHY
    Long premium gets destroyed by IV crush around scheduled events even when
    direction is right. The day-context gate penalises trading when it's an
    event day AND VIX is elevated. This module answers "is today an event day".

  ── SEMANTICS CHANGE (2026-09-06) — READ THIS ──────────────────────────────
    EVENTS now keys on the date the INDIAN SESSION actually feels the event,
    not the raw announcement date. The old keying mis-flagged two of the three
    event types:

      • RBI MPC   — announced ~10:00 IST on the decision day.
                    -> SAME-day intraday impact. Key = decision date.
      • US FOMC   — decision 14:00 ET = ~23:30 IST, after our market closed.
                    -> NEXT India session gaps on it. Key = following session.
      • India CPI — released 16:00 IST, after the 15:40 F&O close.
                    -> NEXT India session. Key = following session.

    Under the old keying the guard armed on a day the news had not landed yet,
    and was unguarded on the day that actually gapped. Raw announcement dates
    are preserved in the comment beside each entry.

  MAINTENANCE (quarterly — this file WILL go stale and fail silently)
    RBI MPC -> rbi.org.in | FOMC -> federalreserve.gov
    India CPI -> mospi.gov.in (12th monthly, 16:00 IST; next working day if
    the 12th is a holiday) | US CPI -> bls.gov/schedule
    If a date is missing the bot treats it as a normal day — the live VIX
    spike guard still applies, but the scheduled guard is blind.

  VERIFY BEFORE RELYING ON THESE LIVE. Compiled 2026-09-06 from public
  reporting of the official calendars, not scraped from source sites. The
  CPI weekend roll-forwards are computed, not checked against NSE holidays.
═══════════════════════════════════════════════════════════════════════════
"""

import datetime

# ── Event days, keyed by INDIAN SESSION IMPACT DATE ────────────────────────
EVENTS = {
    # ── RBI MPC (same-day: announced ~10:00 IST) ───────────────────────────
    "2026-10-07": "RBI MPC rate decision (same-day, ~10:00 IST)",
    "2026-12-04": "RBI MPC rate decision (same-day, ~10:00 IST)",

    # ── US FOMC (India session AFTER the decision) ─────────────────────────
    "2026-09-17": "US FOMC overnight gap (decision 2026-09-16, SEP/dot-plot)",
    "2026-10-29": "US FOMC overnight gap (decision 2026-10-28)",
    "2026-12-10": "US FOMC overnight gap (decision 2026-12-09, SEP/dot-plot)",

    # ── India CPI (released 16:00 IST -> next session reacts) ──────────────
    "2026-09-15": "India CPI reaction (released 2026-09-14; 12th was a Sat)",
    "2026-10-13": "India CPI reaction (released 2026-10-12)",
    "2026-11-13": "India CPI reaction (released 2026-11-12)",
    "2026-12-15": "India CPI reaction (released 2026-12-14; 12th was a Sat)",

    # ── US CPI ────────────────────────────────────────────────────────────
    # NOT POPULATED — dates were not verified at build time. Add from
    # bls.gov/schedule/news_release/cpi.htm. Released 08:30 ET (18:00 IST),
    # so the impact date is the NEXT India session, same rule as FOMC.

    # ── Union Budget — add when announced (historically Feb 1) ─────────────
}

# Historical entries kept so backtests over past sessions reproduce what the
# bot actually did. These use the OLD raw-date keying and are NOT re-mapped.
HISTORICAL_EVENTS = {
    "2026-06-05": "RBI MPC rate decision",
    "2026-08-06": "RBI MPC rate decision",
    "2026-06-17": "US FOMC decision",
    "2026-07-29": "US FOMC decision",
    "2026-06-12": "India CPI",
    "2026-07-13": "India CPI",
    "2026-06-10": "US CPI",
}


def _all_events():
    """Merged view. Current entries win on any key collision."""
    merged = dict(HISTORICAL_EVENTS)
    merged.update(EVENTS)
    return merged


def is_event_day(today: datetime.date | None = None):
    """Return (is_event: bool, label: str). Pure lookup, no network."""
    today = today or datetime.date.today()
    key = today.strftime("%Y-%m-%d")
    ev = _all_events()
    if key in ev:
        return True, ev[key]
    return False, ""


def is_event_tomorrow(today: datetime.date | None = None):
    """Useful to avoid holding into a gap. (bool, label)."""
    today = today or datetime.date.today()
    nxt = (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    ev = _all_events()
    if nxt in ev:
        return True, ev[nxt]
    return False, ""


def upcoming_events(today: datetime.date | None = None, days: int = 7):
    """List events within the next `days` days. For logging/alerts."""
    today = today or datetime.date.today()
    out = []
    for k, label in sorted(_all_events().items()):
        try:
            d = datetime.datetime.strptime(k, "%Y-%m-%d").date()
        except ValueError:
            continue
        delta = (d - today).days
        if 0 <= delta <= days:
            out.append((k, label, delta))
    return out


def calendar_health(today: datetime.date | None = None):
    """Is the calendar still current? Returns (ok: bool, message: str).

    [ADDED 2026-09-06] The calendar ran dry after 2026-08-06 and nothing
    noticed for a month — the event guard was silently blind the whole time.
    The bot calls this at startup and surfaces it in the Telegram banner so a
    stale calendar announces itself instead of failing quietly.
    """
    today = today or datetime.date.today()
    future = [k for k in EVENTS
              if datetime.datetime.strptime(k, "%Y-%m-%d").date() >= today]
    if not future:
        return False, "EVENT CALENDAR EXHAUSTED - no future events; guard is BLIND"
    last = max(datetime.datetime.strptime(k, "%Y-%m-%d").date() for k in future)
    days_left = (last - today).days
    if days_left < 30:
        return False, f"event calendar THIN: {len(future)} left, ends in {days_left}d"
    return True, f"event calendar ok: {len(future)} upcoming, through {last}"
