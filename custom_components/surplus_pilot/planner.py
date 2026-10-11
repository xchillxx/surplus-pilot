"""The energy plan: one pure function that splits the PV power between the
car, the home battery and the managed devices.

No Home Assistant imports — the integration and the offline simulator
(tools/simulate.py) call exactly this code, so every simulation result is a
statement about the shipped logic.

Priority order (validated on 30 days of recorded data, see README):
  1. Car obligation   — what the car must still get for its next departure
                        target, spread over the PV hours left before it.
  2. Home battery     — enough to be full by the end of the PV day. When the
                        car leaves before that, the PV after the departure
                        is counted in (it can fill the battery on its own).
  3. Devices          — by priority; a running device's own draw counts as
                        available to it (the base load excludes it).
  4. Car top-up       — the rest, up to the car's charge limit.
     Exception: top-up goes BEFORE the devices when the car will not get a
     PV chance tomorrow (away during tomorrow's PV window, or tomorrow's
     forecast can't fill it) — then today is the day to charge.
  5. Whatever is left is exported.

If the forecast PV before a departure can't cover the obligation, the car
charges from the grid in the cheapest slots of a short window right before
the departure (as late as possible, so a sunnier-than-forecast day still
wins).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

# --- tuning (all validated in tools/simulate.py) ---
FORECAST_SAFETY = 0.7          # forecast is trusted at 70 %
DEVICE_HYSTERESIS_KW = 0.2     # on above need + 0.2, off below need - 0.2
BATTERY_MAX_TARGET_KW = 6.9    # never reserve more than the battery accepts
MIN_HOURS_LEFT = 0.25
BATTERY_PATH_BUFFER_H = 1.0    # battery must last until solar start + 1 h
EXPORT_GATE_KW = 0.15          # daytime battery path only while exporting
MUST_MIN_SURPLUS_KW = 0.5      # obligation only draws on real surplus
GRID_SAFETY_H = 0.25           # grid fallback: start at the latest this long before it gets too late
SLOT = timedelta(minutes=15)
PV_HOUR_MARGIN_KW = 1.0        # an hour counts as "PV hour" when forecast > base + 1 kW
CHEAP_FORECAST_TRUST = 1.0     # optional cheap top-up: trust the forecast fully (no guarantee needed)
TOMORROW_SHARE = 0.5           # tomorrow must cover this share of the car's missing energy
TOMORROW_FORECAST_TRUST = 1.0  # the top-up is optional: no extra safety factor (x0.7 put it before the devices too often)
DEVICE_MIN_RUN_H = 1.0         # a device only starts when it can run at least this long
MIN_LIFT_PV_SHARE = 0.5        # obligation at minimum current only while PV carries half of it ...
MIN_LIFT_PV_SHARE_KEEP = 0.4   # ... (0.4 to keep an already charging car going)
CHEAP_BATTERY_MARGIN = 3.0     # cheap grid top-up only with the home battery at most this far above its min SoC
CHEAP_PV_SOC_HYSTERESIS = 5.0  # a running cheap block with PV stops this far below the battery floor
GRID_BLOCK_TOLERANCE = 0.005   # a running grid block continues while the price is within 0.5 ct of the block's mean
FEED_CAR_MIN_KW = 1.0          # battery-feeds-car detection: car must draw at least this ...
FEED_DEFICIT_KW = 0.5          # ... with at least this much missing beyond the PV ...
FEED_SOC_MARGIN = 5.0          # ... and the battery this far above its min SoC (it could discharge)


@dataclass
class ForecastHour:
    end: datetime   # energy of the hour ENDING here (Forecast.Solar convention)
    kwh: float


@dataclass
class PriceSlot:
    start: datetime
    end: datetime
    price: float


@dataclass
class Departure:
    start: datetime
    target_soc: float
    name: str = ""
    returns: datetime | None = None


@dataclass
class DeviceInput:
    id: str
    name: str
    priority: int
    decision_kw: float          # what the device is expected to draw
    is_on: bool                 # physically on right now
    enabled: bool = True
    in_window: bool = True
    window_end: datetime | None = None
    depends_on: str | None = None
    forced: bool = False        # min daily runtime must be enforced now
    soc_reserve: float = 0.0    # battery path only above this home-battery SoC


@dataclass
class CarInput:
    present: bool               # home and plugged in
    soc: float | None
    limit_soc: float
    capacity_kwh: float
    efficiency: float
    min_kw: float
    max_kw: float
    current_kw: float = 0.0


@dataclass
class Inputs:
    now: datetime
    pv_kw: float
    base_kw: float              # house load WITHOUT car and managed devices
    battery_soc: float | None   # None = no home battery
    battery_capacity_kwh: float
    battery_min_soc: float
    export_kw: float | None
    solar_start: datetime       # next (or today's, if still ahead) usable PV start
    solar_start_today: datetime
    sunset: datetime            # next sunset
    pv_end: datetime            # sunset - margin
    night_base_kw: float
    forecast: list[ForecastHour] = field(default_factory=list)
    prices: list[PriceSlot] = field(default_factory=list)
    departures: list[Departure] = field(default_factory=list)
    car: CarInput | None = None
    devices: list[DeviceInput] = field(default_factory=list)
    allow_grid_for_car: bool = True
    # False: the home battery is set not to discharge into the car (e.g. in
    # the inverter app) - grid charging then leaves it alone, also at midday
    battery_feeds_car: bool = True
    # cheap top-up while there is PV only with the home battery at least this
    # full (None = no limit): the inverter gives the PV to the wallbox first and
    # the battery carries the house (10.10.: 34 -> 10 % during a midday block).
    # cheap_pv_locked: such a block was stopped for the battery today.
    cheap_pv_battery_soc: float | None = None
    cheap_pv_locked: bool = False
    # the car is grid charging right now (last decision, or drawing near its
    # maximum): a started grid block runs through instead of stop-and-go
    grid_running: bool = False
    # the car's planned grid block (start, end) from the car plan: devices
    # don't plan PV in it and don't start shortly before it
    car_block: tuple[datetime, datetime] | None = None
    # Price knowledge for grid charging: published prices (`prices`), an
    # estimate per hour of day for the not yet published ones (median of the
    # last days), the current price and the "cheap" threshold (percentile of
    # the last days). cheap_target_soc: grid top-up target while it's cheap
    # (None = off).
    price_profile: dict[int, float] = field(default_factory=dict)
    price_now: float | None = None
    cheap_threshold: float | None = None
    cheap_target_soc: float | None = None
    # Between two car decisions the car's current draw is fixed: the devices
    # are then planned on what's left after it (car is not re-planned).
    car_fixed_kw: float | None = None


@dataclass
class DeviceDecision:
    on: bool
    reason: str
    path: str = ""   # "ueberschuss" | "akku" | "pflicht" | ""
    battery_empty: datetime | None = None   # akku_reicht_nicht: home battery at its reserve with this device


@dataclass
class Plan:
    battery_target_kw: float
    battery_mode: str
    car_kw: float
    car_must_kw: float
    car_grid: bool
    car_reason: str
    car_first: bool
    need_kwh: float
    uncovered_kwh: float
    next_departure: Departure | None
    devices: dict[str, DeviceDecision]
    budget: dict
    car_block: tuple[datetime, datetime] | None = None   # planned grid block (now or later)


# ---------------------------------------------------------------- helpers

def forecast_kwh(rows: list[ForecastHour], start: datetime, end: datetime) -> float:
    """Forecast kWh between two instants (partial hours pro rata)."""
    if end <= start:
        return 0.0
    total = 0.0
    for r in rows:
        lo, hi = max(r.end - timedelta(hours=1), start), min(r.end, end)
        if hi > lo:
            total += r.kwh * (hi - lo).total_seconds() / 3600.0
    return total


def forecast_surplus_kwh(rows, start, end, base_kw, safety=FORECAST_SAFETY) -> tuple[float, float]:
    """(kWh of forecast PV above the base load, hours with useful PV)."""
    if end <= start:
        return 0.0, 0.0
    total, pv_hours = 0.0, 0.0
    for r in rows:
        lo, hi = max(r.end - timedelta(hours=1), start), min(r.end, end)
        if hi <= lo:
            continue
        frac = (hi - lo).total_seconds() / 3600.0
        kw = r.kwh * safety  # hourly kWh == mean kW
        total += max(0.0, kw - base_kw) * frac
        if kw > base_kw + PV_HOUR_MARGIN_KW:
            pv_hours += frac
    return total, pv_hours


def car_pv_alone_kwh(inp: Inputs, end: datetime) -> float:
    """Forecast kWh until `end` from the hours whose surplus (x safety) alone
    carries the car's minimum current - charging then needs neither the
    home battery nor the grid."""
    car = inp.car
    total = 0.0
    for r in inp.forecast:
        lo, hi = max(r.end - timedelta(hours=1), inp.now), min(r.end, end)
        if hi <= lo:
            continue
        kw = r.kwh * FORECAST_SAFETY - inp.base_kw - 0.3
        if kw >= car.min_kw:
            total += min(kw, car.max_kw) * (hi - lo).total_seconds() / 3600.0
    return total


def battery_feed_evidence(car_kw: float, pv_kw: float, load_kw: float, battery_kw: float | None,
                          battery_soc: float | None, battery_min_soc: float) -> bool | None:
    """One reading of whether the home battery discharges into the car
    (battery_kw: + charging, - discharging; load_kw includes the car). Only
    a reading with the car charging, power missing beyond the PV and energy
    left in the battery says anything: a discharge clearly beyond the
    house's own gap -> True; the battery covering at most the house while
    the car draws from the grid -> False. The inverter may change this on
    its own (e.g. a price-driven winter mode), so it is re-read all the time."""
    if (car_kw < FEED_CAR_MIN_KW or battery_kw is None or battery_soc is None
            or battery_soc < battery_min_soc + FEED_SOC_MARGIN):
        return None
    deficit = load_kw - pv_kw
    if deficit < FEED_DEFICIT_KW:
        return None
    house_gap = max(0.0, load_kw - car_kw - pv_kw)
    discharge = max(0.0, -battery_kw)
    if discharge > house_gap + 0.5:
        return True
    if discharge < house_gap + 0.2 and deficit - discharge > FEED_DEFICIT_KW:
        return False
    return None


def next_departure(deps: list[Departure], now: datetime) -> Departure | None:
    future = [d for d in deps if d.start > now]
    return min(future, key=lambda d: d.start) if future else None


def battery_target_kw(inp: Inputs, dep: Departure | None) -> tuple[float, str]:
    """Charge rate the home battery needs to be full by the PV end. With a
    departure before that, the PV after it (forecast x safety minus base
    load) can fill part of the battery on its own."""
    if inp.battery_soc is None:
        return 0.0, "kein_akku"
    missing = max(0.0, (100.0 - inp.battery_soc) / 100.0 * inp.battery_capacity_kwh)
    if missing <= 0:
        return 0.0, "voll"
    hours_left = (inp.pv_end - inp.now).total_seconds() / 3600.0
    target = min(BATTERY_MAX_TARGET_KW, missing / max(hours_left, MIN_HOURS_LEFT))
    mode = "frist"
    # only worth it while the car can still use the PV it frees up
    car_home = inp.car is not None and inp.car.present and (
        inp.car.soc is None or inp.car.soc < inp.car.limit_soc)
    if dep is not None and car_home and inp.now < dep.start < inp.pv_end and inp.forecast:
        after = forecast_kwh(inp.forecast, dep.start, inp.pv_end) * FORECAST_SAFETY
        h_dep = (dep.start - inp.now).total_seconds() / 3600.0
        h_after = (inp.pv_end - dep.start).total_seconds() / 3600.0
        fill_after = min(BATTERY_MAX_TARGET_KW * h_after, max(0.0, after - inp.base_kw * h_after), missing)
        target = min(BATTERY_MAX_TARGET_KW, max(0.0, missing - fill_after) / max(h_dep, MIN_HOURS_LEFT))
        mode = "abfahrt"
    return max(0.0, target), mode


def is_daytime(inp: Inputs) -> bool:
    """Usable PV day: from today's solar start until the PV end. Dusk counts
    as night (battery path rules)."""
    # pv_end is derived from the NEXT sunset: after today's sunset it already
    # points to tomorrow, which must read as night.
    return inp.solar_start_today <= inp.now < inp.pv_end and inp.pv_end.date() == inp.now.date()


def car_pv_chance_tomorrow(inp: Inputs, car_missing_kwh: float) -> bool:
    """Will the car be home during most of tomorrow's PV window AND is the
    forecast good enough to fill it then? If not, today is the day."""
    now = inp.now
    start = inp.solar_start if inp.solar_start.date() > now.date() else inp.solar_start + timedelta(days=1)
    end = inp.pv_end if inp.pv_end.date() > now.date() else inp.pv_end + timedelta(days=1)
    if end <= start:
        return True
    window_h = (end - start).total_seconds() / 3600.0
    away_h = 0.0
    for d in inp.departures:
        ret = d.returns
        if ret is None:
            continue  # unknown return: assume the car is back for the PV day
        lo, hi = max(d.start, start), min(ret, end)
        if hi > lo:
            away_h += (hi - lo).total_seconds() / 3600.0
    if away_h > 0.5 * window_h:
        return False
    if not inp.forecast:
        return True
    surplus, _ = forecast_surplus_kwh(inp.forecast, start, end, inp.night_base_kw, TOMORROW_FORECAST_TRUST)
    if surplus <= 0 and forecast_kwh(inp.forecast, start, end) == 0:
        return True  # no forecast published for tomorrow yet -> don't panic
    battery_missing = 0.0  # battery is usually refilled first tomorrow
    if inp.battery_soc is not None:
        battery_missing = 0.5 * inp.battery_capacity_kwh
    return surplus - battery_missing >= TOMORROW_SHARE * car_missing_kwh


def battery_lasts(inp: Inputs, planned_on: dict[str, bool], extra: DeviceInput | None) -> bool:
    """Battery path: would the home battery still carry the night base load
    plus all planned devices (each until its window end) until solar start
    + buffer?"""
    if inp.battery_soc is None:
        return False
    if extra is not None and inp.battery_soc < extra.soc_reserve:
        return False
    end = inp.solar_start + timedelta(hours=BATTERY_PATH_BUFFER_H)
    h = max(0.0, (end - inp.now).total_seconds() / 3600.0)
    avail = max(0.0, (inp.battery_soc - inp.battery_min_soc) / 100.0 * inp.battery_capacity_kwh)
    load = inp.night_base_kw * h
    for d in inp.devices:
        if planned_on.get(d.id) or (extra is not None and d.id == extra.id):
            hh = h
            if d.window_end is not None:
                hh = max(0.0, min(h, (d.window_end - inp.now).total_seconds() / 3600.0))
            load += d.decision_kw * hh
    return avail >= load


def battery_empty_at(inp: Inputs, planned_on: dict[str, bool], extra: DeviceInput) -> datetime | None:
    """When the home battery would reach its reserve with the same load as
    battery_lasts (night base load + planned devices + this one, each until
    its window end) - the reason's "why" in one time."""
    if inp.battery_soc is None:
        return None
    left = max(0.0, (inp.battery_soc - inp.battery_min_soc) / 100.0 * inp.battery_capacity_kwh)
    devs = [d for d in inp.devices if planned_on.get(d.id) or d.id == extra.id]
    t, step = inp.now, timedelta(minutes=5)
    for _ in range(24 * 12):
        kw = inp.night_base_kw + sum(d.decision_kw for d in devs if d.window_end is None or d.window_end > t)
        use = kw * step.total_seconds() / 3600.0
        if use >= left:
            return t + step * (left / use)
        left -= use
        t += step
    return None


