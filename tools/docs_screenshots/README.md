# Documentation screenshots

Takes the screenshots in `docs/images/` against a throwaway Home Assistant that runs only on this
machine (127.0.0.1, port 8129). Nothing touches a real installation.

    tools/docs_screenshots/run.sh              # every screenshot
    tools/docs_screenshots/run.sh card-hero    # only the named ones
    tools/docs_screenshots/run.sh --keep-ha    # leave Home Assistant running afterwards (Ctrl-C ends it)
    DRIVER=v3shots.mjs tools/docs_screenshots/run.sh [--fresh|--reuse] house-card-now   # the 3.0 pictures, see below

Every phase's start and end is printed to stderr as `[timing] ...` (the 3.0 driver also prints its own steps), so a
slow run shows where its time goes. To take a subset, name the pictures: the card, tab and settings pictures are
independent of each other, and a run that names only some does only their work. There is no "changed only" mode:
which pictures a change touches is a judgement (a card change touches every picture of that card), so name them.

Needs `python3`, `node` (22 or newer) and `chromium`. The first run creates `.venv` and installs the
versions in `requirements.txt` (network needed once; Home Assistant also fetches a few component
requirements then). A run of the 2.x driver takes about four minutes, of which about two are a wait for a car to count as
unplugged (see [Car identification](#car-identification)); a 3.0 run on a kept installation takes about a minute
for a card picture.

## What it does

1. `run.sh` rebuilds `.config/` from nothing (the 2.x driver, and the 3.0 driver with `--fresh` or when the kept one
   is from another day; see below): a minimal `configuration.yaml`, the repository's `custom_components/spotnav`
   linked in, and the demo integrations below.
2. Prices come from the demo `nordpool` integration below, as they come from Home Assistant's own Nord Pool
   integration in a real installation (the SpotNav relay delivers no prices since 2.0). Converting SE4's EUR
   prices to SEK needs the ECB's reference rates: `rates_stub.py` serves the recorded rates
   (`tests/fixtures/ecb/ecb_daily.xml`, dated yesterday) on port 8130, and `ha_launch.py` (this tool only)
   redirects requests for the ECB's address to it; the integration is not modified.
3. `driver/shots.mjs` onboards the instance through its onboarding API (user "demo", English, Sweden,
   southern Sweden, metric), signs in through the normal login flow, creates the demo devices, then
   drives headless chromium (1280 px wide, English, Home Assistant's own dark theme: the browser reports `prefers-color-scheme: dark` and the stored profile theme setting is `{"dark":true}`) through the flows and crops each
   dialog or the card to its own bounding box.

## Demo integrations (`demo_components/`)

Named like the real integrations so SpotNav's detection treats them as such, and reduced to the shapes
the detection reads (device tree, entity ids, unique ids, translation keys):

- `ocpp`: a central system with two charge points, each with a connector device named short, as a person
  would rename it ("Garage", "Workshop": SpotNav titles a charger after the device chosen, so the card's header reads
  "Garage"), charge control, session current limit, energy register, per-phase current; the
  `ocpp.get_configuration` service answers `AssignedCurrent` with `1.16,2.10`.
- `sigen`: a plant ("Grid meter") with per-phase power and an inverter with voltages; values move a little
  every few seconds.
- `kia_uvo`: two cars, "Family car" (64 %) and "City car" (48 %), each with capacity, range, charge limits, a
  "plugged in" sensor (`binary_sensor.<car>_ev_battery_plug`, off) and a tracker at home. The screenshot-only
  service `kia_uvo.docs_set_plug` (`vehicle`: `family_car` or `city_car`, `plugged`) moves a car's plug sensor.
- `ocpp` also has the screenshot-only service `ocpp.docs_set_status` (`status`, for example `Available` or
  `Preparing`), which unplugs or plugs in a car at the connector.
- `nordpool`: Home Assistant's Nord Pool integration reduced to its `nordpool.get_prices_for_date` service, which
  answers any area and date with the recorded SE4 day (`tests/fixtures/relay/day_SE4_2026-09-22_96.json`) in EUR/MWh
  per quarter hour of the CET day; tomorrow is a rotated, slightly scaled copy, left out with `STUB_TOMORROW=0`.
  It replaces the core integration in the throwaway instance (a custom integration of the same name wins).
- `demo_vision`: a camera ("Driveway camera") whose picture is drawn by `scene.py` (a carport, a parking bay and
  the parked car seen from the front: no photograph, no number plate), and an AI Task entity ("Local vision
  model AI Task") that takes pictures and answers every comparison with the first car, "medium", so the camera
  never decides alone. `demo_vision.docs_park` (`vehicle`: `family_car`, `city_car` or `none`) chooses the car in
  the picture.

## Charge history

`seed_sessions.py` (loaded by `ha_launch.py` when `DOCS_SEED_SESSIONS` is set, which `run.sh` does) gives
a charger that has no recorded sessions about 75 days of synthetic night charges, with hourly prices, in
the integration's session store in memory only. Early in a month (before the 12th) the driver shows the
previous month so that the bar chart has days to show.

## Car identification

`driver/demo.mjs` (also used by the app's screenshot tool) drives it through the services above and SpotNav's
own WebSocket commands. Once the charger is added it chooses the camera, its AI Task and a frame around the
parking bay, takes a day reference picture of each car (each parked in the drawn picture in turn) and lets
"Family car" report its cable plugged in, so the identification at the charger's first plug-in decides it and
the card's car line says how. Identification stays **Automatic**.

For the identification pictures the car is unplugged after the charge history, and plugged in again once the
unplug counts as one (SpotNav takes an unplug shorter than two minutes for the same plug-in; the driver waits
for what is left of the 130 s): nothing has decided, so the status line leads with "Identifying the car…"
(`card-identifying`). "City car" then reports its cable plugged in and is the car (`card-identified`). Last,
a quick replug with "Family car" reporting its cable too: both cars say they are plugged in, so SpotNav asks
at once (`card-identify-question`).

## Shots

Written: add-type, charger-type, charger-device, charger-found, charger-adjust, join-site, site-basic,
site-meter, site-found, site-options, card-picker, card-hero, card-from-now (the card on the From now axis), card-history, card-settings (the whole Settings
page as it opens), card-settings-price, card-settings-vehicle (the car section with a tab per car),
card-settings-charger (with identification and the camera), card-settings-site, card-settings-support (with
the Automations row), card-automations (the blueprints it lists, one used by an automation the driver makes from it), card-camera-frame (the crop
editor), card-reference-picture, card-settings-plan, card-identifying, card-identified, card-identify-question,
diagnostics.

Only when named: `social-card`, the hero card in the light theme at twice the pixels, written to
`tools/social/card-light.png` for the banner and the README's card and app picture (`tools/social/render.sh`).

Not produced (they need real-world state): card-waiting (only before about 13:45 local with no tomorrow
prices; `STUB_TOMORROW=0` removes tomorrow from the demo Nord Pool, but the integration decides by the real clock),
card-charging (a running charge), card-solar (a sunny day and a forecast source), pairing-approve (a
real app pairing).

## The 3.0 pictures (`driver/v3shots.mjs`)

The house card, the heat card, the hero picture with the three cards, the settings pictures of the home battery and
the beta tester box, and the pictures for the banner are taken by a driver of their own, in Home Assistant's light
theme (`DRIVER=v3shots.mjs tools/docs_screenshots/run.sh [--keep-ha] [shot-name ...]`; the older `shots.mjs` still
takes the 2.x pictures of the charger flow, in the dark theme). Written: `house-card-now`, one per other tab of the
house card (`house-card-plan` with **Per load** and **In words** opened, `house-card-flow`, `house-card-chart` and
`house-card-earnings` on the month, `house-card-price`), `house-card-plan-past` (the Plan tab stepped back to
yesterday, planned against what ran), `heat-card-now`, `heat-card-plan`, `heat-card-use`, `heat-card-hot-water`,
`heat-card-pool`, `heat-card-spa`, `heat-card-sauna` (the Pool tab waits until the water counts, five minutes of
circulation after the start), `settings-home-battery`, `settings-beta`, `settings-priority` (the priority list's editor),
`settings-notifications` and `settings-notification-phone` (the phones, one phone opened), `settings-pool`,
`settings-spa`, `settings-sauna` (the heat card's gear on each tab), `readme-hero` and `docs-home` (a copy of it); only when named: `social-card`
(the house card at twice the pixels, `tools/social/card-light.png`). The house card's tab pictures are taken first,
with no scene pinned, so that the battery's measured and planned level meet at now.

`driver/v3setup.mjs` sets the installation up through the config flow API (the app's screenshot tool uses it too):
the demo integrations, a charger "Garage" with two cars, a site "Home" and the site's heating. What the site finds
beyond the 2.x demo:

- `demo_components/demo_house`: a made-up house, one deterministic simulation in five-minute steps (`model.py`: a sun, the
  house's load, a heat pump with hot-water runs, a home battery, the grid, the rooms and the weather) behind the sensors
  (solar, grid and battery energy counters, the heat pump's meter, three rooms and the outdoor temperature) and two
  switches (a relay that lowers the pump's curve, a hot-water boost). Its service `demo_house.docs_seed` imports the
  simulation's weeks as the recorder's statistics (hourly for 56 days, five-minute for ten), which `v3setup.mjs`
  points the Energy dashboard at; `demo_house.docs_scene` pins the live power (sun, house, heat, battery in kW) for a
  picture, so that no picture depends on the hour it is taken.
- `demo_components/sigen` now also has the home battery: its level, ratings, work mode and the controls the battery
  check writes (a Remote EMS switch, the control mode select and the charge and discharge limits); they move the
  simulated battery, so the check of its control sees a plant that obeys.
- `demo_components/tuya_local`: a pool heat pump (a climate with Tuya Local's `pool_heatpump` translation key) and the
  pool's circulation pump ("Pool pump", a relay on a device of its own) around one small water model; with the fake spa
  and sauna of `tools/fake_devices/` (`balboa`, `saunum`, copied in by `run.sh` and renamed "Spa" and "Sauna" as a
  person would) the site has a pool, a spa and a sauna, each under SpotNav's control. Each run books a bath for tonight
  (20:00) and a sauna session this evening (19:00), tomorrow's when the run is after 15:00, beside weekly ones.
- Two Companion phones ("Anna's phone", "Erik's phone"), registered through the `mobile_app` API as the app does (the push
  address is nothing that listens; nothing is sent) and turned on, for the Notifications section.
- `seed_house.py` (loaded by `ha_launch.py` with `DOCS_SEED_HOUSE`): the house-load history the forecast needs, the heat
  store's hot-water runs and five shadow days (the progress line under the heat card's Plan), a month of reviewed
  battery days and the battery's day money (the earnings), and the decision history's hours from three days back to
  the first one recorded (`house-card-plan-past`: the demo house's measured means, the recorded day's prices, the car
  charging at night and once taking less than planned, the battery and the heat only watched). In memory only, as
  `seed_sessions.py`, except the decision history, which the site writes to its store with the hours it records.

Time of day: the live figures are pinned, but SpotNav plans from the real clock. The pictures of today's energy want an
evening or a day (not just after midnight, when today holds no hours yet). **`heat-card-hot-water` needs a boost
planned for the rest of today**, which is in the cheap hours before the day's peaks (02:30 and 12:30 here): take it
before about 12:00, or just after midnight (there is no way to pin the demo's clock without touching the integration's
planning, so none is pinned); the driver waits for one for up to `HOT_WATER_WAIT_MIN` minutes (150). Taken just after
midnight the Plan tab shows the day's planned runs ahead (hollow droplet) and its peak ticks, but no measured run yet
(the day has none); take it after the night's boost and before noon to see one as well.
`settings-home-battery` waits for the battery's check, about two minutes on a new installation (beta on, battery control on, a
cloudy evening pinned so that the battery discharges; a kept installation has passed it already). The past days' prices (the month's cost and earnings in the app's pictures) come within seconds of the start, see the quick demo waits below.

**A kept installation.** With `DRIVER=v3shots.mjs` the finished configuration directory (the site, the charger, weeks
of recorder history, the Energy dashboard, the hot-water and battery seeds) stays between runs: the driver stamps it
(`.config/.docs_built`, the day it was built) once the site is set up, and the next run reuses it while the stamp is
today's (the demo house's weeks end at the day it was built), which skips onboarding, the demo devices and the site
set-up, the 90-second wait for a new site's first reading and the check of the home battery (a kept installation has
passed it). `--reuse` keeps it whatever its day; `--fresh` rebuilds it. The hours since it was last running are imported
again at its start (`importHistory` in `v3setup.mjs`), and a kept installation answers its REST API before its
integrations are set up, which `HA_ONBOARDED=1` (set by `run.sh` then) makes the driver wait out.

**Quick demo waits (`demo_fast.py`, loaded with `DOCS_FAST_CHECK`, set by `run.sh`).** The demo battery obeys at once, so
the check of its control (`settings-home-battery`) settles in about two minutes instead of twenty: its settle and
reading times and the re-arm pause are cut. The price backfill and the ECB's exchange rates (the past days' prices, which
the month's cost and earnings need) are read within seconds of the start instead of waiting for the hourly cycle, and the
rates stub serves a 90-day file with a table for every weekday, so the past days are converted at all (they were not
before: a day older than the one table had no rate). All of it patches the running Home Assistant process in memory; the
integration's files are never changed, and a real installation never loads it.

`HA_PORT`, `STUB_PORT`, `DOCS_CONFIG` and `DOCS_LOGS` move the instance, so that two can run side by side.

## Things that can break

- The driver finds elements by their text and by Home Assistant's and the card's DOM (`dialog[part~=dialog]`,
  `ha-dropdown-item`, `.spotnav-dialog`, `section[data-section=…]`, `[data-edit=…]`, `[data-vehicle-tab=…]`); a
  frontend or card change can need a selector update.
- The card reads its dashboard every 30 seconds, so the driver reloads it after each identification step.
- The frontend sometimes hangs on its loading screen; the driver reloads up to four times.
- Card pictures follow whatever card is built in `custom_components/spotnav/www/` at the time.
- The hero shows a plan only after the plan settings have been saved once (the driver does this); a fresh
  charger keeps a proposal until then.
- The price curve is the recorded fixture, rotated for tomorrow; around a clock change the 96 intervals of
  a day do not fit and the stub should be adjusted.
