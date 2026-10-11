"""Pure planner + departures checks (no HA needed)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.surplus_pilot import planner as P
from custom_components.surplus_pilot.departures import all_departures

TZ = ZoneInfo("Europe/Berlin")


def base_inputs(now, **kw):
    d = dict(now=now, pv_kw=0.0, base_kw=0.5, battery_soc=60.0, battery_capacity_kwh=14.0, battery_min_soc=15.0,
             export_kw=0.0, solar_start=now.replace(hour=9) + timedelta(days=1 if now.hour >= 9 else 0),
             solar_start_today=now.replace(hour=9, minute=0), sunset=now.replace(hour=19, minute=0),
             pv_end=now.replace(hour=17, minute=30), night_base_kw=0.45)
    if now.hour >= 19:
        d["sunset"] += timedelta(days=1)
        d["pv_end"] += timedelta(days=1)
    d.update(kw)
    return P.Inputs(**d)


def miner(on=False):
    return P.DeviceInput(id="miner", name="Miner", priority=2, decision_kw=0.15, is_on=on)


def test_night_battery_path():
    now = datetime(2026, 10, 6, 22, 0, tzinfo=TZ)
    plan = P.make_plan(base_inputs(now, battery_soc=80.0, devices=[miner()]))
    assert plan.devices["miner"].on and plan.devices["miner"].reason == "akku_reicht"
    plan = P.make_plan(base_inputs(now, battery_soc=25.0, devices=[miner()]))
    assert not plan.devices["miner"].on and plan.devices["miner"].reason == "akku_reicht_nicht"


def test_departure_obligation_from_grid_late_and_cheap():
    now = datetime(2026, 10, 6, 1, 0, tzinfo=TZ)
    dep = P.Departure(start=now.replace(hour=4, minute=30), target_soc=50.0, name="Frühschicht")
    car = P.CarInput(present=True, soc=35.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    prices = [P.PriceSlot(now + timedelta(minutes=15 * i), now + timedelta(minutes=15 * (i + 1)),
                          0.30 if i < 8 else 0.20) for i in range(16)]
    plan = P.make_plan(base_inputs(now, car=car, departures=[dep], prices=prices))
    assert plan.need_kwh > 10 and not plan.car_grid          # 01:00 is expensive -> wait
    later = now.replace(hour=3, minute=0)
    plan = P.make_plan(base_inputs(later, car=car, departures=[dep], prices=prices))
    assert plan.car_grid and plan.car_kw == 11.04              # 03:00 cheapest slots -> charge


def test_devices_before_car_topup_and_car_first_without_pv_chance():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
    car = P.CarInput(present=True, soc=60.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    fc = [P.ForecastHour(end=now.replace(hour=0) + timedelta(hours=h), kwh=8.0 if 10 <= h % 24 <= 16 else 0.0)
          for h in range(48)]
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=100.0, car=car, forecast=fc, devices=[miner()]))
    assert plan.devices["miner"].on and not plan.car_first and plan.car_kw > 3.45
    # tomorrow the car is away all day -> it charges first today
    away = P.Departure(start=(now + timedelta(days=1)).replace(hour=4, minute=30), target_soc=50.0,
                       returns=(now + timedelta(days=1)).replace(hour=18, minute=30))
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=100.0, car=car, forecast=fc, devices=[miner()],
                                   departures=[away]))
    assert plan.car_first


def test_slots_rhythm_and_oneoff():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
    slots = [{"slot": 1, "enabled": True, "name": "Spätschicht", "target_soc": 50, "time": "16:30", "away_h": 14,
              "rhythm_days": 4, "start_date": "2026-10-02"},
             {"slot": 2, "enabled": False, "time": "04:30", "rhythm_days": 1}]
    oneoff = {"enabled": True, "target_soc": 80, "time": "09:00", "start_date": "2026-10-08"}
    deps = all_departures(slots, oneoff, now)
    starts = [d.start.strftime("%m-%d %H:%M") for d in deps]
    assert starts == ["10-06 16:30", "10-08 09:00"]  # every 4 days: next one 10-10, beyond the 4-day horizon
    assert deps[0].returns == deps[0].start + timedelta(hours=14)


def test_departure_battery_mode_only_while_car_can_charge():
    now = datetime(2026, 10, 6, 14, 0, tzinfo=TZ)
    dep = P.Departure(start=now.replace(hour=16, minute=30), target_soc=50.0)
    fc = [P.ForecastHour(end=now.replace(hour=0) + timedelta(hours=h), kwh=6.0 if 10 <= h <= 17 else 0.0)
          for h in range(24)]
    full = P.CarInput(present=True, soc=80.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                      max_kw=11.04)
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=80.0, car=full, departures=[dep], forecast=fc))
    assert plan.battery_mode == "frist"
    half = P.CarInput(**{**full.__dict__, "soc": 60.0})
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=80.0, car=half, departures=[dep], forecast=fc))
    assert plan.battery_mode == "abfahrt"


def _dark_day_inputs(now, night_estimate, price_now=0.20, threshold=None, cheap_target=None, sunny=False):
    dep = P.Departure(start=(now + timedelta(days=1)).replace(hour=4, minute=30), target_soc=50.0, name="Tagschicht")
    car = P.CarInput(present=True, soc=30.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    # today's prices are published (cheap midday 11-14 h), tomorrow's not yet (before 13:00)
    day0 = now.replace(hour=0, minute=0)
    prices = [P.PriceSlot(day0 + timedelta(hours=h), day0 + timedelta(hours=h + 1),
                          0.20 if 11 <= h < 14 else 0.32) for h in range(24)]
    profile = {h: (night_estimate if h < 6 else 0.32) for h in range(24)}
    kwh = 9.0 if sunny else 0.3
    fc = [P.ForecastHour(end=day0 + timedelta(hours=h), kwh=kwh if 10 <= h % 24 <= 16 else 0.0) for h in range(48)]
    return base_inputs(now, pv_kw=0.3, car=car, departures=[dep], prices=prices, price_profile=profile,
                       forecast=fc, price_now=price_now, cheap_threshold=threshold, cheap_target_soc=cheap_target)


def test_day_before_early_shift_charges_at_cheap_midday():
    early = datetime(2026, 11, 3, 11, 0, tzinfo=TZ)
    # 16 kWh need 1.5 h: of the equally cheap 11-14 h slots the LATEST ones are taken
    assert not P.make_plan(_dark_day_inputs(early, night_estimate=0.28)).car_grid
    now = datetime(2026, 11, 3, 12, 45, tzinfo=TZ)
    plan = P.make_plan(_dark_day_inputs(now, night_estimate=0.28))   # night usually dearer -> now
    assert plan.car_grid and plan.car_reason == "netz_pflicht"
    plan = P.make_plan(_dark_day_inputs(now, night_estimate=0.15))   # night usually cheaper -> wait
    assert not plan.car_grid


def test_cheap_topup_only_when_pv_wont_do_it():
    now = datetime(2026, 11, 3, 11, 0, tzinfo=TZ)
    inp = _dark_day_inputs(now, night_estimate=0.15, price_now=0.20, threshold=0.22, cheap_target=80.0)
    inp.car.soc = 55.0  # obligation already met
    inp.battery_soc = 15.0  # home battery empty, it can't feed the car
    plan = P.make_plan(inp)
    assert plan.car_grid and plan.car_reason == "netz_billig"
    inp.battery_soc = 60.0  # it would empty itself into the car first
    assert not P.make_plan(inp).car_grid
    inp = _dark_day_inputs(now, night_estimate=0.15, price_now=0.20, threshold=0.22, cheap_target=80.0, sunny=True)
    inp.car.soc = 55.0
    inp.battery_soc = 15.0
    plan = P.make_plan(inp)
    assert not plan.car_grid


def test_ordinary_autumn_tomorrow_keeps_devices_before_car_topup():
    """07.10.2026 live: car 52 % (limit 80), tomorrow ~25 kWh forecast, car
    home all day. The x0.7 forecast made it 'no PV chance tomorrow' and the
    optional top-up pushed the pool pump off."""
    now = datetime(2026, 10, 7, 10, 30, tzinfo=TZ)
    car = P.CarInput(present=True, soc=52.0, limit_soc=80.0, capacity_kwh=72.89, efficiency=0.9, min_kw=4.14,
                     max_kw=11.04)
    tomorrow = now.replace(hour=0, minute=0) + timedelta(days=1)
    kwh = {10: 1.5, 11: 2.8, 12: 3.8, 13: 4.3, 14: 4.3, 15: 3.8, 16: 2.9, 17: 1.6, 18: 0.6}
    fc = [P.ForecastHour(end=tomorrow + timedelta(hours=h), kwh=v) for h, v in kwh.items()]
    pump = P.DeviceInput(id="pump", name="Poolpumpe", priority=3, decision_kw=1.34, is_on=True, soc_reserve=85.0)
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, base_kw=1.0, battery_soc=70.0, battery_capacity_kwh=13.8,
                                   car=car, forecast=fc, devices=[miner(True), pump]))
    assert not plan.car_first
    assert plan.devices["pump"].on


def test_daytime_off_reason_is_missing_surplus_not_reserve():
    now = datetime(2026, 10, 7, 13, 0, tzinfo=TZ)
    boiler = P.DeviceInput(id="boiler", name="Boiler", priority=5, decision_kw=2.0, is_on=True, soc_reserve=95.0)
    plan = P.make_plan(base_inputs(now, pv_kw=2.0, base_kw=1.0, battery_soc=83.0, export_kw=0.0, devices=[boiler]))
    assert plan.devices["boiler"].reason == "kein_ueberschuss"
    night = now.replace(hour=22)
    plan = P.make_plan(base_inputs(night, battery_soc=83.0, devices=[boiler]))
    assert plan.devices["boiler"].reason == "akku_reserve"


def test_topup_capped_by_recent_window_on_falling_pv():
    """07.10. 15:09 live: 30-min average PV ~9 kW -> 12 A, real PV 7 kW and
    falling; the home battery paid ~2 kWh into the car."""
    now = datetime(2026, 10, 7, 15, 9, tzinfo=TZ)
    car = P.CarInput(present=True, soc=71.0, limit_soc=80.0, capacity_kwh=72.89, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    dep = P.Departure(start=(now + timedelta(days=1)).replace(hour=7, minute=0), target_soc=80.0, name="Einmalig",
                      returns=(now + timedelta(days=1)).replace(hour=18, minute=0))
    fc = [P.ForecastHour(end=now.replace(hour=0, minute=0) + timedelta(hours=h), kwh=6.0 if 10 <= h % 24 <= 17 else 0.0)
          for h in range(48)]
    kw = dict(battery_soc=100.0, car=car, departures=[dep], forecast=fc)
    avg = P.make_plan(base_inputs(now, pv_kw=9.0, base_kw=0.5, **kw))
    recent = P.make_plan(base_inputs(now, pv_kw=7.0, base_kw=0.5, **kw))
    assert avg.car_kw > 8.0 and avg.car_must_kw > 0
    capped = P.cap_car_topup(avg, recent.car_kw, car.min_kw)
    assert capped.car_kw == recent.car_kw < avg.car_kw
    assert capped.car_kw >= capped.car_must_kw
    # rising PV: the recent window is higher -> nothing changes
    assert P.cap_car_topup(recent, avg.car_kw, car.min_kw) is recent
    # never below the obligation; grid charging untouched
    low = P.cap_car_topup(avg, 0.0, car.min_kw)
    assert low.car_kw == max(avg.car_must_kw, car.min_kw)


def test_device_does_not_start_for_a_short_run():
    """07.10. live: pump on at 16:51 with PV end at 17:22 (off 17:49), and on
    again at 20:23 on the battery path at 86 % with an 85 % reserve (off 20:40)."""
    pump = lambda on=False: P.DeviceInput(id="pump", name="Poolpumpe", priority=3, decision_kw=1.34, is_on=on,
                                          soc_reserve=85.0, window_end=datetime(2026, 10, 7, 22, 0, tzinfo=TZ))
    kw = dict(battery_capacity_kwh=13.8)
    # afternoon: plenty of surplus right now, but the PV day ends in 40 min
    late = datetime(2026, 10, 7, 16, 41, tzinfo=TZ)
    plan = P.make_plan(base_inputs(late, pv_kw=4.3, base_kw=0.3, battery_soc=100.0, devices=[miner(True), pump()],
                                   pv_end=late.replace(hour=17, minute=22), **kw))
    assert not plan.devices["pump"].on and plan.devices["pump"].reason == "zu_kurz"
    # already running: keeps running on the surplus
    plan = P.make_plan(base_inputs(late, pv_kw=4.3, base_kw=0.3, battery_soc=100.0,
                                   devices=[miner(True), pump(True)], pv_end=late.replace(hour=17, minute=22), **kw))
    assert plan.devices["pump"].on
    # earlier the same surplus starts it
    noon = late.replace(hour=13)
    plan = P.make_plan(base_inputs(noon, pv_kw=4.3, base_kw=0.3, battery_soc=100.0, devices=[miner(True), pump()],
                                   pv_end=late.replace(hour=17, minute=22), **kw))
    assert plan.devices["pump"].on
    # evening battery path: 86 % would be under the 85 % reserve within minutes
    eve = datetime(2026, 10, 7, 20, 12, tzinfo=TZ)
    plan = P.make_plan(base_inputs(eve, base_kw=0.6, battery_soc=86.0, night_base_kw=0.2,
                                   devices=[miner(True), pump()], **kw))
    assert not plan.devices["pump"].on and plan.devices["pump"].reason == "zu_kurz"
    # a running pump keeps going until the reserve itself is reached
    plan = P.make_plan(base_inputs(eve, base_kw=0.6, battery_soc=86.0, night_base_kw=0.2,
                                   devices=[miner(True), pump(True)], **kw))
    assert plan.devices["pump"].on
    # window closes in 30 min: not worth starting
    plan = P.make_plan(base_inputs(eve.replace(hour=21, minute=30), base_kw=0.6, battery_soc=100.0,
                                   night_base_kw=0.2, devices=[pump()], **kw))
    assert plan.devices["pump"].reason == "zu_kurz"


def _prices_0810():
    """Tibber 08.10.2026, 09:00-22:00 (ct/kWh, quarter hours)."""
    ct = [39.8, 38.3, 36.6, 35.7, 36.3, 34.8, 33.7, 32.9, 33.5, 33.1, 32.4, 32.1, 31.4, 30.8, 30.5, 30.2,
          30.9, 30.1, 29.5, 28.7, 29.5, 28.8, 28.9, 29.2, 28.5, 29.1, 30.8, 30.7, 29.6, 30.3, 31.7, 32.0,
          30.7, 33.3, 35.4, 37.5, 35.4, 36.1, 36.6, 37.4, 37.6, 38.0, 37.8, 38.0, 36.5, 36.1, 36.5, 35.7,
          35.3, 35.8, 34.8, 31.9]
    t0 = datetime(2026, 10, 8, 9, 0, tzinfo=TZ)
    return [P.PriceSlot(t0 + timedelta(minutes=15 * i), t0 + timedelta(minutes=15 * (i + 1)), c / 100)
            for i, c in enumerate(ct)]


def test_min_runtime_one_cheap_block_planned_early_when_pv_wont_cover_it():
    """08.10. live: fog, battery 27 % at 09:00, forecast ~23 kWh -> the
    surplus goes to the battery. Old logic: nothing before 14:00, then
    14:00-17:15 plus a 30-min run at 21:30. Now: one 4 h block in the
    cheapest part of the whole window."""
    end = datetime(2026, 10, 8, 22, 0, tzinfo=TZ)
    pv_end = datetime(2026, 10, 8, 17, 20, tzinfo=TZ)
    fc = [P.ForecastHour(end=datetime(2026, 10, 8, h, 0, tzinfo=TZ), kwh=k)
          for h, k in zip(range(8, 20), [0.0, 0.1, 0.9, 1.7, 2.4, 3.0, 3.3, 3.4, 3.3, 3.1, 2.9, 2.2])]
    now = datetime(2026, 10, 8, 9, 0, tzinfo=TZ)
    sh = P.device_surplus_hours(fc, now, end, 0.6, 1.34, 27.0, 13.8, pv_end, 0.145)
    assert sh < 4
    start = P.min_runtime_start(now, 4.0, end, _prices_0810(), sh)
    assert start == datetime(2026, 10, 8, 12, 30, tzinfo=TZ)
    # nothing forced before the block, forced from its start on
    assert P.min_runtime_start(now.replace(hour=12, minute=15), 4.0, end, _prices_0810(), sh) > \
        now.replace(hour=12, minute=15)
    at = now.replace(hour=12, minute=30)
    assert P.min_runtime_start(at, 4.0, end, _prices_0810(), sh) <= at
    # a sunny forecast keeps the old "latest stretch only" rule: PV first
    assert P.min_runtime_start(now, 4.0, end, _prices_0810(), 6.0) >= now.replace(hour=14)
    # a short remainder is still run as one block of at least an hour
    eve = now.replace(hour=19, minute=30)
    s = P.min_runtime_start(eve, 0.5, end, _prices_0810(), 0.0)
    assert s is not None and s <= now.replace(hour=21)


def test_running_device_finishes_its_min_runtime():
    """09.10. live: pump ran on PV 09:43-13:24 (3.7 h), off with 0.3 h
    missing, forced on again 13:33 for a 1.1 h catch-up run."""
    assert P.min_runtime_finish(True, 0.3)
    assert P.min_runtime_finish(True, P.DEVICE_MIN_RUN_H)
    assert not P.min_runtime_finish(True, 1.5)     # a real run left: cheapest block later
    assert not P.min_runtime_finish(False, 0.3)    # off: no start just for minutes
    assert not P.min_runtime_finish(True, 0.0)     # done: surplus decides again


def _oct10(now, pv_kw, battery_soc, car_soc, prices=None, price_now=None, threshold=None, cheap_target=None,
           current_kw=0.0, scale=1.0):
    """10.10.2026 live: night shift 16:30 (target 50 %), Forecast.Solar for
    the day, Tibber prices (midday 17.2 ct, evening 37 ct)."""
    day0 = now.replace(hour=0, minute=0)
    fc_kwh = {8: 0.08, 9: 0.89, 10: 1.75, 11: 2.9, 12: 4.35, 13: 5.79, 14: 6.77, 15: 6.85, 16: 6.02, 17: 4.71,
              18: 2.9, 19: 0.72}  # hour ENDING at this local time
    fc = [P.ForecastHour(end=day0 + timedelta(hours=h), kwh=fc_kwh.get(h, 0.0) * scale) for h in range(24)]
    dep = P.Departure(start=now.replace(hour=16, minute=30), target_soc=50.0, name="Nachtschicht",
                      returns=(now + timedelta(days=1)).replace(hour=6, minute=30))
    car = P.CarInput(present=True, soc=car_soc, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04, current_kw=current_kw)
    return base_inputs(now, pv_kw=pv_kw, base_kw=0.78, battery_soc=battery_soc, battery_capacity_kwh=13.8,
                       car=car, departures=[dep], forecast=fc, prices=prices or [], price_now=price_now,
                       cheap_threshold=threshold, cheap_target_soc=cheap_target,
                       solar_start_today=now.replace(hour=9, minute=10), pv_end=now.replace(hour=17, minute=15))


def test_obligation_waits_for_real_surplus_instead_of_minimum_from_battery_and_grid():
    """10.10. live 09:15: 0.65 kW surplus, battery at 12 % -> car at the
    3.45 kW minimum, the rest from battery and grid at 22.7 ct."""
    now = datetime(2026, 10, 10, 9, 15, tzinfo=TZ)
    plan = P.make_plan(_oct10(now, pv_kw=1.43, battery_soc=12.0, car_soc=39.0))
    assert plan.car_kw == 0.0 and plan.car_reason == "zu_wenig_ueberschuss"
    # midday with half the minimum from PV and a forecast too weak for PV-only hours -> minimum
    noon = now.replace(hour=12, minute=0)
    weak = _oct10(noon, pv_kw=2.7, battery_soc=30.0, car_soc=39.0, scale=0.6)
    plan = P.make_plan(weak)
    assert plan.car_reason == "pflicht_minimum" and plan.car_kw == 3.45
    # the same moment, but the forecast still brings the target in PV-only hours -> wait for them
    plan = P.make_plan(_oct10(noon, pv_kw=2.7, battery_soc=30.0, car_soc=45.0, scale=1.3))
    assert plan.car_kw == 0.0
    # already charging at the minimum: kept down to 40 % PV share
    weak = _oct10(noon, pv_kw=2.35, battery_soc=30.0, car_soc=39.0, scale=0.6, current_kw=3.45)
    assert P.make_plan(weak).car_reason == "pflicht_minimum"


def test_cheap_topup_waits_for_a_cheaper_slot_without_pv():
    """A cheap slot now, a cheaper PV-free one later before the departure:
    wait. Cheaper slots at midday (PV for the home battery) don't count."""
    now = datetime(2026, 10, 10, 1, 0, tzinfo=TZ)
    day0 = now.replace(hour=0)
    tariff = {1: 0.189, 2: 0.189, 3: 0.175, 4: 0.189, 5: 0.189, 13: 0.15, 14: 0.15}
    prices = [P.PriceSlot(day0 + timedelta(minutes=15 * i), day0 + timedelta(minutes=15 * (i + 1)),
                          tariff.get(i // 4, 0.23)) for i in range(96)]

    def plan_at(t, price):
        return P.make_plan(_oct10(t, pv_kw=0.0, battery_soc=15.0, car_soc=52.0, prices=prices, price_now=price,
                                  threshold=0.19, cheap_target=80.0))

    assert not plan_at(now, 0.189).car_grid                       # 03:00 is cheaper and dark -> wait
    plan = plan_at(now.replace(hour=3), 0.175)
    assert plan.car_grid and plan.car_reason == "netz_billig"
    prices[12:16] = [replace_price(p, 0.23) for p in prices[12:16]]  # 03:00 gone: 01:00 is the best dark slot
    assert plan_at(now, 0.189).car_grid                           # midday at 15 ct is PV time, doesn't count


def replace_price(p, price):
    return P.PriceSlot(p.start, p.end, price)


def _oct10_prices(day0):
    """Tibber 10.10.2026: night ~18.9-19.4 ct, midday 17.16 ct, evening 31-37 ct."""
    q = {0: 19.16, 1: 19.0, 2: 19.0, 3: 19.07, 4: 19.3, 5: 18.89, 6: 20.0, 7: 22.4, 8: 23.8, 9: 22.6, 10: 20.1,
         11: 17.9, 12: 17.2, 13: 17.16, 14: 17.16, 15: 17.16, 16: 18.9}
    return [P.PriceSlot(day0 + timedelta(minutes=15 * i), day0 + timedelta(minutes=15 * (i + 1)),
                        (17.16 if (i // 4 == 16 and i % 4 < 2) else q.get(i // 4, 33.0)) / 100) for i in range(96)]


def test_cheap_topup_without_battery_feed_takes_the_cheap_midday_with_pv():
    """Battery set not to discharge into the wallbox: cheap charging looks
    at all published slots up to the departure, midday PV hours included,
    and charges PV plus grid there (10.10.: 05:00 at 18.9 ct was dearer
    than the midday before the 16:30 departure)."""
    now = datetime(2026, 10, 10, 5, 0, tzinfo=TZ)
    prices = _oct10_prices(now.replace(hour=0))

    def at(t, pv, battery, car_soc=27.0):
        inp = _oct10(t, pv_kw=pv, battery_soc=battery, car_soc=car_soc, prices=prices,
                     price_now=next(p.price for p in prices if p.start <= t < p.end), threshold=0.1904,
                     cheap_target=80.0)
        inp.battery_feeds_car = False
        return P.make_plan(inp)

    assert not at(now, 0.0, 36.0).car_grid                         # night 18.9 ct: midday is cheaper
    plan = at(now.replace(hour=13), 6.8, 40.0, car_soc=45.0)        # 17.16 ct with 6 kW surplus: PV + grid
    assert plan.car_grid and plan.car_reason == "netz_billig" and plan.car_kw == 11.04
    assert not at(now.replace(hour=13), 6.8, 40.0, car_soc=80.0).car_grid   # target reached


def test_battery_feed_evidence():
    """10.10. live: car 3.0 kW, PV 1.4, house 0.8, battery -2.5 kW -> feeds.
    Battery idle (or only covering the house) while the car imports -> doesn't."""
    ev = P.battery_feed_evidence
    assert ev(3.0, 1.4, 3.8, -2.5, 40.0, 15.0) is True
    assert ev(3.0, 1.4, 3.8, 0.0, 40.0, 15.0) is False
    assert ev(11.0, 0.0, 11.8, -0.8, 40.0, 15.0) is False       # battery covers the house only
    assert ev(11.0, 6.0, 11.8, 1.5, 40.0, 15.0) is False        # even charges from PV while the car imports
    assert ev(3.0, 1.4, 3.8, -2.5, 18.0, 15.0) is None          # battery at its reserve: says nothing
    assert ev(0.0, 0.0, 0.8, -0.8, 40.0, 15.0) is None          # car not charging
    assert ev(3.0, 4.0, 3.8, -0.0, 40.0, 15.0) is None          # PV covers it all


def test_grid_charging_is_one_block_not_scattered_quarter_hours():
    """10.10. live: 11:45 17.16 ct, 12:00 17.40, 12:15 17.19, from 12:30
    17.16 - the cheapest single quarter hours would stop the car at 12:00
    and start it again at 12:30. One block instead; a running one goes on."""
    now = datetime(2026, 10, 10, 11, 45, tzinfo=TZ)
    day0 = now.replace(hour=0)

    def tariff(t):
        if (t.hour, t.minute) == (12, 0):
            return 0.174
        if (t.hour, t.minute) == (12, 15):
            return 0.1719
        return 0.1716 if day0.replace(hour=11, minute=45) <= t < day0.replace(hour=16, minute=30) else 0.20

    prices = [P.PriceSlot(day0 + timedelta(minutes=15 * i), day0 + timedelta(minutes=15 * (i + 1)),
                          tariff(day0 + timedelta(minutes=15 * i))) for i in range(96)]

    def at(t, running):
        inp = _oct10(t, pv_kw=6.8, battery_soc=25.0, car_soc=44.0, prices=prices,
                     price_now=next(p.price for p in prices if p.start <= t < p.end), threshold=0.189,
                     cheap_target=80.0)
        inp.battery_feeds_car = False
        inp.grid_running = running
        return P.make_plan(inp)

    assert not at(now, False).car_grid                    # best block starts 12:30
    assert at(now.replace(hour=12, minute=30), False).car_grid
    assert at(now.replace(hour=12, minute=0), True).car_grid   # already running: no stop for 0.2 ct
    # the two cheapest single slots (0.1) are apart: the block 0.1+0.12+0.12 wins
    blk = P._best_block([(now + i * P.SLOT, p) for i, p in enumerate([0.1, 0.4, 0.1, 0.12, 0.12, 0.12])], 3, False)
    assert blk[0] == now + 2 * P.SLOT and blk[1] == 3


def test_devices_know_the_car_grid_block():
    """10.10. live: miner and pump on at 10:43 on PV, the car started at
    10:45 and took the PV - both off again by 11:13. With the car's cheap
    block planned for 11:45 (price still above the threshold at 11:00), a
    device doesn't start on PV within the hour before it, and its forecast
    PV hours don't count the block."""
    now = datetime(2026, 10, 10, 11, 0, tzinfo=TZ)
    day0 = now.replace(hour=0)
    prices = [P.PriceSlot(day0 + timedelta(minutes=15 * i), day0 + timedelta(minutes=15 * (i + 1)),
                          0.1716 if 47 <= i < 66 else 0.20) for i in range(96)]   # 11:45-16:30 cheap
    inp = _oct10(now, pv_kw=8.0, battery_soc=40.0, car_soc=55.0, prices=prices, price_now=0.20,
                 threshold=0.189, cheap_target=80.0)
    inp.battery_feeds_car = False
    inp.devices = [miner()]
    plan = P.make_plan(inp)
    assert not plan.car_grid and plan.car_block[0] == now.replace(minute=45)
    assert not plan.devices["miner"].on and plan.devices["miner"].reason == "zu_kurz"
    inp.car_block = None   # without a block the miner would start
    inp.cheap_target_soc = None
    assert P.make_plan(inp).devices["miner"].on
    # forecast PV hours for a 1.3 kW pump: the block (car 11 kW) takes them
    fc = inp.forecast
    free = P.device_surplus_hours(fc, now, now.replace(hour=17), 0.8, 1.3, 100.0, 13.8, now.replace(hour=17))
    blocked = P.device_surplus_hours(fc, now, now.replace(hour=17), 0.8, 1.3, 100.0, 13.8, now.replace(hour=17),
                                     car_block=(now.replace(minute=45), now.replace(hour=13, minute=45)), car_kw=11.04)
    assert abs((free - blocked) - 2.0) < 0.01


def test_cheap_topup_with_pv_needs_a_full_enough_home_battery():
    """10.10. live 13:03-14:10: cheap block at midday, the inverter gave the
    PV to the wallbox and the battery carried the house (stove) 34 -> 10 %.
    With PV, cheap charging now needs the battery at 50 % (setting); a running
    block goes on to 45 %, below it stops (and stays off in PV for the day).
    Slots without PV stay open - then the night block wins."""
    now = datetime(2026, 10, 10, 13, 0, tzinfo=TZ)
    prices = _oct10_prices(now.replace(hour=0))

    def inp_at(t, pv, battery, running=False, locked=False):
        inp = _oct10(t, pv_kw=pv, battery_soc=battery, car_soc=53.0, prices=prices,
                     price_now=next(p.price for p in prices if p.start <= t < p.end), threshold=0.1904,
                     cheap_target=80.0, scale=0.5)   # dull afternoon: the forecast won't fill the battery
        inp.battery_feeds_car = False
        inp.cheap_pv_battery_soc = 50.0
        inp.grid_running, inp.cheap_pv_locked = running, locked
        return inp

    assert not P.make_plan(inp_at(now, 4.5, 34.0)).car_grid            # today: battery too low
    # 15:15 live: PV 2.4 kW, battery 21 %, 17.2 ct below the threshold -> the status names the battery
    assert P.make_plan(inp_at(now.replace(hour=15, minute=15), 2.4, 21.0)).car_reason == "billig_akku"
    assert P.make_plan(inp_at(now.replace(hour=15, minute=15), 2.4, 60.0)).car_reason == "netz_billig"
    assert P.make_plan(inp_at(now, 4.5, 55.0)).car_grid                # full enough: PV + grid
    assert P.make_plan(inp_at(now, 4.5, 47.0, running=True)).car_grid  # running: down to 45 %
    stop = inp_at(now, 4.5, 44.0, running=True)
    assert P.cheap_pv_battery_stop(stop) and not P.make_plan(stop).car_grid
    assert not P.make_plan(inp_at(now, 4.5, 60.0, locked=True)).car_grid   # stopped once today
    # night: no PV -> the battery rule doesn't apply, 05:00 at 18.9 ct is the cheapest free slot now
    night = inp_at(now.replace(hour=5), 0.0, 30.0)
    night.car.soc = 27.0
    assert P.make_plan(night).car_grid
    # without the setting midday with PV is open as before
    free = inp_at(now, 4.5, 34.0)
    free.cheap_pv_battery_soc = None
    assert P.make_plan(free).car_grid


def test_cheap_topup_with_pv_below_the_battery_value_when_the_forecast_fills_it_anyway():
    """User 10.10.: 'the battery gets nearly full by the forecast anyway'. Below
    50 % cheap charging with PV is fine when the forecast x0.7 after the car's
    block until the PV end fills the battery and carries the house meanwhile.
    10.10. 13:03 (34 %, Forecast.Solar 18 kWh after the block, house ~2.2 kW)
    that's not enough; on a sunny day it is."""
    now = datetime(2026, 10, 10, 13, 0, tzinfo=TZ)
    prices = [P.PriceSlot(now.replace(hour=0) + timedelta(minutes=15 * i),
                          now.replace(hour=0) + timedelta(minutes=15 * (i + 1)), 0.1716) for i in range(96)]

    def plan(scale, battery=34.0):
        inp = _oct10(now, pv_kw=4.5, battery_soc=battery, car_soc=53.0, prices=prices, price_now=0.1716,
                     threshold=0.1904, cheap_target=80.0, scale=scale)
        inp.base_kw = 0.9
        inp.devices = [P.DeviceInput(id="pump", name="Pumpe", priority=3, decision_kw=1.34, is_on=True)]
        inp.battery_feeds_car = False
        inp.cheap_pv_battery_soc = 50.0
        inp.pv_end = now.replace(hour=17, minute=46)
        return inp, P.make_plan(inp)

    inp, p = plan(1.0)                      # 10.10.: ~9 kWh x0.7 after the block, ~19 kWh needed
    assert not P.battery_fills_anyway(inp) and not p.car_grid
    inp, p = plan(3.0)                      # sunny afternoon: PV slots open (the PV then brings the car anyway)
    assert P.battery_fills_anyway(inp) and P.cheap_pv_battery_ok(inp)
    inp, p = plan(3.0, battery=20.0)        # running block below 45 %: no stop while the forecast covers it
    inp.grid_running = True
    assert not P.cheap_pv_battery_stop(inp)


def test_charge_windows_show_the_planned_grid_charging_ahead():
    """Dashboard: the departure block and the cheap block are shown before
    they start - also with the car unplugged - with their mean price; without
    any need from the grid there is no window."""
    now = datetime(2026, 10, 6, 1, 0, tzinfo=TZ)
    dep = P.Departure(start=now.replace(hour=4, minute=30), target_soc=50.0, name="Frühschicht")
    car = P.CarInput(present=False, soc=35.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    prices = [P.PriceSlot(now + timedelta(minutes=15 * i), now + timedelta(minutes=15 * (i + 1)),
                          0.30 if i < 8 else 0.20) for i in range(16)]
    ws = P.charge_windows(base_inputs(now, car=car, departures=[dep], prices=prices))
    assert [w.kind for w in ws] == ["abfahrt"]
    w = ws[0]
    assert w.kwh > 10 and not w.running and w.start >= now.replace(hour=3) and w.end <= dep.start
    assert abs(w.price - 0.20) < 1e-9 and not w.estimated
    # cheap top-up to 80 % below the threshold: second window
    ws = P.charge_windows(base_inputs(now, car=car, departures=[dep], prices=prices, battery_soc=15.0,
                                      cheap_threshold=0.25, cheap_target_soc=80.0, price_now=0.30))
    assert [w.kind for w in ws] == ["abfahrt", "billig"] and ws[1].target_soc == 80.0
    assert ws[1].start >= now.replace(hour=3) and abs(ws[1].price - 0.20) < 1e-9
    # target already reached -> nothing planned
    car.soc = 55.0
    assert P.charge_windows(base_inputs(now, car=car, departures=[dep], prices=prices)) == []


def test_battery_reason_names_when_the_battery_would_be_empty():
    """11.10. live 03:56: battery 31 %, 13.8 kWh, night base 0.39 kW, miner
    0.142 kW -> 2.2 kWh / 0.53 kW: empty ~08:06, needed until 10:09."""
    now = datetime(2026, 10, 11, 3, 56, tzinfo=TZ)
    m = miner()
    m.decision_kw = 0.142
    plan = P.make_plan(base_inputs(now, battery_soc=31.0, battery_capacity_kwh=13.8, night_base_kw=0.39,
                                   devices=[m]))
    dec = plan.devices["miner"]
    assert dec.reason == "akku_reicht_nicht"
    assert abs((dec.battery_empty - now.replace(hour=8, minute=5)).total_seconds()) < 5 * 60
