"""Tokenometr's limit math: what a token weighs, when a limit's week begins, and what is left of it.

The Mac app counts its own transcripts with it, and the team site counts everybody's, so both load this
file as it is. It knows nothing of files or machines: numbers and moments in, numbers out. Python 3.9
(the system one on a Mac) and the standard library only.
"""
import bisect
import itertools
import time
from datetime import datetime, timedelta

# Effective cost of a token relative to a plain input token: re-reading the cached
# conversation is cheap, writing it to the cache costs more, Claude's answer costs most.
WEIGHTS = (1.0, 2.0, 0.1, 5.0)  # input, cache write, cache read, output
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK_RESET = (6, 9, 0)  # unless configured, a weekly limit resets on Sunday at 9:00 local time
MIN_CALIBRATION_PERCENT = 10  # the weekly limit has to move this far before its size can be estimated
WEEK_SECONDS = 7 * 24 * 3600


def weigh(tokens):
    """The weight of one call's tokens: (input, cache write, cache read, output)."""
    return sum(n * w for n, w in zip(tokens, WEIGHTS))


def local_ts(day, hour, minute=0, tz=None):
    """The moment of `hour`:`minute` on `day` in `tz`, in the machine's own zone without one."""
    if tz is None:
        # mktime picks the right DST offset for that particular day
        return time.mktime((day.year, day.month, day.day, hour, minute, 0, 0, 0, -1))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp()


def parse_reset(text):
    """'tue 18:00' -> (1, 18, 0), or None when it can't be read."""
    try:
        day, clock = text.split()
        hour, minute = (int(part) for part in clock.split(":"))
        return WEEKDAYS.index(day.lower()[:3]), hour, minute
    except (AttributeError, ValueError):
        return None


def week_bounds(now, reset=WEEK_RESET, tz=None):
    """When the current week of a limit that resets at `reset` (weekday, hour, minute, in `tz`) began, and when it resets next."""
    weekday, hour, minute = reset
    today = now.astimezone(tz).date()
    reset_day = today - timedelta(days=(today.weekday() - weekday) % 7)
    if local_ts(reset_day, hour, minute, tz) > now.timestamp():
        reset_day -= timedelta(days=7)
    return local_ts(reset_day, hour, minute, tz), local_ts(reset_day + timedelta(days=7), hour, minute, tz)


def week_around(resets_at, at):
    """(start, end) of the limit week around the moment `at`, from one reset the CLI reported: weeks are 7 days apart."""
    while resets_at <= at:
        resets_at += WEEK_SECONDS
    while resets_at - WEEK_SECONDS > at:
        resets_at -= WEEK_SECONDS
    return resets_at - WEEK_SECONDS, resets_at


def period_starts(now, first_call, week_start, tz=None):
    """Where the periods begin: the last 5 hours, today in `tz` (the machine's zone without one), the week, ever."""
    today = now.astimezone(tz).date()
    return {"5h": now.timestamp() - 5 * 3600, "today": local_ts(today, 0, 0, tz), "week": week_start, "all": first_call}


def spending(calls):
    """spent(t0, t1): weighted tokens of `calls` [(time, weighted)] made after t0 and up to t1."""
    calls = sorted(calls)
    times = [t for t, _ in calls]
    totals = list(itertools.accumulate(w for _, w in calls))

    def spent(t0, t1):
        i, j = bisect.bisect_right(times, t0), bisect.bisect_right(times, t1)
        return (totals[j - 1] if j else 0.0) - (totals[i - 1] if i else 0.0)
    return spent


def calibrate(readings, spent_of, week_of):
    """Weighted tokens per 1 % of the weekly limit, from how far the official readings moved; None if unknown.

    Each account's consecutive readings are compared with the tokens that account spent on this Mac in between
    (`spent_of(account)`). Intervals that span the account's weekly reset (`week_of(account, moment)`) or in
    which the account wasn't used here say nothing about the rate and are left out.
    """
    by_account = {}
    for reading in readings:
        by_account.setdefault(reading.get("org"), []).append(reading)
    tokens = percent = 0.0
    for org, own in by_account.items():
        spent = spent_of(org)
        for a, b in zip(own, own[1:]):
            before, after = a["u"].get("sd"), b["u"].get("sd")
            if before is None or after is None or after < before:
                continue  # a reset in between, or no weekly reading
            start, end = a["t"] / 1000, b["t"] / 1000
            if week_of(org, datetime.fromtimestamp(end).astimezone())[0] > start:
                continue  # the week reset in between, even if the reading did not drop
            here = spent(start, end)
            if here <= 0:
                continue  # the account was in use elsewhere
            tokens += here
            percent += after - before
    if percent < MIN_CALIBRATION_PERCENT or tokens <= 0:
        return None
    return tokens / percent


def account_bank(readings, per_percent, spent, now_ts, week_start, resets_at):
    """What is left of one account's weekly limit: its latest reading this week plus what it spent after it.

    `readings` are that account's readings, and `spent` counts only that account's calls. The part of
    the latest reading this Mac's transcripts don't explain was spent elsewhere: cloud tasks, ordinary
    chats, other devices.
    """
    if per_percent is None:
        return None
    readings = [r for r in readings if r["u"].get("sd") is not None]
    reading = readings[-1] if readings else None
    if reading and reading["t"] / 1000 >= week_start:
        reading_at, reading_percent = reading["t"] / 1000, reading["u"]["sd"]
        used_percent = reading_percent + spent(reading_at, now_ts) / per_percent
        off_mac = max(0.0, reading_percent - spent(week_start, reading_at) / per_percent)
    else:
        reading_at = reading_percent = off_mac = None
        used_percent = spent(week_start, now_ts) / per_percent
    used_percent = min(used_percent, 100.0)
    return {
        "total": 100 * per_percent,
        "remaining": (100 - used_percent) * per_percent,
        "usedPercent": used_percent,
        "perPercent": per_percent,
        "readingAt": reading_at,
        "readingPercent": reading_percent,
        "offMacPercent": off_mac,
        "offMacTokens": None if off_mac is None else off_mac * per_percent,
        "resetsAt": resets_at,
    }
