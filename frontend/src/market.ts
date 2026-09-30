// Market read on the client: the catalogue's area choices, their suggestions, and the one strict
// decoder that turns that answer into values.
//
// This contract has its own version. The answer is a pure read with no `ok` envelope; every refusal the
// command can make is entry-level and arrives as a rejected message. Everything decodable is an
// enumerated fact, a bounded text identity or a finite number.
//
// Nothing here derives anything: `currency` is the ISO identity, `major_unit`/`minor_unit` are display
// labels (not identities: `kr` is SEK, NOK and DKK alike), VAT is a percent, tax and transfer are in the
// minor unit, and an absent suggestion (`null`) is not a published zero. A payload that cannot be read
// completely is refused whole: `malformed`, or `unsupported` for another version. Neither carries text.

import { translate, type Language, type TranslationKey } from "./i18n";
import { encodeBody } from "./settings";
import {
  MARKET_API_VERSION,
  MARKET_REASONS,
  MARKET_STATES,
  type AreaOverride,
  type FiscalOverride,
  type MarketAreaV1,
  type MarketOptionsV1,
  type MarketReason,
  type MarketState,
  type MarketSuggestionsV1,
  type SettingsBody,
  type SettingsRecord,
} from "./types";

export type MarketDecodeResult =
  | { ok: true; value: MarketOptionsV1 }
  | { ok: false; failure: "unsupported" | "malformed" };


class MalformedPayload extends Error {}

function bad(): never {
  throw new MalformedPayload("malformed");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : bad();
}

function exactKeys(source: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(source).length !== keys.length) {
    return bad();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      return bad();
    }
  }
}

/**
 * A non-empty identity (area id, name, zone, currency, unit label). Empty text is refused; hostile text
 * is data, and the renderer puts it in the DOM as text.
 */
function identity(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" && value.length > 0 ? value : bad();
}

function identityOrNull(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" && value.length > 0 ? value : bad();
}

function figureOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function oneOf<T extends string>(
  source: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
): T {
  const value = source[key];
  if (typeof value !== "string" || !(allowed as readonly string[]).includes(value)) {
    return bad();
  }
  return value as T;
}

const OPTIONS_KEYS = ["api_version", "state", "reason", "areas", "configured_area"] as const;
const AREA_KEYS = [
  "area_id",
  "name",
  "countries",
  "timezone",
  "currency",
  "major_unit",
  "minor_unit",
  "suggestions",
] as const;
const SUGGESTION_KEYS = ["vat_percent", "tax_minor", "transfer_minor"] as const;

function decodeSuggestions(source: Record<string, unknown>): MarketSuggestionsV1 {
  exactKeys(source, SUGGESTION_KEYS);
  // Finite or absent, no range judgement: whether a figure may be stored is the backend's validation.
  return {
    vat_percent: figureOrNull(source, "vat_percent"),
    tax_minor: figureOrNull(source, "tax_minor"),
    transfer_minor: figureOrNull(source, "transfer_minor"),
  };
}

function decodeArea(source: Record<string, unknown>): MarketAreaV1 {
  exactKeys(source, AREA_KEYS);
  const countries = source["countries"];
  if (!Array.isArray(countries)) {
    return bad();
  }
  return {
    area_id: identity(source, "area_id"),
    name: identity(source, "name"),
    // The list may be empty; that is a fact about the area, not a reason to invent a value.
    countries: countries.map((country) =>
      typeof country === "string" && country.length > 0 ? country : bad(),
    ),
    timezone: identity(source, "timezone"),
    currency: identity(source, "currency"),
    major_unit: identity(source, "major_unit"),
    minor_unit: identity(source, "minor_unit"),
    suggestions: decodeSuggestions(record(source["suggestions"])),
  };
}

/**
 * The one market boundary: the answer is `unknown` until this accepts it.
 *
 * Another `api_version` is `unsupported`; every other disagreement is `malformed`. Neither carries payload
 * text. Two cross-field invariants are judged here: a `ready` catalogue has no failure to report, and an
 * `invalid` one has one. Duplicate area ids are refused because the editor keys per-area drafts by id.
 */