def _hours(inp: Inputs, t: datetime) -> float:
    return (t - inp.now).total_seconds() / 3600.0


def pv_hours_left(inp: Inputs) -> float:
    """Hours of usable PV left today (0 after the PV end; after sunset
    pv_end already points to tomorrow)."""
    if inp.pv_end.date() != inp.now.date():
        return 0.0
    return max(0.0, _hours(inp, inp.pv_end))


def battery_run_ok(inp: Inputs, planned_on: dict[str, bool], d: DeviceInput) -> bool:
    """Battery path start: after DEVICE_MIN_RUN_H with the current house
    load, the planned devices and this one, the battery must still be above
    the device's reserve - otherwise it's off again after a few minutes."""
    if inp.battery_soc is None:
        return False
    draw = inp.base_kw + d.decision_kw + sum(x.decision_kw for x in inp.devices if planned_on.get(x.id))
    drain = max(0.0, draw - inp.pv_kw) * DEVICE_MIN_RUN_H
    after = inp.battery_soc - drain / max(inp.battery_capacity_kwh, 0.1) * 100.0
    return after >= max(d.soc_reserve, inp.battery_min_soc)


def device_surplus_hours(forecast: list[ForecastHour], now: datetime, end: datetime, base_kw: float, kw: float,
                         battery_soc: float | None, battery_capacity_kwh: float, pv_end: datetime,
                         other_kw: float = 0.0, car_block: tuple[datetime, datetime] | None = None,
                         car_kw: float = 0.0) -> float:
    """Hours until `end` in which the forecast surplus (x safety, minus the
    base load, the battery's charge rate to be full by the PV end, the
    higher-priority devices and the car during its planned grid block - it
    takes the PV there) covers this device's draw."""
    rate = 0.0
    if battery_soc is not None and pv_end.date() == now.date() and pv_end > now:
        missing = max(0.0, (100.0 - battery_soc) / 100.0 * battery_capacity_kwh)
        rate = min(BATTERY_MAX_TARGET_KW, missing / max(_h(now, pv_end), MIN_HOURS_LEFT))
    hours = 0.0
    for r in forecast:
        lo, hi = max(r.end - timedelta(hours=1), now), min(r.end, end)
        if hi <= lo:
            continue
        free = r.kwh * FORECAST_SAFETY - base_kw - rate - other_kw
        h = _h(lo, hi)
        ov = 0.0  # minutes of the car's grid block: it takes the PV there
        if car_block is not None:
            ov = max(0.0, _h(max(lo, car_block[0]), min(hi, car_block[1])))
        if free >= kw:
            hours += h - ov
        if ov and free - car_kw >= kw:
            hours += ov
    return hours


