# Fixtures

`dashboard/*`, `settings/v1/*` and `sessions/*` are the WebSocket shapes, which the card and the app vendor. The
webhook answers the same documents without the fields in `APP_UNREAD_SETTINGS`
(`custom_components/spotnav/api/webhook.py`, today `departure_date`, `departure_weekdays`, `fiscal_included`, `notifications`, `fill_to_limit`, `vehicle_ids` and `identify_mode`) in the `settings` record, so
`webhook/*` never contains them (`tests/test_webhook_parity.py` asserts it).

`power_month/*` are `spotnav/get_power_month`'s answers for a seeded month of the pilot's site (two demand-charge
parts, a 6 kW import target): `pilot_month.json` the running month with its hours, `pilot_older_month.json` an older
month with its day summaries only (`tests/test_power_month_api.py`). The webhook's `get_power_month` answers the same
with `ok` and `action` beside it (`webhook/get_power_month_*.json`).

`ecb/*` are the ECB's euro reference-rate files as published: `ecb_daily.xml` is the relay's capture of the daily
file (`spotnav-relay/testdata/upstream/ecb_daily.xml`, table of 2026-09-18) and `ecb_hist_90d_2026-10-30.xml` the
90-day history as served on 2026-10-30 (65 tables, 2026-08-03 to 2026-10-30). Both are keyless public files.

`zonneplan/forecast_2026-11-01.json` is the `forecast` attribute of the Zonneplan integration's tariff sensor
(`current_quarter_hourly_electricity_tariff`) as one installation showed it on 2026-11-01: 232 quarter-hours from
2026-10-31 14:00 to 2026-11-03 00:00 Amsterdam time, turned from the developer tools' YAML into JSON. It holds
Zonneplan's public tariff only, nothing about the connection.
