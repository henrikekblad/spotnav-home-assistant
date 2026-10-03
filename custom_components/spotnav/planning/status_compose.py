"""One status, composed here, worded by each client.

Pure: no Home Assistant imports. `compose_status(StatusFacts)` returns

    {"tone": "normal" | "notice" | "blocking",
     "lines": [{"code": "<stable code>", "params": {<name>: <typed fact>}}, ...]}

`lines[0]` is the headline; the other lines are facts shown with it. Params are typed facts and
never text: instants are offset-bearing ISO strings (clients format them in the market's zone),
energies are kWh, money is `{amount_minor, currency}` (the minor unit, an integer), currents are A,
percents are percent, distances are Swedish mil, and a list is a list of stable codes. `tone`
replaces every client-side red/neutral judgement: a banner is red only when this says `blocking`.

Precedence (first match wins the headline; "add" rows append a fact line)
---------------------------------------------------------------------------
1. BLOCKING. Any blocking condition replaces the whole block: every blocking line, in this order,
   nothing else; the block's tone is that of its strongest line. charger_unavailable,
   price_data_invalid, price_data_unavailable (no usable interval at all), settings_incomplete
   (tone `notice`: a charger still being set up is not a fault, so on its own it is not red),
   solar_unavailable, target_soc_unknown, price_horizon_missing, planning_unavailable,
   planning_error.
   Exception: when the charger is drawing current, one charging_now line follows them (the tone
   is unchanged).
2. paused (headline), then charging_now(until=None) when the charger is drawing anyway (manual
   Start under a pause).
3. Strategy headline when `strategy_state` exists (solar / hybrid), in place of 4.
4. Plan headline, the card's `statusSentence` chain:
     charging_without_prices (+plan_energy) > charging_now (+plan_energy +plan_cost) >
     held_until_window{time} (a charge that started by itself outside the plan was stopped, or a car
     waits for the next window) >
     waiting_for_history > waiting_for_publication > buying_before_publication > auto_planned (+energy +cost +distance) >
     auto_installed > proposal_pending (+energy +cost +distance) > waiting_for_tomorrow > no_plan
   `nothing_to_charge` takes no_plan's place when the plan says there is nothing to charge.
   Nothing matched (a plan that exists but says nothing) leaves `lines` empty: idle.
5. Target facts, right after the headline (never in a blocking block):
     target_reached{soc_percent, basis: reading|estimate, reading_age_s} while the stop record is
     current, i.e. from the stop until a new schedule or a manual Start clears it (the controller
     owns that lifetime; a cancel does not clear it); tone normal.
     target_unverifiable{reason: no_source|reading_unusable} while a plan with a target is
     enforced and no usable reading exists (no record then); tone notice, the target cannot be
     checked (the window still ends the charge). The record wins if both were ever present.
   HA always enforces a target; "this Home Assistant version won't stop at the target" is the
   app's own capability check, never said here.
   `settings_suggested{fields}` (tone normal) follows the notices while first-run defaults
   (area, phases, amps) are unconfirmed; any settings edit clears it.
6. Notices appended after the headline (and after the target fact): price_data_stale,
   price_data_degraded (usable rows exist, or degraded/incomplete), unpriced,
   hold_overridden (a person started the charge again after SpotNav held it, and it may go on),
   held_by_charger (the charger's own scheduler or load balancer holds the charge), charger_disabled
   (its own enable switch is off, so it cannot start), site_measurement_problem (the phases that
   make the site's measurement unusable and why), duplicate_charger (another entry is the same physical
   charger), load_balancing_limited, load_balancing_unavailable. Tone `notice` if any is present;
   otherwise `normal`. A proposal waiting for a window boundary is the normal line proposal_pending.

Rules chosen where the card and the app differ
----------------------------------------------
* Charging inside an installed period is `charging_now{until}`; charging outside any period is
  `charging_now{until: None}` (no end is invented). charging_without_prices still wins.
* Several lines only for facts that hold together (headline + plan figures + notices); a
  headline is never doubled.
* Comparing the app's local plan with HA's is not an HA fact and stays in the app.
* A pause takes the headline (nothing a plan says is true while execution is suspended).
  Load balancing limiting is a notice fact, only in active mode and while charging.
* A finished/empty plan says `nothing_to_charge`, not "no plan".
* `plan_cost` is `amount_minor` (major units x100, rounded).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from ..util import aware_iso


TONE_NORMAL: Final = "normal"
TONE_NOTICE: Final = "notice"
TONE_BLOCKING: Final = "blocking"

_RANK: Final = {TONE_NORMAL: 0, TONE_NOTICE: 1, TONE_BLOCKING: 2}

# Planning reasons that are normal life: never a blocking planning_unavailable.
QUIET_PLANNING_REASONS: Final = frozenset(
    {
        "no_prices_yet",
        "price_data_stale",
        "ready",
        "unpriced",
        "publication_pending",
        "waiting_for_history",
        "buying_before_publication",
        "charging_without_prices",
        "already_at_target",
        "shutdown",
        "solar_running",
    }
)

# Every status code, its tone and its param names; a test pins this table.
STATUS_CODES: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    # `problem` says why when the charge control is gone or disabled (`control_missing`,
    # `control_disabled`), with its `entity`; both are `None` for any other reason.
    # The first minutes after start: what is not yet known is not answered with a fallback. Alone.
    "starting_up": (TONE_NORMAL, ()),
    "charger_unavailable": (TONE_BLOCKING, ("problem", "entity")),
    "price_data_invalid": (TONE_BLOCKING, ("reason",)),
    "price_data_unavailable": (TONE_BLOCKING, ("reason",)),
    # Informational, not red: a new charger that still needs a price area is being set up, not broken.
    "settings_incomplete": (TONE_NOTICE, ("reason", "missing")),
    "solar_unavailable": (TONE_BLOCKING, ()),
    "target_soc_unknown": (TONE_BLOCKING, ("missing",)),
    "price_horizon_missing": (TONE_BLOCKING, ()),
    "planning_unavailable": (TONE_BLOCKING, ("reason",)),
    "planning_error": (TONE_BLOCKING, ("reason",)),
    "paused": (TONE_NORMAL, ("until", "choice")),
    "charging_now": (TONE_NORMAL, ("until",)),
    "charging_without_prices": (TONE_NOTICE, ()),
    "waiting_for_publication": (TONE_NORMAL, ("publication_at",)),
    # A dated departure leaves the unpublished hours for later because the same weekday-hours were
    # `percent` % cheaper over the last `weeks` weeks; `weekday` is ISO 1 (Monday) .. 7 (Sunday).
    "waiting_for_history": (TONE_NORMAL, ("weekday", "percent", "weeks")),
    "buying_before_publication": (TONE_NORMAL, ("kwh",)),
    "auto_planned": (TONE_NORMAL, ("start",)),
    "auto_installed": (TONE_NORMAL, ("start",)),
    # `installs_at` is the end of the Auto window charging now, the boundary a newer proposal waits
    # for, with `waits_for: "window_end"`; both are null when it waits for nothing of the kind.
    "proposal_pending": (TONE_NORMAL, ("installs_at", "waits_for")),
    "waiting_for_tomorrow": (TONE_NORMAL, ()),
    "no_plan": (TONE_NORMAL, ()),
    "nothing_to_charge": (TONE_NORMAL, ()),
    "plan_energy": (TONE_NORMAL, ("kwh",)),
    "plan_cost": (TONE_NORMAL, ("amount_minor", "currency")),
    "plan_distance": (TONE_NORMAL, ("mil",)),
    "solar_charging": (TONE_NORMAL, ("requested_a",)),
    "solar_arming": (TONE_NORMAL, ()),
    "solar_disarming": (TONE_NORMAL, ()),
    "solar_no_reading_stopped": (TONE_NORMAL, ()),
    "solar_no_reading_waiting": (TONE_NORMAL, ()),
    "solar_waiting_for_sun": (TONE_NORMAL, ()),
    "solar_unknown": (TONE_NORMAL, ()),
    "hybrid_grid": (TONE_NORMAL, ("grid_kwh", "credit_kwh", "window_start", "window_end")),
    "hybrid_no_forecast": (TONE_NORMAL, ()),
    "hybrid_no_price_data": (TONE_NORMAL, ()),
    "hybrid_satisfied": (TONE_NORMAL, ()),
    "hybrid_unknown": (TONE_NORMAL, ()),
    "settings_suggested": (TONE_NORMAL, ("fields",)),
    "target_reached": (TONE_NORMAL, ("soc_percent", "basis", "reading_age_s")),
    "target_unverifiable": (TONE_NOTICE, ("reason",)),
    "price_data_stale": (TONE_NOTICE, ("reason",)),
    "price_data_degraded": (TONE_NOTICE, ("reason",)),
    "unpriced": (TONE_NOTICE, ()),
    # `cause` names why where it is known: `battery_shares_fuse` (a home battery charging from the grid
    # shares the main fuse with the car) or `house_consumption`; `None` when it is not known.
    "load_balancing_limited": (TONE_NOTICE, ("limit_a", "phase", "cause")),
    "load_balancing_unavailable": (TONE_NOTICE, ()),
    # The charger's own scheduler, smart start or load balancer holds the charge (Easee): nothing is
    # wrong, and nothing SpotNav sends releases it.
    "held_by_charger": (TONE_NOTICE, ()),
    # The charger's own enable switch is off (Easee's `is_enabled`): it cannot start, whatever is sent.
    "charger_disabled": (TONE_NOTICE, ()),
    # A charge that started by itself outside every planned window was stopped (or a car waits): it
    # starts at `time`, the next window's start.
    "held_until_window": (TONE_NORMAL, ("time",)),
    # A person started the charge again after that stop: the plan is overridden and it may go on.
    "hold_overridden": (TONE_NOTICE, ()),
    # The site's measurement is unusable: `no_value_phases` (L1/L2/L3) read nothing usable, from
    # `no_value_entities`; `stale_phases` are older than `max_age_s`.
    "site_measurement_problem": (
        TONE_NOTICE,
        ("no_value_phases", "no_value_entities", "stale_phases", "max_age_s"),
    ),
    # Another SpotNav charger entry, titled `other`, is the same physical charger as this one.
    "duplicate_charger": (TONE_NOTICE, ("other",)),
}

STATUS_TONES: Final = (TONE_NORMAL, TONE_NOTICE, TONE_BLOCKING)


@dataclass(frozen=True, slots=True)
class PlanningFacts:
    state: str
    reason: str
    missing: tuple[str, ...] = ()
    publication_at: datetime | None = None
    must_buy_now_kwh: float | None = None
    #: `waiting_for_history` only: the weekday (ISO 1..7), whole percent and weeks of history behind the wait.
    history_weekday: int | None = None
    history_percent: int | None = None
    history_weeks: int | None = None


@dataclass(frozen=True, slots=True)
class ProposalFacts:
    periods: tuple[tuple[datetime, datetime], ...]
    identity: str | None = None
    planned_kwh: float | None = None
    requested_kwh: float | None = None
    #: Major units, as the planner states it; the composer converts to the minor unit.
    cost: float | None = None
    currency: str | None = None
    distance_mil: float | None = None
    unpriced: bool | None = None


@dataclass(frozen=True, slots=True)
class SolarFacts:
    state: str
    reason: str | None = None
    requested_a: float | None = None


@dataclass(frozen=True, slots=True)
class HybridFacts:
    reason: str | None = None
    grid_kwh: float | None = None
    credit_kwh: float | None = None
    forecast_configured: bool = False


@dataclass(frozen=True, slots=True)
class SocFacts:
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetFacts:
    #: The current stop record (a charge that ended because its target was reached).
    stop_soc_percent: float | None = None
    stop_basis: str | None = None
    stop_reading_age_s: float | None = None
    #: With no record: why a plan's target cannot be checked right now (`no_source` or
    #: `reading_unusable`), else `None`.
    unverifiable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LoadBalancingFacts:
    state: str
    active_control_enabled: bool
    proposed_current_a: float | None = None
    limiting_phase: str | None = None
    #: Why load balancing holds the car below its plan, where known (see `load_balancing_limited`).
    cause: str | None = None


@dataclass(frozen=True, slots=True)
class SiteMeasurementFacts:
    """Why the site's measurement is unusable: the phases with no usable value (and the entities they
    are read from) and the phases whose value is older than `max_age_s`."""

    no_value_phases: tuple[str, ...] = ()
    no_value_entities: tuple[str, ...] = ()
    stale_phases: tuple[str, ...] = ()
    max_age_s: float | None = None


@dataclass(frozen=True, slots=True)
class StatusFacts:
    now: datetime
    charger_available: bool = True
    charger_problem: str | None = None
    charger_problem_entity: str | None = None
    #: A settings record exists (Auto is configured for this charger).
    has_settings: bool = False
    #: Settings filled in by first-run defaults (`area`, `phases`, `amps`) that no edit has confirmed.
    suggested: tuple[str, ...] = ()
    strategy: str | None = None
    departure_enabled: bool = False
    planning: PlanningFacts | None = None
    price_state: str | None = None
    price_reason: str | None = None
    usable_price_rows: int = 0
    waiting_for_tomorrow: bool | None = None
    prices_unpriced: bool | None = None
    charging: bool = False
    #: The charger itself reports that its own scheduler or load balancer holds the charge.
    held_by_charger: bool = False
    #: The charger's own enable switch is off, so it cannot start.
    charger_disabled: bool = False
    #: The next window's start while a charge is held back for it (`ChargingController.hold_until`).
    hold_until: datetime | None = None
    #: A person started the charge again after the hold and it is allowed to continue.
    hold_overridden: bool = False
    paused: bool = False
    pause_until: datetime | None = None
    pause_choice: str | None = None
    installed_periods: tuple[tuple[datetime, datetime], ...] = ()
    proposal: ProposalFacts | None = None
    relation_applied: bool | None = None
    pending_identity: str | None = None
    solar: SolarFacts | None = None
    hybrid: HybridFacts | None = None
    soc: SocFacts | None = None
    #: The charger advertises load balancing, and its site summary.
    load_balancing_capable: bool = False
    load_balancing: LoadBalancingFacts | None = None
    target: TargetFacts | None = None
    #: The site's measurement problem, `None` while it is healthy or there is no site.
    site_measurement: SiteMeasurementFacts | None = None
    #: Titles of the other charger entries that are this same physical charger.
    duplicate_chargers: tuple[str, ...] = ()
    #: Right after the integration loaded and a source is still awaited (`startup.py`).
    starting_up: bool = False


def _line(code: str, **params: Any) -> dict[str, Any]:
    names = STATUS_CODES[code][1]
    assert set(params) <= set(names), (code, params)
    return {"code": code, "params": {name: params.get(name) for name in names}}


def _tone_of(line: dict[str, Any]) -> str:
    return STATUS_CODES[line["code"]][0]


def _blocking_lines(facts: StatusFacts) -> list[dict[str, Any]]:
    """The card's `issuesFor` blocking branches, in its order."""
    lines: list[dict[str, Any]] = []
    planning = facts.planning
    if not facts.charger_available:
        lines.append(
            _line(
                "charger_unavailable",
                problem=facts.charger_problem,
                entity=facts.charger_problem_entity,
            )
        )
    if facts.has_settings:
        if facts.price_state == "invalid":
            lines.append(_line("price_data_invalid", reason=facts.price_reason))
        elif facts.price_state == "unavailable" and facts.usable_price_rows == 0:
            lines.append(_line("price_data_unavailable", reason=facts.price_reason))
    if planning is None:
        return lines
    if planning.state == "incomplete_settings":
        lines.append(
            _line(
                "settings_incomplete",
                reason=planning.reason,
                missing=[name for name in planning.missing if name],
            )
        )
    elif planning.state == "planning_unavailable":
        reason = planning.reason
        if facts.strategy == "solar" and reason == "solar_running":
            pass
        elif facts.strategy == "solar" and reason == "solar_execution_unavailable":
            lines.append(_line("solar_unavailable"))
        elif _normal_horizon_gap(facts):
            pass
        elif reason in QUIET_PLANNING_REASONS:
            pass
        elif reason in ("target_soc_unknown", "target_capacity_unknown"):
            lines.append(_line("target_soc_unknown", missing=_soc_missing(facts, reason)))
        elif reason == "insufficient_price_horizon":
            lines.append(_line("price_horizon_missing"))
        else:
            lines.append(_line("planning_unavailable", reason=reason))
    elif planning.state == "error":
        lines.append(_line("planning_error", reason=planning.reason))
    return lines


