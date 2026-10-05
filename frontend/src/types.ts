// Shapes of the four contracts the card speaks: the dashboard it reads (plus `spotnav/list_chargers`),
// the action contract it writes with, the settings contract it replaces a record through, and the
// market read. Narrow on purpose: the card renders a handful of facts and never re-derives a
// contract. The strict decoders live beside their contract (`validate.ts`, `settings.ts`, `market.ts`).

export const API_VERSION = 1;

/**
 * The manual-action contract's own version, distinct from the dashboard's: a write and a read are
 * different promises.
 */
export const ACTION_API_VERSION = 1;

export const ACTION_START = "start";
export const ACTION_STOP = "stop";
export const ACTION_RESUME = "resume";
export type ManualAction =
  | typeof ACTION_START
  | typeof ACTION_STOP
  | typeof ACTION_RESUME;

/**
 * The automatic axis's own action name. Not a `ManualAction`: a pause describes Auto's control, while
 * the request that carries it out is a `stop` with a typed `choice`.
 */
export const ACTION_PAUSE = "pause";

export const ACTION_UNAVAILABLE_ERROR = "spotnav_action_unavailable";
export const ACTION_FAILED_ERROR = "spotnav_action_failed";
export const ACTION_RECONCILE_FAILED_ERROR = "spotnav_action_reconcile_failed";
export const INVALID_PAUSE_ERROR = "invalid_pause";

/**
 * The vehicle-side observation vocabulary, as the backend publishes it (`charge_progress.py`): wire
 * strings, not translations. `state` decides whether the card says anything; `reason` is the stable
 * code shown as subdued detail. `normal` covers every honest silence, including a pending observation.
 */
export const CHARGE_PROGRESS_NORMAL = "normal";
export const CHARGE_PROGRESS_VEHICLE_NOT_REQUESTING_CURRENT = "vehicle_not_requesting_current";
export const CHARGE_PROGRESS_UNKNOWN = "unknown";

export const CHARGE_PROGRESS_STATES = [
  CHARGE_PROGRESS_NORMAL,
  CHARGE_PROGRESS_VEHICLE_NOT_REQUESTING_CURRENT,
  CHARGE_PROGRESS_UNKNOWN,
] as const;
export type ChargeProgressState = (typeof CHARGE_PROGRESS_STATES)[number];

/** The `charge_progress` block: state, stable reason, and the backend's own start instant (or `null`). */
export interface ChargeProgress {
  state: ChargeProgressState;
  reason: string;
  since: string | null;
}

/**
 * The charger's connection state as the backend publishes it (`charger_connection.py`): wire strings.
 * `unknown` says nothing is shown.
 */
export const CONNECTION_STATES = [
  "disconnected",
  "connected",
  "charging",
  "paused",
  "finished",
  "error",
  "unknown",
] as const;
export type ConnectionStateName = (typeof CONNECTION_STATES)[number];

/** The `connection` block: the state and the entity it was read from (`null` when none). */
export interface ConnectionState {
  state: ConnectionStateName;
  source: string | null;
}

/**
 * One action answer: the stable envelope. `ok: false` carries a stable code, never prose; a committed
 * failure (`spotnav_action_reconcile_failed`) and a pre-effect one (`spotnav_action_failed`) differ.
 */
export interface ManualActionResult {
  api_version: number;
  ok: boolean;
  error: string | null;
  action: ManualAction | null;
  choice: string | null;
}

/** The settings contract's own version: a full replacement of a charger's planning record. */
export const SETTINGS_API_VERSION = 1;

export const SITE_SETTINGS_API_VERSION = 1;
/** `spotnav/find_region`'s own version. */
export const REGION_API_VERSION = 1;

export const ENTITY_CONFIG_API_VERSION = 1;

/** The debug-bundle command's own version. */
export const DEBUG_API_VERSION = 1;

export const SETTINGS_DRIVER_MANUAL = "manual_kwh";
export const SETTINGS_DRIVER_TARGET_SOC = "target_soc";

