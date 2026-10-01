"""Safe discovery of likely phase-measurement-source candidates.

Scans the entity registry for entities that plausibly carry a per-phase current
measurement, from live state and, failing that, Recorder history, and returns
ranked *suggestions*. A caller (the config flow) must show them for explicit
confirmation; nothing here writes a config entry or calls a service.

* History fallback: an OCPP charger only exposes its `L1`/`L2`/`L3` attributes
  during a session, so an idle one looks empty. A history-only candidate is
  still proposed but carries `REASON_ATTRIBUTES_HISTORICAL_MATCH`, one
  confidence tier lower, and `DiscoveryCandidate.observed_at`.
* Device membership is the only thing separating a charger's sensor from a site
  meter's, so every lookup (live or historical) is scoped to `only_device_id`
  or excludes `excluded_device_ids` before any state is read.
* Generic: only device class, unit, attribute shape and device membership are
  used, never a brand or integration name.
* Every attributes-based candidate must be usable: it is proposed only if the
  parent entity's unit validates and can be stored as `attribute_unit_override`
  (see `_score_attribute_candidate`); a parent unit is never trusted implicitly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import DATA_INSTANCE, get_instance
from homeassistant.util import dt as dt_util

from ..execution.charger_entities import EntityMatcher
from ..execution.charger_profiles import profile_for
from ..site.measurement_source import PhaseMeasurementSource
from ..site.site_capacity import normalized_ampere_unit, PhaseName, PHASES


_LOGGER = logging.getLogger(__name__)

SourceType = Literal["site_current", "charger_current"]
Confidence = Literal["high", "medium", "low"]

# Stable, translatable reason codes (never free text), one per scoring rule.
REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH = "attributes_device_class_and_unit_match"
REASON_ATTRIBUTES_UNIT_MATCH_ONLY = "attributes_unit_match_only"
REASON_ATTRIBUTES_HISTORICAL_MATCH = "attributes_historical_match"
REASON_SEPARATE_ENTITIES_DEVICE_CLASS_AND_UNIT_MATCH = "separate_entities_device_class_and_unit_match"
REASON_SEPARATE_ENTITIES_DEVICE_CLASS_MATCH_ONLY = "separate_entities_device_class_match_only"
REASON_SEPARATE_ENTITIES_UNIT_MATCH_ONLY = "separate_entities_unit_match_only"
REASON_SEPARATE_ENTITIES_NAME_MATCH_ONLY = "separate_entities_name_match_only"
REASON_SEPARATE_ENTITIES_PROFILE_MATCH = "separate_entities_profile_match"
REASON_POSSIBLE_INVERTER_OUTPUT = "possible_inverter_output_not_confirmed_as_grid_input"

# How far back Recorder history is searched. An upper bound only; a shorter
# window can only miss a candidate, never produce a wrong one. Kept short because
# the query reads every row with attributes for the entity in the window.
_HISTORY_LOOKBACK_DAYS = 2

# A history read that takes longer than this is abandoned: discovery then offers
# live candidates only, so a config flow never hangs on a large database.
_HISTORY_TIMEOUT_SECONDS = 10

# Attributes every sensor state carries. An entity with nothing beyond these can
# never have had phase attributes, so its history is not worth reading.
_STANDARD_ATTRIBUTES = frozenset(
    {
        "unit_of_measurement",
        "device_class",
        "state_class",
        "friendly_name",
        "icon",
        "attribution",
        "supported_features",
        "entity_picture",
        "options",
        "suggested_display_precision",
    }
)

# A historical-only match is reported one tier below the equivalent live match.
_HISTORICAL_CONFIDENCE: dict[Confidence, Confidence] = {
    "high": "medium",
    "medium": "low",
    "low": "low",
}

_CURRENT_DEVICE_CLASS = "current"
_INVERTER_WORDS = ("inverter", "pv", "solar")
_GRID_WORD = "grid"

# Name words for a different physical quantity: a name containing one is never
# a phase's current value, even with a phase indicator ("voltage_l1").
_QUANTITY_CONFLICT_WORDS = frozenset(
    {
        "voltage",
        "volt",
        "volts",
        "power",
        "watt",
        "watts",
        "kw",
        "kwh",
        "wh",
        "energy",
        "frequency",
        "freq",
        "hz",
        "hertz",
        "reactive",
        "var",
        "vars",
        "apparent",
        "va",
    }
)

# Words marking a name as specifically about current (tier 1); see `_attribute_name_tier`.
_CURRENT_QUALIFIER_WORDS = frozenset({"current", "amps", "amp", "ampere", "amperes"})

# Phase indicators recognized in ids, names and attribute names; not only
# "L1"/"L2"/"L3", since some meters expose "Phase A/B/C".
_PHASE_TOKEN_TO_PHASE: dict[str, PhaseName] = {
    "l1": "L1",
    "l2": "L2",
    "l3": "L3",
}
_PHASE_LETTER_TO_PHASE: dict[str, PhaseName] = {"a": "L1", "b": "L2", "c": "L3"}
_PHASE_NUMBER_TO_PHASE: dict[str, PhaseName] = {"1": "L1", "2": "L2", "3": "L3"}
_PHASE_MARKER_TOKENS = frozenset(_PHASE_TOKEN_TO_PHASE) | {"phase"} | frozenset(
    _PHASE_LETTER_TO_PHASE
) | frozenset(_PHASE_NUMBER_TO_PHASE)


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    """One ranked, suggested (never applied) measurement-source candidate.

    `observed_at` is `None` for a live match, else the `last_updated` of the
    historical record the phase attributes were found in.
    """

    candidate_id: str
    source_type: SourceType
    mapping: PhaseMeasurementSource
    confidence: Confidence
    reason_code: str
    observed_at: datetime | None = None


def discover_site_current_sources(
    hass: HomeAssistant, *, excluded_device_ids: set[str] | None = None
) -> list[DiscoveryCandidate]:
    """Rank candidates for the site's total grid/incoming current, from live state only.

    `excluded_device_ids` should be every charger's device, so a charger is never
    suggested as the site total. This is the synchronous half of
    `async_discover_site_current_sources`, which adds the history fallback.
    """
    excluded = excluded_device_ids or set()
    candidates = _discover_attribute_candidates(hass, "site_current", excluded_device_ids=excluded)
    candidates += _discover_separate_entity_candidates(hass, "site_current", excluded_device_ids=excluded)
    return _ranked(candidates)


async def async_discover_site_current_sources(
    hass: HomeAssistant, *, excluded_device_ids: set[str] | None = None
) -> list[DiscoveryCandidate]:
    """Site-current candidates from live state, never from Recorder history.

    The site's own measurement is live by nature, and a grid meter can update every
    second, so its history is both unnecessary and potentially enormous. Async only
    to keep the call shape shared with the charger variant.
    """
    return discover_site_current_sources(hass, excluded_device_ids=excluded_device_ids)


def discover_charger_current_sources(
    hass: HomeAssistant, *, charger_device_id: str | None
) -> list[DiscoveryCandidate]:
    """Rank candidates for one charger's own measured current, from live state only.

    Scoped strictly to `charger_device_id`, so it never suggests another charger's
    or the site's sensors. An unresolvable device yields an empty list.
    """
    if not charger_device_id:
        return []
    candidates = _discover_attribute_candidates(hass, "charger_current", only_device_id=charger_device_id)
    candidates += _discover_separate_entity_candidates(
        hass, "charger_current", only_device_id=charger_device_id
    )
    candidates += _profile_phase_entity_candidates(hass, charger_device_id, candidates)
    return _ranked(candidates)


async def async_discover_charger_current_sources(
    hass: HomeAssistant, *, charger_device_id: str | None
) -> list[DiscoveryCandidate]:
    """`discover_charger_current_sources` plus the Recorder-history fallback.

    Meant for an idle OCPP charger that has no phase attributes now but charged in
    the past. History is read only for entities that passed the device scoping.
    """
    if not charger_device_id:
        return []
    candidates = await _async_discover_attribute_candidates(
        hass, "charger_current", only_device_id=charger_device_id
    )
    candidates += _discover_separate_entity_candidates(
        hass, "charger_current", only_device_id=charger_device_id
    )
    candidates += _profile_phase_entity_candidates(hass, charger_device_id, candidates)
    return _ranked(candidates)


def _profile_phase_entity_candidates(
    hass: HomeAssistant, device_id: str, found: list[DiscoveryCandidate]
) -> list[DiscoveryCandidate]:
    """A separate-entities candidate from the per-phase current sensors the charger's platform profile
    names (`current_sensor_keys`, three of them, matched by translation key or unique id), for
    integrations such as Charge Amps whose entity ids carry the phase before the word "current" and
    so escape the name-based scan. Skipped when `found` already holds the same entities.
    """
    registry = er.async_get(hass)
    entries = er.async_entries_for_device(registry, device_id)
    results: list[DiscoveryCandidate] = []
    for platform in dict.fromkeys(entry.platform for entry in entries):
        profile = profile_for(platform)
        if profile is None or len(profile.current_sensor_keys) != len(PHASES):
            continue
        matcher = EntityMatcher(hass, [e for e in entries if e.platform == platform], profile)
        phase_map: dict[PhaseName, str] = {}
        for phase, key in zip(PHASES, profile.current_sensor_keys):
            entry = matcher.first("sensor", (key,))
            if entry is None:
                break
            phase_map[phase] = entry.entity_id
        if len(phase_map) != len(PHASES) or len(set(phase_map.values())) != len(PHASES):
            continue
        if any(
            candidate.mapping.kind == "separate_entities" and dict(candidate.mapping.entity_ids or {}) == phase_map
            for candidate in [*found, *results]
        ):
            continue
        states = {phase: hass.states.get(entity_id) for phase, entity_id in phase_map.items()}
        if any(state is None for state in states.values()):
            continue
        scored = _score_separate_entities_candidate(states, "charger_current")
        if scored is None:
            continue
        results.append(
            DiscoveryCandidate(
                candidate_id="|".join(sorted(phase_map.values())),
                source_type="charger_current",
                mapping=PhaseMeasurementSource(kind="separate_entities", entity_ids=dict(phase_map)),
                confidence=scored[0],
                reason_code=REASON_SEPARATE_ENTITIES_PROFILE_MATCH,
            )
        )
    return results


_CONFIDENCE_RANK: dict[Confidence, int] = {"high": 0, "medium": 1, "low": 2}


def _ranked(candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
    return sorted(candidates, key=lambda c: (_CONFIDENCE_RANK[c.confidence], c.candidate_id))


def _discover_attribute_candidates(
    hass: HomeAssistant,
    source_type: SourceType,
    *,
    excluded_device_ids: set[str] | None = None,
    only_device_id: str | None = None,
) -> list[DiscoveryCandidate]:
    """Attribute-based candidates from live state alone (see `_attribute_scan`)."""
    return _attribute_scan(
        hass,
        source_type,
        excluded_device_ids=excluded_device_ids or set(),
        only_device_id=only_device_id,
    )[0]


async def _async_discover_attribute_candidates(
    hass: HomeAssistant,
    source_type: SourceType,
    *,
    excluded_device_ids: set[str] | None = None,
    only_device_id: str | None = None,
) -> list[DiscoveryCandidate]:
    """`_discover_attribute_candidates` plus a Recorder-history fallback.

    Recorder is touched only for entities that passed the device filter, carry
    non-standard attributes and have no complete phase mapping live, and not at all if there are none.
    """
    live, pending = _attribute_scan(
        hass,
        source_type,
        excluded_device_ids=excluded_device_ids or set(),
        only_device_id=only_device_id,
    )
    if not pending:
        return live
    return live + await _async_historical_attribute_candidates(hass, source_type, pending)


def _attribute_scan(
    hass: HomeAssistant,
    source_type: SourceType,
    *,
    excluded_device_ids: set[str],
    only_device_id: str | None,
) -> tuple[list[DiscoveryCandidate], list[tuple[str, State]]]:
    """One pass over the entity registry: `(live candidates, entities lacking a live phase mapping)`.

    The device-membership rule is applied first, before any state is read: strictly
    `only_device_id`, or everything except `excluded_device_ids` (device-less
    entities stay eligible, as they can be site measurements). The history fallback
    inherits that boundary. The second list only holds entities whose live state
    exists and whose unit passes `_score_attribute_candidate`, since a candidate
    without a valid parent unit is unusable anyway.
    """
    registry = er.async_get(hass)
    results: list[DiscoveryCandidate] = []
    pending: list[tuple[str, State]] = []
    for entry in sorted(registry.entities.values(), key=lambda e: e.entity_id):
        if only_device_id is not None:
            if entry.device_id != only_device_id:
                continue
        elif entry.device_id is not None and entry.device_id in excluded_device_ids:
            continue
        state = hass.states.get(entry.entity_id)
        if state is None:
            # No live state: unusable now and its unit cannot be validated, so
            # history is not consulted either.
            continue
        scored = _score_attribute_candidate(state, source_type)
        if scored is None:
            continue
        mapping = _phase_mapping(state.attributes)
        if mapping is not None:
            confidence, reason, unit_override = scored
            results.append(
                _attribute_candidate(
                    source_type, entry.entity_id, mapping, unit_override, confidence, reason
                )
            )
            continue
        # Valid parent unit but no complete live mapping: the only case where
        # history is consulted.
        if set(state.attributes) - _STANDARD_ATTRIBUTES:
            pending.append((entry.entity_id, state))
    return results, pending


def _attribute_candidate(
    source_type: SourceType,
    entity_id: str,
    mapping: dict[PhaseName, str],
    unit_override: str,
    confidence: Confidence,
    reason: str,
    observed_at: datetime | None = None,
) -> DiscoveryCandidate:
    """The single place an attributes-based candidate is built, so the live and historical paths cannot drift."""
    return DiscoveryCandidate(
        candidate_id=entity_id,
        source_type=source_type,
        mapping=PhaseMeasurementSource(
            kind="attributes",
            entity_id=entity_id,
            attributes=mapping,
            attribute_unit_override=unit_override,
        ),
        confidence=confidence,
        reason_code=reason,
        observed_at=observed_at,
    )


def _phase_mapping(attributes: Any) -> dict[PhaseName, str] | None:
    """`_find_phase_attributes`, requiring a complete three-phase mapping."""
    mapping = _find_phase_attributes(attributes)
    if mapping is None or len(mapping) != 3:
        return None
    return mapping


async def _async_historical_attribute_candidates(
    hass: HomeAssistant, source_type: SourceType, pending: list[tuple[str, State]]
) -> list[DiscoveryCandidate]:
    """History-only attribute candidates for `pending`, and nothing else.

    Only `pending`'s entities (already device-filtered) reach Recorder, so a site
    meter cannot become a charger candidate or vice versa. Device class, unit and
    the inverter heuristic are still read from the *live* state, because the stored
    unit override must be valid for the entity as it is now.
    """
    matches = await _async_historical_phase_matches(
        hass, [entity_id for entity_id, _state in pending]
    )
    if not matches:
        return []
    results: list[DiscoveryCandidate] = []
    for entity_id, state in pending:
        match = matches.get(entity_id)
        if match is None:
            continue
        mapping, observed_at = match
        scored = _score_attribute_candidate(state, source_type, historical=True)
        if scored is None:
            continue
        confidence, reason, unit_override = scored
        results.append(
            _attribute_candidate(
                source_type, entity_id, mapping, unit_override, confidence, reason, observed_at
            )
        )
    return results


async def _async_historical_phase_matches(
    hass: HomeAssistant, entity_ids: list[str]
) -> dict[str, tuple[dict[PhaseName, str], datetime]]:
    """`_history_phase_matches`, run in Recorder's executor thread.

    * **Batched.** One query for all entities.
    * **Optional, never fatal.** No `recorder`, an empty database or a failing
      query all mean "no historical candidates", never an exception in a config flow.
    * **Off the event loop.** `get_significant_states` is blocking I/O.
    * **Bounded.** After `_HISTORY_TIMEOUT_SECONDS` the read is abandoned and no
      historical candidates are reported.
    """
    if not entity_ids:
        return {}
    if DATA_INSTANCE not in hass.data:
        # Recorder can be excluded from configuration; live-only discovery then.
        _LOGGER.debug("Recorder is not set up -- no history-based discovery")
        return {}
    start_time = dt_util.utcnow() - timedelta(days=_HISTORY_LOOKBACK_DAYS)
    try:
        async with asyncio.timeout(_HISTORY_TIMEOUT_SECONDS):
            return await get_instance(hass).async_add_executor_job(
                _history_phase_matches, hass, entity_ids, start_time
            )
    except TimeoutError:
        _LOGGER.debug(
            "Recorder history lookup for discovery timed out after %s s", _HISTORY_TIMEOUT_SECONDS
        )
        return {}
    except Exception as err:  # noqa: BLE001 -- see this function's docstring
        _LOGGER.warning("Recorder history lookup for discovery failed: %s", err)
        return {}


def _history_phase_matches(
    hass: HomeAssistant, entity_ids: list[str], start_time: datetime
) -> dict[str, tuple[dict[PhaseName, str], datetime]]:
    """Runs inside Recorder's executor thread (never on the event loop).

    `significant_changes_only=False` is required: at its default Recorder drops rows
    whose attributes changed but whose state value did not, which can be the only
    evidence of phase attributes appearing. `minimal_response` and
    `compressed_state_format` must stay `False` so rows carry attributes, and
    `include_start_time_state=False` keeps every match inside
    `_HISTORY_LOOKBACK_DAYS`, as `DiscoveryCandidate.observed_at` claims.
    """
    from homeassistant.components.recorder import history  # noqa: PLC0415

    records = history.get_significant_states(
        hass,
        start_time,
        None,
        list(entity_ids),
        None,
        False,
        False,
        False,
        False,
        False,
    )
    matches: dict[str, tuple[dict[PhaseName, str], datetime]] = {}
    for entity_id, entity_records in records.items():
        match = _latest_historical_phase_match(entity_records)
        if match is not None:
            matches[entity_id] = match
    return matches


def _latest_historical_phase_match(
    records: Iterable[State],
) -> tuple[dict[PhaseName, str], datetime] | None:
    """The most recent record with a complete three-phase mapping, plus its `last_updated`, or `None`.

    Newest-first: the latest mapping best matches today's attribute names.
    """
    for record in reversed(list(records)):
        mapping = _phase_mapping(record.attributes)
        if mapping is not None:
            return mapping, record.last_updated
    return None


def _find_phase_attributes(attributes: Any) -> dict[PhaseName, str] | None:
    """Which of `attributes`' keys is the unambiguous best match for each phase, or `None`.

    `None` when any phase ties between equally plausible names. Keys are considered
    in sorted order, so the result does not depend on insertion order. Names of a
    different quantity (`_QUANTITY_CONFLICT_WORDS`) are never selected. Preference
    follows `_attribute_name_tier`: exact phase name, then current-qualified, then
    any other match.
    """
    if not isinstance(attributes, dict):
        return None
    by_phase: dict[PhaseName, list[tuple[int, str]]] = {}
    for key in sorted(k for k in attributes if isinstance(k, str)):
        tokens = _tokenize(key)
        phase = _phase_indicator_in_tokens(tokens)
        if phase is None:
            continue
        by_phase.setdefault(phase, []).append((_attribute_name_tier(tokens), key))

    result: dict[PhaseName, str] = {}
    for phase, candidates in by_phase.items():
        best_tier = min(tier for tier, _key in candidates)
        best = [key for tier, key in candidates if tier == best_tier]
        if len(best) != 1:
            return None
        result[phase] = best[0]
    return result


def _attribute_name_tier(tokens: list[str]) -> int:
    """Rank how tokens matched a phase indicator: 0 exact phase name (`["l1"]`,
    `["phase", "a"]`); 1 plus only current qualifiers (`["current", "l1"]`); 2 mixed
    with anything else. Callers must already know the tokens matched a phase.
    """
    remaining = [token for token in tokens if token not in _PHASE_MARKER_TOKENS]
    if not remaining:
        return 0
    if all(token in _CURRENT_QUALIFIER_WORDS for token in remaining):
        return 1
    return 2


_SignalLevel = Literal["strong", "device_class_only", "unit_only", "none", "conflict"]


def _entity_current_signal(device_class: Any, unit: Any) -> _SignalLevel:
    """How strongly an entity's `device_class`/unit indicate a current measurement.

    `strong`: class `current` and an ampere unit. `device_class_only`: class
    `current`, no unit. `unit_only`: ampere unit, other class. `none`: neither.
    `conflict`: a non-ampere unit is present (an explicit contradiction).
    """
    is_current_class = device_class == _CURRENT_DEVICE_CLASS
    normalized = normalized_ampere_unit(unit)
    has_any_unit = isinstance(unit, str) and unit.strip() != ""
    if has_any_unit and normalized is None:
        return "conflict"
    if is_current_class and normalized is not None:
        return "strong"
    if is_current_class:
        return "device_class_only"
    if normalized is not None:
        return "unit_only"
    return "none"


def _score_attribute_candidate(
    state: State, source_type: SourceType, *, historical: bool = False
) -> tuple[Confidence, str, str] | None:
    """`(confidence, reason_code, attribute_unit_override)`, or `None` if not proposable.

    Attributes carry no unit, so a candidate needs the parent entity's unit to
    validate as ampere-like; it is stored as the override (implicit trust is a
    separate manual opt-in, `PhaseMeasurementSource.trust_entity_unit_for_attributes`).

    `historical=True` (phase attributes seen only in history) reads the same live
    signals but lowers confidence one tier and uses
    `REASON_ATTRIBUTES_HISTORICAL_MATCH`. `REASON_POSSIBLE_INVERTER_OUTPUT` is kept
    as is, at `"low"`: it describes the entity, and `observed_at` tells live from
    historical.
    """
    device_class = state.attributes.get("device_class")
    unit = state.attributes.get("unit_of_measurement")
    signal = _entity_current_signal(device_class, unit)
    if signal == "conflict":
        return None
    normalized_unit = normalized_ampere_unit(unit)
    if normalized_unit is None:
        return None
    if source_type == "site_current" and _looks_like_inverter(state):
        return "low", REASON_POSSIBLE_INVERTER_OUTPUT, normalized_unit
    if signal == "strong":
        confidence: Confidence = "high"
        reason = REASON_ATTRIBUTES_DEVICE_CLASS_AND_UNIT_MATCH
    else:
        # Only "unit_only" is possible here (valid unit rules out "conflict").
        confidence = "medium"
        reason = REASON_ATTRIBUTES_UNIT_MATCH_ONLY
    if historical:
        return (
            _HISTORICAL_CONFIDENCE[confidence],
            REASON_ATTRIBUTES_HISTORICAL_MATCH,
            normalized_unit,
        )
    return confidence, reason, normalized_unit


def _discover_separate_entity_candidates(
    hass: HomeAssistant,
    source_type: SourceType,
    *,
    excluded_device_ids: set[str] | None = None,
    only_device_id: str | None = None,
) -> list[DiscoveryCandidate]:
    registry = er.async_get(hass)
    excluded_device_ids = excluded_device_ids or set()
    # A list per phase, so two entities competing for one phase are detected as
    # ambiguous rather than one winning by iteration order.
    by_group: dict[tuple[str, str, str | None, str] | tuple[str, str], dict[PhaseName, list[str]]] = {}
    for entry in sorted(registry.entities.values(), key=lambda e: e.entity_id):
        if entry.domain != "sensor":
            continue
        if only_device_id is not None:
            if entry.device_id != only_device_id:
                continue
        elif entry.device_id is not None and entry.device_id in excluded_device_ids:
            continue
        phase = _phase_indicator_in_text(entry.entity_id) or _phase_indicator_in_text(
            entry.original_name or ""
        )
        if phase is None:
            continue
        key = _entity_group_key(entry)
        by_group.setdefault(key, {}).setdefault(phase, []).append(entry.entity_id)

    results: list[DiscoveryCandidate] = []
    for key, phase_lists in by_group.items():
        if set(phase_lists) != set(PHASES):
            continue
        if any(len(entity_ids) != 1 for entity_ids in phase_lists.values()):
            # Ambiguous: never guess.
            continue
        phase_map = {phase: entity_ids[0] for phase, entity_ids in phase_lists.items()}
        states = {phase: hass.states.get(entity_id) for phase, entity_id in phase_map.items()}
        if any(state is None for state in states.values()):
            continue
        scored = _score_separate_entities_candidate(states, source_type)
        if scored is None:
            continue
        confidence, reason = scored
        device_id = key[1] if key[0] == "device" else None
        candidate_id = device_id or "|".join(sorted(phase_map.values()))
        results.append(
            DiscoveryCandidate(
                candidate_id=candidate_id,
                source_type=source_type,
                mapping=PhaseMeasurementSource(kind="separate_entities", entity_ids=dict(phase_map)),
                confidence=confidence,
                reason_code=reason,
            )
        )
    return results


def _entity_group_key(entry: Any) -> tuple[str, str] | tuple[str, str, str | None, str]:
    """The identity separate phase entities must share to be combined into one candidate.

    A device is enough. Without one, entities must share config entry, platform and
    the name minus its phase marker, so unrelated device-less sensors never form a
    false three-phase candidate.
    """
    if entry.device_id is not None:
        return ("device", entry.device_id)
    object_id = entry.entity_id.split(".", 1)[-1]
    prefix = _stripped_phase_prefix(object_id)
    return ("no_device", entry.platform, entry.config_entry_id, prefix)


def _stripped_phase_prefix(object_id: str) -> str:
    """`object_id` without phase-marker tokens: "grid_current_l1" and "grid_current_l2"
    both give "grid_current", while "meter_a_current_l1" and "meter_b_current_l1"
    differ and are never combined.
    """
    tokens = _tokenize(object_id)
    kept: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _PHASE_TOKEN_TO_PHASE:
            index += 1
            continue
        if token == "phase" and index + 1 < len(tokens) and (
            tokens[index + 1] in _PHASE_LETTER_TO_PHASE or tokens[index + 1] in _PHASE_NUMBER_TO_PHASE
        ):
            index += 2
            continue
        kept.append(token)
        index += 1
    return "_".join(kept)


def _score_separate_entities_candidate(
    states: dict[PhaseName, State | None], source_type: SourceType
) -> tuple[Confidence, str] | None:
    """`(confidence, reason)` for a per-phase entity group, or `None` if a unit contradicts "current".

    All three `strong` is high; a uniform weaker signal (device class only or unit
    only) is medium; a name-only match is low.
    """
    signals = [
        _entity_current_signal(
            state.attributes.get("device_class"), state.attributes.get("unit_of_measurement")
        )
        for state in states.values()
    ]
    if "conflict" in signals:
        return None
    if source_type == "site_current" and any(
        _looks_like_inverter(state) for state in states.values() if state is not None
    ):
        return "low", REASON_POSSIBLE_INVERTER_OUTPUT
    if all(signal == "strong" for signal in signals):
        return "high", REASON_SEPARATE_ENTITIES_DEVICE_CLASS_AND_UNIT_MATCH
    if all(signal in ("strong", "device_class_only") for signal in signals) and any(
        signal == "device_class_only" for signal in signals
    ):
        return "medium", REASON_SEPARATE_ENTITIES_DEVICE_CLASS_MATCH_ONLY
    if all(signal in ("strong", "unit_only") for signal in signals) and any(
        signal == "unit_only" for signal in signals
    ):
        return "medium", REASON_SEPARATE_ENTITIES_UNIT_MATCH_ONLY
    return "low", REASON_SEPARATE_ENTITIES_NAME_MATCH_ONLY


def _looks_like_inverter(state: State) -> bool:
    """Name-only heuristic: inverter/PV/solar output without "grid" is not proven to equal
    the site's incoming current, so it is never suggested with high confidence.
    """
    text = f"{state.entity_id} {state.attributes.get('friendly_name', '')}".lower()
    return any(word in text for word in _INVERTER_WORDS) and _GRID_WORD not in text


def _tokenize(text: str) -> list[str]:
    return [token for token in re.split(r"[^a-z0-9]+", text.lower()) if token]


def _phase_indicator_in_tokens(tokens: list[str]) -> PhaseName | None:
    """Best-effort phase indicator from tokenized text, deliberately conservative.

    Names containing a different quantity (`_QUANTITY_CONFLICT_WORDS`) never match.
    """
    if not tokens or any(token in _QUANTITY_CONFLICT_WORDS for token in tokens):
        return None
    for index in range(len(tokens) - 1):
        if tokens[index] != "phase":
            continue
        following = tokens[index + 1]
        if following in _PHASE_LETTER_TO_PHASE:
            return _PHASE_LETTER_TO_PHASE[following]
        if following in _PHASE_NUMBER_TO_PHASE:
            return _PHASE_NUMBER_TO_PHASE[following]
    return _PHASE_TOKEN_TO_PHASE.get(tokens[-1])


def _phase_indicator_in_text(text: str) -> PhaseName | None:
    """Phase indicator from text, matching whole tokens only ("Phase A" yes, "alpha" no)."""
    return _phase_indicator_in_tokens(_tokenize(text))
