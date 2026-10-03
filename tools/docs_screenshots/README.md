# Documentation screenshots

Takes the screenshots in `docs/images/` against a throwaway Home Assistant that runs only on this
machine (127.0.0.1, port 8129). Nothing touches a real installation.

    tools/docs_screenshots/run.sh              # every screenshot
    tools/docs_screenshots/run.sh card-hero    # only the named ones
    tools/docs_screenshots/run.sh --keep-ha    # leave Home Assistant running afterwards (Ctrl-C ends it)

Needs `python3`, `node` (22 or newer) and `chromium`. The first run creates `.venv` and installs the
versions in `requirements.txt` (network needed once; Home Assistant also fetches a few component
requirements then). A run takes about two minutes.

## What it does

1. `run.sh` rebuilds `.config/` from nothing: a minimal `configuration.yaml`, the repository's
   `custom_components/spotnav` linked in, and the demo integrations below.
2. `relay_stub.py` serves the recorded relay documents from `tests/fixtures/relay` with today's and
   tomorrow's dates (port 8130). The integration's relay address is fixed in its code, so
   `ha_launch.py` (this tool only) redirects requests for that address to the stub; the integration is
   not modified.
3. `driver/shots.mjs` onboards the instance through its onboarding API (user "demo", English, Sweden,
   southern Sweden, metric), signs in through the normal login flow, creates the demo devices, then
   drives headless chromium (1280 px wide, light theme, English) through the flows and crops each
   dialog or the card to its own bounding box.

## Demo integrations (`demo_components/`)

Named like the real integrations so SpotNav's detection treats them as such, and reduced to the shapes
the detection reads (device tree, entity ids, unique ids, translation keys):

- `ocpp`: a central system with two charge points ("Garage charger", "Workshop charger"), each with a
  connector device, charge control, session current limit, energy register, per-phase current; the
  `ocpp.get_configuration` service answers `AssignedCurrent` with `1.16,2.10`.
- `sigen`: a plant ("Grid meter") with per-phase power and an inverter with voltages; values move a little
  every few seconds.
- `kia_uvo`: "Family car" with charge level 64 %, capacity, range and charge limits.

## Charge history

`seed_sessions.py` (loaded by `ha_launch.py` when `DOCS_SEED_SESSIONS` is set, which `run.sh` does) gives
a charger that has no recorded sessions about 75 days of synthetic night charges, with hourly prices, in
the integration's session store in memory only. Early in a month (before the 12th) the driver shows the
previous month so that the bar chart has days to show.

## Shots

Written: add-type, charger-type, charger-device, charger-found, charger-adjust, join-site, site-basic,
site-meter, site-found, site-options, card-picker, card-hero, card-settings-price, card-settings-plan,
card-settings-vehicle, card-settings-site, card-history, diagnostics.

Not produced (they need real-world state): card-waiting (only before about 13:45 local with no tomorrow
prices; `STUB_TOMORROW=0` removes tomorrow from the stub, but the integration decides by the real clock),
card-charging (a running charge), card-solar (a sunny day and a forecast source), pairing-approve (a
real app pairing).

## Things that can break

- The driver finds elements by their text and by Home Assistant's and the card's DOM (`dialog[part~=dialog]`,
  `ha-dropdown-item`, `.spotnav-dialog`, `section.spotnav-settings-section`); a frontend or card change can
  need a selector update.
- The frontend sometimes hangs on its loading screen; the driver reloads up to four times.
- Card pictures follow whatever card is built in `custom_components/spotnav/www/` at the time.
- The hero shows a plan only after the plan settings have been saved once (the driver does this); a fresh
  charger keeps a proposal until then.
- The price curve is the recorded fixture, rotated for tomorrow; around a clock change the 96 intervals of
  a day do not fit and the stub should be adjusted.
