"""Car charging actuation (vendor-neutral).

Works with any pair of (switch for start/stop, number for the charging
current) — on the car (e.g. a vehicle API integration) or on the wallbox.
Lessons carried over from Smart Car Charger:
  * start FIRST, set the current afterwards: a sleeping car's current
    entity can report a stale `max` until it is awake;
  * a stale `max` is refreshed once, otherwise the current is clamped and
    retried shortly after;
  * failed commands back off 1/2/5/10 min;
  * an optional daily command budget for paid vehicle APIs;
  * a charge the car starts by itself right after plug-in is stopped once
    when the decision is 0 A (unregulated wallboxes do that);
  * while charging, the reported current (and the measured power) is checked
    against the target and the current re-sent: a car can drop a current set
    right with its start (19.11.2026 live: 6 A set, 16 A / 11 kW drawn).
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant

from .const import (
    CAR_AMPS_MAX_FIXES,
    CAR_AMPS_SETTLE_S,
    CAR_AUTOSTART_GUARD_S,
    CAR_RETRY_DELAYS_MIN,
    CAR_STOP_AFTER_LOW_WINDOWS,
    CONF_CAR_CHARGE_SWITCH,
    CONF_CAR_CURRENT_ENTITY,
    CONF_CAR_MAX_A,
    CONF_CAR_MAX_COMMANDS,
    CONF_CAR_MIN_A,
    CONF_CAR_PHASES,
    CONF_CAR_VOLTAGE,
    DEFAULT_CAR_MAX_A,
    DEFAULT_CAR_MIN_A,
    DEFAULT_CAR_PHASES,
    DEFAULT_CAR_VOLTAGE,
)
from .sources import number


def _txt(reason: str) -> str:
    from .coordinator import REASON_TEXT
    return REASON_TEXT.get(reason, reason)

_LOGGER = logging.getLogger(__name__)


@dataclass
class CarDecision:
    time: datetime
    amps: int
    kw: float
    reason: str
    grid: bool
    forced: bool
    plan: dict = field(default_factory=dict)


class CarController:
    def __init__(self, hass: HomeAssistant, cfg: dict, count_command, log) -> None:
        self.hass = hass
        self.cfg = cfg
        self._count_command = count_command
        self._log = log
        self.min_a = int(cfg.get(CONF_CAR_MIN_A, DEFAULT_CAR_MIN_A))
        self.max_a = int(cfg.get(CONF_CAR_MAX_A, DEFAULT_CAR_MAX_A))
        self.kw_per_a = float(cfg.get(CONF_CAR_VOLTAGE, DEFAULT_CAR_VOLTAGE)) * int(
            cfg.get(CONF_CAR_PHASES, DEFAULT_CAR_PHASES)) / 1000.0
        self.low_windows = 0
        self.decision: CarDecision | None = None
        self.applied = True
        self.action: str = "-"
        self.error: dict | None = None
        self._fail = 0
        self._retry_at: datetime | None = None
        self._range_tries = 0
        self._forced_at: datetime | None = None
        self._autostart_done = True
        self.last_change: datetime | None = None
        self._target: int | None = None
        self._amp_fixes = 0
        self._amp_fix_at: datetime | None = None
        self._over_since: datetime | None = None

    @property
    def min_kw(self) -> float:
        return self.min_a * self.kw_per_a

    @property
    def max_kw(self) -> float:
        return self.max_a * self.kw_per_a

    def switch_on(self) -> bool:
        st = self.hass.states.get(self.cfg[CONF_CAR_CHARGE_SWITCH])
        return st is not None and st.state == "on"

    def decide(self, now: datetime, plan, charging: bool, forced: bool) -> CarDecision:
        """Turn the planned kW into a current, with the start/stop hysteresis
        (one window at the minimum before stopping)."""
        if plan.car_grid:
            amps, reason = self.max_a, plan.car_reason
            self.low_windows = 0
        else:
            raw = max(0, min(self.max_a, math.floor(plan.car_kw / self.kw_per_a + 1e-9)))
            if raw >= self.min_a:
                amps, reason = raw, plan.car_reason
                self.low_windows = 0
            else:
                self.low_windows = CAR_STOP_AFTER_LOW_WINDOWS if forced else self.low_windows + 1
                if charging and self.low_windows < CAR_STOP_AFTER_LOW_WINDOWS:
                    amps, reason = self.min_a, "haelt_minimum"
                else:
                    amps, reason = 0, plan.car_reason if plan.car_reason != "pflicht_minimum" else "zu_wenig_ueberschuss"
        d = CarDecision(now, amps, round(amps * self.kw_per_a, 2), reason, plan.car_grid, forced)
        self.decision = d
        self.applied = False
        self._retry_at = None
        self._range_tries = 0
        self._amp_fixes = 0
        self._over_since = None
        if forced:
            self._forced_at = now
            self._autostart_done = False
        return d

    async def async_apply(self, now: datetime, active: bool, charging: bool, soc_known: bool,
                          commands_today: int) -> None:
        d = self.decision
        if d is None or self.applied:
            return
        if not active:
            self.action = "nur_beobachten"
            self.applied = True
            return
        if self._retry_at is not None and now < self._retry_at:
            return
        max_cmd = int(self.cfg.get(CONF_CAR_MAX_COMMANDS) or 0)
        if max_cmd and commands_today >= max_cmd:
            self.action = "befehlslimit"
            self.applied = True
            return
        if d.amps > 0 and not soc_known and not charging:
            self.action = "wartet_auf_fahrzeugdaten"
            return
        switch = self.cfg[CONF_CAR_CHARGE_SWITCH]
        current = self.cfg.get(CONF_CAR_CURRENT_ENTITY)
        on = self.switch_on() or charging
        try:
            if d.amps == 0:
                if on:
                    await self.hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
                    self._count_command()
                    self.action = "gestoppt"
                    self.last_change = now
                    self._log(f"Auto gestoppt ({_txt(d.reason)})")
                else:
                    self.action = "bleibt_aus"
            else:
                started = False
                if not on:
                    await self.hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
                    self._count_command()
                    started = True
                target, complete, changed = (d.amps, True, False)
                if current:
                    target, complete, changed = await self._set_amps(current, d.amps)
                self._target = target
                self.action = (f"ampere_{target}" if changed else f"ampere_{target}_unveraendert") + (
                    "_gestartet" if started else "")
                if started or changed:
                    self.last_change = now
                    self._log(f"Auto {'gestartet mit' if started else 'auf'} {target} A ({_txt(d.reason)})")
                if not complete:
                    self._range_tries += 1
                    if self._range_tries < 4:
                        self._retry_at = now + timedelta(seconds=90)
                        return
        except Exception as err:  # noqa: BLE001 - vehicle API hiccups must not break the cycle
            self._fail += 1
            delay = CAR_RETRY_DELAYS_MIN[min(self._fail, len(CAR_RETRY_DELAYS_MIN)) - 1]
            self._retry_at = now + timedelta(minutes=delay)
            self.error = {"zeit": now.isoformat(), "ampere": d.amps,
                          "fehler": f"{type(err).__name__}: {err}".strip(": "), "versuche": self._fail}
            self.action = "fehler"
            _LOGGER.warning("Car command failed (attempt %d), retry in %d min", self._fail, delay,
                            exc_info=self._fail == 1)
            return
        self.applied = True
        self._fail = 0
        self.error = None

    async def async_guard_autostart(self, now: datetime, active: bool, charging: bool) -> None:
        """Stop once a charge the car started on its own after a 0 A plug-in
        decision (only within a few minutes of that decision)."""
        d = self.decision
        if (not active or d is None or d.amps != 0 or self._autostart_done or self._forced_at is None
                or (now - self._forced_at).total_seconds() > CAR_AUTOSTART_GUARD_S or not charging):
            return
        try:
            await self.hass.services.async_call(
                "switch", "turn_off", {"entity_id": self.cfg[CONF_CAR_CHARGE_SWITCH]}, blocking=True)
            self._count_command()
            self.action = "autostart_gestoppt"
            self._log("Auto hatte von selbst gestartet — gestoppt")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Stopping the auto-start failed", exc_info=True)
        self._autostart_done = True

    async def async_guard_amps(self, now: datetime, charging: bool, measured_kw: float | None,
                               commands_today: int) -> None:
        """Re-send the current when the car charges with another one than set.

        Checked every cycle while charging, from CAR_AMPS_SETTLE_S after the last
        command: the current entity reports a different value, or the measured
        power stays well above the target for CAR_AMPS_SETTLE_S (entity stale).
        At most CAR_AMPS_MAX_FIXES re-sends per decision, within the daily budget."""
        d = self.decision
        entity = self.cfg.get(CONF_CAR_CURRENT_ENTITY)
        target = self._target
        if (d is None or not self.applied or d.amps <= 0 or not charging or not entity
                or target is None or target < 1):
            self._over_since = None
            return
        target_kw = target * self.kw_per_a
        over = measured_kw is not None and measured_kw > target_kw + max(1.5, 0.3 * target_kw)
        if not over:
            self._over_since = None
        elif self._over_since is None:
            self._over_since = now
        since = max(t for t in (self.last_change, self._amp_fix_at, d.time) if t is not None)
        if (now - since).total_seconds() < CAR_AMPS_SETTLE_S or self._amp_fixes >= CAR_AMPS_MAX_FIXES:
            return
        max_cmd = int(self.cfg.get(CONF_CAR_MAX_COMMANDS) or 0)
        if max_cmd and commands_today >= max_cmd:
            return
        cur = number(self.hass, entity)
        reported = round(cur) if cur is not None else None
        overdraw = over and (now - self._over_since).total_seconds() >= CAR_AMPS_SETTLE_S
        if not ((reported is not None and reported != target) or overdraw):
            return
        self._amp_fixes += 1
        self._amp_fix_at = now
        seen = f"{reported} A" if reported is not None and reported != target else f"{measured_kw:.1f} kW"
        try:
            await self.hass.services.async_call("number", "set_value", {"entity_id": entity, "value": target},
                                                blocking=True)
            self._count_command()
        except Exception:  # noqa: BLE001 - next cycle / next decision tries again
            _LOGGER.debug("Re-sending the current failed", exc_info=True)
            return
        self._over_since = None
        self.action = f"ampere_{target}_nachgesetzt"
        self._log(f"Auto lud mit {seen} statt {target} A — {target} A erneut gesendet")

    async def _set_amps(self, entity: str, amps: int) -> tuple[int, bool, bool]:
        def _max() -> float | None:
            st = self.hass.states.get(entity)
            try:
                return float(st.attributes.get("max")) if st is not None else None
            except (TypeError, ValueError):
                return None

        mx = _max()
        if mx is not None and amps > mx:
            try:
                await asyncio.wait_for(self.hass.services.async_call(
                    "homeassistant", "update_entity", {"entity_id": entity}, blocking=True), timeout=45)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Refreshing %s failed", entity, exc_info=True)
            mx = _max()
        target = amps if mx is None or amps <= mx else int(mx)
        cur = number(self.hass, entity)
        changed = False
        if target >= 1 and (cur is None or round(cur) != target):
            await self.hass.services.async_call("number", "set_value", {"entity_id": entity, "value": target},
                                                blocking=True)
            self._count_command()
            changed = True
        return target, target == amps, changed