export const SETTINGS_STRATEGY_CHEAPEST = "cheapest";
export const SETTINGS_STRATEGY_SOLAR = "solar";
export const SETTINGS_STRATEGY_HYBRID = "hybrid";

/**
 * Stable codes of a settings answer. `revision_conflict`: the record moved on, nothing written.
 * `spotnav_settings_reconcile_failed`: the replacement is durable, only the reconcile failed.
 * `spotnav_settings_not_committed`: nothing was written. Never collapse them into "it failed".
 */
export const SETTINGS_REVISION_CONFLICT = "revision_conflict";
export const SETTINGS_RECONCILE_FAILED = "spotnav_settings_reconcile_failed";
export const SETTINGS_NOT_COMMITTED = "spotnav_settings_not_committed";

export interface FiscalOverride {
  enabled: boolean;
  value: number | null;
}

export interface AreaOverride {
  area_id: string;
  vat: FiscalOverride;
  tax: FiscalOverride;
  transfer: FiscalOverride;
}

/** The target-SoC intent (read-only here). */
export interface SettingsTarget {
  vehicle_id: string | null;
  target_percent: number | null;
}

/**
 * One full settings replacement as the contract spells it: twelve keys and no `revision`. The card
 * copies the accepted record and changes only the fields one dialog owns; `revision` travels beside
 * the body as `expected_revision`.
 */
export interface SettingsBody {
  area_id: string | null;
  overrides: AreaOverride[];
  phases: number | null;
  amps: number | null;
  requested_kwh: number;
  /**
   * "Fill": the manual need is the battery's room at each calculation (the kWh slider's last step). Added
   * after the first release of the contract: absent on an older backend, and then never sent.
   */
  fill_to_limit?: boolean;
  max_periods: number;
  departure_enabled: boolean;
  departure_time: string;
  /**
   * The local date (in the market's zone) the departure falls on, or `null` for a daily departure.
   * Added after the first release of the contract: a record without it reads as `null`.
   */
  departure_date: string | null;
  /**
   * The weekdays a daily departure applies on, 1 (Monday) to 7 (Sunday), ascending; every day by default.
   * Added after the first release of the contract: a record without it reads as all seven.
   */
  departure_weekdays: number[];
  strategy: string;
  driver: string;
  target: SettingsTarget;
  /** Sent only by a notifications Save; left out, the stored choice is kept. */
  notifications?: NotificationsBody;
}

export interface SettingsRecord extends Omit<SettingsBody, "notifications"> {
  revision: number;
  /**
   * Which phones hear about which events, and the phones that can (read-only `available`). Added after the
   * first release of the contract: absent on an older backend, and then left out.
   */
  notifications?: NotificationsRecord;
}

/** One notify service a person can choose: `notify.<service>`, named after its phone. */
export interface NotifyService {
  service: string;
  name: string;
}

/** The writable part of `notifications`: the chosen services and events, and where a tap opens. */
export interface NotificationsBody {
  targets: string[];
  events: string[];
  url: string | null;
}

export interface NotificationsRecord extends NotificationsBody {
  available: NotifyService[];
}

/**
 * The persisted pause as the contract observes it; never edited here, and not "is it paused now"
 * (the dashboard's control block owns that).
 */
export interface PauseObservation {
  choice: string | null;
  admitted_at: string | null;
  expires_at: string | null;
  /** Only a manual pause (a person's Start or Stop) carries these. */
  action?: string;
  scope?: string;
}

/**
 * One settings answer as a value: success carries the committed record and the pause; a refusal
 * carries the stable code and, when there is one, the record that still stands.
 */
export type SettingsAnswer =
  | { ok: true; settings: SettingsRecord; pause: PauseObservation }
  | { ok: false; code: string; settings: SettingsRecord | null; pause: PauseObservation | null };


/** The market contract's own version, distinct from the other three contracts. */
export const MARKET_API_VERSION = 1;