def _soc_missing(facts: StatusFacts, reason: str) -> list[str]:
    if facts.soc is not None:
        named = [name for name in facts.soc.missing if name in ("soc", "capacity")]
        if named:
            return named
    return ["capacity"] if reason == "target_capacity_unknown" else ["soc"]


def _normal_horizon_gap(facts: StatusFacts) -> bool:
    """The card's `isNormalHorizonGap`: tomorrow is only unpublished, nothing is wrong."""
    planning = facts.planning
    if planning is None or planning.reason != "insufficient_price_horizon":
        return False
    if not (facts.waiting_for_tomorrow is True or facts.price_state == "waiting_for_tomorrow"):
        return False
    unfinished = any(end > facts.now for _, end in facts.installed_periods)
    return unfinished or not facts.departure_enabled


def _plan_facts(
    proposal: ProposalFacts | None, *, energy: bool, cost: bool, distance: bool
) -> list[dict[str, Any]]:
    """The compound facts a headline may carry: what the proposal reported, never "unknown"."""
    if proposal is None:
        return []
    lines: list[dict[str, Any]] = []
    kwh = proposal.planned_kwh if proposal.planned_kwh is not None else proposal.requested_kwh
    if energy and kwh is not None:
        lines.append(_line("plan_energy", kwh=round(kwh, 3)))
    if cost and proposal.cost is not None and proposal.currency is not None:
        lines.append(
            _line(
                "plan_cost",
                amount_minor=int(round(proposal.cost * 100)),
                currency=proposal.currency,
            )
        )
    if distance and proposal.distance_mil is not None:
        lines.append(_line("plan_distance", mil=round(proposal.distance_mil, 3)))
    return lines


