"""The car keeps the current it was given (19.11.2026 live: 6 A set right with
the start, the car charged at 16 A / 11 kW for 14 min until the next decision)."""
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.surplus_pilot.car import CarController
from custom_components.surplus_pilot.planner import Plan

CFG = {"charge_switch": "switch.car_charge", "current_entity": "number.car_amps", "min_amps": 5,
       "max_amps": 16, "phases": 3, "voltage": 230, "max_commands_per_day": 40}


def _setup(hass):
    calls, log = [], []

    async def fake(call):
        calls.append((call.domain, call.service, dict(call.data)))
        ent = call.data.get("entity_id")
        if call.domain == "switch":
            hass.states.async_set(ent, "on" if call.service == "turn_on" else "off")
        else:
            hass.states.async_set(ent, call.data["value"], {"min": 5, "max": 16})

    for dom, srv in (("switch", "turn_on"), ("switch", "turn_off"), ("number", "set_value")):
        hass.services.async_register(dom, srv, fake)
    hass.states.async_set("switch.car_charge", "off")
    hass.states.async_set("number.car_amps", 5, {"min": 5, "max": 16})
    counter = []
    car = CarController(hass, CFG, lambda: counter.append(1), log.append)
    return car, calls, log, counter


def _plan(kw):
    return Plan(battery_target_kw=0.0, battery_mode="frist", car_kw=kw, car_must_kw=0.0, car_grid=False,
                car_reason="vorrang_rest", car_first=True, need_kwh=0.0, uncovered_kwh=0.0,
                next_departure=None, devices={}, budget={})


async def _start_6a(hass, car, now):
    car.decide(now, _plan(4.2), charging=False, forced=False)
    await car.async_apply(now, True, False, True, 0)
    await hass.async_block_till_done()
    assert car.decision.amps == 6


async def test_reported_current_differs_is_resent_after_settling(hass: HomeAssistant):
    car, calls, log, _ = _setup(hass)
    now = dt_util.now()
    await _start_6a(hass, car, now)
    calls.clear()
    # the car reports 16 A on its own after the start
    hass.states.async_set("number.car_amps", 16, {"min": 5, "max": 16})
    await car.async_guard_amps(now + timedelta(seconds=60), True, 11.0, 2)
    assert calls == []                                   # still settling
    await car.async_guard_amps(now + timedelta(seconds=95), True, 11.0, 2)
    await hass.async_block_till_done()
    assert calls == [("number", "set_value", {"entity_id": "number.car_amps", "value": 6})]
    assert "16 A statt 6 A" in log[-1]
    assert car.action == "ampere_6_nachgesetzt"


async def test_overdraw_with_stale_entity_is_resent(hass: HomeAssistant):
    car, calls, log, _ = _setup(hass)
    now = dt_util.now()
    await _start_6a(hass, car, now)
    calls.clear()
    # entity still says 6 A, but the car draws 11 kW
    for s in (30, 60, 100):
        await car.async_guard_amps(now + timedelta(seconds=s), True, 11.0, 2)
    assert calls == []                                   # overdraw seen only 70 s
    await car.async_guard_amps(now + timedelta(seconds=125), True, 11.0, 2)
    await hass.async_block_till_done()
    assert calls == [("number", "set_value", {"entity_id": "number.car_amps", "value": 6})]
    assert "11.0 kW statt 6 A" in log[-1]


async def test_matching_current_and_normal_power_leave_it_alone(hass: HomeAssistant):
    car, calls, _, _ = _setup(hass)
    now = dt_util.now()
    await _start_6a(hass, car, now)
    calls.clear()
    for s in range(0, 600, 60):
        await car.async_guard_amps(now + timedelta(seconds=s), True, 4.0, 2)   # 6 A ≈ 4.1 kW
    assert calls == []


async def test_resends_are_capped_per_decision_and_reset_by_a_new_one(hass: HomeAssistant):
    car, calls, _, _ = _setup(hass)
    now = dt_util.now()
    await _start_6a(hass, car, now)
    calls.clear()

    async def car_ignores(t):
        hass.states.async_set("number.car_amps", 16, {"min": 5, "max": 16})
        await car.async_guard_amps(t, True, 11.0, 2)
        await hass.async_block_till_done()

    for s in range(90, 900, 30):
        await car_ignores(now + timedelta(seconds=s))
    assert len(calls) == 2                               # CAR_AMPS_MAX_FIXES
    # next quarter-hour decision: may try again
    t = now + timedelta(minutes=15)
    car.decide(t, _plan(4.2), charging=True, forced=False)
    await car.async_apply(t, True, True, True, 4)
    await hass.async_block_till_done()
    assert len(calls) == 3                               # the decision itself sets 6 A again
    await car_ignores(t + timedelta(seconds=95))
    assert len(calls) == 4


async def test_not_charging_or_daily_budget_used_up_sends_nothing(hass: HomeAssistant):
    car, calls, _, _ = _setup(hass)
    now = dt_util.now()
    await _start_6a(hass, car, now)
    calls.clear()
    hass.states.async_set("number.car_amps", 16, {"min": 5, "max": 16})
    await car.async_guard_amps(now + timedelta(seconds=120), False, 0.0, 2)
    await car.async_guard_amps(now + timedelta(seconds=150), True, 11.0, 40)
    assert calls == []
