# Fixtures

`dashboard/*`, `settings/v1/*` and `sessions/*` are the WebSocket shapes, which the card and the app vendor. The
webhook answers the same documents without the fields in `APP_UNREAD_SETTINGS`
(`custom_components/spotnav/api/webhook.py`, today `departure_date` and `departure_weekdays`) in the `settings` record, so
`webhook/*` never contains them (`tests/test_webhook_parity.py` asserts it).