def _solar_line(solar: SolarFacts) -> dict[str, Any]:
    if solar.state == "on":
        return _line("solar_charging", requested_a=solar.requested_a)
    if solar.state == "arming":
        return _line("solar_arming")
    if solar.state == "disarming":
        return _line("solar_disarming")
    if solar.state == "off":
        if solar.reason == "no_basis_stopped":
            return _line("solar_no_reading_stopped")
        if solar.reason == "no_basis_off":
            return _line("solar_no_reading_waiting")
        return _line("solar_waiting_for_sun")
    return _line("solar_unknown")


def _hybrid_line(hybrid: HybridFacts, proposal: ProposalFacts | None) -> dict[str, Any]:
    if hybrid.reason == "no_forecast_cheapest":
        return _line("hybrid_no_forecast")
    if hybrid.reason == "no_price_data":
        return _line("hybrid_no_price_data")
    if hybrid.reason == "satisfied_need_met":
        return _line("hybrid_satisfied")
    if hybrid.grid_kwh is None:
        return _line("hybrid_unknown")
    periods = () if proposal is None else proposal.periods
    credit = hybrid.credit_kwh if hybrid.credit_kwh is not None and hybrid.credit_kwh > 0 else None
    return _line(
        "hybrid_grid",
        grid_kwh=round(hybrid.grid_kwh, 3),
        credit_kwh=None if credit is None else round(credit, 3),
        window_start=aware_iso(periods[0][0]) if periods else None,
        window_end=aware_iso(periods[-1][1]) if periods else None,
    )


