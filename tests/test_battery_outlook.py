"""Home battery range: one calculation (planner.battery_outlook) behind the
battery path, the device reasons and the battery line."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.surplus_pilot import planner as P
from custom_components.surplus_pilot.coordinator import REASON_TEXT, PilotCoordinator

from .test_planner import base_inputs, miner

TZ = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def berlin():
    old = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(TZ)
    yield
    dt_util.set_default_time_zone(old)


def _oct12(soc=83.0, **kw):
    """12.10. 20:00 live: 13.8 kWh, reserve 15 %, learned night base 0.39 kW,
    miner 0.145 kW, PV start 13.10. 09:12 (sunrise 07:42 + 1.5 h)."""
    now = datetime(2026, 10, 12, 20, 0, tzinfo=TZ)
    m = miner(on=True)
    m.decision_kw = 0.145
    d = dict(battery_soc=soc, battery_capacity_kwh=13.8, night_base_kw=0.39, base_kw=0.78, devices=[m],
             solar_start=datetime(2026, 10, 13, 9, 12, tzinfo=TZ))
    d.update(kw)
    return base_inputs(now, **d)


def _line(plan, drain, soc, pv_tag=False):
    co = PilotCoordinator.__new__(PilotCoordinator)
    co.device_plan = plan
    co._battery_drain_kw = drain
    return co._battery_line({"akku_soc": soc, "pv_tag": pv_tag})


def test_observed_case_lasts_well_beyond_pv_start():
    """83 %: 9.38 kWh / (0.39 + 0.145) kW = 17.5 h -> ~13:32 next day, needed
    until 10:12. (The night really ended at 34 % at 08:20.)"""
    plan = P.make_plan(_oct12())
    o = plan.battery
    assert plan.devices["miner"].reason == "akku_reicht"
    assert o.lasts and o.missing_h == 0.0
    assert abs(o.usable_kwh - 9.384) < 1e-9 and abs(o.load_kw - 0.535) < 1e-9
    assert o.needed_until == datetime(2026, 10, 13, 10, 12, tzinfo=TZ)
    assert abs((o.empty_at - datetime(2026, 10, 13, 13, 32, tzinfo=TZ)).total_seconds()) < 60
    line = _line(plan, 0.92, 83.0)
    assert line == ("Hausakku 83 %: reicht bei üblicher Nachtlast 0,5 kW bis ~13:32 (dann 15 %) — "
                    "nötig bis 10:12 (PV-Start 09:12 + 1 h) ✅ · gerade 0,9 kW aus dem Akku")


def test_ends_shortly_before_pv_start():
    plan = P.make_plan(_oct12(soc=50.0, devices=[]))
    o = plan.battery   # 4.83 kWh / 0.39 kW = 12.4 h -> 08:23, needed 10:12
    assert not o.lasts
    assert abs((o.empty_at - datetime(2026, 10, 13, 8, 23, tzinfo=TZ)).total_seconds()) < 60
    assert 1.8 < o.missing_h < 1.85
    line = _line(plan, 0.39, 50.0)
    assert line == ("Hausakku 50 %: reicht bei üblicher Nachtlast 0,4 kW bis ~08:23 (dann 15 %) — "
                    "nötig bis 10:12 (PV-Start 09:12 + 1 h), es fehlen ~1 h 49 min ⚠️")


def test_ends_exactly_at_the_needed_time_counts_as_lasting():
    now = datetime(2026, 10, 12, 18, 0, tzinfo=TZ)      # needed 10:00 next day = 16 h
    inp = base_inputs(now, battery_soc=55.0, battery_capacity_kwh=10.0, night_base_kw=0.25)
    o = P.make_plan(inp).battery                       # 4.0 kWh / 0.25 kW = 16 h
    assert o.lasts and o.empty_at == o.needed_until == datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
    o = P.make_plan(base_inputs(now, battery_soc=54.9, battery_capacity_kwh=10.0, night_base_kw=0.25)).battery
    assert not o.lasts and 0 < o.missing_h < 0.05


def test_battery_already_at_reserve():
    plan = P.make_plan(_oct12(soc=15.0, devices=[miner()]))
    o = plan.battery
    assert o.usable_kwh == 0 and not o.lasts and o.empty_at == datetime(2026, 10, 12, 20, 0, tzinfo=TZ)
    assert plan.devices["miner"].reason == "akku_reicht_nicht"
    assert _line(plan, 0.4, 15.0).startswith("Hausakku 15 %: auf Reserve 15 % — nötig bis 10:12")


def test_charging_battery_and_missing_battery_power_keep_the_same_verdict():
    plan = P.make_plan(_oct12())
    charging = _line(plan, -1.5, 83.0)
    unknown = _line(plan, None, 83.0)
    assert charging.endswith("✅ · wird gerade geladen")
    assert unknown.endswith("(PV-Start 09:12 + 1 h) ✅")
    assert charging.split(" · ")[0] == unknown


def test_current_draw_does_not_change_the_verdict():
    """Evening load comes and goes; the range uses the usual night load."""
    a = P.make_plan(_oct12(base_kw=0.3))
    b = P.make_plan(_oct12(base_kw=1.4))
    assert a.battery == b.battery and a.devices == b.devices
    # a device with a window end only counts until then
    m = miner(on=True)
    m.decision_kw = 0.145
    m.window_end = datetime(2026, 10, 12, 23, 0, tzinfo=TZ)
    o = P.battery_outlook(_oct12(soc=50.0, devices=[m]), {"miner": True})
    # 3 h with 0.535 kW = 1.605 kWh, then 0.39 kW: 3.225 / 0.39 = 8.27 h -> 07:16
    assert abs((o.empty_at - datetime(2026, 10, 13, 7, 16, tzinfo=TZ)).total_seconds()) < 60


def test_names_the_pv_start_not_the_sunrise():
    """Sunrise 07:42, usable PV 09:12: the line and the reasons name the PV start."""
    plan = P.make_plan(_oct12())
    line = _line(plan, 0.92, 83.0)
    assert "PV-Start 09:12" in line and "Sonnenaufgang" not in line
    assert "Sonnenaufgang" not in REASON_TEXT["akku_reicht"] + REASON_TEXT["akku_reicht_nicht"]
    assert "PV-Start + 1 h" in REASON_TEXT["akku_reicht"]


def test_across_midnight():
    now = datetime(2026, 10, 12, 23, 30, tzinfo=TZ)
    o = P.make_plan(base_inputs(now, battery_soc=30.0, battery_capacity_kwh=10.0, night_base_kw=0.5,
                                solar_start=datetime(2026, 10, 13, 9, 0, tzinfo=TZ))).battery
    assert o.empty_at == datetime(2026, 10, 13, 2, 30, tzinfo=TZ) and not o.lasts


def test_dst_end_night_is_an_hour_longer():
    """25.10.2026: 03:00 CEST -> 02:00 CET. 22:00 -> 10:00 is 13 real hours,
    the wall clock says 12 (6.0 kWh at 0.5 kW: 6.2 kWh looked enough)."""
    now = datetime(2026, 10, 24, 22, 0, tzinfo=TZ)
    inp = base_inputs(now, battery_soc=77.0, battery_capacity_kwh=10.0, night_base_kw=0.5,
                      solar_start=datetime(2026, 10, 25, 9, 0, tzinfo=TZ), devices=[miner()])
    o = P.make_plan(inp).battery
    assert not o.lasts and abs(o.missing_h - 0.6) < 1e-6
    assert o.empty_at == datetime(2026, 10, 25, 9, 24, tzinfo=TZ)


def test_dst_start_night_is_an_hour_shorter():
    """28.03.2027: 02:00 CET -> 03:00 CEST. 22:00 -> 09:00 is 10 real hours."""
    now = datetime(2027, 3, 27, 22, 0, tzinfo=TZ)
    inp = base_inputs(now, battery_soc=65.0, battery_capacity_kwh=10.0, night_base_kw=0.5,
                      solar_start=datetime(2027, 3, 28, 8, 0, tzinfo=TZ))
    o = P.make_plan(inp).battery                       # 5.0 kWh / 0.5 kW = 10 h
    assert o.lasts and o.empty_at == o.needed_until == datetime(2027, 3, 28, 9, 0, tzinfo=TZ)


def test_status_line_and_device_reason_agree():
    """Same needed time and the device's empty time is the outlook with it."""
    m = miner()
    m.decision_kw = 0.145
    inp = _oct12(soc=40.0, devices=[m])
    plan = P.make_plan(inp)
    dec = plan.devices["miner"]
    assert dec.reason == "akku_reicht_nicht"
    assert dec.battery_empty == P.battery_outlook(inp, {}, m).empty_at
    assert not P.battery_lasts(inp, {}, m)
    # the line without the (off) miner may still last - and says until when it is needed
    assert "nötig bis 10:12" in _line(plan, 0.4, 40.0)
    # running case: the miner's "lasts" and the line's ✅ come from the same object
    plan = P.make_plan(_oct12())
    assert plan.devices["miner"].reason == "akku_reicht" and plan.battery.lasts
    assert _line(plan, 0.92, 83.0).count("✅") == 1


def test_pv_day_without_battery_device_shows_only_the_current_flow():
    now = datetime(2026, 10, 13, 13, 0, tzinfo=TZ)
    plan = P.make_plan(base_inputs(now, pv_kw=1.0, base_kw=0.5, battery_soc=60.0))
    assert _line(plan, -0.5, 60.0, pv_tag=True) == "Hausakku 60 % — wird gerade geladen"
    assert _line(plan, 0.7, 60.0, pv_tag=True) == "Hausakku 60 % — gerade 0,7 kW aus dem Akku"