def min_runtime_start(now: datetime, need_h: float, end: datetime, prices: list[PriceSlot],
                      surplus_h: float) -> datetime | None:
    """Start of the forced run that completes a device's minimum daily
    runtime, or None (not needed, or no prices and still time). It is one
    contiguous block of at least DEVICE_MIN_RUN_H in the cheapest part of
    the window - never a few scattered quarter hours. When the forecast
    surplus covers the need, only the latest stretch of the window counts,
    so a sunny afternoon still wins; when it doesn't, the whole rest of the
    window counts (08.10. live: foggy morning, battery at 27 %, the cheap
    midday was missed because the planner only looked at the last 8 h)."""
    if need_h <= 0:
        return None
    left_h = _h(now, end)
    if left_h <= need_h + 0.1:
        return now
    block = timedelta(hours=max(need_h, DEVICE_MIN_RUN_H))
    earliest = now
    if surplus_h >= need_h:
        earliest = max(now, end - timedelta(hours=max(2 * need_h, need_h + 2)))
    if earliest + block > end:
        return max(now, end - block)
    slots = sorted((p for p in prices if p.end > earliest and p.start < end), key=lambda p: p.start)

    def cost(s: datetime) -> float | None:
        e, t, total = s + block, s, 0.0
        for p in slots:
            if p.end <= t or p.start >= e:
                continue
            if p.start > t + timedelta(seconds=1):
                return None   # gap in the known prices
            hi = min(p.end, e)
            total += p.price * (hi - t).total_seconds()
            t = hi
            if t >= e:
                return total
        return None

    best, best_cost = None, None
    for s in [earliest] + [p.start for p in slots if p.start > earliest and p.start + block <= end]:
        c = cost(s)
        if c is not None and (best_cost is None or c < best_cost - 1e-6):
            best, best_cost = s, c
    return best