def _plan_headline(facts: StatusFacts) -> list[dict[str, Any]]:
    """The card's `statusSentence` chain, with the compound facts as their own lines."""
    now = facts.now
    planning = facts.planning
    proposal = facts.proposal
    installed = facts.installed_periods
    proposed = () if proposal is None else proposal.periods
    active = next((span for span in installed if span[0] <= now < span[1]), None)
    upcoming = next((span for span in installed if span[1] > now), None)

    if planning is not None and planning.reason == "charging_without_prices":
        return [_line("charging_without_prices"), *_plan_facts(proposal, energy=True, cost=False, distance=False)]
    if facts.charging and active is not None:
        return [
            _line("charging_now", until=aware_iso(active[1])),
            *_plan_facts(proposal, energy=True, cost=True, distance=False),
        ]
    if facts.charging:
        return [_line("charging_now", until=None)]
    if facts.hold_until is not None:
        return [_line("held_until_window", time=aware_iso(facts.hold_until))]
    if planning is not None and planning.reason == "waiting_for_history":
        return [
            _line(
                "waiting_for_history",
                weekday=planning.history_weekday,
                percent=planning.history_percent,
                weeks=planning.history_weeks,
            )
        ]
    if planning is not None and planning.state == "waiting_for_publication":
        return [_line("waiting_for_publication", publication_at=aware_iso(planning.publication_at))]
    if (
        planning is not None
        and planning.reason == "buying_before_publication"
        and planning.must_buy_now_kwh is not None
    ):
        return [_line("buying_before_publication", kwh=round(planning.must_buy_now_kwh, 3))]
    planned = None
    if proposal is not None:
        planned = proposal.planned_kwh if proposal.planned_kwh is not None else proposal.requested_kwh
    if facts.relation_applied is True and proposed and planned is not None:
        return [
            _line("auto_planned", start=aware_iso(proposed[0][0])),
            *_plan_facts(proposal, energy=True, cost=True, distance=True),
        ]
    if installed:
        return [_line("auto_installed", start=aware_iso((upcoming or installed[0])[0]))]
    if facts.relation_applied is False and proposed:
        return [
            _line("proposal_pending", installs_at=None, waits_for=None),
            *_plan_facts(proposal, energy=True, cost=True, distance=True),
        ]
    if facts.waiting_for_tomorrow is True:
        return [_line("waiting_for_tomorrow")]
    if not proposed and not installed:
        if planning is not None and planning.state == "nothing_to_charge":
            return [_line("nothing_to_charge")]
        return [_line("no_plan")]
    return []


