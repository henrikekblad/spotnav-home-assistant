# ENTSO-E fixtures

Copied unchanged from the relay's `spotnav-relay/testdata/upstream/` (the `entsoe_*` and `synthetic_*` files),
where they are described in its `README.md`. They feed `tests/test_entsoe_parse.py`, the port of the relay's
ENTSO-E parser tests.

* `entsoe_*` are real responses from the Transparency Platform, captured on 2026-09-21. Each is named for the
  zone and the **local** day the relay asked for. For the CET-clock zones that is also the CET market day Home
  Assistant asks for; for Finland (`entsoe_FI_2026-09-21_multiday.xml`) the document carries both CET delivery
  days around it, so it serves either calendar.
* `synthetic_*` were built by hand for shapes a real server cannot be asked for: complete clock-change days,
  negative hours, the 2025-10-01 day that straddles the 15-minute switch for an Eastern European zone, and
  acknowledgements with reason 999 and different texts (`no_data`, `auth`, `unknown`).

No file carries a request URL or a credential: the captures were written as response bodies only.
