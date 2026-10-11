<img src="branding/logo.png" alt="Surplus Pilot" width="420">

Home Assistant custom integration that plans your PV surplus **as one budget**
for your electric car, your home battery and any number of switchable devices
(heat pumps, pool pumps, boilers, miners, …).

Most setups run two automations side by side — one for EV surplus charging,
one for devices — and they fight over the same kilowatts: the charger takes
everything the battery doesn't, the device controller then sees "no budget"
and the battery logic of one doesn't know the plans of the other. Surplus
Pilot replaces both with a single plan.

> **Surplus Pilot is the successor of Surplus Load Switch (v2.x).** Version
> 3.0.0 is a rewrite with a new domain (`surplus_pilot`). Existing Surplus
> Load Switch installations are not migrated automatically — set up Surplus
> Pilot fresh and remove the old integration.

![Surplus Pilot dashboard](docs/dashboard-overview.png)

## How the plan works

Every minute Surplus Pilot reads PV production, house load and battery state
and splits the surplus in this order:

1. **Car obligation** — what the car still needs for its next departure
   target (e.g. 50 % by 16:30), spread over the PV hours left before it.
   When the surplus is below the car's minimum current, the car is only
   lifted to the minimum (gap from battery/grid) if the PV carries at least
   half of it *and* the forecast hours that carry the minimum on their own
   won't bring the target — otherwise it waits for those hours.
2. **Home battery** — enough to be full by the end of the PV day. If the car
   leaves before that, the forecast PV *after* the departure is counted in:
   the battery can fill on its own once the car is gone.
3. **Devices** — by priority, each with its own time window, minimum daily
   runtime, battery reserve and dependency (e.g. a pool heat pump that needs
   the pump). What PV doesn't cover of the minimum runtime runs as **one
   block** in the cheapest part of the window — planned early when the
   forecast surplus won't cover it, otherwise only in the last hours so a
   sunny afternoon still wins. A started block runs through (at least 1 h); a device already running with
   at most an hour left just keeps going until its minimum is reached.
   Devices know the car's planned grid block: they don't start on PV within
   the hour before it, and the minimum-runtime planning doesn't count PV in
   the block's hours (the car takes it there).
4. **Car top-up** — whatever is left, up to the car's charge limit. Between
   two car decisions a device may claim power the top-up is using; the car
   follows at its next decision.
   *Exception:* when the car won't get a PV chance tomorrow (it is away during
   most of tomorrow's PV window, or tomorrow's forecast — after the base load
   and half a battery — covers less than half of what the car is missing), it
   charges **before** the devices today.
5. The rest is exported.

If the forecast says PV can't reach a departure target, the car charges the
missing part from the grid in the **cheapest 15-minute slots between now and
the departure**. Prices that aren't published yet (Tibber publishes tomorrow
at ~13:00) are estimated from the median of the same hour over the last 8
days — so a cheap midday today can win over a night that is usually dearer.
Among equally cheap slots the later ones are taken (a sunnier hour may still
help), and once there is just enough time left it charges regardless.

**Cheap charging (optional):** while the price is below the cheap threshold
(default: the cheapest 10 % of the last 8 days) the car is topped up from the
grid up to a target (default 80 %) — but only by the energy the PV forecast
won't bring before the next departure anyway, so a sunny tomorrow isn't
wasted. It charges in the cheapest *block* of consecutive published slots before
the departure (one block, no stop-and-go).
If your home battery discharges into the car (option *Home battery
discharges into the car*, default on), only slots without PV count and only
while the battery is at its minimum SoC — otherwise the battery would just
empty itself into the car, and grid charging in PV hours would take the PV
it needs. With the option off (battery set not to discharge into the
wallbox in the inverter app) every slot up to the departure counts, midday
included, and the car charges PV plus grid there — slots with PV only while
the home battery is at least *Cheap charging with PV from home battery* full
(default 50 %, 0 = no limit) — or below it when the forecast (×0.7) after the
car's block until the PV end still fills the battery and carries the house
(measured base load + running devices) meanwhile. Many inverters (e.g. Huawei) give the PV to the
wallbox first and let the battery carry the house, so a midday block still
drains it; a running block goes on down to 5 % below that value, then stops
and stays off in PV hours for the rest of the day. With a *home battery
power* sensor this is detected while the car charges (battery discharging
beyond the house's own need = it feeds the car) and followed when the
inverter changes it on its own, e.g. a price-driven winter mode. Only new
battery readings taken at least 6 min after the car started count (cloud
inverter data such as FusionSolar arrives every ~5 min), two consistent
ones flip the state (~10 min); the
setting is only the start value and applies again after 3 days without a
reading. Replayed on a sunny September this cost 6 kWh of grid energy a month;
on dark days it is what buys the cheap hours.

At night devices run on the home battery only if it lasts until the next
solar start (+1 h) with all planned loads, and only above each device's
battery reserve.

**Timing**
- The car is re-planned every 15 minutes on a 30-minute average. Its top-up
  above the obligation must also fit the median of the last 15 minutes, so a
  falling afternoon curve isn't bridged by the home battery.
- Plugging in, a changed departure or a wallbox mode change trigger a new car
  decision right away — a car that starts charging by itself after plug-in is
  stopped within seconds.
- Devices are re-planned every minute on a 5-minute median and switch only
  after the decision held for 10 minutes (20 for thermostats). A device that
  depends on another one goes off together with it.
- A device only **starts** when it can run for at least an hour: not in the
  last hour before its window closes, not on PV surplus in the last hour
  before the PV end, and on the battery path only if the battery stays above
  the device's reserve for an hour with the current load. A running device is
  not affected. Replayed over 30 days this cut the pool pump's runs under an
  hour from 25 to 2.
- The action log, the readings of the last 30 minutes and running switch
  countdowns survive a Home Assistant restart.

### Validated on recorded data

The planner is pure Python without Home Assistant imports. The same code was
replayed against 30 days of one household's recorded data (Sept/Oct, 14 kWh
battery, ~13 kWp, car home on about half the days) and compared with the
previous two-controller setup:

