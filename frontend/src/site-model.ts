// The shared settings popover's answers, decoded strictly as the heat card's are (`heat-model.ts`): the site block
// (`spotnav/get_site_settings_view`, `spotnav/list_sites`, backend `api/site_view.py`) and the site's battery control
// status (`spotnav/battery_control`, backend `api/battery_control.py`, codes in `docs/home-battery.md`). A missing,
// mistyped or unknown key makes the answer unreadable. The verification report and the trial are passed through as
// they are (the card reads only the report's mode outcomes, defensively).

import { A, fits, N, O, type Spec } from "./heat-model";
import type { AreaOverride } from "./types";
import {
  decodeFiscal,
  decodeGridTariff,
  decodeSite,
  priceProviderOrNull,
  priceSourceOrNull,
  type Fiscal,
  type GridTariffBlock,
  type PriceProvider,
  type PriceSourceBlock,
  type Site,
} from "./validate";

export const SITE_VIEW_API_VERSION = 1;
export const BATTERY_CONTROL_API_VERSION = 1;

export interface SiteListEntry {
  site_id: string;
  name: string;
  charger_ids: string[];
}

export interface SiteSetupItem {
  id: string;
  dismissible: boolean;
  field?: string;
  value?: boolean;
}

export interface SiteView {
  api_version: number;
  site_id: string;
  name: string;
  charger_ids: string[];
  writable: boolean;
  price: { entry_id: string; title: string | null; area_id: string; area_name: string | null } | null;
  battery: { found: boolean };
  setup: { items: SiteSetupItem[] };
  /** The price section's model, `null` exactly where `price` is (no electricity price applies to the site). */
  price_settings: SitePriceSettings | null;
  /** The site's own `site` block (solar forecast sources, load balancing), `null` while the site is not running. */
  site: Site | null;
}

/** The area's facts the price section words its rows with; `null` while the area list does not name the area. */
export interface SiteMarket {
  area_id: string;
  area_name: string | null;
  countries: string[] | null;
  timezone: string | null;
  currency: string | null;
  major_unit: string | null;
  minor_unit: string | null;
}

/** The dashboard's price blocks, read for the site (`api/site_view.py:price_settings_block`). */
export interface SitePriceSettings {
  market: SiteMarket;
  fiscal: Fiscal | null;
  /** The price entry's stored row, which `spotnav/update_price` writes. */
  fees: AreaOverride;
  provider: PriceProvider | null;
  price_source: PriceSourceBlock;
  grid_tariff: GridTariffBlock;
}

export interface BatteryOptions {
  battery_control_enabled: boolean;
  battery_control_shadow_only: boolean;
  battery_control_ai_takeover_ack: boolean;
  battery_control_allow_sell: boolean;
  battery_control_import_limit_acts: boolean;
}

export interface BatteryStatus {
  options: BatteryOptions;
  state: string;
  line: string;
  key: string;
  report: unknown;
  allowed_actions: string[];
  verification: { started_at: string; mode: string | null; stage: string; modes: string[]; done: string[] } | null;
  waiting_for: string | null;
  needs: string[];
  notes: { code: string; at: string }[];
  trial: unknown;
  grid_charge_cap: GridChargeCap;
}

export type PhaseValues = { L1: number; L2: number; L3: number };

export interface GridChargeCap {
  cap_kw: number | null;
  rule: string;
  main_fuse_a: number | null;
  learned: {
    ready: boolean;
    raw_a: PhaseValues | null;
    factor: number;
    margin_a: number;
    peaks_a: PhaseValues | null;
    days_counted: number;
    days_needed: number;
    hours: number;
    minutes: number;
    heat_peak_w: PhaseValues | null;
  } | null;
}

export interface BatteryAnswer {
  api_version: number;
  ok: boolean;
  error: string | null;
  status: BatteryStatus | null;
}

const SITE_LIST = O({ api_version: "number", sites: A(O({ site_id: "string", name: "string", charger_ids: A("string") })) });

const PLAIN_ITEM = O({ id: "string", dismissible: "boolean" });
const WRITING_ITEM = O({ id: "string", dismissible: "boolean", field: "string", value: "boolean" });

const SITE_VIEW = O({
  api_version: "number",
  site_id: "string",
  name: "string",
  charger_ids: A("string"),
  writable: "boolean",
  price: N(O({ entry_id: "string", title: N("string"), area_id: "string", area_name: N("string") })),
  battery: O({ found: "boolean" }),
  setup: O({ items: A("any") }),
  price_settings: N(
    O({
      market: O({
        area_id: "string",
        area_name: N("string"),
        countries: N(A("string")),
        timezone: N("string"),
        currency: N("string"),
        major_unit: N("string"),
        minor_unit: N("string"),
      }),
      // Each read by the dashboard's own decoder below.
      fiscal: "any",
      fees: O({
        area_id: "string",
        vat: O({ enabled: "boolean", value: N("number") }),
        tax: O({ enabled: "boolean", value: N("number") }),
        transfer: O({ enabled: "boolean", value: N("number") }),
      }),
      provider: "any",
      price_source: "any",
      grid_tariff: "any",
    }),
  ),
  site: "any",
});