def min_runtime_finish(is_on: bool, need_h: float) -> bool:
    """A running device with at most DEVICE_MIN_RUN_H of its minimum runtime
    left keeps running until it's done: stopping now would only mean a
    separate catch-up run of at least that long later (09.10. live: pump off
    at 13:24 with 0.3 h missing, forced on again at 13:33 for 1.1 h)."""
    return is_on and 0 < need_h <= DEVICE_MIN_RUN_H


def _h(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 3600.0


def _price_at(inp: Inputs, t: datetime) -> tuple[float | None, bool]:
    """(price, published) for the slot starting at t; unpublished slots use
    the hour-of-day estimate."""
    for p in inp.prices:
        if p.start <= t < p.end:
            return p.price, True
    return inp.price_profile.get(t.hour), False


def _floor_slot(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def _best_block(slots: list[tuple[datetime, float | None]], n: int, prefer_late: bool) -> tuple[datetime, int, float] | None:
    """Cheapest run of n consecutive 15-min slots (None = not usable, it
    breaks a run): (start, length, mean price). Charging is one block, not
    scattered quarter hours - every stop and start costs a (possibly
    billed) car command and stresses the charging hardware. When no run is
    n long, the longest possible one counts."""
    n = max(1, min(n, len(slots)))
    while n >= 1:
        best = None
        for i in range(len(slots) - n + 1):
            win = slots[i:i + n]
            if any(p is None for _, p in win) or any(win[k + 1][0] - win[k][0] != SLOT for k in range(n - 1)):
                continue
            cost = sum(p for _, p in win)
            if (best is None or cost < best[2] * n - 1e-9
                    or (prefer_late and abs(cost - best[2] * n) <= 1e-9)):
                best = (win[0][0], n, cost / n)
        if best is not None:
            return best
        n -= 1
    return None


def _span(block: tuple[datetime, int, float] | None) -> tuple[datetime, datetime] | None:
    return None if block is None else (block[0], block[0] + block[1] * SLOT)


def _in_block(inp: Inputs, block: tuple[datetime, int, float] | None) -> bool:
    if block is None:
        return False
    start, n, mean = block
    if start <= inp.now < start + n * SLOT:
        return True
    # a running block goes on while the price stays close to the best block
    price = inp.price_now if inp.price_now is not None else _price_at(inp, _floor_slot(inp.now))[0]
    return inp.grid_running and price is not None and price <= mean + GRID_BLOCK_TOLERANCE


def _grid_slot_now(inp: Inputs, dep: Departure, uncovered_kwh: float,
                   max_kw: float) -> tuple[bool, tuple[datetime, int, float | None] | None]:
    """Grid fallback for a departure target the PV forecast can't reach:
    charge in the cheapest block of consecutive 15-min slots between now and
    the departure (unpublished prices estimated per hour of day — so a cheap
    midday today can win over a night whose prices aren't out yet). Ties go
    to the later block (a sunnier hour than forecast may still help). Once
    there is just enough time left, charge regardless of price."""
    need_slots = max(1, math.ceil(uncovered_kwh / max(max_kw, 0.1) / 0.25))
    now_slot = _floor_slot(inp.now)
    slots: list[tuple[datetime, float | None]] = []
    t = now_slot
    while t < dep.start:
        slots.append((t, _price_at(inp, t)[0]))
        t += SLOT
    left_h = (dep.start - inp.now).total_seconds() / 3600.0
    if len(slots) <= need_slots or left_h <= need_slots * 0.25 + GRID_SAFETY_H:
        return True, (now_slot, len(slots), None)
    if not any(x[1] is not None for x in slots):
        return False, None  # no price idea at all: wait for the safety start above
    # one block, equally cheap -> the later one (a sunnier hour may still help)
    block = _best_block(slots, need_slots, prefer_late=True)
    return _in_block(inp, block), block


def _cheap_topup_now(inp: Inputs, dep: Departure | None,
                     surplus: float) -> tuple[bool, float, tuple[datetime, int, float] | None, bool]:
    """Grid top-up while the price is below the cheap threshold — only for
    energy the PV forecast won't bring anyway before the next departure
    (24 h without one), and only in the cheapest published slots until then
    (10.10. live: charged at 05:00 for 18.9 ct, the midday before the
    departure was 17.2 ct). The block is planned even while the price is
    still above the threshold, so the devices know it in advance. Last value:
    the price is cheap now but only the home-battery rule for PV hours holds
    it back (status `billig_akku` instead of "too little surplus")."""
    car = inp.car
    if (inp.cheap_target_soc is None or inp.cheap_threshold is None
            or car is None or car.soc is None or not car.present):
        return False, 0.0, None, False
    target = min(inp.cheap_target_soc, car.limit_soc)
    if car.soc >= target:
        return False, 0.0, None, False
    if inp.battery_feeds_car:
        # the home battery covers any import first: with energy left it
        # would just empty itself into the car (10.10. live: 05:00, battery
        # 36 -> 12 % in 45 min, then the house bought at 22-24 ct all
        # morning); and with PV for the car there's no top-up at all
        if surplus >= car.min_kw:
            return False, 0.0, None, False
        if inp.battery_soc is not None and inp.battery_soc > inp.battery_min_soc + CHEAP_BATTERY_MARGIN:
            return False, 0.0, None, False
    need = (target - car.soc) / 100.0 * car.capacity_kwh / car.efficiency
    end = dep.start if dep is not None else inp.now + timedelta(hours=24)
    pv, _ = forecast_surplus_kwh(inp.forecast, inp.now, end, inp.base_kw + 0.3, CHEAP_FORECAST_TRUST)
    battery_missing = 0.0
    if inp.battery_soc is not None:
        battery_missing = max(0.0, (100.0 - inp.battery_soc) / 100.0 * inp.battery_capacity_kwh)
    from_grid = need - max(0.0, pv - battery_missing)
    if from_grid <= 0.5:
        return False, 0.0, None, False
    in_block, block = _cheapest_free_slot_now(inp, end, from_grid, car.max_kw)
    now_ok = inp.price_now is not None and inp.price_now <= inp.cheap_threshold
    waits = (now_ok and not in_block and not inp.battery_feeds_car and inp.pv_kw > inp.base_kw + 0.3
             and not cheap_pv_battery_ok(inp))
    return in_block and now_ok, from_grid, block, waits


def cheap_pv_battery_ok(inp: Inputs) -> bool:
    """May cheap grid charging use slots with PV? With the home battery at
    least `cheap_pv_battery_soc` full, or below that if the cautious forecast
    still fills it by the PV end anyway (`battery_fills_anyway`). A running
    block goes on down to CHEAP_PV_SOC_HYSTERESIS below the value; once
    stopped for the battery it stays off in PV for the day (no stop-and-go
    while PV refills the battery)."""
    if inp.cheap_pv_battery_soc is None or inp.battery_soc is None:
        return True
    if inp.cheap_pv_locked:
        return False
    floor = inp.cheap_pv_battery_soc - (CHEAP_PV_SOC_HYSTERESIS if inp.grid_running else 0.0)
    return inp.battery_soc >= floor or battery_fills_anyway(inp)


def cheap_pv_battery_stop(inp: Inputs) -> bool:
    """A running cheap block with PV must stop for the battery (-> lock for today)."""
    return (inp.grid_running and inp.cheap_pv_battery_soc is not None and inp.battery_soc is not None
            and not inp.battery_feeds_car and inp.pv_kw > inp.base_kw + 0.3
            and inp.battery_soc < inp.cheap_pv_battery_soc - CHEAP_PV_SOC_HYSTERESIS
            and not battery_fills_anyway(inp))


def battery_fills_anyway(inp: Inputs) -> bool:
    """Below the battery value cheap charging with PV is still fine when the
    forecast (x0.7) after the car's block until the PV end fills the battery
    and carries the house the whole time (during the block the inverter gives
    the PV to the car and the battery carries the house)."""
    car = inp.car
    if (inp.battery_soc is None or car is None or car.soc is None or inp.cheap_target_soc is None
            or inp.pv_end.date() != inp.now.date() or inp.pv_end <= inp.now):
        return False
    car_kwh = max(0.0, min(inp.cheap_target_soc, car.limit_soc) - car.soc) / 100.0 * car.capacity_kwh / car.efficiency
    block_end = inp.now + timedelta(hours=car_kwh / max(car.max_kw, 0.1))
    pv = forecast_kwh(inp.forecast, block_end, inp.pv_end) * FORECAST_SAFETY
    house_kw = inp.base_kw + sum(d.decision_kw for d in inp.devices if d.is_on)
    need = (100.0 - inp.battery_soc) / 100.0 * inp.battery_capacity_kwh + house_kw * _h(inp.now, inp.pv_end)
    return pv >= need


def _cheapest_free_slot_now(inp: Inputs, end: datetime, kwh: float,
                            max_kw: float) -> tuple[bool, tuple[datetime, int, float] | None]:
    """Is now inside the cheapest block of consecutive PUBLISHED slots until
    `end` that are free for grid charging (and below the cheap threshold)? Equal blocks go to the earlier
    one - no point waiting for the same price. When the home battery feeds
    the car, slots with PV above the base load don't count (now: measured,
    later: forecast): grid charging there would take the PV the battery
    needs, it only charges from export."""
    need_slots = max(1, math.ceil(kwh / max(max_kw, 0.1) / 0.25))
    now_slot = _floor_slot(inp.now)
    pv_limit = inp.base_kw + 0.3
    no_pv_slots = inp.battery_feeds_car or not cheap_pv_battery_ok(inp)
    if no_pv_slots and inp.pv_kw > pv_limit:
        return False, None
    slots: list[tuple[datetime, float | None]] = []
    for p in sorted(inp.prices, key=lambda x: x.start):   # hourly prices -> 15-min slots
        t = max(_floor_slot(p.start), now_slot)
        while t < p.end and t < end:
            price: float | None = p.price if p.price <= inp.cheap_threshold else None
            if price is not None and no_pv_slots and t > now_slot:
                pv_kw = forecast_kwh(inp.forecast, t, t + SLOT) / 0.25
                if pv_kw * CHEAP_FORECAST_TRUST > pv_limit:
                    price = None
            slots.append((t, price))
            t += SLOT
    block = _best_block(slots, need_slots, prefer_late=False)
    return _in_block(inp, block), block


@dataclass
class ChargeWindow:
    kind: str                   # "abfahrt" (departure target) | "billig" (cheap top-up)
    target_soc: float
    kwh: float                  # energy expected from the grid
    start: datetime | None      # None: needed, but no fitting slot known yet
    end: datetime | None
    price: float | None = None  # mean price of the block (EUR/kWh)
    estimated: bool = False     # contains not yet published prices
    running: bool = False
    latest: datetime | None = None   # departure without prices: grid start at the latest


def charge_windows(inp: Inputs) -> list[ChargeWindow]:
    """The car's planned grid charging for the dashboard: when the departure
    target or the cheap top-up would charge from the grid because the PV
    forecast won't get there. Same rules as make_plan, but independent of the
    current surplus and of the car being plugged in (as if it were) - so the
    plan is visible ahead of time."""
    car = inp.car
    if car is None or car.soc is None or not inp.allow_grid_for_car:
        return []
    inp = replace(inp, car=replace(car, present=True))
    car = inp.car
    dep = next_departure(inp.departures, inp.now)
    out: list[ChargeWindow] = []
    now_slot = _floor_slot(inp.now)

    def window(kind, target, kwh, blk, running, n_need=None):
        start, n, mean = blk
        if n_need is not None:
            n = min(n, n_need)
        slots = [start + i * SLOT for i in range(n)]
        known = [_price_at(inp, t) for t in slots]
        if mean is None:
            vals = [p for p, _ in known if p is not None]
            mean = sum(vals) / len(vals) if vals else None
        return ChargeWindow(kind, target, kwh, start, start + n * SLOT, mean,
                            any(not pub for _, pub in known), running)

    if dep is not None and dep.target_soc > car.soc:
        need = (dep.target_soc - car.soc) / 100.0 * car.capacity_kwh / car.efficiency
        pv_before, _ = forecast_surplus_kwh(inp.forecast, inp.now, dep.start, inp.base_kw + 0.3)
        uncovered = max(0.0, need - pv_before)
        if uncovered > 0:
            need_slots = max(1, math.ceil(uncovered / max(car.max_kw, 0.1) / 0.25))
            running, blk = _grid_slot_now(inp, dep, uncovered, car.max_kw)
            if blk is not None:
                out.append(window("abfahrt", dep.target_soc, uncovered, blk, running, need_slots))
            else:
                latest = max(now_slot, dep.start - need_slots * SLOT - timedelta(hours=GRID_SAFETY_H))
                out.append(ChargeWindow("abfahrt", dep.target_soc, uncovered, None, None, latest=latest))

    if inp.cheap_target_soc is not None and inp.cheap_threshold is not None:
        running, kwh, blk, _ = _cheap_topup_now(inp, dep, inp.pv_kw - inp.base_kw)
        target = min(inp.cheap_target_soc, car.limit_soc)
        if blk is not None:
            out.append(window("billig", target, kwh, blk, running))
        elif kwh > 0:
            out.append(ChargeWindow("billig", target, kwh, None, None))
    return out


# ---------------------------------------------------------------- the plan

def make_plan(inp: Inputs) -> Plan:
    surplus = inp.pv_kw - inp.base_kw
    dep = next_departure(inp.departures, inp.now)
    target_b, b_mode = battery_target_kw(inp, dep)
    car = inp.car

    if inp.car_fixed_kw is not None:
        avail = surplus - inp.car_fixed_kw - target_b
        devices = _devices(inp, avail, inp.car_block)
        budget = {"pv_kw": inp.pv_kw, "grundlast_kw": inp.base_kw, "akku_ziel_kw": target_b,
                  "auto_kw": inp.car_fixed_kw, "geraete_kw": _used(inp, devices),
                  "frei_kw": avail - _used(inp, devices, surplus_only=True)}
        return Plan(target_b, b_mode, inp.car_fixed_kw, 0.0, False, "fest", False, 0.0, 0.0, dep,
                    devices, budget, inp.car_block)

    must_kw, need_kwh, uncovered, grid = 0.0, 0.0, 0.0, False
    block: tuple[datetime, datetime] | None = None
    car_reason = "kein_auto"
    car_ok = car is not None and car.present and (car.soc is None or car.soc < car.limit_soc)
    if car is not None and not car.present:
        car_reason = "nicht_da"
    elif car is not None and car.soc is not None and car.soc >= car.limit_soc:
        car_reason = "ladelimit_erreicht"

    if car_ok and car.soc is not None and dep is not None and dep.target_soc > car.soc:
        need_kwh = (dep.target_soc - car.soc) / 100.0 * car.capacity_kwh / car.efficiency
        pv_before, pv_h = forecast_surplus_kwh(inp.forecast, inp.now, dep.start, inp.base_kw + 0.3)
        if surplus > MUST_MIN_SURPLUS_KW:
            must_kw = min(car.max_kw, need_kwh / max(pv_h, MIN_HOURS_LEFT))
        uncovered = max(0.0, need_kwh - pv_before)
        if uncovered > 0 and inp.allow_grid_for_car and surplus < car.min_kw:
            grid, blk = _grid_slot_now(inp, dep, uncovered, car.max_kw)
            block = _span(blk)

    devices: dict[str, DeviceDecision] = {}
    budget = {"pv_kw": inp.pv_kw, "grundlast_kw": inp.base_kw, "akku_ziel_kw": target_b}

    grid_reason = "netz_pflicht"
    cheap_waits = False
    if not grid and car_ok and inp.allow_grid_for_car:
        cheap, cheap_kwh, cheap_block, cheap_waits = _cheap_topup_now(inp, dep, surplus)
        block = block or _span(cheap_block)
        if cheap:
            grid, grid_reason = True, "netz_billig"
            uncovered = max(uncovered, cheap_kwh)

    if grid:
        car_kw = car.max_kw
        avail = surplus - car_kw - target_b
        devices = _devices(inp, avail, block)
        budget.update(auto_pflicht_kw=car_kw, geraete_kw=_used(inp, devices), auto_rest_kw=0.0)
        return Plan(target_b, b_mode, car_kw, car_kw, True, grid_reason, False, need_kwh, uncovered,
                    dep, devices, budget, block)

    car_must = min(must_kw, max(surplus, 0.0)) if car_ok else 0.0
    avail = surplus - car_must - target_b

    car_first = False
    if car_ok:
        missing_to_limit = 0.0
        if car.soc is not None:
            missing_to_limit = (car.limit_soc - car.soc) / 100.0 * car.capacity_kwh / car.efficiency
        car_first = not car_pv_chance_tomorrow(inp, missing_to_limit)

    topup = 0.0
    if car_first:
        topup = max(0.0, min(avail, car.max_kw - car_must))
        devices = _devices(inp, avail - topup, block)
    else:
        devices = _devices(inp, avail, block)
        rest = avail - _used(inp, devices, surplus_only=True)
        if car_ok:
            topup = max(0.0, min(rest, car.max_kw - car_must))

    car_kw = car_must + topup if car_ok else 0.0
    if car_ok:
        if car_kw < car.min_kw:
            if must_kw > MUST_MIN_SURPLUS_KW and car_must > 0 and _lift_to_min(inp, dep, need_kwh, surplus):
                car_kw, car_reason = car.min_kw, "pflicht_minimum"  # battery/grid fill the gap
            else:
                car_kw, car_reason = 0.0, "billig_akku" if cheap_waits else "zu_wenig_ueberschuss"
        else:
            car_reason = ("pflicht" if topup < 0.05 else "pflicht_und_rest") if car_must > 0 else (
                "vorrang_rest" if car_first else "rest")
    budget.update(auto_pflicht_kw=car_must, geraete_kw=_used(inp, devices), auto_rest_kw=topup,
                  frei_kw=surplus - car_must - target_b - _used(inp, devices, surplus_only=True) - topup)
    return Plan(target_b, b_mode, car_kw, car_must, False, car_reason, car_first, need_kwh, uncovered,
                dep, devices, budget, block)


def _lift_to_min(inp: Inputs, dep: Departure, need_kwh: float, surplus: float) -> bool:
    """Raise the car to its minimum current for the departure target, the
    gap coming from the home battery or the grid? Only when the PV carries a
    real share of it right now AND the hours whose surplus alone carries the
    minimum won't bring the target anyway (10.10. live: 09:15, 0.65 kW
    surplus, battery at 12 % -> car at 3.45 kW, 2.8 kW from the battery and
    the grid at 22.7 ct while the midday was forecast at 3.5+ kW)."""
    car = inp.car
    share = MIN_LIFT_PV_SHARE_KEEP if car.current_kw >= 0.8 * car.min_kw else MIN_LIFT_PV_SHARE
    if surplus < share * car.min_kw:
        return False
    return car_pv_alone_kwh(inp, dep.start) < need_kwh


def _used(inp: Inputs, decisions: dict[str, DeviceDecision], surplus_only: bool = False) -> float:
    total = 0.0
    for d in inp.devices:
        dec = decisions.get(d.id)
        if dec and dec.on and (not surplus_only or dec.path in ("ueberschuss", "pflicht")):
            total += d.decision_kw
    return total


def _devices(inp: Inputs, remaining: float,
             car_block: tuple[datetime, datetime] | None = None) -> dict[str, DeviceDecision]:
    out: dict[str, DeviceDecision] = {}
    car_block = car_block or inp.car_block
    # a car grid block starting within the minimum run takes the PV: a device
    # started on PV now would be off again soon (10.10. live: miner and pump
    # on at 10:43, car started 10:45, both off again by 11:13)
    block_soon = car_block is not None and 0 <= _hours(inp, car_block[0]) < DEVICE_MIN_RUN_H
    planned: dict[str, bool] = {}
    day = is_daytime(inp)
    exporting = inp.export_kw is not None and inp.export_kw > EXPORT_GATE_KW
    battery_full = inp.battery_soc is not None and inp.battery_soc >= 98.0
    for d in sorted(inp.devices, key=lambda x: x.priority):
        if not d.enabled:
            out[d.id] = DeviceDecision(False, "deaktiviert")
            continue
        if not d.in_window:
            out[d.id] = DeviceDecision(False, "ausserhalb_zeitfenster")
            continue
        if d.depends_on and not planned.get(d.depends_on):
            out[d.id] = DeviceDecision(False, "wartet_auf_abhaengigkeit")
            continue
        if d.forced:
            out[d.id] = DeviceDecision(True, "mindestlaufzeit", "pflicht")
            planned[d.id] = True
            remaining -= d.decision_kw
            continue
        # starting is only worth it for a real run: not shortly before the
        # window closes, the PV day ends or the battery hits the reserve
        # (07.10. live: pump on 30 min before PV end, and at 86 % with an
        # 85 % reserve - off again after 1 h and 17 min)
        starting = not d.is_on
        if starting and d.window_end is not None and _hours(inp, d.window_end) < DEVICE_MIN_RUN_H:
            out[d.id] = DeviceDecision(False, "zu_kurz")
            continue
        short = False
        need = d.decision_kw + (-DEVICE_HYSTERESIS_KW if d.is_on else DEVICE_HYSTERESIS_KW)
        if remaining >= need:
            if starting and (pv_hours_left(inp) < DEVICE_MIN_RUN_H or block_soon):
                short = True
            else:
                out[d.id] = DeviceDecision(True, "ueberschuss", "ueberschuss")
                planned[d.id] = True
                remaining -= d.decision_kw
                continue
        gate = (not day) or exporting or battery_full
        if gate and battery_lasts(inp, planned, d):
            if starting and not battery_run_ok(inp, planned, d):
                short = True
            else:
                out[d.id] = DeviceDecision(True, "akku_reicht", "akku")
                planned[d.id] = True
                continue
        # name the rule that actually blocked it: in daylight without export
        # the battery path is closed anyway, the reserve is not the reason
        if short:
            reason = "zu_kurz"
        elif gate and inp.battery_soc is not None and inp.battery_soc < d.soc_reserve:
            reason = "akku_reserve"
        elif day:
            reason = "kein_ueberschuss"
        else:
            reason = "akku_reicht_nicht"
        empty = battery_empty_at(inp, planned, d) if reason == "akku_reicht_nicht" else None
        out[d.id] = DeviceDecision(False, reason, battery_empty=empty)
    return out


def cap_car_topup(plan: Plan, cap_kw: float, car_min_kw: float) -> Plan:
    """Limit the car's top-up (everything above its obligation) to what a
    more recent window allows. The 30-min average lags behind a falling PV
    curve and the gap would come from the home battery; on a rising curve
    the battery takes the extra, so only the downside is capped."""
    if plan.car_grid or plan.car_reason not in ("rest", "vorrang_rest", "pflicht_und_rest"):
        return plan
    must = plan.car_must_kw
    kw = max(must, min(plan.car_kw, cap_kw))
    if kw >= plan.car_kw - 1e-9:
        return plan
    reason = plan.car_reason
    if kw < car_min_kw:
        kw, reason = (car_min_kw, "pflicht_minimum") if must > 0 else (0.0, "zu_wenig_ueberschuss")
    elif reason == "pflicht_und_rest" and kw - must < 0.05:
        reason = "pflicht"
    budget = {**plan.budget, "auto_rest_kw": max(0.0, kw - must), "auto_begrenzt_auf_kw": round(cap_kw, 2)}
    return replace(plan, car_kw=kw, car_reason=reason, budget=budget)