| 30 days | two controllers | Surplus Pilot |
|---|---|---|
| Grid feed-in | 458 kWh | 360 kWh (−21 %) |
| Device runtime (miner / pool pump / pool heat pump / boiler) | 525 / 194 / 157 / 116 h | 623 / 219 / 181 / 177 h |
| Departures below target | 0 | 0 |
| Grid energy for the car | 0 kWh | 6 kWh (cheap charging on, ≈ 1.40 €) |

## Requirements

Only Home Assistant. Everything else is chosen from your own entities:

**Required**
- PV power sensor (W or kW)
- House consumption sensor (total, *including* wallbox and managed devices)

**Optional**
- Home battery SoC + capacity (without a battery the plan simply has no step 2)
- Grid feed-in power (lets devices run on the battery while you export anyway)
- PV forecast — taken automatically from every integration that feeds the
  Energy dashboard (Forecast.Solar, Solcast, Open-Meteo Solar Forecast, …)
- Electricity prices — Tibber, or any price sensor with a forecast attribute
  (Nordpool `raw_today`/`raw_tomorrow`, EPEX Spot `data`, …). Optionally a
  current-price sensor whose statistics seed the price history on day one

**Car (optional, any brand)**
- A switch that starts/stops charging and a number for the charging current —
  on the car *or* on the wallbox
- State of charge (without it the car charges on surplus only, without
  departure targets), charge limit, plugged-in, charging, charging power,
  location — all optional
- For paid vehicle APIs: a daily command budget
- A "pause" entity/state: while it matches (e.g. the wallbox runs its own
  PV mode), Surplus Pilot leaves the car alone

**Devices (any number)**
- A `switch`/`input_boolean`, or a `climate` entity with the mode that means "on"
- Priority, expected power (learned from an optional power sensor),
  schedule helper or time window, minimum daily runtime, battery reserve,
  dependency on another device

## Installation

1. HACS → three dots → *Custom repositories* → `https://github.com/xchillxx/surplus-pilot`, category *Integration*
2. Install **Surplus Pilot**, restart Home Assistant
3. *Settings → Devices & services → Add integration → Surplus Pilot* (energy sensors)
4. *Configure* → add the car and your devices

## Dashboard

An example dashboard is in [`dashboards/surplus-pilot.yaml`](dashboards/surplus-pilot.yaml):
create a new dashboard, open the raw configuration editor and paste it. The
text cards (status, car, prices, device table, action log) find the Surplus
Pilot entities by themselves; for the tiles and graphs replace `my_car` and the
example device IDs with yours. No custom cards needed.

The screenshots show a German setup with a dark glass theme (card-mod); the UI
is available in English and German.

![Departures](docs/dashboard-departures.png)

## Entities

Entity IDs are created in the language Home Assistant runs in at setup
(German or English); the names below are the English ones.

| Entity | Purpose |
|---|---|
| *Operating mode* (`select`) | Automatic / Observe only / Off |
| *Status* (`sensor`) | One-line summary; attribute `erklaerung` explains every decision in plain words |
| PV power, Base load, Surplus, Battery reservation, Device budget, Forecast rest of today, Forecast tomorrow, Next departure, Last action (`sensor`) | What the plan sees and does; *Last action* keeps a log of the last 60 actions |
| Car: *Charging status*, *Charging power* (`sensor`) | Why the car charges (or not), target current, commands today; attribute `ladeplan` lists the planned grid charging (departure target / cheap top-up) with time, kWh and mean price — also while the car is unplugged |
| Car: *Charging control* (`switch`) | Car control on/off |
| Car: *Cheap charging* (`switch`), *Cheap threshold percentile*, *Cheap charging up to*, *Cheap charging with PV from home battery* (`number`) | Cheap charging on/off, threshold percentile, target, home-battery minimum for cheap charging in PV hours |
| Car: *Cheap threshold*, *Price now* (`sensor`) | Threshold and current price in ct/kWh |
| Device: *Status* (`sensor`) | Why a device is on/off, countdown to the next switch, runtime today, learned power |
| Device: *Automatic* (`switch`) | Off = Surplus Pilot leaves this device alone |
| Departures (`switch`, `time`, `number`, `text`, `date`) | 4 repeating departures (time, target SoC, away duration, every N days, from date) + one one-off departure |

*Observe only* computes and shows everything but switches nothing — useful to
compare with an existing setup before switching over.

## Support

Surplus Pilot is free. If it saves you money and you're about to sign up anyway,
these **referral links** give you and me a bonus at no extra cost:

- **Tibber** (dynamic electricity tariff): https://invite.tibber.com/cw4ufzqw
- **Tesla** (car or solar purchase): https://ts.la/daniel513094

## Feedback

Bugs and ideas: [GitHub issues](https://github.com/xchillxx/surplus-pilot/issues).
Please attach the *Status* sensor's `erklaerung` attribute and the *Last action* log —
they usually show why a decision was made.

## License

MIT
