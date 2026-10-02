# Relay fixtures

Sanitized, hand-checked stand-ins for the deployed SpotNav Relay `/v1` contract.
Nothing here is downloaded at test time and nothing here carries a credential, a
webhook id, or a URL that is not public.

## What revision these represent

The contract is `v: 1` as documented in `spotnav-relay`'s `docs/format.md` and as
served by `https://spotnav.sensnology.se` when these were captured. The field names are the deployed ones, exactly:

| document | fields |
|---|---|
| `areas.json` | `v`, `generated`, `areas[]` — `id`, `eic`, `countries`, `name`, `tz`, `currency`, `major_unit`, `minor_unit`, and the optional `vat_percent`, `suggested_tax`, `suggested_grid_fee` |
| `index.json` | `v`, `generated`, `res_default`, `areas_rev`, `areas{id: {days[], res?}}` |
| `<area>/profile.json` | `v`, `area`, `tz`, `unit`, `generated`, `from`, `to`, `weeks`, `hours[]` — `weekday` (ISO 1..7), `hour` (local 0..23), `median`, `std`, `n` |
| day document | `v`, `area`, `date`, `tz`, `start`, `res`, `unit`, `prices[]`, `fx`, `fx_date`, `fx_src`, `src`, `published`, `retrieved` |

No field is invented because a client would find it convenient. Where the relay
leaves a field out, the fixture leaves it out.

## The valid documents

* `day_SE4_2026-09-22_96.json` — an ordinary 96-quarter day, **kept exactly as the
  relay wrote it** (a real response, prices included).
* `day_SE4_2026-03-29_92.json` — a 23-hour spring day: 92 quarters, and the last
  interval ends at 23:00 in the *new* offset.
* `day_SE4_2025-10-26_100.json` — a 25-hour autumn day: 100 quarters, with
  02:00–02:59 drawn twice.
* `day_SE4_2026-05-24_negative.json` — prices below zero, which is a price and
  not an error.
* `day_SE4_2025-09-30_hourly.json` — `res: 60`, 24 intervals: the Swedish areas
  were hourly until 2025-10-01, and the relay publishes what the market has.
* `day_SE4_2026-09-21_incomplete.json` — the current day published only as far as
  it goes. Structurally valid, and *accepted*: a day that stops early is a
  publication state, not a parse error.
* `index.json` — today and tomorrow for `SE4` and `DE-LU`, today only for `NO1`.
* `areas.json` — four money identities, and the fiscal figures in every state the
  contract has: `SE4` (SEK/`kr`/`öre`, tax 36 and grid fee 30), `DE-LU`
  (EUR/`€`/`cent`, all three absent), `NO1` (NOK/`kr`/`öre`, **grid fee present
  as zero**), `DK1` (DKK/`kr`/`øre`, **tax present as zero**, grid fee absent).
  Present zero and absent are different facts and are covered as such.

## The invalid documents

One field is wrong in each, and the rest is a valid day, so a test failure names
the rule that broke:

* version, area, date, `unit` (a currency that is not EUR), and an index at a
  version this client does not know;
* `res: 30` and `res: 0` (the relay publishes 15 or 60, and anything else is
  refused rather than coerced);
* a `start` that is not the first instant of the document's own local date, and a
  `start` with no offset at all;
* a boolean, a numeric *string*, `NaN` and `Infinity` where a price must be a
  finite JSON number (`NaN`/`Infinity` are what Python's own `json` will happily
  read out of a hostile or broken proxy body, so the parser refuses them by
  name rather than trusting `float()`);
* a zero FX rate and a malformed FX date;
* an index whose `days` are unsorted or repeated, and an index area with no `res`.

### About "duplicate interval" and "broken interval order"

A day document's intervals are **positional**: element *n* starts at
`start + n × res`, there are no per-element timestamps, and per the format's own
documentation there never will be. A duplicate or out-of-order interval is
therefore not expressible on the wire — that is the design's point — so the
parser's interval rules (uniqueness, ordering, positive duration, coherent
coverage) are exercised two ways instead:

* through the wire, by the fixtures that *can* break the geometry —
  `day_SE4_bad_start_offset.json` misaligns every position by one step, and the
  `res` variants break the step itself;
* directly on the parsed model, in `tests/test_relay_contract.py`, which builds
  interval lists by hand to prove the validators reject a duplicate and an
  inversion. That is a test of the validator, not of a wire case, and it is named
  as such.

* `profile_SE4.json` — a history profile (4 weeks ending 2026-10-01): Sunday 00:00-07:59 at
  0.03 EUR/kWh and Thursday 18:00-19:59 at 0.10, every other weekday-hour omitted as the relay
  does below 8 samples. Built to the "Profile contract (relay -> HA), v1".
