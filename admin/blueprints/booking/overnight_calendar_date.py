"""Session anchor date → calendar date for overnight availability (wrap past midnight)."""

from __future__ import annotations

from datetime import date, timedelta


def calendar_date_for_overnight_slot(
    session_anchor_date: date,
    hour: int,
    minute: int,
    avail_start_hhmm: str,
    avail_end_hhmm: str,
) -> date:
    """
    Map webform session anchor date + wall clock to the calendar date used for instants.

    When available hours wrap past midnight (e.g. 3pm–3am), post-midnight options on the form
    still use the *session* date but those wall times are on the following civil day.
    """

    def _hm_to_minutes(x: str) -> int:
        p = x.split(":")
        return int(p[0]) * 60 + int(p[1])

    start_m = _hm_to_minutes(avail_start_hhmm)
    end_m = _hm_to_minutes(avail_end_hhmm)
    t_m = hour * 60 + minute
    if end_m < start_m and t_m <= end_m:
        return session_anchor_date + timedelta(days=1)
    return session_anchor_date