const OPTIONS = O({
  battery_control_enabled: "boolean",
  battery_control_shadow_only: "boolean",
  battery_control_ai_takeover_ack: "boolean",
  battery_control_allow_sell: "boolean",
  battery_control_import_limit_acts: "boolean",
});

const PHASE_VALUES = O({ L1: "number", L2: "number", L3: "number" });

const GRID_CHARGE_CAP = O({
  cap_kw: N("number"),
  rule: "string",
  main_fuse_a: N("number"),
  learned: N(
    O({
      ready: "boolean",
      raw_a: N(PHASE_VALUES),
      factor: "number",
      margin_a: "number",
      peaks_a: N(PHASE_VALUES),
      days_counted: "number",
      days_needed: "number",
      hours: "number",
      minutes: "number",
      heat_peak_w: N(PHASE_VALUES),
    }),
  ),
});

const STATUS: Spec = O({
  options: OPTIONS,
  state: "string",
  line: "string",
  key: "string",
  report: "any",
  allowed_actions: A("string"),
  verification: N(O({ started_at: "string", mode: N("string"), stage: "string", modes: A("string"), done: A("string") })),
  waiting_for: N("string"),
  needs: A("string"),
  notes: A(O({ code: "string", at: "string" })),
  trial: "any",
  grid_charge_cap: GRID_CHARGE_CAP,
});

const BATTERY = O({ api_version: "number", ok: "boolean", error: N("string"), status: N(STATUS) });

export type Decoded<T> = { ok: true; value: T } | { ok: false; failure: "unsupported" | "malformed" };

function versioned<T>(raw: unknown, version: number, spec: Spec): Decoded<T> {
  if (typeof raw !== "object" || raw === null) {
    return { ok: false, failure: "malformed" };
  }
  const found = (raw as Record<string, unknown>)["api_version"];
  if (typeof found === "number" && found !== version) {
    return { ok: false, failure: "unsupported" };
  }
  return fits(raw, spec) ? { ok: true, value: raw as T } : { ok: false, failure: "malformed" };
}

export function decodeSiteList(raw: unknown): Decoded<{ api_version: number; sites: SiteListEntry[] }> {
  return versioned(raw, SITE_VIEW_API_VERSION, SITE_LIST);
}

export function decodeSiteView(raw: unknown): Decoded<SiteView> {
  const decoded = versioned<SiteView>(raw, SITE_VIEW_API_VERSION, SITE_VIEW);
  if (!decoded.ok) {
    return decoded;
  }
  if (!decoded.value.setup.items.every((item) => fits(item, PLAIN_ITEM) || fits(item, WRITING_ITEM))) {
    return { ok: false, failure: "malformed" };
  }
  const price = decodePriceSettings(decoded.value.price_settings);
  const site = decodeSiteBlock(decoded.value.site);
  if (price === undefined || site === undefined || (price === null) !== (decoded.value.price === null)) {
    return { ok: false, failure: "malformed" };
  }
  return { ok: true, value: { ...decoded.value, price_settings: price, site } };
}

/** The price settings through the dashboard's decoders, strictly: `undefined` for anything that does not fit. */
function decodePriceSettings(raw: SitePriceSettings | null): SitePriceSettings | null | undefined {
  if (raw === null) {
    return null;
  }
  const block = raw as unknown as Record<string, unknown>;
  const fiscal = block["fiscal"] === null ? null : decodeFiscal(block["fiscal"]);
  const provider = block["provider"] === null ? null : priceProviderOrNull(block["provider"]);
  const source = priceSourceOrNull(block);
  let tariff: GridTariffBlock;
  try {
    tariff = decodeGridTariff(block["grid_tariff"]);
  } catch {
    return undefined;
  }
  if ((block["fiscal"] !== null && fiscal === null) || (block["provider"] !== null && provider === null) || source === null) {
    return undefined;
  }
  return { ...raw, fiscal, provider, price_source: source, grid_tariff: tariff };
}

function decodeSiteBlock(raw: unknown): Site | null | undefined {
  if (raw === null) {
    return null;
  }
  try {
    return typeof raw === "object" ? decodeSite(raw as Record<string, unknown>) : undefined;
  } catch {
    return undefined;
  }
}

export function decodeBatteryControl(raw: unknown): Decoded<BatteryAnswer> {
  const decoded = versioned<BatteryAnswer>(raw, BATTERY_CONTROL_API_VERSION, BATTERY);
  if (decoded.ok && decoded.value.ok !== (decoded.value.error === null)) {
    return { ok: false, failure: "malformed" };
  }
  return decoded;
}

/** Each mode's last outcome in the verification report (`{modes: {hold: {outcome}}}`), read defensively. */
export function reportOutcomes(report: unknown): Record<string, string> {
  if (typeof report !== "object" || report === null) {
    return {};
  }
  const modes = (report as Record<string, unknown>)["modes"];
  if (typeof modes !== "object" || modes === null) {
    return {};
  }
  const found: Record<string, string> = {};
  for (const [mode, row] of Object.entries(modes as Record<string, unknown>)) {
    const outcome = typeof row === "object" && row !== null ? (row as Record<string, unknown>)["outcome"] : null;
    if (typeof outcome === "string") {
      found[mode] = outcome;
    }
  }
  return found;
}
