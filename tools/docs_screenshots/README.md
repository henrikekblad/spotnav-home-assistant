# Documentation screenshots

Takes the screenshots in `docs/images/` against a throwaway Home Assistant that runs only on this
machine (127.0.0.1, port 8129). Nothing touches a real installation.

    tools/docs_screenshots/run.sh              # every screenshot
    tools/docs_screenshots/run.sh card-hero    # only the named ones
    tools/docs_screenshots/run.sh --keep-ha    # leave Home Assistant running afterwards (Ctrl-C ends it)

Needs `python3`, `node` (22 or newer) and `chromium`. The first run creates `.venv` and installs the
versions in `requirements.txt` (network needed once; Home Assistant also fetches a few component
requirements then). A run takes about four minutes, of which about two are a wait for a car to count as
unplugged (see [Car identification](#car-identification)).

## What it does

1. `run.sh` rebuilds `.config/` from nothing: a minimal `configuration.yaml`, the repository's
   `custom_components/spotnav` linked in, and the demo integrations below.
2. `relay_stub.py` serves the recorded relay documents from `tests/fixtures/relay` with today's and
   tomorrow's dates (port 8130). The integration's relay address is fixed in its code, so
   `ha_launch.py` (this tool only) redirects requests for that address to the stub; the integration is
   not modified.
3. `driver/shots.mjs` onboards the instance through its onboarding API (user "demo", English, Sweden,
   southern Sweden, metric), signs in through the normal login flow, creates the demo devices, then
   drives headless chromium (1280 px wide, English, Home Assistant's own dark theme: the browser reports `prefers-color-scheme: dark` and the stored profile theme setting is `{"dark":true}`) through the flows and crops each
   dialog or the card to its own bounding box.

## Demo integrations (`demo_components/`)

Named like the real integrations so SpotNav's detection treats them as such, and reduced to the shapes
the detection reads (device tree, entity ids, unique ids, translation keys):

- `ocpp`: a central system with two charge points ("Garage charger", "Workshop charger"), each with a
  connector device, charge control, session current limit, energy register, per-phase current; the
  `ocpp.get_configuration` service answers `AssignedCurrent` with `1.16,2.10`.
- `sigen`: a plant ("Grid meter") with per-phase power and an inverter with voltages; values move a little
  every few seconds.
- `kia_uvo`: two cars, "Family car" (64 %) and "City car" (48 %), each with capacity, range, charge limits, a
  "plugged in" sensor (`binary_sensor.<car>_ev_battery_plug`, off) and a tracker at home. The screenshot-only
  service `kia_uvo.docs_set_plug` (`vehicle`: `family_car` or `city_car`, `plugged`) moves a car's plug sensor.
- `ocpp` also has the screenshot-only service `ocpp.docs_set_status` (`status`, for example `Available` or
  `Preparing`), which unplugs or plugs in a car at the connector.
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
site-meter, site-found, site-options, card-picker, card-hero, card-history, card-settings (the whole Settings
page as it opens), card-settings-price, card-settings-vehicle (the car section with a tab per car),
card-settings-charger (with identification and the camera), card-settings-site, card-camera-frame (the crop
editor), card-reference-picture, card-settings-plan, card-identifying, card-identified, card-identify-question,
diagnostics.

Not produced (they need real-world state): card-waiting (only before about 13:45 local with no tomorrow
prices; `STUB_TOMORROW=0` removes tomorrow from the stub, but the integration decides by the real clock),
card-charging (a running charge), card-solar (a sunny day and a forecast source), pairing-approve (a
real app pairing).

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
