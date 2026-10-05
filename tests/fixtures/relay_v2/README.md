# Relay contract v2 fixtures

Copied unchanged from `spotnav-relay` (`web/src/domain/__tests__/fixtures/relay/`, relay commit
`2036ea4`, 2026-10-05, for `areas-v2.json`, which now carries each area's `publication`; the rest from `ce6b257`, 2026-10-03). They are not hand-written: the relay's own Go writer produces them
(`internal/format/web_fixtures_test.go`, which fails when the committed files drift from what the
writer produces; `SPOTNAV_UPDATE_WEB_FIXTURES=1` rewrites them). Re-sync by copying the directory
again after the relay test has been run; never edit a file here by hand.

The scenario is a London evening, 2026-10-04 23:30 BST, which is already 00:30 on the 5th in Paris
and Madrid, so "today" in London and Lisbon needs two market-day files:

| file | what it is |
|---|---|
| `areas-v2.json` | `GB-C` (no `eic`, `market_tz: Europe/Paris`, `included: [vat, tax, grid_fee]`, Octopus source), `PT` (`tz: Europe/Lisbon`, `market_tz: Europe/Madrid`), `SE4` (one calendar) |
| `areas-v1.json` | the same relay's frozen v1 list: `PT` (still `tz: Europe/Madrid`) and `SE4`, no GB |
| `index-v2.json` / `index-v1.json` | the market days 2026-10-04 and 2026-10-05; `res` 30 for `GB-C` |
| `GB-C_2026-10-04.json`, `GB-C_2026-10-05.json` | 48 half-hours each, a Paris day (23:00–23:00 London), `fx.GBP` 0.8712 |
| `GB-C_2026-03-29.json` | the spring clock change: 46 half-hours |
| `GB-C_2026-10-25.json`, `GB-C_2026-10-26.json` | the autumn clock change (50 half-hours) and the day after (48) |
| `PT_2026-10-04.json`, `PT_2026-10-05.json` | 96 quarters each on the Madrid calendar (`tz: Europe/Madrid`, as every v1 zone's file) |
