"""Surplus Pilot coordinator: reads the house every minute, plans with
planner.make_plan, and acts on devices (every minute, debounced) and the
car (every 15 min on a 30-min average, or immediately on plug-in)."""
from __future__ import annotations

import logging
import statistics
from collections import deque
from datetime import datetime, time as dtime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import planner as P
from .car import CarController
from .const import (
    CAR_AVERAGE_MINUTES,
    CAR_DECISION_MINUTES,
    CAR_MIN_COVERAGE_MINUTES,
    CALIBRATION_INTERVAL,
    CLIMATE_DELAY_FACTOR,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CAR,
    CONF_CAR_ALLOW_GRID,
    CONF_CAR_BATTERY_FEEDS,
    CONF_CAR_CAPACITY_KWH,
    CONF_CAR_CHARGING_SENSOR,
    CONF_CAR_CURRENT_ENTITY,
    CONF_CAR_EFFICIENCY,
    CONF_CAR_LIMIT_DEFAULT,
    CONF_CAR_LIMIT_ENTITY,
    CONF_CAR_PAUSE_ENTITY,
    CONF_CAR_PAUSE_STATE,
    CONF_CAR_PLUGGED_FALLBACK,
    CONF_CAR_PLUGGED_SENSOR,
    CONF_CAR_POWER_SENSOR,
    CONF_CAR_SOC_FALLBACK,
    CONF_CAR_SOC_SENSOR,
    CONF_CAR_TRACKER,
    CONF_CAR_TRACKER_FALLBACK,
    CONF_DEV_CLIMATE_MODE,
    CONF_DEV_DEPENDS_ON,
    CONF_DEV_ENTITY,
    CONF_DEV_ID,
    CONF_DEV_KIND,
    CONF_DEV_MIN_RUNTIME_H,
    CONF_DEV_NAME,
    CONF_DEV_POWER_KW,
    CONF_DEV_POWER_SENSOR,
    CONF_DEV_PRIORITY,
    CONF_DEV_SCHEDULE,
    CONF_DEV_SOC_RESERVE,
    CONF_DEV_WINDOW_END,
    CONF_DEV_WINDOW_START,
    CONF_DEVICES,
    CONF_GRID_EXPORT_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_PRICE_HISTORY_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_PRICE_SOURCE,
    CONF_PV_END_BEFORE_SUNSET_H,
    CONF_PV_SENSOR,
    CONF_STALE_MINUTES,
    CONF_TIBBER_HOME,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_CAR_CAPACITY_KWH,
    DEFAULT_CAR_EFFICIENCY,
    DEFAULT_CAR_LIMIT,
    DEFAULT_PV_END_BEFORE_SUNSET_H,
    PRICE_ARCHIVE_DAYS,
    PRICE_MIN_SAMPLES_H,
    DEFAULT_SOLAR_OFFSETS,
    DEFAULT_STALE_MINUTES,
    DEVICE_FORCED_ON_DELAY_S,
    DEVICE_OFF_DELAY_S,
    DEVICE_ON_DELAY_S,
    DEVICE_SMOOTH_MINUTES,
    DOMAIN,
    KIND_CLIMATE,
    LOG_LENGTH,
    MODE_AUTO,
    PRICE_SENSOR,
    PRICE_TIBBER,
    FEED_CAR_STEADY_MIN,
    FEED_CONFIRM_READINGS,
    FEED_EXPIRY_DAYS,
    UPDATE_INTERVAL_SECONDS,
)
from .departures import all_departures, parse_hhmm
from .solar_calibration import SolarOffsetCalibrator
from .sources import (
    age_minutes,
    async_solar_forecast,
    async_tibber_prices,
    is_on,
    number,
    plugged as plugged_state,
    power_kw,
    sensor_prices,
    tracker_home,
)
from .store import PilotStore

_LOGGER = logging.getLogger(__name__)

REASON_TEXT = {
    "deaktiviert": "deaktiviert", "ausserhalb_zeitfenster": "außerhalb Zeitfenster",
    "wartet_auf_abhaengigkeit": "wartet auf Abhängigkeit", "mindestlaufzeit": "Mindestlaufzeit wird erzwungen",
    "ueberschuss": "PV-Überschuss reicht", "akku_reicht": "Hausakku reicht bis Sonnenaufgang",
    "akku_reserve": "Hausakku unter Geräte-Reserve", "kein_ueberschuss": "kein Überschuss übrig",
    "akku_reicht_nicht": "Hausakku würde nicht bis Sonnenaufgang reichen",
    "zu_kurz": "würde nur kurz laufen (PV-Ende, Zeitfenster oder Reserve zu nah)",
    "netz_pflicht": "Abfahrtsziel, aus dem Netz (günstigste Slots)",
    "netz_billig": "Strom gerade billig, PV reicht nicht", "pflicht_minimum": "Abfahrtsziel, Mindeststrom",
    "zu_wenig_ueberschuss": "zu wenig Überschuss", "pflicht": "Abfahrtsziel aus PV",
    "billig_akku": "Strom billig, aber Hausakku zuerst",
    "pflicht_und_rest": "Abfahrtsziel + Rest", "vorrang_rest": "Vorrang, morgen keine PV-Chance",
    "rest": "Rest nach den Geräten", "haelt_minimum": "hält Minimum", "ladelimit_erreicht": "Ladelimit erreicht",
}


def device_is_on(hass: HomeAssistant, dev: dict) -> bool:
    st = hass.states.get(dev[CONF_DEV_ENTITY])
    if st is None:
        return False
    if dev.get(CONF_DEV_KIND) == KIND_CLIMATE:
        return st.state not in ("off", "unavailable", "unknown")
    return st.state == "on"


async def device_switch(hass: HomeAssistant, dev: dict, on: bool) -> None:
    ent = dev[CONF_DEV_ENTITY]
    if dev.get(CONF_DEV_KIND) == KIND_CLIMATE:
        mode = dev.get(CONF_DEV_CLIMATE_MODE, "heat") if on else "off"
        await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": ent, "hvac_mode": mode},
                                       blocking=False)
    else:
        await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": ent},
                                       blocking=False)


_UNSET = object()
CAR_CAP_MINUTES = 15         # the top-up must fit this recent window too
URGENT_DEPARTURE_H = 2


class PilotCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS))
        self.entry = entry
        self.cfg: dict = dict(entry.data)
        self.devices: list[dict] = list(self.cfg.get(CONF_DEVICES) or [])
        self.car_cfg: dict | None = self.cfg.get(CONF_CAR) or None
        self.store = PilotStore(hass, entry.entry_id)
        self.calibrator = SolarOffsetCalibrator(hass, entry.entry_id, self.cfg[CONF_PV_SENSOR])
        self.car = CarController(hass, self.car_cfg, self.store.count_command, self._log) if self.car_cfg else None
        self.samples: deque = deque()            # (ts, pv, base, car_kw)
        self.log: deque = deque(maxlen=LOG_LENGTH)
        self._pending: dict[str, tuple[bool, datetime]] = {}
        self._last_cycle: datetime | None = None
        self._last_good_load: tuple[datetime, float] | None = None
        self._forecast: list[P.ForecastHour] = []
        self._forecast_at: datetime | None = None
        self._prices: list[P.PriceSlot] = []
        self._prices_at: datetime | None = None
        self._car_slot = None
        self._calib_try: datetime | None = None
        self._price_stats: dict = {}
        self._last_plugged: bool | None = None
        self._last_pause: str | None = None
        self._feed_votes = 0
        self._feed_reading: datetime | None = None   # battery power reading last looked at
        self._car_on_since: datetime | None = None
        self._dep_sig: object = _UNSET
        self._forced_since: dict[str, datetime] = {}   # device -> start of its forced run
        self._forced_plan: dict[str, dict] = {}        # device -> planned forced run (status)
        self.device_plan: P.Plan | None = None
        self.car_plan: P.Plan | None = None
        self.charge_windows: list[P.ChargeWindow] = []
        self._battery_drain_kw: float | None = None
        self.status: dict = {}

    def async_watch_car(self):
        """Plug-in and charge start run a cycle right away instead of at the
        next full minute: a car that starts charging by itself is stopped
        within seconds (the home battery pays for every minute)."""
        c = self.car_cfg or {}
        ents = [e for e in (c.get(CONF_CAR_PLUGGED_SENSOR), c.get(CONF_CAR_PLUGGED_FALLBACK),
                            c.get(CONF_CAR_CHARGING_SENSOR)) if e]
        if not ents:
            return None

        @callback
        def _changed(event: Event) -> None:
            old, new = event.data.get("old_state"), event.data.get("new_state")
            if old is not None and new is not None and old.state != new.state:
                self.hass.async_create_task(self.async_request_refresh())

        return async_track_state_change_event(self.hass, ents, _changed)

    async def async_setup(self) -> None:
        await self.store.async_load()
        self._restore_runtime_state()
        await self.calibrator.async_load()
        await self._backfill_runtime()
        await self._seed_price_archive()

    def _restore_runtime_state(self) -> None:
        """Log, recent readings and switch countdowns from before a restart.
        Readings older than the car's averaging window are dropped, so a long
        outage still starts fresh."""
        st = self.store.data
        self.log.extend(e for e in st["log"][:LOG_LENGTH] if isinstance(e, dict))
        now = dt_util.now()
        keep = timedelta(minutes=CAR_AVERAGE_MINUTES + 2)
        for row in st["samples"]:
            try:
                ts = dt_util.parse_datetime(row[0])
                vals = tuple(float(x) for x in row[1:4])
            except (TypeError, ValueError, IndexError):
                continue
            if ts is not None and timedelta(0) <= now - ts <= keep:
                self.samples.append((ts, *vals))
        ids = {d[CONF_DEV_ID] for d in self.devices}
        for did, (want, since) in st["pending"].items():
            ts = dt_util.parse_datetime(since) if isinstance(since, str) else None
            if did in ids and ts is not None and now - ts <= timedelta(hours=1):
                self._pending[did] = (bool(want), ts)
        for did, since in st.get("forced_since", {}).items():
            ts = dt_util.parse_datetime(since) if isinstance(since, str) else None
            if did in ids and ts is not None and ts.date() == now.date():
                self._forced_since[did] = ts

    def _persist_runtime_state(self) -> None:
        st = self.store.data
        st["samples"] = [[s[0].isoformat(), round(s[1], 3), round(s[2], 3), round(s[3], 3)] for s in self.samples]
        st["pending"] = {did: [want, ts.isoformat()] for did, (want, ts) in self._pending.items()}
        st["forced_since"] = {did: ts.isoformat() for did, ts in self._forced_since.items()}
        st["log"] = list(self.log)

    async def _seed_price_archive(self) -> None:
        """Fill the price archive from the recorder's hourly statistics of a
        current-price sensor, so the cheap threshold works from day one."""
        ent = self.cfg.get(CONF_PRICE_HISTORY_SENSOR)
        if not ent or "recorder" not in self.hass.config.components or len(self.store.data["price_archive"]) > 96:
            return
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import statistics_during_period
        except ImportError:
            return
        now = dt_util.now()
        start = now - timedelta(days=PRICE_ARCHIVE_DAYS)

        def _q():
            return statistics_during_period(self.hass, start, now, {ent}, "hour", None, {"mean"})

        try:
            rows = (await get_instance(self.hass).async_add_executor_job(_q)).get(ent, [])
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Price archive seeding failed", exc_info=True)
            return
        arch = self.store.data["price_archive"]
        for r in rows:
            st, mean = r.get("start"), r.get("mean")
            if st is None or mean is None:
                continue
            ts = dt_util.utc_from_timestamp(st) if isinstance(st, (int, float)) else st
            arch.setdefault(dt_util.as_local(ts).isoformat(), float(mean))
        self.store.save()

    def price_stats(self, now: datetime) -> dict:
        """Hour-of-day estimate, cheap threshold and the current price."""
        arch = self.store.data["price_archive"]
        vals, by_hour = [], {}
        for k, v in arch.items():
            t = dt_util.parse_datetime(k)
            if t is None:
                continue
            vals.append(v)
            by_hour.setdefault(dt_util.as_local(t).hour, []).append(v)
        profile = {h: statistics.median(v) for h, v in by_hour.items()}
        thr = None
        enough = len({k[:13] for k in arch}) >= PRICE_MIN_SAMPLES_H
        if enough and vals:
            vals.sort()
            pct = float(self.store.data["cheap_percentile"])
            idx = (len(vals) - 1) * pct / 100.0
            lo, hi = int(idx), min(int(idx) + 1, len(vals) - 1)
            thr = vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)
        now_price = next((p.price for p in self._prices if p.start <= now < p.end), None)
        return {"profile": profile, "threshold": thr, "now": now_price, "samples_h": len({k[:13] for k in arch})}

    async def _backfill_runtime(self) -> None:
        """Today's runtime per device from the recorder when the store has
        none (fresh install, or a store lost on restart) — otherwise a
        device that already ran for hours would look like it needs its
        whole minimum runtime forced again."""
        if "recorder" not in self.hass.config.components:
            return
        self.store.roll_day()
        missing = list(self.devices)  # max(stored, recorded): a lost or late-started counter is repaired
        if not missing:
            return
        try:
            from homeassistant.components.recorder import get_instance, history
        except ImportError:
            return
        now = dt_util.now()
        start = dt_util.start_of_local_day()

        def _query():
            ids = [d[CONF_DEV_ENTITY] for d in missing]
            return {i: history.state_changes_during_period(self.hass, start, now, entity_id=i).get(i, [])
                    for i in ids}

        try:
            result = await get_instance(self.hass).async_add_executor_job(_query)
        except Exception:  # noqa: BLE001 - a missing backfill only means counting from zero
            _LOGGER.debug("Runtime backfill failed", exc_info=True)
            return
        for d in missing:
            states = result.get(d[CONF_DEV_ENTITY], [])
            first = self.hass.states.get(d[CONF_DEV_ENTITY])
            secs = 0.0
            for i, st in enumerate(states):
                end = states[i + 1].last_changed if i + 1 < len(states) else now
                on = st.state not in ("off", "unavailable", "unknown") if d.get(CONF_DEV_KIND) == KIND_CLIMATE \
                    else st.state == "on"
                if on:
                    secs += (end - max(st.last_changed, start)).total_seconds()
            if not states and first is not None and device_is_on(self.hass, d):
                secs = (now - max(first.last_changed, start)).total_seconds()
            sec = self.store.data["runtime"]["seconds"]
            sec[d[CONF_DEV_ID]] = max(sec.get(d[CONF_DEV_ID], 0.0), secs)
            _LOGGER.debug("Backfilled %s runtime today: %.2f h", d.get(CONF_DEV_NAME), secs / 3600)
        self.store.save()

    def _log(self, text: str) -> None:
        self.log.appendleft({"zeit": dt_util.now().strftime("%d.%m. %H:%M"), "text": text})
        self.store.data["log"] = list(self.log)
        self.store.save()

    @property
    def mode(self) -> str:
        return self.store.data["mode"]

    @property
    def active(self) -> bool:
        return self.mode == MODE_AUTO

    # ------------------------------------------------------------ inputs

    def _sun(self, now: datetime) -> dict | None:
        sun = self.hass.states.get("sun.sun")
        if sun is None:
            return None
        rise = dt_util.parse_datetime(str(sun.attributes.get("next_rising")))
        sset = dt_util.parse_datetime(str(sun.attributes.get("next_setting")))
        if rise is None or sset is None:
            return None
        rise, sset = dt_util.as_local(rise), dt_util.as_local(sset)
        rise_today = rise if rise.date() == now.date() else rise - timedelta(days=1)
        offsets = self.calibrator.offsets_for(DEFAULT_SOLAR_OFFSETS)
        off_today = offsets[now.month - 1]
        ss_today = rise_today + timedelta(hours=off_today)
        ss_next = ss_today if now < ss_today else rise + timedelta(hours=offsets[rise.month - 1])
        if ss_next <= now:
            ss_next += timedelta(days=1)
        pv_end = sset - timedelta(hours=float(self.cfg.get(CONF_PV_END_BEFORE_SUNSET_H,
                                                            DEFAULT_PV_END_BEFORE_SUNSET_H)))
        return {"solar_start_today": ss_today, "solar_start": ss_next, "sunset": sset, "pv_end": pv_end,
                "offset_h": off_today}

    def _read_load(self, now: datetime) -> tuple[float | None, bool]:
        """House load; a sensor that stopped reporting is held for one more
        stale period, then the cycle is skipped (never decide on frozen data)."""
        ent = self.cfg[CONF_LOAD_SENSOR]
        stale = float(self.cfg.get(CONF_STALE_MINUTES, DEFAULT_STALE_MINUTES))
        v = power_kw(self.hass, ent)
        age = age_minutes(self.hass, ent)
        if v is not None and (age is None or age <= stale):
            self._last_good_load = (now, v)
            return v, False
        if self._last_good_load and (now - self._last_good_load[0]).total_seconds() / 60 <= stale:
            return self._last_good_load[1], True
        return None, True

    async def _refresh_forecast(self, now: datetime) -> None:
        if self._forecast_at and (now - self._forecast_at).total_seconds() < 1800:
            return
        self._forecast_at = now
        rows = await async_solar_forecast(self.hass)
        if rows:
            self._forecast = rows

    async def _refresh_prices(self, now: datetime) -> None:
        src = self.cfg.get(CONF_PRICE_SOURCE)
        if src == PRICE_SENSOR:
            self._prices = sensor_prices(self.hass, self.cfg.get(CONF_PRICE_SENSOR)) or []
            self.store.archive_prices(self._prices, now, PRICE_ARCHIVE_DAYS)
            return
        if src != PRICE_TIBBER:
            self._prices = []
            return
        if self._prices_at and (now - self._prices_at).total_seconds() < 1800 and self._prices:
            return
        self._prices_at = now
        rows = await async_tibber_prices(self.hass, self.cfg.get(CONF_TIBBER_HOME),
                                         now - timedelta(hours=1), now + timedelta(hours=40))
        if rows:
            self._prices = rows
            self.store.archive_prices(rows, now, PRICE_ARCHIVE_DAYS)
        elif not self._prices:
            self._prices_at = now - timedelta(seconds=1700)  # retry in ~2 min

    def _device_window(self, dev: dict, now: datetime) -> tuple[bool, datetime | None]:
        sched = dev.get(CONF_DEV_SCHEDULE)
        if sched:
            st = self.hass.states.get(sched)
            if st is None:
                return True, None
            on = st.state == "on"
            nxt = dt_util.parse_datetime(str(st.attributes.get("next_event"))) if st.attributes.get(
                "next_event") else None
            return on, (dt_util.as_local(nxt) if (on and nxt) else None)
        ws, we = dev.get(CONF_DEV_WINDOW_START), dev.get(CONF_DEV_WINDOW_END)
        if not ws or not we:
            return True, None
        h1, m1 = parse_hhmm(ws)
        h2, m2 = parse_hhmm(we)
        t = now.time()
        a, b = dtime(h1, m1), dtime(h2, m2)
        inside = (a <= t < b) if a < b else (t >= a or t < b)
        end = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return inside, end if inside else None

    def _forced(self, dev: dict, now: datetime, in_window: bool, window_end: datetime | None, base_kw: float,
                soc: float | None, pv_end: datetime, is_on: bool) -> bool:
        """Minimum daily runtime: one contiguous forced run in the cheapest
        part of today's window (planner.min_runtime_start). A started run is
        finished in one go, at least DEVICE_MIN_RUN_H long; a running device
        close to its minimum just keeps going (planner.min_runtime_finish)."""
        did = dev[CONF_DEV_ID]
        need_h = float(dev.get(CONF_DEV_MIN_RUNTIME_H) or 0) - self.store.runtime_h(did)
        since = self._forced_since.get(did)
        if not in_window or need_h <= 0 and (since is None or now - since >= timedelta(hours=P.DEVICE_MIN_RUN_H)):
            self._forced_since.pop(did, None)
            self._forced_plan.pop(did, None)
            return False
        if since is not None:
            return True
        if P.min_runtime_finish(is_on, need_h):
            self._forced_plan[did] = {"start": now, "need_h": need_h, "surplus_h": None}
            return True
        end = window_end or now.replace(hour=23, minute=59)
        if end.date() != now.date():
            end = now.replace(hour=23, minute=59)
        other_kw = sum(self._decision_kw(x) for x in self.devices
                       if int(x.get(CONF_DEV_PRIORITY, 50)) < int(dev.get(CONF_DEV_PRIORITY, 50))
                       and self.store.data["device_enabled"].get(x[CONF_DEV_ID], True))
        surplus_h = P.device_surplus_hours(
            self._forecast, now, end, base_kw, self._decision_kw(dev), soc,
            float(self.cfg.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)), pv_end, other_kw,
            self.car_plan.car_block if self.car_plan else None, self.car.max_kw if self.car else 0.0)
        start = P.min_runtime_start(now, need_h, end, self._prices, surplus_h)
        self._forced_plan[did] = {"start": start, "need_h": need_h, "surplus_h": surplus_h}
        if start is not None and start <= now:
            self._forced_since[did] = now
            return True
        return False

    # ------------------------------------------------------------ cycle

    def battery_feeds_car(self, now: datetime) -> bool:
        """Does the home battery discharge into the car? The last detected
        state (battery power sensor) while it is fresh, else the setting.
        Without the sensor (detection off) only the setting counts."""
        learned = self.store.data.get("battery_feeds")
        if learned and self.cfg.get(CONF_BATTERY_POWER_SENSOR):
            at = dt_util.parse_datetime(learned.get("at") or "")
            if at is not None and now - at <= timedelta(days=FEED_EXPIRY_DAYS):
                return bool(learned.get("value"))
        return bool(self.car_cfg.get(CONF_CAR_BATTERY_FEEDS, True)) if self.car_cfg else True

    def _observe_battery_feed(self, now: datetime, pv: float, load: float, car_kw: float,
                              soc: float | None) -> None:
        """Re-read the battery's behaviour while the car charges: the
        inverter may change it on its own (e.g. a price-driven winter mode).
        Cloud inverter data arrives only every ~5 min (FusionSolar), so
        only a NEW battery reading counts, and only when the car was already
        charging well before it. A different state needs
        FEED_CONFIRM_READINGS consistent readings; then the car is
        re-planned at once."""
        if car_kw >= P.FEED_CAR_MIN_KW:
            self._car_on_since = self._car_on_since or now
        else:
            self._car_on_since = None
        ent = self.cfg.get(CONF_BATTERY_POWER_SENSOR)
        st = self.hass.states.get(ent) if ent else None
        if st is None or not self.car_cfg:
            return
        stamp = getattr(st, "last_reported", None) or st.last_updated
        if stamp == self._feed_reading:
            return
        self._feed_reading = stamp
        if self._car_on_since is None or stamp - self._car_on_since < timedelta(minutes=FEED_CAR_STEADY_MIN):
            return
        ev = P.battery_feed_evidence(car_kw, pv, load, power_kw(self.hass, ent), soc,
                                     float(self.cfg.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)))
        if ev is None:
            return
        current = self.battery_feeds_car(now)
        if ev == current:
            self._feed_votes = 0
            self.store.data["battery_feeds"] = {"value": ev, "at": now.isoformat()}
            return
        self._feed_votes += 1
        if self._feed_votes < FEED_CONFIRM_READINGS:
            return
        self._feed_votes = 0
        self.store.data["battery_feeds"] = {"value": ev, "at": now.isoformat()}
        self._log("Hausakku entlädt jetzt ins Auto — erkannt" if ev
                  else "Hausakku entlädt nicht mehr ins Auto — erkannt")
        self._car_slot = None  # re-plan the car now

    async def _async_update_data(self) -> dict:
        now = dt_util.now()
        self.store.roll_day()
        cycle_s = (now - self._last_cycle).total_seconds() if self._last_cycle else 0.0
        self._last_cycle = now
        if ("recorder" in self.hass.config.components
                and self.calibrator.due_for_recalibration(CALIBRATION_INTERVAL)
                and (self._calib_try is None or (now - self._calib_try).total_seconds() > 3600)):
            self._calib_try = now
            self.hass.async_create_task(self.calibrator.async_recalibrate())

        pv = power_kw(self.hass, self.cfg[CONF_PV_SENSOR])
        load, load_stale = self._read_load(now)
        sun = self._sun(now)
        if pv is None or load is None or sun is None:
            self.status = {**self.status, "daten_ok": False, "zeit": now.isoformat()}
            return self.status
        pv = max(0.0, pv)
        soc = number(self.hass, self.cfg.get(CONF_BATTERY_SOC_SENSOR))
        export = power_kw(self.hass, self.cfg.get(CONF_GRID_EXPORT_SENSOR))
        if export is not None:
            export = abs(export)

        # ---- car readings
        car_in = None
        car_kw = 0.0
        charging = False
        plugged = None
        if self.car_cfg:
            c = self.car_cfg
            meas = power_kw(self.hass, c.get(CONF_CAR_POWER_SENSOR))
            ch = is_on(self.hass, c.get(CONF_CAR_CHARGING_SENSOR))
            charging = bool(ch) if ch is not None else ((meas or 0.0) > 0.3 or self.car.switch_on())
            car_kw = meas if meas is not None else 0.0
            # no power reading (none configured, or e.g. an MQTT sensor still
            # empty after a restart): estimate it from the set current, so the
            # car's draw doesn't end up in the base load
            if meas is None and charging and c.get(CONF_CAR_CURRENT_ENTITY):
                car_kw = (number(self.hass, c[CONF_CAR_CURRENT_ENTITY]) or 0) * self.car.kw_per_a
            plugged = plugged_state(self.hass, c.get(CONF_CAR_PLUGGED_SENSOR))
            if plugged is None:
                plugged = plugged_state(self.hass, c.get(CONF_CAR_PLUGGED_FALLBACK))
            home = tracker_home(self.hass, c.get(CONF_CAR_TRACKER))
            if home is None:
                home = tracker_home(self.hass, c.get(CONF_CAR_TRACKER_FALLBACK))
            has_plug_sensor = bool(c.get(CONF_CAR_PLUGGED_SENSOR) or c.get(CONF_CAR_PLUGGED_FALLBACK))
            # unknown plug state with a plug sensor configured = not present:
            # never send (possibly billed) commands to a car that may be away
            plug_ok = plugged is True or (plugged is None and (not has_plug_sensor or charging))
            present = plug_ok and (home is not False)
            # a second source covers the primary one being empty (e.g. a fast
            # MQTT sensor that only refills when the car reports again)
            car_soc = number(self.hass, c.get(CONF_CAR_SOC_SENSOR))
            if car_soc is None:
                car_soc = number(self.hass, c.get(CONF_CAR_SOC_FALLBACK))
            limit = number(self.hass, c.get(CONF_CAR_LIMIT_ENTITY)) or float(
                c.get(CONF_CAR_LIMIT_DEFAULT, DEFAULT_CAR_LIMIT))
            car_in = P.CarInput(present=present, soc=car_soc, limit_soc=limit,
                                capacity_kwh=float(c.get(CONF_CAR_CAPACITY_KWH, DEFAULT_CAR_CAPACITY_KWH)),
                                efficiency=float(c.get(CONF_CAR_EFFICIENCY, DEFAULT_CAR_EFFICIENCY)),
                                min_kw=self.car.min_kw, max_kw=self.car.max_kw, current_kw=car_kw)

        # ---- devices: actual state + power
        dev_on: dict[str, bool] = {}
        dev_kw: dict[str, float] = {}
        for d in self.devices:
            did = d[CONF_DEV_ID]
            on = device_is_on(self.hass, d)
            dev_on[did] = on
            meas = power_kw(self.hass, d.get(CONF_DEV_POWER_SENSOR))
            if on and meas is not None and meas > 0.02:
                self.store.learn_power(did, meas)
            if on:
                dev_kw[did] = meas if meas is not None else self._decision_kw(d)
            if on and cycle_s:
                self.store.add_runtime(did, min(cycle_s, 3 * UPDATE_INTERVAL_SECONDS))
        base = max(0.1, load - car_kw - sum(dev_kw.values()))

        self.samples.append((now, pv, base, car_kw))
        while self.samples and (now - self.samples[0][0]).total_seconds() > (CAR_AVERAGE_MINUTES + 2) * 60:
            self.samples.popleft()
        night = not (sun["solar_start_today"] <= now < sun["sunset"])
        if night and not load_stale:
            self.store.learn_night_base(base)
        self.store.save()

        await self._refresh_forecast(now)
        await self._refresh_prices(now)
        deps = all_departures(self.store.data["slots"], self.store.data["oneoff"], now)

        base_now = statistics.median(x[2] for x in self.samples)
        dev_inputs = []
        for d in self.devices:
            did = d[CONF_DEV_ID]
            in_win, w_end = self._device_window(d, now)
            dev_inputs.append(P.DeviceInput(
                id=did, name=d.get(CONF_DEV_NAME, did), priority=int(d.get(CONF_DEV_PRIORITY, 50)),
                decision_kw=self._decision_kw(d), is_on=dev_on[did],
                enabled=self.store.data["device_enabled"].get(did, True), in_window=in_win, window_end=w_end,
                depends_on=d.get(CONF_DEV_DEPENDS_ON) or None,
                forced=self._forced(d, now, in_win, w_end, base_now, soc, sun["pv_end"], dev_on[did]),
                soc_reserve=float(d.get(CONF_DEV_SOC_RESERVE) or 0.0)))

        self._observe_battery_feed(now, pv, load, car_kw, soc)
        feeds_car = self.battery_feeds_car(now)

        ps = self.price_stats(now)
        self._price_stats = ps
        cheap_target = float(self.store.data["cheap_target"]) if self.store.data["cheap_enabled"] else None
        cheap_pv_soc = float(self.store.data["cheap_pv_battery_soc"] or 0) or None

        def inputs(pv_kw: float, base_kw: float, car_fixed: float | None) -> P.Inputs:
            return P.Inputs(
                now=now, pv_kw=pv_kw, base_kw=base_kw, battery_soc=soc,
                battery_capacity_kwh=float(self.cfg.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH)),
                battery_min_soc=float(self.cfg.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC)),
                export_kw=export, solar_start=sun["solar_start"], solar_start_today=sun["solar_start_today"],
                sunset=sun["sunset"], pv_end=sun["pv_end"], night_base_kw=self.store.data["night_base_kw"],
                forecast=self._forecast, prices=self._prices, departures=deps, car=car_in, devices=dev_inputs,
                allow_grid_for_car=bool(self.car_cfg.get(CONF_CAR_ALLOW_GRID, True)) if self.car_cfg else False,
                battery_feeds_car=feeds_car,
                car_block=self.car_plan.car_block if car_fixed is not None and self.car_plan else None,
                grid_running=bool(self.car and ((self.car.decision is not None and self.car.decision.grid)
                                                 or car_kw >= 0.9 * self.car.max_kw)),
                car_fixed_kw=car_fixed, price_profile=ps["profile"], price_now=ps["now"],
                cheap_threshold=ps["threshold"], cheap_target_soc=cheap_target,
                cheap_pv_battery_soc=cheap_pv_soc,
                cheap_pv_locked=self.store.data.get("cheap_pv_lock") == now.date().isoformat())

        # ---- car decision (quarter hours / plug-in / pause change)
        paused = False
        if self.car_cfg and self.car:
            pe = self.car_cfg.get(CONF_CAR_PAUSE_ENTITY)
            pst = self.hass.states.get(pe) if pe else None
            pstate = pst.state if pst is not None else None
            paused = pstate is not None and pstate == self.car_cfg.get(CONF_CAR_PAUSE_STATE)
            just_plugged = plugged is True and self._last_plugged is False
            if plugged is not None:
                self._last_plugged = plugged
            pause_changed = pstate is not None and self._last_pause is not None and pstate != self._last_pause
            if pstate is not None:
                self._last_pause = pstate
            forced = just_plugged or pause_changed
            # a departure set or changed on the dashboard is planned at once,
            # not at the next quarter hour (it may be only minutes away)
            nd = P.next_departure(deps, now)
            dep_sig = (nd.start, nd.target_soc) if nd else None
            replan = self._dep_sig is not _UNSET and dep_sig != self._dep_sig
            slot = (now.date(), now.hour, now.minute // CAR_DECISION_MINUTES)
            if forced or replan or slot != self._car_slot:
                win = [s for s in self.samples if (now - s[0]).total_seconds() <= CAR_AVERAGE_MINUTES * 60]
                covered = (win[-1][0] - win[0][0]).total_seconds() / 60 if len(win) > 1 else 0
                # a departure within 2 h doesn't wait for 10 min of readings
                urgent = nd is not None and nd.start - now <= timedelta(hours=URGENT_DEPARTURE_H)
                if covered >= CAR_MIN_COVERAGE_MINUTES or forced or ((replan or urgent) and win):
                    pv_m = sum(s[1] for s in win) / len(win)
                    base_m = statistics.median(s[2] for s in win)
                    probe = inputs(pv_m, base_m, None)
                    if (self.car.decision is not None and self.car.decision.reason == "netz_billig"
                            and not probe.cheap_pv_locked and P.cheap_pv_battery_stop(probe)):
                        # stopped for the battery: no restart in PV today (PV refills it -> stop-and-go)
                        self.store.data["cheap_pv_lock"] = now.date().isoformat()
                        self.store.save()
                        self._log(f"Billig-Laden mit PV beendet: Hausakku {soc:.0f} % — heute mit PV nicht mehr")
                        probe = inputs(pv_m, base_m, None)
                    self._cheap_pv_waits = (cheap_target is not None and ps["now"] is not None
                                            and ps["threshold"] is not None and ps["now"] <= ps["threshold"]
                                            and probe.pv_kw > probe.base_kw + 0.3
                                            and not P.cheap_pv_battery_ok(probe))
                    plan = P.make_plan(probe)
                    # the top-up must also fit the last 15 min (falling PV)
                    win_cap = [s for s in win if (now - s[0]).total_seconds() <= CAR_CAP_MINUTES * 60]
                    if win_cap:
                        recent = P.make_plan(inputs(statistics.median(s[1] for s in win_cap),
                                                    statistics.median(s[2] for s in win_cap), None))
                        plan = P.cap_car_topup(plan, recent.car_kw, self.car.min_kw)
                    self.car_plan = plan
                    self.charge_windows = P.charge_windows(probe)
                    self.car.decide(now, self.car_plan, charging, forced)
                    self._car_slot = slot
                    self._dep_sig = dep_sig
            car_active = self.active and self.store.data["car_control"] and not paused and (
                car_in is not None and car_in.present and (car_in.soc is None or car_in.soc < car_in.limit_soc
                                                           or charging))
            soc_known = car_in is None or car_in.soc is not None or not self.car_cfg.get(CONF_CAR_SOC_SENSOR)
            if car_active:
                await self.car.async_apply(now, True, charging, soc_known, self.store.commands_today)
                await self.car.async_guard_autostart(now, True, charging)
            elif self.car.decision is not None and not self.car.applied:
                self.car.applied = True
                self.car.action = ("pausiert" if paused else "nur_beobachten" if not self.active
                                   else "steuerung_aus" if not self.store.data["car_control"]
                                   else "nicht_da" if car_in is None or not car_in.present else "ladelimit_erreicht")

        # ---- devices (every cycle, 5-min median, car draw fixed)
        win5 = [s for s in self.samples if (now - s[0]).total_seconds() <= DEVICE_SMOOTH_MINUTES * 60]
        pv5 = statistics.median(s[1] for s in win5)
        base5 = statistics.median(s[2] for s in win5)
        car5 = statistics.median(s[3] for s in win5)
        car_fixed = car5
        cp = self.car_plan
        if cp is not None and not cp.car_first and not cp.car_grid and cp.car_reason != "pflicht_minimum":
            # the top-up ranks below the devices: they may claim it, the car
            # follows at its next decision (only the obligation stays fixed)
            car_fixed = min(car5, cp.car_must_kw)
        self.device_plan = P.make_plan(inputs(pv5, base5, car_fixed))
        # what the home battery has to deliver right now (5-min median): house
        # and running devices beyond the PV, the car only when it feeds it
        self._battery_drain_kw = base5 + sum(dev_kw.values()) + (car5 if feeds_car else 0.0) - pv5
        await self._apply_devices(now, dev_on)
        self._persist_runtime_state()
        self.store.save()

        self.status = self._build_status(now, pv, load, base, soc, export, car_in, car_kw, charging, sun,
                                         load_stale, paused)
        return self.status

    def _decision_kw(self, d: dict) -> float:
        learned = self.store.data["learned_kw"].get(d[CONF_DEV_ID])
        if d.get(CONF_DEV_POWER_SENSOR) and learned:
            return round(learned, 3)
        return float(d.get(CONF_DEV_POWER_KW) or 0.5)

    async def _apply_devices(self, now: datetime, dev_on: dict[str, bool]) -> None:
        plan = self.device_plan
        for d in self.devices:
            did = d[CONF_DEV_ID]
            dec = plan.devices.get(did)
            if dec is None or not self.store.data["device_enabled"].get(did, True):
                self._pending.pop(did, None)
                continue
            want, actual = dec.on, dev_on[did]
            if want == actual:
                self._pending.pop(did, None)
                continue
            # off at once only when it can't run any more: outside its window,
            # or the device it depends on is really off (not merely planned
            # off and still counting down - then it follows with its own delay)
            dep_id = d.get(CONF_DEV_DEPENDS_ON)
            immediate = not want and (dec.reason == "ausserhalb_zeitfenster" or (
                dec.reason == "wartet_auf_abhaengigkeit" and not (dep_id and dev_on.get(dep_id))))
            if want:
                delay = DEVICE_FORCED_ON_DELAY_S if dec.path == "pflicht" else DEVICE_ON_DELAY_S
            else:
                delay = DEVICE_OFF_DELAY_S
            if d.get(CONF_DEV_KIND) == KIND_CLIMATE and dec.path != "pflicht":
                delay *= CLIMATE_DELAY_FACTOR
            pend = self._pending.get(did)
            if pend is None or pend[0] != want:
                self._pending[did] = (want, now)
                pend = self._pending[did]
            if immediate or (now - pend[1]).total_seconds() >= delay:
                if self.active:
                    await device_switch(self.hass, d, want)
                    dev_on[did] = want   # a dependent device further down sees it this cycle
                    self._log(f"{d.get(CONF_DEV_NAME)} {'an' if want else 'aus'} "
                              f"({REASON_TEXT.get(dec.reason, dec.reason)})")
                self._pending.pop(did, None)

    # ------------------------------------------------------------ status for entities

    def countdown_s(self, did: str) -> int | None:
        pend = self._pending.get(did)
        if pend is None:
            return None
        d = next((x for x in self.devices if x[CONF_DEV_ID] == did), None)
        dec = self.device_plan.devices.get(did) if self.device_plan else None
        if d is None or dec is None:
            return None
        delay = (DEVICE_FORCED_ON_DELAY_S if dec.path == "pflicht" else DEVICE_ON_DELAY_S) if pend[0] \
            else DEVICE_OFF_DELAY_S
        if d.get(CONF_DEV_KIND) == KIND_CLIMATE and dec.path != "pflicht":
            delay *= CLIMATE_DELAY_FACTOR
        return max(0, int(delay - (dt_util.now() - pend[1]).total_seconds()))

    def forced_run_start(self, did: str) -> str | None:
        """Start of the (planned or running) forced min-runtime run."""
        if did in self._forced_since:
            return self._forced_since[did].isoformat()
        start = (self._forced_plan.get(did) or {}).get("start")
        return start.isoformat() if start is not None else None

    def _build_status(self, now, pv, load, base, soc, export, car_in, car_kw, charging, sun, load_stale,
                      paused) -> dict:
        dp = self.device_plan
        cp = self.car_plan
        fc_today = P.forecast_kwh(self._forecast, now, now.replace(hour=23, minute=59)) if self._forecast else None
        tmr = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        fc_tomorrow = P.forecast_kwh(self._forecast, tmr, tmr + timedelta(days=1)) if self._forecast else None
        dep = dp.next_departure if dp else None
        return {
            "daten_ok": True, "zeit": now.isoformat(), "last_veraltet": load_stale,
            "pv_kw": round(pv, 2), "verbrauch_kw": round(load, 2), "grundlast_kw": round(base, 2),
            "ueberschuss_kw": round(pv - base, 2), "akku_soc": soc, "einspeisung_kw": export,
            "akku_speist_auto": self.battery_feeds_car(now),
            "akku_speist_auto_erkannt": (self.store.data.get("battery_feeds") or {}).get("at"),
            "akku_ziel_kw": round(dp.battery_target_kw, 2) if dp else None,
            "akku_ziel_modus": dp.battery_mode if dp else None,
            "geraete_budget_kw": round(dp.budget.get("frei_kw", 0.0) + dp.budget.get("geraete_kw", 0.0), 2)
            if dp else None,
            "auto_kw": round(car_kw, 2), "auto_laedt": charging, "auto_pausiert": paused,
            "auto_da": car_in.present if car_in else None, "auto_soc": car_in.soc if car_in else None,
            "auto_limit": car_in.limit_soc if car_in else None,
            "auto_plan": cp, "naechste_abfahrt": dep,
            "solar_start": sun["solar_start"].isoformat(), "pv_ende": sun["pv_end"].isoformat(),
            "solar_offset_h": round(sun["offset_h"], 2),
            "prognose_rest_heute_kwh": round(fc_today, 1) if fc_today is not None else None,
            "prognose_morgen_kwh": round(fc_tomorrow, 1) if fc_tomorrow is not None else None,
            "preise_bekannt": len(self._prices),
            "preis_jetzt": self._price_stats.get("now"),
            "billig_schwelle": self._price_stats.get("threshold"),
            "preis_historie_h": self._price_stats.get("samples_h"),
            "nacht_grundlast_kw": round(self.store.data["night_base_kw"], 2),
        }

    # ------------------------------------------------------------ plain-language explanation

    def _de(self) -> bool:
        return (self.hass.config.language or "en").lower().startswith("de")

    @staticmethod
    def _kw(v: float | None) -> str:
        return "-" if v is None else f"{v:.1f}".replace(".", ",") + " kW"

    def summary(self) -> str:
        d = self.status or {}
        if not d.get("daten_ok", False):
            return "Warte auf Sensordaten" if self._de() else "Waiting for sensor data"
        de = self._de()
        parts = [f"PV {self._kw(d.get('pv_kw'))}"]
        if self.car_cfg and d.get("auto_da"):
            dec = self.car.decision if self.car else None
            parts.append((f"Auto {self._kw(d.get('auto_kw'))}" if de else f"Car {self._kw(d.get('auto_kw'))}")
                         + (f" → {dec.kw:.1f} kW".replace(".", ",") if dec and abs(dec.kw - d.get('auto_kw', 0)) > 0.3
                            else ""))
        if d.get("akku_ziel_kw"):
            parts.append((f"Akku {self._kw(d.get('akku_ziel_kw'))}" if de else f"Battery {self._kw(d.get('akku_ziel_kw'))}"))
        on = [x.get(CONF_DEV_NAME) for x in self.devices
              if self.device_plan and self.device_plan.devices.get(x[CONF_DEV_ID])
              and self.device_plan.devices[x[CONF_DEV_ID]].on]
        parts.append(("Geräte: " if de else "Devices: ") + (", ".join(on) if on else ("keine" if de else "none")))
        if self.mode != MODE_AUTO:
            parts.insert(0, ("NUR BEOBACHTEN" if de else "OBSERVE ONLY") if self.mode == "observe"
                         else ("AUS" if de else "OFF"))
        return " · ".join(parts)

    def explain(self) -> list[str]:
        d = self.status or {}
        dp, cp = self.device_plan, self.car_plan
        if not d.get("daten_ok") or dp is None:
            return []
        lines = [f"PV {self._kw(d['pv_kw'])} − Grundlast {self._kw(d['grundlast_kw'])} = "
                 f"Überschuss {self._kw(d['ueberschuss_kw'])}"]
        if self.car_cfg:
            if not d.get("auto_da"):
                lines.append("Auto: nicht da / nicht angesteckt")
            elif cp is not None:
                dep = cp.next_departure
                txt = f"Auto: Soll {self._kw(self.car.decision.kw if self.car.decision else 0)}"
                if dep is not None and cp.need_kwh > 0:
                    txt += (f" — bis {dep.name or 'Abfahrt'} {dep.start.strftime('%a %H:%M')} fehlen "
                            f"{cp.need_kwh:.1f} kWh bis {dep.target_soc:.0f} %").replace(".", ",")
                    if cp.uncovered_kwh > 0:
                        txt += f", davon {cp.uncovered_kwh:.1f} kWh nicht aus PV zu erwarten".replace(".", ",")
                if cp.car_first:
                    txt += " — lädt VOR den Geräten (morgen keine PV-Chance)"
                elif cp.car_reason in ("rest", "pflicht_und_rest"):
                    txt += " — bekommt den Rest nach den Geräten"
                if "auto_begrenzt_auf_kw" in cp.budget:
                    txt += (f" — begrenzt auf den Überschuss der letzten {CAR_CAP_MINUTES} min "
                            f"({self._kw(cp.budget['auto_begrenzt_auf_kw'])}), damit der Hausakku nicht zuschießt")
                if cp.car_grid and cp.car_reason == "netz_billig":
                    txt += (f" — lädt aus dem Netz: Preis unter der Billig-Schwelle, günstigste Zeit bis zur "
                            f"Abfahrt, und die PV-Prognose bringt bis dahin nicht genug "
                            f"(bis {self.store.data['cheap_target']:.0f} %)")
                elif cp.car_grid:
                    txt += " — lädt aus dem Netz: günstigster Zeitraum bis zur Abfahrt"
                if not cp.car_grid and getattr(self, "_cheap_pv_waits", False):
                    txt += (f" — Strom billig, aber mit PV erst ab Hausakku "
                            f"{float(self.store.data['cheap_pv_battery_soc']):.0f} % "
                            f"oder wenn die Prognose ihn danach trotzdem füllt"
                            + (" (heute schon einmal abgebrochen)"
                               if self.store.data.get("cheap_pv_lock") == dt_util.now().date().isoformat() else ""))
                if cp.car_block and not cp.car_grid and cp.car_block[1] > dt_util.now():
                    a, b = (dt_util.as_local(x).strftime("%H:%M") for x in cp.car_block)
                    txt += f" — Netz-Block geplant {a}–{b} (Geräte richten sich danach)"
                lines.append(txt)
        if d.get("akku_soc") is not None:
            lines.append(self._battery_line(d))
        names = {x[CONF_DEV_ID]: x.get(CONF_DEV_NAME) for x in self.devices}
        for did, dec in dp.devices.items():
            cd = self.countdown_s(did)
            extra = f" (schaltet in {cd // 60} min)" if cd else ""
            fp = self._forced_plan.get(did) or {}
            if not dec.on and fp.get("start") is not None and fp.get("need_h", 0) > 0:
                extra += (f" — Mindestlaufzeit: fehlen {fp['need_h']:.1f} h, Pflichtlauf ab "
                          f"{dt_util.as_local(fp['start']).strftime('%H:%M')}").replace(".", ",")
            if dec.battery_empty is not None:
                need = dt_util.as_local(dt_util.parse_datetime(d["solar_start"])) + timedelta(
                    hours=P.BATTERY_PATH_BUFFER_H)
                extra += (f" (damit leer ~{dt_util.as_local(dec.battery_empty).strftime('%H:%M')}, "
                          f"nötig bis {need.strftime('%H:%M')})")
            lines.append(f"{names.get(did, did)}: {'an' if dec.on else 'aus'} — "
                         f"{REASON_TEXT.get(dec.reason, dec.reason)}{extra}")
        return lines

    def _battery_line(self, d: dict) -> str:
        """Home battery: how long it lasts at the current draw down to the
        reserve, and at night whether that is enough until the PV start."""
        now = dt_util.now()
        soc = d["akku_soc"]
        reserve = float(self.cfg.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC))
        cap = float(self.cfg.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH))
        start = dt_util.as_local(dt_util.parse_datetime(d["solar_start"]))
        end = dt_util.as_local(dt_util.parse_datetime(d["pv_ende"]))

        def num(v: float) -> str:
            return f"{v:.1f}".replace(".", ",")

        # solar start / PV end are in the dashboard footer: only the verdict here
        txt = f"Hausakku {soc:.0f} %"
        drain = self._battery_drain_kw
        if drain is None or drain < 0.05:
            return txt + (" — wird geladen" if drain is not None and drain < -0.05 else "")
        last_h = max(0.0, (soc - reserve) / 100.0 * cap) / drain
        empty = now + timedelta(hours=last_h)
        txt += (f": reicht noch {num(last_h)} h bis {reserve:.0f} % (~{empty.strftime('%H:%M')}, "
                f"bei {self._kw(drain)})")
        if now < end < start:   # PV day: the next solar start is tomorrow
            return txt
        return txt + (" ✅" if empty >= start else " ⚠️ reicht nicht bis PV-Start")
