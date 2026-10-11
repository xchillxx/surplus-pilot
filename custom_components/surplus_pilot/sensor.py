"""Sensors: what Surplus Pilot sees, plans and does — readable on a dashboard."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CAR, CONF_CAR_NAME, CONF_DEV_ID, CONF_DEV_NAME, CONF_DEVICES, DOMAIN
from .coordinator import PilotCoordinator
from .entity import PilotEntity, car_info, device_info, hub_info

DEVICE_REASONS = ["deaktiviert", "ausserhalb_zeitfenster", "wartet_auf_abhaengigkeit", "mindestlaufzeit",
                  "ueberschuss", "akku_reicht", "akku_reserve", "kein_ueberschuss", "akku_reicht_nicht", "zu_kurz",
                  "manuell", "unbekannt"]
CAR_REASONS = ["kein_auto", "nicht_da", "ladelimit_erreicht", "netz_pflicht", "netz_billig", "pflicht_minimum",
               "zu_wenig_ueberschuss", "billig_akku", "pflicht", "pflicht_und_rest", "vorrang_rest", "rest", "haelt_minimum",
               "pausiert", "unbekannt"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    co: PilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    hub = hub_info(entry.entry_id)
    ents: list[SensorEntity] = [
        StatusSensor(co, hub),
        KwSensor(co, hub, "pv", "pv_kw"),
        KwSensor(co, hub, "grundlast", "grundlast_kw"),
        KwSensor(co, hub, "ueberschuss", "ueberschuss_kw"),
        KwSensor(co, hub, "akku_ziel", "akku_ziel_kw", attrs=("akku_ziel_modus",)),
        KwSensor(co, hub, "geraete_budget", "geraete_budget_kw"),
        KwhSensor(co, hub, "prognose_rest_heute", "prognose_rest_heute_kwh"),
        KwhSensor(co, hub, "prognose_morgen", "prognose_morgen_kwh"),
        DepartureSensor(co, hub),
        LogSensor(co, hub),
    ]
    car = entry.data.get(CONF_CAR)
    if car:
        info = car_info(entry.entry_id, car.get(CONF_CAR_NAME) or "Auto")
        ents.append(CarSensor(co, info))
        ents.append(KwSensor(co, info, "auto_leistung", "auto_kw"))
        ents.append(PriceSensor(co, info, "billig_schwelle", "billig_schwelle"))
        ents.append(PriceSensor(co, info, "preis_jetzt", "preis_jetzt"))
    for dev in entry.data.get(CONF_DEVICES) or []:
        ents.append(DeviceSensor(co, device_info(entry.entry_id, dev), dev))
    add(ents)


class KwSensor(PilotEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, co, info, key, field, attrs=()):
        super().__init__(co, key, info)
        self._attr_translation_key = key
        self._field = field
        self._attrs = attrs

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get(self._field)

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {a: d.get(a) for a in self._attrs} or None


class KwhSensor(KwSensor):
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = None
    _attr_suggested_display_precision = 1


class StatusSensor(PilotEntity, SensorEntity):
    """One readable line: what goes where right now, and why."""

    _attr_translation_key = "status"

    def __init__(self, co, info):
        super().__init__(co, "status", info)

    @property
    def native_value(self):
        return self.coordinator.summary()[:250]

    @property
    def extra_state_attributes(self):
        d = dict(self.coordinator.data or {})
        d.pop("auto_plan", None)
        dep = d.pop("naechste_abfahrt", None)
        d["naechste_abfahrt"] = dep.start.isoformat() if dep else None
        d["betriebsart"] = self.coordinator.mode
        plan = self.coordinator.device_plan
        if plan:
            d["budget"] = {k: round(v, 2) for k, v in plan.budget.items()}
        d["erklaerung"] = self.coordinator.explain()
        return d


class DepartureSensor(PilotEntity, SensorEntity):
    _attr_translation_key = "naechste_abfahrt"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, co, info):
        super().__init__(co, "naechste_abfahrt", info)

    @property
    def native_value(self):
        dep = (self.coordinator.data or {}).get("naechste_abfahrt")
        return dep.start if dep else None

    @property
    def extra_state_attributes(self):
        dep = (self.coordinator.data or {}).get("naechste_abfahrt")
        cp = self.coordinator.car_plan
        if not dep:
            return None
        return {"name": dep.name, "ziel_soc": dep.target_soc,
                "zurueck": dep.returns.isoformat() if dep.returns else None,
                "bedarf_kwh": round(cp.need_kwh, 1) if cp else None,
                "aus_pv_nicht_gedeckt_kwh": round(cp.uncovered_kwh, 1) if cp else None}


class LogSensor(PilotEntity, SensorEntity):
    _attr_translation_key = "log"

    def __init__(self, co, info):
        super().__init__(co, "log", info)

    @property
    def native_value(self):
        log = self.coordinator.log
        return log[0]["text"][:250] if log else None

    @property
    def extra_state_attributes(self):
        return {"eintraege": list(self.coordinator.log)}


class CarSensor(PilotEntity, SensorEntity):
    _attr_translation_key = "auto_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CAR_REASONS

    def __init__(self, co, info):
        super().__init__(co, "auto_status", info)

    @property
    def native_value(self):
        car = self.coordinator.car
        d = self.coordinator.data or {}
        if d.get("auto_pausiert"):
            return "pausiert"
        if car is None or car.decision is None:
            return "unbekannt"
        r = car.decision.reason
        return r if r in CAR_REASONS else "unbekannt"

    @property
    def extra_state_attributes(self):
        car = self.coordinator.car
        cp = self.coordinator.car_plan
        d = self.coordinator.data or {}
        if car is None:
            return None
        dec = car.decision
        return {
            "soll_ampere": dec.amps if dec else None, "soll_kw": dec.kw if dec else None,
            "entschieden_um": dec.time.isoformat() if dec else None, "aktion": car.action,
            "befehle_heute": self.coordinator.store.commands_today, "fehler": car.error,
            "pflicht_kw": round(cp.car_must_kw, 2) if cp else None,
            "bedarf_kwh": round(cp.need_kwh, 1) if cp else None,
            "netz_noetig_kwh": round(cp.uncovered_kwh, 1) if cp else None,
            "vorrang_vor_geraeten": cp.car_first if cp else None,
            "soc": d.get("auto_soc"), "limit": d.get("auto_limit"), "da": d.get("auto_da"),
            "laedt": d.get("auto_laedt"),
            # planned grid charging if the PV forecast isn't enough (also while unplugged)
            "ladeplan": [{
                "art": w.kind, "ziel_soc": round(w.target_soc), "kwh": round(w.kwh, 1),
                "start": w.start.isoformat() if w.start else None, "ende": w.end.isoformat() if w.end else None,
                "preis_ct": round(w.price * 100, 1) if w.price is not None else None,
                "geschaetzt": w.estimated, "laeuft": w.running,
                "spaetestens": w.latest.isoformat() if w.latest else None,
            } for w in self.coordinator.charge_windows],
        }


class DeviceSensor(PilotEntity, SensorEntity):
    _attr_translation_key = "geraet_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DEVICE_REASONS

    def __init__(self, co, info, dev):
        super().__init__(co, f"{dev[CONF_DEV_ID]}_status", info)
        self._dev = dev

    @property
    def native_value(self):
        co = self.coordinator
        did = self._dev[CONF_DEV_ID]
        if not co.store.data["device_enabled"].get(did, True):
            return "manuell"
        dec = co.device_plan.devices.get(did) if co.device_plan else None
        if dec is None:
            return "unbekannt"
        return dec.reason if dec.reason in DEVICE_REASONS else "unbekannt"

    @property
    def extra_state_attributes(self):
        co = self.coordinator
        did = self._dev[CONF_DEV_ID]
        dec = co.device_plan.devices.get(did) if co.device_plan else None
        from .coordinator import device_is_on
        return {
            "an": device_is_on(co.hass, self._dev),
            "soll_an": dec.on if dec else None,
            "pfad": dec.path if dec else None,
            "schaltet_in_s": co.countdown_s(did),
            "laufzeit_heute_h": round(co.store.runtime_h(did), 2),
            "mindestlauf_ab": co.forced_run_start(did),
            "leistung_kw": co._decision_kw(self._dev),
            "name": self._dev.get(CONF_DEV_NAME),
        }


class PriceSensor(PilotEntity, SensorEntity):
    """Price in ct/kWh (the archive keeps the source unit, Tibber: EUR/kWh)."""

    _attr_native_unit_of_measurement = "ct/kWh"
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:currency-eur"

    def __init__(self, co, info, key, field):
        super().__init__(co, key, info)
        self._attr_translation_key = key
        self._field = field

    @property
    def native_value(self):
        v = (self.coordinator.data or {}).get(self._field)
        return None if v is None else round(v * 100, 2)

    @property
    def extra_state_attributes(self):
        if self._field != "billig_schwelle":
            return None
        st = self.coordinator.store.data
        return {"perzentil": st["cheap_percentile"], "billig_laden_bis": st["cheap_target"],
                "aktiv": st["cheap_enabled"],
                "historie_stunden": (self.coordinator.data or {}).get("preis_historie_h")}
