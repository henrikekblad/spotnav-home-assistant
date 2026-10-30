# Fixtures

`dashboard/*`, `settings/v1/*` and `sessions/*` are the WebSocket shapes, which the card and the app vendor. The
webhook answers the same documents without the fields in `APP_UNREAD_SETTINGS`
(`custom_components/spotnav/api/webhook.py`, today `departure_date`, `departure_weekdays`, `fiscal_included`, `notifications`, `fill_to_limit`, `vehicle_ids` and `identify_mode`) in the `settings` record, so
`webhook/*` never contains them (`tests/test_webhook_parity.py` asserts it).

`ecb/*` are the ECB's euro reference-rate files as published: `ecb_daily.xml` is the relay's capture of the daily
file (`spotnav-relay/testdata/upstream/ecb_daily.xml`, table of 2026-09-18) and `ecb_hist_90d_2026-10-30.xml` the
90-day history as served on 2026-10-30 (65 tables, 2026-08-03 to 2026-10-30). Both are keyless public files.
