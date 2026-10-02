// Client side of `spotnav/update_site_settings`: its envelope and the pure compare-and-set bodies.
// It has its own version (`SITE_SETTINGS_API_VERSION`). `site` travels in the shape
// `dashboard_api.serialize_site` gives the card, decoded by `decodeSite` from `validate.ts`.

import { formatNumber } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import { decodeSite, type Site } from "./validate";
import { SITE_SETTINGS_API_VERSION } from "./types";

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
    bad();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      bad();
    }
  }
}

function textOrNull(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad();
}

function booleanValue(source: Record<string, unknown>, key: string): boolean {
  const value = source[key];
  return typeof value === "boolean" ? value : bad();
}

export const SITE_SETTINGS_NOT_ADMIN = "spotnav_not_admin";
export const SITE_SETTINGS_NO_SITE = "spotnav_no_site";
export const SITE_SETTINGS_CONFLICT = "spotnav_conflict";
export const SITE_SETTINGS_INVALID_VALUE = "spotnav_invalid_value";
export const SITE_SETTINGS_UNAVAILABLE = "spotnav_site_unavailable";

export const SITE_SETTINGS_ACTIVE_CONTROL_UNAVAILABLE = "spotnav_active_control_unavailable";
export const SITE_SETTINGS_CONFIRMATION_FAILED = "spotnav_confirmation_failed";

const ENVELOPE_KEYS = ["api_version", "ok", "error", "site", "restore"] as const;
const RESTORE_KEYS = ["outcome", "chargers"] as const;
const RESTORE_CHARGER_KEYS = ["charger_id", "charger_name", "outcome", "code", "from_a", "to_a"] as const;

export type RestoreOutcome = "not_needed" | "restored" | "failed";

export interface ChargerRestore {
  chargerId: string;
  chargerName: string | null;
  outcome: RestoreOutcome;
  code: string | null;
  fromA: number | null;
  toA: number | null;
}

export interface RestoreReport {
  outcome: RestoreOutcome;
  chargers: ChargerRestore[];
}

/**
 * One site-settings answer: the committed (or still-standing) `site` block travels with every
 * outcome, except `not_admin` and `no_site`, which never reached a real site.
 */
export type SiteSettingsAnswer =
  | { ok: true; site: Site; restore: RestoreReport | null }
  | { ok: false; code: string; site: Site | null; restore: RestoreReport | null };

export type SiteSettingsDecodeResult =
  | { ok: true; value: SiteSettingsAnswer }
  | { ok: false; failure: "unsupported" | "malformed" };

/**
 * The boundary for the envelope: `unknown` until accepted; a different `api_version` is
 * `unsupported`, anything else that does not fit is `malformed`. No payload text is carried out.
 */
