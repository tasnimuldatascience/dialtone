"""The daily chart, and the timezone that made it lie after 8pm.

FOUND IN A SCREENSHOT. The dashboard said "9 calls handled" in one panel and "0 in a fortnight"
directly beneath the chart in the next. Both numbers came from the same database, seconds apart.

`started_at` is UTC -- `_now()` makes sure of it -- and the window the chart is drawn over was
built from `date.today()`, which is local. West of Greenwich those disagree every evening: a call
placed at 21:00 local is stamped with tomorrow's UTC date, lands in a bucket the window does not
contain, and vanishes from the chart. In the morning it reappears, which is why nobody had ever
reproduced it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dialtone.store.db import _dense_days


def utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


class TestTheWindow:
    def test_it_ends_on_the_utc_day(self):
        """THE BUG. Rows are keyed by UTC date, so the window must be too, or today's calls fall
        outside a chart of the last fourteen days."""
        days = _dense_days([], days=14)
        assert days[-1]["day"] == utc_today()

    def test_todays_calls_appear(self):
        """The whole point: a call written now is visible on the chart now."""
        days = _dense_days([{"day": utc_today(), "calls": 9, "resolved": 8}], days=14)
        assert sum(d["calls"] for d in days) == 9
        assert days[-1]["calls"] == 9

    def test_it_is_contiguous_and_the_right_length(self):
        days = _dense_days([], days=14)
        assert len(days) == 14
        stamps = [datetime.fromisoformat(d["day"]).date() for d in days]
        # NOT strict=True: the two sequences are deliberately unequal -- this pairs each day with
        # the one after it, so the second is always one shorter.
        assert all(b - a == timedelta(days=1) for a, b in zip(stamps, stamps[1:], strict=False))

    def test_quiet_days_are_kept_as_zeroes(self):
        """A gap is information. Dropping empty days stretched one day of calls across the whole
        panel, which reads as "every call happened at once"."""
        days = _dense_days([{"day": utc_today(), "calls": 3, "resolved": 3}], days=14)
        assert [d["calls"] for d in days[:-1]] == [0] * 13

    def test_a_row_outside_the_window_is_not_counted_twice(self):
        old = (datetime.now(UTC).date() - timedelta(days=40)).isoformat()
        days = _dense_days([{"day": old, "calls": 5, "resolved": 5}], days=14)
        assert sum(d["calls"] for d in days) == 0
