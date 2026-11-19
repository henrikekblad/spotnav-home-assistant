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
