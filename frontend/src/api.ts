// The only way the card talks to the backend: authenticated Home Assistant WebSocket commands via
// `hass.callWS`. No fetch, XHR, relay URL or entity reads, and the card never holds a token, webhook
// id or pairing URL.

import {
  ACTION_API_VERSION,
  ACTION_STOP,
  API_VERSION,
  DEBUG_API_VERSION,
  ENTITY_CONFIG_API_VERSION,
  MARKET_API_VERSION,
  SESSIONS_API_VERSION,
  SETTINGS_API_VERSION,
  REGION_API_VERSION,
  SITE_SETTINGS_API_VERSION,
  type ChargerList,
  type HomeAssistantLike,
  type ManualAction,
  type ManualActionResult,
  type SettingsBody,
} from "./types";
import type { EntityRequest, VehicleSocRequest } from "./entity-config";
import type { SiteSettingsRequest } from "./site-settings";

export const UNSUPPORTED_API_VERSION = "spotnav_unsupported_api_version";

/**
 * What every failure is presented as: static text. A rejection's `message` is transport or backend
 * prose (it can quote a path, token or exception) and is never shown, stored or logged; the stable
 * code is the whole content of a refusal.
 */
export const REQUEST_FAILED_MESSAGE = "The request to Home Assistant failed.";

export class SpotnavApiError extends Error {
  readonly code: string | null;

  constructor(code: string | null) {
    super(REQUEST_FAILED_MESSAGE);
    this.name = "SpotnavApiError";
    this.code = code;
  }
}

function failureCode(error: unknown): string | null {
  if (typeof error === "object" && error !== null) {
    const candidate = (error as { code?: unknown }).code;
    if (typeof candidate === "string" && candidate.length > 0) {
      return candidate;
    }
  }
  return null;
}

async function call<T>(hass: HomeAssistantLike, message: Record<string, unknown>): Promise<T> {
  try {
    return await hass.callWS<T>(message);
  } catch (error) {
    throw new SpotnavApiError(failureCode(error));
  }
}

export async function listChargers(hass: HomeAssistantLike): Promise<ChargerList> {
  return await call<ChargerList>(hass, {
    type: "spotnav/list_chargers",
    api_version: API_VERSION,
  });
}

/**
 * One charger's whole view as the backend sent it, deliberately `unknown`: only `decodeDashboard()`
 * turns it into a model. A newer backend is not refused here; the version is one of the facts the
 * decoder judges, so unsupported and malformed stay distinct.
 */
export async function getDashboard(
  hass: HomeAssistantLike,
  chargerId: string,
  version: number = API_VERSION,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/get_dashboard",
    api_version: version,
    charger_id: chargerId,
  });
}

/**
 * The action request is `{type, api_version, charger_id, action}` plus one `choice` only for Stop;
 * the field is added only where the contract allows it.
 */
function actionMessage(
  chargerId: string,
  action: ManualAction,
  choice: string | null,
): Record<string, unknown> {
  const message: Record<string, unknown> = {
    type: "spotnav/manual_action",
    api_version: ACTION_API_VERSION,
    charger_id: chargerId,
    action,
  };
  if (action === ACTION_STOP && choice !== null) {
    message.choice = choice;
  }
  return message;
}

/**
 * One manual action and its stable envelope, decoded strictly (`ok` boolean, `error`/`action`/`choice`
 * string or null, exactly the contract's keys). A refusal is returned as a value, not thrown.
 */
export async function performAction(
  hass: HomeAssistantLike,
  chargerId: string,
  action: ManualAction,
  choice: string | null = null,
): Promise<ManualActionResult> {
  const raw = await call<unknown>(hass, actionMessage(chargerId, action, choice));
  return decodeActionResult(raw);
}

function decodeActionResult(raw: unknown): ManualActionResult {
  const source = typeof raw === "object" && raw !== null && !Array.isArray(raw) ? raw : null;
  if (source === null) {
    throw new SpotnavApiError(null);
  }
  const record = source as Record<string, unknown>;
  const keys = ["api_version", "ok", "error", "action", "choice"];
  for (const key of keys) {
    if (!(key in record)) {
      throw new SpotnavApiError(null);
    }
  }
  if (Object.keys(record).length !== keys.length) {
    throw new SpotnavApiError(null);
  }
  const version = record.api_version;
  const ok = record.ok;
  const error = record.error;
  const answered = record.action;
  const answeredChoice = record.choice;
  if (typeof version !== "number" || typeof ok !== "boolean") {
    throw new SpotnavApiError(null);
  }
  for (const value of [error, answered, answeredChoice]) {
    if (value !== null && typeof value !== "string") {
      throw new SpotnavApiError(null);
    }
  }
  return {
    api_version: version,
    ok,
    error: (error ?? null) as string | null,
    action: (answered ?? null) as ManualAction | null,
    choice: (answeredChoice ?? null) as string | null,
  };
}