export function decodeMarketOptions(raw: unknown): MarketDecodeResult {
  try {
    if (!isRecord(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const version = raw["api_version"];
    if (typeof version === "number" && version !== MARKET_API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys(raw, OPTIONS_KEYS);
    if (version !== MARKET_API_VERSION) {
      return { ok: false, failure: "malformed" };
    }
    const state = oneOf(raw, "state", MARKET_STATES);
    const reasonValue = raw["reason"];
    const reason =
      reasonValue === null ? null : oneOf(raw, "reason", MARKET_REASONS);
    if (state === "ready" && reason !== null) {
      return { ok: false, failure: "malformed" };
    }
    if (state === "invalid" && reason !== "invalid") {
      return { ok: false, failure: "malformed" };
    }
    const rawAreas = raw["areas"];
    if (!Array.isArray(rawAreas)) {
      return { ok: false, failure: "malformed" };
    }
    const areas = rawAreas.map((area) => decodeArea(record(area)));
    const seen = new Set<string>();
    for (const area of areas) {
      if (seen.has(area.area_id)) {
        return { ok: false, failure: "malformed" };
      }
      seen.add(area.area_id);
    }
    return {
      ok: true,
      value: {
        state,
        reason,
        areas,
        // An id the catalogue no longer lists is still a valid configured area: `areas` is what the relay
        // publishes, the id is what the person stored.
        configured_area: identityOrNull(raw, "configured_area"),
      },
    };
  } catch (error) {
    if (error instanceof MalformedPayload) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}

// Area and fiscal model: pure functions (no request, no `hass`, no DOM), so the dialog can only state
// what the reader chose and the card can only send what these functions return.

export const FISCAL_COMPONENTS = ["vat", "tax", "transfer"] as const;
export type FiscalComponent = (typeof FISCAL_COMPONENTS)[number];

/**
 * The three states one component can be in, matching the backend's meanings.
 *
 *   * `off`       -- `enabled=false`; an explicit value is preserved, an absent one stays absent;
 *   * `suggested` -- `enabled=true, value=null`: the backend applies the catalogue's suggestion, so this
 *                    clears any explicit override rather than copying the figure;
 *   * `custom`    -- `enabled=true, value=<finite non-negative number>`.
 *
 * Absent and explicit zero differ: `0` is a valid custom value, no figure is `null`.
 */
export const FISCAL_INTENTS = ["suggested", "custom"] as const;
export type FiscalIntent = (typeof FISCAL_INTENTS)[number];

/**
 * One component as the compact row holds it: checkbox, displayed figure, and what it means.
 *
 * `value` is the figure as displayed and typed (the catalogue's text while `suggested`, without storing it
 * as the person's choice). `intent` says whether the figure is the person's own or still the suggestion;
 * numeric equality must never decide that, since a person may type the suggested number and mean "mine".
 * Editing the figure, or switching on where no suggestion exists, makes it `custom`; only the explicit
 * reset returns to `suggested`. Toggling the checkbox keeps figure and intent. Intent is per area.
 */
export interface FiscalFormValue {
  enabled: boolean;
  value: string;
  intent: FiscalIntent;
}

export function setEnabled(value: FiscalFormValue, enabled: boolean): FiscalFormValue {
  return { ...value, enabled };
}

/** Editing the figure makes it the person's own, whatever it equals. */
export function figureEdited(value: FiscalFormValue, text: string): FiscalFormValue {
  return { enabled: value.enabled, value: text, intent: "custom" };
}

/**
 * Reset: show the catalogue's figure again and mean the suggestion, so the stored value is `null` rather
 * than a copy of today's figure.
 */
export function resetToSuggestion(value: FiscalFormValue): FiscalFormValue {
  return { enabled: value.enabled, value: "", intent: "suggested" };
}

export interface MarketFormValues {
  areaId: string | null;
  vat: FiscalFormValue;
  tax: FiscalFormValue;
  transfer: FiscalFormValue;
}

export type MarketDrafts = Record<string, Omit<MarketFormValues, "areaId">>;

/**
 * One component's draft, read from an accepted row.
 *
 * | stored                     | checkbox  | figure shown        | intent      |
 * |----------------------------|-----------|---------------------|-------------|
 * | `enabled: true, value: null` | checked  | the suggestion      | `suggested` |
 * | `enabled: true, value: N`    | checked  | `N`                 | `custom`    |
 * | `enabled: false, value: N`   | unchecked| `N`, retained       | `custom`    |
 * | `enabled: false, value: null`| unchecked| the suggestion      | `suggested` |
 *
 * An existing figure is the person's own, so re-checking restores it. The text for a `suggested`
 * component is left empty here; the row fills it, as only it knows the current catalogue.
 */
export function fiscalFormFrom(
  row: AreaOverride | null,
  component: FiscalComponent,
): FiscalFormValue {
  const fiscal = row === null ? null : row[component];
  const stored = fiscal === null ? null : fiscal.value;
  return {
    enabled: fiscal !== null && fiscal.enabled,
    value: stored === null ? "" : String(stored),
    intent: stored === null ? "suggested" : "custom",
  };
}

/**
 * The draft for one area, from the accepted record alone: its stored row, else all three components off
 * with absent figures. No suggestion is enabled on the reader's behalf and nothing carries over from
 * another area (a figure in one currency is not a figure in another).
 */
export function marketFormFor(record: SettingsRecord, areaId: string | null): MarketFormValues {
  const row =
    areaId === null
      ? null
      : (record.overrides.find((item) => item.area_id === areaId) ?? null);
  return {
    areaId,
    vat: fiscalFormFrom(row, "vat"),
    tax: fiscalFormFrom(row, "tax"),
    transfer: fiscalFormFrom(row, "transfer"),
  };
}

/**
 * Leave one area's draft and show another's: an area already drafted in this open form returns exactly
 * as left; an untouched one is built from the server's record. Other areas' drafts are copied, never
 * rewritten.
 */
export function switchArea(
  record: SettingsRecord,
  drafts: MarketDrafts,
  current: MarketFormValues | null,
  areaId: string | null,
): { drafts: MarketDrafts; values: MarketFormValues } {
  const next: MarketDrafts = { ...drafts };
  if (current !== null && current.areaId !== null) {
    next[current.areaId] = {
      vat: { ...current.vat },
      tax: { ...current.tax },
      transfer: { ...current.transfer },
    };
  }
  const remembered = areaId === null ? undefined : next[areaId];
  if (remembered === undefined) {
    return { drafts: next, values: marketFormFor(record, areaId) };
  }
  return {
    drafts: next,
    values: {
      areaId,
      vat: { ...remembered.vat },
      tax: { ...remembered.tax },
      transfer: { ...remembered.transfer },
    },
  };
}

export function marketSuggestion(
  options: MarketOptionsV1,
  areaId: string | null,
  component: FiscalComponent,
): number | null {
  const area = areaId === null ? null : (options.areas.find((item) => item.area_id === areaId) ?? null);
  if (area === null) {
    return null;
  }
  if (component === "vat") {
    return area.suggestions.vat_percent;
  }
  return component === "tax" ? area.suggestions.tax_minor : area.suggestions.transfer_minor;
}

/**
 * The unit one component's figure is stated in: VAT is a percent; tax and transfer use the selected
 * area's minor unit. An area the catalogue no longer lists shows no unit rather than borrowing one.
 */
export function fiscalUnit(
  options: MarketOptionsV1,
  areaId: string | null,
  component: FiscalComponent,
): string {
  if (component === "vat") {
    return "%";
  }
  const area = areaId === null ? null : (options.areas.find((item) => item.area_id === areaId) ?? null);
  return area === null ? "" : area.minor_unit;
}

export function marketAreaIsKnown(
  options: MarketOptionsV1,
  base: SettingsRecord,
  areaId: string,
): boolean {
  return (
    areaId === base.area_id || options.areas.some((area) => area.area_id === areaId)
  );
}

function checkCustomFigure(
  text: string,
): { ok: true; value: number } | { ok: false; errorKey: TranslationKey } {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") {
    return { ok: false, errorKey: "settings.error.required" };
  }
  const stated = Number(trimmed);
  if (!Number.isFinite(stated)) {
    return { ok: false, errorKey: "settings.error.invalidNumber" };
  }
  if (stated < 0) {
    return { ok: false, errorKey: "settings.error.outOfRange" };
  }
  return { ok: true, value: stated };
}

function retainedFigure(text: string): number | null {
  const trimmed = text.trim().replace(",", ".");
  if (trimmed === "") {
    return null;
  }
  const kept = Number(trimmed);
  return Number.isFinite(kept) ? kept : null;
}

/**
 * One component's stated record, or the sentence saying why it cannot be stated. Intent decides first:
 *
 *   * `suggested`, unchecked -> `{enabled: false, value: null}`;
 *   * `suggested`, checked -> the suggestion must exist; where the area publishes none, the figure must be
 *     a valid custom number;
 *   * `custom`, unchecked -> `{enabled: false, value: <figure, exactly>}`, or absence when nothing
 *     readable is retained (an off component states no figure to judge);
 *   * `custom`, checked -> a finite, non-negative number; `0` is a value like any other.
 */
function fiscalStated(
  options: MarketOptionsV1,
  areaId: string,
  component: FiscalComponent,
  value: FiscalFormValue,
): { ok: true; value: FiscalOverride } | { ok: false; errorKey: TranslationKey } {
  if (value.intent === "suggested") {
    if (!value.enabled) {
      return { ok: true, value: { enabled: false, value: null } };
    }
    if (marketSuggestion(options, areaId, component) === null) {
      const typed = checkCustomFigure(value.value);
      return typed.ok ? { ok: true, value: { enabled: true, value: typed.value } } : typed;
    }
    return { ok: true, value: { enabled: true, value: null } };
  }
  if (!value.enabled) {
    return { ok: true, value: { enabled: false, value: retainedFigure(value.value) } };
  }
  const typed = checkCustomFigure(value.value);
  return typed.ok ? { ok: true, value: { enabled: true, value: typed.value } } : typed;
}

function sameFiscal(left: FiscalOverride, right: FiscalOverride): boolean {
  return left.enabled === right.enabled && left.value === right.value;
}

function sameRow(left: AreaOverride, right: AreaOverride): boolean {
  return (
    left.area_id === right.area_id &&
    sameFiscal(left.vat, right.vat) &&
    sameFiscal(left.tax, right.tax) &&
    sameFiscal(left.transfer, right.transfer)
  );
}

function rowStatesNothing(row: AreaOverride): boolean {
  for (const component of FISCAL_COMPONENTS) {
    const fiscal = row[component];
    if (fiscal.enabled || fiscal.value !== null) {
      return false;
    }
  }
  return true;
}

/**
 * What one Save would send: the whole replacement, or the input that stops it.
 *
 * Only `area_id` and the override row for the newly selected area are stated. Everything else (mode,
 * strategy, phases, energies, deadline, driver, target record, every other area's fiscal row with key
 * order and figures) is copied through `encodeBody`, so a market edit cannot lose a field it does not own.
 *
 * `changed` is false when nothing differs, and also when all three components are off with no figure for
 * an area with no stored row: adding such a row would change the document to say nothing.
 */
export type MarketReplacementCheck =
  | { ok: true; body: SettingsBody; changed: boolean }
  | { ok: false; errorKey: TranslationKey };

export function marketReplacement(
  base: SettingsRecord,
  values: MarketFormValues,
  options: MarketOptionsV1,
): MarketReplacementCheck {
  const areaId = values.areaId;
  if (areaId === null) {
    return { ok: false, errorKey: "market.error.areaRequired" };
  }
  if (!marketAreaIsKnown(options, base, areaId)) {
    return { ok: false, errorKey: "market.error.areaUnknown" };
  }
  const components: Record<FiscalComponent, FiscalOverride> = {
    vat: { enabled: false, value: null },
    tax: { enabled: false, value: null },
    transfer: { enabled: false, value: null },
  };
  for (const component of FISCAL_COMPONENTS) {
    const stated = fiscalStated(options, areaId, component, values[component]);
    if (!stated.ok) {
      return stated;
    }
    components[component] = stated.value;
  }
  const row: AreaOverride = {
    area_id: areaId,
    vat: components.vat,
    tax: components.tax,
    transfer: components.transfer,
  };
  const existing = base.overrides.find((item) => item.area_id === areaId) ?? null;
  const changed = areaId !== base.area_id
    ? true
    : existing === null
      ? !rowStatesNothing(row)
      : !sameRow(existing, row);
  const overrides =
    existing === null && rowStatesNothing(row)
      ? base.overrides
      : existing === null
        ? [...base.overrides, row]
        : base.overrides.map((item) => (item.area_id === areaId ? row : item));
  return {
    ok: true,
    body: { ...encodeBody(base), area_id: areaId, overrides },
    changed,
  };
}

/**
 * States in which the area and taxes may be changed: `ready`, and `stale` (held catalogue whose last
 * attempt failed, usable while visibly stale). With nothing held (`loading`, `unavailable`, `invalid`)
 * the dialog is a read-only view of what is stored.
 */
export const MARKET_EDITABLE_STATES = ["ready", "stale"] as const satisfies readonly MarketState[];

/**
 * Whether the area and its taxes may be edited right now: one rule used by both the presentation
 * (disable inputs, withhold Save/Reapply) and the card (re-checked before building a replacement, since a
 * catalogue can go missing under an open dialog).
 *
 * Administrator status is necessary but not sufficient: without a held catalogue a stored area can still
 * be displayed, but a figure cannot be stated.
 */
export function marketEditable(state: MarketState, isAdmin: boolean): boolean {
  return isAdmin && (MARKET_EDITABLE_STATES as readonly MarketState[]).includes(state);
}


/**
 * The area trigger's value: public name and id, or one fallback. The id is added only when the name does
 * not already carry it (`Malmö · SE4`, not `Malmö (SE4) · SE4`). With no name the id is shown, with
 * neither the localized "not set"; flags, glyphs and country codes are never the identity.
 */
export function marketAreaLabel(
  language: Language,
  name: string | null,
  id: string | null,
): string {
  const hasName = name !== null && name !== "";
  const hasId = id !== null && id !== "";
  if (!hasName && !hasId) {
    return translate(language, "market.unset");
  }
  if (!hasName) {
    return id as string;
  }
  if (!hasId || (name as string).includes(id as string)) {
    return name as string;
  }
  return `${name} \u00b7 ${id}`;
}

/**
 * Why the catalogue is not `ready`, as one sentence -- or `null` when it is. `stale` is usable but not
 * called fresh; nothing held is either "could not be reached" or "could not be read", never collapsed.
 */
export function marketStateKey(state: MarketState, reason: MarketReason | null): TranslationKey | null {
  if (state === "ready") {
    return null;
  }
  if (state === "stale") {
    return reason === "invalid" ? "market.state.staleInvalid" : "market.state.staleOffline";
  }
  if (state === "invalid") {
    return "market.state.invalid";
  }
  if (state === "unavailable") {
    return "market.state.offline";
  }
  return "market.state.loading";
}

/** One optgroup of the area picker: a country heading and the areas listed under it. */
export interface AreaPickerGroup<T> {
  heading: string;
  areas: T[];
}

/**
 * The picker's structure, as the Android app builds it (`AreaSelection`): areas covering the local
 * `region` come first, then the rest, each part in catalogue order. An area sits under the region when
 * it covers it, else under its own first country; headings follow first appearance. Comparison ignores
 * case and headings are upper case. An area with no countries has no heading and is returned apart, for
 * the caller to list after the groups. With no region nothing is moved forward.
 */
export function groupAreasForPicker<T extends { countries: readonly string[] }>(
  areas: readonly T[],
  region: string | null,
): { groups: AreaPickerGroup<T>[]; ungrouped: T[] } {
  const local = region === null ? "" : region.trim().toUpperCase();
  const upper = (area: T): string[] => area.countries.map((country) => country.toUpperCase());
  const covers = (area: T): boolean => local !== "" && upper(area).includes(local);
  const ordered = [...areas.filter(covers), ...areas.filter((area) => !covers(area))];
  const groups: AreaPickerGroup<T>[] = [];
  const ungrouped: T[] = [];
  for (const area of ordered) {
    const countries = upper(area);
    const heading = covers(area) ? local : countries[0];
    if (heading === undefined) {
      ungrouped.push(area);
      continue;
    }
    const group = groups.find((entry) => entry.heading === heading);
    if (group === undefined) {
      groups.push({ heading, areas: [area] });
    } else {
      group.areas.push(area);
    }
  }
  return { groups, ungrouped };
}

/** A country's name in the card language, or its upper-case code when the runtime knows no name. */
export function countryLabel(language: Language, country: string): string {
  const code = country.trim().toUpperCase();
  try {
    const name = new Intl.DisplayNames([language], { type: "region" }).of(code);
    return name === undefined || name.trim() === "" || name.toUpperCase() === code ? code : name;
  } catch {
    return code;
  }
}