def _notices(facts: StatusFacts) -> list[dict[str, Any]]:
    """The card's degraded issues that are facts beside a headline (not the pending proposal)."""
    lines: list[dict[str, Any]] = []
    if facts.has_settings:
        if facts.price_state == "stale":
            lines.append(_line("price_data_stale", reason=facts.price_reason))
        elif facts.price_state in ("degraded", "incomplete") or (
            facts.price_state == "unavailable" and facts.usable_price_rows > 0
        ):
            lines.append(_line("price_data_degraded", reason=facts.price_reason))
    planning = facts.planning
    proposal = facts.proposal
    if not (planning is not None and planning.reason == "charging_without_prices") and (
        facts.prices_unpriced is True
        or (proposal is not None and proposal.unpriced is True)
    ):
        lines.append(_line("unpriced"))
    if facts.hold_overridden and facts.charging:
        lines.append(_line("hold_overridden"))
    if facts.held_by_charger and not facts.charging:
        lines.append(_line("held_by_charger"))
    if facts.charger_disabled and not facts.charging:
        lines.append(_line("charger_disabled"))
    measurement = facts.site_measurement
    if measurement is not None and (measurement.no_value_phases or measurement.stale_phases):
        lines.append(
            _line(
                "site_measurement_problem",
                no_value_phases=list(measurement.no_value_phases),
                no_value_entities=list(measurement.no_value_entities),
                stale_phases=list(measurement.stale_phases),
                max_age_s=measurement.max_age_s,
            )
        )
    for title in facts.duplicate_chargers:
        lines.append(_line("duplicate_charger", other=title))
    site = facts.load_balancing
    if site is not None:
        if (
            (site.state == "capacity_limited" or site.cause is not None)
            and site.active_control_enabled
            and facts.charging
        ):
            lines.append(
                _line(
                    "load_balancing_limited",
                    limit_a=site.proposed_current_a,
                    phase=site.limiting_phase,
                    cause=site.cause,
                )
            )
    elif facts.load_balancing_capable:
        lines.append(_line("load_balancing_unavailable"))
    return lines