export function decodeSiteSettingsAnswer(raw: unknown): SiteSettingsDecodeResult {
  try {
    if (!isRecord(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const version = raw["api_version"];
    if (typeof version === "number" && version !== SITE_SETTINGS_API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys(raw, ENVELOPE_KEYS);
    if (version !== SITE_SETTINGS_API_VERSION) {
      return { ok: false, failure: "malformed" };
    }
    const ok = booleanValue(raw, "ok");
    const error = textOrNull(raw, "error");
    const rawSite = raw["site"];
    const site = rawSite === null ? null : decodeSite(record(rawSite));
    const restore = decodeRestore(raw["restore"]);
    if (ok) {
      if (error !== null || site === null) {
        return { ok: false, failure: "malformed" };
      }
      return { ok: true, value: { ok: true, site, restore } };
    }
    if (error === null || error === "") {
      return { ok: false, failure: "malformed" };
    }
    return { ok: true, value: { ok: false, code: error, site, restore } };
  } catch (error) {
    if (error instanceof MalformedPayload) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}

function restoreOutcome(source: Record<string, unknown>, key: string): RestoreOutcome {
  const value = source[key];
  return value === "not_needed" || value === "restored" || value === "failed" ? value : bad();
}

function numberOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

/**
 * The `restore` block of an answer, or `null`. The overall outcome must match the per-charger
 * outcomes: a `restored` overall beside a failed charger is malformed.
 */
function decodeRestore(raw: unknown): RestoreReport | null {
  if (raw === null) {
    return null;
  }
  const source = record(raw);
  exactKeys(source, RESTORE_KEYS);
  const outcome = restoreOutcome(source, "outcome");
  const list = source["chargers"];
  if (!Array.isArray(list)) {
    return bad();
  }
  const chargers = list.map((item): ChargerRestore => {
    const entry = record(item);
    exactKeys(entry, RESTORE_CHARGER_KEYS);
    const chargerId = entry["charger_id"];
    if (typeof chargerId !== "string" || chargerId === "") {
      return bad();
    }
    const chargerOutcome = restoreOutcome(entry, "outcome");
    const code = textOrNull(entry, "code");
    if (code !== null && chargerOutcome !== "failed") {
      return bad();
    }
    const toA = numberOrNull(entry, "to_a");
    if (chargerOutcome === "restored" && toA === null) {
      return bad(); // "restored" is only ever said with the current it was restored to
    }
    return {
      chargerId,
      chargerName: textOrNull(entry, "charger_name"),
      outcome: chargerOutcome,
      code,
      fromA: numberOrNull(entry, "from_a"),
      toA,
    };
  });
  const derived: RestoreOutcome = chargers.some((c) => c.outcome === "failed")
    ? "failed"
    : chargers.some((c) => c.outcome === "restored")
      ? "restored"
      : "not_needed";
  if (derived !== outcome) {
    return bad();
  }
  return { outcome, chargers };
}

/** Write bodies: a subset of the two fields this command owns (`site_settings_api._WRITABLE_FIELDS`). */
export interface SiteSettingsChange {
  solar_priority?: string;
  solar_forecast?: string[];
  active_control_enabled?: boolean;
}

export interface SiteSettingsRequest {
  expected: SiteSettingsChange;
  changes: SiteSettingsChange;
}

/**
 * The Solar dialog's Save: only the fields the reader changed travel, each with its confirmed value as
 * `expected`. `null` when nothing changed, which sends nothing.
 */
export function solarSettingsChange(
  confirmed: { priority: string; forecast: readonly string[] },
  chosen: { priority: string; forecast: readonly string[] },
): SiteSettingsRequest | null {
  const expected: SiteSettingsChange = {};
  const changes: SiteSettingsChange = {};
  if (chosen.priority !== confirmed.priority) {
    expected.solar_priority = confirmed.priority;
    changes.solar_priority = chosen.priority;
  }
  const same =
    chosen.forecast.length === confirmed.forecast.length &&
    chosen.forecast.every((id) => confirmed.forecast.includes(id));
  if (!same) {
    expected.solar_forecast = [...confirmed.forecast];
    changes.solar_forecast = [...chosen.forecast];
  }
  return Object.keys(changes).length === 0 ? null : { expected, changes };
}

/**
 * The active load-balancing switch alone, under the same compare-and-set rule; never combined with a
 * solar field.
 */
export function activeControlChange(confirmed: boolean, chosen: boolean): SiteSettingsRequest {
  return { expected: { active_control_enabled: confirmed }, changes: { active_control_enabled: chosen } };
}

/**
 * The sentence for a code. `conflict` has its own wording because the caller adopts the returned
 * `site`; other refusals leave the confirmed values as they were.
 */
export function siteSettingsErrorKey(code: string | null): TranslationKey {
  if (code === null) {
    return "settings.error.generic";
  }
  if (code === SITE_SETTINGS_NOT_ADMIN) {
    return "settings.error.readOnly";
  }
  if (code === SITE_SETTINGS_NO_SITE) {
    return "site.none";
  }
  if (code === SITE_SETTINGS_CONFLICT) {
    return "site.error.conflict";
  }
  if (code === SITE_SETTINGS_INVALID_VALUE) {
    return "settings.error.invalid";
  }
  if (code === SITE_SETTINGS_UNAVAILABLE) {
    return "settings.error.unavailable";
  }
  if (code === SITE_SETTINGS_ACTIVE_CONTROL_UNAVAILABLE) {
    return "site.activeControl.error.unavailable";
  }
  if (code === SITE_SETTINGS_CONFIRMATION_FAILED) {
    return "site.activeControl.error.confirmation";
  }
  return "settings.error.generic";
}

const RESTORE_FAILURE_KEYS: Record<string, TranslationKey> = {
  write_failed: "site.activeControl.restore.failed.write_failed",
  unconfirmed: "site.activeControl.restore.failed.unconfirmed",
  assigned_current_unreadable: "site.activeControl.restore.failed.assigned_current_unreadable",
  no_authoritative_current: "site.activeControl.restore.failed.no_authoritative_current",
  charger_not_loaded: "site.activeControl.restore.failed.charger_not_loaded",
  membership_conflict: "site.activeControl.restore.failed.membership_conflict",
  probe_in_flight: "site.activeControl.restore.failed.probe_in_flight",
  below_minimum: "site.activeControl.restore.failed.below_minimum",
  no_connector_target: "site.activeControl.restore.failed.no_connector_target",
  external_balancer: "site.activeControl.restore.failed.external_balancer",
};

export interface ActiveControlNotice {
  tone: "ok" | "warning";
  lines: string[];
  code: string | null;
}

function restoreLines(language: Language, restore: RestoreReport): { lines: string[]; code: string | null } {
  const acted = restore.chargers.filter((charger) => charger.outcome !== "not_needed");
  if (acted.length === 0) {
    return { lines: [translate(language, "site.activeControl.restore.notNeeded")], code: null };
  }
  const single = restore.chargers.length === 1;
  const lines: string[] = [];
  let firstCode: string | null = null;
  for (const charger of acted) {
    const name = charger.chargerName ?? translate(language, "site.activeControl.restore.unnamed");
    if (charger.outcome === "restored") {
      const to = formatNumber(language, charger.toA ?? 0, 1);
      lines.push(
        single
          ? translate(language, "site.activeControl.restore.restoredOne", { to })
          : translate(language, "site.activeControl.restore.restoredNamed", { name, to }),
      );
      continue;
    }
    const key = charger.code === null ? undefined : RESTORE_FAILURE_KEYS[charger.code];
    const sentence = translate(language, key ?? "site.activeControl.restore.failed.unknown");
    firstCode = firstCode ?? charger.code;
    lines.push(single ? sentence : translate(language, "site.activeControl.restore.line", { name, text: sentence }));
  }
  return { lines, code: firstCode };
}

/**
 * The words for one switch answer, or `null` when there is nothing to say (a successful turn-on).
 * A disable always says what happened to lowered currents; a failed restore never reads as restored.
 */
export function activeControlNotice(
  language: Language,
  answer: SiteSettingsAnswer,
  disabling: boolean,
): ActiveControlNotice | null {
  if (answer.ok) {
    if (!disabling) {
      return null;
    }
    if (answer.restore === null) {
      return {
        tone: "warning",
        lines: [translate(language, "settings.error.generic")],
        code: null,
      };
    }
    const restored = restoreLines(language, answer.restore);
    return {
      tone: answer.restore.outcome === "failed" ? "warning" : "ok",
      lines: [translate(language, "site.activeControl.restore.off"), ...restored.lines],
      code: restored.code,
    };
  }
  const lines = [translate(language, siteSettingsErrorKey(answer.code))];
  let code: string | null = answer.code;
  if (answer.restore !== null && answer.restore.outcome !== "not_needed") {
    const restored = restoreLines(language, answer.restore);
    lines.push(...restored.lines);
    code = restored.code ?? code;
  }
  return { tone: "warning", lines, code };
}