/**
 * One charger's canonical settings as sent, `unknown` until `decodeSettingsAnswer()` decodes it.
 * Edits are built from this record, never from the dashboard's reduced `settings` summary.
 */
export async function getSettings(
  hass: HomeAssistantLike,
  chargerId: string,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/get_settings",
    api_version: SETTINGS_API_VERSION,
    charger_id: chargerId,
  });
}

/**
 * The catalogue's area choices and suggestions, `unknown` until `decodeMarketOptions()`. A refusal
 * here is entry-level and arrives as a rejected message with a stable code.
 */
export async function getMarketOptions(
  hass: HomeAssistantLike,
  chargerId: string,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/get_market_options",
    api_version: MARKET_API_VERSION,
    charger_id: chargerId,
  });
}

/**
 * The Great Britain region of a postcode (`spotnav/find_region`): Home Assistant asks Octopus Energy, the
 * relay never sees it, and nothing keeps it. Anything but a well-formed answer is "unavailable".
 */
export async function findRegion(
  hass: HomeAssistantLike,
  postcode: string,
): Promise<{ region: string | null; reason: string | null }> {
  const answer = await call<unknown>(hass, {
    type: "spotnav/find_region",
    api_version: REGION_API_VERSION,
    postcode,
  });
  if (typeof answer !== "object" || answer === null) {
    return { region: null, reason: "unavailable" };
  }
  const { region, reason } = answer as { region?: unknown; reason?: unknown };
  return {
    region: typeof region === "string" && /^[A-Z0-9-]{1,32}$/.test(region) ? region : null,
    reason: typeof reason === "string" ? reason : null,
  };
}

/**
 * One full settings replacement under compare-and-set: `{type, api_version, charger_id,
 * expected_revision, settings}`. The revision travels beside the body (a `revision` key inside it is
 * refused); `body` comes from the builders in `settings.ts`.
 */
export async function updateSettings(
  hass: HomeAssistantLike,
  chargerId: string,
  expectedRevision: number,
  body: SettingsBody,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/update_settings",
    api_version: SETTINGS_API_VERSION,
    charger_id: chargerId,
    expected_revision: expectedRevision,
    settings: body,
  });
}

/**
 * The one site-wide write (solar priority and/or hybrid forecast sources, admin only). `expected` is
 * the subset last seen, `changes` only the fields this edit owns. The server resolves charger to
 * site and answers with the re-read `site` block, so a conflict is a value, not an exception.
 */
export async function updateSiteSettings(
  hass: HomeAssistantLike,
  chargerId: string,
  request: SiteSettingsRequest,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/update_site_settings",
    api_version: SITE_SETTINGS_API_VERSION,
    charger_id: chargerId,
    expected: request.expected,
    changes: request.changes,
  });
}

/** The redacted installation-wide debug bundle (administrators only), `unknown` until decoded. */
export async function getDebugBundle(hass: HomeAssistantLike): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/get_debug_bundle",
    api_version: DEBUG_API_VERSION,
  });
}

/** Which card the integration serves (any signed-in user): `unknown` until decoded. */
export async function getCardInfo(hass: HomeAssistantLike): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/get_card_info",
    api_version: DEBUG_API_VERSION,
  });
}

export async function getEntityConfig(hass: HomeAssistantLike, chargerId: string): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/get_entity_config",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId,
  });
}

export async function updateEntityConfig(
  hass: HomeAssistantLike,
  chargerId: string,
  request: EntityRequest,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/update_entity_config",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId,
    scope: request.scope,
    expected: request.expected,
    changes: request.changes,
  });
}

/**
 * Choose a vehicle's charge-level sensor, or (`entityId: null`) return it to automatic detection.
 * Administrators only; same envelope as the other entity commands.
 */
export async function setVehicleSoc(
  hass: HomeAssistantLike,
  chargerId: string,
  request: VehicleSocRequest,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/choose_vehicle_soc",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId,
    vehicle_id: request.vehicleId,
    entity_id: request.entityId,
  });
}