def _target_lines(facts: StatusFacts) -> list[dict[str, Any]]:
    target = facts.target
    if target is None:
        return []
    if target.stop_soc_percent is not None:
        age = target.stop_reading_age_s
        return [
            _line(
                "target_reached",
                soc_percent=round(target.stop_soc_percent, 1),
                basis="estimate" if target.stop_basis == "estimate" else "reading",
                reading_age_s=None if age is None else round(max(age, 0.0)),
            )
        ]
    if target.unverifiable_reason is not None:
        return [_line("target_unverifiable", reason=target.unverifiable_reason)]
    return []


def _pending_proposal(facts: StatusFacts) -> bool:
    """This very proposal is queued for a window boundary (Auto never cuts a charging window short)."""
    proposal = facts.proposal
    return (
        facts.relation_applied is False
        and proposal is not None
        and proposal.identity is not None
        and facts.pending_identity == proposal.identity
        and bool(proposal.periods)
    )


def _pending_line(facts: StatusFacts) -> dict[str, Any]:
    """A normal line: the queued proposal and, when a window is open, the end it installs at.

    The same fact Auto's `window_charging_now` uses: the installed plan's window that contains now.
    """
    active = next((span for span in facts.installed_periods if span[0] <= facts.now < span[1]), None)
    if active is None:
        return _line("proposal_pending", installs_at=None, waits_for=None)
    return _line("proposal_pending", installs_at=aware_iso(active[1]), waits_for="window_end")