/** The charge-session history contract (`spotnav/get_sessions`), independent of the others. */
export const SESSIONS_API_VERSION = 1;

/**
 * The five catalogue states in the backend's vocabulary: `loading` (attempt running, nothing held),
 * `ready`, `stale` (held but the last attempt failed), `unavailable` (nothing held, relay unreachable),
 * `invalid` (nothing held, response unreadable). Any other value is refused.
 */
export const MARKET_STATES = ["loading", "ready", "stale", "unavailable", "invalid"] as const;
export type MarketState = (typeof MARKET_STATES)[number];

/**
 * The two public reasons a non-ready catalogue carries (or `null`): the relay could not be reached,
 * or what it sent could not be read. No internal code or exception text travels.
 */
export const MARKET_REASONS = ["offline", "invalid"] as const;
export type MarketReason = (typeof MARKET_REASONS)[number];

export interface MarketSuggestionsV1 {
  vat_percent: number | null;
  tax_minor: number | null;
  transfer_minor: number | null;
}

/**
 * One area the relay publishes: name, place, time zone and how its money is named. The EIC is
 * not part of the shape, since the editor does not need it.
 */
/** Where an area's prices come from (relay contract v2), shown beside them as attribution. */
export interface PriceSource {
  name: string;
  url: string;
}

export interface MarketAreaV1 {
  area_id: string;
  name: string;
  countries: string[];
  timezone: string;
  currency: string;
  major_unit: string;
  minor_unit: string;
  suggestions: MarketSuggestionsV1;
  /**
   * Relay contract v2 (the decoder always states them; absent only in a value built by hand). The zone a
   * relay day file's calendar is in, `timezone` unless the relay names another.
   */
  market_timezone?: string;
  /** The fiscal components the published price already includes: locked, nothing added. */
  included?: ("vat" | "tax" | "transfer")[];
  source?: PriceSource | null;
}

export interface MarketOptionsV1 {
  state: MarketState;
  reason: MarketReason | null;
  areas: MarketAreaV1[];
  /**
   * The charger's stored area id from the canonical record. Never dropped or renamed, even if the
   * catalogue no longer lists it; the card says so itself.
   */
  configured_area: string | null;
}

export type MarketDecodeResult =
  | { ok: true; value: MarketOptionsV1 }
  | { ok: false; failure: "unsupported" | "malformed" };

export const CARD_TYPE = "spotnav-card";
export const CARD_ELEMENT = "spotnav-card";
export const CARD_EDITOR_ELEMENT = "spotnav-card-editor";

export interface Capabilities {
  auto_price: boolean;
  current_limit: boolean;
  set_current: boolean;
  regulated_current: boolean;
  target_soc: boolean;
  load_balancing: boolean;
  refresh_vehicle: boolean;
  set_charge_limit: boolean;
  target_stop: boolean;
}

export interface ChargerSummary {
  charger_id: string;
  charger_name: string;
  available: boolean;
  capabilities: Capabilities;
}

export interface ChargerList {
  api_version: number;
  chargers: ChargerSummary[];
}

export interface HomeAssistantLike {
  callWS<T>(message: Record<string, unknown>): Promise<T>;
  /**
   * The Home Assistant user behind this card. Presentation only: a non-administrator gets a read-only
   * dialog. The backend's `require_admin` on the write is the security boundary.
   */
  user?: { is_admin?: boolean };
  /**
   * The reader's Home Assistant language, optional (a `hass` stub may lack it); resolved through
   * `resolveLanguage()`, which falls back to English.
   */
  language?: string;
}

declare global {
  interface Window {
    customCards?: Array<{
      type: string;
      name: string;
      description: string;
      preview?: boolean;
      documentationURL?: string;
    }>;
    /** Set by Home Assistant's scoped custom element registry polyfill when it installs. */
    CustomElementRegistryPolyfill?: unknown;
  }
}

export {};
