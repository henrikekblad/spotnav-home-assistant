# Profile parity fixtures

Each file is one case of `spotnav-relay/internal/profile/profile_test.go` (plus two more), written
by the relay's own `profile.BuildZones`, unmodified, from a small Go program that imports it:

| key | what |
|---|---|
| `area`, `tz`, `market_tz` | the area, its display zone and its market calendar |
| `now` | the instant the profile was built at |
| `days` | the market-day files given to the build, keyed by market date, in the `"v": 1` day shape |
| `profile` | the relay's `profile.json` for them, or `null` when it publishes none |

`tests/test_price_profile_build.py` parses every day with `parse_day`, builds with
`pricing/price_profile.build_profile` and requires the relay's document to the last decimal.

The cases: `normal_week`, `median_and_population_std`, `seven_days_missing` (21 days, published),
`eight_days_missing` (20, none), `outside_the_window` (old days and tomorrow's only, none),
`today_in_the_window`, `hourly_below_threshold` (every group has 4 values, all dropped),
`two_fridays_at_threshold` (Friday groups at exactly 8), `dst_autumn`, `dst_spring`,
`split_calendar_gb` (Paris files read in London, half-hours), and two not in the relay's tests:
`varied_prices_dst_autumn` (five-decimal prices, negatives, an autumn change, a `now` with seconds),
`split_calendar_pt` (Madrid files read in Lisbon, `now` on different dates in the two zones).

To regenerate: a Go module named under `github.com/henrikekblad/spotnav-relay/` (so it may import
`internal/profile`) with a `replace` to the relay checkout, building each case's files the way
`profile_test.go`'s `day` helper does and writing `BuildZones`' result next to them.