def compose_status(facts: StatusFacts) -> dict[str, Any]:
    """The whole status block for one moment, from typed facts only."""
    if facts.starting_up:
        return {"tone": TONE_NORMAL, "lines": [_line("starting_up")]}
    blocking = _blocking_lines(facts)
    if blocking:
        # A charging car is always shown, even when something else blocks planning:
        # one `charging_now` line (end from an active installed period, else null).
        if facts.charging:
            active = next(
                (span for span in facts.installed_periods if span[0] <= facts.now < span[1]), None
            )
            blocking.append(_line("charging_now", until=None if active is None else aware_iso(active[1])))
        # The block's tone is its strongest line: missing settings alone read as setup, not as a fault.
        tone = TONE_NOTICE
        for line in blocking:
            if _RANK[_tone_of(line)] > _RANK[tone]:
                tone = _tone_of(line)
        return {"tone": tone, "lines": blocking}

    lines: list[dict[str, Any]] = []
    if facts.paused:
        lines.append(_line("paused", until=aware_iso(facts.pause_until), choice=facts.pause_choice))
        if facts.charging:
            lines.append(_line("charging_now", until=None))
    elif facts.solar is not None:
        lines.append(_solar_line(facts.solar))
    elif facts.hybrid is not None:
        lines.append(_hybrid_line(facts.hybrid, facts.proposal))
    else:
        lines.extend(_plan_headline(facts))
    if _pending_proposal(facts) and not any(line["code"] == "proposal_pending" for line in lines):
        lines.append(_pending_line(facts))
    lines.extend(_target_lines(facts))
    lines.extend(_notices(facts))
    if facts.suggested:
        lines.append(_line("settings_suggested", fields=list(facts.suggested)))

    tone = TONE_NORMAL
    for line in lines:
        if _RANK[_tone_of(line)] > _RANK[tone]:
            tone = _tone_of(line)
    return {"tone": tone, "lines": lines}
