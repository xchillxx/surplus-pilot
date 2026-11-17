"""Cheap threshold: recent percentile, capped by the 30-day percentile (17.11.2026)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.surplus_pilot import planner as P

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=TZ)


def plain_pct(vals, pct):
    vals = sorted(vals)
    idx = (len(vals) - 1) * pct / 100.0
    lo, hi = int(idx), min(int(idx) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def test_equal_weights_match_plain_percentile():
    vals = [0.31, 0.29, 0.45, 0.33, 0.40, 0.28, 0.50, 0.36, 0.42]
    for pct in (0, 1, 5, 10, 37, 50, 90, 100):
        got = P.weighted_percentile([(v, 1.0) for v in vals], pct)
        assert abs(got - plain_pct(vals, pct)) < 1e-9


def _archive_15min(start, hours, price_fn):
    arch = {}
    for h in range(hours):
        t0 = start + timedelta(hours=h)
        for q in range(4):
            t = t0 + timedelta(minutes=15 * q)
            arch[t.isoformat()] = price_fn(t)
    return arch


def test_expensive_week_is_capped_by_30_days():
    # days 9..30 ago: hourly means, cheap nights at 0.17; last 8 days: 15-min prices, never below 0.28
    arch = {}
    for h in range(9 * 24, 30 * 24):
        t = (NOW - timedelta(hours=h)).replace(minute=0)
        arch[t.isoformat()] = 0.17 if t.hour < 5 else 0.40
    arch.update(_archive_15min(NOW - timedelta(days=8), 8 * 24, lambda t: 0.28 + 0.01 * (t.hour % 12)))
    thr, recent, cap = P.cheap_thresholds(arch, 5, NOW, 8, 30)
    assert recent >= 0.28
    assert cap is not None and cap < 0.2
    assert thr == cap


def test_cheap_recent_week_keeps_recent_threshold():
    arch = {}
    for h in range(9 * 24, 30 * 24):
        t = (NOW - timedelta(hours=h)).replace(minute=0)
        arch[t.isoformat()] = 0.35
    arch.update(_archive_15min(NOW - timedelta(days=8), 8 * 24, lambda t: 0.15 if t.hour < 4 else 0.30))
    thr, recent, cap = P.cheap_thresholds(arch, 5, NOW, 8, 30)
    assert thr == recent == 0.15
    assert cap >= recent


def test_hourly_and_quarter_hour_rows_weigh_by_time():
    # one hour as 4 quarter prices at 0.10, three hours as hourly means at 0.30: the median hour is 0.30
    base = (NOW - timedelta(hours=4)).replace(minute=0)
    arch = {(base + timedelta(minutes=15 * q)).isoformat(): 0.10 for q in range(4)}
    for h in (1, 2, 3):
        arch[(base + timedelta(hours=h)).isoformat()] = 0.30
    thr, recent, cap = P.cheap_thresholds(arch, 50, NOW, 8, 30)
    assert abs(recent - 0.30) < 1e-9   # unweighted (4 x 0.10 vs 3 x 0.30) would give 0.10


def test_empty_archive():
    assert P.cheap_thresholds({}, 5, NOW, 8, 30) == (None, None, None)
