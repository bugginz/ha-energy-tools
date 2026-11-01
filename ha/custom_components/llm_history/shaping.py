"""Pure helpers that turn recorder data into compact, chart-ready tool results.

No Home Assistant imports, so tests/test_llm_history.py can exercise this with
stdlib unittest on the Mac. Timestamps are float epoch seconds throughout and
only become ISO strings (in HA's local zone) at the very end, in format_rows.
"""

import math
import re
from datetime import datetime, timedelta

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([mhdwy])$")
_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 365 * 86400}
_NOT_A_VALUE = {"unknown", "unavailable", "none", ""}

# Bucket sizes a chart axis reads naturally; past a day we use whole days.
NICE_BUCKETS = (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def parse_when(value, now, tz):
    """'now', a duration ago ('90m', '24h', '7d', '2w', '1y') or an ISO date/datetime.

    Naive ISO values are local (HA's zone), never UTC: a dashboard asking for
    "2026-10-31T18:00" means 6pm here.
    """
    text = str(value).strip().lower()
    if text == "now":
        return now
    if m := _DURATION.match(text):
        return now - timedelta(seconds=float(m.group(1)) * _SECONDS[m.group(2)])
    try:
        when = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError(
            f"Can't read time {value!r}: use 'now', a duration ago like "
            "'90m', '24h', '7d', '1y', or an ISO date/datetime") from None
    return when if when.tzinfo else when.replace(tzinfo=tz)


def resolve_window(start, end, now, tz, default_start):
    """(start, end) datetimes for a tool call; end defaults to now."""
    start_dt = parse_when(start or default_start, now, tz)
    end_dt = parse_when(end, now, tz) if end else now
    if start_dt >= end_dt:
        raise ValueError(f"start ({start_dt.isoformat()}) must be before end "
                         f"({end_dt.isoformat()})")
    return start_dt, end_dt


def pick_bucket(span_seconds, max_points):
    """Smallest nice bucket that keeps span_seconds within max_points buckets."""
    for bucket in NICE_BUCKETS:
        if math.ceil(span_seconds / bucket) <= max_points:
            return bucket
    return math.ceil(span_seconds / max_points / 86400) * 86400


def align_start(ts, bucket, tz):
    """Floor ts to a bucket boundary on the LOCAL clock (day buckets -> local midnight)."""
    offset = datetime.fromtimestamp(ts, tz).utcoffset().total_seconds()
    local = ts + offset
    return local - (local % bucket) - offset


def to_number(state):
    """Float for a numeric state string, None for anything else (incl. nan/inf)."""
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def classify(states):
    """'numeric' when every real (non-unknown/unavailable) state is a number."""
    real = [s for s in states if str(s).strip().lower() not in _NOT_A_VALUE]
    if real and all(to_number(s) is not None for s in real):
        return "numeric"
    return "text"


def bucket_numeric(points, start, end, bucket):
    """Time-weighted [bucket_start, mean, min, max] rows for a held-value series.

    points: sorted (ts, value) pairs; value None means unknown/unavailable and
    that time is left out of the bucket. A state recorded before `start` is the
    value held going into the window. A change exactly on a boundary belongs to
    the later bucket. Buckets with no known value at all are omitted.
    """
    rows = []
    idx, held = 0, None
    while idx < len(points) and points[idx][0] <= start:
        held = points[idx][1]
        idx += 1
    for b0 in range(int(start), int(end), int(bucket)):
        b1 = min(b0 + bucket, end)
        t, weighted, covered, lo, hi = b0, 0.0, 0.0, None, None
        while True:
            nxt = points[idx][0] if idx < len(points) and points[idx][0] < b1 else b1
            if held is not None and nxt > t:
                weighted += held * (nxt - t)
                covered += nxt - t
                lo = held if lo is None else min(lo, held)
                hi = held if hi is None else max(hi, held)
            t = nxt
            if t >= b1:
                break
            held = points[idx][1]
            idx += 1
        if covered > 0:
            rows.append([b0, weighted / covered, lo, hi])
    return rows


def text_changes(points, limit):
    """[ts, state] rows at each change of a text series, newest `limit` kept."""
    rows = []
    for ts, state in points:
        if not rows or rows[-1][1] != state:
            rows.append([ts, state])
    return rows[-limit:], len(rows) > limit


def default_stat_types(has_sum, has_mean):
    """Counters (kWh totals) want per-period change; measurements want mean/min/max."""
    if has_sum and not has_mean:
        return ["change"]
    return ["mean", "min", "max"]


def _compact(value):
    return round(value, 3) if isinstance(value, float) else value


def format_rows(rows, tz):
    """Rows of [epoch_ts, *values] -> [local ISO time, *rounded values]."""
    return [
        [datetime.fromtimestamp(row[0], tz).isoformat(timespec="seconds"),
         *(_compact(v) for v in row[1:])]
        for row in rows
    ]


def statistics_table(stat_rows, types, tz):
    """Recorder statistics rows (dicts keyed by type) -> columnar table."""
    rows = [[r["start"], *(r.get(t) for t in types)] for r in stat_rows]
    return {"columns": ["start", *types], "rows": format_rows(rows, tz)}
