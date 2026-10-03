# Planner scenario fixtures

`scenarios.json` is the behavioural contract of the Auto-price planner
(`custom_components/spotnav/planner.py`). Every scenario is one input and
the result it must produce, in plain JSON: ISO timestamps, numbers, booleans,
strings. Nothing language-specific is in the file.

Where a reference implementation plans from a *short price list* and this planner plans from
whole published days, the day is filled with a price of `100.0` that can never be selected,
so the optimum is unchanged. Four of a day's slots are the interesting ones; the rest
cannot win.

## Schema

Top level: `schema` (name), `schema_version` (1), `contract_version` (must match
the planner's), `tolerance` (the float tolerance every approximate assertion
uses), `provenance`, `scenarios`.

Each scenario: `name`, `documents` (relay day documents, as published — each is
parsed by `relay_contract.parse_day`, so a scenario cannot describe a document the
relay would not publish), `request` (the planner's inputs), `expect`, and an
optional `note` saying what it pins.

`expect` keys, and how each is checked:

| key | meaning | check |
|---|---|---|
| `reason` | the named no-plan reason, or `null` for a plan | exact |
| `slots_needed`, `priced_slots`, `unpriced_slots`, `period_count` | counts | exact |
| `unpriced` | whether the plan charges without published prices | exact |
| `currency`, `major_unit`, `minor_unit` | result identity | exact |
| `periods` | ordered half-open `[start, end)` pairs, as ISO instants | exact |
| `slot_starts` | the selected slots' starts, in order | exact |
| `local_hours`, `local_offsets` | each selected slot's area-local hour / UTC offset | exact |
| `slot_source_days` | which published day each selected slot's price came from | exact |
| `requested_kwh`, `power_kw`, `delivered_kwh`, `distance_mil`, `estimated_cost` | the result's numbers | float, within `tolerance` |
| `local_major_per_kwhs`, `effective_minor_per_kwhs` | per selected slot, in order | float, within `tolerance` |

The validator in `tests/test_planner.py` refuses an unknown key anywhere in
`expect` (and in the top level, the scenario and the request), so a misspelled or
invented field cannot make a scenario pass vacuously — it fails loudly instead.

## The scenarios

| group | scenarios |
|---|---|
| reference-implementation parity cases | `parity_cheapest_contiguous_before_departure`, `estimated_late_night_future_is_retired`, `parity_two_non_adjacent_runs` |
| power and whole-slot overdelivery | `power_single_phase_whole_slot_overdelivery`, `power_three_phase` |
| negative prices | `negative_prices_pay_rather_than_cost` |
| fiscal order | `fiscal_tax_transfer_then_vat` |
| EUR and non-EUR identity | `eur_area_needs_no_rate_table`, `non_eur_area_uses_the_document_rate`, `non_eur_area_without_a_rate_is_refused` |
| ties and period caps | `equal_prices_choose_the_earlier_slot` (its two slots are the only cheapest pair, so it is no tie), `period_cap_of_one_forces_one_run`, `greedy_selection_would_be_wrong` |
| horizon and departures | `no_departure_uses_the_24_hour_horizon`, `departure_crossing_midnight`, `deadline_too_short` |
| hourly input, DST days, overlap | `hourly_document_expands_to_quarters`, `spring_day_92_slots_skips_the_missing_hour`, `autumn_day_100_slots_repeats_the_hour`, `overlapping_documents_deduplicate_by_instant` |
| missing prices (the estimated fallback is retired) | `missing_tomorrow_is_a_named_gap`, `missing_tomorrow_at_the_last_published_slot`, `no_published_prices_at_all` |

Target-SoC energy resolution is a pure function of its own inputs rather than a
document scenario, so it is pinned directly in `tests/test_planner.py`.

## Targeted goldens, global invariants

A scenario here is deliberately **focused**: it pins the behaviour its name describes, and
may state only the fields it needs for that. It does not restate its whole result, and
nothing fills missing fields from the implementation's output to satisfy a completeness
check. The structural properties that must hold for *every* plan — ordering, grid
alignment, period merging, the period cap, the deadline, the count identities, the energy,
cost and distance arithmetic, input immutability and repeated-run equality — are asserted
for all successful scenarios in `tests/test_planner_invariants.py`. That split is the
principle: a golden pins a specific decision, an invariant pins a property.

## Departure, in the area's own timezone

The departure is a **real area-local wall time**, resolved in the area's declared IANA
zone, never a fixed offset. On 2026-03-29, Stockholm's 03:30 is `01:30Z`; a fixed-offset
reading would make it 04:30 local, an hour late. The policies, each with its own test:

- the wall time is built on the local calendar date containing `now`;
- if that instant is not strictly after the first usable slot, the same wall time is
  resolved on the **next local calendar date** — a calendar day, not `+24` elapsed hours,
  which differ by an hour on both transition days;
- an unambiguous wall time maps to its only instant;
- an **ambiguous** wall time (the autumn night's repeated hour) maps to the *earlier*
  occurrence: "ready by 02:30" is the conservative reading and the only one that cannot
  arrive late;
- a **nonexistent** wall time (the spring gap) advances to the first valid instant after
  the gap.

The first usable quarter-hour and the 24-hour candidate horizon stay ordinary elapsed-time
instant arithmetic; only the user's departure needs calendar-zone resolution.

## Equal cost is broken by time, toward the latest slots

The dynamic program compares `(cost, latest-first indices)` lexicographically: lower cost
first, and on exactly equal cost the chronologically latest set of slots (compared from the
last chosen slot backwards), both when retaining a state and when choosing the winner. The
car then charges as late as the prices allow and leaves with the freshest charge. Insertion-ordered
maps plus "replace only when strictly cheaper" do not by themselves guarantee the latest plan;
no epsilon is used, so genuinely different costs are never tied. (Earlier versions chose the earliest
set; a reader that ports the planner must break ties the same way to match the scenarios.)

## Where the expected values come from

Golden expectations are derived from the documented rules, not read back from the
implementation. Where a tie decides the answer, the expectation states which side of the
tie it is on and why.
