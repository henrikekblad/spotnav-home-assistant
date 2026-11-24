# Fixtures

`dashboard/*`, `settings/v1/*` and `sessions/*` are the WebSocket shapes, which the card and the app vendor. The
webhook answers the same documents without the fields in `APP_UNREAD_SETTINGS`
(`custom_components/spotnav/api/webhook.py`, today `departure_date`, `departure_weekdays`, `fiscal_included`, `notifications`, `fill_to_limit`, `vehicle_ids` and `identify_mode`) in the `settings` record, so
`webhook/*` never contains them (`tests/test_webhook_parity.py` asserts it).

`power_month/*` are `spotnav/get_power_month`'s answers for a seeded month of the pilot's site (two demand-charge
parts, a 6 kW import target): `pilot_month.json` the running month with its hours, `pilot_older_month.json` an older
month with its day summaries only (`tests/test_power_month_api.py`). The webhook's `get_power_month` answers the same
with `ok` and `action` beside it (`webhook/get_power_month_*.json`).
`pilot_imported_month.json` is the running month again with its first four days read from Home Assistant's statistics
(`imported`) and an evening on the 8th when the house alone set the month's day peak, after which the hours carry the
level it lifted the target to (`month_peak_kw`).

`house/*` are `spotnav/get_house`'s answers, written by the pure view (`tests/test_house_view.py`): `villa_battery_sun.json`
a villa on 13 November 2026 at 17:20 with a week of reviewed forecasts, a small array and a 40 kWh battery running its
own optimiser, planned beside it in shadow; `house_learning.json` a house alone whose forecast is two days old.

`house_loads/*` are `spotnav/get_house_loads`'s answers, written by the pure answer (`tests/test_house_loads.py`):
`villa_day.json` a villa's day with five individual devices, two of them under 3 % of the house; `meters_over_house.json`
a month whose meters exceed the house (Övrigt clamped at 0); `no_devices.json` a year with no individual device.

`heat_card/*` are `spotnav/get_heat`'s answers, written by the pure view (`tests/test_heat_view.py`): `villa.json` a
villa on 16 November 2026 at 17:20 with three indoor sensors, three electric floor zones and an air-to-air heat pump,
the heat pump's meter split into heat and hot water, and the cooler night on (an administrator's answer, with the
settings' choices); `network_pump.json` a NIBE on its local integration whose own price control runs, away until
Friday (a household member's answer, with the notes).

`webhook/get_house*.json`, `get_heat*.json`, `update_heat_*.json`, `battery_control_*.json`,
`get_site_settings_view_success.json` and `sessions_*.json` are the charger webhook's site actions on the pilot's site
(no sun, no battery, two indoor sensors), each the WebSocket twin's answer with `ok` and `action` beside it
(`tests/test_webhook_site_parity.py`); the richer shapes of the same documents are the directories above. The heat
documents' `money` has the house documents' keys (`currency`, `major_unit`, `minor_unit`).
`webhook/get_plan_success.json` is the webhook's `get_plan` on the same site (a car whose charge fits).
`webhook/pool_*.json`, `spa_*.json` and `sauna_*.json` are the household's presses of the Pool, Spa and Sauna tabs
over the webhook on the same site with a pool, a Balboa spa and a Harvia sauna added (`tests/test_webhook_site_parity.py`):
a press that went through, a value refused, a second "now" within ten minutes, a bath or session that will not be ready
with no later hour where it fits (`spotnav_no_later_bath`, `spotnav_no_later_session`) and a site without the load.
The spa's weekly baths carry ISO weekdays (Monday 1 to Sunday 7), as a car's `departure_weekdays`.
`webhook/get_site_settings_view_priority.json` is `get_site_settings_view` over the webhook on a site with two chargers,
house heat, a water heater, a home battery and a pool: every row of its priority list (`tests/test_site_priority.py`).

`webhook/get_plan_does_not_fit.json`, `dashboard_does_not_fit.json`, `get_plan_takes_what_fits.json`,
`take_what_fits_*.json` and `later_departure_*.json` are a charge that does not fit under a 6 kW import target by its
departure (`tests/test_import_target_hold.py`'s tight night, prices held to the end of tomorrow): the car lane's
`does_not_fit` block in `get_plan`, the same block in the dashboard for an app that reads it (`reads:
["does_not_fit"]`), the plan once "Take what fits" is pressed, and the two choices' answers and refusals
(`tests/test_webhook_site_parity.py`). Ids are placeholders (`<site_id>`, `<charger_id>`).

`pairing/approved_v1_sites.json` is the pairing poll's approved answer for an instance with a home that has a
charger and a home without one (`tests/test_pairing_handshake.py`): `chargers` with each charger's `site_id`, and
`sites` with each home's webhook and chargers (the homes' secrets as placeholders).

`webhook/get_site*.json` and `webhook/site_webhook_*.json` are the home's webhook (`api/site_entry_webhook.py`) on the
pilot's site and on a home without a charger: `get_site`, its view of the site settings (the price read-only), a
site-bound `update_site_settings`, and the refusals for a missing `charger_id`, a charger of another home, a price
write and a home that is not loaded (`site_webhook_site_unloaded.json`, 503). `webhook/get_site_access_*.json` are a charger webhook's `get_site_access`: its home's door, no home, a home not loaded
(503) and the rate limit (`tests/test_site_entry_webhook.py`). Ids are placeholders (`<site_id>`, `<charger_id>`, `<site_webhook>`).

`target_suggestion/pilot.json` is `spotnav/suggest_import_target`'s answer for the pilot's site from a month of fake
statistics (`tests/test_target_suggestion_api.py`); `webhook/suggest_import_target_success.json` the webhook's.

`ecb/*` are the ECB's euro reference-rate files as published: `ecb_daily.xml` is the relay's capture of the daily
file (`spotnav-relay/testdata/upstream/ecb_daily.xml`, table of 2026-09-18) and `ecb_hist_90d_2026-10-30.xml` the
90-day history as served on 2026-10-30 (65 tables, 2026-08-03 to 2026-10-30). Both are keyless public files.

`zonneplan/forecast_2026-11-01.json` is the `forecast` attribute of the Zonneplan integration's tariff sensor
(`current_quarter_hourly_electricity_tariff`) as one installation showed it on 2026-11-01: 232 quarter-hours from
2026-10-31 14:00 to 2026-11-03 00:00 Amsterdam time, turned from the developer tools' YAML into JSON. It holds
Zonneplan's public tariff only, nothing about the connection.

`cars/<domain>.json` are 23 car integrations' entity shapes, read from each integration's source at the recorded
`commit` (research round 6, 2026-11-07): platform, key, translation key, unique id pattern, English name, device
class, unit, state class, options, an invented example state and whether the entity is enabled by default. Literal
names keep their placeholders (`{make} {nickname_or_model} …`). `tests/car_catalogs.py` registers one as a car and
`tests/test_car_catalogs.py` holds SpotNav's golden picks for each.

`beta/*` are `spotnav/beta`'s answers, written by `tests/test_beta.py` against fake engines: `off_idle.json` beta
off with a test that may run, `on_running.json` a beta period with a hardware test running, `on_done_failed.json` a
finished test whose charge step failed (sent by itself, `sent_ref`), `on_done_partial.json` a finished test whose battery was skipped (outside control) while the heat
path passed (the day's slot left), `ended.json` the period ended by itself (no site).