export interface VehicleChanges {
  vehicleId: string;
  changes: {
    capacity_kwh?: number | null;
    consumption_kwh_per_10km?: number | null;
    onboard_phases?: 1 | 3 | null;
    /** The car's own target (the same at every charger); `null` clears it. */
    target_percent?: number | null;
    /** The car's minimum charge level (10-80, steps of 5); `null` turns it off. */
    min_percent?: number | null;
  };
  expected: {
    capacity_kwh?: number | null;
    consumption_kwh_per_10km?: number | null;
    onboard_phases?: 1 | 3 | null;
    target_percent?: number | null;
    min_percent?: number | null;
  };
}

/**
 * Change a vehicle's battery size, consumption and/or onboard charger under compare-and-set. Administrators only;
 * the answer is the entity envelope plus the vehicle's row.
 */
/** The identification commands' own version. */
export const IDENTIFICATION_API_VERSION = 1;

/** A person's answer to "which car is plugged in?" (`spotnav/identify_vehicle`); the answer is decoded by the caller. */
export async function identifyVehicle(hass: HomeAssistantLike, chargerId: string, vehicleId: string): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/identify_vehicle",
    api_version: IDENTIFICATION_API_VERSION,
    charger_id: chargerId,
    vehicle_id: vehicleId,
  });
}

/**
 * Choose a car's plug or location source (`spotnav/choose_vehicle_identification`): an entity, `"none"`, or
 * `null` for automatic.
 */
export async function chooseVehicleIdentification(
  hass: HomeAssistantLike,
  chargerId: string,
  request: { vehicleId: string; source: "plug" | "location"; entityId: string | null },
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/choose_vehicle_identification",
    api_version: IDENTIFICATION_API_VERSION,
    charger_id: chargerId,
    vehicle_id: request.vehicleId,
    source: request.source,
    entity_id: request.entityId,
  });
}

/** `spotnav/write_charge_limit`'s own version. */
export const CHARGE_LIMIT_API_VERSION = 1;

/** Write a car's own charge limit (administrators only); the answer is decoded by the caller. */
export async function setChargeLimit(
  hass: HomeAssistantLike,
  chargerId: string,
  request: { vehicleId: string; percent: number },
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/write_charge_limit",
    api_version: CHARGE_LIMIT_API_VERSION,
    charger_id: chargerId,
    vehicle_id: request.vehicleId,
    percent: request.percent,
  });
}

/** The camera commands' own version. */
export const CAMERA_API_VERSION = 1;

/** One camera command (`spotnav/<command>`, administrators only); the answer is decoded by the caller. */
export async function cameraCommand(
  hass: HomeAssistantLike,
  chargerId: string,
  command: "camera_snapshot" | "save_camera_frame" | "take_reference_picture" | "delete_reference_picture" | "reference_picture",
  fields: Record<string, unknown> = {},
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: `spotnav/${command}`,
    api_version: CAMERA_API_VERSION,
    charger_id: chargerId,
    ...fields,
  });
}

export async function updateVehicle(
  hass: HomeAssistantLike,
  chargerId: string,
  request: VehicleChanges,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/update_vehicle",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId,
    vehicle_id: request.vehicleId,
    changes: request.changes,
    expected: request.expected,
  });
}

/**
 * One charger's charge history as sent, `unknown` until `decodeSessions()` decodes it: this and last
 * month, the months and days that have a charge, the open session and the latest sessions.
 */
export async function getSessions(
  hass: HomeAssistantLike,
  chargerId: string,
  limit = 20,
  month: string | null = null,
): Promise<unknown> {
  const message: Record<string, unknown> = {
    type: "spotnav/get_sessions",
    api_version: SESSIONS_API_VERSION,
    charger_id: chargerId,
    limit,
  };
  if (month !== null) {
    message.month = month;
  }
  return await call<unknown>(hass, message);
}

/** The CSV of the sessions that started in one month (`YYYY-MM`). */
export async function getSessionsCsv(
  hass: HomeAssistantLike,
  chargerId: string,
  month: string,
): Promise<unknown> {
  return await call<unknown>(hass, {
    type: "spotnav/get_sessions",
    api_version: SESSIONS_API_VERSION,
    charger_id: chargerId,
    format: "csv",
    month,
  });
}
