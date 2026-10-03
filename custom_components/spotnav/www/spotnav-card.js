// SpotNav card, compiled from frontend/ in the same integration.
// Integration version 1.4.0. The integration registers this file as a
// Lovelace module resource itself; it is not added by hand.

// src/types.ts
var API_VERSION = 1;
var ACTION_API_VERSION = 1;
var ACTION_START = "start";
var ACTION_STOP = "stop";
var ACTION_RESUME = "resume";
var ACTION_PAUSE = "pause";
var CHARGE_PROGRESS_NORMAL = "normal";
var CHARGE_PROGRESS_VEHICLE_NOT_REQUESTING_CURRENT = "vehicle_not_requesting_current";
var CHARGE_PROGRESS_UNKNOWN = "unknown";
var CHARGE_PROGRESS_STATES = [
  CHARGE_PROGRESS_NORMAL,
  CHARGE_PROGRESS_VEHICLE_NOT_REQUESTING_CURRENT,
  CHARGE_PROGRESS_UNKNOWN
];
var SETTINGS_API_VERSION = 1;
var SITE_SETTINGS_API_VERSION = 1;
var ENTITY_CONFIG_API_VERSION = 1;
var DEBUG_API_VERSION = 1;
var SETTINGS_DRIVER_TARGET_SOC = "target_soc";
var SETTINGS_STRATEGY_CHEAPEST = "cheapest";
var SETTINGS_STRATEGY_SOLAR = "solar";
var SETTINGS_STRATEGY_HYBRID = "hybrid";
var SETTINGS_REVISION_CONFLICT = "revision_conflict";
var SETTINGS_RECONCILE_FAILED = "spotnav_settings_reconcile_failed";
var SETTINGS_NOT_COMMITTED = "spotnav_settings_not_committed";
var MARKET_API_VERSION = 1;
var SESSIONS_API_VERSION = 1;
var MARKET_STATES = ["loading", "ready", "stale", "unavailable", "invalid"];
var MARKET_REASONS = ["offline", "invalid"];
var CARD_TYPE = "spotnav-card";
var CARD_ELEMENT = "spotnav-card";
var CARD_EDITOR_ELEMENT = "spotnav-card-editor";

// src/api.ts
var UNSUPPORTED_API_VERSION = "spotnav_unsupported_api_version";
var REQUEST_FAILED_MESSAGE = "The request to Home Assistant failed.";
var SpotnavApiError = class extends Error {
  constructor(code) {
    super(REQUEST_FAILED_MESSAGE);
    this.name = "SpotnavApiError";
    this.code = code;
  }
};
function failureCode(error) {
  if (typeof error === "object" && error !== null) {
    const candidate = error.code;
    if (typeof candidate === "string" && candidate.length > 0) {
      return candidate;
    }
  }
  return null;
}
async function call(hass, message) {
  try {
    return await hass.callWS(message);
  } catch (error) {
    throw new SpotnavApiError(failureCode(error));
  }
}
async function listChargers(hass) {
  return await call(hass, {
    type: "spotnav/list_chargers",
    api_version: API_VERSION
  });
}
async function getDashboard(hass, chargerId, version = API_VERSION) {
  return await call(hass, {
    type: "spotnav/get_dashboard",
    api_version: version,
    charger_id: chargerId
  });
}
function actionMessage(chargerId, action, choice) {
  const message = {
    type: "spotnav/manual_action",
    api_version: ACTION_API_VERSION,
    charger_id: chargerId,
    action
  };
  if (action === ACTION_STOP && choice !== null) {
    message.choice = choice;
  }
  return message;
}
async function performAction(hass, chargerId, action, choice = null) {
  const raw = await call(hass, actionMessage(chargerId, action, choice));
  return decodeActionResult(raw);
}
function decodeActionResult(raw) {
  const source = typeof raw === "object" && raw !== null && !Array.isArray(raw) ? raw : null;
  if (source === null) {
    throw new SpotnavApiError(null);
  }
  const record7 = source;
  const keys = ["api_version", "ok", "error", "action", "choice"];
  for (const key of keys) {
    if (!(key in record7)) {
      throw new SpotnavApiError(null);
    }
  }
  if (Object.keys(record7).length !== keys.length) {
    throw new SpotnavApiError(null);
  }
  const version = record7.api_version;
  const ok = record7.ok;
  const error = record7.error;
  const answered = record7.action;
  const answeredChoice = record7.choice;
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
    error: error ?? null,
    action: answered ?? null,
    choice: answeredChoice ?? null
  };
}
async function getSettings(hass, chargerId) {
  return await call(hass, {
    type: "spotnav/get_settings",
    api_version: SETTINGS_API_VERSION,
    charger_id: chargerId
  });
}
async function getMarketOptions(hass, chargerId) {
  return await call(hass, {
    type: "spotnav/get_market_options",
    api_version: MARKET_API_VERSION,
    charger_id: chargerId
  });
}
async function updateSettings(hass, chargerId, expectedRevision, body) {
  return await call(hass, {
    type: "spotnav/update_settings",
    api_version: SETTINGS_API_VERSION,
    charger_id: chargerId,
    expected_revision: expectedRevision,
    settings: body
  });
}
async function updateSiteSettings(hass, chargerId, request) {
  return await call(hass, {
    type: "spotnav/update_site_settings",
    api_version: SITE_SETTINGS_API_VERSION,
    charger_id: chargerId,
    expected: request.expected,
    changes: request.changes
  });
}
async function getDebugBundle(hass) {
  return await call(hass, {
    type: "spotnav/get_debug_bundle",
    api_version: DEBUG_API_VERSION
  });
}
async function getEntityConfig(hass, chargerId) {
  return await call(hass, {
    type: "spotnav/get_entity_config",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId
  });
}
async function updateEntityConfig(hass, chargerId, request) {
  return await call(hass, {
    type: "spotnav/update_entity_config",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId,
    scope: request.scope,
    expected: request.expected,
    changes: request.changes
  });
}
async function setVehicleSoc(hass, chargerId, request) {
  return await call(hass, {
    type: "spotnav/choose_vehicle_soc",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId,
    vehicle_id: request.vehicleId,
    entity_id: request.entityId
  });
}
async function updateVehicle(hass, chargerId, request) {
  return await call(hass, {
    type: "spotnav/update_vehicle",
    api_version: ENTITY_CONFIG_API_VERSION,
    charger_id: chargerId,
    vehicle_id: request.vehicleId,
    changes: request.changes,
    expected: request.expected
  });
}
async function getSessions(hass, chargerId, limit = 20) {
  return await call(hass, {
    type: "spotnav/get_sessions",
    api_version: SESSIONS_API_VERSION,
    charger_id: chargerId,
    limit
  });
}
async function getSessionsCsv(hass, chargerId, range) {
  const message = {
    type: "spotnav/get_sessions",
    api_version: SESSIONS_API_VERSION,
    charger_id: chargerId,
    format: "csv"
  };
  if (range.from !== null) {
    message.from = range.from;
  }
  if (range.to !== null) {
    message.to = range.to;
  }
  return await call(hass, message);
}

// src/chart.ts
var MINUTES_PER_DAY = 1440;
var HOUR_MS = 36e5;
var DAY_MS = 24 * HOUR_MS;
var clocks = /* @__PURE__ */ new Map();
function zoneClock(timeZone) {
  const requested = timeZone === "" ? "UTC" : timeZone;
  const existing = clocks.get(requested);
  if (existing !== void 0) {
    return existing;
  }
  const formatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: requested,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
  const partsOf = (instantMs2) => {
    const parts = {};
    for (const part of formatter.formatToParts(new Date(instantMs2))) {
      parts[part.type] = part.value;
    }
    return parts;
  };
  const offsetFormatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: requested,
    timeZoneName: "shortOffset",
    hour: "2-digit"
  });
  const clock2 = {
    timeZone: requested,
    msOfDay(instantMs2) {
      const parts = partsOf(instantMs2);
      return Number(parts.hour) % 24 * 36e5 + Number(parts.minute) * 6e4 + Number(parts.second) * 1e3;
    },
    dayKey(instantMs2) {
      const parts = partsOf(instantMs2);
      return `${parts.year}-${parts.month}-${parts.day}`;
    },
    offsetLabel(instantMs2) {
      const parts = offsetFormatter.formatToParts(new Date(instantMs2));
      return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
    }
  };
  clocks.set(requested, clock2);
  return clock2;
}
function localDayKey(instantMs2, timeZone) {
  return zoneClock(timeZone).dayKey(instantMs2);
}
function localMidnightAt(instantMs2, timeZone) {
  const clock2 = zoneClock(timeZone);
  const estimate = instantMs2 - clock2.msOfDay(instantMs2);
  let best = estimate;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const candidate of [estimate - HOUR_MS, estimate, estimate + HOUR_MS]) {
    const reading = clock2.msOfDay(candidate);
    const distance = Math.min(reading, 24 * HOUR_MS - reading);
    if (distance < bestDistance) {
      best = candidate;
      bestDistance = distance;
    }
  }
  return best;
}
function localDayLengthMs(dayStartMs, timeZone) {
  const next = localMidnightAt(dayStartMs + 28 * HOUR_MS, timeZone);
  return next - dayStartMs;
}
var MIN_SCALE = 0.72;
var MAX_SCALE = 1.45;
var HEADER_TEXT = 15;
var AXIS_TEXT = 12;
var HEADER_CAP = 0.115;
var AXIS_CAP = 0.082;
function chartMetrics(width, height) {
  const w = Math.max(180, width);
  const h = Math.max(100, height);
  const scale = Math.min(Math.max(Math.min(w / 420, h / 220), MIN_SCALE), MAX_SCALE);
  const pad2 = 14 * scale;
  const headerTextSize = Math.min(HEADER_TEXT, h * HEADER_CAP);
  const axisTextSize = Math.min(AXIS_TEXT, h * AXIS_CAP);
  return {
    width: w,
    height: h,
    scale,
    pad: pad2,
    headerTextSize,
    axisTextSize,
    top: pad2 * 0.9,
    bottom: h - pad2 - axisTextSize * 1.35,
    left: pad2 + Math.max(axisTextSize * 2.5, w * 0.07),
    right: w - pad2
  };
}
function chartScale(width, height, values) {
  const metrics = chartMetrics(width, height);
  const finite2 = values.filter((value) => Number.isFinite(value));
  return {
    ...metrics,
    minValue: Math.min(0, ...finite2.length > 0 ? finite2 : [0]),
    maxValue: Math.max(1, ...finite2.length > 0 ? finite2 : [1])
  };
}
function xForWallClock(scale, position) {
  const share = Math.min(Math.max(position, 0), 1);
  return scale.left + share * (scale.right - scale.left);
}
function wallClockPosition(clock2, instantMs2) {
  return clock2.msOfDay(instantMs2) / DAY_MS;
}
function yAt(scale, value) {
  const span = scale.maxValue - scale.minValue;
  const share = span <= 0 ? 0 : (value - scale.minValue) / span;
  return scale.bottom - share * (scale.bottom - scale.top);
}
function gutterLabels(scale) {
  const candidates = scale.maxValue - scale.minValue < 1e-9 ? [scale.minValue] : [scale.minValue, (scale.minValue + scale.maxValue) / 2, scale.maxValue];
  const seen = /* @__PURE__ */ new Set();
  const labels = [];
  for (const value of candidates) {
    const label = String(Math.round(value));
    if (seen.has(label)) {
      continue;
    }
    seen.add(label);
    labels.push({ value, y: yAt(scale, value), label });
  }
  return labels;
}
function hourMarkers() {
  return [0, 6, 12, 18, 24].map((hour) => hour / 24);
}
function dayRoleOf(dayKey, todayKey) {
  if (dayKey === todayKey) {
    return "today";
  }
  return dayKey < todayKey ? "past" : "future";
}
function wallClockOf(instantMs2, clock2) {
  const dayMs = clock2.msOfDay(instantMs2);
  const hours = Math.floor(dayMs / HOUR_MS);
  const minutes = Math.floor(dayMs % HOUR_MS / 6e4);
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}
function chartSeries(rows, timeZone, nowMs) {
  const clock2 = zoneClock(timeZone);
  const todayKey = localDayKey(nowMs, timeZone);
  const ordered = rows.slice().sort((left, right) => left.startMs - right.startMs || left.endMs - right.endMs);
  const byDay = /* @__PURE__ */ new Map();
  const dayOrder = [];
  for (const row of ordered) {
    const bucket = byDay.get(row.day);
    if (bucket === void 0) {
      byDay.set(row.day, [row]);
      dayOrder.push(row.day);
    } else {
      bucket.push(row);
    }
  }
  const days = [];
  for (const key of dayOrder) {
    const bucket = byDay.get(key);
    const startMs = localMidnightAt(bucket[0].startMs, timeZone);
    const prices2 = bucket.map((row) => row.effective_price).filter((price) => price !== null && Number.isFinite(price));
    days.push({
      key,
      role: dayRoleOf(key, todayKey),
      startMs,
      lengthMs: Math.max(1, localDayLengthMs(startMs, timeZone)),
      slots: bucket.length,
      mean: prices2.length === 0 ? null : prices2.reduce((total, price) => total + price, 0) / prices2.length,
      min: prices2.length === 0 ? null : Math.min(...prices2),
      max: prices2.length === 0 ? null : Math.max(...prices2)
    });
  }
  const marks = [];
  for (const [dayIndex, day] of days.entries()) {
    const bucket = byDay.get(day.key);
    for (const row of bucket) {
      const price = row.effective_price;
      if (price === null || !Number.isFinite(price)) {
        continue;
      }
      const hourly = row.duration_minutes >= 60;
      const drawnMs = hourly ? row.startMs + row.duration_minutes * 6e4 / 2 : row.startMs;
      marks.push({
        startMs: row.startMs,
        endMs: row.endMs,
        durationMinutes: row.duration_minutes,
        price,
        dayKey: row.day,
        dayIndex,
        role: day.role,
        tomorrow: day.role !== "today",
        hourly,
        proposalPlanned: row.proposal_planned,
        installedPlanned: row.installed_planned,
        position: wallClockPosition(clock2, drawnMs),
        wallClock: wallClockOf(drawnMs, clock2),
        offset: clock2.offsetLabel(drawnMs)
      });
    }
  }
  const prices = marks.map((mark) => mark.price);
  return {
    marks,
    days,
    axis: axisTicks(days, clock2),
    hourly: marks.length > 0 && marks.every((mark) => mark.hourly),
    minValue: Math.min(0, ...prices.length > 0 ? prices : [0]),
    maxValue: Math.max(1, ...prices.length > 0 ? prices : [1]),
    current: currentMarkFor(marks, nowMs),
    missing: rows.length - marks.length
  };
}
function currentMarkFor(marks, nowMs) {
  const containing = marks.filter((mark) => mark.startMs <= nowMs && nowMs < mark.endMs);
  if (containing.length <= 1) {
    return containing[0] ?? null;
  }
  const onTheNowFactDay = containing.filter((mark) => mark.role === "today");
  return onTheNowFactDay.length === 1 ? onTheNowFactDay[0] : null;
}
function intervalIdentity(mark) {
  return mark === null ? null : `${mark.dayKey}@${mark.startMs}`;
}
function chartNowAt(marks, nowMs) {
  const mark = currentMarkFor(marks, nowMs);
  return { mark, identity: intervalIdentity(mark) };
}
function nextIntervalBoundary(marks, nowMs) {
  let earliest = null;
  for (const mark of marks) {
    const candidate = mark.startMs > nowMs ? mark.startMs : mark.endMs > nowMs ? mark.endMs : null;
    if (candidate === null) {
      continue;
    }
    if (earliest === null || candidate < earliest) {
      earliest = candidate;
    }
  }
  return earliest;
}
function axisTicks(days, clock2) {
  const first = days[0];
  if (first === void 0) {
    return [];
  }
  return hourMarkers().map(
    (position) => position >= 1 ? { position: 1, instantMs: first.startMs + first.lengthMs } : { position, instantMs: instantAtWallClock(days, position * MINUTES_PER_DAY, clock2) }
  );
}
function instantAtWallClock(days, minuteOfDay, clock2) {
  const first = days[0];
  let best = first.startMs + minuteOfDay * 6e4;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const day of days) {
    const estimate = day.startMs + minuteOfDay * 6e4;
    for (const candidate of [estimate - HOUR_MS, estimate, estimate + HOUR_MS]) {
      const reading = clock2.msOfDay(candidate) / 6e4;
      const gap = Math.abs(reading - minuteOfDay);
      const distance = Math.min(gap, MINUTES_PER_DAY - gap);
      if (distance < bestDistance) {
        best = candidate;
        bestDistance = distance;
      }
    }
  }
  return best;
}
var HIT_SLOP = 20;
var NORMAL_DIAMETER_FRACTION = 0.68;
var TOMORROW_RADIUS_RATIO = 0.8;
var MIN_POINT_RADIUS = 2.2;
var MAX_POINT_RADIUS = 6;
var STROKE_SHARE = 0.36;
var TOMORROW_STROKE_SHARE = 0.29;
function preferredMarkRadius(scale, tomorrow) {
  return tomorrow ? Math.max(3.2 * scale.scale, scale.height * 52e-4) : Math.max(4 * scale.scale, scale.height * 62e-4);
}
function nearestCentreSpacing(xs, fallback) {
  const sorted = Array.from(new Set(xs)).sort((left, right) => left - right);
  let smallest = Number.POSITIVE_INFINITY;
  for (let index = 1; index < sorted.length; index += 1) {
    const gap = sorted[index] - sorted[index - 1];
    if (gap > 0) {
      smallest = Math.min(smallest, gap);
    }
  }
  return Number.isFinite(smallest) ? smallest : fallback;
}
function markGeometry(xs, scale, hourly) {
  const panel = Math.max(1, scale.right - scale.left);
  const spacing = nearestCentreSpacing(xs, panel / (MINUTES_PER_DAY / 15));
  const preferred = Math.min(
    Math.max(preferredMarkRadius(scale, false), MIN_POINT_RADIUS),
    MAX_POINT_RADIUS
  );
  const normalRadius = Math.min(preferred, spacing * NORMAL_DIAMETER_FRACTION / 2);
  const tomorrowPreferred = Math.min(
    Math.max(preferredMarkRadius(scale, true), MIN_POINT_RADIUS),
    MAX_POINT_RADIUS
  );
  const tomorrowRadius = Math.min(tomorrowPreferred, normalRadius * TOMORROW_RADIUS_RATIO);
  const hourHalf = hourly ? panel / 24 : 0;
  return {
    spacing,
    normalRadius,
    tomorrowRadius,
    strokeHalf: hourHalf * STROKE_SHARE,
    tomorrowStrokeHalf: hourHalf * TOMORROW_STROKE_SHARE
  };
}
function hitTargets(series, scale, current) {
  const xs = series.marks.map((mark) => xForWallClock(scale, mark.position));
  const geometry = markGeometry(xs, scale, series.hourly);
  const panelWidth = Math.max(0, scale.right - scale.left);
  return series.marks.map((mark, index) => {
    const isCurrent = current !== null && mark.startMs === current.startMs;
    const halfLength = mark.hourly ? mark.durationMinutes * 6e4 / 2 / DAY_MS * panelWidth : 0;
    return {
      mark,
      x: xs[index],
      y: yAt(scale, mark.price),
      radius: mark.hourly ? 0 : mark.tomorrow ? geometry.tomorrowRadius : geometry.normalRadius,
      current: isCurrent,
      halfLength,
      halfThickness: mark.tomorrow ? geometry.tomorrowStrokeHalf : geometry.strokeHalf
    };
  });
}
function distanceToInk(target, point) {
  const dx = Math.abs(point.x - target.x);
  const dy = Math.abs(point.y - target.y);
  let distance = target.radius > 0 ? Math.max(0, Math.hypot(dx, dy) - target.radius) : Number.POSITIVE_INFINITY;
  if (target.mark.hourly) {
    distance = Math.min(
      distance,
      Math.hypot(Math.max(0, dx - target.halfLength), Math.max(0, dy - target.halfThickness))
    );
  }
  return distance;
}
function chooseMark(targets, point, limit) {
  let best = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const target of targets) {
    const distance = distanceToInk(target, point);
    if (distance > limit) {
      continue;
    }
    const better = distance < bestDistance || distance === bestDistance && best !== null && target.mark.startMs < best.mark.startMs;
    if (better) {
      best = target;
      bestDistance = distance;
    }
  }
  return best?.mark ?? null;
}
function nearestMark(targets, point) {
  return chooseMark(targets, point, Number.POSITIVE_INFINITY);
}
function hitTestMarks(targets, point, slop = HIT_SLOP) {
  return chooseMark(targets, point, slop);
}
function walkMarks(marks, current, step) {
  if (marks.length === 0) {
    return null;
  }
  const edge = () => step >= 0 ? marks[0] : marks[marks.length - 1];
  if (current === null) {
    return edge();
  }
  const index = marks.findIndex((mark) => mark.startMs === current.startMs);
  if (index < 0) {
    return edge();
  }
  return marks[Math.min(Math.max(index + step, 0), marks.length - 1)];
}
function bandPosition(instantMs2, day, clock2) {
  const position = wallClockPosition(clock2, instantMs2);
  return instantMs2 > day.startMs && position === 0 ? 1 : position;
}
function runsOf(rows, days, clock2, flag3) {
  const indexOfDay = new Map(days.map((day, index) => [day.key, index]));
  const bands = [];
  let open = null;
  for (const row of rows.slice().sort((left, right) => left.startMs - right.startMs)) {
    const dayIndex = indexOfDay.get(row.day);
    if (!flag3(row) || dayIndex === void 0) {
      open = null;
      continue;
    }
    const day = days[dayIndex];
    if (open !== null && open.dayIndex === dayIndex && open.endMs === row.startMs) {
      open.endMs = row.endMs;
      open.endPosition = bandPosition(row.endMs, day, clock2);
      continue;
    }
    open = {
      dayKey: row.day,
      dayIndex,
      startMs: row.startMs,
      endMs: row.endMs,
      startPosition: bandPosition(row.startMs, day, clock2),
      endPosition: bandPosition(row.endMs, day, clock2)
    };
    bands.push(open);
  }
  return bands;
}
function plannedBands(rows, days, timeZone) {
  const clock2 = zoneClock(timeZone);
  return {
    installed: runsOf(rows, days, clock2, (row) => row.installed_planned),
    proposal: runsOf(rows, days, clock2, (row) => row.proposal_planned && !row.installed_planned)
  };
}

// src/visual-styles.ts
var LONG_VALUE_LENGTH = 18;
function summaryValueClass(value) {
  return value.length > LONG_VALUE_LENGTH ? `${VISUAL_CLASSES.settingsValue} ${VISUAL_CLASSES.settingsValueLong}` : VISUAL_CLASSES.settingsValue;
}
var VISUAL_CLASSES = {
  shell: "spotnav-shell",
  card: "spotnav-card",
  header: "spotnav-header",
  identity: "spotnav-identity",
  visuallyHidden: "spotnav-visually-hidden",
  name: "spotnav-name",
  iconButton: "spotnav-icon-button",
  actionRow: "spotnav-action-row",
  button: "spotnav-button",
  actionButton: "spotnav-action-button",
  plannerButton: "spotnav-planner-button",
  actionHelp: "spotnav-action-help",
  strategyButton: "spotnav-strategy-button",
  actionError: "spotnav-action-error",
  controlNotice: "spotnav-control-notice",
  advisory: "spotnav-advisory",
  pauseChoices: "spotnav-pause-choices",
  choiceButton: "spotnav-choice-button",
  nameBlock: "spotnav-name-block",
  vehicleLine: "spotnav-vehicle-line",
  vehicleLineName: "spotnav-vehicle-line-name",
  vehicleLineCharge: "spotnav-vehicle-line-charge",
  vehicleLineAge: "spotnav-vehicle-line-age",
  vehicleChoices: "spotnav-vehicle-choices",
  vehicleChoice: "spotnav-vehicle-choice",
  vehicleChoiceName: "spotnav-vehicle-choice-name",
  vehicleChoiceCharge: "spotnav-vehicle-choice-charge",
  strategyRow: "spotnav-strategy-row",
  strategyReason: "spotnav-strategy-reason",
  strategyLink: "spotnav-strategy-link",
  settingsRow: "spotnav-settings-row",
  settingsTrigger: "spotnav-settings-trigger",
  settingsSection: "spotnav-settings-section",
  settingsSectionHeading: "spotnav-settings-section-heading",
  settingsSectionValue: "spotnav-settings-section-value",
  settingsSectionConfigure: "spotnav-settings-section-configure",
  siteApplies: "spotnav-site-applies",
  siteFieldset: "spotnav-site-fieldset",
  siteLegend: "spotnav-site-legend",
  entityNumberLabel: "spotnav-entity-number-label",
  siteChoice: "spotnav-site-choice",
  entityMeters: "spotnav-entity-meters",
  entityLine: "spotnav-entity-line",
  entityLineCells: "spotnav-entity-line-cells",
  entityGroup: "spotnav-entity-group",
  entityRow: "spotnav-entity-row",
  entityRowLabel: "spotnav-entity-row-label",
  entityRowValue: "spotnav-entity-row-value",
  entityHelp: "spotnav-entity-help",
  entityWarning: "spotnav-entity-warning",
  entityNotices: "spotnav-entity-notices",
  entityAutomatic: "spotnav-entity-automatic",
  entityDialog: "spotnav-entity-dialog",
  actionIcon: "spotnav-action-icon",
  summaryExtremes: "spotnav-summary-extremes",
  summaryMax: "spotnav-summary-max",
  summaryMin: "spotnav-summary-min",
  summaryCurrent: "spotnav-summary-current",
  summaryArrow: "spotnav-summary-arrow",
  settingsIcon: "spotnav-settings-icon",
  settingsValue: "spotnav-settings-value",
  settingsValueLong: "spotnav-settings-value-long",
  settingsField: "spotnav-settings-field",
  settingsLabel: "spotnav-settings-label",
  settingsInput: "spotnav-settings-input",
  actionBar: "spotnav-action-bar",
  actionBarWrap: "spotnav-action-bar-wrap",
  barCell: "spotnav-bar-cell",
  barCaption: "spotnav-bar-caption",
  barValue: "spotnav-bar-value",
  barPart: "spotnav-bar-part",
  barWide: "spotnav-bar-wide",
  barBusy: "spotnav-bar-busy",
  settingsCheckRow: "spotnav-settings-check-row",
  settingsActions: "spotnav-settings-actions",
  settingsSave: "spotnav-settings-save",
  settingsReload: "spotnav-settings-reload",
  settingsReapply: "spotnav-settings-reapply",
  settingsReadOnly: "spotnav-settings-readonly",
  settingsNote: "spotnav-settings-note",
  capacityBlock: "spotnav-capacity-block",
  socLink: "spotnav-soc-link",
  settingsConflict: "spotnav-settings-conflict",
  settingsPair: "spotnav-settings-pair",
  settingsSlider: "spotnav-settings-slider",
  settingsUnit: "spotnav-settings-unit",
  settingsPower: "spotnav-settings-power",
  settingsError: "spotnav-settings-error",
  settingsNotice: "spotnav-settings-notice",
  marketArea: "spotnav-market-area",
  marketState: "spotnav-market-state",
  marketComponent: "spotnav-market-component",
  marketCheckbox: "spotnav-market-checkbox",
  marketReset: "spotnav-market-reset",
  marketSuggestion: "spotnav-market-suggestion",
  marketValue: "spotnav-market-value",
  banner: "spotnav-banner",
  bannerBlocking: "spotnav-banner-blocking",
  bannerNotice: "spotnav-banner-notice",
  bannerCount: "spotnav-banner-count",
  status: "spotnav-status",
  graphSurface: "spotnav-graph",
  viewport: "spotnav-chart-viewport",
  svg: "spotnav-svg",
  bands: "spotnav-bands",
  bandInstalled: "spotnav-band-installed",
  bandProposal: "spotnav-band-proposal",
  axis: "spotnav-axis",
  gridline: "spotnav-gridline",
  axisLabel: "spotnav-axis-label",
  tickLabel: "spotnav-tick-label",
  now: "spotnav-now",
  selection: "spotnav-selection",
  marks: "spotnav-marks",
  point: "spotnav-point",
  segment: "spotnav-segment",
  current: "spotnav-current",
  cheap: "spotnav-cheap",
  expensive: "spotnav-expensive",
  tomorrow: "spotnav-tomorrow",
  readout: "spotnav-readout",
  readoutHint: "spotnav-readout-hint",
  legend: "spotnav-legend",
  summary: "spotnav-summary",
  figures: "spotnav-figures",
  figure: "spotnav-figure",
  figureLabel: "spotnav-figure-label",
  figureValue: "spotnav-figure-value",
  periods: "spotnav-periods",
  periodsProposal: "spotnav-periods-proposal",
  periodsInstalled: "spotnav-periods-installed",
  periodsHeading: "spotnav-periods-heading",
  period: "spotnav-period",
  periodActive: "spotnav-period-active",
  context: "spotnav-context",
  overlay: "spotnav-dialog-overlay",
  dialog: "spotnav-dialog",
  dialogHeader: "spotnav-dialog-header",
  dialogTitle: "spotnav-dialog-title",
  dialogClose: "spotnav-dialog-close",
  dialogIntro: "spotnav-dialog-intro",
  dialogBody: "spotnav-dialog-body",
  issueItem: "spotnav-issue",
  issueText: "spotnav-issue-text",
  capabilityItem: "spotnav-capability",
  capabilityLabel: "spotnav-capability-label",
  capabilityState: "spotnav-capability-state",
  capabilityNote: "spotnav-capability-note",
  switchGroup: "spotnav-switch-group",
  switchControl: "spotnav-switch",
  activeNotice: "spotnav-active-notice",
  activeNoticeWarning: "spotnav-active-notice-warning",
  historyBody: "spotnav-history-body",
  historyTiles: "spotnav-history-tiles",
  historyTile: "spotnav-history-tile",
  historyTileHeading: "spotnav-history-tile-heading",
  historyFigures: "spotnav-history-figures",
  historySavings: "spotnav-history-savings",
  historyOpen: "spotnav-history-open",
  historyToggle: "spotnav-history-toggle",
  historyToggleButton: "spotnav-history-toggle-button",
  historyList: "spotnav-history-list",
  historyRow: "spotnav-history-row",
  historyRowTitle: "spotnav-history-row-title",
  historyRowFigures: "spotnav-history-row-figures",
  historyRowNote: "spotnav-history-row-note",
  historyHeading: "spotnav-history-heading",
  historyFootnote: "spotnav-history-footnote",
  historyExport: "spotnav-history-export",
  historyExportLabel: "spotnav-history-export-label",
  muted: "spotnav-muted",
  unavailable: "spotnav-unavailable"
};
var FOCUS_LINE_TOKENS = {
  now: { property: "--spotnav-now-line", theme: "--primary-text-color", fallback: "#616161" },
  selection: {
    property: "--spotnav-selection-line",
    theme: "--primary-text-color",
    fallback: "#616161"
  }
};
function focusLineColour(kind) {
  const token = FOCUS_LINE_TOKENS[kind];
  return `var(${token.property}, var(${token.theme}, ${token.fallback}))`;
}
var VISUAL_STYLES = `
  /*
   * The compact settings row and the dialogs it opens: quiet icon-plus-value triggers that wrap on a
   * narrow dashboard. Dialogs are overlays, so they never change the card's height.
   */
  .spotnav-settings-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 8px 0 0;
  }
  .spotnav-settings-trigger {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .spotnav-settings-row > .spotnav-settings-input {
    width: 6rem;
    min-width: 0;
  }
  .spotnav-settings-icon {
    flex: none;
  }
  .spotnav-settings-value {
    font-variant-numeric: tabular-nums;
  }
  .spotnav-settings-field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 8px;
  }
  .spotnav-settings-check-row {
    display: flex;
    flex-direction: row;
    align-items: center;
    justify-content: flex-start;
    gap: 8px;
    margin-top: 8px;
  }
  .spotnav-settings-check-row > input[type="checkbox"] {
    flex: none;
    margin: 0;
    min-height: 0;
    padding: 0;
    border: 0;
    background: none;
  }
  .spotnav-settings-label {
    font-size: 0.85em;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-settings-input {
    font: inherit;
    min-height: 36px;
    max-width: 100%;
    box-sizing: border-box;
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--secondary-background-color, transparent);
    color: var(--primary-text-color, #212121);
  }
  .spotnav-settings-input:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .spotnav-settings-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 12px;
  }
  /*
   * A slider beside its exact number field. Grid sizing rather than flex: the slider takes what is left
   * (minmax(0, 1fr)), the field gets a bounded share wide enough for the longest accepted value, and
   * the unit is sized by its text. The out-of-domain note spans every column. Energy and current share
   * one layout.
   */
  .spotnav-settings-pair {
    display: grid;
    grid-template-columns: minmax(0, 1fr) clamp(4.5rem, 20%, 6rem) auto;
    align-items: center;
    gap: 8px;
  }
  .spotnav-settings-slider {
    width: 100%;
    min-width: 0;
    accent-color: var(--primary-color, #03a9f4);
  }
  .spotnav-settings-slider:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .spotnav-settings-slider:disabled {
    opacity: 0.55;
  }
  .spotnav-settings-pair > .spotnav-settings-input {
    width: 100%;
    min-width: 0;
  }
  .spotnav-settings-pair > .spotnav-settings-note {
    grid-column: 1 / -1;
    margin: 4px 0 0;
  }
  .spotnav-settings-unit {
    white-space: nowrap;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-settings-power {
    margin: 4px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-settings-save {
    background: var(--primary-color, #03a9f4);
    border-color: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    font-weight: 500;
  }
  .spotnav-settings-reload,
  .spotnav-settings-reapply {
    font-weight: 500;
  }
  .spotnav-settings-readonly,
  .spotnav-settings-note {
    margin: 8px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-site-fieldset[data-part="mode"] {
    margin-top: 12px;
  }
  .spotnav-capability[data-soc-row] {
    padding: 2px 0;
  }
  .spotnav-button.spotnav-soc-link {
    margin: 4px 0 0;
    padding: 0;
    border: 0;
    background: none;
    color: var(--primary-color, #03a9f4);
    text-decoration: underline;
    font-size: 0.9rem;
    min-height: 32px;
    text-align: left;
  }
  .spotnav-settings-conflict {
    margin: 8px 0 0;
    color: var(--error-color, #db4437);
  }
  .spotnav-settings-error,
  .spotnav-settings-notice {
    margin: 4px 0 0;
    color: var(--error-color, #db4437);
  }
  /*
   * The area/fiscal dialog: one selector, then one row per component (checkbox, name, number field,
   * unit) on the same grid as the slider pair, so long names or units never push the figure off the row.
   */
  .spotnav-market-area {
    width: 100%;
  }
  .spotnav-market-state {
    margin: 4px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-market-component {
    margin: 12px 0 0;
    padding: 8px 10px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  /*
   * The checkbox keeps its native size (no fixed pixel sizes here); its label is bound to it, so the
   * whole row toggles it.
   */
  .spotnav-market-checkbox {
    margin: 0;
    accent-color: var(--primary-color, #03a9f4);
  }
  .spotnav-market-value {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) clamp(4.5rem, 20%, 6rem) auto;
    align-items: center;
    gap: 8px;
  }
  .spotnav-market-value > .spotnav-settings-label {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .spotnav-market-value > .spotnav-settings-input {
    width: 100%;
    min-width: 0;
  }
  .spotnav-market-reset {
    flex: none;
    font-size: 0.85em;
    padding: 2px 8px;
  }
  .spotnav-market-suggestion {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 4px 0 0;
    color: var(--secondary-text-color, #727272);
  }
  /*
   * The compact action row: one primary button, one strategy trigger and a help line, with phone-sized
   * targets.
   */
  /* For a label that must exist for assistive technology and not be seen: the standard clip rectangle. */
  .spotnav-visually-hidden {
    position: absolute;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .spotnav-action-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    margin: 8px 0 0;
  }
  /*
   * The action bar: four equal cells (2x2 narrow, 4x1 wide). The wrapper is the size container, so the
   * layout follows the card's width, not the viewport.
   */
  .spotnav-action-bar-wrap {
    container-type: inline-size;
    margin: 8px 0 0;
  }
  .spotnav-action-bar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }
  @container (min-width: 520px) {
    .spotnav-action-bar {
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }
  }
  .spotnav-bar-cell {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    justify-content: flex-start;
    gap: 2px;
    min-width: 0;
    min-height: 52px;
    padding: 6px 2px;
    border-radius: 12px;
    text-align: center;
  }
  .spotnav-bar-cell.spotnav-bar-busy {
    opacity: 0.6;
    animation: spotnav-bar-busy 1.4s ease-in-out infinite;
  }
  @keyframes spotnav-bar-busy {
    50% {
      opacity: 0.35;
    }
  }
  .spotnav-bar-caption {
    font-size: 0.68rem;
    line-height: 1.1;
    opacity: 0.8;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .spotnav-bar-value {
    display: inline-flex;
    flex: 1 1 auto;
    flex-wrap: nowrap;
    align-items: center;
    justify-content: center;
    gap: 4px;
    min-width: 0;
    font-size: 0.85rem;
    font-weight: 500;
  }
  .spotnav-bar-value > .spotnav-settings-value {
    min-width: 0;
    /* A long value wraps inside its cell rather than being cut off. */
    overflow-wrap: break-word;
    line-height: 1.15;
  }
  /*
   * A cell whose value is a line of its own (Plan): the icon rides with the caption, the value is
   * smaller and wraps only between its parts.
   */
  .spotnav-bar-wide > .spotnav-bar-caption {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 3px;
  }
  .spotnav-bar-wide > .spotnav-bar-caption > svg {
    flex: none;
    width: 1.15em;
    height: 1.15em;
    margin: 0;
  }
  .spotnav-bar-wide {
    padding-left: 1px;
    padding-right: 1px;
  }
  .spotnav-bar-wide > .spotnav-bar-value {
    font-size: 0.66rem;
    gap: 0;
  }
  .spotnav-bar-part {
    white-space: nowrap;
  }
  .spotnav-bar-value > .spotnav-settings-icon,
  .spotnav-bar-value > .spotnav-action-icon {
    flex: none;
  }
  .spotnav-bar-value > .spotnav-action-icon {
    flex: none;
    margin-right: 0;
  }
  .spotnav-button {
    font: inherit;
    min-height: 36px;
    padding: 6px 14px;
    border-radius: 18px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--secondary-background-color, transparent);
    color: var(--primary-text-color, #212121);
    cursor: pointer;
  }
  .spotnav-button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .spotnav-button:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .spotnav-action-button {
    background: var(--primary-color, #03a9f4);
    border-color: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    font-weight: 500;
  }
  /*
   * The automatic control is outlined, not filled: it answers a different question from the immediate
   * one (suspend the plan versus command the charger) and reads as the quieter of the two.
   */
  .spotnav-planner-button {
    border-color: var(--primary-color, #03a9f4);
    color: var(--primary-color, #03a9f4);
    font-weight: 500;
  }
  .spotnav-strategy-button {
    font-weight: 500;
  }
  .spotnav-action-help {
    font-size: 0.8em;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-action-error,
  .spotnav-control-notice {
    margin: 4px 0 0;
  }
  .spotnav-control-notice {
    color: var(--secondary-text-color, #727272);
  }
  /**
   * The vehicle-side advisory: a subdued warning, not a blocking banner. It names an observation about
   * the car and offers no action. Tinted at text weight rather than as a filled block.
   */
  .spotnav-advisory {
    margin: 8px 0 0;
    overflow-wrap: anywhere;
    color: var(--warning-color, #ffa600);
  }
  .spotnav-pause-choices,
  .spotnav-strategy-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: flex-start;
    margin-top: 8px;
  }
  .spotnav-choice-button {
    font: inherit;
    min-height: 36px;
    padding: 6px 14px;
    border-radius: 18px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--secondary-background-color, transparent);
    color: var(--primary-text-color, #212121);
    cursor: pointer;
  }
  .spotnav-choice-button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  .spotnav-strategy-reason {
    font-size: 0.8em;
    color: var(--secondary-text-color, #727272);
  }
  .spotnav-strategy-link {
    font: inherit;
    font-size: 0.8em;
    padding: 0;
    border: 0;
    background: none;
    color: var(--primary-color, #03a9f4);
    text-decoration: underline;
    cursor: pointer;
  }
  :host {
    display: block;
    box-sizing: border-box;
    max-width: 100%;
  }
  *, *::before, *::after {
    box-sizing: border-box;
  }
  .${VISUAL_CLASSES.card} {
    box-sizing: border-box;
    max-width: 100%;
    min-width: 0;
    padding: 12px 14px;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    color: var(--primary-text-color, #212121);
    border-radius: var(--ha-card-border-radius, 12px);
    font-size: 0.95rem;
    line-height: 1.35;
  }
  .${VISUAL_CLASSES.shell} {
    display: block;
    min-width: 0;
  }
  .${VISUAL_CLASSES.header} {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.identity} {
    flex: 0 0 auto;
    font-weight: 600;
    letter-spacing: 0.01em;
    color: var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.name} {
    flex: 1 1 auto;
    min-width: 0;
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  /*
   * The header's Info and cog buttons: a flex container centres the icon on both axes, and
   * line-height 0 stops the empty line box pushing it off centre. The retry button shares the class.
   */
  .${VISUAL_CLASSES.iconButton} {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    min-width: 44px;
    min-height: 44px;
    margin: 0;
    padding: 0;
    line-height: 0;
    box-sizing: border-box;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.iconButton} > svg {
    display: block;
    margin: 0;
    vertical-align: baseline;
    width: 1.25em;
    height: 1.25em;
    flex: none;
  }
  .${VISUAL_CLASSES.iconButton}:focus-visible,
  .${VISUAL_CLASSES.banner}:focus-visible,
  .${VISUAL_CLASSES.dialogClose}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.banner} {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    width: 100%;
    margin: 10px 0 0;
    padding: 8px 10px;
    font: inherit;
    text-align: start;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.bannerBlocking} {
    border-color: var(--error-color, #db4437);
    color: var(--error-color, #db4437);
  }
  .${VISUAL_CLASSES.bannerNotice} {
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.bannerCount} {
    flex: 0 0 auto;
    color: var(--secondary-text-color, #727272);
    font-size: 0.8rem;
  }
  .${VISUAL_CLASSES.nameBlock} {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
  }
  .${VISUAL_CLASSES.nameBlock} > .${VISUAL_CLASSES.name} {
    /* A two-line title: no heading margins, so the vehicle line reads as the name's subtitle. */
    margin: 0;
    line-height: 1.25;
    flex: 0 0 auto;
    max-width: 100%;
  }
  /* The planned vehicle: a quiet text button under the name; the header's 44 px icon buttons keep the row tall enough to tap. */
  .${VISUAL_CLASSES.vehicleLine} {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    max-width: 100%;
    min-height: 28px;
    margin: 0 0 -4px -4px;
    padding: 2px 4px;
    font: inherit;
    font-size: 0.85rem;
    color: var(--secondary-text-color, #727272);
    background: transparent;
    border: 0;
    border-radius: 6px;
    text-align: start;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.vehicleLine} > svg {
    flex: none;
  }
  .${VISUAL_CLASSES.vehicleLine}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .${VISUAL_CLASSES.vehicleLineName} {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.vehicleLineCharge},
  .${VISUAL_CLASSES.vehicleLineAge} {
    flex: none;
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.vehicleLineAge} {
    opacity: 0.8;
  }
  .${VISUAL_CLASSES.vehicleChoices} {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .${VISUAL_CLASSES.vehicleChoice} {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    padding: 4px 8px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.vehicleChoice} > input {
    flex: none;
    margin: 0;
  }
  .${VISUAL_CLASSES.vehicleChoiceName} {
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.vehicleChoiceCharge} {
    flex: none;
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.status} {
    margin: 8px 0 0;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.muted} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.unavailable} {
    color: var(--secondary-text-color, #727272);
    font-style: italic;
  }
  .${VISUAL_CLASSES.graphSurface} {
    position: relative;
    width: 100%;
    max-width: 640px;
    min-width: 0;
    margin-top: 10px;
    user-select: none;
    -webkit-user-select: none;
    touch-action: pan-y;
    border-radius: 8px;
  }
  /*
   * The viewport is the measured box: the SVG fits it exactly and its height comes from
   * chartHeightForWidth in chart-render.ts, applied in JavaScript. Nothing below it can change its
   * size. No backticks here: this comment lives inside a template literal.
   */
  .${VISUAL_CLASSES.viewport} {
    display: block;
    width: 100%;
    min-width: 0;
  }
  .${VISUAL_CLASSES.viewport}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.svg} {
    display: block;
    width: 100%;
    height: 100%;
  }
  .${VISUAL_CLASSES.gridline} {
    stroke: var(--divider-color, #e0e0e0);
    stroke-width: 1;
  }
  .${VISUAL_CLASSES.axisLabel},
  .${VISUAL_CLASSES.tickLabel} {
    fill: var(--secondary-text-color, #727272);
    font-size: 11px;
  }
  .${VISUAL_CLASSES.bands} {
    pointer-events: none;
  }
  .${VISUAL_CLASSES.marks} {
    pointer-events: none;
  }
  .${VISUAL_CLASSES.cheap} {
    --spotnav-mark-colour: var(--spotnav-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.bandInstalled} {
    fill: var(--primary-color, #03a9f4);
    opacity: 0.16;
  }
  .${VISUAL_CLASSES.bandProposal} {
    fill: var(--primary-color, #03a9f4);
    opacity: 0.07;
  }
  .${VISUAL_CLASSES.now} {
    stroke: ${focusLineColour("now")};
    stroke-width: 1.5;
  }
  .${VISUAL_CLASSES.selection} {
    stroke: ${focusLineColour("selection")};
    stroke-width: 1.5;
    stroke-dasharray: 3 3;
  }
  .${VISUAL_CLASSES.point},
  .${VISUAL_CLASSES.segment} {
    fill: var(--spotnav-cheap, #2e7d32);
    stroke: var(--spotnav-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.segment} {
    stroke-linecap: round;
  }
  .${VISUAL_CLASSES.expensive},
  .${VISUAL_CLASSES.expensive}:is(circle) {
    fill: var(--spotnav-expensive, #c62828);
    stroke: var(--spotnav-expensive, #c62828);
  }
  .${VISUAL_CLASSES.tomorrow},
  .${VISUAL_CLASSES.tomorrow}:is(circle) {
    /*
     * Tomorrow keeps its cheap/expensive semantics for readout and accessibility, but the overlaid
     * series is neutral so colour distinguishes the foreground (today).
     */
    fill: var(--spotnav-tomorrow, var(--secondary-text-color, #727272));
    stroke: var(--spotnav-tomorrow, var(--secondary-text-color, #727272));
    opacity: 0.62;
  }
  .${VISUAL_CLASSES.current} {
    /* A colour accent only: the current mark keeps the ordinary radius so it cannot cover its neighbours. */
    fill: var(--spotnav-current, #03a9f4);
    stroke: var(--primary-color, #03a9f4);
  }
  @media (forced-colors: active) {
    .${VISUAL_CLASSES.now},
    .${VISUAL_CLASSES.selection} {
      stroke: CanvasText;
    }
  }
  .${VISUAL_CLASSES.readout} {
    box-sizing: border-box;
    height: 1.4em;
    margin: 2px 0 0;
    padding: 0 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.78rem;
    line-height: 1.4;
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.readoutHint} {
    margin-top: 2px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
  }
  .${VISUAL_CLASSES.legend} {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    margin-top: 4px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
  }
  /*
   * Max, min and now sit on one line above the plot (never over the y-axis labels), without wrapping
   * at 340 px; the line clips rather than wraps for a very long translation.
   */
  .${VISUAL_CLASSES.summary} {
    display: flex;
    flex-wrap: nowrap;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    margin: 0;
    padding: 0 2px;
    overflow: hidden;
    white-space: nowrap;
    font-size: 0.78rem;
    line-height: 1.2;
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.summaryExtremes} {
    display: flex;
    flex-wrap: nowrap;
    gap: 10px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.summaryMax} {
    color: var(--spotnav-expensive, #c62828);
  }
  .${VISUAL_CLASSES.summaryMin} {
    color: var(--spotnav-cheap, #2e7d32);
  }
  .${VISUAL_CLASSES.summaryCurrent} {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    font-weight: 600;
    color: var(--primary-text-color, #212121);
  }
  .${VISUAL_CLASSES.summaryArrow} {
    display: inline-block;
  }
  /* The Settings popover's sections: one bordered block per topic. */
  .${VISUAL_CLASSES.settingsSection} {
    margin: 12px 0 0;
    padding: 8px 10px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.settingsSection}:first-child {
    margin-top: 0;
  }
  .${VISUAL_CLASSES.settingsSectionHeading} {
    margin: 0 0 4px;
    font-size: 0.9rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.settingsSectionValue} {
    font-variant-numeric: tabular-nums;
  }
  .${VISUAL_CLASSES.settingsSectionConfigure} {
    margin-top: 8px;
  }
  .${VISUAL_CLASSES.siteApplies} {
    margin: 0 0 8px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.82rem;
  }
  .${VISUAL_CLASSES.siteFieldset} {
    margin: 0 0 8px;
    padding: 0;
    border: 0;
  }
  .${VISUAL_CLASSES.siteLegend} {
    padding: 0;
    margin: 0 0 4px;
    font-size: 0.82rem;
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.siteChoice} {
    display: flex;
    align-items: center;
    gap: 6px;
    min-height: 28px;
  }
  /*
   * The entity editors. The three meter lines (L1-L3) stack as labelled groups; when the dialog is
   * wide enough the nine derived meters form a table. The wrapper is the size container, so the
   * layout follows the dialog, not the viewport.
   */
  .${VISUAL_CLASSES.entityMeters} {
    container-type: inline-size;
    margin: 4px 0 8px;
  }
  .${VISUAL_CLASSES.entityLine} {
    min-width: 0;
    margin: 0 0 4px;
    padding: 0;
    border: 0;
  }
  .${VISUAL_CLASSES.entityLineCells} {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 0 8px;
  }
  @container (min-width: 520px) {
    .${VISUAL_CLASSES.entityLineCells} {
      grid-auto-flow: column;
      grid-auto-columns: minmax(0, 1fr);
      grid-template-columns: none;
    }
  }
  .${VISUAL_CLASSES.entityGroup} {
    margin: 8px 0 0;
  }
  /*
   * One entity row: the label sits above its value so a long entity id cannot squeeze it. The value
   * may break anywhere; the label never mid-word.
   */
  .${VISUAL_CLASSES.entityRow} {
    display: block;
    min-width: 0;
    margin: 0 0 8px;
  }
  .${VISUAL_CLASSES.entityRowLabel} {
    display: flex;
    align-items: center;
    gap: 6px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.82rem;
    overflow-wrap: normal;
    word-break: normal;
  }
  .${VISUAL_CLASSES.entityRowValue} {
    display: block;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.entityHelp},
  .${VISUAL_CLASSES.entityAutomatic},
  .${VISUAL_CLASSES.entityWarning} {
    margin: 2px 0 0;
    font-size: 0.8rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.entityNotices} {
    margin: 8px 0 0;
    padding: 0 0 0 18px;
  }
  .${VISUAL_CLASSES.entityHelp},
  .${VISUAL_CLASSES.entityAutomatic} {
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.entityWarning} {
    color: var(--warning-color, #b26a00);
    font-weight: 500;
  }
  .${VISUAL_CLASSES.settingsField} > .${VISUAL_CLASSES.settingsError} {
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.dialog} {
    max-width: 680px;
  }
  .${VISUAL_CLASSES.capacityBlock} {
    margin-top: 16px;
  }
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.settingsRow} {
    margin: 0;
  }
  .${VISUAL_CLASSES.siteFieldset}.${VISUAL_CLASSES.capacityBlock} > .${VISUAL_CLASSES.siteLegend} {
    font-weight: 600;
    font-size: 1rem;
    color: var(--primary-text-color, inherit);
  }
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.siteFieldset} > .${VISUAL_CLASSES.siteLegend} {
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--primary-text-color, inherit);
  }
  /* A number field (main fuse, safety margin, measurement age) is headed like the groups around it. */
  .${VISUAL_CLASSES.entityDialog} .${VISUAL_CLASSES.settingsField} > .${VISUAL_CLASSES.entityNumberLabel} {
    font-weight: 600;
    font-size: 0.95rem;
    color: var(--primary-text-color, inherit);
  }
  .${VISUAL_CLASSES.entityGroup} > .${VISUAL_CLASSES.siteLegend} {
    display: block;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.actionIcon} {
    margin-right: 4px;
    vertical-align: -2px;
  }
  .${VISUAL_CLASSES.figures} {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    margin-top: 10px;
  }
  .${VISUAL_CLASSES.figure} {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .${VISUAL_CLASSES.figureLabel} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
  }
  .${VISUAL_CLASSES.figureValue} {
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.periods} {
    margin-top: 10px;
    padding: 8px 10px;
    border: 1px solid var(--primary-color, #03a9f4);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.periodsInstalled} {
    background: var(--secondary-background-color, transparent);
  }
  .${VISUAL_CLASSES.periodsProposal} {
    border-style: dashed;
  }
  .${VISUAL_CLASSES.periodsHeading} {
    margin: 0 0 4px;
    font-size: 0.9rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.period} {
    display: flex;
    gap: 8px;
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.periodActive} {
    font-weight: 600;
  }
  .${VISUAL_CLASSES.context} {
    margin-top: 10px;
    color: var(--secondary-text-color, #727272);
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.overlay} {
    position: fixed;
    inset: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    background: var(--spotnav-overlay, rgba(0, 0, 0, 0.45));
  }
  .${VISUAL_CLASSES.overlay}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.dialog} {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
    max-width: 420px;
    max-height: 80vh;
    overflow: auto;
    padding: 14px;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    color: var(--primary-text-color, #212121);
    border-radius: 12px;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.28);
  }
  /*
   * One header row: the title takes the width it needs and the close button keeps its 44x44 tap
   * target at the end of the same row; a long title wraps around the button, never under it.
   */
  .${VISUAL_CLASSES.dialogHeader} {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.dialogTitle} {
    flex: 1 1 auto;
    min-width: 0;
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.dialogClose} {
    flex: none;
    min-width: 44px;
    min-height: 44px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.dialogIntro} {
    margin: 0;
  }
  .${VISUAL_CLASSES.dialogBody} {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyBody} {
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyTiles} {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .${VISUAL_CLASSES.historyTile} {
    flex: 1 1 12rem;
    padding: 8px 10px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyTile} > p,
  .${VISUAL_CLASSES.historyRow} > span {
    margin: 0;
  }
  .${VISUAL_CLASSES.historyTileHeading},
  .${VISUAL_CLASSES.historyHeading} {
    margin: 0 0 4px;
    font-size: 0.9rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.historyFigures},
  .${VISUAL_CLASSES.historyRowFigures} {
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.historyFigures} {
    font-size: 1rem;
    font-weight: 500;
  }
  .${VISUAL_CLASSES.historySavings},
  .${VISUAL_CLASSES.historyRowNote},
  .${VISUAL_CLASSES.historyFootnote} {
    font-size: 0.82rem;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.historyOpen} {
    margin: 0;
    padding: 6px 8px;
    border-inline-start: 3px solid var(--success-color, #43a047);
    font-size: 0.9rem;
  }
  .${VISUAL_CLASSES.historyToggle} {
    display: flex;
    gap: 6px;
  }
  .${VISUAL_CLASSES.historyToggleButton} {
    min-height: 36px;
    padding: 4px 12px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: transparent;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 18px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.historyToggleButton}[aria-pressed="true"] {
    background: var(--secondary-background-color, #e5e5e5);
    border-color: var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.historyToggleButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.historyList} {
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    max-height: 16rem;
    overflow-y: auto;
  }
  .${VISUAL_CLASSES.historyRow} {
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: 6px 0;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyRowTitle} {
    font-weight: 500;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.historyExport} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  .${VISUAL_CLASSES.historyExportLabel} {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 1 1 auto;
    min-width: 0;
  }
  .${VISUAL_CLASSES.historyExportLabel} > select {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 36px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
  }
  .${VISUAL_CLASSES.issueItem} {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.issueText} {
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.capabilityItem} {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.capabilityLabel} {
    overflow-wrap: anywhere;
  }
  /*
   * A summary row on the Settings page: the label never breaks mid-label (ellipsis only as a last
   * resort); a short value sits right-aligned beside it. A value that does not fit beside the label
   * wraps to its own line (flex-wrap), and a value past LONG_VALUE_LENGTH characters is stacked
   * under the label left-aligned from the start (the Android app's threshold), so the two never
   * read as extra rows.
   */
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} {
    flex-wrap: wrap;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.capabilityLabel} {
    /* Muted, as in the app: a value stacked under its label must not read as one more label. */
    color: var(--secondary-text-color, #727272);
    flex: 0 1 auto;
    max-width: 100%;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.settingsValue} {
    flex: 1 1 auto;
    min-width: 0;
    text-align: right;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.settingsValueLong} {
    flex: 1 0 100%;
    text-align: left;
    margin: 1px 0 6px;
  }
  [data-slot='vehicles'] > .${VISUAL_CLASSES.settingsSection} {
    margin-top: 12px;
  }
  .${VISUAL_CLASSES.capabilityState} {
    flex: 0 0 auto;
    color: var(--secondary-text-color, #727272);
    font-size: 0.82rem;
  }
  .${VISUAL_CLASSES.capabilityNote} {
    display: block;
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.switchGroup} {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }
  .${VISUAL_CLASSES.switchControl} {
    appearance: none;
    -webkit-appearance: none;
    position: relative;
    box-sizing: border-box;
    flex: 0 0 auto;
    inline-size: 2.6em;
    block-size: 1.5em;
    margin: 0;
    border: 0;
    border-radius: 0.75em;
    background: var(--switch-unchecked-track-color, var(--disabled-text-color, #9e9e9e));
    cursor: pointer;
    transition: background-color 0.15s;
  }
  .${VISUAL_CLASSES.switchControl}::after {
    content: "";
    position: absolute;
    inset-block-start: 0.2em;
    inset-inline-start: 0.2em;
    inline-size: 1.1em;
    block-size: 1.1em;
    border-radius: 50%;
    background: var(--switch-unchecked-button-color, #ffffff);
    transition: transform 0.15s;
  }
  .${VISUAL_CLASSES.switchControl}:checked {
    background: var(--switch-checked-track-color, var(--primary-color, #03a9f4));
  }
  .${VISUAL_CLASSES.switchControl}:checked::after {
    transform: translateX(1.1em);
    background: var(--switch-checked-button-color, #ffffff);
  }
  .${VISUAL_CLASSES.switchControl}:disabled {
    opacity: 0.45;
    cursor: default;
  }
  .${VISUAL_CLASSES.switchControl}[aria-busy="true"] {
    opacity: 0.6;
    cursor: progress;
  }
  .${VISUAL_CLASSES.switchControl}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.activeNotice} {
    margin: 4px 0 0;
    padding: 6px 8px;
    border-inline-start: 3px solid var(--success-color, #43a047);
    font-size: 0.82rem;
    overflow-wrap: anywhere;
  }
  .${VISUAL_CLASSES.activeNotice} > span {
    display: block;
  }
  .${VISUAL_CLASSES.activeNoticeWarning} {
    border-inline-start-color: var(--warning-color, #b26a00);
  }
  @media (max-width: 400px) {
    .${VISUAL_CLASSES.card} {
      padding: 10px 11px;
    }
    .${VISUAL_CLASSES.figures} {
      gap: 6px 12px;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    * {
      transition: none !important;
      animation: none !important;
    }
  }
`;

// src/chart-render.ts
var SVG_NS = "http://www.w3.org/2000/svg";
function svg(name, attributes = {}) {
  const element8 = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) {
    element8.setAttribute(key, String(value));
  }
  return element8;
}
function chartHeightForWidth(width) {
  const bounded = Number.isFinite(width) && width > 0 ? width : 320;
  return Math.round(Math.min(190, Math.max(120, bounded * 0.26)));
}
function markClassNames(target, series) {
  const day = series.days[target.mark.dayIndex];
  const mean = day?.mean ?? null;
  const cheap = mean === null ? true : target.mark.price <= mean;
  const classes = [cheap ? VISUAL_CLASSES.cheap : VISUAL_CLASSES.expensive];
  if (target.mark.tomorrow) {
    classes.push(VISUAL_CLASSES.tomorrow);
  }
  if (target.current) {
    classes.push(VISUAL_CLASSES.current);
  }
  return classes;
}
function bandRect(band, scale, className) {
  const startX = xForWallClock(scale, band.startPosition);
  const endX = xForWallClock(scale, band.endPosition);
  return svg("rect", {
    class: className,
    x: startX,
    y: scale.top,
    width: Math.max(0, endX - startX),
    height: Math.max(0, scale.bottom - scale.top),
    "data-day-index": band.dayIndex,
    "data-day-key": band.dayKey
  });
}
function renderChart(input) {
  const { series, bands, labels, ids } = input;
  const scale = chartScale(
    input.width,
    input.height,
    series.marks.map((mark) => mark.price)
  );
  const root = svg("svg", {
    class: VISUAL_CLASSES.svg,
    viewBox: `0 0 ${scale.width} ${scale.height}`,
    preserveAspectRatio: "none",
    "aria-hidden": "true",
    focusable: "false"
  });
  const title = svg("title", { id: ids.title });
  title.textContent = input.title;
  const description = svg("desc", { id: ids.description });
  description.textContent = input.description;
  root.append(title, description);
  const bandLayer = svg("g", { class: VISUAL_CLASSES.bands });
  for (const band of bands.installed) {
    bandLayer.append(bandRect(band, scale, VISUAL_CLASSES.bandInstalled));
  }
  for (const band of bands.proposal) {
    bandLayer.append(bandRect(band, scale, VISUAL_CLASSES.bandProposal));
  }
  root.append(bandLayer);
  const axis = svg("g", { class: VISUAL_CLASSES.axis });
  for (const label of gutterLabels(scale)) {
    axis.append(
      svg("line", {
        class: VISUAL_CLASSES.gridline,
        x1: scale.left,
        x2: scale.right,
        y1: label.y,
        y2: label.y
      })
    );
    const text5 = svg("text", {
      class: VISUAL_CLASSES.axisLabel,
      x: scale.pad,
      y: label.y + scale.axisTextSize * 0.35,
      "text-anchor": "start"
    });
    text5.textContent = label.label;
    axis.append(text5);
  }
  for (const tick of series.axis) {
    const x = xForWallClock(scale, tick.position);
    axis.append(
      svg("line", {
        class: VISUAL_CLASSES.gridline,
        x1: x,
        x2: x,
        y1: scale.top,
        y2: scale.bottom
      })
    );
    const text5 = svg("text", {
      class: VISUAL_CLASSES.tickLabel,
      x,
      y: scale.bottom + scale.axisTextSize,
      "text-anchor": tick.position === 0 ? "start" : tick.position === 1 ? "end" : "middle"
    });
    text5.textContent = labels.time(tick.instantMs);
    axis.append(text5);
  }
  root.append(axis);
  const nowLine = svg("line", {
    class: VISUAL_CLASSES.now,
    x1: scale.left,
    x2: scale.left,
    y1: scale.top,
    y2: scale.bottom,
    ...input.now.identity === null ? {} : { "data-now-identity": input.now.identity },
    // SVG has no `hidden` semantics, so absence is the `display` attribute.
    display: "none"
  });
  const selectionLine = svg("line", {
    class: VISUAL_CLASSES.selection,
    x1: scale.left,
    x2: scale.left,
    y1: scale.top,
    y2: scale.bottom,
    display: "none"
  });
  root.append(nowLine, selectionLine);
  const targets = hitTargets(series, scale, input.now.mark);
  const markLayer = svg("g", { class: VISUAL_CLASSES.marks });
  const selectedMs = input.selected?.startMs ?? null;
  const paintOrder = [
    ...targets.filter((target) => target.mark.tomorrow),
    ...targets.filter((target) => !target.mark.tomorrow)
  ];
  for (const target of paintOrder) {
    const classes = markClassNames(target, series);
    const shared = {
      class: classes.join(" "),
      "data-day-index": target.mark.dayIndex,
      "data-start-ms": target.mark.startMs,
      "data-selected": target.mark.startMs === selectedMs ? "true" : "false",
      ...target.current ? { "data-current": "true" } : {}
    };
    if (target.mark.hourly) {
      markLayer.append(
        svg("line", {
          ...shared,
          class: `${VISUAL_CLASSES.segment} ${shared.class}`,
          x1: target.x - target.halfLength,
          x2: target.x + target.halfLength,
          y1: target.y,
          y2: target.y,
          "stroke-width": Math.max(1, target.halfThickness * 2)
        })
      );
      continue;
    }
    markLayer.append(
      svg("circle", {
        ...shared,
        class: `${VISUAL_CLASSES.point} ${shared.class}`,
        cx: target.x,
        cy: target.y,
        r: target.radius
      })
    );
  }
  root.append(markLayer);
  const result = { element: root, targets, scale, nowLine, selectionLine };
  applyFocus(result, selectedMs);
  return result;
}
function applyFocus(drawn, selectedMs) {
  const { nowLine, selectionLine, targets } = drawn;
  const selected = selectedMs === null ? void 0 : targets.find((entry) => entry.mark.startMs === selectedMs);
  if (selected !== void 0) {
    nowLine.setAttribute("display", "none");
    selectionLine.setAttribute("x1", String(selected.x));
    selectionLine.setAttribute("x2", String(selected.x));
    selectionLine.removeAttribute("display");
    return;
  }
  selectionLine.setAttribute("display", "none");
  const current = targets.find((entry) => entry.current);
  if (current === void 0) {
    nowLine.setAttribute("display", "none");
    return;
  }
  nowLine.setAttribute("x1", String(current.x));
  nowLine.setAttribute("x2", String(current.x));
  nowLine.removeAttribute("display");
}
function chartLocalPoint(element8, scale, clientX, clientY) {
  const rect = element8.getBoundingClientRect();
  const width = rect.width === 0 ? scale.width : rect.width;
  const height = rect.height === 0 ? scale.height : rect.height;
  return {
    x: (clientX - rect.left) / width * scale.width,
    y: (clientY - rect.top) / height * scale.height
  };
}

// src/chart-interaction.ts
function nearestIntervalTo(marks, nowMs) {
  let best = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const mark of marks) {
    const distance = nowMs < mark.startMs ? mark.startMs - nowMs : nowMs < mark.endMs ? 0 : nowMs - mark.endMs;
    if (distance < bestDistance) {
      best = mark;
      bestDistance = distance;
    }
  }
  return best;
}
var FALLBACK_SIZE = { width: 320, height: 150 };
var DEFAULT_DIRECTION_THRESHOLD = 4;
function defaultMeasure(viewport) {
  const rect = viewport.getBoundingClientRect();
  const width = rect.width > 0 ? rect.width : FALLBACK_SIZE.width;
  return { width, height: chartHeightForWidth(width) };
}
function defaultObserve(host, onResize) {
  const Constructor = globalThis.ResizeObserver;
  if (typeof Constructor !== "function") {
    return null;
  }
  return new Constructor(onResize);
}
function createChartInteraction(options) {
  const threshold = options.directionThreshold ?? DEFAULT_DIRECTION_THRESHOLD;
  const measure = options.measure ?? defaultMeasure;
  const surfaceOf = options.surface;
  const now = options.now ?? (() => Date.now());
  const isInsideInteractive = options.isInsideInteractive ?? ((node) => node instanceof Node && options.host.contains(node));
  let geometry = null;
  let selected = null;
  let size = options.fallbackSize ?? FALLBACK_SIZE;
  let destroyed = false;
  let observer = null;
  let observing = false;
  let gesture = null;
  function notify() {
    if (destroyed) {
      return;
    }
    options.onSelectionChange(selected);
  }
  function select(mark, notifyChange = true) {
    if (destroyed) {
      return;
    }
    if (selected === null && mark === null) {
      return;
    }
    if (selected !== null && mark !== null && selected.startMs === mark.startMs) {
      return;
    }
    selected = mark;
    if (notifyChange) {
      notify();
    }
  }
  function localPoint(event) {
    if (geometry === null) {
      return null;
    }
    const surface = surfaceOf();
    if (surface === null) {
      return null;
    }
    return chartLocalPoint(surface, geometry.scale, event.clientX, event.clientY);
  }
  function onPointerDown(event) {
    if (destroyed || geometry === null) {
      return;
    }
    const point = localPoint(event);
    if (point === null) {
      return;
    }
    gesture = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      dragging: false,
      captured: false
    };
    const hit = hitTestMarks(geometry.targets, point);
    if (hit !== null) {
      select(hit);
    }
  }
  function onPointerMove(event) {
    if (destroyed || geometry === null || gesture === null || gesture.pointerId !== event.pointerId) {
      return;
    }
    const dx = event.clientX - gesture.startX;
    const dy = event.clientY - gesture.startY;
    if (!gesture.dragging) {
      if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) >= threshold) {
        gesture = null;
        return;
      }
      if (Math.abs(dx) < threshold || Math.abs(dx) <= Math.abs(dy)) {
        return;
      }
      gesture.dragging = true;
      gesture.captured = capture(event, gesture.pointerId);
    }
    event.preventDefault();
    const point = localPoint(event);
    if (point === null) {
      return;
    }
    const walked = nearestMark(geometry.targets, point);
    if (walked !== null) {
      select(walked);
    }
  }
  function capture(event, pointerId) {
    const target = event.currentTarget;
    if (target !== null && typeof target.setPointerCapture === "function") {
      try {
        target.setPointerCapture(pointerId);
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }
  function endGesture(event, cancelled) {
    if (gesture === null || gesture.pointerId !== event.pointerId) {
      return;
    }
    const active = gesture;
    gesture = null;
    if (active.captured) {
      const target = event.currentTarget;
      if (target !== null && typeof target.releasePointerCapture === "function") {
        try {
          target.releasePointerCapture(active.pointerId);
        } catch {
        }
      }
    }
    if (cancelled && !active.dragging) {
      return;
    }
  }
  function onPointerUp(event) {
    endGesture(event, false);
  }
  function onPointerCancel(event) {
    endGesture(event, true);
  }
  function onOutsidePointerDown(event) {
    if (destroyed || selected === null) {
      return;
    }
    if (isInsideInteractive(event.target)) {
      return;
    }
    select(null);
  }
  function onKeydown(event) {
    if (destroyed || geometry === null) {
      return;
    }
    const target = event.target;
    if (target instanceof Node && !options.viewport.contains(target)) {
      return;
    }
    const marks = geometry.marks;
    const step = (direction) => {
      if (direction > 0 && selected === null) {
        return walkMarks(marks, null, 1);
      }
      if (direction < 0 && selected === null) {
        return walkMarks(marks, null, -1);
      }
      return walkMarks(marks, selected, direction);
    };
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        select(step(1));
        break;
      case "ArrowLeft":
      case "ArrowUp":
        select(step(-1));
        break;
      case "Home":
        select(walkMarks(marks, null, 1));
        break;
      case "End":
        select(walkMarks(marks, null, -1));
        break;
      case "Escape":
        select(null);
        break;
      case "Enter":
      case " ":
        select(selected ?? geometry.current ?? nearestIntervalTo(marks, now()));
        break;
      default:
        return;
    }
    event.preventDefault();
  }
  function applySize(width, height) {
    if (destroyed) {
      return;
    }
    if (width === size.width && height === size.height) {
      return;
    }
    size = { width, height };
    options.onResize(size);
  }
  function onObservedResize() {
    if (destroyed) {
      return;
    }
    const measured = measure(options.viewport);
    applySize(measured.width, measured.height);
  }
  options.host.addEventListener("pointerdown", onPointerDown);
  options.host.addEventListener("pointermove", onPointerMove);
  options.host.addEventListener("pointerup", onPointerUp);
  options.host.addEventListener("pointercancel", onPointerCancel);
  options.host.addEventListener("keydown", onKeydown);
  options.host.ownerDocument.addEventListener("pointerdown", onOutsidePointerDown, true);
  const created = (options.observe ?? defaultObserve)(options.viewport, onObservedResize);
  if (created === null) {
    observing = false;
    const measured = measure(options.viewport);
    applySize(measured.width, measured.height);
  } else {
    observing = true;
    observer = created;
    created.observe?.(options.viewport);
  }
  return {
    setGeometry(next) {
      if (destroyed) {
        return;
      }
      geometry = next;
    },
    select(mark) {
      select(mark);
    },
    selection() {
      return selected;
    },
    handleKeydown(event) {
      onKeydown(event);
    },
    size() {
      return size;
    },
    isObserving() {
      return observing;
    },
    destroy() {
      if (destroyed) {
        return;
      }
      destroyed = true;
      gesture = null;
      selected = null;
      options.host.removeEventListener("pointerdown", onPointerDown);
      options.host.removeEventListener("pointermove", onPointerMove);
      options.host.removeEventListener("pointerup", onPointerUp);
      options.host.removeEventListener("pointercancel", onPointerCancel);
      options.host.removeEventListener("keydown", onKeydown);
      options.host.ownerDocument.removeEventListener("pointerdown", onOutsidePointerDown, true);
      observer?.disconnect();
      observer = null;
      observing = false;
      geometry = null;
    }
  };
}

// src/dialog.ts
var FOCUSABLE = "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])";
function ownerDocumentOf(owner) {
  return owner.ownerDocument;
}
function hasInert(element8) {
  return "inert" in element8;
}
function createDialog(options) {
  const { owner, idPrefix, labels, onClose, onDismiss } = options;
  const doc = ownerDocumentOf(owner);
  const overlay = doc.createElement("div");
  overlay.className = VISUAL_CLASSES.overlay;
  overlay.hidden = true;
  const dialog = doc.createElement("div");
  dialog.className = VISUAL_CLASSES.dialog;
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.tabIndex = -1;
  const titleId = `${idPrefix}-dialog-title`;
  const introId = `${idPrefix}-dialog-intro`;
  const title = doc.createElement("h3");
  title.className = VISUAL_CLASSES.dialogTitle;
  title.id = titleId;
  dialog.setAttribute("aria-labelledby", titleId);
  const intro = doc.createElement("p");
  intro.className = `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`;
  intro.id = introId;
  intro.hidden = true;
  const close = doc.createElement("button");
  close.type = "button";
  close.className = VISUAL_CLASSES.dialogClose;
  close.textContent = "×";
  close.setAttribute("aria-label", labels.close);
  const body = doc.createElement("div");
  body.className = VISUAL_CLASSES.dialogBody;
  const header = doc.createElement("div");
  header.className = VISUAL_CLASSES.dialogHeader;
  header.append(title, close);
  dialog.append(header, intro, body);
  overlay.append(dialog);
  owner.append(overlay);
  let open = false;
  let destroyed = false;
  let opener = null;
  let hiddenBackground = null;
  function activeElement() {
    if (owner instanceof ShadowRoot) {
      return owner.activeElement ?? doc.activeElement;
    }
    return doc.activeElement;
  }
  function focusables() {
    return Array.from(dialog.querySelectorAll(FOCUSABLE)).filter(
      (element8) => !element8.hasAttribute("disabled") && element8.tabIndex >= 0
    );
  }
  function setBackgroundHidden(hidden) {
    if (!hidden) {
      const previous2 = hiddenBackground;
      hiddenBackground = null;
      if (previous2 === null) {
        return;
      }
      if (previous2.supported) {
        previous2.element.inert = previous2.inert;
      }
      if (previous2.ariaHidden === null) {
        previous2.element.removeAttribute("aria-hidden");
      } else {
        previous2.element.setAttribute("aria-hidden", previous2.ariaHidden);
      }
      return;
    }
    const element8 = options.background?.() ?? null;
    if (element8 === null || hiddenBackground !== null) {
      return;
    }
    const supported = hasInert(element8);
    const previous = {
      element: element8,
      supported,
      inert: supported ? element8.inert : false,
      ariaHidden: element8.getAttribute("aria-hidden")
    };
    if (supported) {
      element8.inert = true;
    } else {
      element8.setAttribute("aria-hidden", "true");
    }
    hiddenBackground = previous;
  }
  function onBackdropClick(event) {
    if (event.target === overlay) {
      dismiss();
    }
  }
  function onCloseClick() {
    dismiss();
  }
  function dismiss() {
    if (open && !destroyed) {
      onDismiss?.();
    }
    hide();
  }
  function onKeydown(event) {
    if (!open) {
      return;
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      dismiss();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const items = focusables();
    if (items.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const active = activeElement();
    const inside = active !== null && dialog.contains(active) ? active : null;
    if (event.shiftKey) {
      if (inside === null || inside === first) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    if (inside === null || inside === last) {
      event.preventDefault();
      first.focus();
    }
  }
  function hide(options2 = {}) {
    if (!open || destroyed) {
      return;
    }
    open = false;
    overlay.hidden = true;
    doc.removeEventListener("keydown", onKeydown, true);
    setBackgroundHidden(false);
    const previous = opener;
    opener = null;
    if (previous !== null && previous.isConnected && options2.restoreFocus !== false) {
      previous.focus();
    }
    onClose?.();
  }
  function show(input) {
    if (destroyed) {
      return;
    }
    const wasOpen = open;
    title.textContent = input.title;
    const introText = input.intro ?? null;
    intro.hidden = introText === null;
    intro.textContent = introText ?? "";
    if (introText === null) {
      dialog.removeAttribute("aria-describedby");
    } else {
      dialog.setAttribute("aria-describedby", introId);
    }
    body.replaceChildren(input.body);
    if (!wasOpen) {
      opener = input.opener ?? null;
      overlay.hidden = false;
      open = true;
      setBackgroundHidden(true);
      doc.addEventListener("keydown", onKeydown, true);
      const items = focusables();
      if (items.length > 0) {
        items[0].focus();
      } else {
        dialog.focus();
      }
      return;
    }
    if (input.opener !== void 0 && input.opener !== null) {
      opener = input.opener;
    }
  }
  close.addEventListener("click", onCloseClick);
  overlay.addEventListener("click", onBackdropClick);
  return {
    element: overlay,
    show,
    hide,
    isOpen: () => open,
    destroy() {
      if (destroyed) {
        return;
      }
      hide();
      destroyed = true;
      doc.removeEventListener("keydown", onKeydown, true);
      close.removeEventListener("click", onCloseClick);
      overlay.removeEventListener("click", onBackdropClick);
      overlay.remove();
    }
  };
}

// src/format.ts
function hasZone(context) {
  return context.timeZone !== "";
}
var numberFormats = /* @__PURE__ */ new Map();
var dateFormats = /* @__PURE__ */ new Map();
function numberFormat(language, options) {
  const key = `${language}|${JSON.stringify(options)}`;
  const existing = numberFormats.get(key);
  if (existing !== void 0) {
    return existing;
  }
  const created = new Intl.NumberFormat(language, options);
  numberFormats.set(key, created);
  return created;
}
function dateFormat(language, options) {
  const key = `${language}|${JSON.stringify(options)}`;
  const existing = dateFormats.get(key);
  if (existing !== void 0) {
    return existing;
  }
  const created = new Intl.DateTimeFormat(language, options);
  dateFormats.set(key, created);
  return created;
}
function formatFixed(language, value, digits = 1) {
  return numberFormat(language, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  }).format(value);
}
function formatNumber(language, value, digits = 1) {
  return numberFormat(language, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits
  }).format(value);
}
function pricePerKwh(context, value) {
  if (value === null || !Number.isFinite(value)) {
    return "";
  }
  return `${formatNumber(context.language, value)} ${context.unit}/kWh`;
}
function money(context, value) {
  if (value === null || !Number.isFinite(value)) {
    return "";
  }
  const amount = formatNumber(context.language, value, 2);
  return context.majorUnit === null ? amount : `${amount} ${context.majorUnit}`;
}
function energyAmount(language, value) {
  if (value === null || !Number.isFinite(value)) {
    return "";
  }
  return `${formatNumber(language, value, 3)} kWh`;
}
function percentAmount(language, value) {
  return `${formatNumber(language, value, 1)} %`;
}
function clock(context, instantMs2) {
  if (!hasZone(context) || !Number.isFinite(instantMs2)) {
    return "";
  }
  return dateFormat(context.language, {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: context.timeZone
  }).format(new Date(instantMs2));
}
function weekdayDate(context, instantMs2) {
  if (!hasZone(context) || !Number.isFinite(instantMs2)) {
    return "";
  }
  return dateFormat(context.language, {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: context.timeZone
  }).format(new Date(instantMs2));
}
function wallReading(context, instantMs2) {
  return dateFormat("en", {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: context.timeZone
  }).format(new Date(instantMs2));
}
function offsetLabel(context, instantMs2) {
  if (!hasZone(context)) {
    return "";
  }
  return zoneClock(context.timeZone).offsetLabel(instantMs2);
}
function wallTimeRepeats(context, instantMs2) {
  if (!hasZone(context) || !Number.isFinite(instantMs2)) {
    return false;
  }
  const target = wallReading(context, instantMs2);
  for (let minutes = 1; minutes <= 90; minutes += 1) {
    if (wallReading(context, instantMs2 - minutes * 6e4) === target) {
      return true;
    }
    if (wallReading(context, instantMs2 + minutes * 6e4) === target) {
      return true;
    }
  }
  return false;
}
function periodIsAmbiguous(context, startMs, endMs) {
  return wallTimeRepeats(context, startMs) || wallTimeRepeats(context, endMs);
}
function periodLabel(context, startMs, endMs) {
  if (!hasZone(context)) {
    return "";
  }
  const day = weekdayDate(context, startMs);
  const from = clock(context, startMs);
  const to = clock(context, endMs);
  const crosses = localDayKey(startMs, context.timeZone) !== localDayKey(endMs, context.timeZone);
  const end = crosses ? `${weekdayDate(context, endMs)} ${to}` : to;
  const ambiguousStart = wallTimeRepeats(context, startMs);
  const ambiguousEnd = wallTimeRepeats(context, endMs);
  const startSuffix = ambiguousStart ? ` (${offsetLabel(context, startMs)})` : "";
  const endSuffix = ambiguousEnd ? ` (${offsetLabel(context, endMs)})` : "";
  return `${day} ${from}${startSuffix}-${end}${endSuffix}`;
}
function distanceText(language, mil) {
  if (language === "sv" || language === "nb") {
    return `${formatNumber(language, mil, 1)} mil`;
  }
  return `${formatNumber(language, mil * 10, 0)} km`;
}
function referenceWeekday(isoWeekday) {
  return Date.UTC(2024, 0, isoWeekday, 12);
}
function dateLabel(language, isoDate) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate);
  if (match === null) {
    return "";
  }
  const instant2 = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12);
  return dateFormat(language === "en" ? "en-GB" : language, {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC"
  }).format(new Date(instant2));
}
function departureDayLabel(language, isoDate, today, words) {
  const label = dateLabel(language, isoDate);
  if (label === "") {
    return null;
  }
  if (today === null) {
    return label;
  }
  if (isoDate < today) {
    return null;
  }
  if (isoDate === today) {
    return words.today;
  }
  const next = /* @__PURE__ */ new Date(`${today}T12:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return isoDate === next.toISOString().slice(0, 10) ? words.tomorrow : label;
}
function weekdayPlural(language, isoWeekday) {
  if (!Number.isInteger(isoWeekday) || isoWeekday < 1 || isoWeekday > 7) {
    return null;
  }
  const name = dateFormat(language, { weekday: "long", timeZone: "UTC" }).format(new Date(referenceWeekday(isoWeekday))).toLocaleLowerCase(language);
  switch (language) {
    case "sv":
      return `${name}ar`;
    case "nb":
      return `${name}er`;
    case "da":
      return `${name}e`;
    case "fi":
      return name.endsWith("i") ? `${name}sin` : `${name}isin`;
    default:
      return `${name.charAt(0).toLocaleUpperCase(language)}${name.slice(1)}s`;
  }
}

// src/i18n/da.ts
var da = {
  "card.title": "SpotNav",
  "state.loading": "Læser ladeplanen…",
  "state.unconfigured": "Vælg én SpotNav-lader i kortets editor.",
  "state.addCharger": "Tilføj en lader: Indstillinger → Enheder og tjenester → SpotNav → Tilføj post → Lader; den tilbyder at blive en del af dette anlæg.",
  "state.noChargers": "Ingen SpotNav-lader er sat op i denne Home Assistant endnu.",
  "state.requestFailed": "Anmodningen mislykkedes. Kontrollér forbindelsen til Home Assistant, og prøv igen.",
  "state.unsupported": "Kortet og integrationen taler forskellige API-versioner. Opdatér begge, så de passer sammen.",
  "state.malformed": "Integrationen svarede med noget, som kortet ikke kan læse. Opdater begge, så de passer sammen.",
  "state.chargerMissing": "Laderen er ukendt, ikke indlæst eller et anlæg. Der blev ikke valgt noget i stedet.",
  "state.retry": "Prøv igen",
  "state.noPrices": "Der er endnu ingen priser for denne lader.",
  "status.externalInstalled": "Eksternt skema: næste opladning kl. {time}.",
  "status.externalNoSchedule": "Ekstern planlægning er aktiv; der er ikke installeret et skema.",
  "status.chargingNow": "Lader nu; planlagt til {time}.",
  "status.autoPlanned": "Planlagt fra {time}.",
  "status.autoInstalled": "Opladning er planlagt fra {time}.",
  "status.heldUntilWindow": "Opladningen venter til den planlagte start kl. {time}.",
  "status.proposalPending": "Et nyt opladningsforslag er klar.",
  "status.proposalPendingAt": "En ny plan er klar og installeres, når det igangværende opladningsvindue slutter kl. {time}.",
  "status.waitingForTomorrow": "Venter på morgendagens priser.",
  "status.waitingForPublication": "Venter på morgendagens priser (~{time}), planlægger derefter.",
  "status.waitingForHistory": "Venter: {weekday} har været {percent} % billigere de seneste {weeks} uger.",
  "status.waitingForHistoryNoDetail": "Venter på timer, der plejer at være billigere, planlægger derefter.",
  "status.waitingForPublicationNoTime": "Venter på morgendagens priser, planlægger derefter.",
  "status.buyingBeforePublication": "Køber {kwh} kWh nu, resten når priserne er offentliggjort.",
  "status.noPlan": "Der kunne ikke beregnes en ladeplan lige nu.",
  "status.loadBalancingLimited": "Opladningen begrænses af anlæggets lastbalancering lige nu.",
  "status.loadBalancingLimitedTo": "Opladningen begrænses til {limit} A af anlæggets lastbalancering.",
  "status.loadBalancingLimitedByBattery": "Hjemmebatteriet oplader fra nettet og deler hovedsikringen: bilen får {limit} A.",
  "status.loadBalancingLimitedByHouse": "Husets forbrug begrænser bilen til {limit} A.",
  "status.chargingNowOpen": "Lader nu.",
  "status.scheduledNoTime": "Opladning er planlagt.",
  "status.nothingToCharge": "Der er intet at lade lige nu.",
  "status.pausedShort": "Automatisk opladning er sat på pause.",
  "status.planEnergy": "{kwh} kWh",
  "status.planCost": "{cost}",
  "status.targetStopped": "Stoppet ved {soc} %",
  "status.targetStoppedAge": "Stoppet ved {soc} % (aflæsningen er {age} gammel)",
  "status.targetStoppedNow": "Stoppet ved {soc} % (lige nu)",
  "status.targetStoppedEstimate": "Stoppet ved {soc} % (anslået)",
  "status.targetStoppedEstimateAge": "Stoppet ved {soc} % (anslået, aflæsningen er {age} gammel)",
  "status.targetAgeMinutes": "{n} min",
  "status.targetAgeHours": "{n} t",
  "issue.targetUnverifiable": "Målet kan ikke kontrolleres lige nu.",
  "status.planDistance": "{distance}",
  "issue.priceDegraded": "Prisdata er ufuldstændige.",
  "issue.banner.blocking": "Noget skal ordnes, før opladning kan planlægges.",
  "issue.banner.notice": "Godt at vide.",
  "issue.chargerMissing": "Den valgte lader kan ikke bruges: den er ukendt, ikke indlæst eller et anlæg.",
  "issue.chargeControlMissing": "Ladestyringen {entity} findes ikke længere. Den er sandsynligvis omdøbt eller fjernet: vælg laderens styring igen i dens indstillinger.",
  "issue.chargeControlDisabled": "Ladestyringen {entity} er deaktiveret i Home Assistant, så SpotNav kan ikke starte eller stoppe laderen. Aktivér den igen.",
  "issue.unsupported": "Kortet og integrationen taler forskellige API-versioner.",
  "issue.malformed": "Svaret fra backend var ikke et gyldigt API v2-svar.",
  "status.finishSetupArea": "Færdiggør opsætningen: vælg et prisområde i Indstillinger.",
  "status.finishSetup": "Færdiggør opsætningen i Indstillinger: {fields}.",
  "status.settingsSuggested": "Foreslået ud fra din placering og lader – tjek Indstillinger.",
  "status.missing.area": "prisområde",
  "status.missing.phases": "faser",
  "status.missing.amps": "ladestrøm",
  "status.missing.vehicle": "køretøj",
  "status.missing.target_percent": "målniveau",
  "issue.incompleteSettings": "Færdiggør opsætningen: nogle indstillinger mangler stadig. Åbn Indstillinger.",
  "issue.missingSettings": "Disse indstillinger mangler: {fields}.",
  "issue.planningUnavailable": "Der kunne ikke beregnes en ladeplan med de data, der er tilgængelige lige nu.",
  "issue.solarUnavailable": "Soloplading er ikke tilgængelig: dette anlæg kan ikke måle soloverskud.",
  "issue.priceHorizon": "Priser mangler for en del af planlægningsvinduet. Angiv et afrejsetidspunkt.",
  "issue.error": "Den seneste beregning mislykkedes.",
  "issue.priceUnavailable": "Der er ingen brugbare prisdata lige nu.",
  "issue.priceInvalid": "Prisdataene er ugyldige.",
  "issue.priceStale": "Priserne er forældede: den seneste vellykkede hentning bruges stadig.",
  "issue.unpriced": "En del af planen lader uden offentliggjorte priser.",
  "issue.chargingWithoutPrices": "Lader uden offentliggjorte priser for at overholde sluttidspunktet.",
  "issue.loadBalancing": "Lastbalancering er ikke tilgængelig for denne lader.",
  "issue.heldByCharger": "Laderens eget skema eller lastbalancering holder opladningen tilbage, så den er ikke startet.",
  "issue.chargerDisabled": "Laderens egen aktiveringskontakt er slået fra, så den kan ikke starte. Slå den til i laderens indstillinger.",
  "issue.holdOverridden": "Opladningen blev startet uden for planen og må fortsætte.",
  "status.siteMeasurement.stale.other": "{phases} er ældre end {seconds} s.",
  "status.siteMeasurement.stale.one": "{phases} er ældre end {seconds} s.",
  "status.siteMeasurement.noValue.other": "{phases} har ingen værdi{where}.",
  "status.siteMeasurement.noValue.one": "{phases} har ingen værdi{where}.",
  "issue.duplicateCharger": "{other} og denne lader er den samme fysiske lader. To SpotNav-ladere på én lader sender modstridende kommandoer, så behold kun én: fjern den anden under Indstillinger → Enheder og tjenester → SpotNav. SpotNav fjerner aldrig en for dig.",
  "issue.siteMeasurement": "Anlæggets måling kan ikke bruges lige nu.",
  "issue.unknown": "Backend rapporterede noget, kortet endnu ikke kender.",
  "header.info": "Om kortet",
  "header.settings": "Kortindstillinger",
  "header.history": "Ladehistorik",
  "history.title": "Ladehistorik",
  "history.titleNamed": "Ladehistorik · {name}",
  "history.loading": "Henter ladehistorikken…",
  "history.failed": "Ladehistorikken kunne ikke læses.",
  "history.intro": "Hvad hver opladning kostede: spotprisen plus energiafgift, nettarif og moms, da energien blev leveret.",
  "history.thisMonth": "Denne måned",
  "history.lastMonth": "Sidste måned",
  "history.noneInMonth": "Ingen opladninger.",
  "history.days": "Dage",
  "history.months": "Måneder",
  "history.listLabel": "Vis pr.",
  "history.sessions.one": "{count} opladning",
  "history.sessions.other": "{count} opladninger",
  "history.solar": "{percent} sol",
  "history.estimated": "anslået energi",
  "history.noCost": "ingen pris",
  "history.savings.saved": "Anslået besparelse: {amount} mod dagens gennemsnitspris",
  "history.savings.extra": "Anslået {amount} mere end dagens gennemsnitspris",
  "history.savings.note": "Besparelsen er et skøn: samme energi til hver dags gennemsnitspris.",
  "history.latest": "Seneste opladninger",
  "history.empty": "Ingen opladninger er gemt endnu. De vises her efter næste opladning.",
  "history.open": "Lader nu siden {time}: {energy}",
  "history.by.plan_window": "planlagt vindue",
  "history.by.manual": "startet manuelt",
  "history.by.solar": "solenergioverskud",
  "history.by.hybrid": "hybrid",
  "history.by.other": "startet andetsteds",
  "history.export.period": "Periode",
  "history.range.thisMonth": "Denne måned",
  "history.range.lastMonth": "Sidste måned",
  "history.range.last12": "Seneste 12 måneder",
  "history.range.all": "Alt",
  "history.export": "Eksportér CSV",
  "history.exportFailed": "Eksporten mislykkedes.",
  "settings.overview.title": "Kortindstillinger",
  "settings.section.market": "Elområde og afgifter",
  "settings.section.vehicle": "Køretøj",
  "settings.section.capabilities": "Sensorer og funktioner",
  "settings.section.site": "Anlæg",
  "settings.section.configure": "Rediger",
  "settings.value.off": "Fra",
  "settings.fiscal.vat": "Moms",
  "settings.fiscal.tax": "Elafgift",
  "settings.fiscal.transfer": "Nettarif",
  "settings.consumption.unit": "kWh/10 km",
  "settings.section.entities": "Lader",
  "entity.group.site": "Anlæggets entiteter",
  "entity.notSet": "Ikke angivet",
  "entity.edit.charger": "Skift laderens entiteter",
  "entity.edit.site": "Skift anlæggets entiteter",
  "entity.editor.charger": "Laderens entiteter",
  "entity.editor.site": "Anlæggets entiteter",
  "entity.loading": "Læser entiteterne…",
  "entity.adminOnly": "Kun administratorer kan ændre entiteter.",
  "entity.error.read": "Entiteterne kunne ikke læses.",
  "entity.field.chargeControl": "Afbryder til ladestyring",
  "entity.field.currentLimit": "Strømgrænse",
  "entity.field.energyRegister": "Energitæller",
  "entity.field.powerEntity": "Effektsensor (smart stik)",
  "entity.field.vehicleSoc": "Køretøjets opladningsniveau",
  "entity.field.mainFuse": "Hovedsikring",
  "entity.field.safetyMargin": "Sikkerhedsmargin",
  "entity.field.measurementMode": "Målemetode",
  "entity.field.voltageBetweenPhases": "Spænding mellem faser",
  "entity.field.chargerPriority": "Ladererens prioritet",
  "entity.field.batteryPower": "Batteriets effekt",
  "entity.field.maxAge": "Højeste måleralder",
  "entity.mode.direct": "Fasestrømme måles direkte",
  "entity.mode.derived": "Fasestrømme beregnes ud fra effekt og spænding",
  "entity.phase.direct": "Strøm, {phase}",
  "entity.derived.power": "Effekt",
  "entity.derived.reactivePower": "Reaktiv effekt",
  "entity.derived.voltage": "Spænding",
  "entity.error.field.required": "Vælg en entitet.",
  "entity.error.field.notFound": "Entiteten findes ikke.",
  "entity.error.field.wrongDomain": "Det er den forkerte type entitet.",
  "entity.error.field.invalid": "Værdien accepteres ikke.",
  "entity.error.field.notWritable": "Det kan ikke ændres her.",
  "entity.error.field.controlPathUnknown": "SpotNav kan ikke afgøre, hvordan en opladning startes og stoppes med den enhed. Vælg laderens kontakt, eller en vælger med tydelige start- og stopvalg.",
  "entity.error.field.unknown": "Feltet genkendes ikke.",
  "entity.error.field.chargeControlInUse": "En anden lader bruger allerede denne afbryder.",
  "entity.error.field.currentLimitInUse": "En anden lader bruger allerede denne strømgrænse.",
  "entity.error.field.duplicateCharger": "En anden SpotNav-lader er allerede den samme lader.",
  "entity.error.conflict": "Ændret et andet sted. De aktuelle værdier vises, og intet blev gemt.",
  "entity.error.invalid": "Nogle værdier blev ikke accepteret. Intet blev gemt.",
  "entity.error.generic": "Entiteterne kunne ikke ændres. Intet er ændret.",
  "entity.error.notAdmin": "Kun administratorer kan ændre entiteter.",
  "entity.missing.optional": "Denne entitet findes ikke længere i Home Assistant. Ryd feltet, eller vælg en anden entitet.",
  "entity.missing.required": "Denne entitet findes ikke længere i Home Assistant. Vælg en anden entitet.",
  "entity.automatic": "Automatisk: {name}",
  "entity.help.chargeControl": "Kontakten der starter og stopper opladningen. SpotNav tænder og slukker den efter planen.",
  "entity.help.currentLimit": "Entiteten der viser laderens indstillede strøm. Automatisk bruger den, SpotNav selv finder, for OCPP 0.12-ladere sessionsgrænsen.",
  "entity.help.energyRegister": "Laderens akkumulerede kWh-måler. SpotNav bruger den til at vide, hvad der allerede er ladet. Findes automatisk for OCPP-ladere.",
  "entity.help.powerEntity": "Til en lader bag et smart stik: stikkets effektsensor, i W eller kW. SpotNav tæller energien fra den og kan se, når bilen er holdt op med at trække strøm. Stikket skal være dimensioneret til laderens kontinuerlige strøm.",
  "entity.help.vehicleSoc": "Køretøjets opladningsniveau, læst fra den sensor der er valgt til køretøjet, eller fundet automatisk når det kun har én.",
  "entity.help.mainFuse": "Anlæggets hovedsikring i ampere. Alle ladere på anlægget holder sig samlet under den.",
  "entity.help.safetyMargin": "Strømmen i ampere, som SpotNav holder fri under hovedsikringen. Opladningen holdes under sikringen minus marginen, som derfor skal være lavere end sikringen.",
  "entity.help.measurementMode": "Om din måler angiver hver fases strøm direkte, eller SpotNav regner den ud fra effekt og spænding.",
  "entity.help.voltageBetweenPhases": "Spændingen mellem to faser i din elinstallation. Trefaset ladeeffekt regnes ud fra den.",
  "entity.help.chargerPriority": "Når flere ladere deler anlæggets sikring, får en lader sat til Først strøm før de andre, og en sat til Sidst får det, der er tilbage.",
  "entity.voltage.tn": "400 V (TN-net, det sædvanlige)",
  "entity.voltage.it": "230 V (IT-net, almindeligt i Norge)",
  "entity.priority.first": "Først",
  "entity.priority.normal": "Normal",
  "entity.priority.last": "Sidst",
  "entity.help.batteryPower": "En sensor for hjemmebatteriets effekt, så SpotNav kan tage højde for batteriet.",
  "entity.help.maxAge": "Hvor gammel en måling må være, i sekunder, før SpotNav holder op med at stole på den.",
  "entity.help.phaseDirect": "Sensoren der måler strømmen på denne fase, i ampere.",
  "entity.help.derivedPower": "Sensoren for aktiv effekt på denne fase.",
  "entity.help.derivedReactivePower": "Sensoren for reaktiv effekt på denne fase.",
  "entity.help.derivedVoltage": "Sensoren for spænding på denne fase.",
  "entity.flag.on": "Til",
  "entity.flag.off": "Fra",
  "entity.field.siteCurrentSigned": "Netstrømmen har fortegn",
  "entity.field.gridPowerInverted": "Sensoren viser eksport som positiv",
  "entity.field.gridPowerSource": "Nettets samlede effekt (til sol)",
  "entity.field.gridPowerSourceExport": "Samlet eksporteffekt (hvis adskilt)",
  "entity.field.batteryDischargePower": "Batteriets afladningseffekt",
  "entity.field.batteryPowerInverted": "Sensoren viser afladning som positiv",
  "entity.derived.powerExport": "Eksporteffekt",
  "entity.derived.apparentPower": "Tilsyneladende effekt",
  "entity.derived.current": "Strøm",
  "entity.help.siteCurrentSigned": "Slå til, hvis måleren angiver negativ strøm ved eksport. SpotNav bruger så strømmens størrelse, som er det, der belaster sikringen, i stedet for at afvise den.",
  "entity.help.gridPowerInverted": "Slå til, hvis målerens effekt er positiv ved eksport (Huawei, SolarEdge, GoodWe og lignende). SpotNav læser så import som positiv. Det gælder også nettets samlede effekt.",
  "entity.help.gridPowerSource": "Kræves til sol- og hybridopladning, når faserne kun angiver strøm.",
  "entity.help.gridPowerSourceExport": "Kun hvis måleren angiver import og eksport som to sensorer: feltet ovenfor er så importen, og dette er eksporten.",
  "entity.help.batteryPowerInverted": "Slå til, hvis batteriets effekt er positiv ved afladning (Tesla, Fronius, Enphase, GoodWe og lignende). SpotNav læser så opladning som positiv.",
  "entity.help.batteryDischargePower": "Kun for et batteri, der angiver opladning og afladning som to sensorer: dette er afladningen, og batterisensoren ovenfor er opladningen.",
  "entity.help.derivedPowerExport": "Kun når måleren angiver import og eksport som to sensorer: effektsensoren ovenfor er så import, og denne er eksport.",
  "entity.help.derivedApparentPower": "Tilsyneladende effekt på denne fase, i VA. Med den bliver strømmen nøjagtig.",
  "entity.help.derivedCurrent": "Målerens egen strøm på denne fase. Med den bliver strømmen nøjagtig.",
  "entity.choice.mixed": "Mere end én af disse er angivet. Kun den valgte beholdes; de andre ryddes, når du gemmer.",
  "entity.choice.choose": "Vælg en entitet",
  "entity.limit.none": "Ingen (SpotNav indstiller ikke strømmen)",
  "entity.grid.title": "Nettets effekt",
  "entity.grid.one": "Én sensor med retning",
  "entity.grid.two": "Import og eksport som to sensorer",
  "entity.current.title": "Strømmen hentes fra",
  "entity.current.measured": "Målerens egen strøm",
  "entity.current.apparent": "Tilsyneladende effekt",
  "entity.current.reactive": "Reaktiv effekt",
  "entity.current.estimated": "Anslået (effektfaktor 0,9)",
  "entity.current.estimatedNote": "SpotNav anslår strømmen ud fra effekten. Anslaget er markeret i kortet.",
  "entity.battery.title": "Batteri",
  "entity.battery.none": "Intet",
  "entity.battery.one": "Én sensor",
  "entity.battery.two": "Opladning og afladning som to sensorer",
  "entity.energy.title": "Energi",
  "entity.energy.meter": "Energitæller (kWh)",
  "entity.energy.power": "Effekt (W) — SpotNav beregner energien",
  "entity.energy.none": "Ingen",
  "entity.notice.estimated": "Strømmen er anslået ud fra effekten med effektfaktor {pf} eller bedre som forudsætning. Anslaget er aldrig lavere end den virkelige strøm ved den effektfaktor eller bedre, og undervurderer den under det. Tilføj målerens strøm, tilsyneladende effekt eller reaktive effekt for en nøjagtig værdi.",
  "entity.notice.estimatedShort": "Anslået",
  "entity.warning.updateInterval": "{integration} opdaterer hvert {seconds} s, langsommere end højeste måleralder.",
  "entity.warning.updateIntervalOption": 'Sænk det med "{option}" i den integration.',
  "entity.warning.onChange": "{integration} rapporterer kun, når en værdi ændres, så en stabil værdi kan se gammel ud. Hæv højeste måleralder, hvis anlægget ofte regnes som forældet.",
  "entity.warning.ownBalancing": "{name} ({integration}) balancerer last selv og kan modvirke SpotNavs aktive styring. Brug en af dem.",
  "entity.warning.externalBalancer": "{integration} balancerer strømmen for {name} selv, så SpotNav starter og stopper laderen, men skriver ikke dens strøm.",
  "entity.warning.unknown": "Anlægget har en meddelelse, som denne version ikke kan vise. Opdater SpotNav.",
  "entity.detect.title": "Fundet i Home Assistant",
  "entity.detect.intro": "Disse blev genkendt i din opsætning. Intet ændres, før du trykker på Brug.",
  "entity.site.intro": "Måling for anlægget. {applies}",
  "entity.detect.meters": "Elmålere",
  "entity.detect.batteries": "Hjemmebatterier",
  "entity.detect.use": "Brug",
  "entity.detect.inUse": "I brug",
  "entity.detect.direct": "Måler fasestrømmen direkte",
  "entity.detect.derived": "Udledt af effekt og spænding",
  "entity.detect.signed": "Strøm med fortegn: læses som sin størrelse",
  "entity.detect.inverted": "Effekt med eksport som positiv: negeres",
  "entity.detect.gridPower": "Nettets samlede effekt fundet: bruges til sol- og hybridopladning",
  "entity.detect.estimated": "Anslået strøm: ingen strøm, tilsyneladende eller reaktiv effekt fundet",
  "entity.detect.enable": "Aktiverer {count} entiteter, som integrationen leverer deaktiveret.",
  "entity.detect.battery.inverted": "Afladning som positiv: negeres",
  "entity.detect.battery.pair": "Opladning og afladning er to sensorer",
  "entity.detect.confidence.low": "Lav sikkerhed: kontroller, at fortegnene passer.",
  "entity.detect.warning.own_load_balancing": "Denne enhed balancerer last selv.",
  "entity.detect.warning.sign_unverified": "Effektens fortegn er ukendt: kontroller, at eksport er negativ.",
  "entity.detect.warning.voltage_from_other_device": "Måleren rapporterer ingen spænding, så fasespændingen fra en anden enhed, som regel vekselretteren, bruges. Det er normalt.",
  "entity.detect.warning.may_measure_subcircuit": "Kontroller, at måleren måler hele hovedtilførslen, ikke en delkreds.",
  "entity.detect.warning.reports_on_change_only": "Rapporterer kun, når en værdi ændres.",
  "market.edit": "Skift prisområde og afgifter",
  "settings.vehicle.change": "Skift køretøj",
  "settings.vehicle.none": "Der er endnu ikke sat noget køretøj op.",
  "settings.vehicle.dialogTitle": "Køretøj · {name}",
  "settings.vehicle.sensorLegend": "Sensor for opladningsniveau",
  "entity.foundAutomatically": "Findes automatisk",
  "site.row.battery": "Batteri",
  "settings.value.none": "Ingen",
  "settings.section.solar": "Sol",
  "site.solar.change": "Skift solindstillinger",
  "site.solar.dialogTitle": "Solindstillinger",
  "strategy.setupSolar": "Indstil sol under Indstillinger",
  "strategy.setupSite": "Åbn anlæggets indstillinger",
  "site.none": "Denne lader har intet anlæg.",
  "site.applies.one": "Gælder den ene lader på dette anlæg.",
  "site.applies.other": "Gælder alle {count} ladere på dette anlæg.",
  "site.solarPriority.title": "Solprioritet",
  "site.solarPriority.carFirst": "Bilen først",
  "site.solarPriority.batteryFirst": "Batteriet først",
  "site.solarForecast.title": "Kilder til solprognose",
  "site.solarForecast.none": "Ingen valgt. Uden en kilde planlægger hybrid en opladning som Billigst.",
  "site.activeControl.title": "Aktiv lastbalancering",
  "site.activeControl.on": "Til",
  "site.activeControl.off": "Fra",
  "site.activeControl.available": "Tilgængelig",
  "site.activeControl.reason.duplicateMembership": "Ikke tilgængelig: denne lader tilhører mere end ét anlæg.",
  "site.activeControl.reason.noCommandableCharger": "Ikke tilgængelig: ingen lader på anlægget tager imod et strømkommando.",
  "site.activeControl.reason.measurement": "Ikke tilgængelig: anlæggets egen effektmåling er ikke i orden.",
  "site.activeControl.note": "Bedste indsats, ingen beskyttelsesanordning. Når den slås fra, får laderen den strøm tilbage, som var sænket.",
  "site.activeControl.pending": "Gemmer…",
  "site.activeControl.error.unavailable": "Lastbalanceringen kan ikke slås til lige nu: anlægget kan ikke styres aktivt i øjeblikket. Intet blev ændret.",
  "site.activeControl.error.confirmation": "Ændringen kunne ikke bekræftes, så den tidligere indstilling blev beholdt.",
  "site.activeControl.restore.off": "Lastbalanceringen er slået fra.",
  "site.activeControl.restore.notNeeded": "Ingen lader havde brug for at få sin strøm tilbage.",
  "site.activeControl.restore.restoredOne": "Laderen blev genoprettet til {to} A.",
  "site.activeControl.restore.restoredNamed": "{name} blev genoprettet til {to} A.",
  "site.activeControl.restore.unnamed": "En lader",
  "site.activeControl.restore.line": "{name}: {text}",
  "site.activeControl.restore.failed.write_failed": "Laderen afviste kommandoen om at genoprette strømmen og kan stadig være begrænset.",
  "site.activeControl.restore.failed.unconfirmed": "Laderen bekræftede ikke den genoprettede strøm og kan stadig være begrænset.",
  "site.activeControl.restore.failed.assigned_current_unreadable": "Laderens strømgrænse kunne ikke læses, og laderen kan stadig være begrænset.",
  "site.activeControl.restore.failed.no_authoritative_current": "SpotNav har ingen ønsket strøm at sætte laderen tilbage til, og den kan stadig være begrænset.",
  "site.activeControl.restore.failed.charger_not_loaded": "Laderen er ikke indlæst i Home Assistant og kan stadig være begrænset.",
  "site.activeControl.restore.failed.membership_conflict": "Laderen tilhører mere end ét anlæg og blev ikke rørt, så den kan stadig være begrænset.",
  "site.activeControl.restore.failed.probe_in_flight": "Laderen var optaget af en strømtest og kan stadig være begrænset.",
  "site.activeControl.restore.failed.below_minimum": "Strømmen, der skulle genoprettes, er lavere end laderen accepterer, og den kan stadig være begrænset.",
  "site.activeControl.restore.failed.external_balancer": "En anden integration balancerer laderens strøm, så SpotNav skrev den ikke, og den kan stadig være begrænset.",
  "site.activeControl.restore.failed.no_connector_target": "SpotNav kunne ikke finde laderens stik at styre, og laderen kan stadig være begrænset.",
  "site.activeControl.restore.failed.unknown": "Laderen kunne ikke genoprettes og kan stadig være begrænset.",
  "site.error.conflict": "Ændret et andet sted. De aktuelle værdier vises.",
  "settings.consumption.label": "Forbrug",
  "settings.value.unset": "Ikke angivet",
  "issue.count.one": "{count} punkt at se på",
  "issue.count.other": "{count} punkter at se på",
  "graph.title": "Ladeplan",
  "graph.description": "Prisgraf fra {from} til {to}, i {unit} pr. kWh. {count} intervaller over {days} dage. Valgt: {selected}.",
  "graph.descriptionNoSelection": "Intet valgt.",
  "graph.descriptionNow": "Aktuelt interval: {now}.",
  "graph.legend.today": "I dag",
  "graph.legend.tomorrow": "I morgen",
  "graph.legend.past": "Tidligere dag",
  "graph.legend.current": "Aktuelt interval",
  "graph.legend.selection": "Valgt interval",
  "graph.legend.cheap": "Billigere end dagens gennemsnit",
  "graph.legend.expensive": "Dyrere end dagens gennemsnit",
  "graph.legend.installed": "Planlagt",
  "graph.legend.proposal": "Forslag",
  "graph.summary.max": "Maks",
  "graph.summary.min": "Min",
  "graph.summary.current": "Nu",
  "graph.readout": "{day} {time} · {price}",
  "graph.readoutMissing": "{day} {time} · ingen pris",
  "graph.readoutWithOffset": "{day} {time} ({offset}) · {price}",
  "graph.readoutMissingWithOffset": "{day} {time} ({offset}) · ingen pris",
  "graph.selected": "Valgt pris",
  "graph.priceAxis": "Pris, {unit} per kWh",
  "graph.noZone": "Tider er ikke tilgængelige: integrationen har ikke rapporteret en markedszone for denne lader.",
  "graph.hint": "Brug piletasterne til at gå gennem intervallerne.",
  "graph.keyboardInstructions": "Prisgraf. Piletasterne flytter mellem intervaller, Home og End springer til enderne, Escape rydder valget.",
  "plan.proposal.one": "Billigste ladeperiode ({count})",
  "plan.proposal.other": "Billigste ladeperioder ({count})",
  "plan.installed.one": "Planlagt ladeperiode ({count})",
  "plan.installed.other": "Planlagte ladeperioder ({count})",
  "plan.period": "{day} {from}–{to}",
  "plan.periodWithOffset": "{day} {from}–{to} ({offset})",
  "plan.cost": "Omkostning",
  "plan.energy": "Energi",
  "plan.distance": "Afstand",
  "plan.power": "Effekt",
  "plan.activeNow": "Nu",
  "plan.missing": "ukendt",
  "cap.title": "Hvad laderen kan",
  "cap.intro": "Taget fra det, integrationen rapporterer for denne lader. Det, der ikke er tilgængeligt her, kan kortet ikke tænde.",
  "cap.autoPrice": "Automatisk prisplanlægning",
  "cap.currentLimit": "Dynamisk strømgrænse",
  "cap.loadBalancing": "Lastbalancering",
  "cap.targetSoc": "Bilens ladetilstand og mål",
  "cap.available": "Tilgængelig",
  "cap.unavailable": "Ikke tilgængelig for denne lader",
  "action.start": "Start nu",
  "action.stop": "Stop",
  "action.resume": "Genoptag automatisk opladning",
  "action.startHelp": "Starter en opladning nu. Den lover ingen varighed: Auto kan tage over igen ved næste afstemning.",
  "action.pauseAutomatic": "Pause automatisk opladning",
  "action.pauseAutomaticShort": "Pause",
  "action.resumeShort": "Genoptag",
  "pause.sheetTitle": "Pause automatisk opladning",
  "pause.intro": "Vælg hvor længe pausen varer. Kun de tidsstyrede valg slutter af sig selv.",
  "pause.nextPeriod": "Indtil næste planlagte periode",
  "pause.untilTomorrow": "Indtil i morgen",
  "pause.untilResumed": "Indtil jeg genoptager",
  "strategy.title": "Strategi",
  "bar.charging": "Opladning",
  "bar.schedule": "Tidsplan",
  "bar.start": "Start",
  "bar.waiting": "Starter…",
  "bar.stop": "Stop",
  "bar.chargeNow": "Oplad nu",
  "bar.chargingNow": "Oplader",
  "bar.scheduleActive": "Tidsplan aktiv",
  "bar.schedulePaused": "Tidsplan pauset",
  "bar.state.notCharging": "oplader ikke",
  "bar.state.charging": "oplader",
  "bar.state.scheduleActive": "aktiv",
  "bar.state.schedulePaused": "pauset",
  "bar.change": "ændre",
  "strategy.dialogTitle": "Opladningsstrategi",
  "strategy.intro": "Hvad planen optimerer. En strategi, der ikke er tilgængelig her, kan ikke vælges endnu.",
  "strategy.cheapest": "Billigst",
  "strategy.solar": "Sol",
  "strategy.hybrid": "Hybrid",
  "strategy.reason.solar": "Kræver måling af soloverskud",
  "strategy.reason.hybrid": "Kræver sol- og prisstyring",
  "strategy.reason.totalPower": "Sol kræver målerens samlede effekt på nettet",
  "strategy.status.solar.charging": "Sol · lader med {amps} A fra overskud",
  "strategy.status.solar.chargingUnknown": "Sol · lader fra overskud",
  "strategy.status.solar.arming": "Sol · overskud fundet, starter snart",
  "strategy.status.solar.disarming": "Sol · overskuddet aftager, stopper snart",
  "strategy.status.solar.noReadingStopped": "Sol · stoppet, ingen brugbar måling",
  "strategy.status.solar.noReadingWaiting": "Sol · ingen brugbar måling endnu",
  "strategy.status.solar.waitingForSun": "Sol · venter på sol",
  "strategy.status.solar.unknown": "Sol · status ukendt",
  "strategy.status.hybrid.grid": "Hybrid · {grid} kWh fra elnettet{window}",
  "strategy.status.hybrid.creditSuffix": ", {credit} kWh forventes fra solen",
  "strategy.status.hybrid.noForecast": "Hybrid · ingen prognosekilde — planlægger som Billigst",
  "strategy.status.hybrid.noPriceData": "Hybrid · venter på prisdata",
  "strategy.status.hybrid.satisfied": "Hybrid · ladebehovet er allerede opfyldt",
  "strategy.status.hybrid.unknown": "Hybrid · planlægger",
  "advisory.vehicleNotRequestingCurrent": "Opladningen blev startet, men køretøjet anmoder ikke om strøm. Kontrollér køretøjets opladningsindstillinger, eller tilslut kablet igen.",
  "advisory.powerBelowThreshold": "Opladningen blev startet, men laderen trækker næsten ingen effekt. Bilen kan være færdig eller lader ikke: kontrollér køretøjets opladningsindstillinger, eller tilslut kablet igen.",
  "control.noSettings": "Denne lader har endnu ingen indstillinger, så der er intet at starte eller stoppe.",
  "control.pauseUnsettled": "En pause er gemt, men er ikke trådt i kraft endnu. Intet anvendes.",
  "control.pauseClearFailed": "En pause er udløbet, men kunne ikke ryddes, så intet anvendes.",
  "control.pauseStopFailed": "Laderen kunne ikke stoppes, da pausen blev accepteret. Prøv Stop igen.",
  "control.startNotAcknowledged": "Laderen bekræftede ikke startkommandoen. Prøv igen.",
  "control.reconcileFailed": "Den seneste ændring blev gemt, men planen kunne ikke opdateres.",
  "control.executionError": "Integrationen rapporterede et problem med den seneste kørsel.",
  "control.pausedUntil": "Sat på pause til {time}.",
  "control.pausedIndefinitely": "Sat på pause, indtil du genoptager.",
  "control.actionPending": "En startkommando afventer laderens bekræftelse.",
  "action.error.unavailable": "Den handling er ikke tilgængelig lige nu.",
  "action.error.failed": "Handlingen kunne ikke udføres. Intet blev ændret.",
  "action.error.reconcileFailed": "Handlingen blev udført, men planen kunne ikke opdateres.",
  "action.error.confirmationFailed": "Handlingen blev accepteret, men den aktuelle tilstand kunne ikke bekræftes.",
  "action.error.invalidPause": "Det pausevalg er ikke tilgængeligt for denne lader lige nu.",
  "action.error.charger": "Den konfigurerede lader kunne ikke nås.",
  "action.error.version": "Kortet og integrationen taler forskellige handlingsversioner.",
  "action.error.generic": "Handlingen mislykkedes. Tjek forbindelsen til Home Assistant, og prøv igen.",
  "dialog.close": "Luk",
  "dialog.issues": "Det kræver opmærksomhed",
  "dialog.issuesIntro": "Alt hvad integrationen rapporterede for denne lader, i rapporteret rækkefølge.",
  "context.area": "Område",
  "context.currency": "Valuta",
  "market.aria": "Skift prisområde og afgifter: {value}",
  "market.unset": "Ikke angivet",
  "market.title": "Område og afgifter",
  "market.intro": "Hvilket marked laderen køber strøm fra, og de afgifter der lægges på priserne.",
  "market.loading": "Læser områdelisten…",
  "market.readOnly": "Kun administratorer kan ændre område og afgifter. Du kan læse dem her.",
  "market.area.label": "Område",
  "market.area.description": "Området bestemmer hvilke priser der bruges, og hvilke afgifter der foreslås.",
  "market.area.unlisted": "udgives ikke længere",
  "market.area.missing": "Der udgives ingen områder lige nu.",
  "market.vat.label": "Moms",
  "market.vat.description": "Merværdiafgift i procent.",
  "market.tax.label": "Energiafgift",
  "market.tax.description": "Energiafgift i områdets mindste valutaenhed.",
  "market.transfer.label": "Netafgift",
  "market.transfer.description": "Netafgift i områdets mindste valutaenhed.",
  "market.enabled.aria": "{component}: aktiveret",
  "market.resetToSuggestion": "Nulstil til forslaget",
  "market.value.aria": "{component}: egen værdi",
  "market.suggestion.label": "Foreslået: {value} {unit}",
  "market.suggestion.none": "Der udgives ingen foreslået værdi for området.",
  "market.error.read": "Områdelisten kunne ikke læses.",
  "market.error.version": "Dette kort og integrationen taler forskellige markedsversioner.",
  "market.error.areaRequired": "Vælg et område, før du gemmer.",
  "market.error.areaUnknown": "Det område er ikke et af de udgivne valg.",
  "market.error.noSuggestion": "Området udgiver ingen foreslået værdi for det.",
  "market.state.loading": "Områdelisten læses stadig.",
  "market.state.staleOffline": "Dette er den seneste områdeliste, Home Assistant modtog; relæet har ikke kunnet nås siden.",
  "market.state.staleInvalid": "Dette er den seneste områdeliste, Home Assistant modtog; relæets nyere liste kunne ikke læses.",
  "market.state.offline": "Relæet kunne ikke nås, så der listes ingen områder lige nu.",
  "market.state.invalid": "Relæets områdeliste kunne ikke læses, så der listes ingen områder lige nu.",
  "settings.energy.aria": "Energi: {value}",
  "settings.deadline.aria": "Frist: {value}",
  "settings.current.aria": "Strøm: {value}",
  "settings.energy.unset": "Ikke angivet",
  "settings.deadline.none": "Ingen frist",
  "settings.current.unset": "Ikke angivet",
  "settings.energy.title": "Ladeenergi",
  "settings.energy.intro": "Hvor meget energi opladningen skal give. Planen laver det om til perioder.",
  "settings.energy.label": "Ønsket energi",
  "settings.energy.targetSoc": "Denne lader planlægger ud fra et mål for ladeniveau, så den manuelle energi kan ikke ændres her.",
  "settings.deadline.title": "Frist og ladeperioder",
  "settings.deadline.intro": "Hvornår opladningen skal være færdig, og hvor mange perioder den må bruge.",
  "settings.deadline.enabled": "Færdig inden en frist",
  "settings.deadline.time": "Afgangstid",
  "settings.deadline.date": "Afgang",
  "settings.deadline.dateDaily": "Hver dag",
  "settings.deadline.dateOn": "En bestemt dato",
  "settings.deadline.dateHelp": "Planen kan vente på timer, der plejer at være billigere. Afgangen holdes altid.",
  "settings.deadline.datePast": "Datoen er passeret, så planen kører hver dag, indtil du vælger en ny dato. Gemmer du, ryddes den.",
  "settings.deadline.weekdays": "Ugedage",
  "settings.deadline.weekdaysHelp": "På en dag, du udelader, er der ingen afgang: planen løber til næste dag, du har valgt.",
  "settings.deadline.today": "i dag",
  "settings.deadline.tomorrow": "i morgen",
  "settings.deadline.periods": "Højeste antal ladeperioder",
  "settings.current.title": "Planlagt strøm",
  "settings.current.intro": "Strømmen planen må bede om. Det er en planværdi, ikke en kommando til laderen.",
  "settings.current.label": "Planlagt strøm",
  "settings.loading": "Læser de aktuelle indstillinger…",
  "settings.section.support": "Support",
  "debug.intro": "Gemmer én fil med versioner, status og de seneste logliner til en fejlrapport. Hemmeligheder og din præcise placering udelades.",
  "debug.download": "Download fejlsøgningsinfo",
  "debug.preparing": "Forbereder…",
  "debug.error.notAdmin": "Kun administratorer kan downloade fejlsøgningsinfo.",
  "debug.error.failed": "Fejlsøgningsinfoen kunne ikke hentes.",
  "settings.readOnly": "Kun administratorer kan ændre indstillinger. Du kan læse dem her.",
  "settings.save": "Gem",
  "settings.cancel": "Annuller",
  "settings.reload": "Brug serverens værdier",
  "settings.reapply": "Brug min ændring igen",
  "settings.conflict.title": "Ændret et andet sted",
  "settings.conflict.intro": "Indstillingerne blev ændret, efter at denne dialog blev åbnet. Intet blev gemt, og dine værdier er stadig her.",
  "settings.error.required": "Udfyld dette, før du gemmer.",
  "settings.error.invalidNumber": "Det er ikke et tal.",
  "settings.error.outOfRange": "Værdien ligger uden for det tilladte interval.",
  "settings.error.invalidDate": "Vælg en gyldig dato.",
  "settings.error.dateRange": "Vælg en dato fra i dag og op til 7 dage frem.",
  "settings.error.weekdays": "Vælg mindst én ugedag.",
  "settings.error.invalidTime": "Brug et klokkeslæt som 06:30.",
  "settings.error.read": "Indstillingerne kunne ikke læses.",
  "settings.error.refused": "Indstillingerne blev afvist. Intet blev ændret.",
  "settings.error.notCommitted": "Indstillingerne blev ikke gemt. Intet blev ændret.",
  "settings.error.reconcileFailed": "Indstillingerne blev gemt, men planen kunne ikke opdateres.",
  "settings.error.confirmationFailed": "Indstillingerne blev gemt, men den aktuelle tilstand kunne ikke bekræftes.",
  "settings.error.version": "Kortet og integrationen taler forskellige indstillingsversioner.",
  "settings.error.unavailable": "Indstillinger er ikke tilgængelige lige nu.",
  "settings.error.charger": "Den konfigurerede lader kunne ikke nås.",
  "settings.error.generic": "Indstillingerne kunne ikke ændres. Intet blev ændret.",
  "settings.error.invalid": "Indstillingerne blev afvist som ugyldige. Intet blev ændret.",
  "settings.error.readOnly": "Kun administratorer kan ændre indstillinger.",
  "settings.energy.slider": "Energiskyd, 0,5 til 100 kWh i halvkWh-trin",
  "settings.current.slider": "Strømskyder, {min} til {max} A i hele ampere",
  "settings.sliderOutOfRange": "Den præcise værdi ligger uden for skydens interval. Brug talfeltet.",
  "settings.current.power": "Nominel effekt ≈ {power} kW",
  "settings.current.powerUnknown": "Nominel effekt ukendt",
  "bar.plan": "Plan",
  "settings.plan.title": "Ladeplan",
  "settings.plan.intro": "Hvor meget der skal oplades, hvornår det skal være færdigt, og hvilken strøm planen må bede om. Det er planlægningsværdier, ikke kommandoer til laderen.",
  "settings.plan.aria": "Plan: {value}",
  "entity.vehicle.automatic": "Automatisk genkendelse",
  "entity.vehicle.several": "Køretøjet har flere batterisensorer. Automatisk genkendelse kan ikke vælge mellem dem, så opladningsniveauet læses ikke, før du vælger en.",
  "entity.error.field.unknownVehicle": "Køretøjet findes ikke længere.",
  "issue.targetSocUnknown": "Ladetilstandsmålet kan ikke planlægges: bilens ladetilstand eller batterikapacitet er ukendt. Åbn Plan for at vælge en sensor eller angive kapaciteten.",
  "settings.plan.mode.legend": "Oplad efter",
  "settings.plan.mode.energy": "Energi (kWh)",
  "settings.plan.mode.soc": "Mål-SoC (%)",
  "settings.soc.target": "Mål for ladetilstand",
  "settings.soc.slider": "Mål for ladetilstand, procent",
  "settings.soc.vehicle": "Køretøj",
  "settings.soc.vehicleUnknown": "Ikke valgt",
  "settings.soc.estimated": "anslået",
  "settings.soc.estimatedFrom": "anslået, sidst aflæst {age}",
  "settings.soc.unknown": "Ukendt",
  "settings.soc.need": "Energi der skal bruges",
  "settings.soc.capacityMissing": "Batterikapacitet mangler — angiv den i Indstillinger",
  "settings.capacity.label": "Batterikapacitet",
  "settings.capacity.unset": "Ikke angivet",
  "settings.soc.needSensor": "En sensor for ladetilstand skal vælges for bilen, før et mål kan planlægges.",
  "settings.soc.age.now": "lige nu",
  "settings.soc.age.min": "for {count} min. siden",
  "settings.soc.age.hour": "for {count} t siden",
  "settings.soc.age.day": "for {count} d siden",
  "settings.overview.titleNamed": "Kortindstillinger · {name}",
  "settings.soc.factNow": "Nu {value}",
  "settings.soc.factLimit": "Bilens ladegrænse {value}",
  "settings.soc.noNeed": "Ingen opladning nødvendig nu",
  "settings.soc.toLimit": "Lader til bilens grænse, {value}",
  "settings.soc.readAge": "Aflæst {age}",
  "settings.soc.needAfterVehicle": "Behovet beregnes, når valget af køretøj er gemt.",
  "vehicleLine.aria": "{name}, {summary}. Vælg hvilket køretøj der skal oplades",
  "vehicleLine.estimateTitle": "Anslået mellem aflæsningerne, aflæst {age}",
  "vehicleLine.dialogTitle": "Hvilket køretøj skal oplades?",
  "vehicleLine.noReading": "Ingen aflæsning",
  "settings.vehicle.unnamed": "Køretøj uden navn",
  "settings.vehicle.plannedHere": "Denne oplader planlægger for det",
  "settings.vehicle.capacityReported": "rapporteret af bilen",
  "settings.vehicle.socNone": "Ingen sensor valgt",
  "settings.vehicle.charge": "Ladeniveau",
  "settings.vehicle.error.capacity": "Batterikapaciteten skal være mellem 1 og 500 kWh.",
  "settings.vehicle.error.consumption": "Forbruget skal være mellem 0,1 og 50 kWh/10 km.",
  "settings.phases.legend": "Faser laderen bruger",
  "settings.phases.one": "1 fase",
  "settings.phases.three": "3 faser",
  "settings.phases.unset": "Antallet af faser er ikke angivet.",
  "settings.phases.help": "Styrer den effekt, planen regner med for en given strøm. En trefaselader kan stadig lade en bil på én fase.",
  "cap.targetSocNote": "Kræver en sensor for bilens ladetilstand og batteriets kapacitet. Angiv dem i Plan og Indstillinger.",
  "control.startStop": "Start og stop",
  "control.startStop.easeeFixed": "SpotNav starter og stopper laderen via Easee-integrationens tjeneste (pause og genoptag), så der er ingen start- eller stopenhed at vælge.",
  "control.limit.minutes": "Strømmen kan højst ændres hvert {count}. minut.",
  "control.limit.seconds": "Strømmen kan højst ændres hvert {count}. sekund.",
  "control.limit.flash": "Strømmen gemmes i laderen og ændres kun, når en opladning starter.",
  "control.limit.stopOnly": "Belastningsfordeling kan kun stoppe opladningen, ikke sænke strømmen.",
  "control.limit.installation": "Grænsen gælder hele installationen.",
  "control.current": "Ladestrøm",
  "control.current.none": "Indstilles ikke af SpotNav (laderen beholder sin egen grænse)",
  "control.current.ocpp": "OCPP ChangeConfiguration",
  "control.current.number": "En talentitet: {name}",
  "control.current.service": "Easees dynamiske strømgrænse",
  "control.current.off": "SpotNav indstiller ikke strømmen. Slå det til i laderens indstillinger.",
  "control.conflict": "Laderens egen {label} er tændt ({name}). Den kan modarbejde SpotNav: slå den fra.",
  "control.disabled": "Laderens egen aktiveringskontakt er slået fra ({name}). SpotNav kan ikke starte den: slå den til.",
  "control.otherController": "{name} styrer også ladere; slå den fra for denne lader, ellers modarbejder {name} og SpotNav hinanden."
};

// src/i18n/en.ts
var en = {
  "card.title": "SpotNav",
  "state.loading": "Reading the charging plan…",
  "state.unconfigured": "Choose one SpotNav charger in this card's editor.",
  "state.addCharger": "Add a charger: Settings → Devices & services → SpotNav → Add entry → Charger; it will offer to join this site.",
  "state.noChargers": "No SpotNav charger is configured in this Home Assistant yet.",
  "state.requestFailed": "The request failed. Check the connection to Home Assistant and try again.",
  "state.unsupported": "This card and the integration speak different API versions. Update both so they match.",
  "state.malformed": "The integration answered with something this card cannot read. Update both so they match.",
  "state.chargerMissing": "That charger is unknown, unloaded or a site. Nothing was selected in its place.",
  "state.retry": "Retry",
  "state.noPrices": "No prices are available for this charger yet.",
  "status.externalInstalled": "External schedule: next charging at {time}.",
  "status.externalNoSchedule": "External planning is active; no schedule is installed.",
  "status.chargingNow": "Charging now; scheduled until {time}.",
  "status.autoPlanned": "Planned from {time}.",
  "status.autoInstalled": "Charging is scheduled from {time}.",
  "status.heldUntilWindow": "Charging waits for the planned start at {time}.",
  "status.proposalPending": "A new charging proposal is ready.",
  "status.proposalPendingAt": "A new plan is ready and is installed when the current charging window ends at {time}.",
  "status.waitingForTomorrow": "Waiting for tomorrow's prices.",
  "status.waitingForPublication": "Waiting for tomorrow's prices (~{time}), will plan then.",
  "status.waitingForHistory": "Waiting: {weekday} were {percent} % cheaper the last {weeks} weeks.",
  "status.waitingForHistoryNoDetail": "Waiting for hours that usually cost less, will plan then.",
  "status.waitingForPublicationNoTime": "Waiting for tomorrow's prices, will plan then.",
  "status.buyingBeforePublication": "Buying {kwh} kWh now, the rest when the prices are published.",
  "status.noPlan": "No charging plan could be calculated right now.",
  "status.loadBalancingLimited": "Charging is limited by the site's load balancing right now.",
  "status.loadBalancingLimitedTo": "Charging is limited to {limit} A by the site's load balancing.",
  "status.loadBalancingLimitedByBattery": "The home battery charges from the grid and shares the main fuse: the car gets {limit} A.",
  "status.loadBalancingLimitedByHouse": "House consumption limits the car to {limit} A.",
  "status.chargingNowOpen": "Charging now.",
  "status.scheduledNoTime": "Charging is scheduled.",
  "status.nothingToCharge": "Nothing to charge right now.",
  "status.pausedShort": "Automatic charging is paused.",
  "status.planEnergy": "{kwh} kWh",
  "status.planCost": "{cost}",
  "status.targetStopped": "Stopped at {soc} %",
  "status.targetStoppedAge": "Stopped at {soc} % (reading {age} old)",
  "status.targetStoppedNow": "Stopped at {soc} % (just now)",
  "status.targetStoppedEstimate": "Stopped at {soc} % (estimated)",
  "status.targetStoppedEstimateAge": "Stopped at {soc} % (estimated, reading {age} old)",
  "status.targetAgeMinutes": "{n} min",
  "status.targetAgeHours": "{n} h",
  "issue.targetUnverifiable": "The target can't be checked right now.",
  "status.planDistance": "{distance}",
  "issue.priceDegraded": "The price data is incomplete.",
  "issue.banner.blocking": "Something needs attention before charging can be planned.",
  "issue.banner.notice": "Good to know.",
  "issue.chargerMissing": "The configured charger is not usable: it is unknown, unloaded or a site.",
  "issue.chargeControlMissing": "The charge control {entity} no longer exists. It was probably renamed or removed: choose the charger's control again in its settings.",
  "issue.chargeControlDisabled": "The charge control {entity} is disabled in Home Assistant, so SpotNav cannot start or stop the charger. Enable it again.",
  "issue.unsupported": "The card and the integration speak different API versions.",
  "issue.malformed": "The backend answer was not a valid API v2 response.",
  "status.finishSetupArea": "Finish setting up: choose a price area in Settings.",
  "status.finishSetup": "Finish setting up in Settings: {fields}.",
  "status.settingsSuggested": "Suggested from your location and charger – check Settings.",
  "status.missing.area": "price area",
  "status.missing.phases": "phases",
  "status.missing.amps": "charging current",
  "status.missing.vehicle": "vehicle",
  "status.missing.target_percent": "target level",
  "issue.incompleteSettings": "Finish setting up: some settings are still missing. Open Settings.",
  "issue.missingSettings": "These settings are still missing: {fields}.",
  "issue.planningUnavailable": "No charging plan could be calculated with the data available right now.",
  "issue.solarUnavailable": "Solar charging is not available: this site cannot measure solar surplus.",
  "issue.priceHorizon": "Prices are missing for part of the required planning window. Set a deadline.",
  "issue.error": "The last calculation failed.",
  "issue.priceUnavailable": "No usable price data is available right now.",
  "issue.priceInvalid": "The price data is invalid.",
  "issue.priceStale": "The prices are stale: the last successful fetch is still being used.",
  "issue.unpriced": "Part of the plan charges without published prices.",
  "issue.chargingWithoutPrices": "Charging without published prices to keep the deadline.",
  "issue.loadBalancing": "Load balancing is not available for this charger.",
  "issue.heldByCharger": "The charger's own schedule or load balancing is holding the charge, so it has not started.",
  "issue.chargerDisabled": "The charger's own enable switch is off, so it cannot start. Turn it on in the charger's settings.",
  "issue.holdOverridden": "Charging was started outside the plan and is allowed to continue.",
  "status.siteMeasurement.stale.other": "{phases} are older than {seconds} s.",
  "status.siteMeasurement.stale.one": "{phases} is older than {seconds} s.",
  "status.siteMeasurement.noValue.other": "{phases} have no value{where}.",
  "status.siteMeasurement.noValue.one": "{phases} has no value{where}.",
  "issue.duplicateCharger": "{other} and this charger are the same physical charger. Two SpotNav chargers on one charger send it conflicting commands, so keep only one: remove the other in Settings → Devices & services → SpotNav. SpotNav never removes one for you.",
  "issue.siteMeasurement": "The site's measurement cannot be used right now.",
  "issue.unknown": "The backend reported something this card does not know yet.",
  "header.info": "About this card",
  "header.settings": "Card settings",
  "header.history": "Charge history",
  "history.title": "Charge history",
  "history.titleNamed": "Charge history · {name}",
  "history.loading": "Loading the charge history…",
  "history.failed": "The charge history could not be read.",
  "history.intro": "What each charge cost: the spot price plus your energy tax, grid fee and VAT, at the time the energy was delivered.",
  "history.thisMonth": "This month",
  "history.lastMonth": "Last month",
  "history.noneInMonth": "No charges.",
  "history.days": "Days",
  "history.months": "Months",
  "history.listLabel": "Show by",
  "history.sessions.one": "{count} charge",
  "history.sessions.other": "{count} charges",
  "history.solar": "{percent} solar",
  "history.estimated": "estimated energy",
  "history.noCost": "no price",
  "history.savings.saved": "Estimated saving: {amount} against the day's average price",
  "history.savings.extra": "Estimated {amount} more than the day's average price",
  "history.savings.note": "Savings are an estimate: the same energy at each day's average price.",
  "history.latest": "Latest charges",
  "history.empty": "No charges recorded yet. They appear here after the next charge.",
  "history.open": "Charging now since {time}: {energy}",
  "history.by.plan_window": "planned window",
  "history.by.manual": "started by hand",
  "history.by.solar": "solar surplus",
  "history.by.hybrid": "hybrid",
  "history.by.other": "started elsewhere",
  "history.export.period": "Period",
  "history.range.thisMonth": "This month",
  "history.range.lastMonth": "Last month",
  "history.range.last12": "Last 12 months",
  "history.range.all": "Everything",
  "history.export": "Export CSV",
  "history.exportFailed": "The export failed.",
  "settings.overview.title": "Card settings",
  "settings.section.market": "Price area and taxes",
  "settings.section.vehicle": "Vehicle",
  "settings.section.capabilities": "Sensors and capabilities",
  "settings.section.site": "Site",
  "settings.section.configure": "Configure",
  "settings.value.off": "Off",
  "settings.fiscal.vat": "VAT",
  "settings.fiscal.tax": "Energy tax",
  "settings.fiscal.transfer": "Grid fee",
  "settings.consumption.unit": "kWh/10 km",
  "settings.section.entities": "Charger",
  "entity.group.site": "Site entities",
  "entity.notSet": "Not set",
  "entity.edit.charger": "Change charger entities",
  "entity.edit.site": "Change site entities",
  "entity.editor.charger": "Charger entities",
  "entity.editor.site": "Site entities",
  "entity.loading": "Reading the entities…",
  "entity.adminOnly": "Only administrators can change entities.",
  "entity.error.read": "The entities could not be read.",
  "entity.field.chargeControl": "Charge control",
  "entity.field.currentLimit": "Current limit",
  "entity.field.energyRegister": "Energy register",
  "entity.field.powerEntity": "Power sensor (smart plug)",
  "entity.field.vehicleSoc": "Vehicle charge level",
  "entity.field.mainFuse": "Main fuse",
  "entity.field.safetyMargin": "Safety margin",
  "entity.field.measurementMode": "Measurement mode",
  "entity.field.voltageBetweenPhases": "Voltage between phases",
  "entity.field.chargerPriority": "Charger priority",
  "entity.field.batteryPower": "Battery power",
  "entity.field.maxAge": "Maximum measurement age",
  "entity.mode.direct": "Phase currents measured directly",
  "entity.mode.derived": "Phase currents derived from power and voltage",
  "entity.phase.direct": "Current, {phase}",
  "entity.derived.power": "Power",
  "entity.derived.reactivePower": "Reactive power",
  "entity.derived.voltage": "Voltage",
  "entity.error.field.required": "Choose an entity.",
  "entity.error.field.notFound": "That entity does not exist.",
  "entity.error.field.wrongDomain": "That is not the right kind of entity.",
  "entity.error.field.invalid": "That value is not accepted.",
  "entity.error.field.notWritable": "This cannot be changed here.",
  "entity.error.field.controlPathUnknown": "SpotNav cannot tell how to start and stop a charge with that entity. Choose the charger's switch, or a selector with clear start and stop options.",
  "entity.error.field.unknown": "That field is not recognised.",
  "entity.error.field.chargeControlInUse": "Another charger already uses this switch.",
  "entity.error.field.currentLimitInUse": "Another charger already uses this current limit.",
  "entity.error.field.duplicateCharger": "Another SpotNav charger is already this same charger.",
  "entity.error.conflict": "Changed elsewhere. The current values are shown and nothing was saved.",
  "entity.error.invalid": "Some values were not accepted. Nothing was saved.",
  "entity.error.generic": "The entities could not be changed. Nothing changed.",
  "entity.error.notAdmin": "Only administrators can change entities.",
  "entity.missing.optional": "This entity no longer exists in Home Assistant. Clear the field, or choose another entity.",
  "entity.missing.required": "This entity no longer exists in Home Assistant. Choose another entity.",
  "entity.automatic": "Automatic: {name}",
  "entity.help.chargeControl": "The entity that starts and stops charging: a switch, a selector or a button. SpotNav uses it to follow the plan.",
  "entity.help.currentLimit": "The entity that reports the charger's current setpoint. Automatic uses the one SpotNav finds itself, for OCPP 0.12 chargers the session limit.",
  "entity.help.energyRegister": "Your charger's cumulative kWh meter. SpotNav uses it to know what has already been charged. Found automatically for OCPP chargers.",
  "entity.help.powerEntity": "For a charger behind a smart plug: the plug's power sensor, in W or kW. SpotNav counts the energy from it and sees when the car has stopped drawing. The plug must be rated for the charger's continuous current.",
  "entity.help.vehicleSoc": "The vehicle's charge level, read from the sensor chosen for the vehicle, or found automatically when it has only one.",
  "entity.help.mainFuse": "The site's main fuse in amperes. All chargers on the site together stay below it.",
  "entity.help.safetyMargin": "The current in amperes that SpotNav keeps free below the main fuse. Charging stays under the fuse minus this margin, so it must be below the fuse.",
  "entity.help.measurementMode": "Whether your meter reports each phase's current directly, or SpotNav works it out from power and voltage.",
  "entity.help.voltageBetweenPhases": "The voltage between two phases of your electrical installation. Three-phase charging power is figured from it.",
  "entity.help.chargerPriority": "When several chargers share the site's fuse, a charger set to First is served before the others and one set to Last gets what is left.",
  "entity.voltage.tn": "400 V (TN network, the usual one)",
  "entity.voltage.it": "230 V (IT network, common in Norway)",
  "entity.priority.first": "First",
  "entity.priority.normal": "Normal",
  "entity.priority.last": "Last",
  "entity.help.batteryPower": "A sensor for the home battery's power, so SpotNav can take the battery into account.",
  "entity.help.maxAge": "How old a measurement may be, in seconds, before SpotNav stops trusting it.",
  "entity.help.phaseDirect": "The sensor that measures the current on this phase, in amperes.",
  "entity.help.derivedPower": "The sensor for active power on this phase.",
  "entity.help.derivedReactivePower": "The sensor for reactive power on this phase.",
  "entity.help.derivedVoltage": "The sensor for voltage on this phase.",
  "entity.flag.on": "On",
  "entity.flag.off": "Off",
  "entity.field.siteCurrentSigned": "Grid current is signed",
  "entity.field.gridPowerInverted": "The sensor shows export as positive",
  "entity.field.gridPowerSource": "Total grid power (for solar)",
  "entity.field.gridPowerSourceExport": "Total export power (if separate)",
  "entity.field.batteryDischargePower": "Battery discharge power",
  "entity.field.batteryPowerInverted": "The sensor shows discharge as positive",
  "entity.derived.powerExport": "Export power",
  "entity.derived.apparentPower": "Apparent power",
  "entity.derived.current": "Current",
  "entity.help.siteCurrentSigned": "Turn on if the meter reports a negative current while exporting. SpotNav then uses the size of the current, which is what loads the fuse, instead of rejecting it.",
  "entity.help.gridPowerInverted": "Turn on if the meter's power is positive while exporting (Huawei, SolarEdge, GoodWe and similar). SpotNav then reads import as positive. It also applies to the total grid power.",
  "entity.help.gridPowerSource": "Needed for solar and hybrid charging when the phases only report current.",
  "entity.help.gridPowerSourceExport": "Only if the meter reports import and export as two sensors: the field above is then the import and this is the export.",
  "entity.help.batteryPowerInverted": "Turn on if the battery's power is positive while discharging (Tesla, Fronius, Enphase, GoodWe and similar). SpotNav then reads charging as positive.",
  "entity.help.batteryDischargePower": "Only for a battery that reports charging and discharging as two sensors: this is the discharge one, and the battery power sensor above is the charge one.",
  "entity.help.derivedPowerExport": "Only when the meter reports import and export as two sensors: the power sensor above is then import and this one is export.",
  "entity.help.derivedApparentPower": "Apparent power on this phase, in VA. With it the current is exact.",
  "entity.help.derivedCurrent": "The meter's own current on this phase. With it the current is exact.",
  "entity.choice.mixed": "More than one of these is set. Only the chosen one is kept; the others are cleared when you save.",
  "entity.choice.choose": "Choose an entity",
  "entity.limit.none": "None (SpotNav does not set the current)",
  "entity.grid.title": "Grid power",
  "entity.grid.one": "One sensor with direction",
  "entity.grid.two": "Import and export as two sensors",
  "entity.current.title": "Current is taken from",
  "entity.current.measured": "The meter's own current",
  "entity.current.apparent": "Apparent power",
  "entity.current.reactive": "Reactive power",
  "entity.current.estimated": "Estimated (power factor 0.9)",
  "entity.current.estimatedNote": "SpotNav estimates the current from power. The estimate is marked in the card.",
  "entity.battery.title": "Battery",
  "entity.battery.none": "None",
  "entity.battery.one": "One sensor",
  "entity.battery.two": "Charging and discharging as two sensors",
  "entity.energy.title": "Energy",
  "entity.energy.meter": "Energy meter (kWh)",
  "entity.energy.power": "Power (W) — SpotNav calculates the energy",
  "entity.energy.none": "None",
  "entity.notice.estimated": "The current is estimated from power (uppskattad), assuming a power factor of {pf} or better. The estimate is never below the real current at that power factor or better, and understates it below that. Add the meter's current, apparent power or reactive power for an exact value.",
  "entity.notice.estimatedShort": "Estimated",
  "entity.warning.updateInterval": "{integration} updates every {seconds} s, slower than the maximum measurement age.",
  "entity.warning.updateIntervalOption": 'Lower it with "{option}" in that integration.',
  "entity.warning.onChange": "{integration} reports only when a value changes, so a steady value can look old. Raise the maximum measurement age if the site is often reported stale.",
  "entity.warning.ownBalancing": "{name} ({integration}) balances load by itself and may fight SpotNav's active control. Use one of them.",
  "entity.warning.externalBalancer": "{integration} balances the current of {name} itself, so SpotNav starts and stops the charger but does not write its current.",
  "entity.warning.unknown": "The site has a notice this version cannot show. Update SpotNav.",
  "entity.detect.title": "Found in Home Assistant",
  "entity.detect.intro": "These were recognised in your setup. Nothing changes until you press Use.",
  "entity.site.intro": "Measurement for the site. {applies}",
  "entity.detect.meters": "Grid meters",
  "entity.detect.batteries": "Home batteries",
  "entity.detect.use": "Use",
  "entity.detect.inUse": "In use",
  "entity.detect.direct": "Measures phase current directly",
  "entity.detect.derived": "Derived from power and voltage",
  "entity.detect.signed": "Signed current: read as its size",
  "entity.detect.inverted": "Export-positive power: negated",
  "entity.detect.gridPower": "Total grid power found: used for solar and hybrid charging",
  "entity.detect.estimated": "Estimated current: no current, apparent or reactive power found",
  "entity.detect.enable": "Enables {count} entities their integration ships disabled.",
  "entity.detect.battery.inverted": "Discharge-positive: negated",
  "entity.detect.battery.pair": "Charge and discharge are two sensors",
  "entity.detect.confidence.low": "Low confidence: check that the signs are right.",
  "entity.detect.warning.own_load_balancing": "This device balances load by itself.",
  "entity.detect.warning.sign_unverified": "The sign of the power is not known: check that export is negative.",
  "entity.detect.warning.voltage_from_other_device": "This meter reports no voltage, so the phase voltage of another device, usually the inverter, is used. This is normal.",
  "entity.detect.warning.may_measure_subcircuit": "Make sure this meter measures the whole main feed, not a sub-circuit.",
  "entity.detect.warning.reports_on_change_only": "Reports only when a value changes.",
  "market.edit": "Edit price area and taxes",
  "settings.vehicle.change": "Change vehicle",
  "settings.vehicle.none": "No vehicle is set up yet.",
  "settings.vehicle.dialogTitle": "Vehicle · {name}",
  "settings.vehicle.sensorLegend": "Charge-level sensor",
  "entity.foundAutomatically": "Found automatically",
  "site.row.battery": "Battery",
  "settings.value.none": "None",
  "settings.section.solar": "Solar",
  "site.solar.change": "Change solar settings",
  "site.solar.dialogTitle": "Solar settings",
  "strategy.setupSolar": "Set up solar in Settings",
  "strategy.setupSite": "Open the site settings",
  "site.none": "This charger has no site.",
  "site.applies.one": "Applies to the one charger on this site.",
  "site.applies.other": "Applies to all {count} chargers on this site.",
  "site.solarPriority.title": "Solar priority",
  "site.solarPriority.carFirst": "Car first",
  "site.solarPriority.batteryFirst": "Battery first",
  "site.solarForecast.title": "Solar forecast sources",
  "site.solarForecast.none": "None selected. Without one, hybrid plans a charge like Cheapest.",
  "site.activeControl.title": "Active load balancing",
  "site.activeControl.on": "On",
  "site.activeControl.off": "Off",
  "site.activeControl.available": "Available",
  "site.activeControl.reason.duplicateMembership": "Not available: this charger is claimed by more than one site.",
  "site.activeControl.reason.noCommandableCharger": "Not available: no charger on this site accepts a current command.",
  "site.activeControl.reason.measurement": "Not available: this site's own power measurement is not healthy.",
  "site.activeControl.note": "Best effort, not a protective device. Turning it off gives any current it had lowered back.",
  "site.activeControl.pending": "Saving…",
  "site.activeControl.error.unavailable": "Load balancing cannot be turned on right now: the site cannot be actively controlled at the moment. Nothing was changed.",
  "site.activeControl.error.confirmation": "The change could not be confirmed, so the previous setting was kept.",
  "site.activeControl.restore.off": "Load balancing is off.",
  "site.activeControl.restore.notNeeded": "No charger needed its current restored.",
  "site.activeControl.restore.restoredOne": "The charger was restored to {to} A.",
  "site.activeControl.restore.restoredNamed": "{name} was restored to {to} A.",
  "site.activeControl.restore.unnamed": "A charger",
  "site.activeControl.restore.line": "{name}: {text}",
  "site.activeControl.restore.failed.write_failed": "The charger refused the command to restore its current, so it may still be limited.",
  "site.activeControl.restore.failed.unconfirmed": "The charger did not confirm the restored current, so it may still be limited.",
  "site.activeControl.restore.failed.assigned_current_unreadable": "The charger's current setting could not be read, so it may still be limited.",
  "site.activeControl.restore.failed.no_authoritative_current": "SpotNav has no requested current to return the charger to, so it may still be limited.",
  "site.activeControl.restore.failed.charger_not_loaded": "The charger is not loaded in Home Assistant, so it may still be limited.",
  "site.activeControl.restore.failed.membership_conflict": "The charger belongs to more than one site and was left untouched, so it may still be limited.",
  "site.activeControl.restore.failed.probe_in_flight": "The charger was busy with a current test, so it may still be limited.",
  "site.activeControl.restore.failed.below_minimum": "The current to restore is below what the charger accepts, so it may still be limited.",
  "site.activeControl.restore.failed.external_balancer": "Another integration balances the charger's current, so SpotNav did not write it and it may still be limited.",
  "site.activeControl.restore.failed.no_connector_target": "SpotNav could not find the charger's connector to command, so it may still be limited.",
  "site.activeControl.restore.failed.unknown": "The charger could not be restored, so it may still be limited.",
  "site.error.conflict": "Changed elsewhere. The current values are shown.",
  "strategy.status.solar.charging": "Solar · charging {amps} A from surplus",
  "strategy.status.solar.chargingUnknown": "Solar · charging from surplus",
  "strategy.status.solar.arming": "Solar · surplus found, starting soon",
  "strategy.status.solar.disarming": "Solar · surplus fading, stopping soon",
  "strategy.status.solar.noReadingStopped": "Solar · stopped, no usable reading",
  "strategy.status.solar.noReadingWaiting": "Solar · no usable reading yet",
  "strategy.status.solar.waitingForSun": "Solar · waiting for sun",
  "strategy.status.solar.unknown": "Solar · status unknown",
  "strategy.status.hybrid.grid": "Hybrid · {grid} kWh from grid{window}",
  "strategy.status.hybrid.creditSuffix": ", {credit} kWh expected from sun",
  "strategy.status.hybrid.noForecast": "Hybrid · no forecast source — planning like Cheapest",
  "strategy.status.hybrid.noPriceData": "Hybrid · waiting for price data",
  "strategy.status.hybrid.satisfied": "Hybrid · charging need already met",
  "strategy.status.hybrid.unknown": "Hybrid · planning",
  "settings.consumption.label": "Consumption",
  "settings.value.unset": "Not set",
  "issue.count.one": "{count} item to review",
  "issue.count.other": "{count} items to review",
  "graph.title": "Charging plan",
  "graph.description": "Price graph from {from} to {to}, in {unit} per kWh. {count} intervals over {days} days. Selected: {selected}.",
  "graph.descriptionNoSelection": "Nothing selected.",
  "graph.descriptionNow": "Current interval: {now}.",
  "graph.legend.today": "Today",
  "graph.legend.tomorrow": "Tomorrow",
  "graph.legend.past": "Earlier day",
  "graph.legend.current": "Current interval",
  "graph.legend.selection": "Selected interval",
  "graph.legend.cheap": "Cheaper than the day's average",
  "graph.legend.expensive": "More expensive than the day's average",
  "graph.legend.installed": "Scheduled",
  "graph.legend.proposal": "Proposal",
  "graph.summary.max": "Max",
  "graph.summary.min": "Min",
  "graph.summary.current": "Now",
  "graph.readout": "{day} {time} · {price}",
  "graph.readoutMissing": "{day} {time} · no price",
  "graph.readoutWithOffset": "{day} {time} ({offset}) · {price}",
  "graph.readoutMissingWithOffset": "{day} {time} ({offset}) · no price",
  "graph.selected": "Selected price",
  "graph.priceAxis": "Price, {unit} per kWh",
  "graph.noZone": "Times are unavailable: the integration has not reported a market zone for this charger.",
  "graph.hint": "Use the arrow keys to step through the intervals.",
  "graph.keyboardInstructions": "Price graph. Arrow keys move from interval to interval, Home and End jump to the ends, Escape clears the selection.",
  "plan.proposal.one": "Cheapest charging period ({count})",
  "plan.proposal.other": "Cheapest charging periods ({count})",
  "plan.installed.one": "Scheduled charging period ({count})",
  "plan.installed.other": "Scheduled charging periods ({count})",
  "plan.period": "{day} {from}–{to}",
  "plan.periodWithOffset": "{day} {from}–{to} ({offset})",
  "plan.cost": "Cost",
  "plan.energy": "Energy",
  "plan.distance": "Distance",
  "plan.power": "Power",
  "plan.activeNow": "Now",
  "plan.missing": "unknown",
  "cap.title": "What this charger can do",
  "cap.intro": "Taken from what the integration reports for this charger. An item that is unavailable here cannot be switched on by this card.",
  "cap.autoPrice": "Automatic price planning",
  "cap.currentLimit": "Dynamic current limit",
  "cap.loadBalancing": "Load balancing",
  "cap.targetSoc": "Vehicle state of charge and target",
  "cap.available": "Available",
  "cap.unavailable": "Unavailable for this charger",
  "action.start": "Start now",
  "action.stop": "Stop",
  "action.resume": "Resume automatic charging",
  "action.startHelp": "Starts a charge now. It promises no duration: Auto may take over again at its next reconciliation.",
  "action.pauseAutomatic": "Pause automatic charging",
  "action.pauseAutomaticShort": "Pause",
  "action.resumeShort": "Resume",
  "pause.sheetTitle": "Pause automatic charging",
  "pause.intro": "Choose how long the pause lasts. Only the timed choices end by themselves.",
  "pause.nextPeriod": "Until the next planned period",
  "pause.untilTomorrow": "Until tomorrow",
  "pause.untilResumed": "Until I resume",
  "strategy.title": "Strategy",
  "bar.charging": "Charging",
  "bar.schedule": "Schedule",
  "bar.start": "Start",
  "bar.waiting": "Starting…",
  "bar.stop": "Stop",
  "bar.chargeNow": "Charge now",
  "bar.chargingNow": "Charging",
  "bar.scheduleActive": "Schedule active",
  "bar.schedulePaused": "Schedule paused",
  "bar.state.notCharging": "not charging",
  "bar.state.charging": "charging",
  "bar.state.scheduleActive": "active",
  "bar.state.schedulePaused": "paused",
  "bar.change": "change",
  "strategy.dialogTitle": "Charging strategy",
  "strategy.intro": "What the plan optimizes. A strategy that is unavailable here cannot be selected yet.",
  "strategy.cheapest": "Cheapest",
  "strategy.solar": "Solar",
  "strategy.hybrid": "Hybrid",
  "strategy.reason.solar": "Requires solar-surplus measurement",
  "strategy.reason.hybrid": "Requires solar and price control",
  "strategy.reason.totalPower": "Solar needs the meter's total grid power",
  "advisory.vehicleNotRequestingCurrent": "Charging was started, but the vehicle is not requesting current. Check the vehicle's charging settings or reconnect the cable.",
  "advisory.powerBelowThreshold": "Charging was started, but the charger draws almost no power. The car may be finished or not charging: check the vehicle's charging settings or reconnect the cable.",
  "control.noSettings": "This charger has no settings yet, so there is nothing to start or stop.",
  "control.pauseUnsettled": "A pause is stored but has not taken effect yet. Nothing is being applied.",
  "control.pauseClearFailed": "A pause has elapsed but could not be cleared, so nothing is being applied.",
  "control.pauseStopFailed": "The charger could not be stopped when the pause was admitted. Try Stop again.",
  "control.startNotAcknowledged": "The charger did not acknowledge the start command. Try again.",
  "control.reconcileFailed": "The last change was saved, but the plan could not be updated.",
  "control.executionError": "The integration reported a problem with the last execution.",
  "control.actionPending": "A start command is waiting for the charger to acknowledge it.",
  "control.pausedUntil": "Paused until {time}.",
  "control.pausedIndefinitely": "Paused until you resume.",
  "action.error.unavailable": "That action is not available right now.",
  "action.error.failed": "The action could not be carried out. Nothing changed.",
  "action.error.reconcileFailed": "The action was carried out, but the plan could not be updated.",
  "action.error.confirmationFailed": "The action was accepted, but its current state could not be confirmed.",
  "action.error.invalidPause": "That pause choice is not available for this charger right now.",
  "action.error.charger": "The configured charger could not be reached.",
  "action.error.version": "This card and the integration speak different action versions.",
  "action.error.generic": "The action failed. Check the connection to Home Assistant and try again.",
  "dialog.close": "Close",
  "dialog.issues": "What needs attention",
  "dialog.issuesIntro": "Everything the integration reported for this charger, in the order it was reported.",
  "context.area": "Area",
  "context.currency": "Currency",
  "market.aria": "Edit price area and taxes: {value}",
  "market.unset": "Not set",
  "market.title": "Area and taxes",
  "market.intro": "Which market this charger buys from, and the taxes that are added to its prices.",
  "market.loading": "Reading the area list…",
  "market.readOnly": "Only administrators can change the area and its taxes. You can read them here.",
  "market.area.label": "Area",
  "market.area.description": "The area decides which prices are used, and which taxes are suggested.",
  "market.area.unlisted": "no longer published",
  "market.area.missing": "No areas are published right now.",
  "market.vat.label": "VAT",
  "market.vat.description": "Value added tax, as a percentage.",
  "market.tax.label": "Energy tax",
  "market.tax.description": "Energy tax, in the area's smallest currency unit.",
  "market.transfer.label": "Grid transfer",
  "market.transfer.description": "Grid transfer, in the area's smallest currency unit.",
  "market.enabled.aria": "{component}: enabled",
  "market.resetToSuggestion": "Reset to suggestion",
  "market.value.aria": "{component}: my own value",
  "market.suggestion.label": "Suggested: {value} {unit}",
  "market.suggestion.none": "No suggested value is published for this area.",
  "market.error.read": "The area list could not be read.",
  "market.error.version": "This card and the integration speak different market versions.",
  "market.error.areaRequired": "Choose an area before saving.",
  "market.error.areaUnknown": "That area is not one of the published choices.",
  "market.error.noSuggestion": "This area publishes no suggested value for that.",
  "market.state.loading": "The area list is still being read.",
  "market.state.staleOffline": "This is the last area list Home Assistant received; the relay could not be reached since.",
  "market.state.staleInvalid": "This is the last area list Home Assistant received; the relay's newer list could not be read.",
  "market.state.offline": "The relay could not be reached, so no areas are listed right now.",
  "market.state.invalid": "The relay's area list could not be read, so no areas are listed right now.",
  "settings.energy.aria": "Energy: {value}",
  "settings.deadline.aria": "Deadline: {value}",
  "settings.current.aria": "Current: {value}",
  "settings.energy.unset": "Not set",
  "settings.deadline.none": "No deadline",
  "settings.current.unset": "Not set",
  "settings.energy.title": "Charging energy",
  "settings.energy.intro": "How much energy this charge should deliver. The plan turns it into periods.",
  "settings.energy.label": "Requested energy",
  "settings.energy.targetSoc": "This charger plans from a target state of charge, so the manual energy is not editable here.",
  "settings.deadline.title": "Deadline and charging periods",
  "settings.deadline.intro": "When the charge must be finished, and how many periods it may use.",
  "settings.deadline.enabled": "Finish by a deadline",
  "settings.deadline.time": "Departure time",
  "settings.deadline.date": "Departure",
  "settings.deadline.dateDaily": "Every day",
  "settings.deadline.dateOn": "On a date",
  "settings.deadline.dateHelp": "The plan may wait for hours that are usually cheaper. The departure is always kept.",
  "settings.deadline.datePast": "This date has gone by, so the plan runs every day until you choose a new date. Saving clears it.",
  "settings.deadline.weekdays": "Weekdays",
  "settings.deadline.weekdaysHelp": "On a day you leave out there is no departure: the plan runs to the next day you choose.",
  "settings.deadline.today": "today",
  "settings.deadline.tomorrow": "tomorrow",
  "settings.deadline.periods": "Maximum charging periods",
  "settings.current.title": "Planned current",
  "settings.current.intro": "The current the plan may ask for. It is a planning value, not a command to the charger.",
  "settings.current.label": "Planned current",
  "settings.loading": "Reading the current settings…",
  "settings.section.support": "Support",
  "debug.intro": "Saves one file with versions, status and the latest log lines for a bug report. Secrets and your exact location are left out.",
  "debug.download": "Download debug info",
  "debug.preparing": "Preparing…",
  "debug.error.notAdmin": "Only administrators can download debug info.",
  "debug.error.failed": "The debug info could not be fetched.",
  "settings.readOnly": "Only administrators can change settings. You can read them here.",
  "settings.save": "Save",
  "settings.cancel": "Cancel",
  "settings.reload": "Use the server values",
  "settings.reapply": "Apply my change again",
  "settings.conflict.title": "Changed elsewhere",
  "settings.conflict.intro": "These settings changed after this dialog was opened. Nothing was saved, and your values are still here.",
  "settings.error.required": "Fill this in before saving.",
  "settings.error.invalidNumber": "That is not a number.",
  "settings.error.outOfRange": "That value is outside the allowed range.",
  "settings.error.invalidDate": "Choose a valid date.",
  "settings.error.dateRange": "Choose a date from today up to 7 days ahead.",
  "settings.error.weekdays": "Choose at least one weekday.",
  "settings.error.invalidTime": "Use a time like 06:30.",
  "settings.error.read": "The settings could not be read.",
  "settings.error.refused": "Those settings were refused. Nothing changed.",
  "settings.error.notCommitted": "The settings were not saved. Nothing changed.",
  "settings.error.reconcileFailed": "The settings were saved, but the plan could not be updated.",
  "settings.error.confirmationFailed": "The settings were saved, but the current state could not be confirmed.",
  "settings.error.version": "This card and the integration speak different settings versions.",
  "settings.error.unavailable": "Settings are not available right now.",
  "settings.error.charger": "The configured charger could not be reached.",
  "settings.error.generic": "The settings could not be changed. Nothing changed.",
  "settings.error.invalid": "The settings were refused as invalid. Nothing changed.",
  "settings.error.readOnly": "Only administrators can change settings.",
  "settings.energy.slider": "Energy slider, 0.5 to 100 kWh in half-kWh steps",
  "settings.current.slider": "Current slider, {min} to {max} A in whole amperes",
  "settings.sliderOutOfRange": "The exact value is outside the slider's range. Use the number field.",
  "settings.current.power": "Nominal power ≈ {power} kW",
  "settings.current.powerUnknown": "Nominal power unknown",
  "bar.plan": "Plan",
  "settings.plan.title": "Charging plan",
  "settings.plan.intro": "How much to charge, when it must be finished and the current the plan may ask for. These are planning values, not commands to the charger.",
  "settings.plan.aria": "Plan: {value}",
  "entity.vehicle.automatic": "Automatic detection",
  "entity.vehicle.several": "This vehicle has several battery sensors. Automatic detection cannot choose between them, so its charge level is not read until you choose one.",
  "entity.error.field.unknownVehicle": "That vehicle is no longer available.",
  "issue.targetSocUnknown": "The target charge level cannot be planned: the vehicle's charge level or battery capacity is not known. Open Plan to choose a sensor or enter the capacity.",
  "settings.plan.mode.legend": "Charge by",
  "settings.plan.mode.energy": "Energy (kWh)",
  "settings.plan.mode.soc": "Target SoC (%)",
  "settings.soc.target": "Target charge level",
  "settings.soc.slider": "Target charge level, percent",
  "settings.soc.vehicle": "Vehicle",
  "settings.soc.vehicleUnknown": "Not chosen",
  "settings.soc.estimated": "estimated",
  "settings.soc.estimatedFrom": "estimated, last read {age}",
  "settings.soc.unknown": "Unknown",
  "settings.soc.need": "Energy needed",
  "settings.soc.capacityMissing": "Battery capacity is missing — enter it in Settings",
  "settings.capacity.label": "Battery capacity",
  "settings.capacity.unset": "Not specified",
  "settings.soc.needSensor": "A charge-level sensor must be chosen for the vehicle before a target can be planned.",
  "settings.soc.age.now": "just now",
  "settings.soc.age.min": "{count} min ago",
  "settings.soc.age.hour": "{count} h ago",
  "settings.soc.age.day": "{count} d ago",
  "settings.overview.titleNamed": "Card settings · {name}",
  "settings.soc.factNow": "Now {value}",
  "settings.soc.factLimit": "Vehicle charge limit {value}",
  "settings.soc.noNeed": "No charging needed now",
  "settings.soc.toLimit": "Charging to the vehicle's limit, {value}",
  "settings.soc.readAge": "Read {age}",
  "settings.soc.needAfterVehicle": "The need is calculated once the vehicle choice is saved.",
  "vehicleLine.aria": "{name}, {summary}. Choose which vehicle to charge",
  "vehicleLine.estimateTitle": "Estimated between readings, read {age}",
  "vehicleLine.dialogTitle": "Which vehicle should be charged?",
  "vehicleLine.noReading": "No reading",
  "settings.vehicle.unnamed": "Unnamed vehicle",
  "settings.vehicle.plannedHere": "This charger plans for it",
  "settings.vehicle.capacityReported": "reported by the vehicle",
  "settings.vehicle.socNone": "No sensor chosen",
  "settings.vehicle.charge": "Charge level",
  "settings.vehicle.error.capacity": "Battery capacity must be between 1 and 500 kWh.",
  "settings.vehicle.error.consumption": "Consumption must be between 0.1 and 50 kWh/10 km.",
  "settings.phases.legend": "Phases the charger uses",
  "settings.phases.one": "1 phase",
  "settings.phases.three": "3 phases",
  "settings.phases.unset": "The phase count is not set.",
  "settings.phases.help": "Sets the power the plan assumes for a given current. A three-phase charger can still charge a vehicle on one phase.",
  "cap.targetSocNote": "Needs a charge-level sensor for the vehicle and its battery capacity. Set them in Plan and Settings.",
  "control.startStop": "Start and stop",
  "control.startStop.easeeFixed": "SpotNav starts and stops the charger through the Easee integration's service (pause and resume), so there is no start or stop entity to choose.",
  "control.limit.minutes": "The current can change at most every {count} minutes.",
  "control.limit.seconds": "The current can change at most every {count} seconds.",
  "control.limit.flash": "The current is stored in the charger and is only changed at a charge start.",
  "control.limit.stopOnly": "Load balancing can only stop the charge, not lower the current.",
  "control.limit.installation": "The limit applies to the whole installation.",
  "control.current": "Charging current",
  "control.current.none": "Not set by SpotNav (the charger keeps its own limit)",
  "control.current.ocpp": "OCPP ChangeConfiguration",
  "control.current.number": "A number entity: {name}",
  "control.current.service": "Easee's dynamic current limit",
  "control.current.off": "SpotNav does not set the current. Turn it on in the charger's options.",
  "control.conflict": "The charger's own {label} is on ({name}). It can fight SpotNav: turn it off.",
  "control.disabled": "The charger's own enable switch is off ({name}). SpotNav cannot start it: turn it on.",
  "control.otherController": "{name} also controls chargers; turn it off for this charger or SpotNav and {name} will fight."
};

// src/i18n/fi.ts
var fi = {
  "card.title": "SpotNav",
  "state.loading": "Luetaan lataussuunnitelmaa…",
  "state.unconfigured": "Valitse yksi SpotNav-laturi kortin muokkaimessa.",
  "state.addCharger": "Lisää latauslaite: Asetukset → Laitteet ja palvelut → SpotNav → Lisää merkintä → Latauslaite; se tarjoutuu liittymään tähän kohteeseen.",
  "state.noChargers": "Tähän Home Assistantiin ei ole vielä asetettu SpotNav-latauslaitetta.",
  "state.requestFailed": "Pyyntö epäonnistui. Tarkista yhteys Home Assistant -palveluun ja yritä uudelleen.",
  "state.unsupported": "Kortti ja integraatio käyttävät eri API-versioita. Päivitä molemmat, jotta ne täsmäävät.",
  "state.malformed": "Integraatio vastasi jotain, mitä kortti ei pysty lukemaan. Päivitä molemmat, jotta ne vastaavat toisiaan.",
  "state.chargerMissing": "Laturi on tuntematon, ei ladattu tai kyseessä on asema. Mitään muuta ei valittu tilalle.",
  "state.retry": "Yritä uudelleen",
  "state.noPrices": "Tälle laturille ei ole vielä hintoja.",
  "status.externalInstalled": "Ulkoinen aikataulu: seuraava lataus klo {time}.",
  "status.externalNoSchedule": "Ulkoinen suunnittelu on käytössä; aikataulua ei ole asennettu.",
  "status.chargingNow": "Ladataan nyt; aikataulun mukaan {time} asti.",
  "status.autoPlanned": "Suunniteltu klo {time} alkaen.",
  "status.autoInstalled": "Lataus on aikataulutettu klo {time} alkaen.",
  "status.heldUntilWindow": "Lataus odottaa suunniteltua alkamisaikaa klo {time}.",
  "status.proposalPending": "Uusi latausehdotus on valmis.",
  "status.proposalPendingAt": "Uusi suunnitelma on valmis ja otetaan käyttöön, kun käynnissä oleva latausikkuna päättyy klo {time}.",
  "status.waitingForTomorrow": "Odotetaan huomisen hintoja.",
  "status.waitingForPublication": "Odotetaan huomisen hintoja (~{time}), suunnitellaan sen jälkeen.",
  "status.waitingForHistory": "Odotetaan: {weekday} on ollut {percent} % halvempaa viimeisten {weeks} viikon aikana.",
  "status.waitingForHistoryNoDetail": "Odotetaan tunteja, jotka ovat yleensä halvempia, suunnitellaan sen jälkeen.",
  "status.waitingForPublicationNoTime": "Odotetaan huomisen hintoja, suunnitellaan sen jälkeen.",
  "status.buyingBeforePublication": "Ostetaan {kwh} kWh nyt, loput kun hinnat on julkaistu.",
  "status.noPlan": "Lataussuunnitelmaa ei voitu laskea juuri nyt.",
  "status.loadBalancingLimited": "Aseman kuormanhallinta rajoittaa latausta juuri nyt.",
  "status.loadBalancingLimitedTo": "Aseman kuormanhallinta rajoittaa latauksen {limit} A:iin.",
  "status.loadBalancingLimitedByBattery": "Kotiakku lataa verkosta ja jakaa pääsulakkeen: auto saa {limit} A.",
  "status.loadBalancingLimitedByHouse": "Talon kulutus rajoittaa auton {limit} A:iin.",
  "status.chargingNowOpen": "Ladataan nyt.",
  "status.scheduledNoTime": "Lataus on aikataulutettu.",
  "status.nothingToCharge": "Ei mitään ladattavaa juuri nyt.",
  "status.pausedShort": "Automaattinen lataus on keskeytetty.",
  "status.planEnergy": "{kwh} kWh",
  "status.planCost": "{cost}",
  "status.targetStopped": "Pysäytetty {soc} %:iin",
  "status.targetStoppedAge": "Pysäytetty {soc} %:iin (lukema on {age} vanha)",
  "status.targetStoppedNow": "Pysäytetty {soc} %:iin (juuri äsken)",
  "status.targetStoppedEstimate": "Pysäytetty {soc} %:iin (arvio)",
  "status.targetStoppedEstimateAge": "Pysäytetty {soc} %:iin (arvio, lukema on {age} vanha)",
  "status.targetAgeMinutes": "{n} min",
  "status.targetAgeHours": "{n} h",
  "issue.targetUnverifiable": "Tavoitetta ei voi tarkistaa juuri nyt.",
  "status.planDistance": "{distance}",
  "issue.priceDegraded": "Hintatiedot ovat puutteellisia.",
  "issue.banner.blocking": "Jotain on korjattava ennen kuin lataus voidaan suunnitella.",
  "issue.banner.notice": "Hyvä tietää.",
  "issue.chargerMissing": "Valittua laturia ei voi käyttää: se on tuntematon, ei ladattu tai kyseessä on asema.",
  "issue.chargeControlMissing": "Latauksen ohjausta {entity} ei enää ole. Se on luultavasti nimetty uudelleen tai poistettu: valitse laturin ohjaus uudelleen sen asetuksista.",
  "issue.chargeControlDisabled": "Latauksen ohjaus {entity} on poistettu käytöstä Home Assistantissa, joten SpotNav ei voi käynnistää tai pysäyttää laturia. Ota se uudelleen käyttöön.",
  "issue.unsupported": "Kortti ja integraatio käyttävät eri API-versioita.",
  "issue.malformed": "Taustajärjestelmän vastaus ei ollut kelvollinen API v2 -vastaus.",
  "status.finishSetupArea": "Viimeistele asetukset: valitse hinta-alue Asetuksissa.",
  "status.finishSetup": "Viimeistele asetukset Asetuksissa: {fields}.",
  "status.settingsSuggested": "Ehdotettu sijaintisi ja laturin perusteella – tarkista Asetukset.",
  "status.missing.area": "hinta-alue",
  "status.missing.phases": "vaiheet",
  "status.missing.amps": "latausvirta",
  "status.missing.vehicle": "ajoneuvo",
  "status.missing.target_percent": "tavoitetaso",
  "issue.incompleteSettings": "Viimeistele asetukset: joitakin asetuksia puuttuu vielä. Avaa Asetukset.",
  "issue.missingSettings": "Nämä asetukset puuttuvat: {fields}.",
  "issue.planningUnavailable": "Lataussuunnitelmaa ei voitu laskea käytettävissä olevilla tiedoilla juuri nyt.",
  "issue.solarUnavailable": "Aurinkolataus ei ole käytettävissä: tämä kohde ei voi mitata aurinkoylijäämää.",
  "issue.priceHorizon": "Hinnat puuttuvat osasta suunnitteluikkunaa. Aseta lähtöaika.",
  "issue.error": "Viimeisin laskenta epäonnistui.",
  "issue.priceUnavailable": "Käyttökelpoisia hintatietoja ei ole juuri nyt.",
  "issue.priceInvalid": "Hintatiedot ovat virheellisiä.",
  "issue.priceStale": "Hinnat ovat vanhentuneita: viimeisin onnistunut haku on yhä käytössä.",
  "issue.unpriced": "Osa suunnitelmasta latautuu ilman julkaistuja hintoja.",
  "issue.chargingWithoutPrices": "Ladataan ilman julkaistuja hintoja, jotta määräaika pysyy.",
  "issue.loadBalancing": "Kuormanhallinta ei ole käytettävissä tälle laturille.",
  "issue.heldByCharger": "Laturin oma aikataulu tai kuormanhallinta pidättää latausta, joten se ei ole alkanut.",
  "issue.chargerDisabled": "Laturin oma käyttöönottokytkin on pois päältä, joten lataus ei voi alkaa. Kytke se päälle laturin asetuksista.",
  "issue.holdOverridden": "Lataus käynnistettiin suunnitelman ulkopuolella ja sen annetaan jatkua.",
  "status.siteMeasurement.stale.other": "{phases}: arvot ovat yli {seconds} s vanhoja.",
  "status.siteMeasurement.stale.one": "{phases}: arvo on yli {seconds} s vanha.",
  "status.siteMeasurement.noValue.other": "{phases}: ei arvoa{where}.",
  "status.siteMeasurement.noValue.one": "{phases}: ei arvoa{where}.",
  "issue.duplicateCharger": "{other} ja tämä latauslaite ovat sama fyysinen laite. Kaksi SpotNav-latauslaitetta yhdellä laitteella lähettää sille ristiriitaisia komentoja, joten säilytä vain yksi: poista toinen kohdasta Asetukset → Laitteet ja palvelut → SpotNav. SpotNav ei koskaan poista kumpaakaan puolestasi.",
  "issue.siteMeasurement": "Kohteen mittausta ei voi käyttää juuri nyt.",
  "issue.unknown": "Taustajärjestelmä raportoi jotain, mitä kortti ei vielä tunne.",
  "header.info": "Tietoja kortista",
  "header.settings": "Kortin asetukset",
  "header.history": "Lataushistoria",
  "history.title": "Lataushistoria",
  "history.titleNamed": "Lataushistoria · {name}",
  "history.loading": "Haetaan latausten historiaa…",
  "history.failed": "Latausten historiaa ei voitu lukea.",
  "history.intro": "Mitä kukin lataus maksoi: pörssisähkön hinta sekä sähkövero, siirtomaksu ja arvonlisävero energian toimitushetkellä.",
  "history.thisMonth": "Tässä kuussa",
  "history.lastMonth": "Viime kuussa",
  "history.noneInMonth": "Ei latauksia.",
  "history.days": "Päivät",
  "history.months": "Kuukaudet",
  "history.listLabel": "Näytä",
  "history.sessions.one": "{count} lataus",
  "history.sessions.other": "{count} latausta",
  "history.solar": "{percent} aurinkoa",
  "history.estimated": "arvioitu energia",
  "history.noCost": "ei hintaa",
  "history.savings.saved": "Arvioitu säästö: {amount} päivän keskihintaan verrattuna",
  "history.savings.extra": "Arviolta {amount} enemmän kuin päivän keskihinta",
  "history.savings.note": "Säästö on arvio: sama energia kunkin päivän keskihintaan.",
  "history.latest": "Viimeisimmät lataukset",
  "history.empty": "Latauksia ei ole vielä tallennettu. Ne näkyvät tässä seuraavan latauksen jälkeen.",
  "history.open": "Latautuu nyt kello {time} alkaen: {energy}",
  "history.by.plan_window": "suunniteltu ikkuna",
  "history.by.manual": "käynnistetty käsin",
  "history.by.solar": "aurinkoylijäämä",
  "history.by.hybrid": "hybridi",
  "history.by.other": "käynnistetty muualla",
  "history.export.period": "Ajanjakso",
  "history.range.thisMonth": "Tässä kuussa",
  "history.range.lastMonth": "Viime kuussa",
  "history.range.last12": "Viimeiset 12 kuukautta",
  "history.range.all": "Kaikki",
  "history.export": "Vie CSV",
  "history.exportFailed": "Vienti epäonnistui.",
  "settings.overview.title": "Kortin asetukset",
  "settings.section.market": "Hinta-alue ja verot",
  "settings.section.vehicle": "Ajoneuvo",
  "settings.section.capabilities": "Anturit ja ominaisuudet",
  "settings.section.site": "Kohde",
  "settings.section.configure": "Muokkaa",
  "settings.value.off": "Pois",
  "settings.fiscal.vat": "ALV",
  "settings.fiscal.tax": "Sähkövero",
  "settings.fiscal.transfer": "Siirtomaksu",
  "settings.consumption.unit": "kWh/10 km",
  "settings.section.entities": "Laturi",
  "entity.group.site": "Laitoksen entiteetit",
  "entity.notSet": "Ei asetettu",
  "entity.edit.charger": "Muuta latauslaitteen entiteettejä",
  "entity.edit.site": "Muuta laitoksen entiteettejä",
  "entity.editor.charger": "Latauslaitteen entiteetit",
  "entity.editor.site": "Laitoksen entiteetit",
  "entity.loading": "Luetaan entiteettejä…",
  "entity.adminOnly": "Vain ylläpitäjät voivat muuttaa entiteettejä.",
  "entity.error.read": "Entiteettejä ei voitu lukea.",
  "entity.field.chargeControl": "Latauksen ohjauskytkin",
  "entity.field.currentLimit": "Virtaraja",
  "entity.field.energyRegister": "Energialaskuri",
  "entity.field.powerEntity": "Tehoanturi (älypistoke)",
  "entity.field.vehicleSoc": "Ajoneuvon lataustaso",
  "entity.field.mainFuse": "Pääsulake",
  "entity.field.safetyMargin": "Turvamarginaali",
  "entity.field.measurementMode": "Mittaustapa",
  "entity.field.voltageBetweenPhases": "Vaiheiden välinen jännite",
  "entity.field.chargerPriority": "Laturin prioriteetti",
  "entity.field.batteryPower": "Akun teho",
  "entity.field.maxAge": "Mittauksen enimmäisikä",
  "entity.mode.direct": "Vaihevirrat mitataan suoraan",
  "entity.mode.derived": "Vaihevirrat lasketaan tehosta ja jännitteestä",
  "entity.phase.direct": "Virta, {phase}",
  "entity.derived.power": "Teho",
  "entity.derived.reactivePower": "Loisteho",
  "entity.derived.voltage": "Jännite",
  "entity.error.field.required": "Valitse entiteetti.",
  "entity.error.field.notFound": "Entiteettiä ei ole.",
  "entity.error.field.wrongDomain": "Entiteetti on väärää tyyppiä.",
  "entity.error.field.invalid": "Arvoa ei hyväksytä.",
  "entity.error.field.notWritable": "Tätä ei voi muuttaa täällä.",
  "entity.error.field.controlPathUnknown": "SpotNav ei voi päätellä, miten lataus käynnistetään ja pysäytetään tällä entiteetillä. Valitse laturin kytkin tai valitsin, jossa on selkeät käynnistys- ja pysäytysvaihtoehdot.",
  "entity.error.field.unknown": "Kenttää ei tunnisteta.",
  "entity.error.field.chargeControlInUse": "Toinen latauslaite käyttää jo tätä kytkintä.",
  "entity.error.field.currentLimitInUse": "Toinen latauslaite käyttää jo tätä virtarajaa.",
  "entity.error.field.duplicateCharger": "Toinen SpotNav-latauslaite on jo sama laite.",
  "entity.error.conflict": "Muutettu muualla. Nykyiset arvot näytetään, eikä mitään tallennettu.",
  "entity.error.invalid": "Joitakin arvoja ei hyväksytty. Mitään ei tallennettu.",
  "entity.error.generic": "Entiteettejä ei voitu muuttaa. Mikään ei muuttunut.",
  "entity.error.notAdmin": "Vain ylläpitäjät voivat muuttaa entiteettejä.",
  "entity.missing.optional": "Tätä entiteettiä ei enää ole Home Assistantissa. Tyhjennä kenttä tai valitse toinen entiteetti.",
  "entity.missing.required": "Tätä entiteettiä ei enää ole Home Assistantissa. Valitse toinen entiteetti.",
  "entity.automatic": "Automaattinen: {name}",
  "entity.help.chargeControl": "Kytkin, joka käynnistää ja pysäyttää latauksen. SpotNav kytkee sen päälle ja pois suunnitelman mukaan.",
  "entity.help.currentLimit": "Entiteetti, joka kertoo laturin asetetun virran. Automaattinen käyttää sitä, jonka SpotNav löytää itse, OCPP 0.12 -latureilla istuntorajaa.",
  "entity.help.energyRegister": "Laturin kumulatiivinen kWh-mittari. SpotNav käyttää sitä tietääkseen, mitä on jo ladattu. Löytyy automaattisesti OCPP-latureille.",
  "entity.help.powerEntity": "Älypistokkeen takana olevalle latauslaitteelle: pistokkeen tehoanturi, W tai kW. SpotNav laskee energian siitä ja huomaa, kun auto ei enää ota virtaa. Pistokkeen on kestettävä latauslaitteen jatkuva virta.",
  "entity.help.vehicleSoc": "Ajoneuvon varaustaso, luettuna ajoneuvolle valitusta anturista tai löydettynä automaattisesti, kun sillä on vain yksi.",
  "entity.help.mainFuse": "Kohteen pääsulake ampeereina. Kaikki kohteen laturit pysyvät yhdessä sen alapuolella.",
  "entity.help.safetyMargin": "Ampeereina se virta, jonka SpotNav pitää vapaana pääsulakkeen alapuolella. Lataus pysyy sulakkeen ja marginaalin erotuksen alla, joten marginaalin on oltava sulaketta pienempi.",
  "entity.help.measurementMode": "Ilmoittaako mittarisi kunkin vaiheen virran suoraan vai laskeeko SpotNav sen tehosta ja jännitteestä.",
  "entity.help.voltageBetweenPhases": "Sähköasennuksesi kahden vaiheen välinen jännite. Kolmivaiheinen latausteho lasketaan siitä.",
  "entity.help.chargerPriority": "Kun useampi latauspiste jakaa kohteen sulakkeen, Ensin-asetuksen laturi saa virtaa ennen muita ja Viimeksi-asetuksen laturi sen, mikä jää jäljelle.",
  "entity.voltage.tn": "400 V (TN-verkko, tavallinen)",
  "entity.voltage.it": "230 V (IT-verkko, yleinen Norjassa)",
  "entity.priority.first": "Ensin",
  "entity.priority.normal": "Normaali",
  "entity.priority.last": "Viimeksi",
  "entity.help.batteryPower": "Kotiakun tehon anturi, jotta SpotNav voi ottaa akun huomioon.",
  "entity.help.maxAge": "Kuinka vanha mittaus saa olla sekunteina, ennen kuin SpotNav lakkaa luottamasta siihen.",
  "entity.help.phaseDirect": "Anturi, joka mittaa tämän vaiheen virran ampeereina.",
  "entity.help.derivedPower": "Anturi tämän vaiheen pätöteholle.",
  "entity.help.derivedReactivePower": "Anturi tämän vaiheen loisteholle.",
  "entity.help.derivedVoltage": "Anturi tämän vaiheen jännitteelle.",
  "entity.flag.on": "Päällä",
  "entity.flag.off": "Pois",
  "entity.field.siteCurrentSigned": "Verkkovirralla on etumerkki",
  "entity.field.gridPowerInverted": "Anturi näyttää viennin positiivisena",
  "entity.field.gridPowerSource": "Verkon kokonaisteho (aurinkoa varten)",
  "entity.field.gridPowerSourceExport": "Viennin kokonaisteho (jos erillinen)",
  "entity.field.batteryDischargePower": "Akun purkausteho",
  "entity.field.batteryPowerInverted": "Anturi näyttää purun positiivisena",
  "entity.derived.powerExport": "Vientiteho",
  "entity.derived.apparentPower": "Näennäisteho",
  "entity.derived.current": "Virta",
  "entity.help.siteCurrentSigned": "Kytke päälle, jos mittari ilmoittaa negatiivisen virran viennin aikana. SpotNav käyttää silloin virran suuruutta, joka kuormittaa sulaketta, sen sijaan että hylkäisi sen.",
  "entity.help.gridPowerInverted": "Kytke päälle, jos mittarin teho on positiivinen viennin aikana (Huawei, SolarEdge, GoodWe ja vastaavat). SpotNav lukee silloin oton positiivisena. Se koskee myös verkon kokonaistehoa.",
  "entity.help.gridPowerSource": "Tarvitaan aurinko- ja hybridilatauksessa, kun vaiheet ilmoittavat vain virran.",
  "entity.help.gridPowerSourceExport": "Vain jos mittari ilmoittaa oton ja viennin kahtena anturina: yllä oleva kenttä on silloin otto ja tämä on vienti.",
  "entity.help.batteryPowerInverted": "Kytke päälle, jos akun teho on positiivinen purun aikana (Tesla, Fronius, Enphase, GoodWe ja vastaavat). SpotNav lukee silloin latauksen positiivisena.",
  "entity.help.batteryDischargePower": "Vain akulle, joka ilmoittaa latauksen ja purun kahtena anturina: tämä on purku, ja yllä oleva akkuanturi on lataus.",
  "entity.help.derivedPowerExport": "Vain kun mittari ilmoittaa oton ja viennin kahtena anturina: yllä oleva tehoanturi on silloin otto ja tämä on vienti.",
  "entity.help.derivedApparentPower": "Näennäisteho tällä vaiheella, VA. Sen kanssa virta on tarkka.",
  "entity.help.derivedCurrent": "Mittarin oma virta tällä vaiheella. Sen kanssa virta on tarkka.",
  "entity.choice.mixed": "Useampi näistä on asetettu. Vain valittu säilytetään; muut tyhjennetään tallennettaessa.",
  "entity.choice.choose": "Valitse entiteetti",
  "entity.limit.none": "Ei mitään (SpotNav ei aseta virtaa)",
  "entity.grid.title": "Verkon teho",
  "entity.grid.one": "Yksi anturi suunnan kanssa",
  "entity.grid.two": "Otto ja vienti kahtena anturina",
  "entity.current.title": "Virta otetaan",
  "entity.current.measured": "Mittarin oma virta",
  "entity.current.apparent": "Näennäisteho",
  "entity.current.reactive": "Loisteho",
  "entity.current.estimated": "Arvioitu (tehokerroin 0,9)",
  "entity.current.estimatedNote": "SpotNav arvioi virran tehosta. Arvio on merkitty kortissa.",
  "entity.battery.title": "Akku",
  "entity.battery.none": "Ei mitään",
  "entity.battery.one": "Yksi anturi",
  "entity.battery.two": "Lataus ja purku kahtena anturina",
  "entity.energy.title": "Energia",
  "entity.energy.meter": "Energiamittari (kWh)",
  "entity.energy.power": "Teho (W) — SpotNav laskee energian",
  "entity.energy.none": "Ei mitään",
  "entity.notice.estimated": "Virta on arvioitu tehosta olettaen tehokertoimeksi {pf} tai parempi. Arvio ei ole koskaan pienempi kuin todellinen virta tällä tehokertoimella tai paremmalla, ja aliarvioi sen sen alapuolella. Lisää mittarin virta, näennäisteho tai loisteho saadaksesi tarkan arvon.",
  "entity.notice.estimatedShort": "Arvioitu",
  "entity.warning.updateInterval": "{integration} päivittää {seconds} s välein, hitaammin kuin mittausten enimmäisikä.",
  "entity.warning.updateIntervalOption": 'Lyhennä sitä asetuksella "{option}" kyseisessä integraatiossa.',
  "entity.warning.onChange": "{integration} ilmoittaa vain arvon muuttuessa, joten vakaa arvo voi näyttää vanhalta. Nosta mittausten enimmäisikää, jos kohde ilmoitetaan usein vanhentuneeksi.",
  "entity.warning.ownBalancing": "{name} ({integration}) tasapainottaa kuorman itse ja voi häiritä SpotNavin aktiivista ohjausta. Käytä toista.",
  "entity.warning.externalBalancer": "{integration} tasapainottaa laitteen {name} virran itse, joten SpotNav käynnistää ja pysäyttää laturin mutta ei kirjoita sen virtaa.",
  "entity.warning.unknown": "Kohteella on ilmoitus, jota tämä versio ei osaa näyttää. Päivitä SpotNav.",
  "entity.detect.title": "Löytyi Home Assistantista",
  "entity.detect.intro": "Nämä tunnistettiin asetuksistasi. Mikään ei muutu ennen kuin painat Käytä.",
  "entity.site.intro": "Kohteen mittaus. {applies}",
  "entity.detect.meters": "Sähkömittarit",
  "entity.detect.batteries": "Kotiakut",
  "entity.detect.use": "Käytä",
  "entity.detect.inUse": "Käytössä",
  "entity.detect.direct": "Mittaa vaihevirran suoraan",
  "entity.detect.derived": "Johdettu tehosta ja jännitteestä",
  "entity.detect.signed": "Virralla on etumerkki: luetaan suuruutena",
  "entity.detect.inverted": "Teho vienti positiivinen: käännetään",
  "entity.detect.gridPower": "Verkon kokonaisteho löytyi: käytetään aurinko- ja hybridilatauksessa",
  "entity.detect.estimated": "Arvioitu virta: virtaa, näennäis- tai loistehoa ei löytynyt",
  "entity.detect.enable": "Ottaa käyttöön {count} entiteettiä, jotka integraatio toimittaa poissa käytöstä.",
  "entity.detect.battery.inverted": "Purku positiivinen: käännetään",
  "entity.detect.battery.pair": "Lataus ja purku ovat kaksi anturia",
  "entity.detect.confidence.low": "Alhainen varmuus: tarkista, että etumerkit ovat oikein.",
  "entity.detect.warning.own_load_balancing": "Tämä laite tasapainottaa kuorman itse.",
  "entity.detect.warning.sign_unverified": "Tehon etumerkki on tuntematon: tarkista, että vienti on negatiivinen.",
  "entity.detect.warning.voltage_from_other_device": "Mittari ei raportoi jännitettä, joten käytetään toisen laitteen, yleensä vaihtosuuntaajan, vaihejännitettä. Tämä on normaalia.",
  "entity.detect.warning.may_measure_subcircuit": "Varmista, että mittari mittaa koko pääsyötön, ei alipiiriä.",
  "entity.detect.warning.reports_on_change_only": "Ilmoittaa vain arvon muuttuessa.",
  "market.edit": "Muuta hinta-aluetta ja veroja",
  "settings.vehicle.change": "Muuta ajoneuvoa",
  "settings.vehicle.none": "Ajoneuvoa ei ole vielä asetettu.",
  "settings.vehicle.dialogTitle": "Ajoneuvo · {name}",
  "settings.vehicle.sensorLegend": "Varaustason anturi",
  "entity.foundAutomatically": "Löytyy automaattisesti",
  "site.row.battery": "Akku",
  "settings.value.none": "Ei mitään",
  "settings.section.solar": "Aurinko",
  "site.solar.change": "Muuta aurinkoasetuksia",
  "site.solar.dialogTitle": "Aurinkoasetukset",
  "strategy.setupSolar": "Määritä aurinko asetuksissa",
  "strategy.setupSite": "Avaa laitoksen asetukset",
  "site.none": "Tällä laturilla ei ole kohdetta.",
  "site.applies.one": "Koskee tämän kohteen ainoaa laturia.",
  "site.applies.other": "Koskee kaikkia {count} laturia tässä kohteessa.",
  "site.solarPriority.title": "Aurinkoenergian etusija",
  "site.solarPriority.carFirst": "Auto ensin",
  "site.solarPriority.batteryFirst": "Akku ensin",
  "site.solarForecast.title": "Aurinkoennusteen lähteet",
  "site.solarForecast.none": "Ei valittu. Ilman lähdettä hybridi suunnittelee latauksen kuin Halvin.",
  "site.activeControl.title": "Aktiivinen kuormanhallinta",
  "site.activeControl.on": "Päällä",
  "site.activeControl.off": "Pois",
  "site.activeControl.available": "Käytettävissä",
  "site.activeControl.reason.duplicateMembership": "Ei käytettävissä: tämä laturi kuuluu useampaan kuin yhteen kohteeseen.",
  "site.activeControl.reason.noCommandableCharger": "Ei käytettävissä: yksikään kohteen laturi ei ota vastaan virtakomentoa.",
  "site.activeControl.reason.measurement": "Ei käytettävissä: kohteen oma tehomittaus ei ole kunnossa.",
  "site.activeControl.note": "Toimii parhaan kyvyn mukaan, ei ole suojalaite. Kun kytket pois, laturi saa takaisin sen virran, jota oli laskettu.",
  "site.activeControl.pending": "Tallennetaan…",
  "site.activeControl.error.unavailable": "Kuormanhallintaa ei voi ottaa käyttöön juuri nyt: kohdetta ei voi ohjata aktiivisesti tällä hetkellä. Mitään ei muutettu.",
  "site.activeControl.error.confirmation": "Muutosta ei voitu vahvistaa, joten aiempi asetus säilytettiin.",
  "site.activeControl.restore.off": "Kuormanhallinta on pois päältä.",
  "site.activeControl.restore.notNeeded": "Yhdenkään laturin virtaa ei tarvinnut palauttaa.",
  "site.activeControl.restore.restoredOne": "Laturi palautettiin arvoon {to} A.",
  "site.activeControl.restore.restoredNamed": "{name} palautettiin arvoon {to} A.",
  "site.activeControl.restore.unnamed": "Laturi",
  "site.activeControl.restore.line": "{name}: {text}",
  "site.activeControl.restore.failed.write_failed": "Laturi hylkäsi virran palautuskomennon ja voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.unconfirmed": "Laturi ei vahvistanut palautettua virtaa ja voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.assigned_current_unreadable": "Laturin virta-asetusta ei voitu lukea, joten laturi voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.no_authoritative_current": "SpotNavilla ei ole pyydettyä virtaa, johon laturi palautettaisiin, joten se voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.charger_not_loaded": "Laturia ei ole ladattu Home Assistantissa, joten se voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.membership_conflict": "Laturi kuuluu useampaan kuin yhteen kohteeseen eikä sitä muutettu, joten se voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.probe_in_flight": "Laturi oli varattu virtatestiin ja voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.below_minimum": "Palautettava virta on pienempi kuin laturi hyväksyy, joten se voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.external_balancer": "Toinen integraatio tasapainottaa laturin virran, joten SpotNav ei kirjoittanut sitä, ja se voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.no_connector_target": "SpotNav ei löytänyt ohjattavaa laturin liitintä, joten laturi voi yhä olla rajoitettu.",
  "site.activeControl.restore.failed.unknown": "Laturia ei voitu palauttaa, joten se voi yhä olla rajoitettu.",
  "site.error.conflict": "Muutettu muualla. Nykyiset arvot näytetään.",
  "settings.consumption.label": "Kulutus",
  "settings.value.unset": "Ei asetettu",
  "issue.count.one": "{count} kohta tarkistettavana",
  "issue.count.other": "{count} kohtaa tarkistettavana",
  "graph.title": "Lataussuunnitelma",
  "graph.description": "Hintakaavio välillä {from}–{to}, yksikössä {unit} / kWh. {count} jaksoa, {days} päivää. Valittu: {selected}.",
  "graph.descriptionNoSelection": "Ei valintaa.",
  "graph.descriptionNow": "Nykyinen jakso: {now}.",
  "graph.legend.today": "Tänään",
  "graph.legend.tomorrow": "Huomenna",
  "graph.legend.past": "Aiempi päivä",
  "graph.legend.current": "Nykyinen jakso",
  "graph.legend.selection": "Valittu jakso",
  "graph.legend.cheap": "Halvempi kuin päivän keskiarvo",
  "graph.legend.expensive": "Kalliimpi kuin päivän keskiarvo",
  "graph.legend.installed": "Aikataulutettu",
  "graph.legend.proposal": "Ehdotus",
  "graph.summary.max": "Enintään",
  "graph.summary.min": "Vähintään",
  "graph.summary.current": "Nyt",
  "graph.readout": "{day} {time} · {price}",
  "graph.readoutMissing": "{day} {time} · ei hintaa",
  "graph.readoutWithOffset": "{day} {time} ({offset}) · {price}",
  "graph.readoutMissingWithOffset": "{day} {time} ({offset}) · ei hintaa",
  "graph.selected": "Valittu hinta",
  "graph.priceAxis": "Hinta, {unit} per kWh",
  "graph.noZone": "Ajat eivät ole käytettävissä: integraatio ei ole raportoinut markkina-aluetta tälle laturille.",
  "graph.hint": "Selaa jaksoja nuolinäppäimillä.",
  "graph.keyboardInstructions": "Hintakaavio. Nuolinäppäimet siirtyvät jaksosta toiseen, Home ja End hyppäävät päihin, Escape tyhjentää valinnan.",
  "plan.proposal.one": "Halvin latausjakso ({count})",
  "plan.proposal.other": "Halvimmat latausjaksot ({count})",
  "plan.installed.one": "Aikataulutettu latausjakso ({count})",
  "plan.installed.other": "Aikataulutetut latausjaksot ({count})",
  "plan.period": "{day} {from}–{to}",
  "plan.periodWithOffset": "{day} {from}–{to} ({offset})",
  "plan.cost": "Kustannus",
  "plan.energy": "Energia",
  "plan.distance": "Matka",
  "plan.power": "Teho",
  "plan.activeNow": "Nyt",
  "plan.missing": "tuntematon",
  "cap.title": "Mitä laturi osaa",
  "cap.intro": "Perustuu siihen, mitä integraatio raportoi tälle laturille. Sitä, mikä ei ole täällä käytettävissä, kortti ei voi kytkeä päälle.",
  "cap.autoPrice": "Automaattinen hintaohjaus",
  "cap.currentLimit": "Dynaaminen virran rajoitus",
  "cap.loadBalancing": "Kuormanhallinta",
  "cap.targetSoc": "Ajoneuvon lataustaso ja tavoite",
  "cap.available": "Käytettävissä",
  "cap.unavailable": "Ei käytettävissä tälle laturille",
  "action.start": "Aloita nyt",
  "action.stop": "Pysäytä",
  "action.resume": "Jatka automaattista latausta",
  "action.startHelp": "Aloittaa latauksen nyt. Se ei lupaa kestoa: Auto voi ottaa ohjat seuraavassa täsmäytyksessä.",
  "action.pauseAutomatic": "Keskeytä automaattinen lataus",
  "action.pauseAutomaticShort": "Keskeytä",
  "action.resumeShort": "Jatka",
  "pause.sheetTitle": "Keskeytä automaattinen lataus",
  "pause.intro": "Valitse, kuinka kauan tauko kestää. Vain aikaan sidotut valinnat päättyvät itsestään.",
  "pause.nextPeriod": "Seuraavaan suunniteltuun jaksoon asti",
  "pause.untilTomorrow": "Huomiseen asti",
  "pause.untilResumed": "Kunnes jatkan",
  "strategy.title": "Strategia",
  "bar.charging": "Lataus",
  "bar.schedule": "Aikataulu",
  "bar.start": "Aloita",
  "bar.waiting": "Käynnistyy…",
  "bar.stop": "Pysäytä",
  "bar.chargeNow": "Lataa nyt",
  "bar.chargingNow": "Latautuu",
  "bar.scheduleActive": "Aikataulu aktiivinen",
  "bar.schedulePaused": "Aikataulu tauolla",
  "bar.state.notCharging": "ei lataa",
  "bar.state.charging": "latautuu",
  "bar.state.scheduleActive": "aktiivinen",
  "bar.state.schedulePaused": "tauolla",
  "bar.change": "muuta",
  "strategy.dialogTitle": "Latausstrategia",
  "strategy.intro": "Mitä suunnitelma optimoi. Strategiaa, joka ei ole täällä käytettävissä, ei voi vielä valita.",
  "strategy.cheapest": "Halvin",
  "strategy.solar": "Aurinko",
  "strategy.hybrid": "Hybridi",
  "strategy.reason.solar": "Vaatii aurinkoylijäämän mittauksen",
  "strategy.reason.hybrid": "Vaatii aurinko- ja hintaohjauksen",
  "strategy.reason.totalPower": "Aurinko vaatii mittarin verkon kokonaistehon",
  "strategy.status.solar.charging": "Aurinko · lataa {amps} A ylijäämästä",
  "strategy.status.solar.chargingUnknown": "Aurinko · lataa ylijäämästä",
  "strategy.status.solar.arming": "Aurinko · ylijäämää löytyi, käynnistyy pian",
  "strategy.status.solar.disarming": "Aurinko · ylijäämä vähenee, pysähtyy pian",
  "strategy.status.solar.noReadingStopped": "Aurinko · pysäytetty, ei käyttökelpoista mittausta",
  "strategy.status.solar.noReadingWaiting": "Aurinko · ei vielä käyttökelpoista mittausta",
  "strategy.status.solar.waitingForSun": "Aurinko · odottaa aurinkoa",
  "strategy.status.solar.unknown": "Aurinko · tila tuntematon",
  "strategy.status.hybrid.grid": "Hybridi · {grid} kWh verkosta{window}",
  "strategy.status.hybrid.creditSuffix": ", {credit} kWh odotetaan auringosta",
  "strategy.status.hybrid.noForecast": "Hybridi · ei ennustelähdettä — suunnittelee kuin Halvin",
  "strategy.status.hybrid.noPriceData": "Hybridi · odottaa hintatietoja",
  "strategy.status.hybrid.satisfied": "Hybridi · lataustarve on jo täytetty",
  "strategy.status.hybrid.unknown": "Hybridi · suunnittelee",
  "advisory.vehicleNotRequestingCurrent": "Lataus aloitettiin, mutta ajoneuvo ei pyydä virtaa. Tarkista ajoneuvon latausasetukset tai kytke kaapeli uudelleen.",
  "advisory.powerBelowThreshold": "Lataus käynnistettiin, mutta latauslaite ei ota juuri lainkaan tehoa. Auto voi olla valmis tai ei lataa: tarkista ajoneuvon latausasetukset tai kytke kaapeli uudelleen.",
  "control.noSettings": "Tällä laturilla ei ole vielä asetuksia, joten aloitettavaa tai pysäytettävää ei ole.",
  "control.pauseUnsettled": "Tauko on tallennettu, mutta se ei ole vielä voimassa. Mitään ei sovelleta.",
  "control.pauseClearFailed": "Tauko on päättynyt, mutta sitä ei voitu poistaa, joten mitään ei sovelleta.",
  "control.pauseStopFailed": "Laturia ei voitu pysäyttää, kun tauko hyväksyttiin. Yritä Pysäytä uudelleen.",
  "control.startNotAcknowledged": "Laturi ei vahvistanut aloituskomentoa. Yritä uudelleen.",
  "control.reconcileFailed": "Viimeisin muutos tallennettiin, mutta suunnitelmaa ei voitu päivittää.",
  "control.executionError": "Integraatio ilmoitti ongelmasta viimeisimmässä suorituksessa.",
  "control.pausedUntil": "Keskeytetty {time} asti.",
  "control.pausedIndefinitely": "Keskeytetty, kunnes jatkat.",
  "control.actionPending": "Aloituskomento odottaa laturin vahvistusta.",
  "action.error.unavailable": "Tämä toiminto ei ole juuri nyt käytettävissä.",
  "action.error.failed": "Toimintoa ei voitu suorittaa. Mikään ei muuttunut.",
  "action.error.reconcileFailed": "Toiminto suoritettiin, mutta suunnitelmaa ei voitu päivittää.",
  "action.error.confirmationFailed": "Toiminto hyväksyttiin, mutta sen nykyistä tilaa ei voitu vahvistaa.",
  "action.error.invalidPause": "Tämä taukovaihtoehto ei ole juuri nyt käytettävissä tälle laturille.",
  "action.error.charger": "Määritettyä laturia ei tavoitettu.",
  "action.error.version": "Kortti ja integraatio käyttävät eri toimintoversioita.",
  "action.error.generic": "Toiminto epäonnistui. Tarkista yhteys Home Assistant -palveluun ja yritä uudelleen.",
  "dialog.close": "Sulje",
  "dialog.issues": "Nämä kaipaavat huomiota",
  "dialog.issuesIntro": "Kaikki, mitä integraatio raportoi tästä laturista, raportointijärjestyksessä.",
  "context.area": "Alue",
  "context.currency": "Valuutta",
  "market.aria": "Muuta hinta-aluetta ja veroja: {value}",
  "market.unset": "Ei asetettu",
  "market.title": "Alue ja verot",
  "market.intro": "Miltä markkinalta laturi ostaa sähkönsä, ja mitkä verot hintoihin lisätään.",
  "market.loading": "Luetaan alueluetteloa…",
  "market.readOnly": "Vain ylläpitäjät voivat muuttaa aluetta ja veroja. Voit lukea ne tästä.",
  "market.area.label": "Alue",
  "market.area.description": "Alue määrää, mitä hintoja käytetään ja mitä veroja ehdotetaan.",
  "market.area.unlisted": "ei enää julkaistu",
  "market.area.missing": "Alueita ei julkaista juuri nyt.",
  "market.vat.label": "Arvonlisävero",
  "market.vat.description": "Arvonlisävero prosentteina.",
  "market.tax.label": "Energiavero",
  "market.tax.description": "Energiavero alueen pienimpänä valuuttayksikkönä.",
  "market.transfer.label": "Verkkosiirto",
  "market.transfer.description": "Verkkosiirto alueen pienimpänä valuuttayksikkönä.",
  "market.enabled.aria": "{component}: käytössä",
  "market.resetToSuggestion": "Palauta ehdotukseen",
  "market.value.aria": "{component}: oma arvo",
  "market.suggestion.label": "Ehdotettu: {value} {unit}",
  "market.suggestion.none": "Alueelle ei julkaista ehdotettua arvoa.",
  "market.error.read": "Alueluetteloa ei voitu lukea.",
  "market.error.version": "Tämä kortti ja integraatio puhuvat eri markkinaversioita.",
  "market.error.areaRequired": "Valitse alue ennen tallennusta.",
  "market.error.areaUnknown": "Se alue ei ole julkaistu vaihtoehto.",
  "market.error.noSuggestion": "Alue ei julkaise ehdotettua arvoa tälle.",
  "market.state.loading": "Alueluetteloa luetaan yhä.",
  "market.state.staleOffline": "Tämä on viimeisin Home Assistantin vastaanottama alueluettelo; releeseen ei ole saatu yhteyttä sen jälkeen.",
  "market.state.staleInvalid": "Tämä on viimeisin Home Assistantin vastaanottama alueluettelo; releen uudempaa luetteloa ei voitu lukea.",
  "market.state.offline": "Releeseen ei saatu yhteyttä, joten alueita ei luetella juuri nyt.",
  "market.state.invalid": "Releen alueluetteloa ei voitu lukea, joten alueita ei luetella juuri nyt.",
  "settings.energy.aria": "Energia: {value}",
  "settings.deadline.aria": "Määräaika: {value}",
  "settings.current.aria": "Virta: {value}",
  "settings.energy.unset": "Ei asetettu",
  "settings.deadline.none": "Ei määräaikaa",
  "settings.current.unset": "Ei asetettu",
  "settings.energy.title": "Latausenergia",
  "settings.energy.intro": "Kuinka paljon energiaa latauksen pitää tuottaa. Suunnitelma muuttaa sen jaksoiksi.",
  "settings.energy.label": "Toivottu energia",
  "settings.energy.targetSoc": "Tämä laturi suunnittelee lataustason tavoitteesta, joten manuaalista energiaa ei voi muuttaa tässä.",
  "settings.deadline.title": "Määräaika ja latausjaksot",
  "settings.deadline.intro": "Milloin lataus on viimeistään valmis ja kuinka monta jaksoa se saa käyttää.",
  "settings.deadline.enabled": "Valmis määräaikaan mennessä",
  "settings.deadline.time": "Lähtöaika",
  "settings.deadline.date": "Lähtö",
  "settings.deadline.dateDaily": "Joka päivä",
  "settings.deadline.dateOn": "Tietty päivämäärä",
  "settings.deadline.dateHelp": "Suunnitelma voi odottaa tunteja, jotka ovat yleensä halvempia. Lähtö pidetään aina.",
  "settings.deadline.datePast": "Päivämäärä on mennyt ohi, joten suunnitelma toimii joka päivä, kunnes valitset uuden päivämäärän. Tallennus tyhjentää sen.",
  "settings.deadline.weekdays": "Viikonpäivät",
  "settings.deadline.weekdaysHelp": "Pois jätettynä päivänä ei ole lähtöä: suunnitelma ulottuu seuraavaan valitsemaasi päivään.",
  "settings.deadline.today": "tänään",
  "settings.deadline.tomorrow": "huomenna",
  "settings.deadline.periods": "Latausjaksojen enimmäismäärä",
  "settings.current.title": "Suunniteltu virta",
  "settings.current.intro": "Virta, jota suunnitelma saa pyytää. Se on suunnitteluarvo, ei komento laturille.",
  "settings.current.label": "Suunniteltu virta",
  "settings.loading": "Luetaan nykyisiä asetuksia…",
  "settings.section.support": "Tuki",
  "debug.intro": "Tallentaa yhden tiedoston, jossa on versiot, tila ja viimeisimmät lokirivit vikailmoitusta varten. Salaisuudet ja tarkka sijaintisi jätetään pois.",
  "debug.download": "Lataa vianetsintätiedot",
  "debug.preparing": "Valmistellaan…",
  "debug.error.notAdmin": "Vain ylläpitäjät voivat ladata vianetsintätiedot.",
  "debug.error.failed": "Vianetsintätietoja ei voitu hakea.",
  "settings.readOnly": "Vain ylläpitäjät voivat muuttaa asetuksia. Voit lukea ne tässä.",
  "settings.save": "Tallenna",
  "settings.cancel": "Peruuta",
  "settings.reload": "Käytä palvelimen arvoja",
  "settings.reapply": "Käytä muutostani uudelleen",
  "settings.conflict.title": "Muutettu muualla",
  "settings.conflict.intro": "Näitä asetuksia muutettiin sen jälkeen, kun tämä valintaikkuna avattiin. Mitään ei tallennettu, ja arvosi ovat edelleen tässä.",
  "settings.error.required": "Täytä tämä ennen tallentamista.",
  "settings.error.invalidNumber": "Se ei ole luku.",
  "settings.error.outOfRange": "Arvo on sallitun alueen ulkopuolella.",
  "settings.error.invalidDate": "Valitse kelvollinen päivämäärä.",
  "settings.error.dateRange": "Valitse päivämäärä tästä päivästä enintään 7 päivän päähän.",
  "settings.error.weekdays": "Valitse vähintään yksi viikonpäivä.",
  "settings.error.invalidTime": "Käytä aikaa kuten 06:30.",
  "settings.error.read": "Asetuksia ei voitu lukea.",
  "settings.error.refused": "Asetukset hylättiin. Mikään ei muuttunut.",
  "settings.error.notCommitted": "Asetuksia ei tallennettu. Mikään ei muuttunut.",
  "settings.error.reconcileFailed": "Asetukset tallennettiin, mutta suunnitelmaa ei voitu päivittää.",
  "settings.error.confirmationFailed": "Asetukset tallennettiin, mutta nykyistä tilaa ei voitu vahvistaa.",
  "settings.error.version": "Kortti ja integraatio käyttävät eri asetusversioita.",
  "settings.error.unavailable": "Asetukset eivät ole juuri nyt käytettävissä.",
  "settings.error.charger": "Määritettyä laturia ei tavoitettu.",
  "settings.error.generic": "Asetuksia ei voitu muuttaa. Mikään ei muuttunut.",
  "settings.error.invalid": "Asetukset hylättiin virheellisinä. Mikään ei muuttunut.",
  "settings.error.readOnly": "Vain ylläpitäjät voivat muuttaa asetuksia.",
  "settings.energy.slider": "Energialiukusäädin, 0,5–100 kWh puolen kWh:n askelin",
  "settings.current.slider": "Virran liukusäädin, {min}–{max} A kokonaisina ampeereina",
  "settings.sliderOutOfRange": "Tarkka arvo on liukusäätimen alueen ulkopuolella. Käytä numerokenttää.",
  "settings.current.power": "Nimellisteho ≈ {power} kW",
  "settings.current.powerUnknown": "Nimellisteho tuntematon",
  "bar.plan": "Suunnitelma",
  "settings.plan.title": "Latausuunnitelma",
  "settings.plan.intro": "Kuinka paljon ladataan, milloin lataus on oltava valmis ja kuinka suurta virtaa suunnitelma saa pyytää. Nämä ovat suunnitteluarvoja, eivät komentoja latauslaitteelle.",
  "settings.plan.aria": "Suunnitelma: {value}",
  "entity.vehicle.automatic": "Automaattinen tunnistus",
  "entity.vehicle.several": "Ajoneuvolla on useita akkuantureita. Automaattinen tunnistus ei voi valita niiden välillä, joten varaustasoa ei lueta ennen kuin valitset yhden.",
  "entity.error.field.unknownVehicle": "Ajoneuvoa ei enää ole.",
  "issue.targetSocUnknown": "Lataustason tavoitetta ei voi suunnitella: ajoneuvon lataustaso tai akun kapasiteetti ei ole tiedossa. Avaa Suunnitelma valitaksesi anturin tai antaaksesi kapasiteetin.",
  "settings.plan.mode.legend": "Lataa perusteella",
  "settings.plan.mode.energy": "Energia (kWh)",
  "settings.plan.mode.soc": "Tavoite-SoC (%)",
  "settings.soc.target": "Lataustason tavoite",
  "settings.soc.slider": "Lataustason tavoite, prosenttia",
  "settings.soc.vehicle": "Ajoneuvo",
  "settings.soc.vehicleUnknown": "Ei valittu",
  "settings.soc.estimated": "arvioitu",
  "settings.soc.estimatedFrom": "arvioitu, luettu viimeksi {age}",
  "settings.soc.unknown": "Tuntematon",
  "settings.soc.need": "Tarvittava energia",
  "settings.soc.capacityMissing": "Akun kapasiteetti puuttuu — anna se Asetuksissa",
  "settings.capacity.label": "Akun kapasiteetti",
  "settings.capacity.unset": "Ei annettu",
  "settings.soc.needSensor": "Ajoneuvolle on valittava lataustasoanturi, ennen kuin tavoitetta voi suunnitella.",
  "settings.soc.age.now": "juuri nyt",
  "settings.soc.age.min": "{count} min sitten",
  "settings.soc.age.hour": "{count} h sitten",
  "settings.soc.age.day": "{count} pv sitten",
  "settings.overview.titleNamed": "Kortin asetukset · {name}",
  "settings.soc.factNow": "Nyt {value}",
  "settings.soc.factLimit": "Auton latausraja {value}",
  "settings.soc.noNeed": "Latausta ei tarvita nyt",
  "settings.soc.toLimit": "Ladataan auton rajaan, {value}",
  "settings.soc.readAge": "Luettu {age}",
  "settings.soc.needAfterVehicle": "Tarve lasketaan, kun ajoneuvon valinta on tallennettu.",
  "vehicleLine.aria": "{name}, {summary}. Valitse ladattava ajoneuvo",
  "vehicleLine.estimateTitle": "Arvio lukemien välillä, luettu {age}",
  "vehicleLine.dialogTitle": "Mikä ajoneuvo ladataan?",
  "vehicleLine.noReading": "Ei lukemaa",
  "settings.vehicle.unnamed": "Nimetön ajoneuvo",
  "settings.vehicle.plannedHere": "Tämä lataaja suunnittelee sille",
  "settings.vehicle.capacityReported": "auton ilmoittama",
  "settings.vehicle.socNone": "Anturia ei valittu",
  "settings.vehicle.charge": "Varaustaso",
  "settings.vehicle.error.capacity": "Akun kapasiteetin on oltava 1–500 kWh.",
  "settings.vehicle.error.consumption": "Kulutuksen on oltava 0,1–50 kWh/10 km.",
  "settings.phases.legend": "Laturin käyttämät vaiheet",
  "settings.phases.one": "1 vaihe",
  "settings.phases.three": "3 vaihetta",
  "settings.phases.unset": "Vaiheiden määrää ei ole asetettu.",
  "settings.phases.help": "Määrää tehon, jota suunnitelma olettaa tietyllä virralla. Kolmivaiheinen laturi voi silti ladata ajoneuvoa yhdellä vaiheella.",
  "cap.targetSocNote": "Vaatii ajoneuvon lataustasoanturin ja akun kapasiteetin. Aseta ne Suunnitelmassa ja Asetuksissa.",
  "control.startStop": "Käynnistys ja pysäytys",
  "control.startStop.easeeFixed": "SpotNav käynnistää ja pysäyttää laturin Easee-integraation palvelulla (tauko ja jatko), joten käynnistys- tai pysäytysentiteettiä ei valita.",
  "control.limit.minutes": "Virtaa voi muuttaa enintään {count} minuutin välein.",
  "control.limit.seconds": "Virtaa voi muuttaa enintään {count} sekunnin välein.",
  "control.limit.flash": "Virta tallentuu laturiin ja muuttuu vain latauksen alkaessa.",
  "control.limit.stopOnly": "Kuormanhallinta voi vain pysäyttää latauksen, ei pienentää virtaa.",
  "control.limit.installation": "Raja koskee koko asennusta.",
  "control.current": "Latausvirta",
  "control.current.none": "SpotNav ei aseta sitä (laturi pitää oman rajansa)",
  "control.current.ocpp": "OCPP ChangeConfiguration",
  "control.current.number": "Numeroentiteetti: {name}",
  "control.current.service": "Easeen dynaaminen virtaraja",
  "control.current.off": "SpotNav ei aseta virtaa. Ota se käyttöön laturin asetuksissa.",
  "control.conflict": "Laturin oma {label} on päällä ({name}). Se voi häiritä SpotNavia: sammuta se.",
  "control.disabled": "Laturin oma käyttöönottokytkin on pois päältä ({name}). SpotNav ei voi käynnistää latausta: kytke se päälle.",
  "control.otherController": "{name} ohjaa myös latauslaitteita; sammuta se tälle latauslaitteelle, muuten {name} ja SpotNav toimivat toisiaan vastaan."
};

// src/i18n/nb.ts
var nb = {
  "card.title": "SpotNav",
  "state.loading": "Leser ladeplanen…",
  "state.unconfigured": "Velg én SpotNav-lader i kortets redigeringsverktøy.",
  "state.addCharger": "Legg til en lader: Innstillinger → Enheter og tjenester → SpotNav → Legg til oppføring → Lader; den tilbyr å bli med i dette anlegget.",
  "state.noChargers": "Ingen SpotNav-lader er satt opp i denne Home Assistant ennå.",
  "state.requestFailed": "Forespørselen mislyktes. Sjekk forbindelsen til Home Assistant, og prøv igjen.",
  "state.unsupported": "Kortet og integrasjonen snakker ulike API-versjoner. Oppdater begge så de samsvarer.",
  "state.malformed": "Integrasjonen svarte med noe kortet ikke kan lese. Oppdater begge så de samsvarer.",
  "state.chargerMissing": "Laderen er ukjent, ikke lastet eller et anlegg. Ingenting ble valgt i stedet.",
  "state.retry": "Prøv igjen",
  "state.noPrices": "Det finnes ingen priser for denne laderen ennå.",
  "status.externalInstalled": "Eksternt skjema: neste lading kl. {time}.",
  "status.externalNoSchedule": "Ekstern planlegging er aktiv; ingen plan er installert.",
  "status.chargingNow": "Lader nå; planlagt til {time}.",
  "status.autoPlanned": "Planlagt fra {time}.",
  "status.autoInstalled": "Lading er planlagt fra {time}.",
  "status.heldUntilWindow": "Ladingen venter til planlagt start kl. {time}.",
  "status.proposalPending": "Et nytt ladeforslag er klart.",
  "status.proposalPendingAt": "En ny plan er klar og installeres når det pågående ladevinduet slutter kl. {time}.",
  "status.waitingForTomorrow": "Venter på morgendagens priser.",
  "status.waitingForPublication": "Venter på morgendagens priser (~{time}), planlegger da.",
  "status.waitingForHistory": "Venter: {weekday} har vært {percent} % billigere de siste {weeks} ukene.",
  "status.waitingForHistoryNoDetail": "Venter på timer som pleier å være billigere, planlegger da.",
  "status.waitingForPublicationNoTime": "Venter på morgendagens priser, planlegger da.",
  "status.buyingBeforePublication": "Kjøper {kwh} kWh nå, resten når prisene er publisert.",
  "status.noPlan": "Ingen ladeplan kunne beregnes akkurat nå.",
  "status.loadBalancingLimited": "Ladingen begrenses av anleggets lastbalansering akkurat nå.",
  "status.loadBalancingLimitedTo": "Ladingen begrenses til {limit} A av anleggets lastbalansering.",
  "status.loadBalancingLimitedByBattery": "Hjemmebatteriet lader fra nettet og deler hovedsikringen: bilen får {limit} A.",
  "status.loadBalancingLimitedByHouse": "Husets forbruk begrenser bilen til {limit} A.",
  "status.chargingNowOpen": "Lader nå.",
  "status.scheduledNoTime": "Lading er planlagt.",
  "status.nothingToCharge": "Ingenting å lade akkurat nå.",
  "status.pausedShort": "Automatisk lading er pauset.",
  "status.planEnergy": "{kwh} kWh",
  "status.planCost": "{cost}",
  "status.targetStopped": "Stoppet ved {soc} %",
  "status.targetStoppedAge": "Stoppet ved {soc} % (avlesningen er {age} gammel)",
  "status.targetStoppedNow": "Stoppet ved {soc} % (akkurat nå)",
  "status.targetStoppedEstimate": "Stoppet ved {soc} % (anslått)",
  "status.targetStoppedEstimateAge": "Stoppet ved {soc} % (anslått, avlesningen er {age} gammel)",
  "status.targetAgeMinutes": "{n} min",
  "status.targetAgeHours": "{n} t",
  "issue.targetUnverifiable": "Målet kan ikke kontrolleres akkurat nå.",
  "status.planDistance": "{distance}",
  "issue.priceDegraded": "Prisdataene er ufullstendige.",
  "issue.banner.blocking": "Noe må ordnes før lading kan planlegges.",
  "issue.banner.notice": "Greit å vite.",
  "issue.chargerMissing": "Den valgte laderen kan ikke brukes: den er ukjent, ikke lastet eller et anlegg.",
  "issue.chargeControlMissing": "Ladestyringen {entity} finnes ikke lenger. Den er sannsynligvis omdøpt eller fjernet: velg laderens styring på nytt i innstillingene.",
  "issue.chargeControlDisabled": "Ladestyringen {entity} er deaktivert i Home Assistant, så SpotNav kan ikke starte eller stoppe laderen. Aktiver den igjen.",
  "issue.unsupported": "Kortet og integrasjonen snakker ulike API-versjoner.",
  "issue.malformed": "Svaret fra backend var ikke et gyldig API v2-svar.",
  "status.finishSetupArea": "Fullfør oppsettet: velg et prisområde i Innstillinger.",
  "status.finishSetup": "Fullfør oppsettet i Innstillinger: {fields}.",
  "status.settingsSuggested": "Foreslått ut fra plassering og lader – sjekk Innstillinger.",
  "status.missing.area": "prisområde",
  "status.missing.phases": "faser",
  "status.missing.amps": "ladestrøm",
  "status.missing.vehicle": "kjøretøy",
  "status.missing.target_percent": "målnivå",
  "issue.incompleteSettings": "Fullfør oppsettet: noen innstillinger mangler fortsatt. Åpne Innstillinger.",
  "issue.missingSettings": "Disse innstillingene mangler: {fields}.",
  "issue.planningUnavailable": "Ingen ladeplan kunne beregnes med dataene som finnes akkurat nå.",
  "issue.solarUnavailable": "Sollading er ikke tilgjengelig: dette anlegget kan ikke måle soloverskudd.",
  "issue.priceHorizon": "Priser mangler for en del av planleggingsvinduet. Angi et avreisetidspunkt.",
  "issue.error": "Den siste beregningen mislyktes.",
  "issue.priceUnavailable": "Ingen brukbare prisdata finnes akkurat nå.",
  "issue.priceInvalid": "Prisdataene er ugyldige.",
  "issue.priceStale": "Prisene er utdaterte: den siste vellykkede hentingen brukes fortsatt.",
  "issue.unpriced": "En del av planen lader uten publiserte priser.",
  "issue.chargingWithoutPrices": "Lader uten publiserte priser for å holde sluttiden.",
  "issue.loadBalancing": "Lastbalansering er ikke tilgjengelig for denne laderen.",
  "issue.heldByCharger": "Laderens egen timeplan eller lastbalansering holder tilbake ladingen, så den har ikke startet.",
  "issue.chargerDisabled": "Laderens egen aktiveringsbryter er av, så den kan ikke starte. Slå den på i laderens innstillinger.",
  "issue.holdOverridden": "Ladingen ble startet utenfor planen og får fortsette.",
  "status.siteMeasurement.stale.other": "{phases} er eldre enn {seconds} s.",
  "status.siteMeasurement.stale.one": "{phases} er eldre enn {seconds} s.",
  "status.siteMeasurement.noValue.other": "{phases} har ingen verdi{where}.",
  "status.siteMeasurement.noValue.one": "{phases} har ingen verdi{where}.",
  "issue.duplicateCharger": "{other} og denne laderen er den samme fysiske laderen. To SpotNav-ladere på én lader sender motstridende kommandoer, så behold bare én: fjern den andre under Innstillinger → Enheter og tjenester → SpotNav. SpotNav fjerner aldri en for deg.",
  "issue.siteMeasurement": "Anleggets måling kan ikke brukes akkurat nå.",
  "issue.unknown": "Backend rapporterte noe kortet ikke kjenner igjen ennå.",
  "header.info": "Om kortet",
  "header.settings": "Kortinnstillinger",
  "header.history": "Ladehistorikk",
  "history.title": "Ladehistorikk",
  "history.titleNamed": "Ladehistorikk · {name}",
  "history.loading": "Henter ladehistorikken…",
  "history.failed": "Ladehistorikken kunne ikke leses.",
  "history.intro": "Hva hver lading kostet: spotprisen pluss energiavgift, nettleie og mva., da energien ble levert.",
  "history.thisMonth": "Denne måneden",
  "history.lastMonth": "Forrige måned",
  "history.noneInMonth": "Ingen ladinger.",
  "history.days": "Dager",
  "history.months": "Måneder",
  "history.listLabel": "Vis per",
  "history.sessions.one": "{count} lading",
  "history.sessions.other": "{count} ladinger",
  "history.solar": "{percent} sol",
  "history.estimated": "estimert energi",
  "history.noCost": "ingen pris",
  "history.savings.saved": "Estimert besparelse: {amount} mot dagens snittpris",
  "history.savings.extra": "Estimert {amount} mer enn dagens snittpris",
  "history.savings.note": "Besparelsen er et estimat: samme energi til hver dags snittpris.",
  "history.latest": "Siste ladinger",
  "history.empty": "Ingen ladinger er lagret ennå. De vises her etter neste lading.",
  "history.open": "Lader nå siden {time}: {energy}",
  "history.by.plan_window": "planlagt vindu",
  "history.by.manual": "startet for hånd",
  "history.by.solar": "solcelleoverskudd",
  "history.by.hybrid": "hybrid",
  "history.by.other": "startet andre steder",
  "history.export.period": "Periode",
  "history.range.thisMonth": "Denne måneden",
  "history.range.lastMonth": "Forrige måned",
  "history.range.last12": "Siste 12 måneder",
  "history.range.all": "Alt",
  "history.export": "Eksporter CSV",
  "history.exportFailed": "Eksporten mislyktes.",
  "settings.overview.title": "Kortinnstillinger",
  "settings.section.market": "Elområde og avgifter",
  "settings.section.vehicle": "Kjøretøy",
  "settings.section.capabilities": "Sensorer og funksjoner",
  "settings.section.site": "Anlegg",
  "settings.section.configure": "Endre",
  "settings.value.off": "Av",
  "settings.fiscal.vat": "MVA",
  "settings.fiscal.tax": "Elavgift",
  "settings.fiscal.transfer": "Nettleie",
  "settings.consumption.unit": "kWh/10 km",
  "settings.section.entities": "Lader",
  "entity.group.site": "Anleggets entiteter",
  "entity.notSet": "Ikke angitt",
  "entity.edit.charger": "Endre laderens entiteter",
  "entity.edit.site": "Endre anleggets entiteter",
  "entity.editor.charger": "Laderens entiteter",
  "entity.editor.site": "Anleggets entiteter",
  "entity.loading": "Leser entitetene…",
  "entity.adminOnly": "Bare administratorer kan endre entiteter.",
  "entity.error.read": "Entitetene kunne ikke leses.",
  "entity.field.chargeControl": "Bryter for ladestyring",
  "entity.field.currentLimit": "Strømgrense",
  "entity.field.energyRegister": "Energiteller",
  "entity.field.powerEntity": "Effektsensor (smartplugg)",
  "entity.field.vehicleSoc": "Kjøretøyets ladenivå",
  "entity.field.mainFuse": "Hovedsikring",
  "entity.field.safetyMargin": "Sikkerhetsmargin",
  "entity.field.measurementMode": "Målemetode",
  "entity.field.voltageBetweenPhases": "Spenning mellom faser",
  "entity.field.chargerPriority": "Laderens prioritet",
  "entity.field.batteryPower": "Batteriets effekt",
  "entity.field.maxAge": "Høyeste målealder",
  "entity.mode.direct": "Fasestrømmer måles direkte",
  "entity.mode.derived": "Fasestrømmer beregnes fra effekt og spenning",
  "entity.phase.direct": "Strøm, {phase}",
  "entity.derived.power": "Effekt",
  "entity.derived.reactivePower": "Reaktiv effekt",
  "entity.derived.voltage": "Spenning",
  "entity.error.field.required": "Velg en entitet.",
  "entity.error.field.notFound": "Entiteten finnes ikke.",
  "entity.error.field.wrongDomain": "Det er feil type entitet.",
  "entity.error.field.invalid": "Verdien godtas ikke.",
  "entity.error.field.notWritable": "Dette kan ikke endres her.",
  "entity.error.field.controlPathUnknown": "SpotNav kan ikke avgjøre hvordan en lading startes og stoppes med den enheten. Velg laderens bryter, eller en velger med tydelige start- og stoppvalg.",
  "entity.error.field.unknown": "Feltet gjenkjennes ikke.",
  "entity.error.field.chargeControlInUse": "En annen lader bruker allerede denne bryteren.",
  "entity.error.field.currentLimitInUse": "En annen lader bruker allerede denne strømgrensen.",
  "entity.error.field.duplicateCharger": "En annen SpotNav-lader er allerede den samme laderen.",
  "entity.error.conflict": "Endret et annet sted. De gjeldende verdiene vises, og ingenting ble lagret.",
  "entity.error.invalid": "Noen verdier ble ikke godtatt. Ingenting ble lagret.",
  "entity.error.generic": "Entitetene kunne ikke endres. Ingenting ble endret.",
  "entity.error.notAdmin": "Bare administratorer kan endre entiteter.",
  "entity.missing.optional": "Denne entiteten finnes ikke lenger i Home Assistant. Tøm feltet, eller velg en annen entitet.",
  "entity.missing.required": "Denne entiteten finnes ikke lenger i Home Assistant. Velg en annen entitet.",
  "entity.automatic": "Automatisk: {name}",
  "entity.help.chargeControl": "Bryteren som starter og stopper ladingen. SpotNav slår den av og på etter planen.",
  "entity.help.currentLimit": "Entiteten som viser laderens innstilte strøm. Automatisk bruker den SpotNav finner selv, for OCPP 0.12-ladere sesjonsgrensen.",
  "entity.help.energyRegister": "Laderens akkumulerte kWh-måler. SpotNav bruker den for å vite hva som allerede er ladet. Finnes automatisk for OCPP-ladere.",
  "entity.help.powerEntity": "For en lader bak en smartplugg: pluggens effektsensor, i W eller kW. SpotNav teller energien fra den og ser når bilen har sluttet å trekke strøm. Pluggen må være dimensjonert for laderens kontinuerlige strøm.",
  "entity.help.vehicleSoc": "Kjøretøyets ladenivå, lest fra sensoren som er valgt for kjøretøyet, eller funnet automatisk når det bare har én.",
  "entity.help.mainFuse": "Anleggets hovedsikring i ampere. Alle laderne på anlegget holder seg samlet under den.",
  "entity.help.safetyMargin": "Strømmen i ampere som SpotNav holder fri under hovedsikringen. Ladingen holdes under sikringen minus marginen, som derfor må være lavere enn sikringen.",
  "entity.help.measurementMode": "Om måleren oppgir strømmen for hver fase direkte, eller SpotNav regner den ut fra effekt og spenning.",
  "entity.help.voltageBetweenPhases": "Spenningen mellom to faser i det elektriske anlegget ditt. Trefaset ladeeffekt regnes ut fra den.",
  "entity.help.chargerPriority": "Når flere ladere deler anleggets sikring, får en lader satt til Først strøm før de andre, og en satt til Sist får det som er igjen.",
  "entity.voltage.tn": "400 V (TN-nett, det vanlige)",
  "entity.voltage.it": "230 V (IT-nett, vanlig i Norge)",
  "entity.priority.first": "Først",
  "entity.priority.normal": "Normal",
  "entity.priority.last": "Sist",
  "entity.help.batteryPower": "En sensor for hjemmebatteriets effekt, slik at SpotNav kan ta hensyn til batteriet.",
  "entity.help.maxAge": "Hvor gammel en måling kan være, i sekunder, før SpotNav slutter å stole på den.",
  "entity.help.phaseDirect": "Sensoren som måler strømmen på denne fasen, i ampere.",
  "entity.help.derivedPower": "Sensoren for aktiv effekt på denne fasen.",
  "entity.help.derivedReactivePower": "Sensoren for reaktiv effekt på denne fasen.",
  "entity.help.derivedVoltage": "Sensoren for spenning på denne fasen.",
  "entity.flag.on": "På",
  "entity.flag.off": "Av",
  "entity.field.siteCurrentSigned": "Nettstrømmen har fortegn",
  "entity.field.gridPowerInverted": "Sensoren viser eksport som positiv",
  "entity.field.gridPowerSource": "Nettets samlede effekt (for sol)",
  "entity.field.gridPowerSourceExport": "Samlet eksporteffekt (hvis separat)",
  "entity.field.batteryDischargePower": "Batteriets utladingseffekt",
  "entity.field.batteryPowerInverted": "Sensoren viser utlading som positiv",
  "entity.derived.powerExport": "Eksporteffekt",
  "entity.derived.apparentPower": "Tilsynelatende effekt",
  "entity.derived.current": "Strøm",
  "entity.help.siteCurrentSigned": "Slå på hvis måleren oppgir negativ strøm ved eksport. SpotNav bruker da strømmens størrelse, som er det som belaster sikringen, i stedet for å avvise den.",
  "entity.help.gridPowerInverted": "Slå på hvis målerens effekt er positiv ved eksport (Huawei, SolarEdge, GoodWe og lignende). SpotNav leser da import som positiv. Det gjelder også nettets samlede effekt.",
  "entity.help.gridPowerSource": "Trengs for sol- og hybridlading når fasene bare oppgir strøm.",
  "entity.help.gridPowerSourceExport": "Bare hvis måleren oppgir import og eksport som to sensorer: feltet over er da importen, og dette er eksporten.",
  "entity.help.batteryPowerInverted": "Slå på hvis batteriets effekt er positiv ved utlading (Tesla, Fronius, Enphase, GoodWe og lignende). SpotNav leser da lading som positiv.",
  "entity.help.batteryDischargePower": "Bare for et batteri som oppgir lading og utlading som to sensorer: dette er utladingen, og batterisensoren over er ladingen.",
  "entity.help.derivedPowerExport": "Bare når måleren oppgir import og eksport som to sensorer: effektsensoren over er da import og denne er eksport.",
  "entity.help.derivedApparentPower": "Tilsynelatende effekt på denne fasen, i VA. Med den blir strømmen nøyaktig.",
  "entity.help.derivedCurrent": "Målerens egen strøm på denne fasen. Med den blir strømmen nøyaktig.",
  "entity.choice.mixed": "Mer enn ett av disse er satt. Bare det valgte beholdes; de andre tømmes når du lagrer.",
  "entity.choice.choose": "Velg en entitet",
  "entity.limit.none": "Ingen (SpotNav stiller ikke inn strømmen)",
  "entity.grid.title": "Nettets effekt",
  "entity.grid.one": "Én sensor med retning",
  "entity.grid.two": "Import og eksport som to sensorer",
  "entity.current.title": "Strømmen hentes fra",
  "entity.current.measured": "Målerens egen strøm",
  "entity.current.apparent": "Tilsynelatende effekt",
  "entity.current.reactive": "Reaktiv effekt",
  "entity.current.estimated": "Anslått (effektfaktor 0,9)",
  "entity.current.estimatedNote": "SpotNav anslår strømmen ut fra effekten. Anslaget er markert i kortet.",
  "entity.battery.title": "Batteri",
  "entity.battery.none": "Ingen",
  "entity.battery.one": "Én sensor",
  "entity.battery.two": "Lading og utlading som to sensorer",
  "entity.energy.title": "Energi",
  "entity.energy.meter": "Energiteller (kWh)",
  "entity.energy.power": "Effekt (W) — SpotNav beregner energien",
  "entity.energy.none": "Ingen",
  "entity.notice.estimated": "Strømmen er anslått ut fra effekten, med effektfaktor {pf} eller bedre som forutsetning. Anslaget er aldri lavere enn den virkelige strømmen ved den effektfaktoren eller bedre, og undervurderer den under det. Legg til målerens strøm, tilsynelatende effekt eller reaktive effekt for en nøyaktig verdi.",
  "entity.notice.estimatedShort": "Anslått",
  "entity.warning.updateInterval": "{integration} oppdaterer hvert {seconds} s, tregere enn høyeste måleralder.",
  "entity.warning.updateIntervalOption": "Senk det med «{option}» i den integrasjonen.",
  "entity.warning.onChange": "{integration} rapporterer bare når en verdi endres, så en stabil verdi kan se gammel ut. Øk høyeste måleralder hvis anlegget ofte regnes som utdatert.",
  "entity.warning.ownBalancing": "{name} ({integration}) balanserer last selv og kan motvirke SpotNavs aktive styring. Bruk én av dem.",
  "entity.warning.externalBalancer": "{integration} balanserer strømmen for {name} selv, så SpotNav starter og stopper laderen, men skriver ikke strømmen.",
  "entity.warning.unknown": "Anlegget har en melding som denne versjonen ikke kan vise. Oppdater SpotNav.",
  "entity.detect.title": "Funnet i Home Assistant",
  "entity.detect.intro": "Disse ble gjenkjent i oppsettet ditt. Ingenting endres før du trykker på Bruk.",
  "entity.site.intro": "Måling for anlegget. {applies}",
  "entity.detect.meters": "Strømmålere",
  "entity.detect.batteries": "Hjemmebatterier",
  "entity.detect.use": "Bruk",
  "entity.detect.inUse": "I bruk",
  "entity.detect.direct": "Måler fasestrømmen direkte",
  "entity.detect.derived": "Utledet fra effekt og spenning",
  "entity.detect.signed": "Strøm med fortegn: leses som sin størrelse",
  "entity.detect.inverted": "Effekt med eksport som positiv: negeres",
  "entity.detect.gridPower": "Nettets samlede effekt funnet: brukes til sol- og hybridlading",
  "entity.detect.estimated": "Anslått strøm: ingen strøm, tilsynelatende eller reaktiv effekt funnet",
  "entity.detect.enable": "Aktiverer {count} entiteter integrasjonen leverer deaktivert.",
  "entity.detect.battery.inverted": "Utlading som positiv: negeres",
  "entity.detect.battery.pair": "Lading og utlading er to sensorer",
  "entity.detect.confidence.low": "Lav sikkerhet: kontroller at fortegnene stemmer.",
  "entity.detect.warning.own_load_balancing": "Denne enheten balanserer last selv.",
  "entity.detect.warning.sign_unverified": "Effektens fortegn er ukjent: kontroller at eksport er negativ.",
  "entity.detect.warning.voltage_from_other_device": "Måleren rapporterer ingen spenning, så fasespenningen fra en annen enhet, vanligvis vekselretteren, brukes. Dette er normalt.",
  "entity.detect.warning.may_measure_subcircuit": "Kontroller at måleren måler hele hovedtilførselen, ikke en delkrets.",
  "entity.detect.warning.reports_on_change_only": "Rapporterer bare når en verdi endres.",
  "market.edit": "Endre prisområde og avgifter",
  "settings.vehicle.change": "Endre kjøretøy",
  "settings.vehicle.none": "Ingen kjøretøy er satt opp ennå.",
  "settings.vehicle.dialogTitle": "Kjøretøy · {name}",
  "settings.vehicle.sensorLegend": "Sensor for ladenivå",
  "entity.foundAutomatically": "Finnes automatisk",
  "site.row.battery": "Batteri",
  "settings.value.none": "Ingen",
  "settings.section.solar": "Sol",
  "site.solar.change": "Endre solinnstillinger",
  "site.solar.dialogTitle": "Solinnstillinger",
  "strategy.setupSolar": "Sett opp sol under Innstillinger",
  "strategy.setupSite": "Åpne anleggets innstillinger",
  "site.none": "Denne laderen har ikke noe anlegg.",
  "site.applies.one": "Gjelder den ene laderen på dette anlegget.",
  "site.applies.other": "Gjelder alle {count} ladere på dette anlegget.",
  "site.solarPriority.title": "Solprioritet",
  "site.solarPriority.carFirst": "Bilen først",
  "site.solarPriority.batteryFirst": "Batteriet først",
  "site.solarForecast.title": "Kilder til solprognose",
  "site.solarForecast.none": "Ingen valgt. Uten en kilde planlegger hybrid en lading som Billigst.",
  "site.activeControl.title": "Aktiv lastbalansering",
  "site.activeControl.on": "På",
  "site.activeControl.off": "Av",
  "site.activeControl.available": "Tilgjengelig",
  "site.activeControl.reason.duplicateMembership": "Ikke tilgjengelig: denne laderen tilhører mer enn ett anlegg.",
  "site.activeControl.reason.noCommandableCharger": "Ikke tilgjengelig: ingen lader på anlegget tar imot en strømkommando.",
  "site.activeControl.reason.measurement": "Ikke tilgjengelig: anleggets egen effektmåling er ikke i orden.",
  "site.activeControl.note": "Best effort, ingen verneinnretning. Når den slås av, får laderen tilbake strømmen som var senket.",
  "site.activeControl.pending": "Lagrer…",
  "site.activeControl.error.unavailable": "Lastbalanseringen kan ikke slås på akkurat nå: anlegget kan ikke styres aktivt for øyeblikket. Ingenting ble endret.",
  "site.activeControl.error.confirmation": "Endringen kunne ikke bekreftes, så den forrige innstillingen ble beholdt.",
  "site.activeControl.restore.off": "Lastbalanseringen er av.",
  "site.activeControl.restore.notNeeded": "Ingen lader trengte å få strømmen tilbake.",
  "site.activeControl.restore.restoredOne": "Laderen ble tilbakestilt til {to} A.",
  "site.activeControl.restore.restoredNamed": "{name} ble tilbakestilt til {to} A.",
  "site.activeControl.restore.unnamed": "En lader",
  "site.activeControl.restore.line": "{name}: {text}",
  "site.activeControl.restore.failed.write_failed": "Laderen avviste kommandoen om å gjenopprette strømmen og kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.unconfirmed": "Laderen bekreftet ikke den gjenopprettede strømmen og kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.assigned_current_unreadable": "Laderens strømgrense kunne ikke leses, og laderen kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.no_authoritative_current": "SpotNav har ingen ønsket strøm å sette laderen tilbake til, og den kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.charger_not_loaded": "Laderen er ikke lastet i Home Assistant og kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.membership_conflict": "Laderen tilhører mer enn ett anlegg og ble ikke rørt, så den kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.probe_in_flight": "Laderen var opptatt med en strømtest og kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.below_minimum": "Strømmen som skulle gjenopprettes er lavere enn laderen godtar, og den kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.external_balancer": "En annen integrasjon balanserer laderens strøm, så SpotNav skrev den ikke, og den kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.no_connector_target": "SpotNav fant ikke laderens kontakt å styre, og laderen kan fortsatt være begrenset.",
  "site.activeControl.restore.failed.unknown": "Laderen kunne ikke gjenopprettes og kan fortsatt være begrenset.",
  "site.error.conflict": "Endret et annet sted. De gjeldende verdiene vises.",
  "settings.consumption.label": "Forbruk",
  "settings.value.unset": "Ikke angitt",
  "issue.count.one": "{count} punkt å se på",
  "issue.count.other": "{count} punkter å se på",
  "graph.title": "Ladeplan",
  "graph.description": "Prisgraf fra {from} til {to}, i {unit} per kWh. {count} intervaller over {days} dager. Valgt: {selected}.",
  "graph.descriptionNoSelection": "Ingenting valgt.",
  "graph.descriptionNow": "Gjeldende intervall: {now}.",
  "graph.legend.today": "I dag",
  "graph.legend.tomorrow": "I morgen",
  "graph.legend.past": "Tidligere dag",
  "graph.legend.current": "Gjeldende intervall",
  "graph.legend.selection": "Valgt intervall",
  "graph.legend.cheap": "Billigere enn dagens gjennomsnitt",
  "graph.legend.expensive": "Dyrere enn dagens gjennomsnitt",
  "graph.legend.installed": "Planlagt",
  "graph.legend.proposal": "Forslag",
  "graph.summary.max": "Maks",
  "graph.summary.min": "Min",
  "graph.summary.current": "Nå",
  "graph.readout": "{day} {time} · {price}",
  "graph.readoutMissing": "{day} {time} · ingen pris",
  "graph.readoutWithOffset": "{day} {time} ({offset}) · {price}",
  "graph.readoutMissingWithOffset": "{day} {time} ({offset}) · ingen pris",
  "graph.selected": "Valgt pris",
  "graph.priceAxis": "Pris, {unit} per kWh",
  "graph.noZone": "Tider er ikke tilgjengelige: integrasjonen har ikke rapportert en markedszone for denne laderen.",
  "graph.hint": "Bruk piltastene for å gå gjennom intervallene.",
  "graph.keyboardInstructions": "Prisgraf. Piltastene flytter mellom intervaller, Home og End hopper til endene, Escape tømmer valget.",
  "plan.proposal.one": "Billigste ladeperiode ({count})",
  "plan.proposal.other": "Billigste ladeperioder ({count})",
  "plan.installed.one": "Planlagt ladeperiode ({count})",
  "plan.installed.other": "Planlagte ladeperioder ({count})",
  "plan.period": "{day} {from}–{to}",
  "plan.periodWithOffset": "{day} {from}–{to} ({offset})",
  "plan.cost": "Kostnad",
  "plan.energy": "Energi",
  "plan.distance": "Distanse",
  "plan.power": "Effekt",
  "plan.activeNow": "Nå",
  "plan.missing": "ukjent",
  "cap.title": "Hva laderen kan",
  "cap.intro": "Hentet fra det integrasjonen rapporterer for denne laderen. Det som ikke er tilgjengelig her, kan kortet ikke slå på.",
  "cap.autoPrice": "Automatisk prisplanlegging",
  "cap.currentLimit": "Dynamisk strømgrense",
  "cap.loadBalancing": "Lastbalansering",
  "cap.targetSoc": "Bilens ladenivå og mål",
  "cap.available": "Tilgjengelig",
  "cap.unavailable": "Ikke tilgjengelig for denne laderen",
  "action.start": "Start nå",
  "action.stop": "Stopp",
  "action.resume": "Gjenoppta automatisk lading",
  "action.startHelp": "Starter en lading nå. Den lover ingen varighet: Auto kan ta over igjen ved neste avstemming.",
  "action.pauseAutomatic": "Pause automatisk lading",
  "action.pauseAutomaticShort": "Pause",
  "action.resumeShort": "Gjenoppta",
  "pause.sheetTitle": "Pause automatisk lading",
  "pause.intro": "Velg hvor lenge pausen varer. Bare de tidsstyrte valgene tar slutt av seg selv.",
  "pause.nextPeriod": "Til neste planlagte periode",
  "pause.untilTomorrow": "Til i morgen",
  "pause.untilResumed": "Til jeg gjenopptar",
  "strategy.title": "Strategi",
  "bar.charging": "Lading",
  "bar.schedule": "Tidsplan",
  "bar.start": "Start",
  "bar.waiting": "Starter…",
  "bar.stop": "Stopp",
  "bar.chargeNow": "Lad nå",
  "bar.chargingNow": "Lader",
  "bar.scheduleActive": "Tidsplan aktiv",
  "bar.schedulePaused": "Tidsplan pauset",
  "bar.state.notCharging": "lader ikke",
  "bar.state.charging": "lader",
  "bar.state.scheduleActive": "aktiv",
  "bar.state.schedulePaused": "pauset",
  "bar.change": "endre",
  "strategy.dialogTitle": "Ladestrategi",
  "strategy.intro": "Hva planen optimaliserer. En strategi som ikke er tilgjengelig her kan ikke velges ennå.",
  "strategy.cheapest": "Billigst",
  "strategy.solar": "Sol",
  "strategy.hybrid": "Hybrid",
  "strategy.reason.solar": "Krever måling av soloverskudd",
  "strategy.reason.hybrid": "Krever sol- og prisstyring",
  "strategy.reason.totalPower": "Sol krever målerens samlede effekt på nettet",
  "strategy.status.solar.charging": "Sol · lader med {amps} A fra overskudd",
  "strategy.status.solar.chargingUnknown": "Sol · lader fra overskudd",
  "strategy.status.solar.arming": "Sol · overskudd funnet, starter snart",
  "strategy.status.solar.disarming": "Sol · overskuddet avtar, stopper snart",
  "strategy.status.solar.noReadingStopped": "Sol · stoppet, ingen brukbar måling",
  "strategy.status.solar.noReadingWaiting": "Sol · ingen brukbar måling ennå",
  "strategy.status.solar.waitingForSun": "Sol · venter på sol",
  "strategy.status.solar.unknown": "Sol · status ukjent",
  "strategy.status.hybrid.grid": "Hybrid · {grid} kWh fra strømnettet{window}",
  "strategy.status.hybrid.creditSuffix": ", {credit} kWh forventes fra solen",
  "strategy.status.hybrid.noForecast": "Hybrid · ingen prognosekilde — planlegger som Billigst",
  "strategy.status.hybrid.noPriceData": "Hybrid · venter på prisdata",
  "strategy.status.hybrid.satisfied": "Hybrid · ladebehovet er allerede dekket",
  "strategy.status.hybrid.unknown": "Hybrid · planlegger",
  "advisory.vehicleNotRequestingCurrent": "Ladingen ble startet, men kjøretøyet ber ikke om strøm. Kontroller kjøretøyets ladeinnstillinger, eller koble til kabelen på nytt.",
  "advisory.powerBelowThreshold": "Ladingen ble startet, men laderen trekker nesten ingen effekt. Bilen kan være ferdig eller lader ikke: kontroller kjøretøyets ladeinnstillinger, eller koble til kabelen på nytt.",
  "control.noSettings": "Denne laderen har ingen innstillinger ennå, så det er ingenting å starte eller stoppe.",
  "control.pauseUnsettled": "En pause er lagret, men har ikke trådt i kraft ennå. Ingenting brukes.",
  "control.pauseClearFailed": "En pause har utløpt, men kunne ikke ryddes, så ingenting brukes.",
  "control.pauseStopFailed": "Laderen kunne ikke stoppes da pausen ble godtatt. Prøv Stopp igjen.",
  "control.startNotAcknowledged": "Laderen bekreftet ikke startkommandoen. Prøv igjen.",
  "control.reconcileFailed": "Den siste endringen ble lagret, men planen kunne ikke oppdateres.",
  "control.executionError": "Integrasjonen rapporterte et problem med den siste kjøringen.",
  "control.pausedUntil": "Pauset til {time}.",
  "control.pausedIndefinitely": "Pauset til du gjenopptar.",
  "control.actionPending": "En startkommando venter på at laderen skal bekrefte.",
  "action.error.unavailable": "Den handlingen er ikke tilgjengelig akkurat nå.",
  "action.error.failed": "Handlingen kunne ikke utføres. Ingenting ble endret.",
  "action.error.reconcileFailed": "Handlingen ble utført, men planen kunne ikke oppdateres.",
  "action.error.confirmationFailed": "Handlingen ble godtatt, men tilstanden kunne ikke bekreftes.",
  "action.error.invalidPause": "Det pausevalget er ikke tilgjengelig for denne laderen akkurat nå.",
  "action.error.charger": "Den konfigurerte laderen kunne ikke nås.",
  "action.error.version": "Kortet og integrasjonen snakker ulike handlingsversjoner.",
  "action.error.generic": "Handlingen mislyktes. Sjekk tilkoblingen til Home Assistant og prøv igjen.",
  "dialog.close": "Lukk",
  "dialog.issues": "Dette trenger oppfølging",
  "dialog.issuesIntro": "Alt integrasjonen rapporterte for denne laderen, i rapportert rekkefølge.",
  "context.area": "Område",
  "context.currency": "Valuta",
  "market.aria": "Endre prisområde og avgifter: {value}",
  "market.unset": "Ikke angitt",
  "market.title": "Område og avgifter",
  "market.intro": "Hvilket marked laderen kjøper strøm fra, og avgiftene som legges på prisene.",
  "market.loading": "Leser områdelisten…",
  "market.readOnly": "Bare administratorer kan endre område og avgifter. Du kan lese dem her.",
  "market.area.label": "Område",
  "market.area.description": "Området avgjør hvilke priser som brukes, og hvilke avgifter som foreslås.",
  "market.area.unlisted": "publiseres ikke lenger",
  "market.area.missing": "Ingen områder publiseres akkurat nå.",
  "market.vat.label": "Merverdiavgift",
  "market.vat.description": "Merverdiavgift i prosent.",
  "market.tax.label": "Energiavgift",
  "market.tax.description": "Energiavgift i områdets minste valutaenhet.",
  "market.transfer.label": "Nettavgift",
  "market.transfer.description": "Nettavgift i områdets minste valutaenhet.",
  "market.enabled.aria": "{component}: aktivert",
  "market.resetToSuggestion": "Tilbakestill til forslaget",
  "market.value.aria": "{component}: egen verdi",
  "market.suggestion.label": "Foreslått: {value} {unit}",
  "market.suggestion.none": "Ingen foreslått verdi er publisert for området.",
  "market.error.read": "Områdelisten kunne ikke leses.",
  "market.error.version": "Dette kortet og integrasjonen snakker ulike markedsversjoner.",
  "market.error.areaRequired": "Velg et område før du lagrer.",
  "market.error.areaUnknown": "Det området er ikke ett av de publiserte valgene.",
  "market.error.noSuggestion": "Området publiserer ingen foreslått verdi for det.",
  "market.state.loading": "Områdelisten leses fortsatt.",
  "market.state.staleOffline": "Dette er den siste områdelisten Home Assistant mottok; reléet har ikke vært nåbart siden.",
  "market.state.staleInvalid": "Dette er den siste områdelisten Home Assistant mottok; reléets nyere liste kunne ikke leses.",
  "market.state.offline": "Reléet kunne ikke nås, så ingen områder listes akkurat nå.",
  "market.state.invalid": "Reléets områdeliste kunne ikke leses, så ingen områder listes akkurat nå.",
  "settings.energy.aria": "Energi: {value}",
  "settings.deadline.aria": "Frist: {value}",
  "settings.current.aria": "Strøm: {value}",
  "settings.energy.unset": "Ikke angitt",
  "settings.deadline.none": "Ingen frist",
  "settings.current.unset": "Ikke angitt",
  "settings.energy.title": "Ladeenergi",
  "settings.energy.intro": "Hvor mye energi ladingen skal gi. Planen gjør det om til perioder.",
  "settings.energy.label": "Ønsket energi",
  "settings.energy.targetSoc": "Denne laderen planlegger fra et mål for ladenivå, så den manuelle energien kan ikke endres her.",
  "settings.deadline.title": "Frist og ladeperioder",
  "settings.deadline.intro": "Når ladingen må være ferdig, og hvor mange perioder den kan bruke.",
  "settings.deadline.enabled": "Ferdig innen en frist",
  "settings.deadline.time": "Avreisetid",
  "settings.deadline.date": "Avreise",
  "settings.deadline.dateDaily": "Hver dag",
  "settings.deadline.dateOn": "En bestemt dato",
  "settings.deadline.dateHelp": "Planen kan vente på timer som pleier å være billigere. Avreisen holdes alltid.",
  "settings.deadline.datePast": "Datoen er passert, så planen kjører hver dag til du velger en ny dato. Lagring fjerner den.",
  "settings.deadline.weekdays": "Ukedager",
  "settings.deadline.weekdaysHelp": "På en dag du utelater er det ingen avreise: planen går til neste dag du har valgt.",
  "settings.deadline.today": "i dag",
  "settings.deadline.tomorrow": "i morgen",
  "settings.deadline.periods": "Høyeste antall ladeperioder",
  "settings.current.title": "Planlagt strøm",
  "settings.current.intro": "Strømmen planen kan be om. Det er en planverdi, ikke en kommando til laderen.",
  "settings.current.label": "Planlagt strøm",
  "settings.loading": "Leser de gjeldende innstillingene…",
  "settings.section.support": "Støtte",
  "debug.intro": "Lagrer én fil med versjoner, status og de siste loggradene til en feilrapport. Hemmeligheter og din nøyaktige posisjon utelates.",
  "debug.download": "Last ned feilsøkingsinfo",
  "debug.preparing": "Forbereder…",
  "debug.error.notAdmin": "Bare administratorer kan laste ned feilsøkingsinfo.",
  "debug.error.failed": "Feilsøkingsinfoen kunne ikke hentes.",
  "settings.readOnly": "Bare administratorer kan endre innstillinger. Du kan lese dem her.",
  "settings.save": "Lagre",
  "settings.cancel": "Avbryt",
  "settings.reload": "Bruk verdiene fra serveren",
  "settings.reapply": "Bruk endringen min igjen",
  "settings.conflict.title": "Endret et annet sted",
  "settings.conflict.intro": "Innstillingene ble endret etter at denne dialogen ble åpnet. Ingenting ble lagret, og verdiene dine er fortsatt her.",
  "settings.error.required": "Fyll inn dette før du lagrer.",
  "settings.error.invalidNumber": "Det er ikke et tall.",
  "settings.error.outOfRange": "Verdien er utenfor det tillatte området.",
  "settings.error.invalidDate": "Velg en gyldig dato.",
  "settings.error.dateRange": "Velg en dato fra i dag og opptil 7 dager frem.",
  "settings.error.weekdays": "Velg minst én ukedag.",
  "settings.error.invalidTime": "Bruk et klokkeslett som 06:30.",
  "settings.error.read": "Innstillingene kunne ikke leses.",
  "settings.error.refused": "Innstillingene ble avvist. Ingenting ble endret.",
  "settings.error.notCommitted": "Innstillingene ble ikke lagret. Ingenting ble endret.",
  "settings.error.reconcileFailed": "Innstillingene ble lagret, men planen kunne ikke oppdateres.",
  "settings.error.confirmationFailed": "Innstillingene ble lagret, men tilstanden kunne ikke bekreftes.",
  "settings.error.version": "Kortet og integrasjonen snakker ulike innstillingsversjoner.",
  "settings.error.unavailable": "Innstillinger er ikke tilgjengelige akkurat nå.",
  "settings.error.charger": "Den konfigurerte laderen kunne ikke nås.",
  "settings.error.generic": "Innstillingene kunne ikke endres. Ingenting ble endret.",
  "settings.error.invalid": "Innstillingene ble avvist som ugyldige. Ingenting ble endret.",
  "settings.error.readOnly": "Bare administratorer kan endre innstillinger.",
  "settings.energy.slider": "Energiskyver, 0,5 til 100 kWh i halvkWh-trinn",
  "settings.current.slider": "Strømglidebryter, {min} til {max} A i hele ampere",
  "settings.sliderOutOfRange": "Den eksakte verdien er utenfor skyverens område. Bruk tallfeltet.",
  "settings.current.power": "Nominell effekt ≈ {power} kW",
  "settings.current.powerUnknown": "Nominell effekt ukjent",
  "bar.plan": "Plan",
  "settings.plan.title": "Ladeplan",
  "settings.plan.intro": "Hvor mye som skal lades, når det skal være ferdig og hvilken strøm planen kan be om. Dette er planleggingsverdier, ikke kommandoer til laderen.",
  "settings.plan.aria": "Plan: {value}",
  "entity.vehicle.automatic": "Automatisk gjenkjenning",
  "entity.vehicle.several": "Kjøretøyet har flere batterisensorer. Automatisk gjenkjenning kan ikke velge mellom dem, så ladenivået leses ikke før du velger en.",
  "entity.error.field.unknownVehicle": "Kjøretøyet finnes ikke lenger.",
  "issue.targetSocUnknown": "Ladenivåmålet kan ikke planlegges: bilens ladenivå eller batterikapasitet er ukjent. Åpne Plan for å velge sensor eller oppgi kapasiteten.",
  "settings.plan.mode.legend": "Lad etter",
  "settings.plan.mode.energy": "Energi (kWh)",
  "settings.plan.mode.soc": "Mål-SoC (%)",
  "settings.soc.target": "Mål for ladenivå",
  "settings.soc.slider": "Mål for ladenivå, prosent",
  "settings.soc.vehicle": "Kjøretøy",
  "settings.soc.vehicleUnknown": "Ikke valgt",
  "settings.soc.estimated": "estimert",
  "settings.soc.estimatedFrom": "estimert, sist avlest {age}",
  "settings.soc.unknown": "Ukjent",
  "settings.soc.need": "Energi som trengs",
  "settings.soc.capacityMissing": "Batterikapasitet mangler — angi den i Innstillinger",
  "settings.capacity.label": "Batterikapasitet",
  "settings.capacity.unset": "Ikke angitt",
  "settings.soc.needSensor": "En sensor for ladenivå må velges for bilen før et mål kan planlegges.",
  "settings.soc.age.now": "nettopp",
  "settings.soc.age.min": "for {count} min siden",
  "settings.soc.age.hour": "for {count} t siden",
  "settings.soc.age.day": "for {count} d siden",
  "settings.overview.titleNamed": "Kortinnstillinger · {name}",
  "settings.soc.factNow": "Nå {value}",
  "settings.soc.factLimit": "Bilens laddegrense {value}",
  "settings.soc.noNeed": "Ingen lading trengs nå",
  "settings.soc.toLimit": "Lader til bilens grense, {value}",
  "settings.soc.readAge": "Avlest {age}",
  "settings.soc.needAfterVehicle": "Behovet beregnes når kjøretøyvalget er lagret.",
  "vehicleLine.aria": "{name}, {summary}. Velg hvilket kjøretøy som skal lades",
  "vehicleLine.estimateTitle": "Anslått mellom avlesningene, avlest {age}",
  "vehicleLine.dialogTitle": "Hvilket kjøretøy skal lades?",
  "vehicleLine.noReading": "Ingen avlesning",
  "settings.vehicle.unnamed": "Kjøretøy uten navn",
  "settings.vehicle.plannedHere": "Denne laderen planlegger for det",
  "settings.vehicle.capacityReported": "rapportert av bilen",
  "settings.vehicle.socNone": "Ingen sensor valgt",
  "settings.vehicle.charge": "Ladenivå",
  "settings.vehicle.error.capacity": "Batterikapasiteten må være mellom 1 og 500 kWh.",
  "settings.vehicle.error.consumption": "Forbruket må være mellom 0,1 og 50 kWh/10 km.",
  "settings.phases.legend": "Faser laderen bruker",
  "settings.phases.one": "1 fase",
  "settings.phases.three": "3 faser",
  "settings.phases.unset": "Antall faser er ikke satt.",
  "settings.phases.help": "Styrer effekten planen regner med for en gitt strøm. En trefaselader kan likevel lade en bil på én fase.",
  "cap.targetSocNote": "Krever en sensor for bilens ladenivå og batteriets kapasitet. Angi dem i Plan og Innstillinger.",
  "control.startStop": "Start og stopp",
  "control.startStop.easeeFixed": "SpotNav starter og stopper laderen via Easee-integrasjonens tjeneste (pause og gjenoppta), så det finnes ingen start- eller stoppenhet å velge.",
  "control.limit.minutes": "Strømmen kan endres høyst hvert {count}. minutt.",
  "control.limit.seconds": "Strømmen kan endres høyst hvert {count}. sekund.",
  "control.limit.flash": "Strømmen lagres i laderen og endres bare når en lading starter.",
  "control.limit.stopOnly": "Lastbalansering kan bare stoppe ladingen, ikke senke strømmen.",
  "control.limit.installation": "Grensen gjelder hele installasjonen.",
  "control.current": "Ladestrøm",
  "control.current.none": "Settes ikke av SpotNav (laderen beholder sin egen grense)",
  "control.current.ocpp": "OCPP ChangeConfiguration",
  "control.current.number": "En tallentitet: {name}",
  "control.current.service": "Easees dynamiske strømgrense",
  "control.current.off": "SpotNav setter ikke strømmen. Slå det på i ladernes alternativer.",
  "control.conflict": "Laderens egen {label} er på ({name}). Den kan motarbeide SpotNav: slå den av.",
  "control.disabled": "Laderens egen aktiveringsbryter er av ({name}). SpotNav kan ikke starte den: slå den på.",
  "control.otherController": "{name} styrer også ladere; slå den av for denne laderen, ellers motarbeider {name} og SpotNav hverandre."
};

// src/i18n/sv.ts
var sv = {
  "card.title": "SpotNav",
  "state.loading": "Läser laddplanen…",
  "state.unconfigured": "Välj en SpotNav-laddare i kortets redigerare.",
  "state.addCharger": "Lägg till en laddare: Inställningar → Enheter och tjänster → SpotNav → Lägg till post → Laddare; den erbjuder sig att ansluta till den här anläggningen.",
  "state.noChargers": "Ingen SpotNav-laddare är inrättad i den här Home Assistant ännu.",
  "state.requestFailed": "Begäran misslyckades. Kontrollera anslutningen till Home Assistant och försök igen.",
  "state.unsupported": "Kortet och integrationen talar olika API-versioner. Uppdatera båda så att de matchar.",
  "state.malformed": "Integrationen svarade med något som kortet inte kan läsa. Uppdatera båda så att de matchar.",
  "state.chargerMissing": "Laddaren är okänd, inte laddad eller en anläggning. Ingenting valdes i dess ställe.",
  "state.retry": "Försök igen",
  "state.noPrices": "Det finns inga priser för den här laddaren ännu.",
  "status.externalInstalled": "Externt schema: nästa laddning kl. {time}.",
  "status.externalNoSchedule": "Extern planering är aktiv; inget schema är installerat.",
  "status.chargingNow": "Laddar nu; schemalagd till {time}.",
  "status.autoPlanned": "Planerat från {time}.",
  "status.autoInstalled": "Laddning är schemalagd från {time}.",
  "status.heldUntilWindow": "Laddningen väntar till planerad start kl. {time}.",
  "status.proposalPending": "Ett nytt laddförslag är klart.",
  "status.proposalPendingAt": "En ny plan väntar och installeras när pågående laddfönster slutar kl. {time}.",
  "status.waitingForTomorrow": "Väntar på morgondagens priser.",
  "status.waitingForPublication": "Väntar på morgondagens priser (~{time}), planerar då.",
  "status.waitingForHistory": "Väntar: {weekday} har varit {percent} % billigare de senaste {weeks} veckorna.",
  "status.waitingForHistoryNoDetail": "Väntar på timmar som brukar vara billigare, planerar då.",
  "status.waitingForPublicationNoTime": "Väntar på morgondagens priser, planerar då.",
  "status.buyingBeforePublication": "Köper {kwh} kWh nu, resten när priserna publiceras.",
  "status.noPlan": "Ingen laddplan kunde beräknas just nu.",
  "status.loadBalancingLimited": "Laddningen begränsas av anläggningens lastbalansering just nu.",
  "status.loadBalancingLimitedTo": "Laddningen begränsas till {limit} A av anläggningens lastbalansering.",
  "status.loadBalancingLimitedByBattery": "Hemmabatteriet laddar från nätet och delar huvudsäkringen: bilen får {limit} A.",
  "status.loadBalancingLimitedByHouse": "Hushållets förbrukning begränsar bilen till {limit} A.",
  "status.chargingNowOpen": "Laddar nu.",
  "status.scheduledNoTime": "Laddning är schemalagd.",
  "status.nothingToCharge": "Inget att ladda just nu.",
  "status.pausedShort": "Automatisk laddning är pausad.",
  "status.planEnergy": "{kwh} kWh",
  "status.planCost": "{cost}",
  "status.targetStopped": "Stoppad vid {soc} %",
  "status.targetStoppedAge": "Stoppad vid {soc} % (avläsningen {age} gammal)",
  "status.targetStoppedNow": "Stoppad vid {soc} % (nyss)",
  "status.targetStoppedEstimate": "Stoppad vid {soc} % (uppskattat)",
  "status.targetStoppedEstimateAge": "Stoppad vid {soc} % (uppskattat, avläsningen {age} gammal)",
  "status.targetAgeMinutes": "{n} min",
  "status.targetAgeHours": "{n} h",
  "issue.targetUnverifiable": "Målet kan inte kontrolleras just nu.",
  "status.planDistance": "{distance}",
  "issue.priceDegraded": "Prisunderlaget är ofullständigt.",
  "issue.banner.blocking": "Något behöver åtgärdas innan laddning kan planeras.",
  "issue.banner.notice": "Bra att veta.",
  "issue.chargerMissing": "Den valda laddaren går inte att använda: den är okänd, inte laddad eller en anläggning.",
  "issue.chargeControlMissing": "Laddstyrningen {entity} finns inte längre. Den har troligen bytt namn eller tagits bort: välj laddarens styrning igen i dess inställningar.",
  "issue.chargeControlDisabled": "Laddstyrningen {entity} är avstängd i Home Assistant, så SpotNav kan inte starta eller stoppa laddaren. Aktivera den igen.",
  "issue.unsupported": "Kortet och integrationen talar olika API-versioner.",
  "issue.malformed": "Svaret från backend var inte ett giltigt API v2-svar.",
  "status.finishSetupArea": "Slutför inställningen: välj ett prisområde i Inställningar.",
  "status.finishSetup": "Slutför inställningen i Inställningar: {fields}.",
  "status.settingsSuggested": "Förslag utifrån din plats och laddare – kontrollera Inställningar.",
  "status.missing.area": "prisområde",
  "status.missing.phases": "faser",
  "status.missing.amps": "laddström",
  "status.missing.vehicle": "fordon",
  "status.missing.target_percent": "målnivå",
  "issue.incompleteSettings": "Slutför inställningen: några inställningar saknas fortfarande. Öppna Inställningar.",
  "issue.missingSettings": "De här inställningarna saknas: {fields}.",
  "issue.planningUnavailable": "Ingen laddplan kunde beräknas med de uppgifter som finns just nu.",
  "issue.solarUnavailable": "Solladdning är inte tillgänglig: anläggningen kan inte mäta solöverskott.",
  "issue.priceHorizon": "Priser saknas för en del av planeringsperioden. Ange en avresetid.",
  "issue.error": "Den senaste beräkningen misslyckades.",
  "issue.priceUnavailable": "Inga användbara prisuppgifter finns just nu.",
  "issue.priceInvalid": "Prisuppgifterna är ogiltiga.",
  "issue.priceStale": "Priserna är inaktuella: den senaste lyckade hämtningen används fortfarande.",
  "issue.unpriced": "En del av planen laddar utan publicerade priser.",
  "issue.chargingWithoutPrices": "Laddar utan publicerade priser för att hålla sluttiden.",
  "issue.loadBalancing": "Lastbalansering är inte tillgänglig för den här laddaren.",
  "issue.heldByCharger": "Laddarens eget schema eller lastbalansering håller tillbaka laddningen, så den har inte startat.",
  "issue.chargerDisabled": "Laddarens egen aktiveringsbrytare är av, så den kan inte starta. Slå på den i laddarens inställningar.",
  "issue.holdOverridden": "Laddningen startades utanför planen och får fortsätta.",
  "status.siteMeasurement.stale.other": "{phases} är äldre än {seconds} s.",
  "status.siteMeasurement.stale.one": "{phases} är äldre än {seconds} s.",
  "status.siteMeasurement.noValue.other": "{phases} saknar värde{where}.",
  "status.siteMeasurement.noValue.one": "{phases} saknar värde{where}.",
  "issue.duplicateCharger": "{other} och den här laddaren är samma fysiska laddare. Två SpotNav-laddare på en laddare skickar motstridiga kommandon, så behåll bara en: ta bort den andra under Inställningar → Enheter och tjänster → SpotNav. SpotNav tar aldrig bort en åt dig.",
  "issue.siteMeasurement": "Anläggningens mätning kan inte användas just nu.",
  "issue.unknown": "Backend rapporterade något som kortet inte känner igen ännu.",
  "header.info": "Om kortet",
  "header.settings": "Kortinställningar",
  "header.history": "Laddhistorik",
  "history.title": "Laddhistorik",
  "history.titleNamed": "Laddhistorik · {name}",
  "history.loading": "Hämtar laddhistoriken…",
  "history.failed": "Laddhistoriken kunde inte läsas.",
  "history.intro": "Vad varje laddning kostade: spotpriset plus din energiskatt, nätavgift och moms, när energin levererades.",
  "history.thisMonth": "Denna månad",
  "history.lastMonth": "Förra månaden",
  "history.noneInMonth": "Inga laddningar.",
  "history.days": "Dagar",
  "history.months": "Månader",
  "history.listLabel": "Visa per",
  "history.sessions.one": "{count} laddning",
  "history.sessions.other": "{count} laddningar",
  "history.solar": "{percent} sol",
  "history.estimated": "uppskattad energi",
  "history.noCost": "inget pris",
  "history.savings.saved": "Uppskattad besparing: {amount} mot dagens snittpris",
  "history.savings.extra": "Uppskattat {amount} mer än dagens snittpris",
  "history.savings.note": "Besparingen är en uppskattning: samma energi till varje dags snittpris.",
  "history.latest": "Senaste laddningarna",
  "history.empty": "Inga laddningar har sparats än. De visas här efter nästa laddning.",
  "history.open": "Laddar nu sedan {time}: {energy}",
  "history.by.plan_window": "planerat fönster",
  "history.by.manual": "startad för hand",
  "history.by.solar": "solöverskott",
  "history.by.hybrid": "hybrid",
  "history.by.other": "startad på annat håll",
  "history.export.period": "Period",
  "history.range.thisMonth": "Denna månad",
  "history.range.lastMonth": "Förra månaden",
  "history.range.last12": "Senaste 12 månaderna",
  "history.range.all": "Allt",
  "history.export": "Exportera CSV",
  "history.exportFailed": "Exporten misslyckades.",
  "settings.overview.title": "Kortinställningar",
  "settings.section.market": "Elområde och skatter",
  "settings.section.vehicle": "Fordon",
  "settings.section.capabilities": "Sensorer och funktioner",
  "settings.section.site": "Anläggning",
  "settings.section.configure": "Ändra",
  "settings.value.off": "Av",
  "settings.fiscal.vat": "Moms",
  "settings.fiscal.tax": "Energiskatt",
  "settings.fiscal.transfer": "Nätavgift",
  "settings.consumption.unit": "kWh/10 km",
  "settings.section.entities": "Laddare",
  "entity.group.site": "Anläggningens entiteter",
  "entity.notSet": "Inte angiven",
  "entity.edit.charger": "Ändra laddarens entiteter",
  "entity.edit.site": "Ändra anläggningens entiteter",
  "entity.editor.charger": "Laddarens entiteter",
  "entity.editor.site": "Anläggningens entiteter",
  "entity.loading": "Läser entiteterna…",
  "entity.adminOnly": "Bara administratörer kan ändra entiteter.",
  "entity.error.read": "Entiteterna kunde inte läsas.",
  "entity.field.chargeControl": "Laddstyrning",
  "entity.field.currentLimit": "Strömgräns",
  "entity.field.energyRegister": "Energiräknare",
  "entity.field.powerEntity": "Effektsensor (smart plugg)",
  "entity.field.vehicleSoc": "Fordonets laddnivå",
  "entity.field.mainFuse": "Huvudsäkring",
  "entity.field.safetyMargin": "Säkerhetsmarginal",
  "entity.field.measurementMode": "Mätsätt",
  "entity.field.voltageBetweenPhases": "Spänning mellan faser",
  "entity.field.chargerPriority": "Laddarens prioritet",
  "entity.field.batteryPower": "Batteriets effekt",
  "entity.field.maxAge": "Högsta mätvärdesålder",
  "entity.mode.direct": "Fasströmmar mäts direkt",
  "entity.mode.derived": "Fasströmmar räknas fram ur effekt och spänning",
  "entity.phase.direct": "Ström, {phase}",
  "entity.derived.power": "Effekt",
  "entity.derived.reactivePower": "Reaktiv effekt",
  "entity.derived.voltage": "Spänning",
  "entity.error.field.required": "Välj en entitet.",
  "entity.error.field.notFound": "Entiteten finns inte.",
  "entity.error.field.wrongDomain": "Det är fel typ av entitet.",
  "entity.error.field.invalid": "Värdet godtas inte.",
  "entity.error.field.notWritable": "Det här kan inte ändras här.",
  "entity.error.field.controlPathUnknown": "SpotNav kan inte avgöra hur en laddning startas och stoppas med den entiteten. Välj laddarens strömbrytare, eller en väljare med tydliga start- och stoppalternativ.",
  "entity.error.field.unknown": "Fältet känns inte igen.",
  "entity.error.field.chargeControlInUse": "En annan laddare använder redan den här brytaren.",
  "entity.error.field.currentLimitInUse": "En annan laddare använder redan den här strömgränsen.",
  "entity.error.field.duplicateCharger": "En annan SpotNav-laddare är redan samma laddare.",
  "entity.error.conflict": "Ändrat någon annanstans. De aktuella värdena visas och inget sparades.",
  "entity.error.invalid": "Några värden godtogs inte. Inget sparades.",
  "entity.error.generic": "Entiteterna kunde inte ändras. Inget ändrades.",
  "entity.error.notAdmin": "Bara administratörer kan ändra entiteter.",
  "entity.missing.optional": "Den här entiteten finns inte längre i Home Assistant. Rensa fältet eller välj en annan entitet.",
  "entity.missing.required": "Den här entiteten finns inte längre i Home Assistant. Välj en annan entitet.",
  "entity.automatic": "Automatiskt: {name}",
  "entity.help.chargeControl": "Entiteten som startar och stoppar laddningen: en strömbrytare, en väljare eller en knapp. SpotNav använder den för att följa planen.",
  "entity.help.currentLimit": "Entiteten som visar laddarens inställda ström. Automatiskt använder den som SpotNav hittar själv, för OCPP 0.12-laddare sessionsgränsen.",
  "entity.help.energyRegister": "Laddarens ackumulerade kWh-mätare. SpotNav använder den för att veta vad som redan laddats. Hittas automatiskt för OCPP-laddare.",
  "entity.help.powerEntity": "För en laddare bakom en smart plugg: pluggens effektsensor, i W eller kW. SpotNav räknar energin från den och ser när bilen slutat ta ström. Pluggen måste vara dimensionerad för laddarens kontinuerliga ström.",
  "entity.help.vehicleSoc": "Fordonets laddnivå, läst från sensorn som valts för fordonet, eller hittad automatiskt när det bara har en.",
  "entity.help.mainFuse": "Anläggningens huvudsäkring i ampere. Alla laddare på anläggningen håller sig tillsammans under den.",
  "entity.help.safetyMargin": "Strömmen i ampere som SpotNav håller fri under huvudsäkringen. Laddningen hålls under säkringen minus marginalen, som därför måste vara lägre än säkringen.",
  "entity.help.measurementMode": "Om din mätare anger varje fas ström direkt, eller om SpotNav räknar ut den från effekt och spänning.",
  "entity.help.voltageBetweenPhases": "Spänningen mellan två faser i din elanläggning. Trefasig laddeffekt räknas ut från den.",
  "entity.help.chargerPriority": "När flera laddare delar anläggningens säkring får en laddare med Först tilldelad ström före de andra, och en med Sist får det som blir över.",
  "entity.voltage.tn": "400 V (TN-nät, det vanliga)",
  "entity.voltage.it": "230 V (IT-nät, vanligt i Norge)",
  "entity.priority.first": "Först",
  "entity.priority.normal": "Normal",
  "entity.priority.last": "Sist",
  "entity.help.batteryPower": "En sensor för hemmabatteriets effekt, så att SpotNav kan ta hänsyn till batteriet.",
  "entity.help.maxAge": "Hur gammal en mätning får vara, i sekunder, innan SpotNav slutar lita på den.",
  "entity.help.phaseDirect": "Sensorn som mäter strömmen på den här fasen, i ampere.",
  "entity.help.derivedPower": "Sensorn för aktiv effekt på den här fasen.",
  "entity.help.derivedReactivePower": "Sensorn för reaktiv effekt på den här fasen.",
  "entity.help.derivedVoltage": "Sensorn för spänning på den här fasen.",
  "entity.flag.on": "På",
  "entity.flag.off": "Av",
  "entity.field.siteCurrentSigned": "Nätströmmen är teckenmärkt",
  "entity.field.gridPowerInverted": "Sensorn visar export som positiv",
  "entity.field.gridPowerSource": "Nätets totala effekt (för sol)",
  "entity.field.gridPowerSourceExport": "Total exporteffekt (om separat)",
  "entity.field.batteryDischargePower": "Batteriets urladdningseffekt",
  "entity.field.batteryPowerInverted": "Sensorn visar urladdning som positiv",
  "entity.derived.powerExport": "Exporteffekt",
  "entity.derived.apparentPower": "Skenbar effekt",
  "entity.derived.current": "Ström",
  "entity.help.siteCurrentSigned": "Slå på om mätaren anger negativ ström vid export. SpotNav använder då strömmens storlek, som är det som belastar säkringen, i stället för att avvisa den.",
  "entity.help.gridPowerInverted": "Slå på om mätarens effekt är positiv vid export (Huawei, SolarEdge, GoodWe och liknande). SpotNav läser då import som positiv. Det gäller även nätets totala effekt.",
  "entity.help.gridPowerSource": "Behövs för sol- och hybridladdning när faserna bara anger ström.",
  "entity.help.gridPowerSourceExport": "Bara om mätaren anger import och export som två sensorer: fältet ovan är då importen och det här är exporten.",
  "entity.help.batteryPowerInverted": "Slå på om batteriets effekt är positiv vid urladdning (Tesla, Fronius, Enphase, GoodWe och liknande). SpotNav läser då laddning som positiv.",
  "entity.help.batteryDischargePower": "Bara för ett batteri som anger laddning och urladdning som två sensorer: det här är urladdningen, och batterisensorn ovan är laddningen.",
  "entity.help.derivedPowerExport": "Bara när mätaren anger import och export som två sensorer: effektsensorn ovan är då import och den här är export.",
  "entity.help.derivedApparentPower": "Skenbar effekt på den här fasen, i VA. Med den blir strömmen exakt.",
  "entity.help.derivedCurrent": "Mätarens egen ström på den här fasen. Med den blir strömmen exakt.",
  "entity.choice.mixed": "Fler än ett av dessa är angivet. Bara det valda behålls; de andra rensas när du sparar.",
  "entity.choice.choose": "Välj en entitet",
  "entity.limit.none": "Ingen (SpotNav ställer inte in strömmen)",
  "entity.grid.title": "Nätets effekt",
  "entity.grid.one": "En sensor med riktning",
  "entity.grid.two": "Import och export som två sensorer",
  "entity.current.title": "Strömmen hämtas från",
  "entity.current.measured": "Mätarens egen ström",
  "entity.current.apparent": "Skenbar effekt",
  "entity.current.reactive": "Reaktiv effekt",
  "entity.current.estimated": "Uppskattad (effektfaktor 0,9)",
  "entity.current.estimatedNote": "SpotNav uppskattar strömmen från effekten. Uppskattningen markeras i kortet.",
  "entity.battery.title": "Batteri",
  "entity.battery.none": "Inget",
  "entity.battery.one": "En sensor",
  "entity.battery.two": "Laddning och urladdning som två sensorer",
  "entity.energy.title": "Energi",
  "entity.energy.meter": "Energiräknare (kWh)",
  "entity.energy.power": "Effekt (W) — SpotNav räknar ut energin",
  "entity.energy.none": "Ingen",
  "entity.notice.estimated": "Strömmen är uppskattad från effekten, med effektfaktor {pf} eller bättre som antagande. Uppskattningen är aldrig lägre än den verkliga strömmen vid den effektfaktorn eller bättre, och underskattar den under det. Lägg till mätarens ström, skenbara effekt eller reaktiva effekt för ett exakt värde.",
  "entity.notice.estimatedShort": "Uppskattad",
  "entity.warning.updateInterval": "{integration} uppdaterar var {seconds} s, långsammare än högsta mätvärdesålder.",
  "entity.warning.updateIntervalOption": 'Sänk det med "{option}" i den integrationen.',
  "entity.warning.onChange": "{integration} rapporterar bara när ett värde ändras, så ett stabilt värde kan se gammalt ut. Höj högsta mätvärdesålder om anläggningen ofta anses inaktuell.",
  "entity.warning.ownBalancing": "{name} ({integration}) balanserar last själv och kan motverka SpotNavs aktiva styrning. Använd en av dem.",
  "entity.warning.externalBalancer": "{integration} balanserar strömmen för {name} själv, så SpotNav startar och stoppar laddaren men skriver inte dess ström.",
  "entity.warning.unknown": "Anläggningen har en avisering som den här versionen inte kan visa. Uppdatera SpotNav.",
  "entity.detect.title": "Hittat i Home Assistant",
  "entity.detect.intro": "De här känns igen i din installation. Inget ändras förrän du trycker på Använd.",
  "entity.site.intro": "Mätning för anläggningen. {applies}",
  "entity.detect.meters": "Elmätare",
  "entity.detect.batteries": "Hembatterier",
  "entity.detect.use": "Använd",
  "entity.detect.inUse": "Används",
  "entity.detect.direct": "Mäter fasströmmen direkt",
  "entity.detect.derived": "Räknas fram ur effekt och spänning",
  "entity.detect.signed": "Teckenmärkt ström: läses som sin storlek",
  "entity.detect.inverted": "Effekt med export som positiv: negeras",
  "entity.detect.gridPower": "Total näteffekt hittad: används för sol- och hybridladdning",
  "entity.detect.estimated": "Uppskattad ström: ingen ström, skenbar eller reaktiv effekt hittades",
  "entity.detect.enable": "Aktiverar {count} entiteter som integrationen levererar avaktiverade.",
  "entity.detect.battery.inverted": "Urladdning som positiv: negeras",
  "entity.detect.battery.pair": "Laddning och urladdning är två sensorer",
  "entity.detect.confidence.low": "Låg säkerhet: kontrollera att tecknen stämmer.",
  "entity.detect.warning.own_load_balancing": "Den här enheten balanserar last själv.",
  "entity.detect.warning.sign_unverified": "Effektens tecken är okänt: kontrollera att export är negativ.",
  "entity.detect.warning.voltage_from_other_device": "Mätaren rapporterar ingen spänning, så fasspänningen från en annan enhet, oftast växelriktaren, används. Det är normalt.",
  "entity.detect.warning.may_measure_subcircuit": "Kontrollera att mätaren mäter hela huvudmatningen, inte en delkrets.",
  "entity.detect.warning.reports_on_change_only": "Rapporterar bara när ett värde ändras.",
  "market.edit": "Ändra elområde och skatter",
  "settings.vehicle.change": "Ändra fordon",
  "settings.vehicle.none": "Inget fordon är inställt ännu.",
  "settings.vehicle.dialogTitle": "Fordon · {name}",
  "settings.vehicle.sensorLegend": "Sensor för laddnivå",
  "entity.foundAutomatically": "Hittas automatiskt",
  "site.row.battery": "Batteri",
  "settings.value.none": "Inget",
  "settings.section.solar": "Sol",
  "site.solar.change": "Ändra solinställningar",
  "site.solar.dialogTitle": "Solinställningar",
  "strategy.setupSolar": "Ställ in sol under Inställningar",
  "strategy.setupSite": "Öppna anläggningens inställningar",
  "site.none": "Den här laddaren har ingen anläggning.",
  "site.applies.one": "Gäller den enda laddaren på den här anläggningen.",
  "site.applies.other": "Gäller alla {count} laddare på den här anläggningen.",
  "site.solarPriority.title": "Solprioritet",
  "site.solarPriority.carFirst": "Bilen först",
  "site.solarPriority.batteryFirst": "Batteriet först",
  "site.solarForecast.title": "Källor för solprognos",
  "site.solarForecast.none": "Ingen vald. Utan en källa planerar hybrid en laddning som Billigast.",
  "site.activeControl.title": "Aktiv lastbalansering",
  "site.activeControl.on": "På",
  "site.activeControl.off": "Av",
  "site.activeControl.available": "Tillgänglig",
  "site.activeControl.reason.duplicateMembership": "Inte tillgänglig: den här laddaren tillhör mer än en anläggning.",
  "site.activeControl.reason.noCommandableCharger": "Inte tillgänglig: ingen laddare på anläggningen tar emot ett strömkommando.",
  "site.activeControl.reason.measurement": "Inte tillgänglig: anläggningens egen effektmätning är inte i gott skick.",
  "site.activeControl.note": "Görs efter bästa förmåga och är ingen skyddsanordning. När den slås av får laddaren tillbaka den ström som sänkts.",
  "site.activeControl.pending": "Sparar…",
  "site.activeControl.error.unavailable": "Lastbalanseringen kan inte slås på just nu: anläggningen går inte att styra aktivt för tillfället. Inget ändrades.",
  "site.activeControl.error.confirmation": "Ändringen kunde inte bekräftas, så den tidigare inställningen behölls.",
  "site.activeControl.restore.off": "Lastbalanseringen är av.",
  "site.activeControl.restore.notNeeded": "Ingen laddare behövde få tillbaka sin ström.",
  "site.activeControl.restore.restoredOne": "Laddaren återställdes till {to} A.",
  "site.activeControl.restore.restoredNamed": "{name} återställdes till {to} A.",
  "site.activeControl.restore.unnamed": "En laddare",
  "site.activeControl.restore.line": "{name}: {text}",
  "site.activeControl.restore.failed.write_failed": "Laddaren avvisade kommandot att återställa strömmen och kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.unconfirmed": "Laddaren bekräftade inte den återställda strömmen och kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.assigned_current_unreadable": "Laddarens strömgräns gick inte att läsa och laddaren kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.no_authoritative_current": "SpotNav har ingen begärd ström att återställa laddaren till och den kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.charger_not_loaded": "Laddaren är inte laddad i Home Assistant och kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.membership_conflict": "Laddaren tillhör mer än en anläggning och lämnades orörd, så den kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.probe_in_flight": "Laddaren var upptagen med ett strömtest och kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.below_minimum": "Strömmen som skulle återställas är lägre än laddaren accepterar och den kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.external_balancer": "En annan integration balanserar laddarens ström, så SpotNav skrev den inte och den kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.no_connector_target": "SpotNav hittade inte laddarens kontakt att styra och laddaren kan fortfarande vara begränsad.",
  "site.activeControl.restore.failed.unknown": "Laddaren kunde inte återställas och kan fortfarande vara begränsad.",
  "site.error.conflict": "Ändrat någon annanstans. De aktuella värdena visas.",
  "settings.consumption.label": "Förbrukning",
  "settings.value.unset": "Inte inställt",
  "issue.count.one": "{count} punkt att granska",
  "issue.count.other": "{count} punkter att granska",
  "graph.title": "Laddplan",
  "graph.description": "Prisgraf från {from} till {to}, i {unit} per kWh. {count} intervall över {days} dagar. Vald: {selected}.",
  "graph.descriptionNoSelection": "Inget valt.",
  "graph.descriptionNow": "Aktuellt intervall: {now}.",
  "graph.legend.today": "Idag",
  "graph.legend.tomorrow": "Imorgon",
  "graph.legend.past": "Tidigare dag",
  "graph.legend.current": "Aktuellt intervall",
  "graph.legend.selection": "Valt intervall",
  "graph.legend.cheap": "Billigare än dagens medelpris",
  "graph.legend.expensive": "Dyrare än dagens medelpris",
  "graph.legend.installed": "Schemalagd",
  "graph.legend.proposal": "Förslag",
  "graph.summary.max": "Max",
  "graph.summary.min": "Min",
  "graph.summary.current": "Nu",
  "graph.readout": "{day} {time} · {price}",
  "graph.readoutMissing": "{day} {time} · inget pris",
  "graph.readoutWithOffset": "{day} {time} ({offset}) · {price}",
  "graph.readoutMissingWithOffset": "{day} {time} ({offset}) · inget pris",
  "graph.selected": "Valt pris",
  "graph.priceAxis": "Pris, {unit} per kWh",
  "graph.noZone": "Tider är inte tillgängliga: integrationen har inte rapporterat någon marknadszon för den här laddaren.",
  "graph.hint": "Använd piltangenterna för att stega genom intervallen.",
  "graph.keyboardInstructions": "Prisgraf. Piltangenterna flyttar mellan intervall, Home och End hoppar till ändarna, Escape rensar valet.",
  "plan.proposal.one": "Billigaste laddperioden ({count})",
  "plan.proposal.other": "Billigaste laddperioderna ({count})",
  "plan.installed.one": "Schemalagd laddperiod ({count})",
  "plan.installed.other": "Schemalagda laddperioder ({count})",
  "plan.period": "{day} {from}–{to}",
  "plan.periodWithOffset": "{day} {from}–{to} ({offset})",
  "plan.cost": "Kostnad",
  "plan.energy": "Energi",
  "plan.distance": "Sträcka",
  "plan.power": "Effekt",
  "plan.activeNow": "Nu",
  "plan.missing": "okänt",
  "cap.title": "Vad laddaren kan",
  "cap.intro": "Hämtat från vad integrationen rapporterar för den här laddaren. Det som inte är tillgängligt här kan kortet inte slå på.",
  "cap.autoPrice": "Automatisk prisplanering",
  "cap.currentLimit": "Dynamisk strömbegränsning",
  "cap.loadBalancing": "Lastbalansering",
  "cap.targetSoc": "Bilens laddnivå och mål",
  "cap.available": "Tillgängligt",
  "cap.unavailable": "Inte tillgängligt för den här laddaren",
  "action.start": "Starta nu",
  "action.stop": "Stoppa",
  "action.resume": "Återuppta automatisk laddning",
  "action.startHelp": "Startar en laddning nu. Den lovar ingen varaktighet: Auto kan ta över igen vid nästa avstämning.",
  "action.pauseAutomatic": "Pausa automatisk laddning",
  "action.pauseAutomaticShort": "Pausa",
  "action.resumeShort": "Återuppta",
  "pause.sheetTitle": "Pausa automatisk laddning",
  "pause.intro": "Välj hur länge pausen varar. Endast de tidsstyrda valen tar slut av sig själva.",
  "pause.nextPeriod": "Till nästa planerade period",
  "pause.untilTomorrow": "Till i morgon",
  "pause.untilResumed": "Tills jag återupptar",
  "strategy.title": "Strategi",
  "bar.charging": "Laddning",
  "bar.schedule": "Schema",
  "bar.start": "Starta",
  "bar.waiting": "Startar…",
  "bar.stop": "Stoppa",
  "bar.chargeNow": "Ladda nu",
  "bar.chargingNow": "Laddar",
  "bar.scheduleActive": "Schema aktivt",
  "bar.schedulePaused": "Schema pausat",
  "bar.state.notCharging": "laddar inte",
  "bar.state.charging": "laddar",
  "bar.state.scheduleActive": "aktivt",
  "bar.state.schedulePaused": "pausat",
  "bar.change": "ändra",
  "strategy.dialogTitle": "Laddstrategi",
  "strategy.intro": "Vad planen optimerar. En strategi som inte är tillgänglig här kan inte väljas ännu.",
  "strategy.cheapest": "Billigast",
  "strategy.solar": "Sol",
  "strategy.hybrid": "Hybrid",
  "strategy.reason.solar": "Kräver mätning av solöverskott",
  "strategy.reason.hybrid": "Kräver sol- och prisstyrning",
  "strategy.reason.totalPower": "Sol kräver mätarens totala näteffekt",
  "strategy.status.solar.charging": "Sol · laddar med {amps} A från överskott",
  "strategy.status.solar.chargingUnknown": "Sol · laddar från överskott",
  "strategy.status.solar.arming": "Sol · överskott hittat, startar snart",
  "strategy.status.solar.disarming": "Sol · överskottet minskar, stoppar snart",
  "strategy.status.solar.noReadingStopped": "Sol · stoppad, ingen användbar mätning",
  "strategy.status.solar.noReadingWaiting": "Sol · ingen användbar mätning ännu",
  "strategy.status.solar.waitingForSun": "Sol · väntar på sol",
  "strategy.status.solar.unknown": "Sol · status okänd",
  "strategy.status.hybrid.grid": "Hybrid · {grid} kWh från elnätet{window}",
  "strategy.status.hybrid.creditSuffix": ", {credit} kWh väntas från solen",
  "strategy.status.hybrid.noForecast": "Hybrid · ingen prognoskälla — planerar som Billigast",
  "strategy.status.hybrid.noPriceData": "Hybrid · väntar på prisuppgifter",
  "strategy.status.hybrid.satisfied": "Hybrid · laddbehovet är redan uppfyllt",
  "strategy.status.hybrid.unknown": "Hybrid · planerar",
  "advisory.vehicleNotRequestingCurrent": "Laddningen startades, men fordonet begär ingen ström. Kontrollera fordonets laddningsinställningar eller anslut kabeln igen.",
  "advisory.powerBelowThreshold": "Laddningen startades, men laddaren drar nästan ingen effekt. Bilen kan vara klar eller inte ladda: kontrollera fordonets laddningsinställningar eller anslut kabeln igen.",
  "control.noSettings": "Den här laddaren har inga inställningar ännu, så det finns inget att starta eller stoppa.",
  "control.pauseUnsettled": "En paus är sparad men har inte börjat gälla ännu. Inget tillämpas.",
  "control.pauseClearFailed": "En paus har löpt ut men kunde inte rensas, så inget tillämpas.",
  "control.pauseStopFailed": "Laddaren kunde inte stoppas när pausen godkändes. Försök med Stoppa igen.",
  "control.startNotAcknowledged": "Laddaren bekräftade inte startkommandot. Försök igen.",
  "control.reconcileFailed": "Den senaste ändringen sparades, men planen kunde inte uppdateras.",
  "control.executionError": "Integrationen rapporterade ett problem med den senaste körningen.",
  "control.pausedUntil": "Pausad till {time}.",
  "control.pausedIndefinitely": "Pausad tills du återupptar.",
  "control.actionPending": "Ett startkommando väntar på att laddaren ska bekräfta.",
  "action.error.unavailable": "Den åtgärden är inte tillgänglig just nu.",
  "action.error.failed": "Åtgärden kunde inte utföras. Inget ändrades.",
  "action.error.reconcileFailed": "Åtgärden utfördes, men planen kunde inte uppdateras.",
  "action.error.confirmationFailed": "Åtgärden togs emot, men dess aktuella tillstånd kunde inte bekräftas.",
  "action.error.invalidPause": "Det pausvalet är inte tillgängligt för den här laddaren just nu.",
  "action.error.charger": "Den konfigurerade laddaren kunde inte nås.",
  "action.error.version": "Kortet och integrationen talar olika åtgärdsversioner.",
  "action.error.generic": "Åtgärden misslyckades. Kontrollera anslutningen till Home Assistant och försök igen.",
  "dialog.close": "Stäng",
  "dialog.issues": "Detta behöver åtgärdas",
  "dialog.issuesIntro": "Allt som integrationen rapporterade för den här laddaren, i rapporterad ordning.",
  "context.area": "Område",
  "context.currency": "Valuta",
  "market.aria": "Ändra elområde och skatter: {value}",
  "market.unset": "Inte angett",
  "market.title": "Område och skatter",
  "market.intro": "Vilken marknad laddaren köper el från, och de skatter som läggs på priserna.",
  "market.loading": "Läser områdeslistan…",
  "market.readOnly": "Endast administratörer kan ändra område och skatter. Du kan läsa dem här.",
  "market.area.label": "Område",
  "market.area.description": "Området avgör vilka priser som används och vilka skatter som föreslås.",
  "market.area.unlisted": "publiceras inte längre",
  "market.area.missing": "Inga områden publiceras just nu.",
  "market.vat.label": "Moms",
  "market.vat.description": "Mervärdesskatt i procent.",
  "market.tax.label": "Energiskatt",
  "market.tax.description": "Energiskatt i områdets minsta valutaenhet.",
  "market.transfer.label": "Nätavgift",
  "market.transfer.description": "Nätavgift i områdets minsta valutaenhet.",
  "market.enabled.aria": "{component}: aktiverad",
  "market.resetToSuggestion": "Återställ till förslaget",
  "market.value.aria": "{component}: eget värde",
  "market.suggestion.label": "Föreslaget: {value} {unit}",
  "market.suggestion.none": "Inget föreslaget värde publiceras för området.",
  "market.error.read": "Områdeslistan gick inte att läsa.",
  "market.error.version": "Det här kortet och integrationen talar olika marknadsversioner.",
  "market.error.areaRequired": "Välj ett område innan du sparar.",
  "market.error.areaUnknown": "Det området är inte ett av de publicerade valen.",
  "market.error.noSuggestion": "Området publicerar inget föreslaget värde för det.",
  "market.state.loading": "Områdeslistan läses fortfarande.",
  "market.state.staleOffline": "Det här är den senaste områdeslistan Home Assistant tog emot; reläet har inte gått att nå sedan dess.",
  "market.state.staleInvalid": "Det här är den senaste områdeslistan Home Assistant tog emot; reläets nyare lista gick inte att läsa.",
  "market.state.offline": "Reläet gick inte att nå, så inga områden listas just nu.",
  "market.state.invalid": "Reläets områdeslista gick inte att läsa, så inga områden listas just nu.",
  "settings.energy.aria": "Energi: {value}",
  "settings.deadline.aria": "Sluttid: {value}",
  "settings.current.aria": "Ström: {value}",
  "settings.energy.unset": "Ej angivet",
  "settings.deadline.none": "Ingen sluttid",
  "settings.current.unset": "Ej angivet",
  "settings.energy.title": "Laddenergi",
  "settings.energy.intro": "Hur mycket energi laddningen ska ge. Planen gör om det till perioder.",
  "settings.energy.label": "Önskad energi",
  "settings.energy.targetSoc": "Den här laddaren planerar från ett målvärde för laddningsnivån, så den manuella energin går inte att ändra här.",
  "settings.deadline.title": "Sluttid och laddperioder",
  "settings.deadline.intro": "När laddningen senast ska vara klar, och hur många perioder den får använda.",
  "settings.deadline.enabled": "Klar senast en tid",
  "settings.deadline.time": "Avresetid",
  "settings.deadline.date": "Avresa",
  "settings.deadline.dateDaily": "Varje dag",
  "settings.deadline.dateOn": "Ett visst datum",
  "settings.deadline.dateHelp": "Planen kan vänta på timmar som brukar vara billigare. Avresan hålls alltid.",
  "settings.deadline.datePast": "Datumet har passerat, så planen körs varje dag tills du väljer ett nytt datum. Sparar du rensas det.",
  "settings.deadline.weekdays": "Veckodagar",
  "settings.deadline.weekdaysHelp": "En dag du utelämnar har ingen avresa: planen löper till nästa dag du valt.",
  "settings.deadline.today": "idag",
  "settings.deadline.tomorrow": "imorgon",
  "settings.deadline.periods": "Högsta antal laddperioder",
  "settings.current.title": "Planerad ström",
  "settings.current.intro": "Strömmen planen får begära. Det är ett planeringsvärde, inte ett kommando till laddaren.",
  "settings.current.label": "Planerad ström",
  "settings.loading": "Läser in de aktuella inställningarna…",
  "settings.section.support": "Support",
  "debug.intro": "Sparar en fil med versioner, status och de senaste loggraderna för en felanmälan. Hemligheter och din exakta plats lämnas utanför.",
  "debug.download": "Ladda ner felsökningsinfo",
  "debug.preparing": "Förbereder…",
  "debug.error.notAdmin": "Bara administratörer kan ladda ner felsökningsinfo.",
  "debug.error.failed": "Felsökningsinfon kunde inte hämtas.",
  "settings.readOnly": "Bara administratörer kan ändra inställningar. Du kan läsa dem här.",
  "settings.save": "Spara",
  "settings.cancel": "Avbryt",
  "settings.reload": "Använd serverns värden",
  "settings.reapply": "Använd min ändring igen",
  "settings.conflict.title": "Ändrat någon annanstans",
  "settings.conflict.intro": "Inställningarna ändrades efter att den här dialogen öppnades. Inget sparades, och dina värden finns kvar.",
  "settings.error.required": "Fyll i detta innan du sparar.",
  "settings.error.invalidNumber": "Det är inte ett tal.",
  "settings.error.outOfRange": "Värdet ligger utanför tillåtet intervall.",
  "settings.error.invalidDate": "Välj ett giltigt datum.",
  "settings.error.dateRange": "Välj ett datum från idag och upp till 7 dagar fram.",
  "settings.error.weekdays": "Välj minst en veckodag.",
  "settings.error.invalidTime": "Använd en tid som 06:30.",
  "settings.error.read": "Inställningarna kunde inte läsas.",
  "settings.error.refused": "Inställningarna avvisades. Inget ändrades.",
  "settings.error.notCommitted": "Inställningarna sparades inte. Inget ändrades.",
  "settings.error.reconcileFailed": "Inställningarna sparades, men planen kunde inte uppdateras.",
  "settings.error.confirmationFailed": "Inställningarna sparades, men det aktuella tillståndet kunde inte bekräftas.",
  "settings.error.version": "Kortet och integrationen talar olika inställningsversioner.",
  "settings.error.unavailable": "Inställningar är inte tillgängliga just nu.",
  "settings.error.charger": "Den konfigurerade laddaren kunde inte nås.",
  "settings.error.generic": "Inställningarna kunde inte ändras. Inget ändrades.",
  "settings.error.invalid": "Inställningarna avvisades som ogiltiga. Inget ändrades.",
  "settings.error.readOnly": "Bara administratörer kan ändra inställningar.",
  "settings.energy.slider": "Energireglage, 0,5 till 100 kWh i halvkWh-steg",
  "settings.current.slider": "Strömreglage, {min} till {max} A i hela ampere",
  "settings.sliderOutOfRange": "Det exakta värdet ligger utanför reglagets intervall. Använd sifferfältet.",
  "settings.current.power": "Nominell effekt ≈ {power} kW",
  "settings.current.powerUnknown": "Nominell effekt okänd",
  "bar.plan": "Plan",
  "settings.plan.title": "Laddplan",
  "settings.plan.intro": "Hur mycket som ska laddas, när det ska vara klart och vilken ström planen får begära. Det är planeringsvärden, inte kommandon till laddaren.",
  "settings.plan.aria": "Plan: {value}",
  "entity.vehicle.automatic": "Automatisk avkänning",
  "entity.vehicle.several": "Fordonet har flera batterisensorer. Automatisk avkänning kan inte välja mellan dem, så laddnivån läses inte förrän du väljer en.",
  "entity.error.field.unknownVehicle": "Fordonet finns inte längre.",
  "issue.targetSocUnknown": "Målnivån kan inte planeras: fordonets laddnivå eller batterikapacitet är okänd. Öppna Plan för att välja sensor eller ange kapaciteten.",
  "settings.plan.mode.legend": "Ladda efter",
  "settings.plan.mode.energy": "Energi (kWh)",
  "settings.plan.mode.soc": "Mål-SoC (%)",
  "settings.soc.target": "Målnivå för laddningen",
  "settings.soc.slider": "Målnivå för laddningen, procent",
  "settings.soc.vehicle": "Fordon",
  "settings.soc.vehicleUnknown": "Inte valt",
  "settings.soc.estimated": "uppskattad",
  "settings.soc.estimatedFrom": "uppskattad, senast avläst {age}",
  "settings.soc.unknown": "Okänd",
  "settings.soc.need": "Energi som behövs",
  "settings.soc.capacityMissing": "Batterikapacitet saknas — ange den i Inställningar",
  "settings.capacity.label": "Batterikapacitet",
  "settings.capacity.unset": "Inte angiven",
  "settings.soc.needSensor": "En sensor för laddnivå måste väljas för fordonet innan ett mål kan planeras.",
  "settings.soc.age.now": "nyss",
  "settings.soc.age.min": "för {count} min sedan",
  "settings.soc.age.hour": "för {count} h sedan",
  "settings.soc.age.day": "för {count} d sedan",
  "settings.overview.titleNamed": "Kortinställningar · {name}",
  "settings.soc.factNow": "Nu {value}",
  "settings.soc.factLimit": "Bilens laddgräns {value}",
  "settings.soc.noNeed": "Ingen laddning behövs nu",
  "settings.soc.toLimit": "Laddar till bilens gräns, {value}",
  "settings.soc.readAge": "Avläst {age}",
  "settings.soc.needAfterVehicle": "Behovet räknas när fordonsvalet är sparat.",
  "vehicleLine.aria": "{name}, {summary}. Välj vilket fordon som ska laddas",
  "vehicleLine.estimateTitle": "Uppskattad mellan avläsningarna, avläst {age}",
  "vehicleLine.dialogTitle": "Vilket fordon ska laddas?",
  "vehicleLine.noReading": "Ingen avläsning",
  "settings.vehicle.unnamed": "Fordon utan namn",
  "settings.vehicle.plannedHere": "Laddaren planerar för det här fordonet",
  "settings.vehicle.capacityReported": "rapporterad av bilen",
  "settings.vehicle.socNone": "Ingen sensor vald",
  "settings.vehicle.charge": "Laddnivå",
  "settings.vehicle.error.capacity": "Batterikapaciteten måste vara mellan 1 och 500 kWh.",
  "settings.vehicle.error.consumption": "Förbrukningen måste vara mellan 0,1 och 50 kWh/10 km.",
  "settings.phases.legend": "Faser som laddaren använder",
  "settings.phases.one": "1 fas",
  "settings.phases.three": "3 faser",
  "settings.phases.unset": "Antalet faser är inte angivet.",
  "settings.phases.help": "Styr vilken effekt planen räknar med för en viss ström. En trefasladdare kan ändå ladda ett fordon på en fas.",
  "cap.targetSocNote": "Kräver en sensor för fordonets laddnivå och batteriets kapacitet. Ange dem i Plan och Inställningar.",
  "control.startStop": "Start och stopp",
  "control.startStop.easeeFixed": "SpotNav startar och stoppar laddaren via Easee-integrationens tjänst (pausa och återuppta), så det finns ingen start- eller stoppentitet att välja.",
  "control.limit.minutes": "Det går minst {count} minuter mellan två strömändringar.",
  "control.limit.seconds": "Det går minst {count} sekunder mellan två strömändringar.",
  "control.limit.flash": "Strömmen lagras i laddaren och ändras bara när en laddning startar.",
  "control.limit.stopOnly": "Lastbalansering kan bara stoppa laddningen, inte sänka strömmen.",
  "control.limit.installation": "Gränsen gäller hela installationen.",
  "control.current": "Laddström",
  "control.current.none": "Sätts inte av SpotNav (laddaren behåller sin egen gräns)",
  "control.current.ocpp": "OCPP ChangeConfiguration",
  "control.current.number": "En nummerentitet: {name}",
  "control.current.service": "Easees dynamiska strömgräns",
  "control.current.off": "SpotNav sätter inte strömmen. Slå på det i laddarens alternativ.",
  "control.conflict": "Laddarens egen {label} är på ({name}). Den kan motverka SpotNav: stäng av den.",
  "control.disabled": "Laddarens egen aktiveringsbrytare är av ({name}). SpotNav kan inte starta den: slå på den.",
  "control.otherController": "{name} styr också laddare; stäng av den för den här laddaren, annars motverkar {name} och SpotNav varandra."
};

// src/i18n/index.ts
var LANGUAGES = ["en", "sv", "nb", "da", "fi"];
var TRANSLATIONS = { en, sv, nb, da, fi };
var pluralRules = /* @__PURE__ */ new Map();
function rulesFor(language) {
  const existing = pluralRules.get(language);
  if (existing !== void 0) {
    return existing;
  }
  const created = new Intl.PluralRules(language, { type: "cardinal" });
  pluralRules.set(language, created);
  return created;
}
function pluralForm(language, count) {
  return rulesFor(language).select(count) === "one" ? "one" : "other";
}
function resolveLanguage(language) {
  if (typeof language !== "string" || language === "") {
    return "en";
  }
  const base = (language.toLowerCase().split(/[-_]/)[0] ?? "").trim();
  if (base === "no" || base === "nn" || base === "nb") {
    return "nb";
  }
  return LANGUAGES.includes(base) ? base : "en";
}
function translate(language, key, params = {}) {
  const template = TRANSLATIONS[language][key] ?? TRANSLATIONS.en[key];
  return template.replace(/\{(\w+)\}/g, (match, name) => params[name] ?? match);
}

// src/settings.ts
var MalformedPayload = class extends Error {
};
function bad() {
  throw new MalformedPayload("malformed");
}
function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function record(value) {
  return isRecord(value) ? value : bad();
}
function exactKeys(source, keys) {
  if (Object.keys(source).length !== keys.length) {
    return bad();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      return bad();
    }
  }
}
function text(source, key) {
  const value = source[key];
  return typeof value === "string" ? value : bad();
}
function textOrNull(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad();
}
function booleanValue(source, key) {
  const value = source[key];
  return typeof value === "boolean" ? value : bad();
}
function finite(source, key) {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}
function whole(source, key) {
  const value = source[key];
  return typeof value === "number" && Number.isInteger(value) ? value : bad();
}
function wholeOrNull(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isInteger(value) ? value : bad();
}
function oneOf(source, key, allowed) {
  const value = source[key];
  if (typeof value !== "string" || !allowed.includes(value)) {
    return bad();
  }
  return value;
}
function list(source, key) {
  const value = source[key];
  return Array.isArray(value) ? value : bad();
}
var WALL_TIME = /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/;
var ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
function isIsoDate(value) {
  if (!ISO_DATE.test(value)) {
    return false;
  }
  const parsed = /* @__PURE__ */ new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}
function dateOrNull(source, key) {
  const value = textOrNull(source, key);
  return value === null || isIsoDate(value) ? value : bad();
}
function wallTime(source, key) {
  const value = text(source, key);
  return WALL_TIME.test(value) ? value : bad();
}
function instantOrNull(source, key) {
  const value = textOrNull(source, key);
  if (value === null) {
    return null;
  }
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) {
    return bad();
  }
  return value;
}
var PAUSE_CHOICES = ["next_period", "until_tomorrow", "until_resumed"];
var DRIVERS = ["manual_kwh", "target_soc"];
var STRATEGIES = [SETTINGS_STRATEGY_CHEAPEST, SETTINGS_STRATEGY_SOLAR, SETTINGS_STRATEGY_HYBRID];
var FISCAL_KEYS = ["enabled", "value"];
var OVERRIDE_KEYS = ["area_id", "vat", "tax", "transfer"];
var TARGET_KEYS = ["vehicle_id", "target_percent"];
var BODY_KEYS = [
  "area_id",
  "overrides",
  "phases",
  "amps",
  "requested_kwh",
  "max_periods",
  "departure_enabled",
  "departure_time",
  "strategy",
  "driver",
  "target"
];
var RECORD_KEYS = [...BODY_KEYS, "revision"];
var OPTIONAL_RECORD_KEYS = ["departure_date", "departure_weekdays"];
var ALL_WEEKDAYS = [1, 2, 3, 4, 5, 6, 7];
function weekdays(source, key) {
  const days = list(source, key).map(
    (day) => typeof day === "number" && Number.isInteger(day) && day >= 1 && day <= 7 ? day : bad()
  );
  return days.length === 0 || new Set(days).size !== days.length ? bad() : [...days].sort((a, b) => a - b);
}
var ENVELOPE_KEYS = ["api_version", "ok", "error", "settings", "pause"];
var PAUSE_KEYS = ["choice", "admitted_at", "expires_at"];
function decodeFiscal(source) {
  exactKeys(source, FISCAL_KEYS);
  const enabled = booleanValue(source, "enabled");
  const value = source["value"];
  if (value !== null && !(typeof value === "number" && Number.isFinite(value))) {
    return bad();
  }
  return { enabled, value };
}
function decodeOverride(source) {
  exactKeys(source, OVERRIDE_KEYS);
  return {
    area_id: text(source, "area_id"),
    vat: decodeFiscal(record(source["vat"])),
    tax: decodeFiscal(record(source["tax"])),
    transfer: decodeFiscal(record(source["transfer"]))
  };
}
function decodeTarget(source) {
  exactKeys(source, TARGET_KEYS);
  let targetPercent = null;
  if (source["target_percent"] !== null) {
    const percent2 = finite(source, "target_percent");
    if (percent2 < 0 || percent2 > 100) {
      return bad();
    }
    targetPercent = percent2;
  }
  return {
    vehicle_id: textOrNull(source, "vehicle_id"),
    target_percent: targetPercent
  };
}
function decodeSettingsRecord(raw) {
  const source = record(raw);
  const present = OPTIONAL_RECORD_KEYS.filter((key) => Object.prototype.hasOwnProperty.call(source, key));
  const hasDate = present.includes("departure_date");
  const hasWeekdays = present.includes("departure_weekdays");
  exactKeys(source, [...RECORD_KEYS, ...present]);
  const revision = whole(source, "revision");
  if (revision < 0) {
    return bad();
  }
  return {
    revision,
    area_id: textOrNull(source, "area_id"),
    overrides: list(source, "overrides").map((item) => decodeOverride(record(item))),
    phases: wholeOrNull(source, "phases"),
    amps: wholeOrNull(source, "amps"),
    requested_kwh: finite(source, "requested_kwh"),
    max_periods: whole(source, "max_periods"),
    departure_enabled: booleanValue(source, "departure_enabled"),
    departure_time: wallTime(source, "departure_time"),
    departure_date: hasDate ? dateOrNull(source, "departure_date") : null,
    departure_weekdays: hasWeekdays ? weekdays(source, "departure_weekdays") : [...ALL_WEEKDAYS],
    strategy: oneOf(source, "strategy", STRATEGIES),
    driver: oneOf(source, "driver", DRIVERS),
    target: decodeTarget(record(source["target"]))
  };
}
function decodePauseObservation(raw) {
  const source = record(raw);
  exactKeys(source, PAUSE_KEYS);
  const choice = textOrNull(source, "choice");
  if (choice !== null && !PAUSE_CHOICES.includes(choice)) {
    return bad();
  }
  const admittedAt = instantOrNull(source, "admitted_at");
  const expiresAt = instantOrNull(source, "expires_at");
  if (choice === null) {
    return admittedAt !== null || expiresAt !== null ? bad() : { choice: null, admitted_at: null, expires_at: null };
  }
  if (choice === "until_resumed" ? expiresAt !== null : expiresAt === null) {
    return bad();
  }
  if (admittedAt !== null && expiresAt !== null && Date.parse(expiresAt) <= Date.parse(admittedAt)) {
    return bad();
  }
  return { choice, admitted_at: admittedAt, expires_at: expiresAt };
}
function malformed() {
  return { ok: false, failure: "malformed" };
}
function decodeSettingsAnswer(raw) {
  try {
    if (!isRecord(raw)) {
      return malformed();
    }
    const version = raw["api_version"];
    if (typeof version === "number" && version !== SETTINGS_API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys(raw, ENVELOPE_KEYS);
    if (version !== SETTINGS_API_VERSION) {
      return malformed();
    }
    const ok = booleanValue(raw, "ok");
    const error = textOrNull(raw, "error");
    const rawSettings = raw["settings"];
    const rawPause = raw["pause"];
    const settings = rawSettings === null ? null : decodeSettingsRecord(rawSettings);
    const pause = rawPause === null ? null : decodePauseObservation(rawPause);
    if (ok) {
      if (error !== null || settings === null || pause === null) {
        return malformed();
      }
      return { ok: true, value: { ok: true, settings, pause } };
    }
    if (error === null || error === "" || settings === null !== (pause === null)) {
      return malformed();
    }
    return { ok: true, value: { ok: false, code: error, settings, pause } };
  } catch (error) {
    if (error instanceof MalformedPayload) {
      return malformed();
    }
    throw error;
  }
}
function encodeBody(record7) {
  return {
    area_id: record7.area_id,
    overrides: record7.overrides.map((item) => ({
      area_id: item.area_id,
      vat: { ...item.vat },
      tax: { ...item.tax },
      transfer: { ...item.transfer }
    })),
    phases: record7.phases,
    amps: record7.amps,
    requested_kwh: record7.requested_kwh,
    max_periods: record7.max_periods,
    departure_enabled: record7.departure_enabled,
    departure_time: record7.departure_time,
    departure_date: record7.departure_date,
    departure_weekdays: [...record7.departure_weekdays],
    strategy: record7.strategy,
    driver: record7.driver,
    target: { ...record7.target }
  };
}
function strategyReplacement(record7, strategy) {
  if (!STRATEGIES.includes(strategy)) {
    return { ok: false, errorKey: "settings.error.invalid" };
  }
  return { ok: true, body: { ...encodeBody(record7), strategy }, changed: strategy !== record7.strategy };
}
function vehicleReplacement(record7, vehicleId) {
  if (vehicleId.trim() === "") {
    return { ok: false, errorKey: "settings.error.invalid" };
  }
  const body = encodeBody(record7);
  return {
    ok: true,
    body: { ...body, target: { ...record7.target, vehicle_id: vehicleId } },
    changed: vehicleId !== record7.target.vehicle_id
  };
}
function formFromRecord(record7) {
  return {
    energy: String(record7.requested_kwh),
    deadlineEnabled: record7.departure_enabled,
    deadlineTime: record7.departure_time,
    departureDate: record7.departure_date ?? "",
    departureWeekdays: record7.departure_weekdays.join(""),
    maxPeriods: String(record7.max_periods),
    current: record7.amps === null ? "" : String(record7.amps),
    driver: record7.driver,
    targetPercent: record7.target.target_percent === null ? "" : String(record7.target.target_percent),
    vehicleId: record7.target.vehicle_id ?? "",
    phases: record7.phases === null ? "" : String(record7.phases)
  };
}
function decimal(text5, minimum, maximum) {
  const trimmed = text5.trim().replace(",", ".");
  if (trimmed === "") {
    return { ok: false, errorKey: "settings.error.required" };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value)) {
    return { ok: false, errorKey: "settings.error.invalidNumber" };
  }
  if (value < minimum || value > maximum) {
    return { ok: false, errorKey: "settings.error.outOfRange" };
  }
  return { ok: true, value };
}
function integer(text5, minimum, maximum) {
  const trimmed = text5.trim();
  if (trimmed === "") {
    return { ok: false, errorKey: "settings.error.required" };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value) || !Number.isInteger(value)) {
    return { ok: false, errorKey: "settings.error.invalidNumber" };
  }
  if (value < minimum || value > maximum) {
    return { ok: false, errorKey: "settings.error.outOfRange" };
  }
  return { ok: true, value };
}
var TARGET_PERCENT_MIN = 0;
var TARGET_PERCENT_MAX = 100;
var CAPACITY_MIN_KWH = 1;
var CAPACITY_MAX_KWH = 500;
function checkTargetPercent(text5) {
  return decimal(text5, TARGET_PERCENT_MIN, TARGET_PERCENT_MAX);
}
function checkCapacity(text5) {
  const check = decimal(text5, CAPACITY_MIN_KWH, CAPACITY_MAX_KWH);
  return check.ok ? { ok: true, value: Math.round(check.value * 10) / 10 } : check;
}
function checkEnergy(text5) {
  return decimal(text5, ENERGY_MIN_KWH, ENERGY_MAX_KWH);
}
function checkDeadlineTime(text5) {
  const trimmed = text5.trim();
  if (trimmed === "") {
    return { ok: false, errorKey: "settings.error.required" };
  }
  return WALL_TIME.test(trimmed) ? { ok: true, value: trimmed } : { ok: false, errorKey: "settings.error.invalidTime" };
}
var DEPARTURE_DAYS_AHEAD = 7;
function departureDays(timeZone, nowMs) {
  if (!hasZone({ language: "en", timeZone, unit: "", currency: null, majorUnit: null })) {
    return null;
  }
  const midnight = localMidnightAt(nowMs, timeZone);
  const HOUR = 36e5;
  const dayAfter = (days) => localDayKey(midnight + days * 24 * HOUR + 12 * HOUR, timeZone);
  const today = localDayKey(nowMs, timeZone);
  return {
    today,
    max: dayAfter(DEPARTURE_DAYS_AHEAD),
    nextOccurrence: (wallTime2) => {
      const match = /^(\d{2}):(\d{2})$/.exec(wallTime2);
      const wallMs = match === null ? Number.NaN : (Number(match[1]) * 60 + Number(match[2])) * 6e4;
      const sinceMidnight = nowMs - midnight;
      return Number.isFinite(wallMs) && wallMs > sinceMidnight ? today : dayAfter(1);
    }
  };
}
function checkDepartureDate(text5, days, moved) {
  const trimmed = text5.trim();
  if (trimmed === "") {
    return { ok: true, value: null };
  }
  if (!isIsoDate(trimmed)) {
    return { ok: false, errorKey: "settings.error.invalidDate" };
  }
  if (moved && days !== null && (trimmed < days.today || trimmed > days.max)) {
    return { ok: false, errorKey: "settings.error.dateRange" };
  }
  return { ok: true, value: trimmed };
}
function checkMaxPeriods(text5) {
  return integer(text5, PERIODS_MIN, PERIODS_MAX);
}
function checkCurrent(text5) {
  return integer(text5, AMPS_MIN, AMPS_MAX);
}
function checkCurrentInRange(text5, range) {
  const amps = checkCurrent(text5);
  if (!amps.ok) {
    return amps;
  }
  return amps.value < range.minA || amps.value > range.maxA ? { ok: false, errorKey: "settings.error.outOfRange" } : amps;
}
var NOMINAL_VOLTS_SINGLE_PHASE = 230;
var NOMINAL_VOLTS_THREE_PHASE = 400;
function nominalPowerKw(amps, phases) {
  if (!Number.isFinite(amps)) {
    return null;
  }
  if (phases === 1) {
    return NOMINAL_VOLTS_SINGLE_PHASE * amps / 1e3;
  }
  if (phases === 3) {
    return Math.sqrt(3) * NOMINAL_VOLTS_THREE_PHASE * amps / 1e3;
  }
  return null;
}
var ENERGY_SLIDER_MIN_KWH = 0.5;
var ENERGY_SLIDER_MAX_KWH = 100;
var ENERGY_SLIDER_STEP_KWH = 0.5;
var CURRENT_SLIDER_STEP_A = 1;
var STEP_EPSILON = 1e-9;
function sliderRepresents(value, minimum, step) {
  if (!Number.isFinite(value) || value < minimum) {
    return false;
  }
  const steps = (value - minimum) / step;
  return Math.abs(steps - Math.round(steps)) < STEP_EPSILON;
}
function energySliderMaximum(value) {
  return Number.isFinite(value) && value > ENERGY_SLIDER_MAX_KWH ? value : ENERGY_SLIDER_MAX_KWH;
}
var ENERGY_MIN_KWH = 0.1;
var ENERGY_MAX_KWH = 1e3;
var PERIODS_MIN = 1;
var PERIODS_MAX = 8;
var AMPS_MIN = 1;
var AMPS_MAX = 80;
var CONSUMPTION_MIN_KWH_PER_10KM = 0.1;
var CONSUMPTION_MAX_KWH_PER_10KM = 50;
function checkConsumption(text5) {
  return decimal(text5, CONSUMPTION_MIN_KWH_PER_10KM, CONSUMPTION_MAX_KWH_PER_10KM);
}
function replacementFor(kind, record7, values, range = null, opened = null, days = null) {
  const body = encodeBody(record7);
  const energy = kind === "energy" || kind === "plan" ? checkEnergy(values.energy) : null;
  if (energy !== null && !energy.ok) {
    return energy;
  }
  const amps = kind === "current" || kind === "plan" ? currentCheck(values, record7, range) : null;
  if (amps !== null && !amps.ok) {
    return amps;
  }
  const time = kind === "deadline" || kind === "plan" ? checkDeadlineTime(values.deadlineTime) : null;
  if (time !== null && !time.ok) {
    return time;
  }
  const periods = kind === "deadline" || kind === "plan" ? checkMaxPeriods(values.maxPeriods) : null;
  if (periods !== null && !periods.ok) {
    return periods;
  }
  const dateBase = opened === null ? record7 : opened;
  const dateMoved = values.departureDate !== (dateBase.departure_date ?? "");
  const date = kind === "deadline" || kind === "plan" ? checkDepartureDate(values.deadlineEnabled ? values.departureDate : "", days, dateMoved) : null;
  if (date !== null && !date.ok) {
    return date;
  }
  const dayList = kind === "deadline" || kind === "plan" ? weekdaysFromText(values.departureWeekdays) : null;
  if (dayList !== null && dayList === "invalid") {
    return { ok: false, errorKey: "settings.error.weekdays" };
  }
  const driverOk = values.driver === "manual_kwh" || values.driver === SETTINGS_DRIVER_TARGET_SOC;
  if (kind === "plan" && !driverOk) {
    return { ok: false, errorKey: "settings.error.invalid" };
  }
  const soc = kind === "plan" && values.driver === SETTINGS_DRIVER_TARGET_SOC;
  const targetPercent = soc ? checkTargetPercent(values.targetPercent) : null;
  if (targetPercent !== null && !targetPercent.ok) {
    return targetPercent;
  }
  let changed = false;
  const next = { ...body };
  if (kind === "plan" && driverOk && (opened === null || values.driver !== opened.driver)) {
    next.driver = values.driver;
    changed = changed || values.driver !== record7.driver;
  }
  const target = { ...record7.target };
  let targetMoved = false;
  if (targetPercent !== null && targetPercent.ok && (opened === null || targetPercent.value !== opened.target.target_percent)) {
    target.target_percent = targetPercent.value;
    targetMoved = targetMoved || targetPercent.value !== record7.target.target_percent;
  }
  const vehicleId = values.vehicleId.trim();
  if (soc && vehicleId !== "" && (opened === null || vehicleId !== (opened.target.vehicle_id ?? ""))) {
    target.vehicle_id = vehicleId;
    targetMoved = targetMoved || vehicleId !== record7.target.vehicle_id;
  }
  if (targetMoved) {
    next.target = target;
    changed = true;
  }
  const chosenPhases = values.phases === "1" ? 1 : values.phases === "3" ? 3 : null;
  if (kind === "plan" && chosenPhases !== null && (opened === null || chosenPhases !== opened.phases)) {
    next.phases = chosenPhases;
    changed = changed || chosenPhases !== record7.phases;
  }
  if (energy !== null && energy.ok && (opened === null || energy.value !== opened.requested_kwh)) {
    next.requested_kwh = energy.value;
    changed = changed || energy.value !== record7.requested_kwh;
  }
  if (amps !== null && amps.ok && (opened === null || amps.value !== opened.amps)) {
    next.amps = amps.value;
    changed = changed || amps.value !== record7.amps;
  }
  const deadlineMoved = opened === null || time !== null && time.ok && periods !== null && periods.ok && (values.deadlineEnabled !== opened.departure_enabled || time.value !== opened.departure_time || periods.value !== opened.max_periods || date !== null && date.ok && date.value !== opened.departure_date);
  const weekdaysMoved = dayList !== null && (opened === null ? dayList.join("") !== record7.departure_weekdays.join("") : dayList.join("") !== opened.departure_weekdays.join(""));
  if (weekdaysMoved && dayList !== null) {
    next.departure_weekdays = dayList;
    changed = changed || dayList.join("") !== record7.departure_weekdays.join("");
  }
  if (time !== null && time.ok && periods !== null && periods.ok && deadlineMoved) {
    next.departure_enabled = values.deadlineEnabled;
    next.departure_time = time.value;
    next.max_periods = periods.value;
    if (date !== null && date.ok) {
      next.departure_date = date.value;
    }
    changed = changed || values.deadlineEnabled !== record7.departure_enabled || time.value !== record7.departure_time || periods.value !== record7.max_periods || date !== null && date.ok && date.value !== record7.departure_date;
  }
  return { ok: true, body: next, changed };
}
function weekdaysFromText(text5) {
  const days = [...new Set([...text5].map(Number))].filter((day) => day >= 1 && day <= 7).sort((a, b) => a - b);
  return days.length === 0 ? "invalid" : days;
}
function currentCheck(values, record7, range) {
  const amps = checkCurrent(values.current);
  if (!amps.ok || range === null || amps.value === record7.amps) {
    return amps;
  }
  return checkCurrentInRange(values.current, range);
}
function settingsSummaries(language, settings, today = null) {
  const energy = settings?.requested_kwh ?? null;
  const amps = settings?.amps ?? null;
  const time = settings?.departure_time ?? null;
  return {
    energy: energy === null || !Number.isFinite(energy) ? translate(language, "settings.energy.unset") : energyAmount(language, energy),
    deadline: settings?.departure_enabled === true && time !== null && WALL_TIME.test(time) ? departureText(language, settings.departure_date, time, today) : translate(language, "settings.deadline.none"),
    current: amps === null || !Number.isFinite(amps) ? translate(language, "settings.current.unset") : `${formatNumber(language, amps, 0)} A`
  };
}
function departureText(language, date, time, today) {
  const day = date === null ? null : departureDayLabel(language, date, today, {
    today: translate(language, "settings.deadline.today"),
    tomorrow: translate(language, "settings.deadline.tomorrow")
  });
  return day === null ? time : `${day} ${time}`;
}
function planSummaryParts(language, settings, today = null) {
  const summaries = settingsSummaries(language, settings, today);
  const first = settings?.driver === SETTINGS_DRIVER_TARGET_SOC ? settings.target.target_percent === null || !Number.isFinite(settings.target.target_percent) ? translate(language, "settings.energy.unset") : percentAmount(language, settings.target.target_percent) : summaries.energy;
  return [first, summaries.deadline, summaries.current];
}
function manualEnergyReadOnly(record7) {
  return record7.driver === SETTINGS_DRIVER_TARGET_SOC;
}
function settingsErrorKey(code) {
  if (code === null) {
    return "settings.error.generic";
  }
  if (code === SETTINGS_NOT_COMMITTED) {
    return "settings.error.notCommitted";
  }
  if (code === SETTINGS_RECONCILE_FAILED) {
    return "settings.error.reconcileFailed";
  }
  if (code === "spotnav_settings_unavailable") {
    return "settings.error.unavailable";
  }
  if (code.startsWith("invalid_")) {
    return "settings.error.invalid";
  }
  if (code === "unauthorized") {
    return "settings.error.readOnly";
  }
  if (code === "spotnav_unsupported_api_version") {
    return "settings.error.version";
  }
  if (code.endsWith("charger") || code === "spotnav_charger_unloaded") {
    return "settings.error.charger";
  }
  return "settings.error.generic";
}
function fiscalRows(language, fiscal) {
  if (fiscal === null) {
    return [];
  }
  const labels = {
    vat: translate(language, "settings.fiscal.vat"),
    tax: translate(language, "settings.fiscal.tax"),
    transfer: translate(language, "settings.fiscal.transfer")
  };
  return ["vat", "tax", "transfer"].map((key) => {
    const component = fiscal[key];
    let value;
    if (component.policy === "off") {
      value = translate(language, "settings.value.off");
    } else if (component.effective === null) {
      value = translate(language, "settings.value.unset");
    } else {
      const figure = formatNumber(language, component.effective, 2);
      value = component.unit === null ? figure : `${figure} ${component.unit}`;
    }
    return { key, label: labels[key], value };
  });
}

// src/validate.ts
var Malformed = class extends Error {
};
function has(source, key) {
  return Object.prototype.hasOwnProperty.call(source, key);
}
function required(source, key) {
  return has(source, key) ? source[key] : bad2();
}
var OFFSET_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/;
var DURATION_TOLERANCE_MS = 1e3;
function bad2() {
  throw new Malformed("malformed");
}
function isRecord2(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function record2(value) {
  return isRecord2(value) ? value : bad2();
}
function text2(source, key) {
  const value = source[key];
  return typeof value === "string" ? value : bad2();
}
function textOrNull2(source, key) {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad2();
}
function numberValue(source, key) {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : bad2();
}
function numberOrNull(source, key) {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad2();
}
function booleanValue2(source, key) {
  const value = source[key];
  return typeof value === "boolean" ? value : bad2();
}
function booleanOrNull(source, key) {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  return typeof value === "boolean" ? value : bad2();
}
function arrayValue(source, key) {
  const value = source[key];
  return Array.isArray(value) ? value : bad2();
}
function stringListOrNull(source, key) {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  if (!Array.isArray(value)) {
    return bad2();
  }
  return value.map((entry) => typeof entry === "string" ? entry : bad2());
}
function sectionOrNull(source, key, decode) {
  const value = required(source, key);
  return value === null ? null : decode(record2(value));
}
function numberList(source, key) {
  return arrayValue(source, key).map(
    (entry) => typeof entry === "number" && Number.isFinite(entry) ? entry : bad2()
  );
}
function instantMs(value) {
  if (typeof value !== "string" || !OFFSET_SUFFIX.test(value)) {
    return bad2();
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : bad2();
}
function instant(source, key) {
  const value = source[key];
  const ms2 = instantMs(value);
  return { text: value, ms: ms2 };
}
function isValidTimeZone(timeZone) {
  try {
    new Intl.DateTimeFormat("en-US", { timeZone });
    return true;
  } catch {
    return false;
  }
}
function exactKeys2(source, keys) {
  presentKeys(source, keys);
  for (const key of Object.keys(source)) {
    if (!keys.includes(key)) {
      bad2();
    }
  }
}
function presentKeys(source, keys) {
  for (const key of keys) {
    required(source, key);
  }
}
function decodeCapabilities(source) {
  return {
    auto_price: booleanValue2(source, "auto_price"),
    current_limit: booleanValue2(source, "current_limit"),
    set_current: booleanValue2(source, "set_current"),
    regulated_current: booleanValue2(source, "regulated_current"),
    target_soc: booleanValue2(source, "target_soc"),
    load_balancing: booleanValue2(source, "load_balancing"),
    refresh_vehicle: booleanValue2(source, "refresh_vehicle"),
    set_charge_limit: booleanValue2(source, "set_charge_limit"),
    target_stop: booleanValue2(source, "target_stop")
  };
}
function decodeCharger(source) {
  return {
    charger_id: text2(source, "charger_id"),
    charger_name: textOrNull2(source, "charger_name"),
    available: booleanValue2(source, "available"),
    capabilities: decodeCapabilities(record2(source.capabilities))
  };
}
function decodeFiscal2(raw) {
  if (!isRecord2(raw)) {
    return null;
  }
  const component = (value) => {
    if (!isRecord2(value) || typeof value["policy"] !== "string") {
      return null;
    }
    const effective = value["effective_value"];
    const unit = value["unit"];
    return {
      policy: value["policy"],
      effective: typeof effective === "number" && Number.isFinite(effective) ? effective : null,
      unit: typeof unit === "string" ? unit : null
    };
  };
  const vat = component(raw["vat"]);
  const tax = component(raw["tax"]);
  const transfer = component(raw["transfer"]);
  return vat === null || tax === null || transfer === null ? null : { vat, tax, transfer };
}
var PLANNING_KEYS = [
  "state",
  "reason",
  "settings_revision",
  "generation",
  "calculated_at",
  "historical",
  "missing",
  "price_state",
  "price_identity",
  "today",
  "tomorrow",
  "execution_state",
  "execution_reason",
  "applied",
  "applied_identity",
  "pending_identity",
  "pending_attempt",
  "execution_paused",
  "price_wait",
  "publication_at",
  "must_buy_now_kwh"
];
function decodePlanning(source) {
  presentKeys(source, PLANNING_KEYS);
  return {
    state: textOrNull2(source, "state"),
    reason: textOrNull2(source, "reason"),
    settings_revision: numberOrNull(source, "settings_revision"),
    price_state: textOrNull2(source, "price_state"),
    execution_state: textOrNull2(source, "execution_state"),
    execution_reason: textOrNull2(source, "execution_reason"),
    execution_paused: booleanOrNull(source, "execution_paused"),
    applied: booleanOrNull(source, "applied"),
    missing: stringListOrNull(source, "missing"),
    today: textOrNull2(source, "today"),
    tomorrow: textOrNull2(source, "tomorrow"),
    price_wait: textOrNull2(source, "price_wait"),
    publication_at: textOrNull2(source, "publication_at"),
    must_buy_now_kwh: numberOrNull(source, "must_buy_now_kwh")
  };
}
var MARKET_KEYS = [
  "catalogue_state",
  "catalogue_fetched_at",
  "catalogue_attempt_error",
  "area_id",
  "area_name",
  "countries",
  "timezone",
  "currency",
  "major_unit",
  "minor_unit",
  "suggested_vat_percent",
  "suggested_tax",
  "suggested_grid_fee"
];
function decodeMarket(source) {
  presentKeys(source, MARKET_KEYS);
  const timezone = textOrNull2(source, "timezone");
  if (timezone !== null && !isValidTimeZone(timezone)) {
    bad2();
  }
  return {
    catalogue_state: textOrNull2(source, "catalogue_state"),
    area_id: textOrNull2(source, "area_id"),
    area_name: textOrNull2(source, "area_name"),
    countries: stringListOrNull(source, "countries"),
    timezone,
    currency: textOrNull2(source, "currency"),
    major_unit: textOrNull2(source, "major_unit"),
    minor_unit: textOrNull2(source, "minor_unit"),
    suggested_vat_percent: numberOrNull(source, "suggested_vat_percent")
  };
}
function decodeInterval(raw, timeZone, previous) {
  const source = record2(raw);
  const start = instant(source, "start");
  const end = instant(source, "end");
  const day = text2(source, "day");
  const duration = numberValue(source, "duration_minutes");
  if (end.ms <= start.ms) {
    bad2();
  }
  if (!(duration > 0)) {
    bad2();
  }
  if (Math.abs(end.ms - start.ms - duration * 6e4) > DURATION_TOLERANCE_MS) {
    bad2();
  }
  if (timeZone !== null && localDayKey(start.ms, timeZone) !== day) {
    bad2();
  }
  if (previous !== null && start.ms <= previous.startMs) {
    bad2();
  }
  if (previous !== null && start.ms < previous.endMs) {
    bad2();
  }
  return {
    start: start.text,
    startMs: start.ms,
    end: end.text,
    endMs: end.ms,
    day,
    duration_minutes: duration,
    raw_price: numberOrNull(source, "raw_price"),
    effective_price: numberOrNull(source, "effective_price"),
    proposal_planned: booleanValue2(source, "proposal_planned"),
    installed_planned: booleanValue2(source, "installed_planned")
  };
}
var PRICES_KEYS = [
  "area_id",
  "state",
  "reason",
  "today",
  "tomorrow",
  "today_state",
  "tomorrow_state",
  "today_source",
  "tomorrow_source",
  "today_fetched_at",
  "tomorrow_fetched_at",
  "today_attempt_at",
  "tomorrow_attempt_at",
  "today_attempt_error",
  "tomorrow_attempt_error",
  "index_state",
  "index_revision",
  "waiting_for_tomorrow",
  "resolution_minutes",
  "resolutions_minutes",
  "priced_slots",
  "unpriced_slots",
  "unpriced",
  "interval_count",
  "intervals"
];
function decodePrices(source, timeZone) {
  presentKeys(source, PRICES_KEYS);
  const intervals = [];
  let previous = null;
  for (const raw of arrayValue(source, "intervals")) {
    const next = decodeInterval(raw, timeZone, previous);
    intervals.push(next);
    previous = next;
  }
  return {
    state: textOrNull2(source, "state"),
    reason: textOrNull2(source, "reason"),
    waiting_for_tomorrow: booleanOrNull(source, "waiting_for_tomorrow"),
    unpriced: booleanOrNull(source, "unpriced"),
    interval_count: numberValue(source, "interval_count"),
    priced_slots: numberOrNull(source, "priced_slots"),
    unpriced_slots: numberOrNull(source, "unpriced_slots"),
    resolution_minutes: numberOrNull(source, "resolution_minutes"),
    resolutions_minutes: numberList(source, "resolutions_minutes"),
    intervals
  };
}
function decodePeriods(source) {
  const periods = [];
  for (const raw of arrayValue(source, "periods")) {
    const entry = record2(raw);
    const start = instant(entry, "start");
    const end = instant(entry, "end");
    if (end.ms <= start.ms) {
      bad2();
    }
    periods.push({ start: start.text, startMs: start.ms, end: end.text, endMs: end.ms });
  }
  return periods.sort((left, right) => left.startMs - right.startMs);
}
function decodeProposal(source) {
  presentKeys(source, [
    "identity",
    "settings_revision",
    "periods",
    "amps",
    "phases",
    "power_kw",
    "requested_kwh",
    "planned_kwh",
    "cost",
    "distance_mil",
    "unpriced",
    "unpriced_slots",
    "priced_slots"
  ]);
  const cost = required(source, "cost");
  return {
    identity: textOrNull2(source, "identity"),
    settings_revision: numberOrNull(source, "settings_revision"),
    periods: decodePeriods(source),
    amps: numberOrNull(source, "amps"),
    phases: numberOrNull(source, "phases"),
    unpriced: booleanValue2(source, "unpriced"),
    unpriced_slots: numberOrNull(source, "unpriced_slots"),
    priced_slots: numberOrNull(source, "priced_slots"),
    planned_kwh: numberOrNull(source, "planned_kwh"),
    requested_kwh: numberOrNull(source, "requested_kwh"),
    cost: cost === null ? null : {
      value: numberValue(record2(cost), "value"),
      currency: textOrNull2(record2(cost), "currency")
    },
    distance_mil: numberOrNull(source, "distance_mil"),
    power_kw: numberOrNull(source, "power_kw")
  };
}
function decodeInstalled(source) {
  return {
    identity: textOrNull2(source, "identity"),
    periods: decodePeriods(source),
    amps: numberOrNull(source, "amps"),
    phases: numberOrNull(source, "phases"),
    power_kw: numberOrNull(source, "power_kw"),
    active_period_index: numberOrNull(source, "active_period_index")
  };
}
function decodeRelation(source) {
  presentKeys(source, ["applied", "applied_identity", "pending_identity"]);
  return {
    applied: booleanOrNull(source, "applied"),
    applied_identity: textOrNull2(source, "applied_identity"),
    pending_identity: textOrNull2(source, "pending_identity")
  };
}
var PLAN_KEYS = [
  "proposal",
  "installed",
  "relation",
  "delivered_kwh",
  "remaining_kwh"
];
function decodePlan(source) {
  presentKeys(source, PLAN_KEYS);
  const proposal = required(source, "proposal");
  const installed = required(source, "installed");
  return {
    proposal: proposal === null ? null : decodeProposal(record2(proposal)),
    installed: installed === null ? null : decodeInstalled(record2(installed)),
    relation: decodeRelation(record2(required(source, "relation"))),
    delivered_kwh: numberOrNull(source, "delivered_kwh"),
    remaining_kwh: numberOrNull(source, "remaining_kwh")
  };
}
function decodeLive(source) {
  return {
    charging: booleanOrNull(source, "charging"),
    schedule_active: booleanOrNull(source, "schedule_active"),
    requested_current_a: numberOrNull(source, "requested_current_a"),
    setpoint_current_a: numberOrNull(source, "setpoint_current_a"),
    measured_current_a: numberOrNull(source, "measured_current_a")
  };
}
var PAUSE_CHOICES2 = ["next_period", "until_tomorrow", "until_resumed"];
var STRATEGIES2 = ["cheapest", "solar", "hybrid"];
var UNAVAILABLE_STRATEGIES = ["solar", "hybrid"];
var STRATEGY_REASONS = [
  "needs_solar_surplus_measurement",
  "needs_solar_and_price_control",
  "needs_total_grid_power"
];
var IMMEDIATE_ACTIONS = [ACTION_START, ACTION_STOP, "none"];
var AUTOMATIC_ACTIONS = [ACTION_PAUSE, ACTION_RESUME, "none"];
var IMMEDIATE_REASONS = ["no_settings", "action_pending"];
var AUTOMATIC_REASONS = [
  "no_settings",
  "pause_unsettled",
  "pause_clear_failed",
  "action_pending"
];
function instantOrNull2(source, key) {
  const value = required(source, key);
  if (value === null) {
    return [null, null];
  }
  if (typeof value !== "string" || !OFFSET_SUFFIX.test(value)) {
    return bad2();
  }
  const milliseconds = Date.parse(value);
  if (Number.isNaN(milliseconds)) {
    return bad2();
  }
  return [value, milliseconds];
}
function enumOrNull(source, key, allowed) {
  const value = required(source, key);
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !allowed.includes(value)) {
    return bad2();
  }
  return value;
}
function decodePause(source) {
  exactKeys2(source, ["choice", "admitted_at", "expires_at"]);
  const choice = enumOrNull(source, "choice", PAUSE_CHOICES2);
  const [admittedAt, admittedAtMs] = instantOrNull2(source, "admitted_at");
  const [expiresAt, expiresAtMs] = instantOrNull2(source, "expires_at");
  if (choice === null) {
    if (admittedAt !== null || expiresAt !== null) {
      return bad2();
    }
    return {
      choice,
      admitted_at: null,
      admitted_at_ms: null,
      expires_at: null,
      expires_at_ms: null
    };
  }
  if (choice === "until_resumed" ? expiresAt !== null : expiresAt === null) {
    return bad2();
  }
  if (admittedAtMs !== null && expiresAtMs !== null && expiresAtMs <= admittedAtMs) {
    return bad2();
  }
  return {
    choice,
    admitted_at: admittedAt,
    admitted_at_ms: admittedAtMs,
    expires_at: expiresAt,
    expires_at_ms: expiresAtMs
  };
}
function decodeStrategyRow(source) {
  exactKeys2(source, ["strategy", "available", "reason"]);
  const strategy = enumOrNull(source, "strategy", STRATEGIES2);
  const available = booleanValue2(source, "available");
  const reason = textOrNull2(source, "reason");
  if (strategy === null) {
    return bad2();
  }
  if (available && reason !== null) {
    return bad2();
  }
  if (!available && (reason === null || !STRATEGY_REASONS.includes(reason) || !UNAVAILABLE_STRATEGIES.includes(strategy))) {
    return bad2();
  }
  return { strategy, available, reason };
}
function decodeStrategy(source) {
  exactKeys2(source, ["selected", "available"]);
  const selected = enumOrNull(source, "selected", STRATEGIES2);
  const rows = arrayValue(source, "available").map((row) => decodeStrategyRow(record2(row)));
  const seen = /* @__PURE__ */ new Set();
  for (const row of rows) {
    if (seen.has(row.strategy)) {
      return bad2();
    }
    seen.add(row.strategy);
  }
  const enabled = rows.filter((row) => row.available).map((row) => row.strategy);
  if (selected === null ? enabled.length !== 0 : !enabled.includes(selected)) {
    return bad2();
  }
  return { selected, rows };
}
function decodeControl(source) {
  exactKeys2(source, [
    "immediate_action",
    "immediate_action_reason",
    "automatic_action",
    "automatic_action_reason",
    "pause_choices",
    "pause",
    "pause_blocks_execution",
    "execution_error",
    "can_act"
  ]);
  const immediate = enumOrNull(source, "immediate_action", IMMEDIATE_ACTIONS);
  const automatic = enumOrNull(source, "automatic_action", AUTOMATIC_ACTIONS);
  if (immediate === null || automatic === null) {
    return bad2();
  }
  const immediateReason = enumOrNull(source, "immediate_action_reason", IMMEDIATE_REASONS);
  const automaticReason = enumOrNull(source, "automatic_action_reason", AUTOMATIC_REASONS);
  if (immediate === "none" ? immediateReason === null : immediateReason !== null) {
    return bad2();
  }
  if (automatic === "none" ? automaticReason === null : automaticReason !== null) {
    return bad2();
  }
  const choices = arrayValue(source, "pause_choices").map((value) => {
    if (typeof value !== "string" || !PAUSE_CHOICES2.includes(value)) {
      return bad2();
    }
    return value;
  });
  if (new Set(choices).size !== choices.length) {
    return bad2();
  }
  if (automatic !== ACTION_PAUSE && choices.length > 0) {
    return bad2();
  }
  const blocks = booleanValue2(source, "pause_blocks_execution");
  const rawPause = required(source, "pause");
  const pause = rawPause === null ? null : decodePause(record2(rawPause));
  if (pause === null) {
    if (immediate !== "none" || immediateReason !== "no_settings" || automatic !== "none" || automaticReason !== "no_settings" || blocks || choices.length > 0) {
      return bad2();
    }
  } else if (blocks !== (pause.choice !== null)) {
    return bad2();
  }
  if (automaticReason === "pause_clear_failed" && (pause === null || pause.choice === null)) {
    return bad2();
  }
  return {
    immediate_action: immediate,
    immediate_action_reason: immediateReason,
    automatic_action: automatic,
    automatic_action_reason: automaticReason,
    pause_choices: choices,
    pause,
    pause_blocks_execution: blocks,
    execution_error: textOrNull2(source, "execution_error"),
    can_act: booleanValue2(source, "can_act")
  };
}
var SOLAR_STRATEGY_STATE_KEYS = [
  "state",
  "reason",
  "available_w",
  "requested_a",
  "priority_effective"
];
var HYBRID_STRATEGY_STATE_KEYS = [
  "state",
  "reason",
  "grid_kwh",
  "credit_kwh",
  "slack_kwh",
  "plan_window_active",
  "forecast_configured"
];
function decodeStrategyState(root, selected) {
  const raw = required(root, "strategy_state");
  if (selected !== "solar" && selected !== "hybrid") {
    return raw === null ? null : bad2();
  }
  if (raw === null) {
    return bad2();
  }
  const value = record2(raw);
  if (selected === "solar") {
    exactKeys2(value, SOLAR_STRATEGY_STATE_KEYS);
    return {
      kind: "solar",
      state: text2(value, "state"),
      reason: textOrNull2(value, "reason"),
      available_w: numberOrNull(value, "available_w"),
      requested_a: numberOrNull(value, "requested_a"),
      priority_effective: textOrNull2(value, "priority_effective")
    };
  }
  exactKeys2(value, HYBRID_STRATEGY_STATE_KEYS);
  return {
    kind: "hybrid",
    state: text2(value, "state"),
    reason: textOrNull2(value, "reason"),
    grid_kwh: numberOrNull(value, "grid_kwh"),
    credit_kwh: numberOrNull(value, "credit_kwh"),
    slack_kwh: numberOrNull(value, "slack_kwh"),
    plan_window_active: booleanValue2(value, "plan_window_active"),
    forecast_configured: booleanValue2(value, "forecast_configured")
  };
}
var SOLAR_PRIORITIES = ["car_first", "battery_first"];
function decodeSolarForecastChoice(source) {
  exactKeys2(source, ["id", "title"]);
  return { id: text2(source, "id"), title: text2(source, "title") };
}
function decodeSite(source) {
  exactKeys2(source, [
    "name",
    "charger_count",
    "solar_priority",
    "solar_forecast",
    "active_control",
    "writable"
  ]);
  const priority = enumOrNull(source, "solar_priority", SOLAR_PRIORITIES);
  if (priority === null) {
    return bad2();
  }
  const forecast = record2(required(source, "solar_forecast"));
  exactKeys2(forecast, ["selected", "choices"]);
  const selectedIds = arrayValue(forecast, "selected").map(
    (entry) => typeof entry === "string" ? entry : bad2()
  );
  const choices = arrayValue(forecast, "choices").map((entry) => decodeSolarForecastChoice(record2(entry)));
  const choiceIds = new Set(choices.map((choice) => choice.id));
  if (new Set(choiceIds).size !== choices.length || selectedIds.some((id) => !choiceIds.has(id))) {
    return bad2();
  }
  const activeControl = record2(required(source, "active_control"));
  exactKeys2(activeControl, ["available", "enabled", "reason", "writable"]);
  const available = booleanValue2(activeControl, "available");
  const reason = textOrNull2(activeControl, "reason");
  if (available && reason !== null) {
    return bad2();
  }
  return {
    name: text2(source, "name"),
    charger_count: numberValue(source, "charger_count"),
    solar_priority: priority,
    solar_forecast: { selected: selectedIds, choices },
    active_control: {
      available,
      enabled: booleanValue2(activeControl, "enabled"),
      reason,
      writable: booleanValue2(activeControl, "writable")
    },
    writable: booleanValue2(source, "writable")
  };
}
var STATUS_TONES = ["normal", "notice", "blocking"];
var STATUS_CODE_TABLE = {
  charger_unavailable: ["blocking", { problem: "textOrNull", entity: "textOrNull" }],
  price_data_invalid: ["blocking", { reason: "textOrNull" }],
  price_data_unavailable: ["blocking", { reason: "textOrNull" }],
  settings_incomplete: ["notice", { reason: "textOrNull", missing: "codes" }],
  solar_unavailable: ["blocking", {}],
  target_soc_unknown: ["blocking", { missing: "codes" }],
  price_horizon_missing: ["blocking", {}],
  planning_unavailable: ["blocking", { reason: "textOrNull" }],
  planning_error: ["blocking", { reason: "textOrNull" }],
  paused: ["normal", { until: "instantOrNull", choice: "choiceOrNull" }],
  charging_now: ["normal", { until: "instantOrNull" }],
  charging_without_prices: ["notice", {}],
  waiting_for_publication: ["normal", { publication_at: "instantOrNull" }],
  waiting_for_history: ["normal", { weekday: "int", percent: "int", weeks: "int" }],
  buying_before_publication: ["normal", { kwh: "number" }],
  auto_planned: ["normal", { start: "instant" }],
  auto_installed: ["normal", { start: "instant" }],
  proposal_pending: ["normal", { installs_at: "instantOrNull", waits_for: "textOrNull" }],
  waiting_for_tomorrow: ["normal", {}],
  no_plan: ["normal", {}],
  nothing_to_charge: ["normal", {}],
  plan_energy: ["normal", { kwh: "number" }],
  plan_cost: ["normal", { amount_minor: "int", currency: "text" }],
  plan_distance: ["normal", { mil: "number" }],
  solar_charging: ["normal", { requested_a: "numberOrNull" }],
  solar_arming: ["normal", {}],
  solar_disarming: ["normal", {}],
  solar_no_reading_stopped: ["normal", {}],
  solar_no_reading_waiting: ["normal", {}],
  solar_waiting_for_sun: ["normal", {}],
  solar_unknown: ["normal", {}],
  hybrid_grid: [
    "normal",
    { grid_kwh: "number", credit_kwh: "numberOrNull", window_start: "instantOrNull", window_end: "instantOrNull" }
  ],
  hybrid_no_forecast: ["normal", {}],
  hybrid_no_price_data: ["normal", {}],
  hybrid_satisfied: ["normal", {}],
  hybrid_unknown: ["normal", {}],
  settings_suggested: ["normal", { fields: "codes" }],
  target_reached: ["normal", { soc_percent: "number", basis: "text", reading_age_s: "numberOrNull" }],
  target_unverifiable: ["notice", { reason: "text" }],
  price_data_stale: ["notice", { reason: "textOrNull" }],
  price_data_degraded: ["notice", { reason: "textOrNull" }],
  unpriced: ["notice", {}],
  load_balancing_limited: ["notice", { limit_a: "numberOrNull", phase: "textOrNull", cause: "textOrNull" }],
  load_balancing_unavailable: ["notice", {}],
  held_by_charger: ["notice", {}],
  charger_disabled: ["notice", {}],
  held_until_window: ["normal", { time: "instant" }],
  hold_overridden: ["notice", {}],
  site_measurement_problem: [
    "notice",
    { no_value_phases: "codes", no_value_entities: "codes", stale_phases: "codes", max_age_s: "numberOrNull" }
  ],
  duplicate_charger: ["notice", { other: "text" }]
};
function decodeStatusParam(source, key, kind) {
  switch (kind) {
    case "text":
      return text2(source, key);
    case "textOrNull":
      return textOrNull2(source, key);
    case "instant": {
      const [iso] = instantOrNull2(source, key);
      return iso ?? bad2();
    }
    case "instantOrNull":
      return instantOrNull2(source, key)[0];
    case "number":
      return numberValue(source, key);
    case "numberOrNull":
      return numberOrNull(source, key);
    case "int": {
      const value = numberValue(source, key);
      return Number.isInteger(value) ? value : bad2();
    }
    case "choiceOrNull":
      return enumOrNull(source, key, PAUSE_CHOICES2);
    case "codes":
      return arrayValue(source, key).map((entry) => typeof entry === "string" ? entry : bad2());
  }
}
function decodeStatus(source) {
  exactKeys2(source, ["tone", "lines"]);
  const tone = text2(source, "tone");
  if (!STATUS_TONES.includes(tone)) {
    return bad2();
  }
  let blocking = false;
  const lines = arrayValue(source, "lines").map((raw) => {
    const line = record2(raw);
    exactKeys2(line, ["code", "params"]);
    const code = text2(line, "code");
    if (!Object.prototype.hasOwnProperty.call(STATUS_CODE_TABLE, code)) {
      return bad2();
    }
    const [lineTone, kinds] = STATUS_CODE_TABLE[code];
    const params = record2(line.params);
    exactKeys2(params, Object.keys(kinds));
    blocking ||= lineTone === "blocking";
    return {
      code,
      params: Object.fromEntries(
        Object.entries(kinds).map(([key, kind]) => [key, decodeStatusParam(params, key, kind)])
      )
    };
  });
  if (blocking !== (tone === "blocking")) {
    return bad2();
  }
  return { tone, lines };
}
function decodeDashboard(raw) {
  try {
    const root = isRecord2(raw) ? raw : bad2();
    const version = root.api_version;
    if (typeof version !== "number" || !Number.isFinite(version)) {
      return { ok: false, failure: "malformed" };
    }
    if (version !== API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys2(
      Object.fromEntries(Object.entries(root).filter(([key]) => !OPTIONAL_DASHBOARD_KEYS.includes(key))),
      DASHBOARD_KEYS
    );
    const market = sectionOrNull(root, "market", decodeMarket);
    const timeZone = market?.timezone ?? null;
    const strategy = decodeStrategy(record2(required(root, "strategy")));
    const settings = required(root, "settings");
    return {
      ok: true,
      value: {
        api_version: 1,
        generated_at: textOrNull2(root, "generated_at"),
        charger: decodeCharger(record2(root.charger)),
        settings: settings === null ? null : decodeSettingsRecord(settings),
        fiscal: decodeFiscal2(root["fiscal"]),
        planning: sectionOrNull(root, "planning", decodePlanning),
        market,
        prices: decodePrices(record2(required(root, "prices")), timeZone),
        plan: decodePlan(record2(required(root, "plan"))),
        live: decodeLive(record2(required(root, "live"))),
        strategy,
        strategy_options: strategyOptions(root),
        strategy_state: decodeStrategyState(root, strategy.selected),
        control: decodeControl(record2(required(root, "control"))),
        charge_progress: progressOrNull(root),
        site: sectionOrNull(root, "site", decodeSite),
        current_range: currentRangeOrNull(root),
        soc: socOrNull(root),
        vehicles: arrayValue(root, "vehicles").map(decodeVehicle),
        target_vehicle_id: textOrNull2(root, "target_vehicle_id"),
        detected_phases: detectedPhases(root),
        phase_detection: decodePhaseDetection(record2(required(root, "phase_detection"))),
        chargers: arrayValue(root, "chargers").map((entry) => {
          const item = record2(entry);
          exactKeys2(item, ["id", "name"]);
          return { id: text2(item, "id"), name: text2(item, "name") };
        }),
        status: decodeStatus(record2(required(root, "status")))
      }
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}
var DASHBOARD_KEYS = [
  "api_version",
  "generated_at",
  "charger",
  "settings",
  "fiscal",
  "planning",
  "market",
  "prices",
  "plan",
  "live",
  "strategy",
  "strategy_options",
  "strategy_state",
  "control",
  "charge_progress",
  "site",
  "current_range",
  "soc",
  "vehicles",
  "target_vehicle_id",
  "detected_phases",
  "phase_detection",
  "chargers",
  "status",
  // The setup in words, for a client other than this card (which builds the same lines from the
  // entity configuration); accepted and not read.
  "summary"
];
var OPTIONAL_DASHBOARD_KEYS = ["sessions_summary"];
function strategyOptions(root) {
  const options = arrayValue(root, "strategy_options").map(
    (entry) => typeof entry === "string" && STRATEGIES2.includes(entry) ? entry : bad2()
  );
  return new Set(options).size === options.length ? options : bad2();
}
function detectedPhases(root) {
  const value = numberOrNull(root, "detected_phases");
  return value === null || value === 1 || value === 3 ? value : bad2();
}
function decodePhaseDetection(source) {
  exactKeys2(source, ["source", "confidence"]);
  return { source: text2(source, "source"), confidence: text2(source, "confidence") };
}
function currentRangeOrNull(root) {
  const value = root.current_range;
  if (!isRecord2(value)) {
    return null;
  }
  try {
    exactKeys2(value, ["min_a", "max_a", "source"]);
    const min = value.min_a;
    const max = value.max_a;
    if (typeof min !== "number" || typeof max !== "number" || !Number.isInteger(min) || !Number.isInteger(max) || min < 1 || max > 80 || max < min) {
      return null;
    }
    return { min_a: min, max_a: max, source: text2(value, "source") };
  } catch {
    return null;
  }
}
var SOC_MISSING = ["soc", "capacity", "vehicle"];
var SOC_SOURCES = ["charger", "vehicle"];
function boundedOrNull(source, key, minimum, maximum, exclusiveMinimum = false) {
  const value = numberOrNull(source, key);
  if (value === null) {
    return null;
  }
  return value > maximum || value < minimum || exclusiveMinimum && value === minimum ? bad2() : value;
}
function socOrNull(root) {
  if (root.soc === null) {
    return null;
  }
  const source = record2(root.soc);
  exactKeys2(source, [
    "value",
    "age_s",
    "source",
    "estimated",
    "target_percent",
    "need_kwh",
    "capacity_kwh",
    "vehicle_name",
    "vehicle_id",
    "vehicles",
    "vehicle_max_percent",
    "efficiency",
    "missing"
  ]);
  const missing = arrayValue(source, "missing").map(
    (entry) => SOC_MISSING.includes(entry) ? entry : bad2()
  );
  if (new Set(missing).size !== missing.length) {
    return bad2();
  }
  return {
    value: boundedOrNull(source, "value", 0, 100),
    age_s: boundedOrNull(source, "age_s", 0, Number.POSITIVE_INFINITY),
    source: enumOrNull(source, "source", SOC_SOURCES),
    estimated: booleanValue2(source, "estimated"),
    target_percent: boundedOrNull(source, "target_percent", 0, 100),
    need_kwh: boundedOrNull(source, "need_kwh", 0, Number.POSITIVE_INFINITY),
    capacity_kwh: boundedOrNull(source, "capacity_kwh", 0, Number.POSITIVE_INFINITY, true),
    vehicle_name: textOrNull2(source, "vehicle_name"),
    vehicle_id: textOrNull2(source, "vehicle_id"),
    vehicles: arrayValue(source, "vehicles").map((entry) => {
      const item = record2(entry);
      exactKeys2(item, ["id", "name"]);
      return { id: text2(item, "id"), name: text2(item, "name") };
    }),
    vehicle_max_percent: boundedOrNull(source, "vehicle_max_percent", 0, 100),
    efficiency: boundedOrNull(source, "efficiency", 0, 1, true) ?? bad2(),
    missing
  };
}
var CAPACITY_SOURCES = ["reported", "stored"];
function decodeVehicle(raw) {
  const source = record2(raw);
  exactKeys2(source, [
    "id",
    "name",
    "soc_entity_id",
    "capacity_kwh",
    "capacity_source",
    "consumption_kwh_per_10km",
    "max_percent",
    "soc_percent"
  ]);
  const capacity = boundedOrNull(source, "capacity_kwh", 0, Number.POSITIVE_INFINITY, true);
  const origin = enumOrNull(source, "capacity_source", CAPACITY_SOURCES);
  if (capacity === null !== (origin === null)) {
    return bad2();
  }
  return {
    id: text2(source, "id"),
    name: textOrNull2(source, "name"),
    soc_entity_id: textOrNull2(source, "soc_entity_id"),
    capacity_kwh: capacity,
    capacity_source: origin,
    consumption_kwh_per_10km: boundedOrNull(source, "consumption_kwh_per_10km", 0, Number.POSITIVE_INFINITY, true),
    max_percent: boundedOrNull(source, "max_percent", 0, 100),
    soc_percent: boundedOrNull(source, "soc_percent", 0, 100)
  };
}
function progressOrNull(root) {
  const value = root.charge_progress;
  if (!isRecord2(value)) {
    return null;
  }
  try {
    exactKeys2(value, ["state", "reason", "since"]);
    const state = text2(value, "state");
    if (!CHARGE_PROGRESS_STATES.includes(state)) {
      return null;
    }
    return {
      state,
      reason: text2(value, "reason"),
      since: textOrNull2(value, "since")
    };
  } catch {
    return null;
  }
}

// src/status.ts
function issueText(language, issue) {
  return issue.text ?? translate(language, issue.textKey, issue.params);
}
function listOf(language, items) {
  return new Intl.ListFormat(language, { style: "long", type: "conjunction" }).format(items);
}
function measurementProblemText(language, facts) {
  const parts = [];
  if (facts.noValuePhases.length > 0) {
    const where = facts.noValueEntities.length > 0 ? ` (${facts.noValueEntities.join(", ")})` : "";
    const key = `status.siteMeasurement.noValue.${pluralForm(language, facts.noValuePhases.length)}`;
    parts.push(translate(language, key, { phases: listOf(language, facts.noValuePhases), where }));
  }
  if (facts.stalePhases.length > 0) {
    const key = `status.siteMeasurement.stale.${pluralForm(language, facts.stalePhases.length)}`;
    parts.push(
      translate(language, key, {
        phases: listOf(language, facts.stalePhases),
        seconds: formatNumber(language, facts.maxAgeS ?? 0, 0)
      })
    );
  }
  return parts.length === 0 ? translate(language, "issue.siteMeasurement") : parts.join(" ");
}
function strings(value) {
  return Array.isArray(value) ? value.filter((entry) => typeof entry === "string") : [];
}
function measurementLineText(language, p) {
  return measurementProblemText(language, {
    noValuePhases: strings(p["no_value_phases"]),
    noValueEntities: strings(p["no_value_entities"]),
    stalePhases: strings(p["stale_phases"]),
    maxAgeS: num(p["max_age_s"])
  });
}
var STATUS_WORDING = {
  charger_unavailable: "issue.chargerMissing",
  price_data_invalid: "issue.priceInvalid",
  price_data_unavailable: "issue.priceUnavailable",
  settings_incomplete: "issue.incompleteSettings",
  solar_unavailable: "issue.solarUnavailable",
  target_soc_unknown: "issue.targetSocUnknown",
  price_horizon_missing: "issue.priceHorizon",
  planning_unavailable: "issue.planningUnavailable",
  planning_error: "issue.error",
  paused: "control.pausedUntil",
  charging_now: "status.chargingNow",
  charging_without_prices: "issue.chargingWithoutPrices",
  waiting_for_publication: "status.waitingForPublication",
  waiting_for_history: "status.waitingForHistory",
  buying_before_publication: "status.buyingBeforePublication",
  auto_planned: "status.autoPlanned",
  auto_installed: "status.autoInstalled",
  proposal_pending: "status.proposalPending",
  waiting_for_tomorrow: "status.waitingForTomorrow",
  no_plan: "status.noPlan",
  nothing_to_charge: "status.nothingToCharge",
  plan_energy: "status.planEnergy",
  plan_cost: "status.planCost",
  plan_distance: "status.planDistance",
  solar_charging: "strategy.status.solar.charging",
  solar_arming: "strategy.status.solar.arming",
  solar_disarming: "strategy.status.solar.disarming",
  solar_no_reading_stopped: "strategy.status.solar.noReadingStopped",
  solar_no_reading_waiting: "strategy.status.solar.noReadingWaiting",
  solar_waiting_for_sun: "strategy.status.solar.waitingForSun",
  solar_unknown: "strategy.status.solar.unknown",
  hybrid_grid: "strategy.status.hybrid.grid",
  hybrid_no_forecast: "strategy.status.hybrid.noForecast",
  hybrid_no_price_data: "strategy.status.hybrid.noPriceData",
  hybrid_satisfied: "strategy.status.hybrid.satisfied",
  hybrid_unknown: "strategy.status.hybrid.unknown",
  settings_suggested: "status.settingsSuggested",
  target_reached: "status.targetStopped",
  target_unverifiable: "issue.targetUnverifiable",
  price_data_stale: "issue.priceStale",
  price_data_degraded: "issue.priceDegraded",
  unpriced: "issue.unpriced",
  load_balancing_limited: "status.loadBalancingLimitedTo",
  load_balancing_unavailable: "issue.loadBalancing",
  held_by_charger: "issue.heldByCharger",
  charger_disabled: "issue.chargerDisabled",
  held_until_window: "status.heldUntilWindow",
  hold_overridden: "issue.holdOverridden",
  site_measurement_problem: "issue.siteMeasurement",
  duplicate_charger: "issue.duplicateCharger"
};
var MISSING_FIELD_KEYS = {
  area: "status.missing.area",
  phases: "status.missing.phases",
  amps: "status.missing.amps",
  vehicle: "status.missing.vehicle",
  target_percent: "status.missing.target_percent"
};
function chargerProblemKey(problem) {
  if (problem === "control_missing") {
    return "issue.chargeControlMissing";
  }
  return problem === "control_disabled" ? "issue.chargeControlDisabled" : null;
}
function loadBalancingLimitKey(cause) {
  if (cause === "battery_shares_fuse") {
    return "status.loadBalancingLimitedByBattery";
  }
  if (cause === "house_consumption") {
    return "status.loadBalancingLimitedByHouse";
  }
  return "status.loadBalancingLimitedTo";
}
function ms(value) {
  if (typeof value !== "string") {
    return null;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}
function num(value) {
  return typeof value === "number" ? value : null;
}
function moment(format, instantMs2, nowMs) {
  const time = clock(format, instantMs2);
  if (localDayKey(instantMs2, format.timeZone) === localDayKey(nowMs, format.timeZone)) {
    return time;
  }
  return `${weekdayDate(format, instantMs2)} ${time}`;
}
function lineText(line, format, nowMs) {
  const language = format.language;
  const say = (key, params = {}) => translate(language, key, params);
  const zoned = hasZone(format);
  const p = line.params;
  switch (line.code) {
    case "paused": {
      const until = ms(p["until"]);
      if (until === null) {
        return say("control.pausedIndefinitely");
      }
      return zoned ? say("control.pausedUntil", { time: moment(format, until, nowMs) }) : say("status.pausedShort");
    }
    case "charging_now": {
      const until = ms(p["until"]);
      return until === null || !zoned ? say("status.chargingNowOpen") : say("status.chargingNow", { time: clock(format, until) });
    }
    case "waiting_for_publication": {
      const at = ms(p["publication_at"]);
      return at === null || !zoned ? say("status.waitingForPublicationNoTime") : say("status.waitingForPublication", { time: clock(format, at) });
    }
    case "waiting_for_history": {
      const weekday = weekdayPlural(language, num(p["weekday"]) ?? Number.NaN);
      const percent2 = num(p["percent"]);
      const weeks = num(p["weeks"]);
      return weekday === null || percent2 === null || weeks === null ? say("status.waitingForHistoryNoDetail") : say("status.waitingForHistory", {
        weekday,
        percent: formatNumber(language, percent2, 0),
        weeks: formatNumber(language, weeks, 0)
      });
    }
    case "buying_before_publication":
      return say("status.buyingBeforePublication", { kwh: formatNumber(language, num(p["kwh"]) ?? 0, 1) });
    case "auto_planned":
    case "auto_installed": {
      const start = ms(p["start"]);
      return start === null || !zoned ? say("status.scheduledNoTime") : say(STATUS_WORDING[line.code], { time: clock(format, start) });
    }
    case "proposal_pending": {
      const at = ms(p["installs_at"]);
      return at === null || !zoned ? say("status.proposalPending") : say("status.proposalPendingAt", { time: moment(format, at, nowMs) });
    }
    case "held_until_window": {
      const time = ms(p["time"]);
      return time === null || !zoned ? say("status.scheduledNoTime") : say("status.heldUntilWindow", { time: clock(format, time) });
    }
    case "plan_energy":
      return say("status.planEnergy", { kwh: formatNumber(language, num(p["kwh"]) ?? 0, 1) });
    case "plan_cost": {
      const major = (num(p["amount_minor"]) ?? 0) / 100;
      const currency = typeof p["currency"] === "string" ? p["currency"] : "";
      const unit = currency === format.currency && format.majorUnit !== null ? format.majorUnit : currency;
      return say("status.planCost", { cost: `${formatNumber(language, major, 2)} ${unit}`.trim() });
    }
    case "plan_distance":
      return say("status.planDistance", { distance: distanceText(language, num(p["mil"]) ?? 0) });
    case "solar_charging": {
      const amps = num(p["requested_a"]);
      return amps === null ? say("strategy.status.solar.chargingUnknown") : say("strategy.status.solar.charging", { amps: formatNumber(language, amps, 0) });
    }
    case "hybrid_grid": {
      const start = ms(p["window_start"]);
      const end = ms(p["window_end"]);
      const window2 = start !== null && end !== null && zoned ? ` ${clock(format, start)}–${clock(format, end)}` : "";
      const grid = say("strategy.status.hybrid.grid", {
        grid: formatNumber(language, num(p["grid_kwh"]) ?? 0, 1),
        window: window2
      });
      const credit = num(p["credit_kwh"]);
      return credit !== null && credit > 0 ? `${grid}${say("strategy.status.hybrid.creditSuffix", { credit: formatNumber(language, credit, 1) })}` : grid;
    }
    case "load_balancing_limited": {
      const limit = num(p["limit_a"]);
      return limit === null ? say("status.loadBalancingLimited") : say(loadBalancingLimitKey(p["cause"]), { limit: formatNumber(language, limit, 0) });
    }
    case "settings_incomplete": {
      const missing = Array.isArray(p["missing"]) ? p["missing"] : [];
      const names = missing.map((field2) => MISSING_FIELD_KEYS[field2]).filter((key) => key !== void 0);
      if (names.length === 0 || names.length !== missing.length) {
        return say("issue.incompleteSettings");
      }
      return missing.length === 1 && missing[0] === "area" ? say("status.finishSetupArea") : say("status.finishSetup", { fields: names.map((key) => say(key)).join(", ") });
    }
    case "target_reached": {
      const soc = formatNumber(language, num(p["soc_percent"]) ?? 0, 0);
      const estimated = p["basis"] === "estimate";
      const ageS = num(p["reading_age_s"]);
      const age = ageS === null || ageS < 60 ? null : ageS < 3600 ? say("status.targetAgeMinutes", { n: formatNumber(language, Math.floor(ageS / 60), 0) }) : say("status.targetAgeHours", { n: formatNumber(language, Math.floor(ageS / 3600), 0) });
      if (age === null) {
        if (estimated) {
          return say("status.targetStoppedEstimate", { soc });
        }
        return ageS === null ? say("status.targetStopped", { soc }) : say("status.targetStoppedNow", { soc });
      }
      return say(estimated ? "status.targetStoppedEstimateAge" : "status.targetStoppedAge", { soc, age });
    }
    case "site_measurement_problem":
      return measurementLineText(language, p);
    case "duplicate_charger":
      return say("issue.duplicateCharger", { other: typeof p["other"] === "string" ? p["other"] : "" });
    case "charger_unavailable": {
      const key = chargerProblemKey(p["problem"]);
      return key === null ? say("issue.chargerMissing") : say(key, { entity: typeof p["entity"] === "string" ? p["entity"] : "" });
    }
    default:
      return say(STATUS_WORDING[line.code]);
  }
}
var NOTE_CODE = "settings_suggested";
function statusText(status, format, nowMs) {
  if (status === null) {
    return null;
  }
  const lines = status.lines.filter((line) => line.code !== NOTE_CODE);
  if (lines.length === 0) {
    return null;
  }
  const parts = lines.map((line) => lineText(line, format, nowMs));
  return parts.map((part, index) => index < parts.length - 1 ? part.replace(/[.。]$/u, "") : part).join(" · ");
}
function statusNote(status, format, nowMs) {
  const line = status?.lines.find((entry) => entry.code === NOTE_CODE);
  return line === void 0 ? null : lineText(line, format, nowMs);
}
function reasonOf(line) {
  const reason = line.params["reason"];
  return typeof reason === "string" && reason !== "" ? reason : null;
}
function issuesOf(status, language) {
  if (status === null) {
    return [];
  }
  const issues = [];
  for (const line of status.lines) {
    const severity = STATUS_CODE_TABLE[line.code][0];
    if (severity === "normal") {
      continue;
    }
    if (line.code === "site_measurement_problem") {
      issues.push({
        code: line.code,
        severity,
        textKey: STATUS_WORDING[line.code],
        params: {},
        technical: null,
        text: measurementLineText(language, line.params)
      });
      continue;
    }
    if (line.code === "duplicate_charger") {
      const other = typeof line.params["other"] === "string" ? line.params["other"] : "";
      issues.push({ code: line.code, severity, textKey: STATUS_WORDING[line.code], params: { other }, technical: null });
      continue;
    }
    if (line.code === "charger_unavailable") {
      const key = chargerProblemKey(line.params["problem"]);
      const entity = typeof line.params["entity"] === "string" ? line.params["entity"] : "";
      issues.push({ code: line.code, severity, textKey: key ?? STATUS_WORDING[line.code], params: key === null ? {} : { entity }, technical: null });
      continue;
    }
    const limit = line.code === "load_balancing_limited" ? num(line.params["limit_a"]) : null;
    issues.push({
      code: line.code,
      severity,
      textKey: line.code === "load_balancing_limited" ? limit === null ? "status.loadBalancingLimited" : loadBalancingLimitKey(line.params["cause"]) : STATUS_WORDING[line.code],
      params: limit === null ? {} : { limit: formatNumber(language, limit, 0) },
      technical: reasonOf(line)
    });
    if (line.code === "settings_incomplete") {
      const missing = line.params["missing"];
      if (Array.isArray(missing) && missing.length > 0) {
        issues.push({
          code: "missing_settings",
          severity,
          textKey: "issue.missingSettings",
          params: { fields: missing.join(", ") },
          technical: null
        });
      }
    }
  }
  return issues;
}

// src/model.ts
function planRelationOf(dashboard) {
  const proposal = dashboard.plan.proposal;
  const installed = dashboard.plan.installed;
  const hasProposal = proposal !== null && proposal.periods.length > 0;
  const hasInstalled = installed !== null && installed.periods.length > 0;
  if (!hasProposal && !hasInstalled) {
    return "none";
  }
  if (hasProposal && !hasInstalled) {
    return "proposal_only";
  }
  if (!hasProposal && hasInstalled) {
    return "installed_only";
  }
  return dashboard.plan.relation.applied === true ? "applied_same" : "pending_beside_installed";
}
function capabilitiesFor(dashboard) {
  const capabilities = dashboard.charger.capabilities;
  const item = (key, labelKey, available = capabilities[key]) => {
    if (available) {
      return { key, labelKey, state: "available" };
    }
    return { key, labelKey, state: "unavailable" };
  };
  return [
    item("auto_price", "cap.autoPrice"),
    // "Dynamic current limit" is what SpotNav can do to this charger, not whether it has a limit
    // entity: a charger whose current SpotNav may not or cannot set is unavailable here.
    item("current_limit", "cap.currentLimit", capabilities.set_current),
    item("load_balancing", "cap.loadBalancing"),
    item("target_soc", "cap.targetSoc")
  ];
}
function toPeriods(periods, format, activeIndex) {
  return periods.map((period, index) => ({
    startMs: period.startMs,
    endMs: period.endMs,
    label: periodLabel(format, period.startMs, period.endMs),
    activeNow: activeIndex !== null && index === activeIndex,
    ambiguous: periodIsAmbiguous(format, period.startMs, period.endMs)
  }));
}
function figuresFor(dashboard, format) {
  const proposal = dashboard.plan.proposal;
  if (proposal === null) {
    return { cost: null, energy: null, distance: null, power: null };
  }
  const plannedKwh = proposal.planned_kwh ?? proposal.requested_kwh;
  return {
    cost: proposal.cost === null ? null : money(format, proposal.cost.value),
    // A planned amount is the planner's estimate, stated to a tenth; `energyAmount` is for amounts a record
    // holds, not for this.
    energy: plannedKwh === null ? null : `${formatNumber(format.language, plannedKwh, 1)} kWh`,
    distance: proposal.distance_mil === null ? null : distanceText(format.language, proposal.distance_mil),
    power: proposal.power_kw === null ? null : `${formatNumber(format.language, proposal.power_kw, 1)} kW`
  };
}
var IMMEDIATE_LABELS = {
  start: "action.start",
  stop: "action.stop"
};
var AUTOMATIC_LABELS = {
  pause: "action.pauseAutomatic",
  resume: "action.resume"
};
var REASON_KEYS = {
  no_settings: "control.noSettings",
  pause_unsettled: "control.pauseUnsettled",
  pause_clear_failed: "control.pauseClearFailed",
  action_pending: "control.actionPending"
};
var EXECUTION_ERROR_KEYS = {
  pause_clear_failed: "control.pauseClearFailed",
  pause_stop_failed: "control.pauseStopFailed",
  action_failed: "control.startNotAcknowledged",
  reconcile_failed: "control.reconcileFailed"
};
var CHOICE_KEYS = {
  next_period: "pause.nextPeriod",
  until_tomorrow: "pause.untilTomorrow",
  until_resumed: "pause.untilResumed"
};
var STRATEGY_KEYS = {
  cheapest: "strategy.cheapest",
  solar: "strategy.solar",
  hybrid: "strategy.hybrid"
};
var STRATEGY_REASON_KEYS = {
  needs_solar_surplus_measurement: "strategy.reason.solar",
  needs_solar_and_price_control: "strategy.reason.hybrid",
  needs_total_grid_power: "strategy.reason.totalPower"
};
function controlNotice(language, reason, executionError) {
  if (reason !== null) {
    const key = REASON_KEYS[reason];
    return { text: translate(language, key ?? "issue.unknown"), code: key === void 0 ? reason : null };
  }
  if (executionError === null) {
    return { text: null, code: null };
  }
  const known = EXECUTION_ERROR_KEYS[executionError];
  if (known !== void 0) {
    return { text: translate(language, known), code: null };
  }
  return { text: translate(language, "control.executionError"), code: executionError };
}
function axisFactsFor(language, action, reasonCode, labels) {
  if (action !== "none") {
    return { action, labelKey: labels[action] ?? null, reason: null, reasonCode: null, pending: false };
  }
  const key = reasonCode === null ? void 0 : REASON_KEYS[reasonCode];
  return {
    action,
    labelKey: null,
    reason: translate(language, key ?? "issue.unknown"),
    // A reason with a sentence is not repeated as a code; the code is technical detail only when no sentence
    // exists.
    reasonCode: key === void 0 ? reasonCode : null,
    pending: reasonCode === "action_pending"
  };
}
function controlFactsFor(dashboard, language) {
  const control = dashboard.control;
  const reason = control.immediate_action_reason ?? control.automatic_action_reason;
  const notice = controlNotice(language, reason, control.execution_error);
  return {
    immediate: axisFactsFor(
      language,
      control.immediate_action,
      control.immediate_action_reason,
      IMMEDIATE_LABELS
    ),
    automatic: axisFactsFor(
      language,
      control.automatic_action,
      control.automatic_action_reason,
      AUTOMATIC_LABELS
    ),
    choices: control.pause_choices.map((id) => ({
      id,
      labelKey: CHOICE_KEYS[id] ?? "pause.untilResumed"
    })),
    notice: notice.text,
    noticeCode: notice.code,
    canAct: control.can_act
  };
}
function advisoryFor(dashboard, language) {
  if (dashboard.charge_progress === null) {
    return null;
  }
  const progress = dashboard.charge_progress;
  if (progress.state !== CHARGE_PROGRESS_VEHICLE_NOT_REQUESTING_CURRENT) {
    return null;
  }
  return {
    // A charger behind a smart plug is judged by its power; a connector status says it differently.
    text: translate(
      language,
      progress.reason === "power_below_threshold" ? "advisory.powerBelowThreshold" : "advisory.vehicleNotRequestingCurrent"
    ),
    code: progress.reason
  };
}
function strategyFactsFor(dashboard, language) {
  const strategy = dashboard.strategy;
  return {
    selected: strategy.selected === null ? null : translate(language, STRATEGY_KEYS[strategy.selected] ?? "strategy.cheapest"),
    selectedId: strategy.selected,
    rows: strategy.rows.map((row) => ({
      id: row.strategy,
      labelKey: STRATEGY_KEYS[row.strategy] ?? "strategy.cheapest",
      available: row.available,
      reason: row.reason === null ? null : translate(language, STRATEGY_REASON_KEYS[row.reason] ?? "issue.unknown"),
      reasonCode: row.reason
    }))
  };
}
var ACTIVE_CONTROL_REASON_KEYS = {
  duplicate_membership: "site.activeControl.reason.duplicateMembership",
  no_commandable_charger: "site.activeControl.reason.noCommandableCharger"
};
function activeControlReasonText(language, reason) {
  if (reason === null) {
    return null;
  }
  const known = ACTIVE_CONTROL_REASON_KEYS[reason];
  if (known !== void 0) {
    return { text: translate(language, known), code: null };
  }
  if (reason.startsWith("site_measurement_")) {
    return { text: translate(language, "site.activeControl.reason.measurement"), code: reason };
  }
  return { text: translate(language, "issue.unknown"), code: reason };
}
var DEFAULT_CURRENT_RANGE = { minA: 6, maxA: 32 };
function currentRangeFor(dashboard) {
  const range = dashboard.current_range;
  return range === null ? DEFAULT_CURRENT_RANGE : { minA: range.min_a, maxA: range.max_a };
}
function socFor(dashboard) {
  return dashboard.soc;
}
function vehiclesFor(dashboard) {
  return dashboard.vehicles;
}
function targetVehicleIdFor(dashboard) {
  return dashboard.target_vehicle_id;
}
function siteFactsFor(site, language) {
  if (site === null) {
    return null;
  }
  const count = site.charger_count;
  const key = pluralForm(language, count) === "one" ? "site.applies.one" : "site.applies.other";
  const selected = new Set(site.solar_forecast.selected);
  return {
    name: site.name,
    chargerCount: count,
    appliesToText: translate(language, key, { count: String(count) }),
    solarPriority: site.solar_priority,
    solarForecastChoices: site.solar_forecast.choices.map((choice) => ({
      id: choice.id,
      title: choice.title,
      selected: selected.has(choice.id)
    })),
    solarForecastNone: site.solar_forecast.selected.length === 0,
    activeControlAvailable: site.active_control.available,
    activeControlWritable: site.active_control.writable,
    activeControlEnabled: site.active_control.enabled,
    activeControlReason: activeControlReasonText(language, site.active_control.reason),
    writable: site.writable
  };
}
function admittedAction(control, action, choice) {
  if (action === ACTION_START) {
    return choice === null && control.immediate_action === ACTION_START;
  }
  if (action === ACTION_STOP) {
    if (choice === null) {
      return control.immediate_action === ACTION_STOP;
    }
    return control.automatic_action === ACTION_PAUSE && control.pause_choices.includes(choice);
  }
  return choice === null && control.automatic_action === ACTION_RESUME;
}
function actionErrorKey(code) {
  if (code === null) {
    return "action.error.generic";
  }
  if (code === "spotnav_action_unavailable") {
    return "action.error.unavailable";
  }
  if (code === "spotnav_action_failed") {
    return "action.error.failed";
  }
  if (code === "spotnav_action_reconcile_failed") {
    return "action.error.reconcileFailed";
  }
  if (code === "invalid_pause") {
    return "action.error.invalidPause";
  }
  if (code === "spotnav_unsupported_api_version") {
    return "action.error.version";
  }
  if (code.startsWith("spotnav_") && code.endsWith("charger") || code === "spotnav_charger_unloaded") {
    return "action.error.charger";
  }
  return "action.error.generic";
}
function buildModel(input) {
  const { dashboard, language } = input;
  const market = dashboard.market;
  const format = {
    language,
    timeZone: market?.timezone ?? "",
    unit: market?.minor_unit ?? "",
    currency: market?.currency ?? null,
    majorUnit: market?.major_unit ?? null
  };
  const rows = dashboard.prices.intervals;
  const chart = chartSeries(rows, format.timeZone, input.nowMs);
  const bands = plannedBands(rows, chart.days, format.timeZone);
  const status = dashboard.status;
  const issues = issuesOf(status, language);
  const zone = hasZone(format);
  const price = (value) => value === null || !zone ? null : pricePerKwh(format, value);
  const figure = (value) => value === null || !zone || !Number.isFinite(value) ? null : formatNumber(language, value);
  const nowDay = chart.days.find((day) => day.role === "today") ?? null;
  const installed = dashboard.plan.installed;
  const technicalCodes = issues.map((entry) => entry.technical).filter((code) => code !== null && code !== "");
  return {
    language,
    format,
    timesAvailable: zone,
    chargerName: dashboard.charger.charger_name ?? dashboard.charger.charger_id,
    dashboardSettings: dashboard.settings,
    today: zone ? localDayKey(input.nowMs, format.timeZone) : null,
    dashboardFiscal: dashboard.fiscal,
    status: statusText(status, format, input.nowMs),
    statusNote: statusNote(status, format, input.nowMs),
    issues,
    severity: status === null || status.tone === "normal" || issues.length === 0 ? null : status.tone,
    chart,
    bands,
    summary: {
      max: price(nowDay?.max ?? null),
      min: price(nowDay?.min ?? null),
      current: price(chart.current?.price ?? null),
      maxFigure: figure(nowDay?.max ?? null),
      minFigure: figure(nowDay?.min ?? null)
    },
    figures: figuresFor(dashboard, format),
    proposalPeriods: toPeriods(dashboard.plan.proposal?.periods ?? [], format, null),
    installedPeriods: toPeriods(
      installed?.periods ?? [],
      format,
      installed?.active_period_index ?? null
    ),
    planRelation: planRelationOf(dashboard),
    capabilities: capabilitiesFor(dashboard),
    control: controlFactsFor(dashboard, language),
    advisory: advisoryFor(dashboard, language),
    strategy: strategyFactsFor(dashboard, language),
    site: siteFactsFor(dashboard.site, language),
    currentRange: currentRangeFor(dashboard),
    soc: socFor(dashboard),
    vehicles: vehiclesFor(dashboard),
    targetVehicleId: targetVehicleIdFor(dashboard),
    contextArea: market?.area_id ?? market?.area_name ?? null,
    contextCurrency: market?.currency ?? null,
    contextAreaName: market?.area_name ?? null,
    contextAreaId: market?.area_id ?? null,
    technicalCodes
  };
}

// src/entity-config.ts
var MalformedPayload2 = class extends Error {
};
function bad3() {
  throw new MalformedPayload2("malformed");
}
function isRecord3(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function record3(value) {
  return isRecord3(value) ? value : bad3();
}
function exactKeys3(source, keys) {
  if (Object.keys(source).length !== keys.length) {
    bad3();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      bad3();
    }
  }
}
function text3(source, key) {
  const value = source[key];
  return typeof value === "string" && value !== "" ? value : bad3();
}
function textOrNull3(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad3();
}
function flag(source, key) {
  const value = source[key];
  return typeof value === "boolean" ? value : bad3();
}
function textList(source, key) {
  const value = source[key];
  if (!Array.isArray(value)) {
    return bad3();
  }
  return value.map((entry) => typeof entry === "string" ? entry : bad3());
}
function numberOrNull2(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad3();
}
var ENTITY_NOT_ADMIN = "spotnav_not_admin";
var ENTITY_CONFLICT = "spotnav_conflict";
var ENTITY_INVALID_VALUE = "spotnav_invalid_value";
var ENTITY_NO_SITE = "spotnav_no_site";
var MEASUREMENT_DIRECT = "direct_phase_current";
var MEASUREMENT_DERIVED = "derived_phase_current";
var ENVELOPE_KEYS2 = ["api_version", "ok", "error", "field_errors", "config"];
var ENTITY_KEYS = [
  "allowed_device_classes",
  "allowed_domains",
  "current",
  "effective",
  "field",
  "kind",
  "required",
  "scope",
  "writable"
];
var ENTITY_OPTIONAL_KEYS = ["none"];
var NUMBER_KEYS = ["field", "kind", "minimum", "required", "scope", "value", "writable"];
var ENUM_KEYS = ["choices", "field", "kind", "required", "scope", "value", "writable"];
var FLAG_KEYS = ["field", "kind", "required", "scope", "value", "writable"];
function decodeField(raw) {
  const source = record3(raw);
  const kind = source["kind"];
  const scopeValue = source["scope"];
  if (scopeValue !== "charger" && scopeValue !== "site") {
    return bad3();
  }
  const scope = scopeValue;
  const base = {
    field: text3(source, "field"),
    scope,
    required: flag(source, "required"),
    writable: flag(source, "writable")
  };
  if (kind === "entity") {
    const rawNone = source["none"];
    exactKeys3(
      source,
      rawNone === void 0 ? ENTITY_KEYS : [...ENTITY_KEYS, ...ENTITY_OPTIONAL_KEYS]
    );
    let none = null;
    if (rawNone !== void 0) {
      const choice = record3(rawNone);
      exactKeys3(choice, ["allowed", "automatic", "chosen"]);
      const rawAutomatic = choice["automatic"];
      let automatic = null;
      if (rawAutomatic !== null) {
        const ref = record3(rawAutomatic);
        exactKeys3(ref, ["entity_id", "friendly_name"]);
        automatic = { entityId: text3(ref, "entity_id"), friendlyName: text3(ref, "friendly_name") };
      }
      none = { allowed: flag(choice, "allowed"), chosen: flag(choice, "chosen"), automatic };
    }
    const rawCurrent = source["current"];
    let current = null;
    if (rawCurrent !== null) {
      const ref = record3(rawCurrent);
      exactKeys3(ref, ["entity_id", "friendly_name", "exists"]);
      current = {
        entityId: text3(ref, "entity_id"),
        friendlyName: text3(ref, "friendly_name"),
        exists: flag(ref, "exists")
      };
    }
    const rawEffective = source["effective"];
    let effective = null;
    if (rawEffective !== null) {
      const ref = record3(rawEffective);
      exactKeys3(ref, ["entity_id", "friendly_name", "source"]);
      const origin = ref["source"];
      if (origin !== "configured" && origin !== "automatic") {
        return bad3();
      }
      effective = {
        entityId: text3(ref, "entity_id"),
        friendlyName: text3(ref, "friendly_name"),
        source: origin
      };
    }
    return {
      ...base,
      kind,
      current,
      effective,
      none,
      domains: textList(source, "allowed_domains"),
      deviceClasses: textList(source, "allowed_device_classes")
    };
  }
  if (kind === "number") {
    exactKeys3(source, NUMBER_KEYS);
    const minimum = source["minimum"];
    if (typeof minimum !== "number" || !Number.isFinite(minimum)) {
      return bad3();
    }
    return { ...base, kind, minimum, value: numberOrNull2(source, "value") };
  }
  if (kind === "enum") {
    exactKeys3(source, ENUM_KEYS);
    return { ...base, kind, choices: textList(source, "choices"), value: textOrNull3(source, "value") };
  }
  if (kind === "flag") {
    exactKeys3(source, FLAG_KEYS);
    return { ...base, kind, value: flag(source, "value") };
  }
  return bad3();
}
function list2(source, key) {
  const value = source[key];
  return Array.isArray(value) ? value : bad3();
}
function intervalOrNull(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad3();
}
var BASES = ["measured", "apparent", "reactive", "estimated"];
function decodeBasis(value) {
  if (value === null) {
    return null;
  }
  return typeof value === "string" && BASES.includes(value) ? value : bad3();
}
function decodeMeasurement(raw) {
  const source = record3(raw);
  exactKeys3(source, ["mode", "current_estimated", "assumed_power_factor", "basis"]);
  const basis = record3(source["basis"]);
  exactKeys3(basis, PHASES);
  return {
    mode: text3(source, "mode"),
    currentEstimated: flag(source, "current_estimated"),
    assumedPowerFactor: numberOrNull2(source, "assumed_power_factor"),
    basis: {
      L1: decodeBasis(basis["L1"]),
      L2: decodeBasis(basis["L2"]),
      L3: decodeBasis(basis["L3"])
    }
  };
}
function decodeWarningPhase(raw) {
  const source = record3(raw);
  exactKeys3(source, ["phase", "cause", "entity_id", "age_s"]);
  return {
    phase: text3(source, "phase"),
    cause: oneOf2(source, "cause", ["no_value", "stale"]),
    entityId: textOrNull3(source, "entity_id"),
    ageS: intervalOrNull(source, "age_s")
  };
}
function decodeWarning(raw) {
  const source = record3(raw);
  exactKeys3(source, ["code", "integration", "entity_id", "interval_s", "option", "device_name", "phases"]);
  const phases = source["phases"];
  if (!Array.isArray(phases)) {
    return bad3();
  }
  return {
    code: text3(source, "code"),
    integration: textOrNull3(source, "integration"),
    entityId: textOrNull3(source, "entity_id"),
    intervalS: intervalOrNull(source, "interval_s"),
    option: textOrNull3(source, "option"),
    deviceName: textOrNull3(source, "device_name"),
    phases: phases.map(decodeWarningPhase)
  };
}
function decodeDetectedEntity(raw) {
  const source = record3(raw);
  exactKeys3(source, ["role", "phase", "entity_id", "friendly_name", "disabled"]);
  return {
    role: text3(source, "role"),
    phase: textOrNull3(source, "phase"),
    entityId: text3(source, "entity_id"),
    friendlyName: text3(source, "friendly_name"),
    disabled: flag(source, "disabled")
  };
}
function decodeMeter(raw) {
  const source = record3(raw);
  exactKeys3(source, [
    "id",
    "integration",
    "title",
    "mode",
    "confidence",
    "current_signed",
    "power_inverted",
    "estimated",
    "disabled_entities",
    "entities",
    "warnings",
    "applied"
  ]);
  const confidence = source["confidence"];
  if (confidence !== "high" && confidence !== "medium" && confidence !== "low") {
    return bad3();
  }
  return {
    id: text3(source, "id"),
    integration: text3(source, "integration"),
    title: text3(source, "title"),
    mode: text3(source, "mode"),
    confidence,
    currentSigned: flag(source, "current_signed"),
    powerInverted: flag(source, "power_inverted"),
    estimated: flag(source, "estimated"),
    disabledEntities: textList(source, "disabled_entities"),
    entities: list2(source, "entities").map(decodeDetectedEntity),
    warnings: textList(source, "warnings"),
    applied: flag(source, "applied")
  };
}
function decodeBattery(raw) {
  const source = record3(raw);
  exactKeys3(source, [
    "id",
    "integration",
    "title",
    "entity_id",
    "friendly_name",
    "inverted",
    "discharge_entity_id",
    "disabled_entities"
  ]);
  return {
    id: text3(source, "id"),
    integration: text3(source, "integration"),
    title: text3(source, "title"),
    entityId: text3(source, "entity_id"),
    friendlyName: text3(source, "friendly_name"),
    inverted: flag(source, "inverted"),
    dischargeEntityId: textOrNull3(source, "discharge_entity_id"),
    disabledEntities: textList(source, "disabled_entities")
  };
}
function decodeRef(raw) {
  const ref = record3(raw);
  exactKeys3(ref, ["entity_id", "friendly_name"]);
  return { entityId: text3(ref, "entity_id"), friendlyName: text3(ref, "friendly_name") };
}
function decodeVehicle2(raw) {
  const source = record3(raw);
  exactKeys3(source, ["id", "name", "selected", "source", "candidates"]);
  const origin = source["source"];
  if (origin !== "confirmed" && origin !== "automatic" && origin !== null) {
    return bad3();
  }
  const candidates = source["candidates"];
  if (!Array.isArray(candidates)) {
    return bad3();
  }
  return {
    id: text3(source, "id"),
    name: text3(source, "name"),
    selected: source["selected"] === null ? null : decodeRef(source["selected"]),
    source: origin,
    candidates: candidates.map(decodeRef)
  };
}
function oneOf2(source, key, allowed) {
  const value = source[key];
  return allowed.find((candidate) => candidate === value) ?? bad3();
}
var START_STOP_KINDS = ["switch", "select", "buttons", "easee", "number_pause"];
function lenientKind(source, key, allowed) {
  const value = source[key];
  if (typeof value !== "string" || value === "") {
    return bad3();
  }
  return allowed.find((candidate) => candidate === value) ?? "other";
}
function integer2(source, key) {
  const value = source[key];
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : bad3();
}
function decodeControl2(raw) {
  const source = record3(raw);
  exactKeys3(source, ["platform", "start_stop", "current", "charging_state", "policy", "capabilities", "conflicts"]);
  const startStop = record3(source["start_stop"]);
  exactKeys3(startStop, ["kind", "entity_ids", "inverted", "start_option", "stop_option"]);
  const current = record3(source["current"]);
  exactKeys3(current, ["kind", "entity_id", "service", "enabled"]);
  const state = record3(source["charging_state"]);
  exactKeys3(state, ["source", "entity_id"]);
  const policy = record3(source["policy"]);
  exactKeys3(policy, [
    "min_interval_s",
    "max_writes_per_minute",
    "flash_stored",
    "regulator_writes",
    "zero_pauses",
    "ignored_while_paused",
    "installation_wide",
    "resend_after_plug_in"
  ]);
  const capabilities = record3(source["capabilities"]);
  exactKeys3(capabilities, [
    "start_stop",
    "set_current",
    "regulated_current",
    "reads_charging_state",
    "reads_measured_current",
    "reads_energy_register"
  ]);
  const conflicts = source["conflicts"];
  if (!Array.isArray(conflicts)) {
    return bad3();
  }
  const minInterval = policy["min_interval_s"];
  if (typeof minInterval !== "number" || !Number.isFinite(minInterval) || minInterval < 0) {
    return bad3();
  }
  const perMinute = policy["max_writes_per_minute"];
  if (perMinute !== null) {
    integer2(policy, "max_writes_per_minute");
  }
  return {
    platform: textOrNull3(source, "platform"),
    startStop: {
      kind: lenientKind(startStop, "kind", START_STOP_KINDS),
      entityIds: textList(startStop, "entity_ids"),
      inverted: flag(startStop, "inverted"),
      startOption: textOrNull3(startStop, "start_option"),
      stopOption: textOrNull3(startStop, "stop_option")
    },
    current: {
      kind: oneOf2(current, "kind", ["none", "ocpp", "number", "service"]),
      entityId: textOrNull3(current, "entity_id"),
      service: textOrNull3(current, "service"),
      enabled: flag(current, "enabled")
    },
    chargingState: {
      source: oneOf2(state, "source", ["status", "control"]),
      entityId: textOrNull3(state, "entity_id")
    },
    policy: {
      minIntervalS: minInterval,
      maxWritesPerMinute: perMinute === null ? null : perMinute,
      flashStored: flag(policy, "flash_stored"),
      regulatorWrites: flag(policy, "regulator_writes"),
      zeroPauses: flag(policy, "zero_pauses"),
      ignoredWhilePaused: flag(policy, "ignored_while_paused"),
      installationWide: flag(policy, "installation_wide"),
      resendAfterPlugIn: flag(policy, "resend_after_plug_in")
    },
    capabilities: {
      startStop: flag(capabilities, "start_stop"),
      setCurrent: flag(capabilities, "set_current"),
      regulatedCurrent: flag(capabilities, "regulated_current"),
      readsChargingState: flag(capabilities, "reads_charging_state"),
      readsMeasuredCurrent: flag(capabilities, "reads_measured_current"),
      readsEnergyRegister: flag(capabilities, "reads_energy_register")
    },
    conflicts: conflicts.map((entry) => {
      const item = record3(entry);
      exactKeys3(item, ["kind", "entity_id", "label", "state"]);
      return {
        kind: oneOf2(item, "kind", ["own_mode", "disabled", "duplicate_charger", "other_controller"]),
        entityId: text3(item, "entity_id"),
        label: text3(item, "label"),
        state: text3(item, "state")
      };
    })
  };
}
function decodeConfig(raw) {
  const source = record3(raw);
  exactKeys3(source, ["charger_id", "control", "fields", "site", "vehicles"]);
  const vehicles = source["vehicles"];
  if (!Array.isArray(vehicles)) {
    return bad3();
  }
  const fields = source["fields"];
  if (!Array.isArray(fields)) {
    return bad3();
  }
  const rawSite = source["site"];
  let site = null;
  if (rawSite !== null) {
    const siteSource = record3(rawSite);
    exactKeys3(siteSource, ["name", "charger_count", "measurement", "warnings", "detection"]);
    const count = siteSource["charger_count"];
    if (typeof count !== "number" || !Number.isInteger(count) || count < 0) {
      return bad3();
    }
    const detection = record3(siteSource["detection"]);
    exactKeys3(detection, ["meters", "batteries"]);
    site = {
      name: text3(siteSource, "name"),
      chargerCount: count,
      measurement: decodeMeasurement(siteSource["measurement"]),
      warnings: list2(siteSource, "warnings").map(decodeWarning),
      meters: list2(detection, "meters").map(decodeMeter),
      batteries: list2(detection, "batteries").map(decodeBattery)
    };
  }
  return {
    chargerId: text3(source, "charger_id"),
    fields: fields.map(decodeField),
    site,
    vehicles: vehicles.map(decodeVehicle2),
    control: source["control"] === null ? null : decodeControl2(source["control"])
  };
}
function decodeEnvelope(raw, extra) {
  const version = raw["api_version"];
  if (typeof version === "number" && version !== ENTITY_CONFIG_API_VERSION) {
    return "unsupported";
  }
  exactKeys3(raw, [...ENVELOPE_KEYS2, ...extra]);
  if (version !== ENTITY_CONFIG_API_VERSION) {
    return "malformed";
  }
  const ok = flag(raw, "ok");
  const error = textOrNull3(raw, "error");
  const rawConfig = raw["config"];
  const config = rawConfig === null ? null : decodeConfig(rawConfig);
  const rawErrors = raw["field_errors"];
  if (!Array.isArray(rawErrors)) {
    return "malformed";
  }
  const fieldErrors = rawErrors.map((entry) => {
    const item = record3(entry);
    exactKeys3(item, ["field", "code"]);
    return { field: text3(item, "field"), code: text3(item, "code") };
  });
  if (ok) {
    if (error !== null || config === null || fieldErrors.length > 0) {
      return "malformed";
    }
  } else if (error === null || error === "") {
    return "malformed";
  }
  return { ok, error, config, fieldErrors };
}
function decodeEntityAnswer(raw) {
  try {
    if (!isRecord3(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const envelope = decodeEnvelope(raw, []);
    if (typeof envelope === "string") {
      return { ok: false, failure: envelope };
    }
    if (envelope.ok && envelope.config !== null) {
      return { ok: true, value: { ok: true, config: envelope.config } };
    }
    return {
      ok: true,
      value: { ok: false, code: envelope.error ?? "", config: envelope.config, fieldErrors: envelope.fieldErrors }
    };
  } catch (error) {
    if (error instanceof MalformedPayload2 || error instanceof Error && error.message === "malformed") {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}
function decodeVehicleAnswer(raw) {
  try {
    if (!isRecord3(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const envelope = decodeEnvelope(raw, ["vehicle"]);
    if (typeof envelope === "string") {
      return { ok: false, failure: envelope };
    }
    const vehicle = raw["vehicle"] === null ? null : decodeVehicle(raw["vehicle"]);
    if (envelope.ok) {
      if (envelope.config === null || vehicle === null) {
        return { ok: false, failure: "malformed" };
      }
      return { ok: true, value: { ok: true, config: envelope.config, vehicle, fieldErrors: [] } };
    }
    return {
      ok: true,
      value: {
        ok: false,
        code: envelope.error ?? "",
        config: envelope.config,
        vehicle,
        fieldErrors: envelope.fieldErrors
      }
    };
  } catch (error) {
    if (error instanceof MalformedPayload2 || error instanceof Error && error.message === "malformed") {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}
var PHASES = ["L1", "L2", "L3"];
var GRID_TOTAL_FIELDS = ["grid_power_source_power", "grid_power_source_power_export"];
var DERIVED_KINDS = ["power", "voltage", "power_export", "reactive_power", "apparent_power", "current"];
var DERIVED_REQUIRED_KINDS = ["power", "voltage"];
function directFieldName(phase) {
  return `direct_${phase}`;
}
function derivedFieldName(phase, kind) {
  return `derived_${phase}_${kind}`;
}
var PHASE_FIELD = /^(direct_L[123]|derived_L[123]_(power|power_export|reactive_power|apparent_power|current|voltage))$/;
function isPhaseField(field2) {
  return PHASE_FIELD.test(field2);
}
function phaseFieldNames(mode) {
  if (mode === MEASUREMENT_DERIVED) {
    return PHASES.flatMap((phase) => DERIVED_KINDS.map((kind) => derivedFieldName(phase, kind)));
  }
  return PHASES.map(directFieldName);
}
var DERIVED_CLASSES = {
  power: "power",
  power_export: "power",
  reactive_power: "reactive_power",
  apparent_power: "apparent_power",
  current: "current",
  voltage: "voltage"
};
function phaseField(config, field2) {
  const listed = config.fields.find((entry) => entry.field === field2);
  if (listed !== void 0 && listed.kind === "entity") {
    return listed;
  }
  const derived = /^derived_L[123]_(.+)$/.exec(field2);
  const deviceClass = derived === null ? "current" : DERIVED_CLASSES[derived[1] ?? ""] ?? "power";
  return {
    field: field2,
    kind: "entity",
    scope: "site",
    required: derived === null || DERIVED_REQUIRED_KINDS.includes(derived[1] ?? ""),
    writable: true,
    current: null,
    effective: null,
    none: null,
    domains: ["sensor"],
    deviceClasses: [deviceClass]
  };
}
function fieldsOf(config, scope) {
  return config.fields.filter((entry) => entry.scope === scope);
}
function storedMode(config) {
  const mode = config.fields.find((entry) => entry.field === "measurement_mode");
  return mode !== void 0 && mode.kind === "enum" ? mode.value : null;
}
var NONE_VALUE = "none";
function readText(field2) {
  if (field2.kind === "entity") {
    if (field2.current === null && field2.none !== null && field2.none.chosen) {
      return NONE_VALUE;
    }
    return field2.current === null ? "" : field2.current.entityId;
  }
  if (field2.kind === "number") {
    return field2.value === null ? "" : String(field2.value);
  }
  if (field2.kind === "flag") {
    return field2.value ? "true" : "false";
  }
  return field2.value ?? "";
}
function draftFrom(config, scope) {
  const draft = {};
  for (const field2 of fieldsOf(config, scope)) {
    if (field2.writable) {
      draft[field2.field] = readText(field2);
    }
  }
  return draft;
}
var APPLY_DETECTION = "apply_detection";
function parseNumber(value) {
  const trimmed = value.trim().replace(",", ".");
  if (trimmed === "") {
    return null;
  }
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}
function entityChange(config, scope, draft) {
  const changes = {};
  const expected = {};
  const errors = [];
  const applied = draft[APPLY_DETECTION];
  if (scope === "site" && applied !== void 0 && applied !== "") {
    return { ok: true, changed: true, request: { scope, expected: {}, changes: { [APPLY_DETECTION]: applied } } };
  }
  let considered = fieldsOf(config, scope).filter((field2) => field2.writable && !isPhaseField(field2.field));
  if (scope === "site") {
    const mode = draft["measurement_mode"] ?? storedMode(config) ?? MEASUREMENT_DIRECT;
    considered = considered.concat(phaseFieldNames(mode).map((name) => phaseField(config, name)));
  }
  for (const field2 of considered) {
    const listed = config.fields.find((entry) => entry.field === field2.field);
    const read = listed === void 0 ? "" : readText(listed);
    const value = draft[field2.field] ?? read;
    if (field2.kind === "number") {
      const parsed = parseNumber(value);
      if (parsed === null) {
        errors.push({ field: field2.field, code: "required" });
        continue;
      }
      if (Number.isNaN(parsed) || parsed < field2.minimum) {
        errors.push({ field: field2.field, code: "invalid_value" });
        continue;
      }
      const stored = listed !== void 0 && listed.kind === "number" ? listed.value : null;
      if (parsed !== stored) {
        changes[field2.field] = parsed;
        if (stored !== null) {
          expected[field2.field] = stored;
        }
      }
      continue;
    }
    if (field2.kind === "flag") {
      if (value !== read && (value === "true" || value === "false")) {
        changes[field2.field] = value === "true";
        expected[field2.field] = read === "true";
      }
      continue;
    }
    const trimmed = value.trim();
    if (trimmed === read) {
      continue;
    }
    changes[field2.field] = trimmed;
    if (listed !== void 0) {
      expected[field2.field] = read;
    }
  }
  if (errors.length > 0) {
    return { ok: false, errors };
  }
  if (Object.keys(changes).length === 0) {
    return { ok: true, changed: false };
  }
  return { ok: true, changed: true, request: { scope, expected, changes } };
}
function vehicleChoice(vehicle) {
  return vehicle.source === "confirmed" && vehicle.selected !== null ? vehicle.selected.entityId : "";
}
function vehicleSocChanges(vehicles, draft) {
  const requests = [];
  for (const vehicle of vehicles) {
    const chosen = draft[vehicle.id] ?? vehicleChoice(vehicle);
    if (chosen !== vehicleChoice(vehicle)) {
      requests.push({ vehicleId: vehicle.id, entityId: chosen === "" ? null : chosen });
    }
  }
  return requests;
}
var FIELD_LABELS = {
  charge_control: "entity.field.chargeControl",
  current_limit: "entity.field.currentLimit",
  energy_register_entity: "entity.field.energyRegister",
  power_entity: "entity.field.powerEntity",
  vehicle_soc: "entity.field.vehicleSoc",
  main_fuse_a: "entity.field.mainFuse",
  safety_margin_a: "entity.field.safetyMargin",
  measurement_mode: "entity.field.measurementMode",
  voltage_between_phases_v: "entity.field.voltageBetweenPhases",
  charger_priority: "entity.field.chargerPriority",
  battery_aggregate_power_entity: "entity.field.batteryPower",
  battery_discharge_power_entity: "entity.field.batteryDischargePower",
  battery_power_inverted: "entity.field.batteryPowerInverted",
  site_current_signed: "entity.field.siteCurrentSigned",
  grid_power_inverted: "entity.field.gridPowerInverted",
  grid_power_source_power: "entity.field.gridPowerSource",
  grid_power_source_power_export: "entity.field.gridPowerSourceExport",
  max_age_s: "entity.field.maxAge"
};
function fieldLabelKey(field2) {
  return FIELD_LABELS[field2] ?? null;
}
var DETECT_WARNING_KEYS = {
  own_load_balancing: "entity.detect.warning.own_load_balancing",
  sign_unverified: "entity.detect.warning.sign_unverified",
  voltage_from_other_device: "entity.detect.warning.voltage_from_other_device",
  may_measure_subcircuit: "entity.detect.warning.may_measure_subcircuit",
  reports_on_change_only: "entity.detect.warning.reports_on_change_only"
};
var INFORMATIONAL_DETECT_WARNINGS = /* @__PURE__ */ new Set([
  "voltage_from_other_device",
  "reports_on_change_only"
]);
function batteryApplied(config, battery) {
  const entity = (name) => {
    const field2 = config.fields.find((entry) => entry.field === name);
    return field2 !== void 0 && field2.kind === "entity" && field2.current !== null ? field2.current.entityId : "";
  };
  const flagField = config.fields.find((entry) => entry.field === "battery_power_inverted");
  const inverted = flagField !== void 0 && flagField.kind === "flag" ? flagField.value : false;
  return entity("battery_aggregate_power_entity") === battery.entityId && entity("battery_discharge_power_entity") === (battery.dischargeEntityId ?? "") && inverted === battery.inverted;
}
var DERIVED_KIND_KEYS = {
  power: "entity.derived.power",
  power_export: "entity.derived.powerExport",
  reactive_power: "entity.derived.reactivePower",
  apparent_power: "entity.derived.apparentPower",
  current: "entity.derived.current",
  voltage: "entity.derived.voltage"
};
var FIELD_ERROR_KEYS = {
  required: "entity.error.field.required",
  entity_not_found: "entity.error.field.notFound",
  wrong_domain: "entity.error.field.wrongDomain",
  invalid_value: "entity.error.field.invalid",
  not_writable: "entity.error.field.notWritable",
  control_path_unknown: "entity.error.field.controlPathUnknown",
  unknown_field: "entity.error.field.unknown",
  charge_control_in_use: "entity.error.field.chargeControlInUse",
  current_limit_in_use: "entity.error.field.currentLimitInUse",
  duplicate_charger: "entity.error.field.duplicateCharger",
  unknown_vehicle: "entity.error.field.unknownVehicle",
  invalid_capacity: "settings.vehicle.error.capacity",
  invalid_consumption: "settings.vehicle.error.consumption"
};
function fieldErrorKey(code) {
  return FIELD_ERROR_KEYS[code] ?? "entity.error.field.unknown";
}
function entityErrorKey(code) {
  if (code === ENTITY_NOT_ADMIN) {
    return "entity.error.notAdmin";
  }
  if (code === ENTITY_CONFLICT) {
    return "entity.error.conflict";
  }
  if (code === ENTITY_INVALID_VALUE) {
    return "entity.error.invalid";
  }
  if (code === ENTITY_NO_SITE) {
    return "site.none";
  }
  return "entity.error.generic";
}
var FIELD_HELP = {
  charge_control: "entity.help.chargeControl",
  current_limit: "entity.help.currentLimit",
  energy_register_entity: "entity.help.energyRegister",
  power_entity: "entity.help.powerEntity",
  vehicle_soc: "entity.help.vehicleSoc",
  main_fuse_a: "entity.help.mainFuse",
  safety_margin_a: "entity.help.safetyMargin",
  measurement_mode: "entity.help.measurementMode",
  voltage_between_phases_v: "entity.help.voltageBetweenPhases",
  charger_priority: "entity.help.chargerPriority",
  battery_aggregate_power_entity: "entity.help.batteryPower",
  battery_discharge_power_entity: "entity.help.batteryDischargePower",
  battery_power_inverted: "entity.help.batteryPowerInverted",
  site_current_signed: "entity.help.siteCurrentSigned",
  grid_power_inverted: "entity.help.gridPowerInverted",
  grid_power_source_power: "entity.help.gridPowerSource",
  grid_power_source_power_export: "entity.help.gridPowerSourceExport",
  max_age_s: "entity.help.maxAge"
};
var DERIVED_HELP = {
  power: "entity.help.derivedPower",
  power_export: "entity.help.derivedPowerExport",
  reactive_power: "entity.help.derivedReactivePower",
  apparent_power: "entity.help.derivedApparentPower",
  current: "entity.help.derivedCurrent",
  voltage: "entity.help.derivedVoltage"
};
function fieldHelpKey(field2) {
  const fixed = FIELD_HELP[field2];
  if (fixed !== void 0) {
    return fixed;
  }
  if (/^direct_L[123]$/.test(field2)) {
    return "entity.help.phaseDirect";
  }
  const derived = /^derived_L[123]_(.+)$/.exec(field2);
  return derived === null ? null : DERIVED_HELP[derived[1] ?? ""] ?? null;
}
function isMissingEntity(field2) {
  return field2.current !== null && !field2.current.exists;
}
function automaticEntity(field2) {
  const effective = field2.effective;
  if (effective !== null && effective.source === "automatic") {
    return effective;
  }
  return field2.none === null ? null : field2.none.automatic;
}

// src/entity-editor.ts
function element(doc, tag, className, content) {
  const node = doc.createElement(tag);
  if (className !== void 0) {
    node.className = className;
  }
  if (content !== void 0) {
    node.textContent = content;
  }
  return node;
}
function modeLabel(language, mode) {
  return translate(language, mode === MEASUREMENT_DERIVED ? "entity.mode.derived" : "entity.mode.direct");
}
function labelOf(language, field2) {
  const key = fieldLabelKey(field2);
  if (key !== null) {
    return translate(language, key);
  }
  const direct = /^direct_(L[123])$/.exec(field2);
  if (direct !== null) {
    return translate(language, "entity.phase.direct", { phase: direct[1] ?? "" });
  }
  const derived = /^derived_(L[123])_(.+)$/.exec(field2);
  if (derived !== null) {
    const kindKey = DERIVED_KIND_KEYS[derived[2] ?? ""];
    const kind = kindKey === void 0 ? derived[2] ?? "" : translate(language, kindKey);
    return `${derived[1] ?? ""} ${kind.toLowerCase()}`;
  }
  return field2;
}
function warningText(language, warning) {
  const integration = warning.integration ?? "";
  if (warning.code === "measurement_unhealthy") {
    const of = (cause) => warning.phases.filter((item) => item.cause === cause);
    return measurementProblemText(language, {
      noValuePhases: of("no_value").map((item) => item.phase),
      noValueEntities: of("no_value").flatMap((item) => item.entityId === null ? [] : [item.entityId]),
      stalePhases: of("stale").map((item) => item.phase),
      maxAgeS: warning.intervalS
    });
  }
  if (warning.code === "update_interval_exceeds_max_age") {
    const lines = [
      translate(language, "entity.warning.updateInterval", {
        integration,
        seconds: formatNumber(language, warning.intervalS ?? 0, 0)
      })
    ];
    if (warning.option !== null) {
      lines.push(translate(language, "entity.warning.updateIntervalOption", { option: warning.option }));
    }
    return lines.join(" ");
  }
  if (warning.code === "reports_on_change_only") {
    return translate(language, "entity.warning.onChange", { integration });
  }
  if (warning.code === "own_load_balancing") {
    return translate(language, "entity.warning.ownBalancing", { name: warning.deviceName ?? integration, integration });
  }
  if (warning.code === "external_current_balancer") {
    return translate(language, "entity.warning.externalBalancer", { name: warning.deviceName ?? "", integration });
  }
  return translate(language, "entity.warning.unknown");
}
var NOTE_RANK = {
  measurement_unhealthy: 0,
  own_load_balancing: 1,
  external_current_balancer: 2,
  update_interval_exceeds_max_age: 3,
  reports_on_change_only: 4,
  estimated: 5
};
var UNKNOWN_NOTE_RANK = 6;
function siteNotes(language, site, includeEstimate = true) {
  const notes = site.warnings.map((warning) => ({
    code: warning.code,
    kind: "warning",
    text: warningText(language, warning)
  }));
  if (includeEstimate && site.measurement.currentEstimated) {
    notes.push({
      code: "estimated",
      kind: "notice",
      text: translate(language, "entity.notice.estimated", {
        pf: formatNumber(language, site.measurement.assumedPowerFactor ?? 0.9, 1)
      })
    });
  }
  const seen = /* @__PURE__ */ new Set();
  return notes.map((note, index) => ({ note, index })).sort(
    (a, b) => (NOTE_RANK[a.note.code] ?? UNKNOWN_NOTE_RANK) - (NOTE_RANK[b.note.code] ?? UNKNOWN_NOTE_RANK) || a.index - b.index
  ).map(({ note }) => note).filter((note) => {
    if (seen.has(note.text)) {
      return false;
    }
    seen.add(note.text);
    return true;
  });
}
function siteWarningRows(doc, language, site) {
  return siteNotes(language, site, false).map((note) => {
    const row = element(doc, "p", VISUAL_CLASSES.entityWarning, note.text);
    row.dataset["warning"] = note.code;
    return row;
  });
}
function siteNotices(doc, language, site) {
  const notes = siteNotes(language, site);
  if (notes.length === 0) {
    return null;
  }
  const list4 = element(doc, "ul", VISUAL_CLASSES.entityNotices);
  list4.dataset["notices"] = "site";
  for (const note of notes) {
    const item = element(doc, "li", VISUAL_CLASSES.entityWarning, note.text);
    item.dataset[note.kind] = note.code;
    list4.append(item);
  }
  return list4;
}
async function ensureHaSelector(win, timeoutMs = 3e3) {
  const registry = win?.customElements;
  if (registry === void 0 || registry.get("ha-selector") !== void 0) {
    return;
  }
  const load = win.loadCardHelpers;
  if (typeof load !== "function") {
    return;
  }
  try {
    const helpers = await load();
    const card = helpers.createCardElement?.({ type: "entities", entities: [] });
    await card?.constructor?.getConfigElement?.();
    await Promise.race([
      registry.whenDefined("ha-selector"),
      new Promise((resolve) => setTimeout(resolve, timeoutMs))
    ]);
  } catch {
  }
}
function controlCurrentText(language, control, name) {
  const current = control.current;
  if (current.kind === "none") {
    return translate(language, "control.current.none");
  }
  if (!current.enabled) {
    return translate(language, "control.current.off");
  }
  if (current.kind === "ocpp") {
    return translate(language, "control.current.ocpp");
  }
  if (current.kind === "service") {
    return translate(language, "control.current.service");
  }
  return translate(language, "control.current.number", { name });
}
function currentRestrictions(language, control) {
  if (control.current.kind === "none") {
    return [];
  }
  const { policy, capabilities } = control;
  const lines = [];
  if (policy.minIntervalS >= 60) {
    const wholeMinutes = policy.minIntervalS % 60 === 0 && policy.minIntervalS >= 120;
    lines.push(
      wholeMinutes ? translate(language, "control.limit.minutes", { count: formatNumber(language, policy.minIntervalS / 60, 0) }) : translate(language, "control.limit.seconds", { count: formatNumber(language, policy.minIntervalS, 0) })
    );
  }
  if (policy.flashStored || !policy.regulatorWrites) {
    lines.push(translate(language, "control.limit.flash"));
  }
  if (capabilities.startStop && !capabilities.regulatedCurrent) {
    lines.push(translate(language, "control.limit.stopOnly"));
  }
  if (policy.installationWide) {
    lines.push(translate(language, "control.limit.installation"));
  }
  return lines;
}
function controlNotes(doc, language, control, nameOf) {
  const nodes = [];
  if (control.startStop.kind === "easee") {
    const fixed = element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, "control.startStop.easeeFixed"));
    fixed.dataset["controlRow"] = "start_stop_fixed";
    nodes.push(fixed);
  }
  for (const conflict of control.conflicts) {
    const warning = element(doc, "p", VISUAL_CLASSES.entityWarning, conflictText(language, conflict, nameOf(conflict.entityId)));
    warning.dataset["conflict"] = conflict.entityId;
    nodes.push(warning);
  }
  if (nodes.length === 0) {
    return null;
  }
  const block = element(doc, "div", VISUAL_CLASSES.entityRow);
  block.dataset["control"] = "path";
  block.append(...nodes);
  return block;
}
function conflictText(language, conflict, name) {
  if (conflict.kind === "duplicate_charger") {
    return translate(language, "issue.duplicateCharger", { other: conflict.state });
  }
  if (conflict.kind === "other_controller") {
    return translate(language, "control.otherController", { name: conflict.label });
  }
  return conflict.kind === "disabled" ? translate(language, "control.disabled", { name }) : translate(language, "control.conflict", { label: conflict.label, name });
}
function entityNameIn(config, entityId) {
  for (const field2 of config.fields) {
    if (field2.kind === "entity" && field2.current !== null && field2.current.entityId === entityId) {
      return field2.current.friendlyName;
    }
  }
  return entityId;
}
function selectorFor(field2) {
  const entity = { domain: field2.domains };
  if (field2.deviceClasses.length > 0) {
    entity["device_class"] = field2.deviceClasses;
  }
  return { entity };
}
function entityEditorBody(doc, language, input, handlers, idPrefix) {
  const { config, scope } = input;
  const values = draftFrom(config, scope);
  const pickers = /* @__PURE__ */ new Set();
  const disabledWhenPending = [];
  const errorNodes = /* @__PURE__ */ new Map();
  let pending = false;
  const locked = input.readOnly === true;
  const keepEnabled = /* @__PURE__ */ new Set();
  const body = element(doc, "form");
  body.noValidate = true;
  body.dataset["entityEditor"] = scope;
  const notice = element(doc, "p", VISUAL_CLASSES.settingsNotice);
  notice.setAttribute("role", "status");
  notice.hidden = true;
  body.append(notice);
  if (input.appliesText !== null) {
    body.append(
      element(
        doc,
        "p",
        VISUAL_CLASSES.siteApplies,
        scope === "site" ? translate(language, "entity.site.intro", { applies: input.appliesText }) : input.appliesText
      )
    );
  }
  if (scope === "charger" && config.control !== null) {
    const notes = controlNotes(doc, language, config.control, (entityId) => entityNameIn(config, entityId));
    if (notes !== null) {
      body.append(notes);
    }
  }
  if (scope === "site" && config.site !== null) {
    const notices = siteNotices(doc, language, config.site);
    if (notices !== null) {
      body.append(notices);
    }
    const detection = detectionSection(config, config.site);
    if (detection !== null) {
      body.append(detection);
    }
  }
  const ctor = doc.defaultView?.customElements;
  const useSelector = ctor !== void 0 && ctor.get("ha-selector") !== void 0;
  function entityControl(field2, label) {
    const id = `${idPrefix}-entity-${field2.field}`;
    if (useSelector) {
      const picker = doc.createElement("ha-selector");
      picker.hass = input.hass();
      picker.selector = selectorFor(field2);
      picker.value = values[field2.field] === "" ? void 0 : values[field2.field];
      picker.label = label;
      picker.required = false;
      picker.dataset["field"] = field2.field;
      picker.addEventListener("value-changed", (event) => {
        event.stopPropagation();
        const detail = event.detail;
        const next = detail?.value;
        values[field2.field] = typeof next === "string" ? next : "";
      });
      pickers.add(picker);
      return { node: picker, input: picker, picker };
    }
    const text5 = doc.createElement("input");
    text5.type = "text";
    text5.id = id;
    text5.className = VISUAL_CLASSES.settingsInput;
    text5.value = values[field2.field] ?? "";
    text5.dataset["field"] = field2.field;
    text5.autocomplete = "off";
    text5.spellcheck = false;
    text5.setAttribute("autocapitalize", "off");
    text5.setAttribute("aria-label", label);
    text5.addEventListener("input", () => {
      values[field2.field] = text5.value;
    });
    disabledWhenPending.push(text5);
    return { node: text5, input: text5, picker: null };
  }
  function isChoiceGrouped(field2) {
    return scope === "charger" && field2.writable && (field2.field === "current_limit" || field2.field === "energy_register_entity" && automaticEntity(field2) !== null);
  }
  function appendInfo(block, field2) {
    if (field2.kind === "entity" && isMissingEntity(field2)) {
      const warning = element(
        doc,
        "p",
        VISUAL_CLASSES.entityWarning,
        translate(language, field2.required ? "entity.missing.required" : "entity.missing.optional")
      );
      warning.dataset["missing"] = field2.field;
      block.append(warning);
    }
    if (field2.kind === "entity" && isChoiceGrouped(field2)) {
      return;
    }
    const automatic = field2.kind === "entity" ? automaticEntity(field2) : null;
    if (automatic !== null) {
      block.append(
        element(doc, "p", VISUAL_CLASSES.entityAutomatic, translate(language, "entity.automatic", { name: automatic.friendlyName }))
      );
    }
    const key = isPhaseField(field2.field) ? null : fieldHelpKey(field2.field);
    if (key !== null) {
      const help = element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, key));
      help.dataset["help"] = field2.field;
      block.append(help);
    }
  }
  function fieldBlock(name, label, control, showLabel, field2 = null) {
    const block = element(doc, "div", VISUAL_CLASSES.settingsField);
    block.dataset["fieldBlock"] = name;
    if (showLabel && control.picker === null) {
      const caption = element(doc, "label", VISUAL_CLASSES.settingsLabel, label);
      caption.setAttribute("for", control.input.id);
      block.append(caption);
    }
    block.append(control.node);
    if (field2 !== null) {
      appendInfo(block, field2);
    }
    const error = element(doc, "p", VISUAL_CLASSES.settingsError);
    error.hidden = true;
    error.dataset["fieldError"] = name;
    error.setAttribute("role", "alert");
    const errorId = `${idPrefix}-error-${name}`;
    error.id = errorId;
    block.append(error);
    errorNodes.set(name, { node: error, input: control.input });
    return block;
  }
  function numberControl(field2, label) {
    const id = `${idPrefix}-entity-${field2.field}`;
    const number2 = doc.createElement("input");
    number2.type = "number";
    number2.id = id;
    number2.className = VISUAL_CLASSES.settingsInput;
    number2.inputMode = "decimal";
    number2.step = "any";
    if (field2.kind === "number") {
      number2.min = String(field2.minimum);
    }
    number2.value = values[field2.field] ?? "";
    number2.dataset["field"] = field2.field;
    number2.setAttribute("aria-label", label);
    number2.addEventListener("input", () => {
      values[field2.field] = number2.value;
    });
    disabledWhenPending.push(number2);
    return { node: number2, input: number2, picker: null };
  }
  function entityField(field2, label, showLabel = true) {
    const control = entityControl(field2, label);
    if (!showLabel && control.picker !== null) {
      control.picker.label = "";
    }
    return fieldBlock(field2.field, label, control, showLabel, field2);
  }
  function flagField(field2, label) {
    const block = element(doc, "div", VISUAL_CLASSES.settingsField);
    block.dataset["fieldBlock"] = field2.field;
    const row = element(doc, "label", VISUAL_CLASSES.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.id = `${idPrefix}-entity-${field2.field}`;
    box.checked = values[field2.field] === "true";
    box.dataset["field"] = field2.field;
    box.addEventListener("change", () => {
      values[field2.field] = box.checked ? "true" : "false";
    });
    disabledWhenPending.push(box);
    row.append(box, doc.createTextNode(label));
    block.append(row);
    appendInfo(block, field2);
    const error = element(doc, "p", VISUAL_CLASSES.settingsError);
    error.hidden = true;
    error.dataset["fieldError"] = field2.field;
    error.setAttribute("role", "alert");
    error.id = `${idPrefix}-error-${field2.field}`;
    block.append(error);
    errorNodes.set(field2.field, { node: error, input: box });
    return block;
  }
  function detectionSection(full, site) {
    const batteryInUse = new Map(site.batteries.map((battery) => [battery.id, batteryApplied(full, battery)]));
    const differs = site.meters.some((meter) => !meter.applied) || site.batteries.some((battery) => batteryInUse.get(battery.id) !== true);
    if (!differs) {
      return null;
    }
    const section = element(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
    section.dataset["detection"] = "site";
    section.append(element(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "entity.detect.title")));
    section.append(element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, "entity.detect.intro")));
    const apply = (id, name) => {
      const button = element(doc, "button", VISUAL_CLASSES.button, translate(language, "entity.detect.use"));
      button.setAttribute("aria-label", `${translate(language, "entity.detect.use")}: ${name}`);
      button.type = "button";
      button.dataset["apply"] = id;
      button.addEventListener("click", () => {
        if (!pending) {
          handlers.onSave({ [APPLY_DETECTION]: id });
        }
      });
      disabledWhenPending.push(button);
      return button;
    };
    const line = (text5, code) => {
      const node = element(doc, "p", VISUAL_CLASSES.entityHelp, text5);
      node.dataset["detect"] = code;
      return node;
    };
    const enableLine = (count) => count === 0 ? null : line(translate(language, "entity.detect.enable", { count: String(count) }), "enable");
    if (site.meters.length > 0) {
      section.append(element(doc, "strong", void 0, translate(language, "entity.detect.meters")));
    }
    for (const meter of site.meters) {
      section.append(meterCard(meter, apply, line, enableLine));
    }
    if (site.batteries.length > 0) {
      section.append(element(doc, "strong", void 0, translate(language, "entity.detect.batteries")));
    }
    for (const battery of site.batteries) {
      section.append(batteryCard(battery, batteryInUse.get(battery.id) === true, apply, line, enableLine));
    }
    return section;
  }
  function meterCard(meter, apply, line, enableLine) {
    const card = element(doc, "div", VISUAL_CLASSES.entityRow);
    card.dataset["detectedMeter"] = meter.id;
    card.append(element(doc, "span", VISUAL_CLASSES.entityRowLabel, `${meter.title} (${meter.integration})`));
    card.append(
      line(
        translate(language, meter.mode === MEASUREMENT_DERIVED ? "entity.detect.derived" : "entity.detect.direct"),
        "mode"
      )
    );
    if (meter.currentSigned) {
      card.append(line(translate(language, "entity.detect.signed"), "signed"));
    }
    if (meter.powerInverted) {
      card.append(line(translate(language, "entity.detect.inverted"), "inverted"));
    }
    if (meter.entities.some((entity) => entity.role === "grid_power")) {
      card.append(line(translate(language, "entity.detect.gridPower"), "gridPower"));
    }
    if (meter.estimated) {
      const estimated = line(translate(language, "entity.detect.estimated"), "estimated");
      estimated.className = VISUAL_CLASSES.entityWarning;
      card.append(estimated);
    }
    if (meter.confidence === "low") {
      card.append(line(translate(language, "entity.detect.confidence.low"), "confidence"));
    }
    for (const code of meter.warnings) {
      const key = DETECT_WARNING_KEYS[code];
      if (key !== void 0) {
        const warning = line(translate(language, key), code);
        warning.className = INFORMATIONAL_DETECT_WARNINGS.has(code) ? VISUAL_CLASSES.entityHelp : VISUAL_CLASSES.entityWarning;
        card.append(warning);
      }
    }
    const enable = enableLine(meter.disabledEntities.length);
    if (enable !== null) {
      card.append(enable);
    }
    if (meter.applied) {
      card.append(element(doc, "span", VISUAL_CLASSES.entityAutomatic, translate(language, "entity.detect.inUse")));
    } else {
      card.append(apply(meter.id, meter.title));
    }
    return card;
  }
  function batteryCard(battery, inUse, apply, line, enableLine) {
    const card = element(doc, "div", VISUAL_CLASSES.entityRow);
    card.dataset["detectedBattery"] = battery.id;
    card.append(element(doc, "span", VISUAL_CLASSES.entityRowLabel, `${battery.friendlyName} (${battery.integration})`));
    if (battery.inverted) {
      card.append(line(translate(language, "entity.detect.battery.inverted"), "inverted"));
    }
    if (battery.dischargeEntityId !== null) {
      card.append(line(translate(language, "entity.detect.battery.pair"), "pair"));
    }
    const enable = enableLine(battery.disabledEntities.length);
    if (enable !== null) {
      card.append(enable);
    }
    if (inUse) {
      card.append(element(doc, "span", VISUAL_CLASSES.entityAutomatic, translate(language, "entity.detect.inUse")));
    } else {
      card.append(apply(battery.id, battery.friendlyName));
    }
    return card;
  }
  const MANAGED_SITE = /* @__PURE__ */ new Set([
    "site_current_signed",
    ...GRID_TOTAL_FIELDS,
    "grid_power_inverted",
    "battery_aggregate_power_entity",
    "battery_discharge_power_entity",
    "battery_power_inverted",
    "max_age_s"
  ]);
  const MANAGED_CHARGER = /* @__PURE__ */ new Set(["current_limit", "energy_register_entity", "power_entity"]);
  const managed = /* @__PURE__ */ new Map();
  const isManaged = (name) => scope === "site" ? MANAGED_SITE.has(name) : MANAGED_CHARGER.has(name);
  for (const field2 of fieldsOf(config, scope)) {
    if (!field2.writable || isPhaseField(field2.field) || field2.field === "measurement_mode") {
      continue;
    }
    const label = labelOf(language, field2.field);
    let block = null;
    if (field2.kind === "entity" && scope === "charger" && field2.field === "charge_control") {
      block = element(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
      block.dataset["part"] = "charge-control";
      block.append(element(doc, "legend", VISUAL_CLASSES.siteLegend, label), entityField(field2, label, false));
    } else if (field2.kind === "entity") {
      block = entityField(field2, label);
    } else if (field2.kind === "flag") {
      block = flagField(field2, label);
    } else if (field2.kind === "number") {
      const row = fieldBlock(field2.field, label, numberControl(field2, label), true, field2);
      row.querySelector("label")?.classList.add(VISUAL_CLASSES.entityNumberLabel);
      const control = row.querySelector("input");
      if (control !== null) {
        const line = element(doc, "div", VISUAL_CLASSES.settingsRow);
        control.replaceWith(line);
        line.append(control, element(doc, "span", VISUAL_CLASSES.settingsUnit, field2.field === "main_fuse_a" || field2.field === "safety_margin_a" ? "A" : "s"));
      }
      block = row;
    }
    if (block === null) {
      continue;
    }
    if (isManaged(field2.field)) {
      managed.set(field2.field, block);
    } else {
      body.append(block);
    }
  }
  const blocksOf = (...names) => names.flatMap((name) => {
    const block = managed.get(name);
    return block === void 0 ? [] : [block];
  });
  function choiceGroup(part, title, options, get, set, extra = {}) {
    const fieldset = element(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
    fieldset.dataset["part"] = part;
    if (title !== null) {
      fieldset.append(element(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, title)));
    }
    if (extra.intro !== void 0) {
      fieldset.append(extra.intro);
    }
    const fields = element(doc, "div");
    fields.dataset["choiceFields"] = part;
    const radios = [];
    for (const option of options) {
      const line = element(doc, "label", VISUAL_CLASSES.siteChoice);
      const radio = doc.createElement("input");
      radio.type = "radio";
      radio.name = `${idPrefix}-choice-${part}`;
      radio.value = option.value;
      radio.dataset["choice"] = option.value;
      radio.checked = get() === option.value;
      radio.addEventListener("change", () => {
        if (radio.checked) {
          set(option.value);
        }
      });
      disabledWhenPending.push(radio);
      radios.push(radio);
      line.append(radio, doc.createTextNode(option.text ?? translate(language, option.label)));
      fieldset.append(line);
      if (extra.fieldsAfter === option.value) {
        fieldset.append(fields);
      }
    }
    const note = element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, "entity.choice.mixed"));
    note.dataset["choiceNote"] = part;
    note.hidden = true;
    fieldset.append(note);
    if (fields.parentElement === null) {
      fieldset.append(fields);
    }
    return {
      fieldset,
      fields,
      sync() {
        for (const radio of radios) {
          radio.checked = get() === radio.value;
        }
      },
      showNote(mixed) {
        note.hidden = !mixed;
      }
    };
  }
  function keepErrorWith(group, name) {
    const entry = errorNodes.get(name);
    if (entry !== void 0) {
      group.fieldset.append(entry.node);
    }
  }
  const fieldHelp = (name, key) => {
    const help = element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, key));
    help.dataset["help"] = name;
    return help;
  };
  const clearers = [];
  const isSet = (name) => (values[name] ?? "").trim() !== "";
  const isOn = (name) => values[name] === "true";
  if (scope === "charger") {
    const limitField = config.fields.find((entry) => entry.field === "current_limit");
    if (limitField !== void 0 && limitField.kind === "entity" && limitField.writable && managed.has("current_limit")) {
      const automatic = automaticEntity(limitField);
      const noneChosen = limitField.none !== null && limitField.none.chosen;
      const noneOffered = limitField.none !== null && (limitField.none.allowed || noneChosen);
      if (values["current_limit"] === NONE_VALUE) {
        values["current_limit"] = "";
      }
      const impliedNone = !noneChosen && automatic === null && limitField.current === null;
      let limitKind = noneChosen ? "none" : limitField.current !== null ? "choose" : automatic !== null ? "automatic" : "choose";
      const options = [];
      if (automatic !== null) {
        options.push({
          value: "automatic",
          label: "entity.automatic",
          text: translate(language, "entity.automatic", { name: automatic.friendlyName })
        });
      }
      options.push({ value: "choose", label: "entity.choice.choose" });
      if (noneOffered) {
        options.push({ value: "none", label: "entity.limit.none" });
      }
      const paintLimit = () => {
        limitGroup.fields.replaceChildren(...limitKind === "choose" ? blocksOf("current_limit") : []);
        applyPending();
      };
      const limitGroup = choiceGroup(
        "current-limit",
        "entity.field.currentLimit",
        options,
        () => limitKind,
        (value) => {
          limitKind = value;
          paintLimit();
        },
        { intro: fieldHelp("current_limit", "entity.help.currentLimit"), fieldsAfter: "choose" }
      );
      clearers.push((draft) => {
        if (limitKind === "automatic") {
          draft["current_limit"] = "";
        } else if (limitKind === "none") {
          draft["current_limit"] = impliedNone ? "" : NONE_VALUE;
        }
      });
      keepErrorWith(limitGroup, "current_limit");
      const restrictions = config.control === null ? [] : currentRestrictions(language, config.control);
      if (restrictions.length > 0) {
        const line = element(doc, "p", VISUAL_CLASSES.entityHelp, restrictions.join(" "));
        line.dataset["limitRestrictions"] = "current";
        limitGroup.fieldset.querySelector("[data-help='current_limit']")?.after(line);
      }
      body.append(limitGroup.fieldset);
      paintLimit();
    }
    const hasRegister = managed.has("energy_register_entity");
    const hasPlug = managed.has("power_entity");
    const registerField = config.fields.find((entry) => entry.field === "energy_register_entity");
    const found = registerField !== void 0 && registerField.kind === "entity" && automaticEntity(registerField) !== null;
    const energyKind = () => isSet("energy_register_entity") || found ? "meter" : hasPlug && isSet("power_entity") ? "power" : "none";
    let energy = energyKind();
    const energyMixed = isSet("energy_register_entity") && isSet("power_entity");
    if (hasRegister || hasPlug) {
      const options = [];
      if (hasRegister) {
        options.push({ value: "meter", label: "entity.energy.meter" });
      }
      if (hasPlug) {
        options.push({ value: "power", label: "entity.energy.power" });
      }
      options.push({ value: "none", label: "entity.energy.none" });
      let registerKind = isSet("energy_register_entity") ? "choose" : "automatic";
      const paintRegister = () => {
        registerGroup?.fields.replaceChildren(...registerKind === "choose" ? blocksOf("energy_register_entity") : []);
        applyPending();
      };
      const foundRegister = registerField !== void 0 && registerField.kind === "entity" ? automaticEntity(registerField) : null;
      const registerGroup = hasRegister && foundRegister !== null ? choiceGroup(
        "energy-source",
        null,
        [
          {
            value: "automatic",
            label: "entity.automatic",
            text: translate(language, "entity.automatic", { name: foundRegister.friendlyName })
          },
          { value: "choose", label: "entity.choice.choose" }
        ],
        () => registerKind,
        (value) => {
          registerKind = value;
          paintRegister();
        },
        { intro: fieldHelp("energy_register_entity", "entity.help.energyRegister"), fieldsAfter: "choose" }
      ) : null;
      if (registerGroup !== null) {
        keepErrorWith(registerGroup, "energy_register_entity");
        paintRegister();
      }
      const paintEnergy = () => {
        group.fields.replaceChildren(
          ...energy === "meter" ? registerGroup !== null ? [registerGroup.fieldset] : blocksOf("energy_register_entity") : energy === "power" ? blocksOf("power_entity") : []
        );
        applyPending();
      };
      const group = choiceGroup("energy", "entity.energy.title", options, () => energy, (value) => {
        energy = value;
        paintEnergy();
      });
      group.showNote(energyMixed);
      clearers.push((draft) => {
        if (energy !== "meter" || registerGroup !== null && registerKind === "automatic") {
          draft["energy_register_entity"] = "";
        }
        if (energy !== "power") {
          draft["power_entity"] = "";
        }
      });
      body.append(group.fieldset);
      paintEnergy();
    }
  }
  if (scope === "site") {
    const modeField = fieldsOf(config, "site").find((entry) => entry.field === "measurement_mode");
    if (modeField !== void 0 && modeField.kind === "enum") {
      const fieldset = element(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
      fieldset.dataset["fieldBlock"] = "measurement_mode";
      fieldset.append(element(doc, "legend", VISUAL_CLASSES.siteLegend, labelOf(language, "measurement_mode")));
      const modeHelp = element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, "entity.help.measurementMode"));
      modeHelp.dataset["help"] = "measurement_mode";
      fieldset.append(modeHelp);
      const radioName = `${idPrefix}-mode`;
      const head = element(doc, "div");
      head.dataset["part"] = "head";
      const phases = element(doc, "div", VISUAL_CLASSES.entityMeters);
      phases.dataset["part"] = "phases";
      const tail = element(doc, "div");
      tail.dataset["part"] = "tail";
      const currentMode = () => values["measurement_mode"] ?? storedMode(config) ?? MEASUREMENT_DIRECT;
      const phaseValue = (name) => values[name] ?? phaseField(config, name).current?.entityId ?? "";
      const anyPhase = (kind) => PHASES.some((phase) => phaseValue(derivedFieldName(phase, kind)).trim() !== "");
      const CURRENT_KINDS = [
        ["measured", "current"],
        ["apparent", "apparent_power"],
        ["reactive", "reactive_power"]
      ];
      const usedKinds = CURRENT_KINDS.filter(([, kind]) => anyPhase(kind));
      let currentKind = usedKinds[0]?.[0] ?? "estimated";
      const currentMixed = usedKinds.length > 1;
      const gridFromValues = () => (currentMode() === MEASUREMENT_DERIVED ? anyPhase("power_export") : phaseValue(GRID_TOTAL_FIELDS[1]).trim() !== "") ? "two" : "one";
      let gridKind = gridFromValues();
      let batteryKind = isSet("battery_discharge_power_entity") ? "two" : isSet("battery_aggregate_power_entity") ? "one" : "none";
      const paintPhases = () => {
        for (const name of phaseFieldNames(MEASUREMENT_DIRECT).concat(phaseFieldNames(MEASUREMENT_DERIVED))) {
          errorNodes.delete(name);
        }
        for (const picker of [...pickers]) {
          if (picker.dataset["field"] !== void 0 && isPhaseField(picker.dataset["field"])) {
            pickers.delete(picker);
          }
        }
        phases.replaceChildren();
        const mode = currentMode();
        phases.dataset["mode"] = mode;
        const derived = mode === MEASUREMENT_DERIVED;
        const currentName = CURRENT_KINDS.find(([choice]) => choice === currentKind)?.[1] ?? null;
        const phaseHelp = element(doc, "div");
        phaseHelp.dataset["help"] = "phases";
        if (!derived) {
          phaseHelp.append(element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, "entity.help.phaseDirect")));
        }
        for (const phase of PHASES) {
          const group = element(doc, "fieldset", VISUAL_CLASSES.entityLine);
          group.dataset["phase"] = phase;
          group.append(element(doc, "legend", VISUAL_CLASSES.siteLegend, phase));
          const cells = element(doc, "div", VISUAL_CLASSES.entityLineCells);
          const names = derived ? [
            ...DERIVED_REQUIRED_KINDS.map((kind) => derivedFieldName(phase, kind)),
            ...gridKind === "two" ? [derivedFieldName(phase, "power_export")] : [],
            ...currentName === null ? [] : [derivedFieldName(phase, currentName)]
          ] : [directFieldName(phase)];
          for (const name of names) {
            const field2 = phaseField(config, name);
            if (values[name] === void 0) {
              values[name] = field2.current === null ? "" : field2.current.entityId;
            }
            cells.append(entityField(field2, labelOf(language, name)));
          }
          group.append(cells);
          phases.append(group);
        }
        phases.append(phaseHelp);
        applyPending();
      };
      const grid = choiceGroup(
        "grid",
        "entity.grid.title",
        [
          { value: "one", label: "entity.grid.one" },
          { value: "two", label: "entity.grid.two" }
        ],
        () => gridKind,
        (value) => {
          gridKind = value;
          paintGrid();
          if (currentMode() === MEASUREMENT_DERIVED) {
            paintPhases();
          }
        }
      );
      const signedShown = () => currentMode() !== MEASUREMENT_DERIVED || currentKind === "measured";
      const paintGrid = () => {
        const derived = currentMode() === MEASUREMENT_DERIVED;
        const help = element(
          doc,
          "p",
          VISUAL_CLASSES.entityHelp,
          [
            translate(language, "entity.help.derivedPower"),
            translate(language, "entity.help.derivedVoltage"),
            ...gridKind === "two" ? [translate(language, "entity.help.derivedPowerExport")] : []
          ].join(" ")
        );
        help.dataset["help"] = "grid-phases";
        grid.fields.replaceChildren(
          ...derived ? [help] : blocksOf(GRID_TOTAL_FIELDS[0], ...gridKind === "two" ? [GRID_TOTAL_FIELDS[1]] : []),
          ...gridKind === "one" ? blocksOf("grid_power_inverted") : []
        );
        grid.showNote(gridKind === "two" && isOn("grid_power_inverted"));
        applyPending();
      };
      const current = choiceGroup(
        "current-source",
        "entity.current.title",
        [
          { value: "measured", label: "entity.current.measured" },
          { value: "apparent", label: "entity.current.apparent" },
          { value: "reactive", label: "entity.current.reactive" },
          { value: "estimated", label: "entity.current.estimated" }
        ],
        () => currentKind,
        (value) => {
          currentKind = value;
          paintCurrent();
          paintPhases();
        }
      );
      current.showNote(currentMixed);
      const paintCurrent = () => {
        const kind = CURRENT_KINDS.find(([choice]) => choice === currentKind)?.[1] ?? null;
        const helpKey = kind === null ? "entity.current.estimatedNote" : fieldHelpKey(derivedFieldName("L1", kind));
        const help = element(doc, "p", VISUAL_CLASSES.entityHelp, helpKey === null ? "" : translate(language, helpKey));
        help.dataset["help"] = kind === null ? "current-estimated" : "current-source";
        current.fields.replaceChildren(help, ...signedShown() ? blocksOf("site_current_signed") : []);
      };
      const battery = choiceGroup(
        "battery",
        "entity.battery.title",
        [
          { value: "none", label: "entity.battery.none" },
          { value: "one", label: "entity.battery.one" },
          { value: "two", label: "entity.battery.two" }
        ],
        () => batteryKind,
        (value) => {
          batteryKind = value;
          paintBattery();
        }
      );
      const paintBattery = () => {
        battery.fields.replaceChildren(
          ...batteryKind === "none" ? [] : blocksOf("battery_aggregate_power_entity"),
          ...batteryKind === "two" ? blocksOf("battery_discharge_power_entity") : [],
          ...batteryKind === "one" ? blocksOf("battery_power_inverted") : []
        );
        battery.showNote(batteryKind !== "one" && isOn("battery_power_inverted"));
        applyPending();
      };
      const layoutMode = () => {
        const derived = currentMode() === MEASUREMENT_DERIVED;
        gridKind = gridFromValues();
        grid.sync();
        paintGrid();
        paintCurrent();
        head.replaceChildren(...derived ? [grid.fieldset, current.fieldset] : []);
        tail.replaceChildren(
          ...derived ? [] : [grid.fieldset, ...blocksOf("site_current_signed")],
          battery.fieldset,
          ...blocksOf("max_age_s")
        );
        paintPhases();
      };
      clearers.push((draft) => {
        const derived = currentMode() === MEASUREMENT_DERIVED;
        if (derived) {
          const keep = CURRENT_KINDS.find(([choice]) => choice === currentKind)?.[1] ?? null;
          for (const phase of PHASES) {
            for (const [, kind] of CURRENT_KINDS) {
              if (kind !== keep) {
                draft[derivedFieldName(phase, kind)] = "";
              }
            }
            if (gridKind === "one") {
              draft[derivedFieldName(phase, "power_export")] = "";
            }
          }
        } else if (gridKind === "one") {
          draft[GRID_TOTAL_FIELDS[1]] = "";
        }
        if (gridKind === "two") {
          draft["grid_power_inverted"] = "false";
        }
        if (!signedShown()) {
          draft["site_current_signed"] = "false";
        }
        if (batteryKind === "none") {
          draft["battery_aggregate_power_entity"] = "";
        }
        if (batteryKind !== "two") {
          draft["battery_discharge_power_entity"] = "";
        }
        if (batteryKind !== "one") {
          draft["battery_power_inverted"] = "false";
        }
      });
      for (const choice of modeField.choices) {
        const label = element(doc, "label", VISUAL_CLASSES.siteChoice);
        const radio = doc.createElement("input");
        radio.type = "radio";
        radio.name = radioName;
        radio.value = choice;
        radio.checked = currentMode() === choice;
        radio.dataset["mode"] = choice;
        radio.addEventListener("change", () => {
          if (radio.checked) {
            values["measurement_mode"] = choice;
            layoutMode();
          }
        });
        disabledWhenPending.push(radio);
        label.append(radio, doc.createTextNode(modeLabel(language, choice)));
        fieldset.append(label);
      }
      const modeError = element(doc, "p", VISUAL_CLASSES.settingsError);
      modeError.hidden = true;
      modeError.dataset["fieldError"] = "measurement_mode";
      modeError.setAttribute("role", "alert");
      errorNodes.set("measurement_mode", { node: modeError, input: fieldset });
      fieldset.append(modeError);
      body.append(fieldset, head, phases, tail);
      paintBattery();
      layoutMode();
    }
  }
  const voltageField = fieldsOf(config, scope).find((entry) => entry.field === "voltage_between_phases_v");
  if (voltageField !== void 0 && voltageField.kind === "enum" && voltageField.writable) {
    const voltageOptions = voltageField.choices.map((choice) => ({
      value: choice,
      label: choice === "230" ? "entity.voltage.it" : "entity.voltage.tn"
    }));
    const voltage = choiceGroup(
      "voltage",
      "entity.field.voltageBetweenPhases",
      voltageOptions,
      () => values["voltage_between_phases_v"] ?? voltageField.value ?? "400",
      (value) => {
        values["voltage_between_phases_v"] = value;
      },
      { intro: fieldHelp("voltage_between_phases_v", "entity.help.voltageBetweenPhases") }
    );
    const voltageError = element(doc, "p", VISUAL_CLASSES.settingsError);
    voltageError.hidden = true;
    voltageError.dataset["fieldError"] = "voltage_between_phases_v";
    voltageError.setAttribute("role", "alert");
    errorNodes.set("voltage_between_phases_v", { node: voltageError, input: voltage.fieldset });
    voltage.fieldset.append(voltageError);
    body.append(voltage.fieldset);
  }
  const priorityField = fieldsOf(config, scope).find((entry) => entry.field === "charger_priority");
  if (priorityField !== void 0 && priorityField.kind === "enum" && priorityField.writable) {
    const priorityOptions = priorityField.choices.map((choice) => ({
      value: choice,
      label: choice === "first" ? "entity.priority.first" : choice === "last" ? "entity.priority.last" : "entity.priority.normal"
    }));
    const priority = choiceGroup(
      "priority",
      "entity.field.chargerPriority",
      priorityOptions,
      () => values["charger_priority"] ?? priorityField.value ?? "normal",
      (value) => {
        values["charger_priority"] = value;
      },
      { intro: fieldHelp("charger_priority", "entity.help.chargerPriority") }
    );
    const priorityError = element(doc, "p", VISUAL_CLASSES.settingsError);
    priorityError.hidden = true;
    priorityError.dataset["fieldError"] = "charger_priority";
    priorityError.setAttribute("role", "alert");
    errorNodes.set("charger_priority", { node: priorityError, input: priority.fieldset });
    priority.fieldset.append(priorityError);
    body.append(priority.fieldset);
  }
  const actions = element(doc, "div", VISUAL_CLASSES.settingsActions);
  const save = element(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSave}`, translate(language, "settings.save"));
  save.type = "submit";
  const cancel = element(doc, "button", VISUAL_CLASSES.button, translate(language, "settings.cancel"));
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  disabledWhenPending.push(save, cancel);
  keepEnabled.add(cancel);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending && !locked) {
      handlers.onSave(resolvedDraft());
    }
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });
  function resolvedDraft() {
    const draft = { ...values };
    for (const clear of clearers) {
      clear(draft);
    }
    return draft;
  }
  function applyPending() {
    for (const control of disabledWhenPending) {
      control.disabled = pending || locked && !keepEnabled.has(control);
    }
    for (const picker of pickers) {
      picker.disabled = pending || locked;
    }
  }
  applyPending();
  return {
    body,
    draft: resolvedDraft,
    markErrors(errors) {
      for (const [, entry] of errorNodes) {
        entry.node.hidden = true;
        entry.node.textContent = "";
        entry.input.removeAttribute("aria-invalid");
        entry.input.removeAttribute("aria-describedby");
      }
      for (const error of errors) {
        const entry = errorNodes.get(error.field);
        if (entry === void 0) {
          continue;
        }
        const key = fieldErrorKey(error.code);
        entry.node.hidden = false;
        entry.node.textContent = translate(language, key);
        entry.node.dataset["code"] = error.code;
        entry.input.setAttribute("aria-invalid", "true");
        if (entry.node.id !== "") {
          entry.input.setAttribute("aria-describedby", entry.node.id);
        }
      }
    },
    setNotice(text5, code) {
      notice.hidden = text5 === null;
      notice.textContent = text5 ?? "";
      if (code === null) {
        notice.removeAttribute("data-code");
      } else {
        notice.dataset["code"] = code;
      }
    },
    setPending(next) {
      pending = next;
      applyPending();
    },
    setHass(hass) {
      for (const picker of pickers) {
        picker.hass = hass;
      }
    }
  };
}
function vehicleEditorBody(doc, language, input, handlers, idPrefix) {
  const { row, sensor } = input;
  const values = {};
  const controls = [];
  const errorNodes = /* @__PURE__ */ new Map();
  let pending = false;
  const body = element(doc, "form");
  body.noValidate = true;
  body.dataset["entityEditor"] = "vehicle";
  body.dataset["vehicle"] = input.vehicleId;
  const notice = element(doc, "p", VISUAL_CLASSES.settingsNotice);
  notice.setAttribute("role", "status");
  notice.hidden = true;
  body.append(notice);
  const errorFor = (name, control) => {
    const node = element(doc, "p", VISUAL_CLASSES.settingsError);
    node.hidden = true;
    node.dataset["fieldError"] = name;
    node.setAttribute("role", "alert");
    errorNodes.set(name, { node, input: control });
    return node;
  };
  if (sensor !== null) {
    values["soc"] = vehicleChoice(sensor);
    const group = element(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
    group.dataset["part"] = "soc";
    group.append(element(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "settings.vehicle.sensorLegend")));
    const radioName = `${idPrefix}-vehicle-soc`;
    const choose = (value, label, title) => {
      const line = element(doc, "label", VISUAL_CLASSES.siteChoice);
      const radio = doc.createElement("input");
      radio.type = "radio";
      radio.name = radioName;
      radio.value = value;
      radio.checked = values["soc"] === value;
      radio.dataset["vehicleChoice"] = value === "" ? "automatic" : value;
      radio.addEventListener("change", () => {
        if (radio.checked) {
          values["soc"] = value;
        }
      });
      controls.push(radio);
      line.append(radio, doc.createTextNode(label));
      if (title !== null) {
        line.title = title;
      }
      group.append(line);
    };
    for (const candidate of sensor.candidates) {
      choose(candidate.entityId, candidate.friendlyName, candidate.entityId);
    }
    choose("", translate(language, "entity.vehicle.automatic"), null);
    if (sensor.candidates.length > 1 && sensor.selected === null) {
      group.append(element(doc, "p", VISUAL_CLASSES.entityHelp, translate(language, "entity.vehicle.several")));
    }
    group.append(errorFor("vehicle_soc", group));
    body.append(group);
  }
  const numberField = (name, errorField, labelKey, unit, current, range) => {
    const block = element(doc, "div", VISUAL_CLASSES.settingsField);
    block.dataset["part"] = name;
    const id = `${idPrefix}-vehicle-${name}`;
    const control = doc.createElement("input");
    control.type = "number";
    control.step = "0.1";
    control.min = String(range.min);
    control.max = String(range.max);
    control.inputMode = "decimal";
    control.className = VISUAL_CLASSES.settingsInput;
    control.id = id;
    values[name] = current === null ? "" : current.toFixed(1);
    control.value = values[name] ?? "";
    control.placeholder = current === null ? translate(language, "settings.capacity.unset") : "";
    control.addEventListener("input", () => {
      values[name] = control.value;
    });
    controls.push(control);
    const label = element(doc, "label", VISUAL_CLASSES.settingsLabel, translate(language, labelKey));
    label.setAttribute("for", id);
    const line = element(doc, "div", VISUAL_CLASSES.settingsRow);
    line.append(control, element(doc, "span", VISUAL_CLASSES.settingsUnit, unit));
    block.append(label, line, errorFor(errorField, control));
    body.append(block);
  };
  if (row === null) {
  } else if (row.capacity_source === "reported" && row.capacity_kwh !== null) {
    const line = element(doc, "div", VISUAL_CLASSES.capabilityItem);
    line.dataset["row"] = "capacity";
    line.append(
      element(doc, "span", VISUAL_CLASSES.capabilityLabel, translate(language, "settings.capacity.label")),
      element(doc, "span", VISUAL_CLASSES.settingsValue, `${formatFixed(language, row.capacity_kwh, 1)} kWh`)
    );
    body.append(line, element(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.vehicle.capacityReported")));
  } else {
    numberField("capacity", "capacity_kwh", "settings.capacity.label", "kWh", row.capacity_kwh, {
      min: CAPACITY_MIN_KWH,
      max: CAPACITY_MAX_KWH
    });
  }
  if (row !== null) {
    numberField(
      "consumption",
      "consumption_kwh_per_10km",
      "settings.consumption.label",
      translate(language, "settings.consumption.unit"),
      row.consumption_kwh_per_10km,
      { min: CONSUMPTION_MIN_KWH_PER_10KM, max: CONSUMPTION_MAX_KWH_PER_10KM }
    );
  }
  const actions = element(doc, "div", VISUAL_CLASSES.settingsActions);
  const save = element(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSave}`, translate(language, "settings.save"));
  save.type = "submit";
  const cancel = element(doc, "button", VISUAL_CLASSES.button, translate(language, "settings.cancel"));
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  controls.push(save, cancel);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending) {
      handlers.onSave({ ...values });
    }
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });
  return {
    body,
    draft: () => ({ ...values }),
    markErrors(errors) {
      for (const [, entry] of errorNodes) {
        entry.node.hidden = true;
        entry.node.textContent = "";
        entry.node.removeAttribute("data-code");
        entry.input.removeAttribute("aria-invalid");
      }
      for (const error of errors) {
        const entry = errorNodes.get(error.field);
        if (entry === void 0) {
          continue;
        }
        entry.node.hidden = false;
        entry.node.textContent = translate(language, fieldErrorKey(error.code));
        entry.node.dataset["code"] = error.code;
        entry.input.setAttribute("aria-invalid", "true");
      }
    },
    setNotice(text5, code) {
      notice.hidden = text5 === null;
      notice.textContent = text5 ?? "";
      if (code === null) {
        notice.removeAttribute("data-code");
      } else {
        notice.dataset["code"] = code;
      }
    },
    setPending(next) {
      pending = next;
      for (const control of controls) {
        control.disabled = pending;
      }
    },
    setHass() {
    }
  };
}

// src/solar-editor.ts
var SOLAR_PRIORITY_KEY = "priority";
var SOLAR_FORECAST_PREFIX = "forecast:";
function solarDraftFrom(site) {
  const draft = { [SOLAR_PRIORITY_KEY]: site.solarPriority };
  for (const choice of site.solarForecastChoices) {
    draft[`${SOLAR_FORECAST_PREFIX}${choice.id}`] = String(choice.selected);
  }
  return draft;
}
function element2(doc, tag, className, text5) {
  const node = doc.createElement(tag);
  if (className !== void 0) {
    node.className = className;
  }
  if (text5 !== void 0) {
    node.textContent = text5;
  }
  return node;
}
function solarEditorBody(doc, language, site, handlers, idPrefix) {
  const values = solarDraftFrom(site);
  const controls = [];
  let pending = false;
  const body = element2(doc, "form");
  body.noValidate = true;
  body.dataset["entityEditor"] = "solar";
  const notice = element2(doc, "p", VISUAL_CLASSES.settingsNotice);
  notice.setAttribute("role", "status");
  notice.hidden = true;
  body.append(notice);
  body.append(element2(doc, "p", VISUAL_CLASSES.siteApplies, site.appliesToText));
  const priority = element2(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
  priority.dataset["part"] = "priority";
  priority.append(element2(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "site.solarPriority.title")));
  for (const value of ["car_first", "battery_first"]) {
    const label = element2(doc, "label", VISUAL_CLASSES.siteChoice);
    const radio = doc.createElement("input");
    radio.type = "radio";
    radio.name = `${idPrefix}-solar-priority`;
    radio.value = value;
    radio.checked = site.solarPriority === value;
    radio.addEventListener("change", () => {
      if (radio.checked) {
        values[SOLAR_PRIORITY_KEY] = value;
      }
    });
    controls.push(radio);
    label.append(
      radio,
      doc.createTextNode(translate(language, value === "car_first" ? "site.solarPriority.carFirst" : "site.solarPriority.batteryFirst"))
    );
    priority.append(label);
  }
  body.append(priority);
  const forecast = element2(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
  forecast.dataset["part"] = "forecast";
  forecast.append(element2(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "site.solarForecast.title")));
  const none = element2(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "site.solarForecast.none"));
  const paintNone = () => {
    none.hidden = !site.solarForecastChoices.every((choice) => values[`${SOLAR_FORECAST_PREFIX}${choice.id}`] !== "true");
  };
  for (const choice of site.solarForecastChoices) {
    const label = element2(doc, "label", VISUAL_CLASSES.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.checked = choice.selected;
    box.dataset["forecast"] = choice.id;
    box.addEventListener("change", () => {
      values[`${SOLAR_FORECAST_PREFIX}${choice.id}`] = String(box.checked);
      paintNone();
    });
    controls.push(box);
    label.append(box, doc.createTextNode(choice.title));
    forecast.append(label);
  }
  forecast.append(none);
  paintNone();
  body.append(forecast);
  const actions = element2(doc, "div", VISUAL_CLASSES.settingsActions);
  const save = element2(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSave}`, translate(language, "settings.save"));
  save.type = "submit";
  const cancel = element2(doc, "button", VISUAL_CLASSES.button, translate(language, "settings.cancel"));
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  controls.push(save, cancel);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending) {
      handlers.onSave({ ...values });
    }
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });
  return {
    body,
    draft: () => ({ ...values }),
    markErrors(_errors) {
    },
    setNotice(text5, code) {
      notice.hidden = text5 === null;
      notice.textContent = text5 ?? "";
      if (code === null) {
        notice.removeAttribute("data-code");
      } else {
        notice.dataset["code"] = code;
      }
    },
    setPending(next) {
      pending = next;
      for (const control of controls) {
        control.disabled = pending;
      }
    },
    setHass() {
    }
  };
}

// src/market.ts
var MalformedPayload3 = class extends Error {
};
function bad4() {
  throw new MalformedPayload3("malformed");
}
function isRecord4(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function record4(value) {
  return isRecord4(value) ? value : bad4();
}
function exactKeys4(source, keys) {
  if (Object.keys(source).length !== keys.length) {
    return bad4();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      return bad4();
    }
  }
}
function identity(source, key) {
  const value = source[key];
  return typeof value === "string" && value.length > 0 ? value : bad4();
}
function identityOrNull(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" && value.length > 0 ? value : bad4();
}
function figureOrNull(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad4();
}
function oneOf3(source, key, allowed) {
  const value = source[key];
  if (typeof value !== "string" || !allowed.includes(value)) {
    return bad4();
  }
  return value;
}
var OPTIONS_KEYS = ["api_version", "state", "reason", "areas", "configured_area"];
var AREA_KEYS = [
  "area_id",
  "name",
  "countries",
  "timezone",
  "currency",
  "major_unit",
  "minor_unit",
  "suggestions"
];
var SUGGESTION_KEYS = ["vat_percent", "tax_minor", "transfer_minor"];
function decodeSuggestions(source) {
  exactKeys4(source, SUGGESTION_KEYS);
  return {
    vat_percent: figureOrNull(source, "vat_percent"),
    tax_minor: figureOrNull(source, "tax_minor"),
    transfer_minor: figureOrNull(source, "transfer_minor")
  };
}
function decodeArea(source) {
  exactKeys4(source, AREA_KEYS);
  const countries = source["countries"];
  if (!Array.isArray(countries)) {
    return bad4();
  }
  return {
    area_id: identity(source, "area_id"),
    name: identity(source, "name"),
    // The list may be empty; that is a fact about the area, not a reason to invent a value.
    countries: countries.map(
      (country) => typeof country === "string" && country.length > 0 ? country : bad4()
    ),
    timezone: identity(source, "timezone"),
    currency: identity(source, "currency"),
    major_unit: identity(source, "major_unit"),
    minor_unit: identity(source, "minor_unit"),
    suggestions: decodeSuggestions(record4(source["suggestions"]))
  };
}
function decodeMarketOptions(raw) {
  try {
    if (!isRecord4(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const version = raw["api_version"];
    if (typeof version === "number" && version !== MARKET_API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys4(raw, OPTIONS_KEYS);
    if (version !== MARKET_API_VERSION) {
      return { ok: false, failure: "malformed" };
    }
    const state = oneOf3(raw, "state", MARKET_STATES);
    const reasonValue = raw["reason"];
    const reason = reasonValue === null ? null : oneOf3(raw, "reason", MARKET_REASONS);
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
    const areas = rawAreas.map((area) => decodeArea(record4(area)));
    const seen = /* @__PURE__ */ new Set();
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
        configured_area: identityOrNull(raw, "configured_area")
      }
    };
  } catch (error) {
    if (error instanceof MalformedPayload3) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}
var FISCAL_COMPONENTS = ["vat", "tax", "transfer"];
function setEnabled(value, enabled) {
  return { ...value, enabled };
}
function figureEdited(value, text5) {
  return { enabled: value.enabled, value: text5, intent: "custom" };
}
function resetToSuggestion(value) {
  return { enabled: value.enabled, value: "", intent: "suggested" };
}
function fiscalFormFrom(row, component) {
  const fiscal = row === null ? null : row[component];
  const stored = fiscal === null ? null : fiscal.value;
  return {
    enabled: fiscal !== null && fiscal.enabled,
    value: stored === null ? "" : String(stored),
    intent: stored === null ? "suggested" : "custom"
  };
}
function marketFormFor(record7, areaId) {
  const row = areaId === null ? null : record7.overrides.find((item) => item.area_id === areaId) ?? null;
  return {
    areaId,
    vat: fiscalFormFrom(row, "vat"),
    tax: fiscalFormFrom(row, "tax"),
    transfer: fiscalFormFrom(row, "transfer")
  };
}
function switchArea(record7, drafts, current, areaId) {
  const next = { ...drafts };
  if (current !== null && current.areaId !== null) {
    next[current.areaId] = {
      vat: { ...current.vat },
      tax: { ...current.tax },
      transfer: { ...current.transfer }
    };
  }
  const remembered = areaId === null ? void 0 : next[areaId];
  if (remembered === void 0) {
    return { drafts: next, values: marketFormFor(record7, areaId) };
  }
  return {
    drafts: next,
    values: {
      areaId,
      vat: { ...remembered.vat },
      tax: { ...remembered.tax },
      transfer: { ...remembered.transfer }
    }
  };
}
function marketSuggestion(options, areaId, component) {
  const area = areaId === null ? null : options.areas.find((item) => item.area_id === areaId) ?? null;
  if (area === null) {
    return null;
  }
  if (component === "vat") {
    return area.suggestions.vat_percent;
  }
  return component === "tax" ? area.suggestions.tax_minor : area.suggestions.transfer_minor;
}
function fiscalUnit(options, areaId, component) {
  if (component === "vat") {
    return "%";
  }
  const area = areaId === null ? null : options.areas.find((item) => item.area_id === areaId) ?? null;
  return area === null ? "" : area.minor_unit;
}
function marketAreaIsKnown(options, base, areaId) {
  return areaId === base.area_id || options.areas.some((area) => area.area_id === areaId);
}
function checkCustomFigure(text5) {
  const trimmed = text5.trim().replace(",", ".");
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
function retainedFigure(text5) {
  const trimmed = text5.trim().replace(",", ".");
  if (trimmed === "") {
    return null;
  }
  const kept = Number(trimmed);
  return Number.isFinite(kept) ? kept : null;
}
function fiscalStated(options, areaId, component, value) {
  if (value.intent === "suggested") {
    if (!value.enabled) {
      return { ok: true, value: { enabled: false, value: null } };
    }
    if (marketSuggestion(options, areaId, component) === null) {
      const typed2 = checkCustomFigure(value.value);
      return typed2.ok ? { ok: true, value: { enabled: true, value: typed2.value } } : typed2;
    }
    return { ok: true, value: { enabled: true, value: null } };
  }
  if (!value.enabled) {
    return { ok: true, value: { enabled: false, value: retainedFigure(value.value) } };
  }
  const typed = checkCustomFigure(value.value);
  return typed.ok ? { ok: true, value: { enabled: true, value: typed.value } } : typed;
}
function sameFiscal(left, right) {
  return left.enabled === right.enabled && left.value === right.value;
}
function sameRow(left, right) {
  return left.area_id === right.area_id && sameFiscal(left.vat, right.vat) && sameFiscal(left.tax, right.tax) && sameFiscal(left.transfer, right.transfer);
}
function rowStatesNothing(row) {
  for (const component of FISCAL_COMPONENTS) {
    const fiscal = row[component];
    if (fiscal.enabled || fiscal.value !== null) {
      return false;
    }
  }
  return true;
}
function marketReplacement(base, values, options) {
  const areaId = values.areaId;
  if (areaId === null) {
    return { ok: false, errorKey: "market.error.areaRequired" };
  }
  if (!marketAreaIsKnown(options, base, areaId)) {
    return { ok: false, errorKey: "market.error.areaUnknown" };
  }
  const components = {
    vat: { enabled: false, value: null },
    tax: { enabled: false, value: null },
    transfer: { enabled: false, value: null }
  };
  for (const component of FISCAL_COMPONENTS) {
    const stated = fiscalStated(options, areaId, component, values[component]);
    if (!stated.ok) {
      return stated;
    }
    components[component] = stated.value;
  }
  const row = {
    area_id: areaId,
    vat: components.vat,
    tax: components.tax,
    transfer: components.transfer
  };
  const existing = base.overrides.find((item) => item.area_id === areaId) ?? null;
  const changed = areaId !== base.area_id ? true : existing === null ? !rowStatesNothing(row) : !sameRow(existing, row);
  const overrides = existing === null && rowStatesNothing(row) ? base.overrides : existing === null ? [...base.overrides, row] : base.overrides.map((item) => item.area_id === areaId ? row : item);
  return {
    ok: true,
    body: { ...encodeBody(base), area_id: areaId, overrides },
    changed
  };
}
var MARKET_EDITABLE_STATES = ["ready", "stale"];
function marketEditable(state, isAdmin) {
  return isAdmin && MARKET_EDITABLE_STATES.includes(state);
}
function marketAreaLabel(language, name, id) {
  const hasName = name !== null && name !== "";
  const hasId = id !== null && id !== "";
  if (!hasName && !hasId) {
    return translate(language, "market.unset");
  }
  if (!hasName) {
    return id;
  }
  if (!hasId || name.includes(id)) {
    return name;
  }
  return `${name} · ${id}`;
}
function marketStateKey(state, reason) {
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
function groupAreasForPicker(areas, region) {
  const local = region === null ? "" : region.trim().toUpperCase();
  const upper = (area) => area.countries.map((country) => country.toUpperCase());
  const covers = (area) => local !== "" && upper(area).includes(local);
  const ordered = [...areas.filter(covers), ...areas.filter((area) => !covers(area))];
  const groups = [];
  const ungrouped = [];
  for (const area of ordered) {
    const countries = upper(area);
    const heading = covers(area) ? local : countries[0];
    if (heading === void 0) {
      ungrouped.push(area);
      continue;
    }
    const group = groups.find((entry) => entry.heading === heading);
    if (group === void 0) {
      groups.push({ heading, areas: [area] });
    } else {
      group.areas.push(area);
    }
  }
  return { groups, ungrouped };
}
function countryLabel(language, country) {
  const code = country.trim().toUpperCase();
  try {
    const name = new Intl.DisplayNames([language], { type: "region" }).of(code);
    return name === void 0 || name.trim() === "" || name.toUpperCase() === code ? code : name;
  } catch {
    return code;
  }
}

// src/market-editor.ts
function element3(doc, tag, className, text5) {
  const node = doc.createElement(tag);
  if (className !== void 0) {
    node.className = className;
  }
  if (text5 !== void 0) {
    node.textContent = text5;
  }
  return node;
}
function componentKey(component, suffix) {
  return `market.${component}.${suffix}`;
}
function marketIcon(doc) {
  const ns = "http://www.w3.org/2000/svg";
  const svg2 = doc.createElementNS(ns, "svg");
  svg2.setAttribute("viewBox", "0 0 24 24");
  svg2.setAttribute("width", "18");
  svg2.setAttribute("height", "18");
  svg2.setAttribute("aria-hidden", "true");
  svg2.setAttribute("focusable", "false");
  svg2.classList.add(VISUAL_CLASSES.settingsIcon);
  const path = doc.createElementNS(ns, "path");
  path.setAttribute("d", "M9 3v6M15 3v6M6 9h12v3a6 6 0 0 1-12 0V9zm6 9v3");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  svg2.append(path);
  return svg2;
}
function marketTrigger(doc, language, value) {
  const button = doc.createElement("button");
  button.type = "button";
  button.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsTrigger}`;
  button.dataset["setting"] = "market";
  button.setAttribute("aria-label", translate(language, "market.aria", { value }));
  button.append(marketIcon(doc));
  button.append(element3(doc, "span", void 0, translate(language, "market.edit")));
  return button;
}
function areaChoices(options) {
  const choices = options.areas.map((area) => ({
    id: area.area_id,
    name: area.name,
    published: true
  }));
  const configured = options.configured_area;
  if (configured !== null && !options.areas.some((area) => area.area_id === configured)) {
    choices.push({ id: configured, name: null, published: false });
  }
  return choices;
}
function optionText(language, choice) {
  const base = choice.name === null ? choice.id : marketAreaLabel(language, choice.name, choice.id);
  return choice.published ? base : `${base} · ${translate(language, "market.area.unlisted")}`;
}
function figureText(language, value) {
  return formatNumber(language, value, 2);
}
function suggestionText(language, suggestion) {
  return suggestion === null ? "" : figureText(language, suggestion);
}
function marketEditorBody(doc, language, form, handlers, idPrefix, region = null) {
  const body = element3(doc, "div");
  body.append(element3(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(language, "market.intro")));
  const stateKey = marketStateKey(form.options.state, form.options.reason);
  if (stateKey !== null) {
    body.append(element3(doc, "p", VISUAL_CLASSES.marketState, translate(language, stateKey)));
  }
  const areaId = `${idPrefix}-area`;
  const areaDescriptionId = `${areaId}-description`;
  const choices = areaChoices(form.options);
  const select = doc.createElement("select");
  select.className = `${VISUAL_CLASSES.settingsInput} ${VISUAL_CLASSES.marketArea}`;
  const areaField = element3(doc, "div", VISUAL_CLASSES.settingsField);
  const areaLabel = element3(doc, "label", VISUAL_CLASSES.settingsLabel, translate(language, "market.area.label"));
  areaLabel.setAttribute("for", areaId);
  select.id = areaId;
  if (form.values.areaId === null) {
    const placeholder = doc.createElement("option");
    placeholder.value = "";
    placeholder.textContent = translate(language, "market.unset");
    placeholder.disabled = true;
    placeholder.selected = true;
    select.append(placeholder);
  }
  const optionFor = (choice) => {
    const option = doc.createElement("option");
    option.value = choice.id;
    option.textContent = optionText(language, choice);
    return option;
  };
  const countriesOf = new Map(form.options.areas.map((area) => [area.area_id, area.countries]));
  const { groups, ungrouped } = groupAreasForPicker(
    choices.map((choice) => ({ choice, countries: countriesOf.get(choice.id) ?? [] })),
    region
  );
  for (const group of groups) {
    const optgroup = doc.createElement("optgroup");
    optgroup.label = countryLabel(language, group.heading);
    optgroup.append(...group.areas.map((entry) => optionFor(entry.choice)));
    select.append(optgroup);
  }
  select.append(...ungrouped.map((entry) => optionFor(entry.choice)));
  select.value = form.values.areaId ?? "";
  select.disabled = form.readOnly;
  const areaDescription = element3(
    doc,
    "p",
    `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.settingsNote}`,
    translate(language, "market.area.description")
  );
  areaDescription.id = areaDescriptionId;
  if (choices.length === 0 && form.values.areaId === null) {
    areaDescription.textContent = translate(language, "market.area.missing");
  }
  select.setAttribute("aria-describedby", areaDescriptionId);
  areaField.append(areaLabel, select, areaDescription);
  body.append(areaField);
  const states = {
    vat: form.values.vat,
    tax: form.values.tax,
    transfer: form.values.transfer
  };
  const shown = (value, suggestion) => value.intent === "suggested" ? suggestionText(language, suggestion) : value.value;
  for (const component of FISCAL_COMPONENTS) {
    const current = states[component];
    const unit = fiscalUnit(form.options, form.values.areaId, component);
    const suggestion = marketSuggestion(form.options, form.values.areaId, component);
    const label = translate(language, componentKey(component, "label"));
    const suggestionId = `${idPrefix}-${component}-suggestion`;
    const descriptionId = `${idPrefix}-${component}-description`;
    const block = element3(doc, "div", VISUAL_CLASSES.marketComponent);
    const row = element3(doc, "div", VISUAL_CLASSES.marketValue);
    const checkbox = doc.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = VISUAL_CLASSES.marketCheckbox;
    checkbox.id = `${idPrefix}-${component}-enabled`;
    checkbox.checked = current.enabled;
    checkbox.disabled = form.readOnly;
    checkbox.setAttribute(
      "aria-label",
      translate(language, "market.enabled.aria", { component: label })
    );
    checkbox.setAttribute("aria-describedby", `${suggestionId} ${descriptionId}`);
    const name = element3(doc, "label", VISUAL_CLASSES.settingsLabel, label);
    name.setAttribute("for", checkbox.id);
    const figure = doc.createElement("input");
    figure.type = "number";
    figure.step = "any";
    figure.min = "0";
    figure.inputMode = "decimal";
    figure.value = shown(current, suggestion);
    figure.className = VISUAL_CLASSES.settingsInput;
    figure.id = `${idPrefix}-${component}-value`;
    figure.disabled = form.readOnly;
    figure.setAttribute(
      "aria-label",
      translate(language, "market.value.aria", { component: label })
    );
    figure.setAttribute("aria-describedby", `${suggestionId} ${descriptionId}`);
    row.append(checkbox, name, figure, element3(doc, "span", VISUAL_CLASSES.settingsUnit, unit));
    block.append(row);
    const suggestionLine = element3(
      doc,
      "p",
      VISUAL_CLASSES.marketSuggestion,
      suggestion === null ? translate(language, "market.suggestion.none") : translate(language, "market.suggestion.label", {
        value: figureText(language, suggestion),
        unit
      })
    );
    suggestionLine.id = suggestionId;
    const reset = doc.createElement("button");
    reset.type = "button";
    reset.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.marketReset}`;
    reset.textContent = translate(language, "market.resetToSuggestion");
    reset.disabled = form.readOnly;
    reset.setAttribute("aria-describedby", suggestionId);
    suggestionLine.append(" ", reset);
    block.append(suggestionLine);
    const description = element3(
      doc,
      "p",
      `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.settingsNote}`,
      translate(language, componentKey(component, "description"))
    );
    description.id = descriptionId;
    block.append(description);
    const paint = () => {
      reset.hidden = form.readOnly || suggestion === null || states[component].intent !== "custom";
    };
    checkbox.addEventListener("change", () => {
      states[component] = setEnabled(states[component], checkbox.checked);
      paint();
    });
    figure.addEventListener("input", () => {
      states[component] = figureEdited(states[component], figure.value);
      paint();
    });
    reset.addEventListener("click", () => {
      states[component] = resetToSuggestion(states[component]);
      figure.value = shown(states[component], suggestion);
      paint();
    });
    paint();
    body.append(block);
  }
  const builtAreaId = form.values.areaId;
  const read = () => ({
    areaId: builtAreaId,
    vat: { ...states.vat },
    tax: { ...states.tax },
    transfer: { ...states.transfer }
  });
  select.addEventListener("change", () => {
    handlers.onAreaChange(select.value === "" ? null : select.value, read());
  });
  if (form.conflict !== null) {
    body.append(element3(doc, "p", VISUAL_CLASSES.settingsConflict, translate(language, "settings.conflict.intro")));
  }
  const editable = !form.readOnly && form.values.areaId !== null;
  if (editable) {
    const actions = element3(doc, "div", VISUAL_CLASSES.settingsActions);
    if (form.conflict === null) {
      const save = doc.createElement("button");
      save.type = "button";
      save.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSave}`;
      save.textContent = translate(language, "settings.save");
      save.addEventListener("click", () => handlers.onSave(read()));
      actions.append(save);
    } else {
      const reapply = doc.createElement("button");
      reapply.type = "button";
      reapply.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsReapply}`;
      reapply.textContent = translate(language, "settings.reapply");
      reapply.addEventListener("click", () => handlers.onReapply(read()));
      const reload = doc.createElement("button");
      reload.type = "button";
      reload.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsReload}`;
      reload.textContent = translate(language, "settings.reload");
      reload.addEventListener("click", () => handlers.onReload());
      actions.append(reapply, reload);
    }
    const cancel = doc.createElement("button");
    cancel.type = "button";
    cancel.className = VISUAL_CLASSES.button;
    cancel.textContent = translate(language, "settings.cancel");
    cancel.addEventListener("click", () => handlers.onCancel());
    actions.append(cancel);
    body.append(actions);
  }
  return { body, values: read };
}

// src/vehicle-line.ts
var STALE_READING_S = 3600;
function ageSentence(language, seconds) {
  if (seconds < 90) {
    return translate(language, "settings.soc.age.now");
  }
  if (seconds < 3600) {
    return translate(language, "settings.soc.age.min", { count: String(Math.floor(seconds / 60)) });
  }
  if (seconds < 86400) {
    return translate(language, "settings.soc.age.hour", { count: String(Math.floor(seconds / 3600)) });
  }
  return translate(language, "settings.soc.age.day", { count: String(Math.floor(seconds / 86400)) });
}
function vehicleName(language, name) {
  return name !== null && name.trim() !== "" ? name : translate(language, "settings.vehicle.unnamed");
}
function vehicleLineFor(language, soc, settings) {
  if (soc === null || soc.vehicle_id === null || soc.value === null) {
    return null;
  }
  const name = vehicleName(language, soc.vehicle_name);
  const target = settings?.driver === SETTINGS_DRIVER_TARGET_SOC ? settings.target.target_percent ?? soc.target_percent : null;
  const now = percentAmount(language, soc.value);
  const charge = target === null ? now : `${now} → ${percentAmount(language, target)}`;
  const estimatePrefix = soc.estimated ? "~" : "";
  const age = !soc.estimated && soc.age_s !== null && soc.age_s > STALE_READING_S ? ageSentence(language, soc.age_s) : null;
  const estimateTitle = soc.estimated && soc.age_s !== null ? translate(language, "vehicleLine.estimateTitle", { age: ageSentence(language, soc.age_s) }) : null;
  const spoken = [`${estimatePrefix}${charge}`, estimateTitle ?? age].filter((part) => part !== null).join(", ");
  return {
    vehicleId: soc.vehicle_id,
    name,
    charge,
    estimatePrefix,
    age,
    estimateTitle,
    ariaLabel: translate(language, "vehicleLine.aria", { name, summary: spoken })
  };
}
function vehicleChoicesFor(language, vehicles, plannedId) {
  return vehicles.map((vehicle) => ({
    id: vehicle.id,
    name: vehicleName(language, vehicle.name),
    charge: vehicle.soc_percent === null ? null : percentAmount(language, vehicle.soc_percent),
    selected: vehicle.id === plannedId
  }));
}

// src/target-need.ts
function pythonRound(value) {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff < 0.5) {
    return floor;
  }
  if (diff > 0.5) {
    return floor + 1;
  }
  return floor % 2 === 0 ? floor : floor + 1;
}
function chargeCeiling(maxPercent) {
  if (maxPercent === null || !Number.isFinite(maxPercent)) {
    return 100;
  }
  return Math.min(100, Math.max(0, Math.floor(maxPercent)));
}
function effectiveTarget(targetPercent, maxPercent) {
  return Math.min(pythonRound(targetPercent), chargeCeiling(maxPercent));
}
function targetNeedKwh(facts) {
  const { soc, capacityKwh } = facts;
  if (soc === null || capacityKwh === null || !(capacityKwh > 0) || !Number.isFinite(facts.targetPercent)) {
    return null;
  }
  const needed = Math.max(0, (effectiveTarget(facts.targetPercent, facts.maxPercent) - soc) / 100 * capacityKwh);
  return needed === 0 ? 0 : needed / facts.efficiency;
}
function pythonRoundedAbove(targetPercent, maxPercent) {
  return maxPercent !== null && pythonRound(targetPercent) > chargeCeiling(maxPercent);
}

// src/settings-editor.ts
function element4(doc, tag, className, text5) {
  const node = doc.createElement(tag);
  if (className !== void 0) {
    node.className = className;
  }
  if (text5 !== void 0) {
    node.textContent = text5;
  }
  return node;
}
function field(doc, id, labelText, control) {
  control.id = id;
  const wrapper = element4(doc, "div", VISUAL_CLASSES.settingsField);
  const label = element4(doc, "label", VISUAL_CLASSES.settingsLabel, labelText);
  label.setAttribute("for", id);
  wrapper.append(label, control);
  return wrapper;
}
function checkboxField(doc, id, labelText, control) {
  control.id = id;
  const wrapper = element4(doc, "div", VISUAL_CLASSES.settingsCheckRow);
  const label = element4(doc, "label", VISUAL_CLASSES.settingsLabel, labelText);
  label.setAttribute("for", id);
  wrapper.append(control, label);
  return wrapper;
}
function numberInput(doc, options) {
  const input = doc.createElement("input");
  input.type = "number";
  input.min = String(options.min);
  input.max = String(options.max);
  input.step = String(options.step);
  input.value = options.value;
  input.inputMode = "decimal";
  input.className = VISUAL_CLASSES.settingsInput;
  return input;
}
function rangeInput(doc, options) {
  const input = doc.createElement("input");
  input.type = "range";
  input.className = VISUAL_CLASSES.settingsSlider;
  input.min = String(options.min);
  input.max = String(options.max);
  input.step = String(options.step);
  input.value = String(options.value);
  input.setAttribute("aria-label", options.label);
  return input;
}
function pairedControls(doc, language, options) {
  const number2 = options.input;
  number2.step = "any";
  number2.min = String(options.minimum);
  const markId = `${options.id}-slider-mark`;
  const mark = element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.sliderOutOfRange"));
  mark.id = markId;
  const readNumber = () => Number(number2.value.trim().replace(",", "."));
  const slider = rangeInput(doc, {
    min: options.minimum,
    max: options.maximumOf(readNumber()),
    step: options.step,
    value: options.minimum,
    label: options.sliderLabel
  });
  slider.setAttribute("aria-describedby", markId);
  const paint = () => {
    const value = readNumber();
    const maximum = options.maximumOf(value);
    const represents = sliderRepresents(value, options.minimum, options.step) && value <= maximum;
    slider.max = String(maximum);
    slider.value = represents ? String(value) : String(options.minimum);
    slider.disabled = options.readOnly || !represents;
    mark.hidden = represents || options.readOnly;
    options.onChange();
  };
  slider.addEventListener("input", () => {
    number2.value = slider.value;
    options.onChange();
  });
  number2.addEventListener("input", paint);
  const pair = element4(doc, "div", VISUAL_CLASSES.settingsPair);
  pair.append(slider, number2, element4(doc, "span", VISUAL_CLASSES.settingsUnit, options.unit));
  pair.append(mark);
  if (options.readOnly) {
    number2.disabled = true;
  }
  const fieldNode = element4(doc, "div", VISUAL_CLASSES.settingsField);
  fieldNode.setAttribute("role", "group");
  fieldNode.setAttribute("aria-labelledby", `${options.id}-label`);
  const label = element4(doc, "label", VISUAL_CLASSES.settingsLabel, options.labelText);
  label.id = `${options.id}-label`;
  label.setAttribute("for", number2.id);
  fieldNode.append(label, pair);
  paint();
  return { field: fieldNode, slider, mark };
}
function settingsEditorBody(doc, language, form, handlers, idPrefix) {
  const body = element4(doc, "div");
  const introKey = `settings.${form.kind}.intro`;
  body.append(element4(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(language, introKey)));
  if (form.readOnly) {
    body.append(element4(doc, "p", VISUAL_CLASSES.settingsReadOnly, translate(language, "settings.readOnly")));
  }
  const values = { ...form.values };
  const energyInput = numberInput(doc, {
    min: ENERGY_MIN_KWH,
    max: ENERGY_MAX_KWH,
    step: "any",
    value: form.values.energy
  });
  energyInput.inputMode = "decimal";
  const enabledInput = doc.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.className = VISUAL_CLASSES.settingsInput;
  enabledInput.checked = form.values.deadlineEnabled;
  const timeInput = doc.createElement("input");
  timeInput.type = "time";
  timeInput.className = VISUAL_CLASSES.settingsInput;
  timeInput.value = form.values.deadlineTime;
  const dateInput = doc.createElement("input");
  dateInput.type = "date";
  dateInput.className = VISUAL_CLASSES.settingsInput;
  if (form.days != null) {
    dateInput.min = form.days.today;
    dateInput.max = form.days.max;
  }
  dateInput.value = form.values.departureDate;
  const periodsInput = rangeInput(doc, {
    min: PERIODS_MIN,
    max: PERIODS_MAX,
    step: 1,
    value: Number(form.values.maxPeriods),
    label: translate(language, "settings.deadline.periods")
  });
  const currentInput = numberInput(doc, {
    min: form.currentRange.minA,
    max: form.currentRange.maxA,
    step: 1,
    value: form.values.current
  });
  const appendEnergy = (into = body) => {
    if (form.energyReadOnly && form.kind !== "plan") {
      const machine = form.values.energy.trim();
      const amount = machine === "" ? Number.NaN : Number(machine);
      const readOnlyValue = element4(
        doc,
        "p",
        VISUAL_CLASSES.settingsReadOnly,
        Number.isFinite(amount) ? energyAmount(language, amount) : form.values.energy
      );
      readOnlyValue.dataset["energy"] = form.values.energy;
      into.append(readOnlyValue);
      into.append(element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.energy.targetSoc")));
      return;
    }
    into.append(
      pairedControls(doc, language, {
        id: `${idPrefix}-energy`,
        labelText: translate(language, "settings.energy.label"),
        sliderLabel: translate(language, "settings.energy.slider"),
        minimum: ENERGY_SLIDER_MIN_KWH,
        step: ENERGY_SLIDER_STEP_KWH,
        maximumOf: energySliderMaximum,
        unit: "kWh",
        input: energyInput,
        readOnly: form.readOnly,
        onChange: () => {
        }
      }).field
    );
  };
  const appendDate = (into) => {
    const days = form.days ?? null;
    const weekdays2 = weekdayGroup();
    if (days === null && dateInput.value === "") {
      into.append(weekdays2);
      return;
    }
    const group = element4(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
    group.dataset["part"] = "departure-date";
    group.append(element4(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "settings.deadline.date")));
    const radioName = `${idPrefix}-departure-day`;
    const dailyRadio = doc.createElement("input");
    const dateRadio = doc.createElement("input");
    const choice = (radio, value, labelKey) => {
      radio.type = "radio";
      radio.name = radioName;
      radio.value = value;
      radio.disabled = form.readOnly;
      const label = element4(doc, "label", VISUAL_CLASSES.siteChoice);
      label.append(radio, doc.createTextNode(translate(language, labelKey)));
      return label;
    };
    dailyRadio.dataset["departureDay"] = "daily";
    dateRadio.dataset["departureDay"] = "date";
    dateRadio.checked = dateInput.value !== "";
    dailyRadio.checked = !dateRadio.checked;
    const dateRow = element4(doc, "div");
    dateRow.dataset["part"] = "departure-date-row";
    dateRow.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap";
    dateRow.append(choice(dateRadio, "date", "settings.deadline.dateOn"), dateInput);
    group.append(choice(dailyRadio, "daily", "settings.deadline.dateDaily"), dateRow);
    dateInput.id = `${idPrefix}-deadline-date`;
    dateInput.setAttribute("aria-label", translate(language, "settings.deadline.dateOn"));
    const note = element4(doc, "p", VISUAL_CLASSES.settingsNote);
    note.dataset["departureDateNote"] = "true";
    const help = element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.deadline.dateHelp"));
    help.dataset["departureDateHelp"] = "true";
    const dated = element4(doc, "div");
    dated.dataset["part"] = "departure-date-picker";
    dated.append(note, help);
    const paint = () => {
      const value = dateInput.value;
      dated.hidden = !dateRadio.checked;
      dateInput.hidden = !dateRadio.checked;
      dateInput.disabled = form.readOnly || days === null;
      const gone = days !== null && dateRadio.checked && value !== "" && value < days.today;
      note.hidden = !gone;
      note.textContent = gone ? translate(language, "settings.deadline.datePast") : "";
    };
    dateRadio.addEventListener("change", () => {
      if (dateRadio.checked && dateInput.value === "" && days !== null) {
        dateInput.value = days.nextOccurrence(timeInput.value);
      }
      paint();
    });
    dailyRadio.addEventListener("change", () => {
      if (dailyRadio.checked) {
        dateInput.value = "";
      }
      paint();
    });
    dateInput.addEventListener("input", paint);
    dateInput.addEventListener("change", paint);
    into.append(group, dated, weekdays2);
    const paintWeekdays = () => {
      weekdays2.hidden = dateRadio.checked;
    };
    dateRadio.addEventListener("change", paintWeekdays);
    dailyRadio.addEventListener("change", paintWeekdays);
    paintWeekdays();
    paint();
  };
  const weekdayChecks = [];
  const weekdayGroup = () => {
    const group = element4(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
    group.dataset["part"] = "departure-weekdays";
    group.append(element4(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "settings.deadline.weekdays")));
    const chosen = new Set([...form.values.departureWeekdays].map(Number));
    const row = element4(doc, "div");
    row.style.cssText = "display:flex;flex-wrap:wrap;gap:4px 12px";
    for (let day = 1; day <= 7; day += 1) {
      const check = doc.createElement("input");
      check.type = "checkbox";
      check.value = String(day);
      check.checked = chosen.has(day);
      check.disabled = form.readOnly;
      check.dataset["weekday"] = String(day);
      weekdayChecks.push(check);
      const label = element4(doc, "label", VISUAL_CLASSES.siteChoice);
      label.append(check, doc.createTextNode(weekdayName(language, day)));
      row.append(label);
    }
    group.append(row, element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.deadline.weekdaysHelp")));
    return group;
  };
  const appendDeadline = () => {
    enabledInput.disabled = form.readOnly;
    timeInput.disabled = form.readOnly;
    periodsInput.disabled = form.readOnly;
    body.append(
      checkboxField(doc, `${idPrefix}-deadline-enabled`, translate(language, "settings.deadline.enabled"), enabledInput)
    );
    const departure = element4(doc, "div");
    departure.dataset["part"] = "departure";
    departure.append(field(doc, `${idPrefix}-deadline-time`, translate(language, "settings.deadline.time"), timeInput));
    appendDate(departure);
    departure.hidden = !enabledInput.checked;
    enabledInput.addEventListener("change", () => {
      departure.hidden = !enabledInput.checked;
    });
    body.append(departure);
    const periodsValue = element4(doc, "output", VISUAL_CLASSES.settingsUnit, periodsInput.value);
    periodsValue.dataset["periodsValue"] = "true";
    periodsInput.addEventListener("input", () => {
      periodsValue.textContent = periodsInput.value;
    });
    const periodsPair = element4(doc, "div", VISUAL_CLASSES.settingsPair);
    periodsPair.append(periodsInput, periodsValue);
    periodsInput.id = `${idPrefix}-deadline-periods`;
    const periodsLabel = element4(doc, "label", VISUAL_CLASSES.settingsLabel, translate(language, "settings.deadline.periods"));
    periodsLabel.setAttribute("for", periodsInput.id);
    const periodsBlock = element4(doc, "div", VISUAL_CLASSES.settingsField);
    periodsBlock.append(periodsLabel, periodsPair);
    body.append(periodsBlock);
  };
  const phaseRadios = [];
  const draftPhases = () => {
    const chosen = phaseRadios.find((radio) => radio.checked);
    return chosen === void 0 ? form.phases : Number(chosen.value);
  };
  const appendPhases = () => {
    const group = element4(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
    group.dataset["part"] = "phases";
    group.append(element4(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "settings.phases.legend")));
    for (const count of [1, 3]) {
      const label = element4(doc, "label", VISUAL_CLASSES.siteChoice);
      const radio = doc.createElement("input");
      radio.type = "radio";
      radio.name = `${idPrefix}-phases`;
      radio.value = String(count);
      radio.checked = form.values.phases === String(count);
      radio.disabled = form.readOnly;
      radio.dataset["phases"] = String(count);
      phaseRadios.push(radio);
      label.append(radio, doc.createTextNode(translate(language, count === 1 ? "settings.phases.one" : "settings.phases.three")));
      group.append(label);
    }
    body.append(group);
    if (form.values.phases === "") {
      body.append(element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.phases.unset")));
    }
    body.append(element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.phases.help")));
  };
  const appendCurrent = () => {
    currentInput.disabled = form.readOnly;
    const power = element4(doc, "p", VISUAL_CLASSES.settingsPower);
    power.setAttribute("aria-live", "polite");
    const paintPower = () => {
      const amps = Number(currentInput.value.trim().replace(",", "."));
      const nominal = nominalPowerKw(amps, draftPhases());
      power.textContent = nominal === null ? translate(language, "settings.current.powerUnknown") : translate(language, "settings.current.power", {
        power: formatNumber(language, nominal, 1)
      });
    };
    const paired = pairedControls(doc, language, {
      id: `${idPrefix}-current`,
      labelText: translate(language, "settings.current.label"),
      sliderLabel: translate(language, "settings.current.slider", {
        min: String(form.currentRange.minA),
        max: String(form.currentRange.maxA)
      }),
      minimum: form.currentRange.minA,
      step: CURRENT_SLIDER_STEP_A,
      maximumOf: () => form.currentRange.maxA,
      unit: "A",
      input: currentInput,
      readOnly: form.readOnly,
      onChange: paintPower
    });
    body.append(paired.field);
    if (form.kind === "plan") {
      appendPhases();
    }
    body.append(power);
    for (const radio of phaseRadios) {
      radio.addEventListener("change", paintPower);
    }
    paintPower();
  };
  const socRadio = doc.createElement("input");
  const energyRadio = doc.createElement("input");
  const targetInput = numberInput(doc, {
    min: TARGET_PERCENT_MIN,
    max: TARGET_PERCENT_MAX,
    step: "any",
    value: form.values.targetPercent === "" ? "80" : form.values.targetPercent
  });
  let vehicleSelect = null;
  const socRow = (key, label, value) => {
    const row = element4(doc, "div", VISUAL_CLASSES.capabilityItem);
    row.dataset["socRow"] = key;
    row.append(element4(doc, "span", VISUAL_CLASSES.capabilityLabel, label), element4(doc, "span", summaryValueClass(value), value));
    return row;
  };
  const ageSentence2 = (seconds) => ageSentence(language, seconds);
  const socBlock = () => {
    const block = element4(doc, "div");
    block.dataset["part"] = "soc";
    const soc = form.soc;
    targetInput.disabled = form.readOnly;
    const facts = element4(doc, "p", VISUAL_CLASSES.settingsNote);
    facts.dataset["soc"] = "facts";
    const verdict = element4(doc, "p", VISUAL_CLASSES.settingsNote);
    verdict.dataset["soc"] = "verdict";
    const need = element4(doc, "div");
    const reading = element4(doc, "p", VISUAL_CLASSES.settingsNote);
    reading.dataset["soc"] = "reading";
    const pickedVehicle = () => {
      const picked = vehicleSelect === null ? "" : vehicleSelect.value;
      return picked !== "" ? picked : values.vehicleId !== "" ? values.vehicleId : soc?.vehicle_id ?? "";
    };
    const percent2 = (value) => percentAmount(language, value);
    const paint = () => {
      facts.replaceChildren();
      verdict.replaceChildren();
      need.replaceChildren();
      reading.replaceChildren();
      facts.hidden = verdict.hidden = reading.hidden = true;
      if (soc === null) {
        return;
      }
      const picked = pickedVehicle();
      const other = picked !== "" && picked !== (soc.vehicle_id ?? "");
      const own = other ? form.vehicles.find((entry) => entry.id === picked) : void 0;
      const now = other ? null : soc.value;
      const limit = other ? own?.max_percent ?? null : soc.vehicle_max_percent;
      const capacity = other ? own?.capacity_kwh ?? null : soc.capacity_kwh;
      const draft = Number(targetInput.value.trim().replace(",", "."));
      const draftKnown = targetInput.value.trim() !== "" && Number.isFinite(draft);
      const parts = [];
      if (now !== null) {
        parts.push(translate(language, "settings.soc.factNow", { value: percent2(now) }));
      }
      if (limit !== null) {
        parts.push(translate(language, "settings.soc.factLimit", { value: percent2(chargeCeiling(limit)) }));
      }
      if (parts.length > 0) {
        facts.textContent = parts.join(" · ");
        facts.hidden = false;
      }
      if (!other && now !== null && soc.age_s !== null) {
        const age = ageSentence2(soc.age_s);
        const note = soc.estimated ? translate(language, "settings.soc.estimatedFrom", { age }) : soc.age_s >= 90 ? translate(language, "settings.soc.readAge", { age }) : "";
        if (note !== "") {
          reading.textContent = note.charAt(0).toUpperCase() + note.slice(1);
          reading.dataset["estimated"] = String(soc.estimated);
          reading.hidden = false;
        }
      }
      if (draftKnown) {
        if (now !== null && effectiveTarget(draft, limit) <= now) {
          verdict.textContent = translate(language, "settings.soc.noNeed");
          verdict.dataset["verdict"] = "none";
          verdict.hidden = false;
        } else if (limit !== null && pythonRoundedAbove(draft, limit)) {
          verdict.textContent = translate(language, "settings.soc.toLimit", { value: percent2(chargeCeiling(limit)) });
          verdict.dataset["verdict"] = "limit";
          verdict.hidden = false;
        }
      }
      let kwh = null;
      if (!other && draftKnown) {
        kwh = soc.target_percent !== null && draft === soc.target_percent && soc.need_kwh !== null ? soc.need_kwh : targetNeedKwh({
          soc: now,
          capacityKwh: capacity,
          targetPercent: draft,
          maxPercent: limit,
          efficiency: soc.efficiency
        });
      }
      need.append(
        socRow(
          "need",
          translate(language, "settings.soc.need"),
          kwh === null ? translate(language, "settings.soc.unknown") : `${formatFixed(language, kwh, 1)} kWh`
        )
      );
      if (other) {
        need.append(element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.soc.needAfterVehicle")));
      }
    };
    if (soc !== null) {
      if (soc.vehicles.length > 1) {
        const select = doc.createElement("select");
        select.dataset["soc"] = "vehicle-choice";
        select.disabled = form.readOnly;
        const none = doc.createElement("option");
        none.value = "";
        none.textContent = translate(language, "settings.soc.vehicleUnknown");
        select.append(none);
        for (const vehicle of soc.vehicles) {
          const option = doc.createElement("option");
          option.value = vehicle.id;
          option.textContent = vehicle.name;
          select.append(option);
        }
        select.value = values.vehicleId !== "" ? values.vehicleId : soc.vehicle_id ?? "";
        select.addEventListener("change", paint);
        vehicleSelect = select;
        block.append(field(doc, `${idPrefix}-vehicle`, translate(language, "settings.soc.vehicle"), select));
      } else {
        block.append(
          socRow("vehicle", translate(language, "settings.soc.vehicle"), soc.vehicle_name ?? translate(language, "settings.soc.vehicleUnknown"))
        );
      }
      block.append(facts);
      block.append(reading);
    }
    block.append(
      pairedControls(doc, language, {
        id: `${idPrefix}-target`,
        labelText: translate(language, "settings.soc.target"),
        sliderLabel: translate(language, "settings.soc.slider"),
        minimum: TARGET_PERCENT_MIN,
        step: 1,
        maximumOf: () => TARGET_PERCENT_MAX,
        unit: "%",
        input: targetInput,
        readOnly: form.readOnly,
        onChange: paint
      }).field
    );
    if (soc !== null) {
      block.append(verdict, need);
      paint();
      if (soc.missing.includes("capacity")) {
        const note = element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.soc.capacityMissing"));
        note.dataset["soc"] = "capacity-missing";
        block.append(note);
      }
      if (soc.missing.includes("soc")) {
        const note = element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.soc.needSensor"));
        note.dataset["soc"] = "need-sensor";
        block.append(note);
      }
    }
    return block;
  };
  const appendMode = () => {
    const group = element4(doc, "fieldset", VISUAL_CLASSES.siteFieldset);
    group.dataset["part"] = "mode";
    group.append(element4(doc, "legend", VISUAL_CLASSES.siteLegend, translate(language, "settings.plan.mode.legend")));
    const radioName = `${idPrefix}-mode`;
    const choice = (radio, value, labelKey) => {
      radio.type = "radio";
      radio.name = radioName;
      radio.value = value;
      radio.disabled = form.readOnly;
      const label = element4(doc, "label", VISUAL_CLASSES.siteChoice);
      label.append(radio, doc.createTextNode(translate(language, labelKey)));
      return label;
    };
    energyRadio.dataset["mode"] = "manual_kwh";
    socRadio.dataset["mode"] = "target_soc";
    group.append(
      choice(energyRadio, "manual_kwh", "settings.plan.mode.energy"),
      choice(socRadio, "target_soc", "settings.plan.mode.soc")
    );
    const targetSaved = form.values.driver === "target_soc";
    if (form.soc === null && !targetSaved) {
      socRadio.disabled = true;
    }
    socRadio.checked = form.values.driver === "target_soc";
    energyRadio.checked = !socRadio.checked;
    body.append(group);
    if (form.soc === null) {
      const note = element4(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.soc.needSensor"));
      note.dataset["soc"] = "need-sensor";
      body.append(note);
    }
    const energyPart = element4(doc, "div");
    energyPart.dataset["part"] = "energy";
    appendEnergy(energyPart);
    let socPart = null;
    body.append(energyPart);
    const paintMode = () => {
      energyPart.hidden = socRadio.checked;
      if (socRadio.checked && socPart === null) {
        socPart = socBlock();
        energyPart.after(socPart);
      }
      if (socPart !== null) {
        socPart.hidden = !socRadio.checked;
      }
    };
    energyRadio.addEventListener("change", paintMode);
    socRadio.addEventListener("change", paintMode);
    paintMode();
  };
  if (form.kind === "plan") {
    appendMode();
    appendDeadline();
    appendCurrent();
  } else if (form.kind === "energy") {
    appendEnergy();
  } else if (form.kind === "deadline") {
    appendDeadline();
  } else if (form.kind === "current") {
    appendCurrent();
  }
  const read = () => {
    values.energy = energyInput.value;
    values.deadlineEnabled = enabledInput.checked;
    values.deadlineTime = timeInput.value;
    values.departureDate = dateInput.value;
    if (weekdayChecks.length > 0) {
      values.departureWeekdays = weekdayChecks.filter((check) => check.checked).map((check) => check.value).join("");
    }
    values.maxPeriods = periodsInput.value;
    values.current = currentInput.value;
    if (form.kind === "plan") {
      const chosen = phaseRadios.find((radio) => radio.checked);
      values.phases = chosen === void 0 ? values.phases : chosen.value;
      values.driver = socRadio.checked ? "target_soc" : "manual_kwh";
      values.targetPercent = targetInput.value;
      const picked = vehicleSelect === null ? "" : vehicleSelect.value;
      values.vehicleId = picked !== "" ? picked : values.vehicleId !== "" ? values.vehicleId : form.soc?.vehicle_id ?? "";
    }
    return { ...values };
  };
  if (form.conflict !== null) {
    body.append(element4(doc, "p", VISUAL_CLASSES.settingsConflict, translate(language, "settings.conflict.intro")));
  }
  const editable = !form.readOnly && !(form.kind === "energy" && form.energyReadOnly);
  if (editable) {
    const actions = element4(doc, "div", VISUAL_CLASSES.settingsActions);
    if (form.conflict === null) {
      const save = doc.createElement("button");
      save.type = "button";
      save.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSave}`;
      save.textContent = translate(language, "settings.save");
      save.addEventListener("click", () => handlers.onSave(read()));
      const cancel = doc.createElement("button");
      cancel.type = "button";
      cancel.className = VISUAL_CLASSES.button;
      cancel.dataset["action"] = "cancel";
      cancel.textContent = translate(language, "settings.cancel");
      cancel.addEventListener("click", () => handlers.onCancel?.());
      actions.append(save, cancel);
    } else {
      const reapply = doc.createElement("button");
      reapply.type = "button";
      reapply.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsReapply}`;
      reapply.textContent = translate(language, "settings.reapply");
      reapply.addEventListener("click", () => handlers.onReapply(read()));
      const reload = doc.createElement("button");
      reload.type = "button";
      reload.className = `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsReload}`;
      reload.textContent = translate(language, "settings.reload");
      reload.addEventListener("click", () => handlers.onReload());
      actions.append(reapply, reload);
    }
    body.append(actions);
  }
  return { body, values: read };
}
function weekdayName(language, day) {
  try {
    return new Intl.DateTimeFormat(language, { weekday: "short", timeZone: "UTC" }).format(
      new Date(Date.UTC(2024, 0, day))
    );
  } catch {
    return String(day);
  }
}

// src/vehicle-settings.ts
function element5(doc, tag, className, text5) {
  const node = doc.createElement(tag);
  if (className !== void 0) {
    node.className = className;
  }
  if (text5 !== void 0) {
    node.textContent = text5;
  }
  return node;
}
function vehicleSummary(doc, language, input) {
  const { row } = input;
  const card = element5(doc, "section", VISUAL_CLASSES.settingsSection);
  card.dataset["section"] = "vehicle";
  card.dataset["vehicle"] = row.id;
  card.dataset["planned"] = String(input.planned);
  card.append(
    element5(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, row.name ?? translate(language, "settings.vehicle.unnamed"))
  );
  if (input.planned) {
    const mark = element5(doc, "p", VISUAL_CLASSES.settingsNote, translate(language, "settings.vehicle.plannedHere"));
    mark.dataset["vehicleMark"] = "planned";
    card.append(mark);
  }
  const valueRow = (key, label, value) => {
    const line = element5(doc, "div", VISUAL_CLASSES.capabilityItem);
    line.dataset["row"] = key;
    line.append(element5(doc, "span", VISUAL_CLASSES.capabilityLabel, label), element5(doc, "span", summaryValueClass(value), value));
    card.append(line);
  };
  const notSet = translate(language, "entity.notSet");
  valueRow("charge_level", translate(language, "settings.vehicle.charge"), input.charge);
  if (input.properties) {
    valueRow(
      "capacity",
      translate(language, "settings.capacity.label"),
      row.capacity_kwh === null ? notSet : `${formatFixed(language, row.capacity_kwh, 1)} kWh`
    );
    valueRow(
      "consumption",
      translate(language, "settings.consumption.label"),
      row.consumption_kwh_per_10km === null ? notSet : `${formatFixed(language, row.consumption_kwh_per_10km, 1)} ${translate(language, "settings.consumption.unit")}`
    );
  }
  const button = element5(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSectionConfigure}`, translate(language, "settings.vehicle.change"));
  button.type = "button";
  button.disabled = !input.isAdmin;
  button.dataset["editVehicle"] = row.id;
  button.addEventListener("click", input.onChange);
  card.append(button);
  return card;
}

// src/history.ts
var Malformed2 = class extends Error {
};
function bad5() {
  throw new Malformed2();
}
function record5(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value : bad5();
}
function text4(source, key) {
  const value = source[key];
  return typeof value === "string" ? value : bad5();
}
function textOrNull4(source, key) {
  const value = source[key];
  return value === null ? null : typeof value === "string" ? value : bad5();
}
function number(source, key) {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : bad5();
}
function numberOrNull3(source, key) {
  const value = source[key];
  return value === null ? null : typeof value === "number" && Number.isFinite(value) ? value : bad5();
}
function flag2(source, key) {
  const value = source[key];
  return typeof value === "boolean" ? value : bad5();
}
function shareOrNull(source, key) {
  const value = numberOrNull3(source, key);
  return value !== null && (value < 0 || value > 1) ? bad5() : value;
}
function decodeBucket(raw) {
  const source = record5(raw);
  const sessions = number(source, "sessions");
  if (!Number.isInteger(sessions) || sessions < 0) {
    bad5();
  }
  return {
    period: text4(source, "period"),
    sessions,
    energy_kwh: number(source, "energy_kwh"),
    cost: numberOrNull3(source, "cost"),
    currency: textOrNull4(source, "currency"),
    major_unit: textOrNull4(source, "major_unit"),
    minor_unit: textOrNull4(source, "minor_unit"),
    average_price_minor_per_kwh: numberOrNull3(source, "average_price_minor_per_kwh"),
    solar_share: shareOrNull(source, "solar_share"),
    reference_cost: numberOrNull3(source, "reference_cost"),
    savings: numberOrNull3(source, "savings"),
    estimated: flag2(source, "estimated")
  };
}
function decodeRecord(raw) {
  const source = record5(raw);
  return {
    id: text4(source, "id"),
    start: text4(source, "start"),
    end: textOrNull4(source, "end"),
    energy_kwh: number(source, "energy_kwh"),
    energy_source: text4(source, "energy_source"),
    estimated: flag2(source, "estimated"),
    cost: numberOrNull3(source, "cost"),
    currency: textOrNull4(source, "currency"),
    major_unit: textOrNull4(source, "major_unit"),
    minor_unit: textOrNull4(source, "minor_unit"),
    average_price_minor_per_kwh: numberOrNull3(source, "average_price_minor_per_kwh"),
    started_by: text4(source, "started_by"),
    strategy: textOrNull4(source, "strategy"),
    vehicle: textOrNull4(source, "vehicle"),
    solar_share: shareOrNull(source, "solar_share"),
    reference_cost: numberOrNull3(source, "reference_cost"),
    savings: numberOrNull3(source, "savings")
  };
}
function list3(source, key, decode) {
  const value = source[key];
  return Array.isArray(value) ? value.map(decode) : bad5();
}
function decodeSessions(raw) {
  try {
    const source = record5(raw);
    if (source.api_version !== SESSIONS_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    return {
      ok: true,
      value: {
        this_month: decodeBucket(source.this_month),
        last_month: decodeBucket(source.last_month),
        months: list3(source, "months", decodeBucket),
        days: list3(source, "days", decodeBucket),
        open: source.open === null ? null : decodeRecord(source.open),
        sessions: list3(source, "sessions", decodeRecord)
      }
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}
function decodeCsv(raw) {
  try {
    const source = record5(raw);
    if (source.api_version !== SESSIONS_API_VERSION || source.format !== "csv") {
      return null;
    }
    return { filename: text4(source, "filename"), csv: text4(source, "csv") };
  } catch {
    return null;
  }
}
var HISTORY_RANGES = ["thisMonth", "lastMonth", "last12", "all"];
function monthParts(period) {
  const match = /^(\d{4})-(\d{2})$/.exec(period);
  return match === null ? null : [Number(match[1]), Number(match[2])];
}
function pad(value) {
  return String(value).padStart(2, "0");
}
function monthStart(year, month) {
  return `${year}-${pad(month)}-01`;
}
function monthEnd(year, month) {
  return `${year}-${pad(month)}-${pad(new Date(Date.UTC(year, month, 0)).getUTCDate())}`;
}
function exportDates(range, answer) {
  const current = monthParts(answer.this_month.period);
  const previous = monthParts(answer.last_month.period);
  if (range === "all" || current === null || previous === null) {
    return { from: null, to: null };
  }
  if (range === "thisMonth") {
    return { from: monthStart(...current), to: monthEnd(...current) };
  }
  if (range === "lastMonth") {
    return { from: monthStart(...previous), to: monthEnd(...previous) };
  }
  const first = new Date(Date.UTC(current[0], current[1] - 1 - 11, 1));
  return { from: monthStart(first.getUTCFullYear(), first.getUTCMonth() + 1), to: null };
}
var monthFormats = /* @__PURE__ */ new Map();
function monthLabel(language, period) {
  const parts = monthParts(period);
  if (parts === null) {
    return period;
  }
  let format = monthFormats.get(language);
  if (format === void 0) {
    format = new Intl.DateTimeFormat(language, { month: "long", year: "numeric", timeZone: "UTC" });
    monthFormats.set(language, format);
  }
  return format.format(new Date(Date.UTC(parts[0], parts[1] - 1, 1, 12)));
}
function formatContext(language, source) {
  return {
    language,
    timeZone: "",
    unit: source.minor_unit ?? "",
    currency: source.currency,
    majorUnit: source.major_unit
  };
}
function element6(doc, tag, className, content) {
  const created = doc.createElement(tag);
  if (className !== void 0) {
    created.className = className;
  }
  if (content !== void 0) {
    created.textContent = content;
  }
  return created;
}
function clockOf(iso) {
  return /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(iso)?.[1] ?? "";
}
function dayOf(iso) {
  return iso.slice(0, 10);
}
function percent(language, share) {
  return `${formatNumber(language, share * 100, 0)} %`;
}
function sessionsCount(language, count) {
  const key = `history.sessions.${pluralForm(language, count)}`;
  return translate(language, key, { count: formatNumber(language, count, 0) });
}
function startedByKey(startedBy) {
  switch (startedBy) {
    case "plan_window":
      return "history.by.plan_window";
    case "manual":
      return "history.by.manual";
    case "solar":
      return "history.by.solar";
    case "hybrid":
      return "history.by.hybrid";
    default:
      return "history.by.other";
  }
}
function savingsLine(language, source) {
  if (source.savings === null || Math.abs(source.savings) < 5e-3) {
    return null;
  }
  const amount = money(formatContext(language, source), Math.abs(source.savings));
  return translate(language, source.savings > 0 ? "history.savings.saved" : "history.savings.extra", { amount });
}
function figures(language, bucket) {
  const context = formatContext(language, bucket);
  const cost = bucket.cost === null ? translate(language, "history.noCost") : money(context, bucket.cost);
  const parts = [energyAmount(language, bucket.energy_kwh), cost];
  if (bucket.average_price_minor_per_kwh !== null && bucket.minor_unit !== null) {
    parts.push(pricePerKwh(context, bucket.average_price_minor_per_kwh));
  }
  return parts.join(" · ");
}
function bucketCard(doc, language, titleKey, bucket) {
  const card = element6(doc, "section", VISUAL_CLASSES.historyTile);
  card.dataset["tile"] = titleKey === "history.thisMonth" ? "thisMonth" : "lastMonth";
  card.append(element6(doc, "h4", VISUAL_CLASSES.historyTileHeading, translate(language, titleKey)));
  if (bucket.sessions === 0) {
    card.append(element6(doc, "p", VISUAL_CLASSES.muted, translate(language, "history.noneInMonth")));
    return card;
  }
  card.append(element6(doc, "p", VISUAL_CLASSES.historyFigures, figures(language, bucket)));
  const notes = [sessionsCount(language, bucket.sessions)];
  if (bucket.solar_share !== null) {
    notes.push(translate(language, "history.solar", { percent: percent(language, bucket.solar_share) }));
  }
  if (bucket.estimated) {
    notes.push(translate(language, "history.estimated"));
  }
  card.append(element6(doc, "p", VISUAL_CLASSES.muted, notes.join(" · ")));
  const savings = savingsLine(language, bucket);
  if (savings !== null) {
    card.append(element6(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.historySavings}`, savings));
  }
  return card;
}
function periodRow(doc, language, kind, bucket) {
  const row = element6(doc, "li", VISUAL_CLASSES.historyRow);
  row.dataset["period"] = bucket.period;
  row.append(
    element6(
      doc,
      "span",
      VISUAL_CLASSES.historyRowTitle,
      kind === "days" ? dateLabel(language, bucket.period) : monthLabel(language, bucket.period)
    ),
    element6(doc, "span", VISUAL_CLASSES.historyRowFigures, figures(language, bucket))
  );
  const notes = [sessionsCount(language, bucket.sessions)];
  if (bucket.solar_share !== null) {
    notes.push(translate(language, "history.solar", { percent: percent(language, bucket.solar_share) }));
  }
  const savings = savingsLine(language, bucket);
  if (savings !== null) {
    notes.push(savings);
  }
  row.append(element6(doc, "span", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.historyRowNote}`, notes.join(" · ")));
  return row;
}
function sessionRow(doc, language, session) {
  const row = element6(doc, "li", VISUAL_CLASSES.historyRow);
  row.dataset["session"] = session.id;
  const end = session.end === null ? "" : clockOf(session.end);
  const crosses = session.end !== null && dayOf(session.end) !== dayOf(session.start);
  const when = `${dateLabel(language, dayOf(session.start))} ${clockOf(session.start)}–${crosses ? `${dateLabel(language, dayOf(session.end))} ` : ""}${end}`;
  row.append(
    element6(doc, "span", VISUAL_CLASSES.historyRowTitle, when),
    element6(doc, "span", VISUAL_CLASSES.historyRowFigures, figures(language, session))
  );
  const notes = [translate(language, startedByKey(session.started_by))];
  if (session.vehicle !== null) {
    notes.push(session.vehicle);
  }
  if (session.solar_share !== null) {
    notes.push(translate(language, "history.solar", { percent: percent(language, session.solar_share) }));
  }
  if (session.estimated) {
    notes.push(translate(language, "history.estimated"));
  }
  row.append(element6(doc, "span", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.historyRowNote}`, notes.join(" · ")));
  return row;
}
function toggle(doc, language, ui, handlers) {
  const group = element6(doc, "div", VISUAL_CLASSES.historyToggle);
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", translate(language, "history.listLabel"));
  for (const choice of ["days", "months"]) {
    const button = element6(doc, "button", VISUAL_CLASSES.historyToggleButton, translate(language, choice === "days" ? "history.days" : "history.months"));
    button.type = "button";
    button.dataset["list"] = choice;
    button.setAttribute("aria-pressed", String(ui.list === choice));
    button.addEventListener("click", () => handlers.onList(choice));
    group.append(button);
  }
  return group;
}
function exportRow(doc, language, ui, handlers) {
  const row = element6(doc, "div", VISUAL_CLASSES.historyExport);
  const label = element6(doc, "label", VISUAL_CLASSES.historyExportLabel, translate(language, "history.export.period"));
  const select = element6(doc, "select");
  select.dataset["exportRange"] = "true";
  for (const range of HISTORY_RANGES) {
    const option = new Option(translate(language, `history.range.${range}`), range);
    option.selected = range === ui.range;
    select.append(option);
  }
  select.addEventListener("change", () => {
    const chosen = HISTORY_RANGES.find((range) => range === select.value);
    if (chosen !== void 0) {
      handlers.onRange(chosen);
    }
  });
  label.append(select);
  const button = element6(doc, "button", VISUAL_CLASSES.button, translate(language, "history.export"));
  button.type = "button";
  button.dataset["action"] = "export";
  button.disabled = ui.exporting;
  button.addEventListener("click", () => handlers.onExport());
  row.append(label, button);
  return row;
}
function historyBody(doc, language, state, ui, handlers) {
  const body = element6(doc, "div", VISUAL_CLASSES.historyBody);
  if (state.kind === "loading") {
    const loading = element6(doc, "p", VISUAL_CLASSES.muted, translate(language, "history.loading"));
    loading.setAttribute("role", "status");
    body.append(loading);
    return body;
  }
  if (state.kind === "failed") {
    const failed = element6(doc, "p", VISUAL_CLASSES.settingsNotice, translate(language, state.sentenceKey));
    failed.setAttribute("role", "status");
    if (state.code !== null) {
      failed.dataset["code"] = state.code;
    }
    body.append(failed);
    return body;
  }
  const answer = state.answer;
  body.append(element6(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(language, "history.intro")));
  if (answer.open !== null) {
    const open = element6(
      doc,
      "p",
      VISUAL_CLASSES.historyOpen,
      translate(language, "history.open", {
        time: clockOf(answer.open.start),
        energy: energyAmount(language, answer.open.energy_kwh)
      })
    );
    open.setAttribute("role", "status");
    body.append(open);
  }
  const tiles = element6(doc, "div", VISUAL_CLASSES.historyTiles);
  tiles.append(
    bucketCard(doc, language, "history.thisMonth", answer.this_month),
    bucketCard(doc, language, "history.lastMonth", answer.last_month)
  );
  body.append(tiles);
  if (answer.sessions.length === 0 && answer.open === null) {
    body.append(element6(doc, "p", VISUAL_CLASSES.muted, translate(language, "history.empty")));
  } else {
    body.append(toggle(doc, language, ui, handlers));
    const buckets = ui.list === "days" ? answer.days : answer.months;
    const periods = element6(doc, "ul", VISUAL_CLASSES.historyList);
    periods.dataset["list"] = ui.list;
    for (const bucket of buckets) {
      periods.append(periodRow(doc, language, ui.list, bucket));
    }
    body.append(periods);
    body.append(element6(doc, "h4", VISUAL_CLASSES.historyHeading, translate(language, "history.latest")));
    const latest = element6(doc, "ul", VISUAL_CLASSES.historyList);
    latest.dataset["list"] = "sessions";
    for (const session of answer.sessions) {
      latest.append(sessionRow(doc, language, session));
    }
    body.append(latest);
  }
  body.append(element6(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.historyFootnote}`, translate(language, "history.savings.note")));
  body.append(exportRow(doc, language, ui, handlers));
  if (ui.notice !== null) {
    const notice = element6(doc, "p", VISUAL_CLASSES.settingsNotice, translate(language, ui.notice));
    notice.setAttribute("role", "status");
    body.append(notice);
  }
  return body;
}

// src/card-view.ts
var BOUNDARY_HORIZON_MS = 24 * 36e5;
function element7(doc, tag, className, text5) {
  const created = doc.createElement(tag);
  if (className !== void 0) {
    created.className = className;
  }
  if (text5 !== void 0) {
    created.textContent = text5;
  }
  return created;
}
function readoutDay(model, mark) {
  return hasZone(model.format) ? weekdayDate(model.format, mark.startMs) : "";
}
function readoutTextFor(model, mark) {
  const language = model.language;
  if (mark === null) {
    return translate(language, "graph.descriptionNoSelection");
  }
  const day = readoutDay(model, mark);
  const time = mark.wallClock;
  const zone = hasZone(model.format);
  const ambiguous = zone && wallTimeRepeats(model.format, mark.startMs);
  const offset = ambiguous ? mark.offset : "";
  const price = zone ? pricePerKwh(model.format, mark.price) : "";
  if (price === "") {
    const key2 = ambiguous ? "graph.readoutMissingWithOffset" : "graph.readoutMissing";
    return translate(language, key2, { day, time, offset }).trim();
  }
  const key = ambiguous ? "graph.readoutWithOffset" : "graph.readout";
  return translate(language, key, { day, time, offset, price }).trim();
}
function readoutDisplayText(model, mark) {
  return mark === null ? "" : readoutTextFor(model, mark);
}
function currentPriceText(model, mark) {
  return mark === null || !hasZone(model.format) ? null : pricePerKwh(model.format, mark.price);
}
function nowSummaryText(model, figure) {
  return figure === null ? null : `${translate(model.language, "graph.summary.current")} ${figure}`;
}
function homeAssistantCountry(hass) {
  if (typeof hass !== "object" || hass === null) {
    return null;
  }
  const config = hass.config;
  const country = typeof config === "object" && config !== null ? config.country : void 0;
  return typeof country === "string" && country.trim() !== "" ? country : null;
}
function bannerSeverity(model) {
  return model.severity;
}
function bannerRepeatsStatus(model, severity) {
  if (severity !== "notice" || model.status === null || model.issues.length === 0) {
    return false;
  }
  const strip = (text5) => text5.trim().replace(/[.。]$/u, "");
  const shown = model.status.split(" · ").map(strip);
  return model.issues.every((issue) => shown.includes(strip(issueText(model.language, issue))));
}
var SOLAR_SETUP_ROWS = /* @__PURE__ */ new Set(["solar", "hybrid"]);
var STRATEGY_NEEDS_TOTAL_POWER = "needs_total_grid_power";
function issueCountText(language, count) {
  const key = pluralForm(language, count) === "one" ? "issue.count.one" : "issue.count.other";
  return translate(language, key, { count: String(count) });
}
function periodBlockOrder(model) {
  const table = {
    none: [],
    proposal_only: ["proposal"],
    installed_only: ["installed"],
    applied_same: ["proposal"],
    pending_beside_installed: ["installed", "proposal"]
  };
  return table[model.planRelation].filter(
    (kind) => kind === "installed" ? model.installedPeriods.length > 0 : model.proposalPeriods.length > 0
  );
}
function periodHeading(model, base, count) {
  const form = pluralForm(model.language, count);
  return translate(model.language, `${base}.${form}`, { count: String(count) });
}
function periodLine(doc, model, period) {
  const line = element7(doc, "div", VISUAL_CLASSES.period);
  line.textContent = period.label === "" ? translate(model.language, "plan.missing") : period.label;
  return line;
}
function periodBlock(doc, model, kind, periods) {
  const block = element7(
    doc,
    "section",
    `${VISUAL_CLASSES.periods} ${kind === "installed" ? VISUAL_CLASSES.periodsInstalled : VISUAL_CLASSES.periodsProposal}`
  );
  const heading = element7(
    doc,
    "h4",
    VISUAL_CLASSES.periodsHeading,
    periodHeading(model, kind === "installed" ? "plan.installed" : "plan.proposal", periods.length)
  );
  block.append(heading);
  periods.forEach((period, index) => {
    const line = periodLine(doc, model, period);
    const active = kind === "installed" && period.activeNow;
    if (active) {
      line.classList.add(VISUAL_CLASSES.periodActive);
      line.dataset["activeIndex"] = String(index);
    }
    block.append(line);
  });
  return block;
}
var MARK_PATH = "m214 42-6 62-18-10-23 49 47 3-86.00154 76.23385L142 232l-50 32 6-62 13.99846 9.52923L139 163l-47-3 81.29385-75.527692-16.59077-9.06z";
function brandMark(doc, idPrefix) {
  const ns = "http://www.w3.org/2000/svg";
  const make = (tag, attributes) => {
    const node = doc.createElementNS(ns, tag);
    for (const [name, value] of Object.entries(attributes)) {
      node.setAttribute(name, value);
    }
    return node;
  };
  const stop = (offset, color) => make("stop", { offset, "stop-color": color });
  const backgroundId = `${idPrefix}-mark-background`;
  const boltId = `${idPrefix}-mark-bolt`;
  const haloId = `${idPrefix}-mark-halo`;
  const svg2 = make("svg", {
    viewBox: "0 0 306 306",
    width: "22",
    height: "22",
    "aria-hidden": "true",
    focusable: "false"
  });
  svg2.classList.add(VISUAL_CLASSES.identity);
  const background = make("radialGradient", {
    id: backgroundId,
    cx: "106.158",
    cy: "79.888",
    r: "261",
    gradientUnits: "userSpaceOnUse"
  });
  background.append(stop("0", "#454d55"), stop(".42", "#1b1e22"), stop(".76", "#101216"), stop("1", "#080a0d"));
  const bolt = make("linearGradient", {
    id: boltId,
    x1: "155.35",
    y1: "4.69",
    x2: "129.22",
    y2: "303.87",
    gradientUnits: "userSpaceOnUse"
  });
  bolt.append(
    stop("0", "#ff0000"),
    stop(".102", "#ff0000"),
    stop(".43", "#ffd22e"),
    stop(".57", "#ffe56a"),
    stop(".904", "#00ff06"),
    stop("1", "#00ff00")
  );
  const halo = make("filter", { id: haloId, x: "-30%", y: "-30%", width: "160%", height: "160%" });
  halo.append(make("feGaussianBlur", { stdDeviation: "3.2" }));
  const defs = make("defs", {});
  defs.append(background, bolt, halo);
  svg2.append(
    defs,
    make("rect", { x: "8", y: "8", width: "290", height: "290", rx: "70", fill: `url(#${backgroundId})` }),
    make("path", {
      d: MARK_PATH,
      fill: "none",
      stroke: "#ffe36b",
      "stroke-width": "8",
      "stroke-opacity": ".36",
      "stroke-linejoin": "round",
      filter: `url(#${haloId})`
    }),
    make("path", {
      d: MARK_PATH,
      fill: `url(#${boltId})`,
      stroke: "#ffe36b",
      "stroke-width": "1.2",
      "stroke-opacity": ".52",
      "stroke-linejoin": "round"
    })
  );
  return svg2;
}
function icon(doc, build) {
  const ns = "http://www.w3.org/2000/svg";
  const svg2 = doc.createElementNS(ns, "svg");
  svg2.setAttribute("viewBox", "0 0 24 24");
  svg2.setAttribute("width", "16");
  svg2.setAttribute("height", "16");
  svg2.setAttribute("aria-hidden", "true");
  svg2.setAttribute("focusable", "false");
  svg2.classList.add(VISUAL_CLASSES.actionIcon);
  build(svg2, ns);
  return svg2;
}
function strokePath(ns, doc, d) {
  const path = doc.createElementNS(ns, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  return path;
}
function fillPath(ns, doc, d) {
  const path = doc.createElementNS(ns, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "currentColor");
  return path;
}
function infoIcon(doc) {
  return icon(doc, (svg2, ns) => {
    svg2.append(
      fillPath(
        ns,
        doc,
        "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"
      )
    );
  });
}
function historyIcon(doc) {
  return icon(doc, (svg2, ns) => {
    svg2.append(
      strokePath(ns, doc, "M3.5 12a8.5 8.5 0 1 0 2.6-6.1"),
      strokePath(ns, doc, "M3.5 4.5v4.5H8"),
      strokePath(ns, doc, "M12 7.5V12l3 2")
    );
  });
}
function settingsGearIcon(doc) {
  return icon(doc, (svg2, ns) => {
    svg2.append(
      fillPath(
        ns,
        doc,
        "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.22,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.22,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.68 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z"
      )
    );
  });
}
function batteryIcon(doc) {
  return icon(doc, (svg2, ns) => {
    const body = doc.createElementNS(ns, "path");
    body.setAttribute("d", "M4 8h13a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1zM19 11h1.5v2H19z");
    body.setAttribute("fill", "none");
    body.setAttribute("stroke", "currentColor");
    body.setAttribute("stroke-width", "1.6");
    body.setAttribute("stroke-linejoin", "round");
    const level = doc.createElementNS(ns, "path");
    level.setAttribute("d", "M5.5 10h7v4h-7z");
    level.setAttribute("fill", "currentColor");
    svg2.append(body, level);
  });
}
function pauseIcon(doc) {
  return icon(doc, (svg2, ns) => {
    svg2.append(fillPath(ns, doc, "M7 5h3v14H7zM14 5h3v14h-3z"));
  });
}
function playIcon(doc) {
  return icon(doc, (svg2, ns) => {
    svg2.append(fillPath(ns, doc, "M8 5v14l11-7Z"));
  });
}
function stopIcon(doc) {
  return icon(doc, (svg2, ns) => {
    svg2.append(fillPath(ns, doc, "M7 7h10v10H7z"));
  });
}
function filledEllipse(ns, doc, cx, cy, rx, ry) {
  const ellipse = doc.createElementNS(ns, "ellipse");
  ellipse.setAttribute("cx", String(cx));
  ellipse.setAttribute("cy", String(cy));
  ellipse.setAttribute("rx", String(rx));
  ellipse.setAttribute("ry", String(ry));
  ellipse.setAttribute("fill", "currentColor");
  return ellipse;
}
function filledRect(ns, doc, x, y, width, height) {
  const rect = doc.createElementNS(ns, "rect");
  rect.setAttribute("x", String(x));
  rect.setAttribute("y", String(y));
  rect.setAttribute("width", String(width));
  rect.setAttribute("height", String(height));
  rect.setAttribute("rx", "0.6");
  rect.setAttribute("fill", "currentColor");
  return rect;
}
function piggyBankPath(ns, doc) {
  return [
    filledEllipse(ns, doc, 10.5, 13.5, 7.5, 5.5),
    filledEllipse(ns, doc, 18, 13.5, 2.3, 1.9),
    fillPath(ns, doc, "M6.2 8.4 9.8 6.6 8.7 10.8Z"),
    filledRect(ns, doc, 5.8, 17.6, 1.8, 3.2),
    filledRect(ns, doc, 10.2, 18.2, 1.8, 3.2),
    filledRect(ns, doc, 14.6, 17.6, 1.8, 3.2)
  ];
}
function sunPath(ns, doc) {
  const circle = doc.createElementNS(ns, "circle");
  circle.setAttribute("cx", "12");
  circle.setAttribute("cy", "12");
  circle.setAttribute("r", "3.4");
  circle.setAttribute("fill", "none");
  circle.setAttribute("stroke", "currentColor");
  circle.setAttribute("stroke-width", "2");
  const rays = strokePath(
    ns,
    doc,
    "M12 3v2.4M12 18.6V21M21 12h-2.4M5.4 12H3M18.1 5.9l-1.7 1.7M7.6 16.4l-1.7 1.7M18.1 18.1l-1.7-1.7M7.6 7.6 5.9 5.9"
  );
  return [circle, rays];
}
function strategyIcon(doc, strategyId) {
  if (strategyId === "cheapest") {
    return icon(doc, (svg2, ns) => svg2.append(...piggyBankPath(ns, doc)));
  }
  if (strategyId === "solar") {
    return icon(doc, (svg2, ns) => svg2.append(...sunPath(ns, doc)));
  }
  if (strategyId === "hybrid") {
    return icon(doc, (svg2, ns) => {
      const sun = doc.createElementNS(ns, "g");
      sun.setAttribute("transform", "translate(1 -2) scale(0.62)");
      sun.append(...sunPath(ns, doc));
      const piggy = doc.createElementNS(ns, "g");
      piggy.setAttribute("transform", "translate(-2 4) scale(0.72)");
      piggy.append(...piggyBankPath(ns, doc));
      svg2.append(sun, piggy);
    });
  }
  return null;
}
function capabilityStateText(language, state) {
  return translate(language, state === "available" ? "cap.available" : "cap.unavailable");
}
function issueListBody(doc, model) {
  const body = element7(doc, "div");
  body.append(element7(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(model.language, "dialog.issuesIntro")));
  for (const issue of model.issues) {
    body.append(issueRow(doc, model, issue));
  }
  return body;
}
function issueRow(doc, model, issue) {
  const row = element7(doc, "div", VISUAL_CLASSES.issueItem);
  row.dataset["code"] = issue.code;
  row.dataset["severity"] = issue.severity;
  row.append(
    element7(doc, "span", VISUAL_CLASSES.issueText, issueText(model.language, issue))
  );
  return row;
}
function capabilityBody(doc, model) {
  const body = element7(doc, "div");
  body.append(element7(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(model.language, "cap.intro")));
  for (const item of model.capabilities) {
    const row = element7(doc, "div", VISUAL_CLASSES.capabilityItem);
    row.dataset["capability"] = item.key;
    row.dataset["state"] = item.state;
    row.append(element7(doc, "span", VISUAL_CLASSES.capabilityLabel, translate(model.language, item.labelKey)));
    row.append(element7(doc, "span", VISUAL_CLASSES.capabilityState, capabilityStateText(model.language, item.state)));
    body.append(row);
    if (item.key === "target_soc" && item.state === "unavailable") {
      body.append(element7(doc, "span", VISUAL_CLASSES.capabilityNote, translate(model.language, "cap.targetSocNote")));
    }
  }
  return body;
}
function graphDescription(model, selected, now, marks, days) {
  const language = model.language;
  const zone = hasZone(model.format);
  const first = marks[0] ?? null;
  const last = marks[marks.length - 1] ?? null;
  const stamp = (instantMs2, fallback) => instantMs2 === void 0 || !zone ? fallback : `${weekdayDate(model.format, instantMs2)} ${clock(model.format, instantMs2)}`;
  const from = stamp(first?.startMs, translate(language, "plan.missing"));
  const to = stamp(last?.endMs, translate(language, "plan.missing"));
  const unit = model.format.unit === "" ? translate(language, "plan.missing") : model.format.unit;
  const selectedText = selected === null ? translate(language, "graph.descriptionNoSelection") : readoutTextFor(model, selected);
  const parts = [
    translate(language, "graph.description", {
      from,
      to,
      unit,
      count: String(marks.length),
      days: String(days),
      selected: selectedText
    }),
    // The one focus fact a reader cannot see: which interval is current, said in words rather than
    // left to a decorative line the accessibility tree never receives.
    ...now === null ? [] : [translate(language, "graph.descriptionNow", { now: readoutTextFor(model, now) })],
    translate(language, "graph.keyboardInstructions")
  ];
  if (!zone) {
    parts.push(translate(language, "graph.noZone"));
  }
  return parts.join(" ");
}
function createCardView(input) {
  const { model, idPrefix } = input;
  const doc = input.mount.ownerDocument;
  const now = input.now ?? (() => Date.now());
  let destroyed = false;
  const card = element7(doc, "div", VISUAL_CLASSES.card);
  card.style.boxSizing = "border-box";
  const header = element7(doc, "div", VISUAL_CLASSES.header);
  header.append(brandMark(doc, idPrefix));
  const vehicleLine = vehicleLineFor(model.language, model.soc, model.dashboardSettings);
  let vehicleButton = null;
  if (vehicleLine !== null) {
    const identity2 = element7(doc, "div", VISUAL_CLASSES.nameBlock);
    if (model.chargerName !== null) {
      identity2.append(element7(doc, "h3", VISUAL_CLASSES.name, model.chargerName));
    }
    vehicleButton = element7(doc, "button", VISUAL_CLASSES.vehicleLine);
    vehicleButton.type = "button";
    vehicleButton.dataset["vehicleLine"] = vehicleLine.vehicleId;
    vehicleButton.setAttribute("aria-label", vehicleLine.ariaLabel);
    vehicleButton.setAttribute("aria-haspopup", "dialog");
    if (vehicleLine.estimateTitle !== null) {
      vehicleButton.title = vehicleLine.estimateTitle;
      vehicleButton.dataset["estimated"] = "true";
    }
    vehicleButton.append(
      batteryIcon(doc),
      element7(doc, "span", VISUAL_CLASSES.vehicleLineName, vehicleLine.name),
      element7(doc, "span", VISUAL_CLASSES.vehicleLineCharge, `· ${vehicleLine.estimatePrefix}${vehicleLine.charge}`)
    );
    if (vehicleLine.age !== null) {
      vehicleButton.append(element7(doc, "span", VISUAL_CLASSES.vehicleLineAge, `· ${vehicleLine.age}`));
    }
    vehicleButton.addEventListener("click", () => {
      openVehicleChoice();
    });
    identity2.append(vehicleButton);
    header.append(identity2);
  } else if (model.chargerName !== null) {
    header.append(element7(doc, "h3", VISUAL_CLASSES.name, model.chargerName));
  }
  header.append(
    element7(doc, "span", VISUAL_CLASSES.visuallyHidden, translate(model.language, "card.title"))
  );
  const help = element7(doc, "button", VISUAL_CLASSES.iconButton);
  help.type = "button";
  help.setAttribute("aria-label", translate(model.language, "header.info"));
  help.title = translate(model.language, "header.info");
  help.append(infoIcon(doc));
  header.append(help);
  const historyButton = element7(doc, "button", VISUAL_CLASSES.iconButton);
  historyButton.type = "button";
  historyButton.setAttribute("aria-label", translate(model.language, "header.history"));
  historyButton.setAttribute("aria-haspopup", "dialog");
  historyButton.title = translate(model.language, "header.history");
  historyButton.dataset["history"] = "open";
  historyButton.append(historyIcon(doc));
  header.append(historyButton);
  const settingsGeneral = element7(doc, "button", VISUAL_CLASSES.iconButton);
  settingsGeneral.type = "button";
  settingsGeneral.setAttribute("aria-label", translate(model.language, "header.settings"));
  settingsGeneral.title = translate(model.language, "header.settings");
  settingsGeneral.append(settingsGearIcon(doc));
  header.append(settingsGeneral);
  card.append(header);
  const labels = { close: translate(model.language, "dialog.close") };
  const issuesDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-issues`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  const capabilityDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-capabilities`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  const pauseDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-pause`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  const strategyDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-strategy`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  const vehicleDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-vehicle`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  const settingsDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-settings`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  const marketDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-market`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
    // Leaving the price/tax editor by any of the reader's own routes lands on the Settings page it
    // was opened from, exactly as a Save does.
    onDismiss: () => leaveSettingsChild(marketDialog, input.onCancelMarket)
  });
  const entityDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-entities`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
    onDismiss: () => leaveSettingsChild(entityDialog, input.onCancelEntities)
  });
  entityDialog.element.classList.add(VISUAL_CLASSES.entityDialog);
  const settingsOverviewDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-settings-overview`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  const historyDialog = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-history`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged
  });
  function leaveSettingsChild(dialog, cancel) {
    const returns = cancel?.() !== false;
    dialog.hide({ restoreFocus: false });
    if (returns && !destroyed) {
      openSettingsOverview();
    }
  }
  function anyDialogOpenNow() {
    return issuesDialog.isOpen() || capabilityDialog.isOpen() || pauseDialog.isOpen() || strategyDialog.isOpen() || vehicleDialog.isOpen() || settingsDialog.isOpen() || marketDialog.isOpen() || entityDialog.isOpen() || settingsOverviewDialog.isOpen() || historyDialog.isOpen();
  }
  function notifyDialogsChanged() {
    queueMicrotask(() => {
      if (destroyed || anyDialogOpenNow()) {
        return;
      }
      input.onDialogsClosed?.();
    });
  }
  const severity = bannerSeverity(model);
  if (severity !== null && !bannerRepeatsStatus(model, severity)) {
    const banner = element7(doc, "button", `${VISUAL_CLASSES.banner} ${severity === "blocking" ? VISUAL_CLASSES.bannerBlocking : VISUAL_CLASSES.bannerNotice}`);
    banner.type = "button";
    banner.append(
      element7(
        doc,
        "span",
        void 0,
        translate(model.language, severity === "blocking" ? "issue.banner.blocking" : "issue.banner.notice")
      )
    );
    banner.append(element7(doc, "span", VISUAL_CLASSES.bannerCount, issueCountText(model.language, model.issues.length)));
    banner.addEventListener("click", () => {
      openIssues(banner);
    });
    card.append(banner);
  }
  const actionError = element7(doc, "p", VISUAL_CLASSES.actionError);
  actionError.setAttribute("role", "status");
  actionError.hidden = true;
  card.append(actionError);
  const settingsError = element7(doc, "p", VISUAL_CLASSES.settingsError);
  settingsError.setAttribute("role", "status");
  settingsError.hidden = true;
  card.append(settingsError);
  if (model.control.notice !== null) {
    const notice = element7(doc, "p", VISUAL_CLASSES.controlNotice, model.control.notice);
    card.append(notice);
  }
  if (model.advisory !== null) {
    const advisory = element7(doc, "p", VISUAL_CLASSES.advisory, model.advisory.text);
    advisory.setAttribute("role", "status");
    card.append(advisory);
  }
  if (model.status !== null) {
    card.append(element7(doc, "p", VISUAL_CLASSES.status, model.status));
  }
  if (model.statusNote !== null) {
    card.append(element7(doc, "p", `${VISUAL_CLASSES.status} ${VISUAL_CLASSES.muted}`, model.statusNote));
  }
  const graph = element7(doc, "section", VISUAL_CLASSES.graphSurface);
  const viewport = element7(doc, "div", VISUAL_CLASSES.viewport);
  viewport.tabIndex = 0;
  viewport.setAttribute("role", "img");
  viewport.setAttribute("aria-labelledby", `${idPrefix}-chart-title`);
  viewport.setAttribute("aria-describedby", `${idPrefix}-chart-description`);
  const readout = element7(doc, "p", VISUAL_CLASSES.readout, readoutDisplayText(model, null));
  readout.textContent = "";
  readout.setAttribute("aria-live", "polite");
  const hint = element7(doc, "p", `${VISUAL_CLASSES.readoutHint} ${VISUAL_CLASSES.visuallyHidden}`, translate(model.language, "graph.hint"));
  const legend = element7(doc, "p", `${VISUAL_CLASSES.legend} ${VISUAL_CLASSES.visuallyHidden}`);
  function legendKeys(current, selected) {
    const keys = [];
    if (current.chart.days.some((day) => day.role === "today")) {
      keys.push("graph.legend.today");
    }
    if (current.chart.days.some((day) => day.role === "future")) {
      keys.push("graph.legend.tomorrow");
    }
    if (current.chart.days.some((day) => day.role === "past")) {
      keys.push("graph.legend.past");
    }
    keys.push("graph.legend.cheap", "graph.legend.expensive");
    if (nowState.mark !== null) {
      keys.push("graph.legend.current");
    }
    if (current.bands.installed.length > 0) {
      keys.push("graph.legend.installed");
    }
    if (current.bands.proposal.length > 0) {
      keys.push("graph.legend.proposal");
    }
    if (!current.timesAvailable) {
      keys.push("graph.noZone");
    }
    if (selected !== null) {
      keys.push("graph.legend.selection");
    }
    return keys;
  }
  function paintLegend(selected) {
    legend.replaceChildren(
      ...legendKeys(model, selected).map(
        (key) => element7(doc, "span", void 0, translate(model.language, key))
      )
    );
  }
  let interaction = null;
  let drawn = null;
  const initial = input.size?.() ?? FALLBACK_SIZE;
  let size = { width: initial.width, height: chartHeightForWidth(initial.width) };
  let nowState = chartNowAt(model.chart.marks, now());
  const setTimer = input.setTimer ?? ((callback, delayMs) => window.setTimeout(callback, delayMs));
  const clearTimer = input.clearTimer ?? ((handle) => window.clearTimeout(handle));
  let boundaryHandle = null;
  let nowValue = null;
  function render(next) {
    size = next;
    nowState = chartNowAt(model.chart.marks, now());
    if (nowValue !== null) {
      const text5 = nowSummaryText(model, currentPriceText(model, nowState.mark));
      nowValue.hidden = text5 === null;
      nowValue.textContent = text5 ?? "";
    }
    const labels2 = {
      time: (instantMs2) => clock(model.format, instantMs2)
    };
    const result = renderChart({
      series: model.chart,
      bands: model.bands,
      now: nowState,
      width: size.width,
      height: size.height,
      selected: interaction?.selection() ?? null,
      title: translate(model.language, "graph.title"),
      description: graphDescription(
        model,
        interaction?.selection() ?? null,
        nowState.mark,
        model.chart.marks,
        model.chart.days.length
      ),
      labels: labels2,
      ids: { title: `${idPrefix}-chart-title`, description: `${idPrefix}-chart-description` }
    });
    drawn?.element.remove();
    drawn = result;
    viewport.style.height = `${chartHeightForWidth(size.width)}px`;
    viewport.append(result.element);
    publish();
  }
  function publish() {
    if (interaction === null || drawn === null) {
      return;
    }
    interaction.setGeometry({
      scale: drawn.scale,
      targets: drawn.targets,
      marks: model.chart.marks,
      current: nowState.mark
    });
    applyFocus(drawn, interaction.selection()?.startMs ?? null);
  }
  function scheduleBoundary() {
    cancelBoundary();
    if (destroyed) {
      return;
    }
    const at = now();
    const boundary = nextIntervalBoundary(model.chart.marks, at);
    if (boundary === null || boundary - at > BOUNDARY_HORIZON_MS) {
      return;
    }
    boundaryHandle = setTimer(onBoundary, Math.max(0, boundary - at));
  }
  function cancelBoundary() {
    if (boundaryHandle === null) {
      return;
    }
    clearTimer(boundaryHandle);
    boundaryHandle = null;
  }
  function onBoundary() {
    boundaryHandle = null;
    if (destroyed) {
      return;
    }
    if (chartNowAt(model.chart.marks, now()).identity !== nowState.identity) {
      render(size);
      paintLegend(interaction?.selection() ?? null);
    }
    scheduleBoundary();
  }
  const summary = element7(doc, "div", VISUAL_CLASSES.summary);
  const summaryExtremes = element7(doc, "span", VISUAL_CLASSES.summaryExtremes);
  const maxLine = element7(doc, "span", VISUAL_CLASSES.summaryMax);
  maxLine.append(element7(doc, "span", VISUAL_CLASSES.summaryArrow, "▲"), doc.createTextNode(` ${model.summary.maxFigure ?? ""}`));
  maxLine.querySelector(`.${VISUAL_CLASSES.summaryArrow}`)?.setAttribute("aria-hidden", "true");
  maxLine.setAttribute("aria-label", `${translate(model.language, "graph.summary.max")} ${model.summary.max ?? ""}`);
  maxLine.hidden = model.summary.max === null;
  const minLine = element7(doc, "span", VISUAL_CLASSES.summaryMin);
  minLine.append(element7(doc, "span", VISUAL_CLASSES.summaryArrow, "▼"), doc.createTextNode(` ${model.summary.minFigure ?? ""}`));
  minLine.querySelector(`.${VISUAL_CLASSES.summaryArrow}`)?.setAttribute("aria-hidden", "true");
  minLine.setAttribute("aria-label", `${translate(model.language, "graph.summary.min")} ${model.summary.min ?? ""}`);
  minLine.hidden = model.summary.min === null;
  summaryExtremes.append(maxLine, minLine);
  const currentText = nowSummaryText(model, model.summary.current);
  const currentValue = element7(doc, "span", VISUAL_CLASSES.summaryCurrent, currentText ?? "");
  currentValue.hidden = currentText === null;
  nowValue = currentValue;
  summary.append(summaryExtremes, currentValue);
  graph.append(summary, viewport, readout, hint, legend);
  card.append(graph);
  interaction = createChartInteraction({
    // Listeners on the stable outer section; measurement and observation on the viewport alone.
    host: graph,
    viewport,
    surface: () => viewport.querySelector("svg"),
    fallbackSize: size,
    now,
    measure: input.measure,
    observe: input.observe,
    onResize: (next) => {
      render(next);
    },
    onSelectionChange: (mark) => {
      if (drawn !== null) {
        applyFocus(drawn, mark?.startMs ?? null);
      }
      readout.textContent = mark === null ? "" : readoutDisplayText(model, mark);
      paintLegend(mark);
      if (drawn !== null) {
        const description = graphDescription(
          model,
          mark,
          nowState.mark,
          model.chart.marks,
          model.chart.days.length
        );
        drawn.element.querySelector("desc")?.replaceChildren(document.createTextNode(description));
      }
    },
    // The viewport and the readout are the region where a press belongs to the chart; anywhere else
    // in the card is "outside" and clears the selection.
    isInsideInteractive: (node) => node instanceof Node && (viewport.contains(node) || readout.contains(node))
  });
  publish();
  for (const kind of periodBlockOrder(model)) {
    const block = kind === "installed" ? periodBlock(doc, model, "installed", model.installedPeriods) : periodBlock(doc, model, "proposal", model.proposalPeriods);
    block.classList.add(VISUAL_CLASSES.visuallyHidden);
    card.append(block);
  }
  const bar = element7(doc, "div", VISUAL_CLASSES.actionBar);
  const barWrap = element7(doc, "div", VISUAL_CLASSES.actionBarWrap);
  barWrap.append(bar);
  const immediateLabelKey = model.control.immediate.labelKey;
  const automaticLabelKey = model.control.automatic.labelKey;
  let actionButton = null;
  let plannerButton = null;
  let strategyButton = null;
  const axisName = (key) => translate(model.language, key);
  const changeWord = translate(model.language, "bar.change");
  function cell(cellClass, id, caption, glyph, value, ariaLabel, wide = false) {
    const button = element7(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.barCell} ${cellClass}`);
    button.type = "button";
    button.dataset["cell"] = id;
    button.setAttribute("aria-label", ariaLabel);
    const captionNode = element7(doc, "span", VISUAL_CLASSES.barCaption, caption);
    captionNode.setAttribute("aria-hidden", "true");
    const line = element7(doc, "span", VISUAL_CLASSES.barValue);
    if (wide) {
      button.classList.add(VISUAL_CLASSES.barWide);
    }
    if (glyph !== null) {
      line.append(glyph);
    }
    const valueNode = element7(doc, "span", VISUAL_CLASSES.settingsValue);
    if (typeof value === "string") {
      valueNode.textContent = value;
    } else {
      value.forEach((part, index) => {
        if (index > 0) {
          valueNode.append(doc.createTextNode(" · "));
        }
        valueNode.append(element7(doc, "span", VISUAL_CLASSES.barPart, part));
      });
    }
    line.append(valueNode);
    button.append(captionNode, line);
    return button;
  }
  const startHelpId = `${idPrefix}-start-help`;
  const startHelp = element7(doc, "p", `${VISUAL_CLASSES.actionHelp} ${VISUAL_CLASSES.visuallyHidden}`, translate(model.language, "action.startHelp"));
  startHelp.id = startHelpId;
  if (immediateLabelKey !== null) {
    const action = model.control.immediate.action;
    const running = action === "stop";
    actionButton = cell(
      `${VISUAL_CLASSES.actionButton}`,
      "charging",
      axisName(running ? "bar.chargingNow" : "bar.chargeNow"),
      running ? stopIcon(doc) : playIcon(doc),
      axisName(running ? "bar.stop" : "bar.start"),
      `${axisName("bar.charging")}: ${axisName(running ? "bar.state.charging" : "bar.state.notCharging")}. ${axisName(immediateLabelKey)}`
    );
    actionButton.dataset["action"] = action;
    actionButton.disabled = !model.control.canAct;
    actionButton.dataset["renderedDisabled"] = String(!model.control.canAct);
    actionButton.addEventListener("click", () => {
      input.onAction(action, null);
    });
    if (action === "start") {
      actionButton.setAttribute("aria-describedby", startHelpId);
    }
    bar.append(actionButton);
  }
  if (immediateLabelKey === null && model.control.immediate.pending) {
    actionButton = cell(
      `${VISUAL_CLASSES.actionButton}`,
      "charging",
      axisName("bar.chargeNow"),
      playIcon(doc),
      axisName("bar.waiting"),
      `${axisName("bar.charging")}: ${axisName("bar.state.notCharging")}. ${axisName("bar.waiting")}. ${axisName("control.actionPending")}`
    );
    actionButton.dataset["action"] = "start";
    actionButton.disabled = true;
    actionButton.dataset["renderedDisabled"] = "true";
    actionButton.dataset["waiting"] = "true";
    actionButton.setAttribute("aria-busy", "true");
    actionButton.classList.add(VISUAL_CLASSES.barBusy);
    bar.append(actionButton);
  }
  if (automaticLabelKey !== null) {
    const action = model.control.automatic.action;
    const paused = action === "resume";
    plannerButton = cell(
      VISUAL_CLASSES.plannerButton,
      "schedule",
      axisName(paused ? "bar.schedulePaused" : "bar.scheduleActive"),
      paused ? playIcon(doc) : pauseIcon(doc),
      axisName(paused ? "action.resumeShort" : "action.pauseAutomaticShort"),
      `${axisName("bar.schedule")}: ${axisName(paused ? "bar.state.schedulePaused" : "bar.state.scheduleActive")}. ${axisName(automaticLabelKey)}`
    );
    plannerButton.dataset["action"] = action;
    plannerButton.disabled = !model.control.canAct;
    plannerButton.dataset["renderedDisabled"] = String(!model.control.canAct);
    plannerButton.addEventListener("click", () => {
      if (action === "pause") {
        openPause();
        return;
      }
      if (action === "resume") {
        input.onAction("resume", null);
      }
    });
    bar.append(plannerButton);
  }
  if (model.strategy.selected !== null) {
    strategyButton = cell(
      VISUAL_CLASSES.strategyButton,
      "strategy",
      axisName("strategy.title"),
      strategyIcon(doc, model.strategy.selectedId),
      model.strategy.selected,
      `${axisName("strategy.title")}: ${model.strategy.selected}. ${changeWord}`
    );
    strategyButton.addEventListener("click", () => {
      openStrategy();
    });
    bar.append(strategyButton);
  }
  const planParts = planSummaryParts(model.language, model.dashboardSettings, model.today);
  const planCaption = axisName("bar.plan");
  const planTrigger = cell(
    VISUAL_CLASSES.settingsTrigger,
    "plan",
    planCaption,
    null,
    planParts,
    `${planCaption}: ${planParts.join(" · ")}. ${changeWord}`,
    true
  );
  planTrigger.dataset["setting"] = "plan";
  planTrigger.addEventListener("click", () => {
    input.onOpenSettings("plan");
  });
  bar.append(planTrigger);
  card.append(barWrap);
  if (actionButton !== null && actionButton.dataset["action"] === "start") {
    card.append(startHelp);
  }
  mountCard();
  paintLegend(null);
  render(size);
  scheduleBoundary();
  function mountCard() {
    if (!input.mount.contains(card)) {
      input.mount.append(card);
    }
  }
  function openIssues(opener) {
    if (destroyed) {
      return;
    }
    capabilityDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    issuesDialog.show({
      title: translate(model.language, "dialog.issues"),
      body: issueListBody(doc, model),
      opener
    });
  }
  function openCapabilities() {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    capabilityDialog.show({
      title: translate(model.language, "cap.title"),
      body: capabilityBody(doc, model),
      opener: help
    });
  }
  function openPause() {
    if (destroyed || model.control.choices.length === 0) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const body = element7(doc, "div");
    body.append(element7(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(model.language, "pause.intro")));
    const list4 = element7(doc, "div", VISUAL_CLASSES.pauseChoices);
    for (const choice of model.control.choices) {
      const button = element7(doc, "button", VISUAL_CLASSES.choiceButton, translate(model.language, choice.labelKey));
      button.type = "button";
      button.dataset["choice"] = choice.id;
      button.disabled = !model.control.canAct;
      button.addEventListener("click", () => {
        pauseDialog.hide({ restoreFocus: false });
        input.onAction("stop", choice.id);
      });
      list4.append(button);
    }
    body.append(list4);
    pauseDialog.show({
      title: translate(model.language, "pause.sheetTitle"),
      body,
      // The sheet belongs to the automatic control, so focus returns to *that* button.
      opener: plannerButton
    });
  }
  function openVehicleChoice() {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const body = element7(doc, "div");
    if (!input.isAdmin) {
      body.append(element7(doc, "p", VISUAL_CLASSES.settingsReadOnly, translate(model.language, "settings.readOnly")));
    }
    const group = element7(doc, "div", VISUAL_CLASSES.vehicleChoices);
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", translate(model.language, "vehicleLine.dialogTitle"));
    const plannedId = model.soc?.vehicle_id ?? model.targetVehicleId;
    const name = `${idPrefix}-vehicle-choice`;
    for (const choice of vehicleChoicesFor(model.language, model.vehicles, plannedId)) {
      const label = element7(doc, "label", VISUAL_CLASSES.vehicleChoice);
      const radio = element7(doc, "input");
      radio.type = "radio";
      radio.name = name;
      radio.value = choice.id;
      radio.checked = choice.selected;
      radio.disabled = !input.isAdmin;
      radio.dataset["vehicle"] = choice.id;
      radio.addEventListener("change", () => {
        if (!radio.checked || choice.selected || !input.isAdmin) {
          return;
        }
        vehicleDialog.hide({ restoreFocus: false });
        input.onSelectVehicle?.(choice.id);
      });
      label.append(
        radio,
        element7(doc, "span", VISUAL_CLASSES.vehicleChoiceName, choice.name),
        element7(
          doc,
          "span",
          VISUAL_CLASSES.vehicleChoiceCharge,
          choice.charge ?? translate(model.language, "vehicleLine.noReading")
        )
      );
      group.append(label);
    }
    body.append(group);
    vehicleDialog.show({
      title: translate(model.language, "vehicleLine.dialogTitle"),
      body,
      opener: vehicleButton
    });
  }
  function openStrategy() {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const body = element7(doc, "div");
    body.append(element7(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(model.language, "strategy.intro")));
    if (!input.isAdmin) {
      body.append(element7(doc, "p", VISUAL_CLASSES.settingsReadOnly, translate(model.language, "settings.readOnly")));
    }
    for (const row of model.strategy.rows) {
      const item = element7(doc, "div", VISUAL_CLASSES.strategyRow);
      const selected = row.id === model.strategy.selectedId;
      const writable = row.available && !selected && input.isAdmin;
      const button = element7(doc, "button", VISUAL_CLASSES.choiceButton, translate(model.language, row.labelKey));
      button.type = "button";
      button.dataset["strategy"] = row.id;
      button.disabled = !writable;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      if (writable) {
        button.addEventListener("click", () => {
          strategyDialog.hide({ restoreFocus: false });
          vehicleDialog.hide({ restoreFocus: false });
          input.onSelectStrategy(row.id);
        });
      }
      item.append(button);
      if (row.reason !== null) {
        item.append(element7(doc, "span", VISUAL_CLASSES.strategyReason, row.reason));
        if (!row.available && model.site !== null && SOLAR_SETUP_ROWS.has(row.id) && row.reasonCode !== STRATEGY_NEEDS_TOTAL_POWER) {
          const link = element7(doc, "button", VISUAL_CLASSES.strategyLink, translate(model.language, "strategy.setupSolar"));
          link.type = "button";
          link.dataset["action"] = "setup-solar";
          link.addEventListener("click", () => {
            strategyDialog.hide({ restoreFocus: false });
            vehicleDialog.hide({ restoreFocus: false });
            openSettingsOverview();
            overviewBodyNode?.querySelector("[data-section='solar']")?.scrollIntoView?.({ block: "nearest" });
            overviewBodyNode?.querySelector("[data-edit-solar]")?.focus();
          });
          item.append(link);
        }
        if (!row.available && row.reasonCode === STRATEGY_NEEDS_TOTAL_POWER && input.isAdmin) {
          const link = element7(doc, "button", VISUAL_CLASSES.strategyLink, translate(model.language, "strategy.setupSite"));
          link.type = "button";
          link.dataset["action"] = "setup-site";
          link.addEventListener("click", () => {
            strategyDialog.hide({ restoreFocus: false });
            vehicleDialog.hide({ restoreFocus: false });
            input.onOpenEntityEditor?.("site");
          });
          item.append(link);
        }
      }
      body.append(item);
    }
    strategyDialog.show({
      title: translate(model.language, "strategy.dialogTitle"),
      body,
      opener: strategyButton
    });
  }
  function settingsOverviewBody() {
    const body = element7(doc, "div");
    overviewBodyNode = body;
    overviewNoticeNode = element7(doc, "p", VISUAL_CLASSES.settingsNotice);
    overviewNoticeNode.hidden = true;
    overviewNoticeNode.setAttribute("role", "status");
    body.append(overviewNoticeNode);
    paintOverviewNotice();
    if (!input.isAdmin) {
      body.append(element7(doc, "p", VISUAL_CLASSES.settingsReadOnly, translate(model.language, "settings.readOnly")));
    }
    const marketSection = element7(doc, "section", VISUAL_CLASSES.settingsSection);
    marketSection.dataset["section"] = "market";
    marketSection.append(
      element7(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, translate(model.language, "settings.section.market"))
    );
    if (model.contextArea !== null || model.contextAreaName !== null) {
      marketSection.append(
        overviewRow(
          "area",
          translate(model.language, "context.area"),
          marketAreaLabel(model.language, model.contextAreaName, model.contextAreaId)
        )
      );
    }
    for (const fiscal of fiscalRows(model.language, model.dashboardFiscal)) {
      marketSection.append(overviewRow(fiscal.key, fiscal.label, fiscal.value));
    }
    const marketButton = marketTrigger(
      doc,
      model.language,
      marketAreaLabel(model.language, model.contextAreaName, model.contextAreaId)
    );
    marketButton.classList.add(VISUAL_CLASSES.settingsSectionConfigure);
    marketButton.addEventListener("click", () => {
      input.onOpenMarket();
    });
    marketSection.append(marketButton);
    body.append(marketSection);
    vehicleRows = model.vehicles.map((entry) => ({ ...entry }));
    vehicleListSlot = element7(doc, "div");
    vehicleListSlot.dataset["slot"] = "vehicles";
    body.append(vehicleListSlot);
    entitySlot = element7(doc, "section", VISUAL_CLASSES.settingsSection);
    entitySlot.dataset["section"] = "entities";
    body.append(entitySlot);
    body.append(siteSectionBody());
    const solar = solarSectionBody();
    if (solar !== null) {
      body.append(solar);
    }
    paintEntities();
    if (input.isAdmin) {
      body.append(supportSectionBody());
    }
    return body;
  }
  function supportSectionBody() {
    const section = element7(doc, "section", VISUAL_CLASSES.settingsSection);
    section.dataset["section"] = "support";
    section.append(
      element7(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, translate(model.language, "settings.section.support")),
      element7(doc, "p", VISUAL_CLASSES.muted, translate(model.language, "debug.intro"))
    );
    const button = element7(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSectionConfigure}`);
    button.type = "button";
    button.dataset["downloadDebug"] = "true";
    button.addEventListener("click", () => {
      if (!debugPending) {
        input.onDownloadDebug?.();
      }
    });
    debugButton = button;
    paintDebugButton();
    section.append(button);
    return section;
  }
  let debugButton = null;
  let debugPending = false;
  function paintDebugButton() {
    if (debugButton === null) {
      return;
    }
    debugButton.disabled = debugPending;
    debugButton.textContent = translate(model.language, debugPending ? "debug.preparing" : "debug.download");
  }
  function overviewRow(key, label, value) {
    const row = element7(doc, "div", VISUAL_CLASSES.capabilityItem);
    row.dataset["row"] = key;
    row.append(element7(doc, "span", VISUAL_CLASSES.capabilityLabel, label), element7(doc, "span", summaryValueClass(value), value));
    return row;
  }
  let overviewBodyNode = null;
  let entityState = input.isAdmin ? { kind: "loading" } : { kind: "adminOnly" };
  let entitySlot = null;
  let vehicleListSlot = null;
  let vehicleRows = [];
  let siteEntitySlot = null;
  let siteButtonSlot = null;
  function chargeFor(row) {
    const soc = model.soc;
    if (soc !== null && soc.vehicle_id === row.id && soc.value !== null) {
      return `${soc.estimated ? "~" : ""}${percentAmount(model.language, soc.value)}`;
    }
    return row.soc_percent === null ? translate(model.language, "vehicleLine.noReading") : percentAmount(model.language, row.soc_percent);
  }
  function unreadableLine(state) {
    if (state.kind === "loading") {
      return element7(doc, "p", VISUAL_CLASSES.muted, translate(model.language, "entity.loading"));
    }
    if (state.kind === "adminOnly") {
      return element7(doc, "p", VISUAL_CLASSES.muted, translate(model.language, "entity.adminOnly"));
    }
    if (state.kind === "failed") {
      const sentence = element7(doc, "p", VISUAL_CLASSES.settingsNotice, translate(model.language, state.failure.sentenceKey));
      if (state.failure.code !== null) {
        sentence.dataset["code"] = state.failure.code;
      }
      return sentence;
    }
    return null;
  }
  function changeButton(label, scope, enabled) {
    const button = element7(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSectionConfigure}`, translate(model.language, label));
    button.type = "button";
    button.dataset["editEntities"] = scope;
    button.disabled = !enabled;
    if (enabled) {
      button.addEventListener("click", () => {
        input.onOpenEntityEditor?.(scope);
      });
    }
    return button;
  }
  function fieldEntityName(config, scope, name) {
    const field2 = fieldsOf(config, scope).find((entry) => entry.field === name);
    if (field2 === void 0 || field2.kind !== "entity" || field2.current === null) {
      return null;
    }
    return field2.current.friendlyName;
  }
  function chargerRows(config) {
    const notSet = translate(model.language, "entity.notSet");
    const control = config.control;
    const nodes = [];
    const startStopId = control?.startStop.entityIds[0];
    const startStopName = startStopId !== void 0 ? entityNameIn(config, startStopId) : fieldEntityName(config, "charger", "charge_control");
    nodes.push(overviewRow("start_stop", translate(model.language, "control.startStop"), startStopName ?? notSet));
    const chargeControl = fieldsOf(config, "charger").find((entry) => entry.field === "charge_control");
    if (chargeControl !== void 0 && chargeControl.kind === "entity" && isMissingEntity(chargeControl)) {
      const warning = element7(doc, "p", VISUAL_CLASSES.entityWarning, translate(model.language, "entity.missing.required"));
      warning.dataset["missing"] = "charge_control";
      nodes.push(warning);
    }
    const currentId = control?.current.entityId ?? null;
    nodes.push(
      overviewRow(
        "current",
        translate(model.language, "control.current"),
        control === null ? fieldEntityName(config, "charger", "current_limit") ?? notSet : controlCurrentText(model.language, control, currentId === null ? "" : entityNameIn(config, currentId))
      )
    );
    const energyField = fieldsOf(config, "charger").find((entry) => entry.field === "energy_register_entity");
    let energy = translate(model.language, "entity.foundAutomatically");
    if (energyField !== void 0 && energyField.kind === "entity") {
      if (energyField.current !== null) {
        energy = energyField.current.friendlyName;
      } else {
        const automatic = automaticEntity(energyField);
        if (automatic !== null) {
          energy = translate(model.language, "entity.automatic", { name: automatic.friendlyName });
        }
      }
    }
    nodes.push(overviewRow("energy_register", translate(model.language, "entity.field.energyRegister"), energy));
    const powerField = fieldsOf(config, "charger").find((entry) => entry.field === "power_entity");
    if (powerField !== void 0 && powerField.kind === "entity" && powerField.current !== null) {
      nodes.push(overviewRow("power_entity", translate(model.language, "entity.field.powerEntity"), powerField.current.friendlyName));
    }
    for (const conflict of control?.conflicts ?? []) {
      const warning = element7(
        doc,
        "p",
        VISUAL_CLASSES.entityWarning,
        conflictText(model.language, conflict, entityNameIn(config, conflict.entityId))
      );
      warning.dataset["conflict"] = conflict.entityId;
      nodes.push(warning);
    }
    return nodes;
  }
  function siteRows(config) {
    const nodes = [];
    const fuse = fieldsOf(config, "site").find((entry) => entry.field === "main_fuse_a");
    if (fuse !== void 0 && fuse.kind === "number" && fuse.value !== null) {
      nodes.push(
        overviewRow("main_fuse_a", translate(model.language, "entity.field.mainFuse"), `${formatNumber(model.language, fuse.value, 1)} A`)
      );
    }
    const mode = storedMode(config);
    if (mode !== null) {
      nodes.push(overviewRow("measurement_mode", translate(model.language, "entity.field.measurementMode"), modeLabel(model.language, mode)));
    }
    nodes.push(
      overviewRow(
        "battery",
        translate(model.language, "site.row.battery"),
        fieldEntityName(config, "site", "battery_aggregate_power_entity") ?? translate(model.language, "settings.value.none")
      )
    );
    if (config.site !== null) {
      nodes.push(...siteWarningRows(doc, model.language, config.site));
    }
    return nodes;
  }
  function paintEntities() {
    const state = entityState;
    if (vehicleListSlot !== null) {
      const root = vehicleListSlot.getRootNode();
      const active = root.activeElement;
      const focusId = active !== null && vehicleListSlot.contains(active) ? active.dataset["editVehicle"] : void 0;
      vehicleListSlot.replaceChildren();
      if (vehicleRows.length === 0 && !(state.kind === "ready" && state.config.vehicles.length > 0)) {
        const none = element7(doc, "section", VISUAL_CLASSES.settingsSection);
        none.dataset["section"] = "vehicle";
        none.append(
          element7(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, translate(model.language, "settings.section.vehicle")),
          element7(doc, "p", VISUAL_CLASSES.muted, translate(model.language, "settings.vehicle.none"))
        );
        vehicleListSlot.append(none);
      }
      const extra = state.kind === "ready" ? state.config.vehicles.filter((entry) => !vehicleRows.some((row) => row.id === entry.id)).map((entry) => ({
        id: entry.id,
        name: entry.name,
        soc_entity_id: null,
        capacity_kwh: null,
        capacity_source: null,
        consumption_kwh_per_10km: null,
        max_percent: null,
        soc_percent: null
      })) : [];
      for (const row of [...vehicleRows, ...extra]) {
        vehicleListSlot.append(
          vehicleSummary(doc, model.language, {
            row,
            properties: !extra.includes(row),
            planned: row.id === model.targetVehicleId,
            charge: chargeFor(row),
            isAdmin: input.isAdmin,
            onChange: () => {
              input.onOpenVehicleEditor?.(row.id);
            }
          })
        );
      }
      if (focusId !== void 0) {
        vehicleListSlot.querySelector(`[data-edit-vehicle="${focusId}"]`)?.focus();
      }
    }
    if (entitySlot !== null) {
      entitySlot.replaceChildren(
        element7(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, translate(model.language, "settings.section.entities"))
      );
      const line = unreadableLine(state);
      if (line !== null) {
        entitySlot.append(line);
      }
      if (state.kind === "ready") {
        entitySlot.append(...chargerRows(state.config));
      }
      entitySlot.append(changeButton("entity.edit.charger", "charger", state.kind === "ready" && input.isAdmin));
    }
    if (siteEntitySlot !== null) {
      siteEntitySlot.replaceChildren();
      const line = unreadableLine(state);
      if (line !== null) {
        siteEntitySlot.append(line);
      }
      if (state.kind === "ready" && state.config.site !== null) {
        siteEntitySlot.append(...siteRows(state.config));
      }
    }
    if (siteButtonSlot !== null) {
      siteButtonSlot.replaceChildren(
        changeButton("entity.edit.site", "site", state.kind === "ready" && state.config.site !== null && input.isAdmin)
      );
    }
  }
  function siteSectionBody() {
    const site = model.site;
    const siteSection = element7(doc, "section", VISUAL_CLASSES.settingsSection);
    siteSection.dataset["section"] = "site";
    siteEntitySlot = null;
    siteButtonSlot = null;
    if (site === null) {
      siteSection.append(
        element7(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, translate(model.language, "settings.section.site"))
      );
      siteSection.append(element7(doc, "p", VISUAL_CLASSES.muted, translate(model.language, "site.none")));
      return siteSection;
    }
    const name = site.name.trim();
    siteSection.append(
      element7(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, name === "" ? translate(model.language, "settings.section.site") : name)
    );
    siteSection.append(element7(doc, "p", VISUAL_CLASSES.siteApplies, site.appliesToText));
    siteEntitySlot = element7(doc, "div");
    siteEntitySlot.dataset["slot"] = "site-entities";
    siteSection.append(siteEntitySlot);
    activeSlot = element7(doc, "div");
    activeSlot.dataset["slot"] = "active-control";
    siteSection.append(activeSlot);
    paintActiveControl();
    siteButtonSlot = element7(doc, "div");
    siteSection.append(siteButtonSlot);
    return siteSection;
  }
  function solarSectionBody() {
    const site = model.site;
    if (site === null) {
      return null;
    }
    const section = element7(doc, "section", VISUAL_CLASSES.settingsSection);
    section.dataset["section"] = "solar";
    section.append(element7(doc, "h4", VISUAL_CLASSES.settingsSectionHeading, translate(model.language, "settings.section.solar")));
    section.append(element7(doc, "p", VISUAL_CLASSES.siteApplies, site.appliesToText));
    section.append(
      overviewRow(
        "solar_priority",
        translate(model.language, "site.solarPriority.title"),
        translate(model.language, site.solarPriority === "car_first" ? "site.solarPriority.carFirst" : "site.solarPriority.batteryFirst")
      )
    );
    const sources = site.solarForecastChoices.filter((choice) => choice.selected).map((choice) => choice.title);
    section.append(
      overviewRow(
        "solar_forecast",
        translate(model.language, "site.solarForecast.title"),
        sources.length === 0 ? translate(model.language, "settings.value.none") : sources.join(", ")
      )
    );
    const button = element7(doc, "button", `${VISUAL_CLASSES.button} ${VISUAL_CLASSES.settingsSectionConfigure}`, translate(model.language, "site.solar.change"));
    button.type = "button";
    button.dataset["editSolar"] = "true";
    button.disabled = !(input.isAdmin && site.writable);
    button.addEventListener("click", () => {
      input.onOpenSolarEditor?.();
    });
    section.append(button);
    return section;
  }
  let activeSlot = null;
  let activeOverride = null;
  let activePending = false;
  let activeNotice = null;
  function paintActiveControl() {
    const site = model.site;
    if (activeSlot === null || site === null) {
      return;
    }
    const state = activeOverride ?? {
      available: site.activeControlAvailable,
      enabled: site.activeControlEnabled,
      reason: site.activeControlReason
    };
    const stateText = activePending ? translate(model.language, "site.activeControl.pending") : translate(model.language, state.enabled ? "site.activeControl.on" : "site.activeControl.off");
    const nodes = [];
    const row = element7(doc, "div", VISUAL_CLASSES.capabilityItem);
    row.dataset["row"] = "active-control";
    const title = element7(doc, "label", VISUAL_CLASSES.capabilityLabel, translate(model.language, "site.activeControl.title"));
    const group = element7(doc, "span", VISUAL_CLASSES.switchGroup);
    const status = element7(doc, "span", VISUAL_CLASSES.capabilityState, stateText);
    status.dataset["role"] = "active-control-state";
    group.append(status);
    if (site.writable && site.activeControlWritable) {
      const control = doc.createElement("input");
      control.type = "checkbox";
      control.className = VISUAL_CLASSES.switchControl;
      control.id = `${idPrefix}-active-control`;
      control.setAttribute("role", "switch");
      control.checked = state.enabled;
      control.disabled = activePending || !state.available && !state.enabled;
      if (activePending) {
        control.setAttribute("aria-busy", "true");
      }
      title.htmlFor = control.id;
      control.addEventListener("change", () => {
        const chosen = control.checked;
        control.checked = state.enabled;
        input.onSetActiveControl?.(state.enabled, chosen);
      });
      group.append(control);
    }
    row.append(title, group);
    nodes.push(row);
    if (state.reason !== null) {
      nodes.push(element7(doc, "p", VISUAL_CLASSES.capabilityNote, state.reason.text));
    } else if (!site.writable) {
      nodes.push(element7(doc, "p", VISUAL_CLASSES.capabilityNote, translate(model.language, "site.activeControl.available")));
    }
    nodes.push(element7(doc, "p", VISUAL_CLASSES.capabilityNote, translate(model.language, "site.activeControl.note")));
    if (activeNotice !== null) {
      const notice = element7(
        doc,
        "p",
        activeNotice.tone === "warning" ? `${VISUAL_CLASSES.activeNotice} ${VISUAL_CLASSES.activeNoticeWarning}` : VISUAL_CLASSES.activeNotice
      );
      notice.setAttribute("role", "status");
      for (const line of activeNotice.lines) {
        notice.append(element7(doc, "span", "", line));
      }
      if (activeNotice.code !== null) {
        notice.dataset["code"] = activeNotice.code;
      }
      nodes.push(notice);
    }
    activeSlot.replaceChildren(...nodes);
  }
  let overviewNotice = null;
  let overviewNoticeNode = null;
  function paintOverviewNotice() {
    if (overviewNoticeNode === null) {
      return;
    }
    if (overviewNotice === null) {
      overviewNoticeNode.hidden = true;
      overviewNoticeNode.textContent = "";
      overviewNoticeNode.removeAttribute("data-code");
      return;
    }
    overviewNoticeNode.hidden = false;
    overviewNoticeNode.textContent = translate(model.language, overviewNotice.sentenceKey);
    if (overviewNotice.code === null) {
      overviewNoticeNode.removeAttribute("data-code");
    } else {
      overviewNoticeNode.dataset["code"] = overviewNotice.code;
    }
  }
  function openSettingsOverview() {
    if (destroyed) {
      return;
    }
    overviewNotice = null;
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    entityDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.show({
      title: model.chargerName === null ? translate(model.language, "settings.overview.title") : translate(model.language, "settings.overview.titleNamed", { name: model.chargerName }),
      body: settingsOverviewBody(),
      opener: settingsGeneral
    });
    input.onSettingsOverviewOpened?.();
  }
  let historyState = { kind: "loading" };
  const historyUi = { list: "days", range: "thisMonth", exporting: false, notice: null };
  function paintHistory() {
    if (destroyed || !historyDialog.isOpen()) {
      return;
    }
    const focused = input.mount instanceof ShadowRoot ? input.mount.activeElement : doc.activeElement;
    const refocus = focused !== null && historyDialog.element.contains(focused) ? focused.dataset["list"] !== void 0 ? `[data-list="${focused.dataset["list"]}"]` : focused.dataset["action"] === "export" ? "[data-action='export']" : focused.dataset["exportRange"] !== void 0 ? "[data-export-range]" : null : null;
    historyDialog.show({
      title: model.chargerName === null ? translate(model.language, "history.title") : translate(model.language, "history.titleNamed", { name: model.chargerName }),
      body: historyBody(doc, model.language, historyState, historyUi, {
        onList: (list4) => {
          historyUi.list = list4;
          paintHistory();
        },
        onRange: (range) => {
          historyUi.range = range;
          paintHistory();
        },
        onExport: () => {
          input.onExportHistory?.(historyUi.range);
        }
      })
    });
    if (refocus !== null) {
      historyDialog.element.querySelector(refocus)?.focus();
    }
  }
  function openHistory() {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    historyUi.notice = null;
    historyUi.exporting = false;
    historyState = { kind: "loading" };
    historyDialog.show({
      title: translate(model.language, "history.title"),
      body: historyBody(doc, model.language, historyState, historyUi, {
        onList: () => void 0,
        onRange: () => void 0,
        onExport: () => void 0
      }),
      opener: historyButton
    });
    paintHistory();
    input.onOpenHistory?.();
  }
  help.addEventListener("click", () => {
    openCapabilities();
  });
  historyButton.addEventListener("click", () => {
    openHistory();
  });
  settingsGeneral.addEventListener("click", () => {
    openSettingsOverview();
  });
  let settingsKind = null;
  let settingsForm = null;
  let settingsBody = null;
  let settingsNotice = null;
  let settingsNoticeNode = null;
  let settingsSaveButton = null;
  let settingsReapplyButton = null;
  let settingsPending = false;
  let settingsBodyReader = null;
  function settingsTitleKey(kind) {
    return `settings.${kind}.title`;
  }
  function settingsTriggerFor(kind) {
    return bar.querySelector(`[data-setting="${kind}"]`);
  }
  function applySettingsPending() {
    if (settingsSaveButton !== null) {
      settingsSaveButton.disabled = settingsPending;
    }
    if (settingsReapplyButton !== null) {
      settingsReapplyButton.disabled = settingsPending;
    }
    const cancel = settingsBody?.querySelector("[data-action='cancel']") ?? null;
    if (cancel !== null) {
      cancel.disabled = settingsPending;
    }
  }
  function paintSettingsNotice() {
    settingsNoticeNode?.remove();
    settingsNoticeNode = null;
    if (settingsBody !== null && settingsNotice !== null) {
      if (settingsForm === null) {
        settingsBody.replaceChildren();
      }
      const paragraph = element7(doc, "p", VISUAL_CLASSES.settingsNotice, translate(model.language, settingsNotice.sentenceKey));
      if (settingsNotice.code !== null) {
        paragraph.dataset["code"] = settingsNotice.code;
      }
      settingsBody.append(paragraph);
      settingsNoticeNode = paragraph;
    }
    applySettingsPending();
  }
  function renderSettingsBody() {
    const form = settingsForm;
    if (form === null || destroyed) {
      return;
    }
    const built = settingsEditorBody(
      doc,
      model.language,
      form,
      {
        onSave: (values) => input.onSaveSettings(form.kind, values),
        onCancel: () => closeSettingsEditor(),
        onReload: () => input.onReloadSettings(form.kind),
        onReapply: (values) => input.onReapplySettings(form.kind, values)
      },
      `${idPrefix}-settings-${form.kind}`
    );
    settingsBody = built.body;
    settingsBodyReader = built.values;
    settingsSaveButton = built.body.querySelector(`.${VISUAL_CLASSES.settingsSave}`);
    settingsReapplyButton = built.body.querySelector(`.${VISUAL_CLASSES.settingsReapply}`);
    settingsDialog.show({
      title: translate(model.language, settingsTitleKey(form.kind)),
      body: built.body,
      opener: settingsTriggerFor(form.kind)
    });
    paintSettingsNotice();
  }
  function openSettingsEditor(kind) {
    if (destroyed) {
      return;
    }
    settingsKind = kind;
    settingsForm = null;
    settingsBody = null;
    settingsBodyReader = null;
    settingsNotice = null;
    settingsNoticeNode = null;
    settingsSaveButton = null;
    settingsReapplyButton = null;
    settingsPending = false;
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const loading = element7(
      doc,
      "p",
      `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`,
      translate(model.language, "settings.loading")
    );
    settingsBody = loading;
    settingsDialog.show({
      title: translate(model.language, settingsTitleKey(kind)),
      body: loading,
      opener: settingsTriggerFor(kind)
    });
  }
  function showSettingsEditorForm(form) {
    if (destroyed || settingsKind !== form.kind) {
      return;
    }
    settingsForm = form;
    settingsNotice = null;
    settingsPending = false;
    renderSettingsBody();
  }
  function showSettingsEditorConflict(revision, phases) {
    const form = settingsForm;
    if (destroyed || form === null) {
      return;
    }
    const values = settingsBodyReader === null ? form.values : settingsBodyReader();
    settingsForm = { ...form, values, conflict: revision, phases };
    settingsNotice = null;
    settingsPending = false;
    renderSettingsBody();
  }
  function setSettingsEditorNotice(failure) {
    settingsNotice = failure;
    paintSettingsNotice();
  }
  function setSettingsEditorPending(pending) {
    settingsPending = pending;
    applySettingsPending();
  }
  function closeSettingsEditor() {
    settingsDialog.hide();
    settingsKind = null;
    settingsForm = null;
    settingsBody = null;
    settingsBodyReader = null;
    settingsNotice = null;
    settingsNoticeNode = null;
    settingsSaveButton = null;
    settingsReapplyButton = null;
    settingsPending = false;
  }
  function settingsEditorOpen() {
    return settingsDialog.isOpen() ? settingsKind : null;
  }
  function setSettingsError(failure) {
    if (failure === null) {
      settingsError.hidden = true;
      settingsError.textContent = "";
      settingsError.removeAttribute("data-code");
      return;
    }
    settingsError.hidden = false;
    settingsError.textContent = translate(model.language, failure.sentenceKey);
    if (failure.code === null) {
      settingsError.removeAttribute("data-code");
    } else {
      settingsError.dataset["code"] = failure.code;
    }
  }
  let marketForm = null;
  let marketBody = null;
  let marketNotice = null;
  let marketNoticeNode = null;
  let marketSaveButton = null;
  let marketReapplyButton = null;
  let marketPending = false;
  let marketBodyReader = null;
  function marketTriggerFor() {
    return settingsGeneral;
  }
  function applyMarketPending() {
    if (marketSaveButton !== null) {
      marketSaveButton.disabled = marketPending;
    }
    if (marketReapplyButton !== null) {
      marketReapplyButton.disabled = marketPending;
    }
  }
  function paintMarketNotice() {
    marketNoticeNode?.remove();
    marketNoticeNode = null;
    if (marketBody !== null && marketNotice !== null) {
      if (marketForm === null) {
        marketBody.replaceChildren();
      }
      const paragraph = element7(doc, "p", VISUAL_CLASSES.settingsNotice, translate(model.language, marketNotice.sentenceKey));
      if (marketNotice.code !== null) {
        paragraph.dataset["code"] = marketNotice.code;
      }
      marketBody.append(paragraph);
      marketNoticeNode = paragraph;
    }
    applyMarketPending();
  }
  function renderMarketBody() {
    const form = marketForm;
    if (form === null || destroyed) {
      return;
    }
    const built = marketEditorBody(
      doc,
      model.language,
      form,
      {
        onSave: (values) => input.onSaveMarket(values),
        onReload: () => input.onReloadMarket(),
        onReapply: (values) => input.onReapplyMarket(values),
        onCancel: () => leaveSettingsChild(marketDialog, input.onCancelMarket),
        onAreaChange: (areaId, live) => input.onMarketAreaChange(areaId, live)
      },
      idPrefix,
      homeAssistantCountry(input.hass?.())
    );
    marketBody = built.body;
    marketBodyReader = built.values;
    marketSaveButton = built.body.querySelector(`.${VISUAL_CLASSES.settingsSave}`);
    marketReapplyButton = built.body.querySelector(`.${VISUAL_CLASSES.settingsReapply}`);
    marketDialog.show({
      title: translate(model.language, "market.title"),
      body: built.body,
      opener: marketTriggerFor()
    });
    paintMarketNotice();
  }
  function openMarketEditor() {
    if (destroyed) {
      return;
    }
    marketForm = null;
    marketBody = null;
    marketBodyReader = null;
    marketNotice = null;
    marketNoticeNode = null;
    marketSaveButton = null;
    marketReapplyButton = null;
    marketPending = false;
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const loading = element7(doc, "p", `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`, translate(model.language, "market.loading"));
    marketBody = loading;
    marketDialog.show({
      title: translate(model.language, "market.title"),
      body: loading,
      opener: marketTriggerFor()
    });
  }
  function showMarketEditorForm(form) {
    if (destroyed || !marketDialog.isOpen()) {
      return;
    }
    marketForm = form;
    marketNotice = null;
    marketPending = false;
    renderMarketBody();
  }
  function showMarketEditorConflict(revision) {
    const form = marketForm;
    if (destroyed || form === null) {
      return;
    }
    const values = marketBodyReader === null ? form.values : marketBodyReader();
    marketForm = { ...form, values, conflict: revision };
    marketNotice = null;
    marketPending = false;
    renderMarketBody();
  }
  function setMarketEditorNotice(failure) {
    marketNotice = failure;
    paintMarketNotice();
  }
  function setMarketEditorPending(pending) {
    marketPending = pending;
    applyMarketPending();
  }
  function marketEditorOpen() {
    return marketDialog.isOpen();
  }
  function closeMarketEditor(options = {}) {
    marketDialog.hide(options);
    marketForm = null;
    marketBody = null;
    marketBodyReader = null;
    marketNotice = null;
    marketNoticeNode = null;
    marketSaveButton = null;
    marketReapplyButton = null;
    marketPending = false;
  }
  let entityEditor = null;
  function openEntityEditor(scope, config, notice = null) {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const built = entityEditorBody(
      doc,
      model.language,
      {
        scope,
        config,
        hass: () => input.hass?.(),
        appliesText: scope === "site" ? model.site?.appliesToText ?? null : null
      },
      {
        onSave: (draft) => input.onSaveEntities?.(scope, draft),
        onCancel: () => leaveSettingsChild(entityDialog, input.onCancelEntities)
      },
      idPrefix
    );
    entityEditor = { scope, built };
    if (notice !== null) {
      built.setNotice(translate(model.language, notice.sentenceKey), notice.code);
    }
    entityDialog.show({
      title: translate(model.language, scope === "site" ? "entity.editor.site" : "entity.editor.charger"),
      body: built.body,
      opener: settingsGeneral
    });
  }
  function hideForChildDialog() {
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
  }
  let vehicleEditorId = null;
  function openVehicleEditor(vehicleId, config, notice = null, adopted) {
    const row = adopted ?? model.vehicles.find((entry) => entry.id === vehicleId) ?? null;
    const sensor = config?.vehicles.find((entry) => entry.id === vehicleId) ?? null;
    if (destroyed || row === null && sensor === null) {
      return;
    }
    hideForChildDialog();
    const built = vehicleEditorBody(
      doc,
      model.language,
      { vehicleId, row, sensor },
      {
        onSave: (draft) => input.onSaveVehicle?.(vehicleId, draft),
        onCancel: () => leaveSettingsChild(entityDialog, input.onCancelEntities)
      },
      idPrefix
    );
    entityEditor = { scope: "vehicle", built };
    vehicleEditorId = vehicleId;
    if (notice !== null) {
      built.setNotice(translate(model.language, notice.sentenceKey), notice.code);
    }
    entityDialog.show({
      title: translate(model.language, "settings.vehicle.dialogTitle", {
        name: row?.name ?? sensor?.name ?? translate(model.language, "settings.vehicle.unnamed")
      }),
      body: built.body,
      opener: settingsGeneral
    });
  }
  function openSolarEditor(notice = null) {
    if (destroyed || model.site === null) {
      return;
    }
    hideForChildDialog();
    const built = solarEditorBody(
      doc,
      model.language,
      model.site,
      {
        onSave: (draft) => input.onSaveSolar?.(draft),
        onCancel: () => leaveSettingsChild(entityDialog, input.onCancelSolar)
      },
      idPrefix
    );
    entityEditor = { scope: "solar", built };
    if (notice !== null) {
      built.setNotice(translate(model.language, notice.sentenceKey), notice.code);
    }
    entityDialog.show({
      title: translate(model.language, "site.solar.dialogTitle"),
      body: built.body,
      opener: settingsGeneral
    });
  }
  function vehicleEditorOpen() {
    return entityEditor !== null && entityEditor.scope === "vehicle" && entityDialog.isOpen() ? vehicleEditorId : null;
  }
  function solarEditorOpen() {
    return entityEditor !== null && entityEditor.scope === "solar" && entityDialog.isOpen();
  }
  function entityEditorOpen() {
    return entityEditor !== null && entityEditor.scope !== "vehicle" && entityEditor.scope !== "solar" && entityDialog.isOpen() ? entityEditor.scope : null;
  }
  function closeEntityEditor() {
    entityDialog.hide({ restoreFocus: false });
    entityEditor = null;
  }
  return {
    element: card,
    chart: () => interaction,
    selection: () => interaction?.selection() ?? null,
    readoutText: () => readout.textContent ?? "",
    // The opener is what focus returns to; the banner passes itself when it is clicked.
    openIssues: (opener = null) => {
      openIssues(opener);
    },
    openCapabilities,
    openPause,
    openStrategy,
    openSettingsOverview,
    openHistory,
    setHistoryState(state) {
      historyState = state;
      paintHistory();
    },
    setHistoryExport(notice, exporting) {
      historyUi.notice = notice;
      historyUi.exporting = exporting;
      paintHistory();
    },
    dialogOpen: (kind) => {
      if (kind === "issues") {
        return issuesDialog.isOpen();
      }
      if (kind === "capabilities") {
        return capabilityDialog.isOpen();
      }
      if (kind === "pause") {
        return pauseDialog.isOpen();
      }
      if (kind === "strategy") {
        return strategyDialog.isOpen();
      }
      if (kind === "history") {
        return historyDialog.isOpen();
      }
      return settingsOverviewDialog.isOpen();
    },
    setActionPending(pending, action, choice) {
      const pressed = action === void 0 ? null : action === "resume" || (choice ?? null) !== null ? plannerButton : actionButton;
      for (const button of [actionButton, plannerButton]) {
        if (button === null) {
          continue;
        }
        button.disabled = pending || button.dataset["renderedDisabled"] === "true";
        const busy = pending && button === pressed || button.dataset["waiting"] === "true";
        button.classList.toggle(VISUAL_CLASSES.barBusy, busy);
        if (busy) {
          button.setAttribute("aria-busy", "true");
        } else {
          button.removeAttribute("aria-busy");
        }
      }
    },
    openSettingsEditor,
    showSettingsEditorForm,
    setSettingsEditorNotice,
    showSettingsEditorConflict,
    setSettingsEditorPending,
    settingsEditorOpen,
    closeSettingsEditor,
    setSettingsError,
    openMarketEditor,
    showMarketEditorForm,
    setMarketEditorNotice,
    showMarketEditorConflict,
    setMarketEditorPending,
    marketEditorOpen,
    closeMarketEditor,
    setActiveControlPending(pending) {
      activePending = pending;
      paintActiveControl();
    },
    adoptActiveControl(site, notice) {
      if (site !== null) {
        activeOverride = {
          available: site.activeControlAvailable,
          enabled: site.activeControlEnabled,
          reason: site.activeControlReason
        };
      }
      activeNotice = notice;
      activePending = false;
      paintActiveControl();
    },
    anyDialogOpen: anyDialogOpenNow,
    setEntityState(state) {
      entityState = state;
      paintEntities();
    },
    openEntityEditor,
    openVehicleEditor,
    vehicleEditorOpen,
    openSolarEditor,
    solarEditorOpen,
    entityEditorOpen,
    setEntityEditorNotice(failure) {
      entityEditor?.built.setNotice(
        failure === null ? null : translate(model.language, failure.sentenceKey),
        failure === null ? null : failure.code
      );
    },
    markEntityFieldErrors(errors) {
      entityEditor?.built.markErrors(errors);
    },
    setEntityEditorPending(pending) {
      entityEditor?.built.setPending(pending);
    },
    setEntityHass(hass) {
      entityEditor?.built.setHass(hass);
    },
    closeEntityEditor,
    setDebugPending(pending) {
      debugPending = pending;
      paintDebugButton();
    },
    setOverviewNotice(failure) {
      overviewNotice = failure;
      paintOverviewNotice();
    },
    closeSettingsOverview() {
      settingsOverviewDialog.hide({ restoreFocus: false });
    },
    setActionError(failure) {
      if (failure === null) {
        actionError.hidden = true;
        actionError.textContent = "";
        actionError.removeAttribute("data-code");
        return;
      }
      actionError.hidden = false;
      actionError.textContent = translate(model.language, failure.sentenceKey);
      if (failure.code === null) {
        actionError.removeAttribute("data-code");
      } else {
        actionError.dataset["code"] = failure.code;
      }
    },
    destroy() {
      if (destroyed) {
        return;
      }
      destroyed = true;
      cancelBoundary();
      interaction?.destroy();
      issuesDialog.destroy();
      capabilityDialog.destroy();
      pauseDialog.destroy();
      strategyDialog.destroy();
      vehicleDialog.destroy();
      settingsDialog.destroy();
      marketDialog.destroy();
      entityDialog.destroy();
      settingsOverviewDialog.destroy();
      historyDialog.destroy();
      card.remove();
    }
  };
}

// src/debug-download.ts
function decodeDebugAnswer(raw) {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const record7 = raw;
  if (record7.api_version !== DEBUG_API_VERSION || typeof record7.ok !== "boolean") {
    return null;
  }
  if (record7.ok) {
    const bundle = record7.bundle;
    if (typeof bundle !== "object" || bundle === null || Array.isArray(bundle)) {
      return null;
    }
    return { ok: true, bundle };
  }
  return { ok: false, code: typeof record7.error === "string" ? record7.error : null };
}
function debugFileName(now) {
  const pad2 = (value) => String(value).padStart(2, "0");
  return `spotnav-debug-${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}.json`;
}
function saveDebugBundle(doc, bundle, now) {
  const view = doc.defaultView;
  if (view === null || typeof view.URL?.createObjectURL !== "function") {
    return false;
  }
  const blob = new view.Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
  const url = view.URL.createObjectURL(blob);
  const link = doc.createElement("a");
  link.href = url;
  link.download = debugFileName(now);
  link.hidden = true;
  doc.body.append(link);
  link.click();
  link.remove();
  view.setTimeout(() => view.URL.revokeObjectURL(url), 0);
  return true;
}

// src/download.ts
function saveTextFile(doc, filename, text5, type = "text/csv;charset=utf-8") {
  const view = doc.defaultView;
  if (view === null) {
    throw new Error("no window to save from");
  }
  const url = view.URL.createObjectURL(new view.Blob([text5], { type }));
  const link = doc.createElement("a");
  link.href = url;
  link.download = filename;
  link.hidden = true;
  doc.body.append(link);
  link.click();
  link.remove();
  view.setTimeout(() => view.URL.revokeObjectURL(url), 0);
}

// src/site-settings.ts
var MalformedPayload4 = class extends Error {
};
function bad6() {
  throw new MalformedPayload4("malformed");
}
function isRecord5(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function record6(value) {
  return isRecord5(value) ? value : bad6();
}
function exactKeys5(source, keys) {
  if (Object.keys(source).length !== keys.length) {
    bad6();
  }
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) {
      bad6();
    }
  }
}
function textOrNull5(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "string" ? value : bad6();
}
function booleanValue3(source, key) {
  const value = source[key];
  return typeof value === "boolean" ? value : bad6();
}
var SITE_SETTINGS_NOT_ADMIN = "spotnav_not_admin";
var SITE_SETTINGS_NO_SITE = "spotnav_no_site";
var SITE_SETTINGS_CONFLICT = "spotnav_conflict";
var SITE_SETTINGS_INVALID_VALUE = "spotnav_invalid_value";
var SITE_SETTINGS_UNAVAILABLE = "spotnav_site_unavailable";
var SITE_SETTINGS_ACTIVE_CONTROL_UNAVAILABLE = "spotnav_active_control_unavailable";
var SITE_SETTINGS_CONFIRMATION_FAILED = "spotnav_confirmation_failed";
var ENVELOPE_KEYS3 = ["api_version", "ok", "error", "site", "restore"];
var RESTORE_KEYS = ["outcome", "chargers"];
var RESTORE_CHARGER_KEYS = ["charger_id", "charger_name", "outcome", "code", "from_a", "to_a"];
function decodeSiteSettingsAnswer(raw) {
  try {
    if (!isRecord5(raw)) {
      return { ok: false, failure: "malformed" };
    }
    const version = raw["api_version"];
    if (typeof version === "number" && version !== SITE_SETTINGS_API_VERSION) {
      return { ok: false, failure: "unsupported" };
    }
    exactKeys5(raw, ENVELOPE_KEYS3);
    if (version !== SITE_SETTINGS_API_VERSION) {
      return { ok: false, failure: "malformed" };
    }
    const ok = booleanValue3(raw, "ok");
    const error = textOrNull5(raw, "error");
    const rawSite = raw["site"];
    const site = rawSite === null ? null : decodeSite(record6(rawSite));
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
    if (error instanceof MalformedPayload4) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}
function restoreOutcome(source, key) {
  const value = source[key];
  return value === "not_needed" || value === "restored" || value === "failed" ? value : bad6();
}
function numberOrNull4(source, key) {
  const value = source[key];
  if (value === null) {
    return null;
  }
  return typeof value === "number" && Number.isFinite(value) ? value : bad6();
}
function decodeRestore(raw) {
  if (raw === null) {
    return null;
  }
  const source = record6(raw);
  exactKeys5(source, RESTORE_KEYS);
  const outcome = restoreOutcome(source, "outcome");
  const list4 = source["chargers"];
  if (!Array.isArray(list4)) {
    return bad6();
  }
  const chargers = list4.map((item) => {
    const entry = record6(item);
    exactKeys5(entry, RESTORE_CHARGER_KEYS);
    const chargerId = entry["charger_id"];
    if (typeof chargerId !== "string" || chargerId === "") {
      return bad6();
    }
    const chargerOutcome = restoreOutcome(entry, "outcome");
    const code = textOrNull5(entry, "code");
    if (code !== null && chargerOutcome !== "failed") {
      return bad6();
    }
    const toA = numberOrNull4(entry, "to_a");
    if (chargerOutcome === "restored" && toA === null) {
      return bad6();
    }
    return {
      chargerId,
      chargerName: textOrNull5(entry, "charger_name"),
      outcome: chargerOutcome,
      code,
      fromA: numberOrNull4(entry, "from_a"),
      toA
    };
  });
  const derived = chargers.some((c) => c.outcome === "failed") ? "failed" : chargers.some((c) => c.outcome === "restored") ? "restored" : "not_needed";
  if (derived !== outcome) {
    return bad6();
  }
  return { outcome, chargers };
}
function solarSettingsChange(confirmed, chosen) {
  const expected = {};
  const changes = {};
  if (chosen.priority !== confirmed.priority) {
    expected.solar_priority = confirmed.priority;
    changes.solar_priority = chosen.priority;
  }
  const same = chosen.forecast.length === confirmed.forecast.length && chosen.forecast.every((id) => confirmed.forecast.includes(id));
  if (!same) {
    expected.solar_forecast = [...confirmed.forecast];
    changes.solar_forecast = [...chosen.forecast];
  }
  return Object.keys(changes).length === 0 ? null : { expected, changes };
}
function activeControlChange(confirmed, chosen) {
  return { expected: { active_control_enabled: confirmed }, changes: { active_control_enabled: chosen } };
}
function siteSettingsErrorKey(code) {
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
var RESTORE_FAILURE_KEYS = {
  write_failed: "site.activeControl.restore.failed.write_failed",
  unconfirmed: "site.activeControl.restore.failed.unconfirmed",
  assigned_current_unreadable: "site.activeControl.restore.failed.assigned_current_unreadable",
  no_authoritative_current: "site.activeControl.restore.failed.no_authoritative_current",
  charger_not_loaded: "site.activeControl.restore.failed.charger_not_loaded",
  membership_conflict: "site.activeControl.restore.failed.membership_conflict",
  probe_in_flight: "site.activeControl.restore.failed.probe_in_flight",
  below_minimum: "site.activeControl.restore.failed.below_minimum",
  no_connector_target: "site.activeControl.restore.failed.no_connector_target",
  external_balancer: "site.activeControl.restore.failed.external_balancer"
};
function restoreLines(language, restore) {
  const acted = restore.chargers.filter((charger) => charger.outcome !== "not_needed");
  if (acted.length === 0) {
    return { lines: [translate(language, "site.activeControl.restore.notNeeded")], code: null };
  }
  const single = restore.chargers.length === 1;
  const lines = [];
  let firstCode = null;
  for (const charger of acted) {
    const name = charger.chargerName ?? translate(language, "site.activeControl.restore.unnamed");
    if (charger.outcome === "restored") {
      const to = formatNumber(language, charger.toA ?? 0, 1);
      lines.push(
        single ? translate(language, "site.activeControl.restore.restoredOne", { to }) : translate(language, "site.activeControl.restore.restoredNamed", { name, to })
      );
      continue;
    }
    const key = charger.code === null ? void 0 : RESTORE_FAILURE_KEYS[charger.code];
    const sentence = translate(language, key ?? "site.activeControl.restore.failed.unknown");
    firstCode = firstCode ?? charger.code;
    lines.push(single ? sentence : translate(language, "site.activeControl.restore.line", { name, text: sentence }));
  }
  return { lines, code: firstCode };
}
function activeControlNotice(language, answer, disabling) {
  if (answer.ok) {
    if (!disabling) {
      return null;
    }
    if (answer.restore === null) {
      return {
        tone: "warning",
        lines: [translate(language, "settings.error.generic")],
        code: null
      };
    }
    const restored = restoreLines(language, answer.restore);
    return {
      tone: answer.restore.outcome === "failed" ? "warning" : "ok",
      lines: [translate(language, "site.activeControl.restore.off"), ...restored.lines],
      code: restored.code
    };
  }
  const lines = [translate(language, siteSettingsErrorKey(answer.code))];
  let code = answer.code;
  if (answer.restore !== null && answer.restore.outcome !== "not_needed") {
    const restored = restoreLines(language, answer.restore);
    lines.push(...restored.lines);
    code = restored.code ?? code;
  }
  return { tone: "warning", lines, code };
}

// src/view.ts
function parseCardConfig(config) {
  if (typeof config !== "object" || config === null || Array.isArray(config)) {
    throw new Error("SpotNav card: configuration must be an object");
  }
  const candidate = config;
  if (candidate.type !== `custom:${CARD_TYPE}`) {
    throw new Error(`SpotNav card: type must be "custom:${CARD_TYPE}"`);
  }
  if (Object.keys(candidate).some((key) => key !== "type" && key !== "charger")) {
    throw new Error("SpotNav card: only the type and charger options are supported");
  }
  const charger = candidate.charger;
  if (charger === void 0 || charger === "") {
    return { type: `custom:${CARD_TYPE}`, charger: "" };
  }
  if (typeof charger !== "string" || charger.trim() === "" || charger.trim() !== charger) {
    throw new Error(
      "SpotNav card: charger must be a config entry id, with no surrounding whitespace"
    );
  }
  return { type: `custom:${CARD_TYPE}`, charger };
}
function editorConfig(chargerId) {
  return { type: `custom:${CARD_TYPE}`, charger: chargerId };
}
function chargerOptions(list4, current) {
  const options = (list4?.chargers ?? []).map((charger) => ({
    charger_id: charger.charger_id,
    label: charger.charger_name || charger.charger_id,
    available: charger.available
  }));
  if (current !== null && current !== "" && !options.some((o) => o.charger_id === current)) {
    options.unshift({ charger_id: current, label: current, available: false });
  }
  return options;
}
function stubChargerId(list4) {
  const available = (list4?.chargers ?? []).filter((charger) => charger.available);
  return available.length === 1 ? available[0].charger_id : null;
}

// src/card.ts
var REFRESH_INTERVAL_MS = 3e4;
var cardInstanceCounter = 0;
var CHARGER_REFUSALS = [
  "spotnav_unknown_charger",
  "spotnav_charger_unloaded",
  "spotnav_site_not_charger"
];
var ACTION_CONFIRM_NOTICE = {
  sentenceKey: "action.error.confirmationFailed",
  target: "action"
};
var SETTINGS_CONFIRM_NOTICE = {
  sentenceKey: "settings.error.confirmationFailed",
  target: "settings"
};
var STUB_TIMEOUT_MS = 3e3;
var STATE_KEYS = {
  unconfigured: "state.unconfigured",
  loading: "state.loading",
  unsupported: "state.unsupported",
  malformed: "state.malformed",
  "charger-missing": "state.chargerMissing",
  failed: "state.requestFailed"
};
var SpotnavCard = class extends HTMLElement {
  constructor() {
    super();
    this.hassObject = null;
    this.config = null;
    this.cardState = { kind: "unconfigured" };
    this.timer = null;
    this.connected = false;
    this.assigned = false;
    this.generation = 0;
    this.attempt = 0;
    this.language = null;
    this.view = null;
    this.actionInFlight = null;
    this.editor = null;
    /**
     * The open area/fiscal editor: the accepted record and catalogue context (always adopted together, as
     * they arrive in one operation), the per-area drafts, and its own operation. `values` is the draft for
     * the area currently shown.
     */
    this.marketEditor = null;
    this.editorOperation = 0;
    this.strategyOperation = 0;
    this.siteOperation = 0;
    this.activeControlBusy = false;
    this.entityConfig = null;
    this.entityOperation = 0;
    this.historyOperation = 0;
    this.history = null;
    this.entitySaving = false;
    this.reopenOverview = false;
    this.confirmReadFailed = false;
    this.deferredRefresh = false;
    this.inFlight = null;
    this.renderPending = false;
    this.root = this.attachShadow({ mode: "open" });
    cardInstanceCounter += 1;
    this.idPrefix = `spotnav-card-${cardInstanceCounter}`;
  }
  setConfig(config) {
    this.config = parseCardConfig(config);
    this.generation += 1;
    this.attempt += 1;
    this.inFlight = null;
    this.actionInFlight = null;
    this.deferredRefresh = false;
    this.marketEditor = null;
    this.entityConfig = null;
    this.entityOperation += 1;
    this.historyOperation += 1;
    this.history = null;
    this.entitySaving = false;
    this.closeEditor();
    this.cardState = this.config.charger === "" ? { kind: "unconfigured" } : { kind: "loading" };
    this.render({ force: true });
    this.startWork();
  }
  set hass(hass) {
    const nextLanguage = resolveLanguage(hass.language);
    const changed = this.language !== null && this.language !== nextLanguage;
    this.hassObject = hass;
    this.view?.setEntityHass(hass);
    this.language = nextLanguage;
    if (!this.assigned) {
      this.assigned = true;
      this.startWork();
      return;
    }
    if (changed) {
      this.renderLocal();
    }
  }
  get hass() {
    return this.hassObject;
  }
  connectedCallback() {
    this.connected = true;
    this.startWork();
  }
  disconnectedCallback() {
    this.connected = false;
    this.stopTimer();
    this.generation += 1;
    this.attempt += 1;
    this.inFlight = null;
    this.actionInFlight = null;
    this.deferredRefresh = false;
    this.editor = null;
    this.marketEditor = null;
    this.entityOperation += 1;
    this.historyOperation += 1;
    this.entitySaving = false;
    this.editorOperation += 1;
    this.releaseView();
  }
  getCardSize() {
    return 3;
  }
  getGridOptions() {
    return { rows: "auto", columns: "full" };
  }
  static getConfigElement() {
    return document.createElement(CARD_EDITOR_ELEMENT);
  }
  /**
   * What the card picker adds: the one charger there is, so the preview shows the real card, else
   * an unconfigured card. Bounded: a slow or failing backend gives the unconfigured one too.
   */
  static async getStubConfig(hass) {
    const stub = { type: `custom:${CARD_TYPE}`, charger: "" };
    if (hass === void 0) {
      return stub;
    }
    let timer;
    try {
      const list4 = await Promise.race([
        listChargers(hass),
        new Promise((resolve) => {
          timer = setTimeout(() => resolve(null), STUB_TIMEOUT_MS);
        })
      ]);
      if (list4 !== null && list4.chargers.length === 1) {
        return { ...stub, charger: list4.chargers[0].charger_id };
      }
    } catch {
    } finally {
      clearTimeout(timer);
    }
    return stub;
  }
  startWork() {
    if (!this.connected || this.hassObject === null) {
      return;
    }
    const config = this.config;
    if (config === null || config.charger === "") {
      return;
    }
    this.startTimer();
    void this.refresh();
  }
  startTimer() {
    if (this.timer !== null) {
      return;
    }
    this.timer = window.setInterval(() => void this.refresh(), REFRESH_INTERVAL_MS);
  }
  stopTimer() {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
  }
  /**
   * Coalesced, generation-, attempt- and purpose-guarded refresh.
   *
   * While an action is in flight it owns the read lifecycle: an ordinary read is deferred to one read that
   * runs when the action finishes, so it cannot become the newest attempt and supersede the action's
   * confirmation. Timer and connect paths coalesce into an in-flight attempt. The visible Retry and a
   * confirmation read force a fresh attempt (Retry may click in the window where `inFlight` has not yet
   * cleared; a confirmation must be newer than its action). A forced refresh coalesces into an in-flight
   * forced one.
   */
  refresh(options = {}) {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return Promise.resolve();
    }
    const purpose = options.purpose ?? "ordinary";
    const confirm = options.confirm ?? ACTION_CONFIRM_NOTICE;
    if (purpose === "confirm") {
      this.confirmReadFailed = false;
    }
    if (purpose === "ordinary" && this.actionInFlight !== null) {
      this.deferredRefresh = true;
      return Promise.resolve();
    }
    const current = this.inFlight;
    const sameAttempt = current !== null && current.generation === this.generation && current.charger === config.charger;
    const fresh = options.force === true || purpose === "confirm";
    if (current !== null && sameAttempt && (!fresh || current.forced)) {
      return current.promise;
    }
    const generation = this.generation;
    const charger = config.charger;
    this.attempt += 1;
    const attempt = this.attempt;
    const forced = options.force === true;
    const promise = this.load(hass, charger, generation, attempt, purpose, confirm).finally(() => {
      if (this.inFlight !== null && this.inFlight.promise === promise) {
        this.inFlight = null;
      }
    });
    this.inFlight = { generation, charger, attempt, forced, promise };
    return promise;
  }
  /**
   * One action, one request, one confirmed answer. The guards run synchronously before the first await:
   * a disabled attribute only takes effect on the next paint, so this is what stops a double click.
   */
  async performAction(action, choice = null) {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    const state = this.cardState;
    if (state.kind !== "ready") {
      return;
    }
    const control = state.dashboard.control;
    if (!control.can_act) {
      return;
    }
    if (!admittedAction(control, action, choice)) {
      return;
    }
    if (this.actionInFlight !== null) {
      return;
    }
    const generation = this.generation;
    const attempt = this.attempt;
    let confirmed = false;
    this.actionInFlight = { generation, attempt, action };
    this.view?.setActionError(null);
    this.view?.setActionPending(true, action, choice);
    try {
      const result = await performAction(hass, config.charger, action, choice);
      if (!this.actionAnswerIsCurrent(generation, attempt)) {
        return;
      }
      if (result.ok) {
        confirmed = true;
        this.deferredRefresh = false;
        await this.refresh({ purpose: "confirm" });
        return;
      }
      this.view?.setActionError({ sentenceKey: actionErrorKey(result.error), code: result.error });
    } catch (error) {
      if (!this.actionAnswerIsCurrent(generation, attempt)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.view?.setActionError({ sentenceKey: actionErrorKey(code), code });
    } finally {
      if (this.actionInFlight !== null && this.actionInFlight.generation === generation) {
        this.actionInFlight = null;
        const deferred = this.deferredRefresh;
        this.deferredRefresh = false;
        this.view?.setActionPending(false);
        if (deferred && !confirmed) {
          void this.refresh({ purpose: "ordinary" });
        }
      }
    }
  }
  actionAnswerIsCurrent(generation, attempt) {
    return this.connected && generation === this.generation && attempt === this.attempt;
  }
  showFailure(confirm, code) {
    const failure = { sentenceKey: confirm.sentenceKey, code };
    if (confirm.target === "settings") {
      this.view?.setSettingsError(failure);
      return;
    }
    this.view?.setActionError(failure);
  }
  get isAdmin() {
    return this.hassObject?.user?.is_admin === true;
  }
  editorIsCurrent(generation, operation) {
    return this.connected && generation === this.generation && operation === this.editorOperation;
  }
  showEditorNotice(kind, sentenceKey, code) {
    const view = this.view;
    if (view === null || view.settingsEditorOpen() !== kind) {
      return;
    }
    view.setSettingsEditorNotice({ sentenceKey, code });
  }
  /**
   * Open one editor from one `get_settings` read, never from the dashboard's reduced section. A
   * non-administrator gets a read-only form; the backend's `require_admin` is the security boundary.
   */
  async openSettings(kind) {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    this.editor = null;
    this.view?.openSettingsEditor(kind);
    try {
      const raw = await getSettings(hass, config.charger);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showEditorNotice(
          kind,
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.read",
          null
        );
        return;
      }
      const answer = decoded.value;
      if (answer.settings === null) {
        this.showEditorNotice(
          kind,
          answer.ok ? "settings.error.read" : settingsErrorKey(answer.code),
          answer.ok ? null : answer.code
        );
        return;
      }
      const record7 = answer.settings;
      this.editor = { kind, record: record7, conflict: null, operation, generation, saving: false };
      this.view?.showSettingsEditorForm({
        kind,
        readOnly: !this.isAdmin,
        values: formFromRecord(record7),
        energyReadOnly: manualEnergyReadOnly(record7),
        phases: record7.phases,
        currentRange: this.currentRange(),
        conflict: null,
        soc: this.socFacts(),
        vehicles: this.vehicleFacts(),
        days: this.departureDays()
      });
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showEditorNotice(kind, settingsErrorKey(code), code);
    }
  }
  /**
   * Read the record and the catalogue context together (parallel) and adopt them only as a pair: the
   * guard is checked once after both settle, so a late half is inert. If either half is unreadable the
   * whole pair is refused, each with its own sentence.
   */
  async readMarketPair(hass, charger, generation, operation) {
    const [settingsOutcome, marketOutcome] = await Promise.allSettled([
      getSettings(hass, charger),
      getMarketOptions(hass, charger)
    ]);
    if (!this.editorIsCurrent(generation, operation)) {
      return { ok: false, sentenceKey: "settings.error.generic", code: null };
    }
    if (settingsOutcome.status === "rejected") {
      const code = settingsOutcome.reason instanceof SpotnavApiError ? settingsOutcome.reason.code : null;
      return { ok: false, sentenceKey: settingsErrorKey(code), code };
    }
    const decodedSettings = decodeSettingsAnswer(settingsOutcome.value);
    if (!decodedSettings.ok) {
      return {
        ok: false,
        sentenceKey: decodedSettings.failure === "unsupported" ? "settings.error.version" : "settings.error.read",
        code: null
      };
    }
    const settings = decodedSettings.value;
    if (settings.settings === null) {
      return {
        ok: false,
        sentenceKey: settings.ok ? "settings.error.read" : settingsErrorKey(settings.code),
        code: settings.ok ? null : settings.code
      };
    }
    if (marketOutcome.status === "rejected") {
      const code = marketOutcome.reason instanceof SpotnavApiError ? marketOutcome.reason.code : null;
      return { ok: false, sentenceKey: settingsErrorKey(code), code };
    }
    const decodedMarket = decodeMarketOptions(marketOutcome.value);
    if (!decodedMarket.ok) {
      return {
        ok: false,
        sentenceKey: decodedMarket.failure === "unsupported" ? "market.error.version" : "market.error.read",
        code: null
      };
    }
    return { ok: true, record: settings.settings, options: decodedMarket.value };
  }
  /**
   * Open the area/fiscal editor: two reads, and a form only once both are accepted. Non-administrators
   * get the form without Save; `control.can_act` is not consulted because it says nothing about settings
   * permission.
   */
  async openMarket() {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    this.marketEditor = null;
    this.view?.openMarketEditor();
    const pair = await this.readMarketPair(hass, config.charger, generation, operation);
    if (!pair.ok) {
      if (this.editorIsCurrent(generation, operation)) {
        this.showMarketNotice(pair.sentenceKey, pair.code);
      }
      return;
    }
    this.marketEditor = {
      record: pair.record,
      options: pair.options,
      drafts: {},
      values: marketFormFor(pair.record, pair.record.area_id),
      conflict: null,
      generation,
      operation,
      saving: false
    };
    this.showMarketForm();
  }
  showMarketForm() {
    const editor = this.marketEditor;
    if (editor === null || !this.connected) {
      return;
    }
    this.view?.showMarketEditorForm({
      readOnly: !marketEditable(editor.options.state, this.isAdmin),
      options: editor.options,
      values: editor.values,
      conflict: editor.conflict === null ? null : editor.conflict.revision
    });
  }
  switchMarketArea(areaId, live) {
    const editor = this.marketEditor;
    if (editor === null || !this.connected) {
      return;
    }
    const switched = switchArea(editor.record, editor.drafts, live, areaId);
    editor.drafts = switched.drafts;
    editor.values = switched.values;
    this.showMarketForm();
  }
  async reloadMarket() {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.marketEditor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.saving) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    this.view?.setMarketEditorPending(true);
    try {
      const pair = await this.readMarketPair(hass, config.charger, generation, operation);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      if (!pair.ok) {
        this.showMarketNotice(pair.sentenceKey, pair.code);
        return;
      }
      editor.record = pair.record;
      editor.options = pair.options;
      editor.drafts = {};
      editor.values = marketFormFor(pair.record, pair.record.area_id);
      editor.conflict = null;
      this.showMarketForm();
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showMarketNotice(settingsErrorKey(code), code);
    } finally {
      if (this.marketEditor !== null && this.marketEditor.operation === operation) {
        this.marketEditor.saving = false;
        this.view?.setMarketEditorPending(false);
      }
    }
  }
  /**
   * One market Save: a single full replacement under compare-and-set. The body (`marketReplacement`)
   * states only `area_id` and the selected area's row; everything else travels as the accepted record
   * holds it. A Save that would state nothing sends nothing. Price subscriptions and the graph move only
   * after the confirmation read.
   */
  async saveMarket(values, reapply = false) {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.marketEditor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.saving) {
      return;
    }
    if (!this.isAdmin) {
      this.showMarketNotice("settings.error.readOnly", null);
      return;
    }
    if (!marketEditable(editor.options.state, this.isAdmin)) {
      const sentence = marketStateKey(editor.options.state, editor.options.reason);
      if (sentence !== null) {
        this.showMarketNotice(sentence, null);
      }
      return;
    }
    const base = reapply ? editor.conflict : editor.record;
    if (base === null) {
      return;
    }
    const check = marketReplacement(base, values, editor.options);
    if (!check.ok) {
      this.showMarketNotice(check.errorKey, null);
      return;
    }
    if (!check.changed) {
      this.closeEditor();
      this.view?.openSettingsOverview();
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    editor.values = values;
    this.view?.setMarketEditorPending(true);
    try {
      const raw = await updateSettings(hass, config.charger, base.revision, check.body);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showMarketNotice(
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          null
        );
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        await this.adoptThenOverview(answer.settings, null);
        return;
      }
      if (answer.code === SETTINGS_REVISION_CONFLICT && answer.settings !== null) {
        editor.conflict = answer.settings;
        this.view?.showMarketEditorConflict(answer.settings.revision);
        return;
      }
      if (answer.code === SETTINGS_RECONCILE_FAILED) {
        if (answer.settings !== null) {
          await this.adoptThenOverview(answer.settings, {
            sentenceKey: "settings.error.reconcileFailed",
            code: answer.code
          });
          return;
        }
        this.showMarketNotice("settings.error.reconcileFailed", answer.code);
        return;
      }
      if (answer.code === SETTINGS_NOT_COMMITTED) {
        this.showMarketNotice("settings.error.notCommitted", answer.code);
        return;
      }
      this.showMarketNotice(settingsErrorKey(answer.code), answer.code);
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showMarketNotice(settingsErrorKey(code), code);
    } finally {
      if (this.marketEditor !== null && this.marketEditor.operation === operation) {
        this.marketEditor.saving = false;
        this.view?.setMarketEditorPending(false);
      }
    }
  }
  async adoptThenOverview(record7, notice) {
    this.reopenOverview = true;
    await this.adoptSettings(record7, notice, "market");
    if (this.reopenOverview) {
      this.reopenOverview = false;
      if (this.connected && this.view !== null && !this.view.anyDialogOpen()) {
        this.view.openSettingsOverview();
      }
    }
  }
  showMarketNotice(sentenceKey, code) {
    const view = this.view;
    if (view === null || !view.marketEditorOpen()) {
      return;
    }
    view.setMarketEditorNotice({ sentenceKey, code });
  }
  /**
   * One Save: local judgement, then at most one full replacement under compare-and-set, built from the
   * accepted record (`replacementFor`). A Save that changes nothing writes nothing, since it would still
   * cost a revision and a reconcile.
   */
  currentRange() {
    return this.cardState.kind === "ready" ? currentRangeFor(this.cardState.dashboard) : DEFAULT_CURRENT_RANGE;
  }
  /** What the departure date picker offers now, in the market's own zone; `null` while that is unknown. */
  departureDays() {
    return this.cardState.kind === "ready" ? departureDays(this.cardState.dashboard.market?.timezone ?? "", Date.now()) : null;
  }
  socFacts() {
    return this.cardState.kind === "ready" ? socFor(this.cardState.dashboard) : null;
  }
  siteFacts() {
    return this.cardState.kind === "ready" ? siteFactsFor(this.cardState.dashboard.site, this.languageOrFallback) : null;
  }
  vehicleFacts() {
    return this.cardState.kind === "ready" ? vehiclesFor(this.cardState.dashboard) : [];
  }
  async saveSettings(kind, values, reapply = false) {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.editor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.kind !== kind || editor.saving) {
      return;
    }
    if (!this.isAdmin) {
      this.showEditorNotice(kind, "settings.error.readOnly", null);
      return;
    }
    const base = reapply ? editor.conflict : editor.record;
    if (base === null) {
      return;
    }
    const check = replacementFor(
      kind,
      base,
      values,
      this.currentRange(),
      reapply ? editor.record : null,
      this.departureDays()
    );
    if (!check.ok) {
      this.showEditorNotice(kind, check.errorKey, null);
      return;
    }
    if (!check.changed) {
      this.closeEditor();
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    this.view?.setSettingsEditorPending(true);
    try {
      const raw = await updateSettings(hass, config.charger, base.revision, check.body);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showEditorNotice(
          kind,
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          null
        );
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        await this.adoptSettings(answer.settings, null);
        return;
      }
      if (answer.code === SETTINGS_REVISION_CONFLICT && answer.settings !== null) {
        editor.conflict = answer.settings;
        this.view?.showSettingsEditorConflict(answer.settings.revision, answer.settings.phases);
        return;
      }
      if (answer.code === SETTINGS_RECONCILE_FAILED) {
        if (answer.settings !== null) {
          await this.adoptSettings(answer.settings, {
            sentenceKey: "settings.error.reconcileFailed",
            code: answer.code
          });
          return;
        }
        this.showEditorNotice(kind, "settings.error.reconcileFailed", answer.code);
        return;
      }
      if (answer.code === SETTINGS_NOT_COMMITTED) {
        this.showEditorNotice(kind, "settings.error.notCommitted", answer.code);
        return;
      }
      this.showEditorNotice(kind, settingsErrorKey(answer.code), answer.code);
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showEditorNotice(kind, settingsErrorKey(code), code);
    } finally {
      if (this.editor !== null && this.editor.operation === operation) {
        this.editor.saving = false;
        this.view?.setSettingsEditorPending(false);
      }
    }
  }
  async reloadSettings(kind) {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.editor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.kind !== kind || editor.saving) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    this.view?.setSettingsEditorPending(true);
    try {
      const raw = await getSettings(hass, config.charger);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showEditorNotice(
          kind,
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.read",
          null
        );
        return;
      }
      const answer = decoded.value;
      if (answer.settings === null) {
        this.showEditorNotice(
          kind,
          answer.ok ? "settings.error.read" : settingsErrorKey(answer.code),
          answer.ok ? null : answer.code
        );
        return;
      }
      const record7 = answer.settings;
      editor.record = record7;
      editor.conflict = null;
      this.view?.showSettingsEditorForm({
        kind,
        readOnly: !this.isAdmin,
        values: formFromRecord(record7),
        energyReadOnly: manualEnergyReadOnly(record7),
        phases: record7.phases,
        currentRange: this.currentRange(),
        conflict: null,
        soc: this.socFacts(),
        vehicles: this.vehicleFacts(),
        days: this.departureDays()
      });
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showEditorNotice(kind, settingsErrorKey(code), code);
    } finally {
      if (this.editor !== null && this.editor.operation === operation) {
        this.editor.saving = false;
        this.view?.setSettingsEditorPending(false);
      }
    }
  }
  /**
   * Adopt a committed record, close the dialog, then confirm with one forced dashboard read. Nothing
   * proposed is rendered before that read is accepted; if it fails the reader is told the state could not
   * be confirmed.
   */
  async adoptSettings(record7, notice, editor = "settings") {
    this.editorOperation += 1;
    this.editor = null;
    this.marketEditor = null;
    if (editor === "market") {
      this.view?.closeMarketEditor();
    } else {
      this.view?.closeSettingsEditor();
    }
    this.view?.setSettingsError(notice);
    this.confirmReadFailed = false;
    await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
    if (notice !== null && this.connected && !this.confirmReadFailed) {
      this.view?.setSettingsError(notice);
    }
  }
  /**
   * Switch strategy: one settings write with no dialog or confirmation prompt. The view already refuses
   * unavailable, unwritable or already-selected rows, so this is a defensive re-check.
   *
   * The record is read fresh because a full replacement needs its current revision, which the dashboard's
   * `strategy` block does not carry (see `settings.ts`). A refusal or conflict leaves the confirmed
   * strategy as shown and is reported through the row-level sentence.
   */
  async selectStrategy(strategyId) {
    await this.writeFreshSettings((record7) => strategyReplacement(record7, strategyId));
  }
  /**
   * Choose the vehicle the charger plans for: the same dialog-free write as the strategy, changing only
   * `target.vehicle_id` of a freshly read record under its revision.
   */
  async selectVehicle(vehicleId) {
    await this.writeFreshSettings((record7) => vehicleReplacement(record7, vehicleId));
  }
  async writeFreshSettings(build) {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "" || !this.isAdmin) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.strategyOperation;
    const stale = () => generation !== this.generation || operation !== this.strategyOperation || !this.connected;
    try {
      const rawRecord = await getSettings(hass, config.charger);
      if (stale()) {
        return;
      }
      const decodedRecord = decodeSettingsAnswer(rawRecord);
      if (!decodedRecord.ok || decodedRecord.value.settings === null) {
        this.view?.setSettingsError({
          sentenceKey: decodedRecord.ok && !decodedRecord.value.ok ? settingsErrorKey(decodedRecord.value.code) : "settings.error.read",
          code: decodedRecord.ok && !decodedRecord.value.ok ? decodedRecord.value.code : null
        });
        return;
      }
      const record7 = decodedRecord.value.settings;
      const check = build(record7);
      if (!check.ok) {
        this.view?.setSettingsError({ sentenceKey: check.errorKey, code: null });
        return;
      }
      if (!check.changed) {
        return;
      }
      const raw = await updateSettings(hass, config.charger, record7.revision, check.body);
      if (stale()) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.view?.setSettingsError({
          sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          code: null
        });
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        await this.adoptSettings(answer.settings, null);
        return;
      }
      if (answer.code === SETTINGS_RECONCILE_FAILED && answer.settings !== null) {
        await this.adoptSettings(answer.settings, {
          sentenceKey: "settings.error.reconcileFailed",
          code: answer.code
        });
        return;
      }
      this.view?.setSettingsError({ sentenceKey: settingsErrorKey(answer.code), code: answer.code });
    } catch (error) {
      if (stale()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.view?.setSettingsError({ sentenceKey: settingsErrorKey(code), code });
    }
  }
  /** Fetch the redacted bundle and save it as a file; any failure is a sentence in the Settings popover. */
  async downloadDebug() {
    const hass = this.hassObject;
    const view = this.view;
    const doc = this.ownerDocument;
    if (!this.connected || hass === null || view === null || !this.isAdmin) {
      return;
    }
    const fail = (sentenceKey, code) => {
      if (this.view === view) {
        view.setDebugPending(false);
        view.setOverviewNotice({ sentenceKey, code });
      }
    };
    view.setOverviewNotice(null);
    view.setDebugPending(true);
    let raw;
    try {
      raw = await getDebugBundle(hass);
    } catch (error) {
      fail("debug.error.failed", error instanceof SpotnavApiError ? error.code : null);
      return;
    }
    const answer = decodeDebugAnswer(raw);
    if (answer === null) {
      fail("settings.error.version", null);
      return;
    }
    if (!answer.ok) {
      fail(answer.code === "spotnav_not_admin" ? "debug.error.notAdmin" : "debug.error.failed", answer.code);
      return;
    }
    if (!saveDebugBundle(doc, answer.bundle, /* @__PURE__ */ new Date())) {
      fail("debug.error.failed", null);
      return;
    }
    if (this.view === view) {
      view.setDebugPending(false);
    }
  }
  /**
   * The History dialog was opened: read the charge history (any signed-in user may), and answer the open
   * view. A newer open, a reconfiguration or a disconnect makes an older answer inert.
   */
  async loadHistory() {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.historyOperation;
    const current = () => this.connected && generation === this.generation && operation === this.historyOperation && this.view === view;
    try {
      const decoded = decodeSessions(await getSessions(hass, config.charger));
      if (!current()) {
        return;
      }
      if (!decoded.ok) {
        view.setHistoryState({
          kind: "failed",
          sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "history.failed",
          code: null
        });
        return;
      }
      this.history = decoded.value;
      view.setHistoryState({ kind: "ready", answer: decoded.value });
    } catch (error) {
      if (!current()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      view.setHistoryState({ kind: "failed", sentenceKey: "history.failed", code });
    }
  }
  /** Export CSV: one request for the chosen period, then the file is saved; failure is one sentence. */
  async exportHistory(range) {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    const answer = this.history;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null || answer === null) {
      return;
    }
    const generation = this.generation;
    const operation = this.historyOperation;
    const current = () => this.connected && generation === this.generation && operation === this.historyOperation && this.view === view;
    view.setHistoryExport(null, true);
    try {
      const file = decodeCsv(await getSessionsCsv(hass, config.charger, exportDates(range, answer)));
      if (!current()) {
        return;
      }
      if (file === null) {
        view.setHistoryExport("history.exportFailed", false);
        return;
      }
      saveTextFile(this.ownerDocument, file.filename, file.csv);
      view.setHistoryExport(null, false);
    } catch {
      if (current()) {
        view.setHistoryExport("history.exportFailed", false);
      }
    }
  }
  async loadEntityConfig() {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityState({ kind: "adminOnly" });
      return;
    }
    const generation = this.generation;
    const operation = ++this.entityOperation;
    const current = () => this.connected && generation === this.generation && operation === this.entityOperation && this.view === view;
    view.setEntityState(
      this.entityConfig === null ? { kind: "loading" } : { kind: "ready", config: this.entityConfig }
    );
    try {
      const [raw] = await Promise.all([
        getEntityConfig(hass, config.charger),
        ensureHaSelector(this.ownerDocument?.defaultView)
      ]);
      if (!current()) {
        return;
      }
      const decoded = decodeEntityAnswer(raw);
      if (!decoded.ok) {
        view.setEntityState({
          kind: "failed",
          failure: {
            sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "entity.error.read",
            code: null
          }
        });
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        this.entityConfig = answer.config;
        view.setEntityState({ kind: "ready", config: answer.config });
        return;
      }
      view.setEntityState(
        answer.code === ENTITY_NOT_ADMIN ? { kind: "adminOnly" } : { kind: "failed", failure: { sentenceKey: "entity.error.read", code: answer.code } }
      );
    } catch (error) {
      if (!current()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      view.setEntityState({ kind: "failed", failure: { sentenceKey: "entity.error.read", code } });
    }
  }
  openEntityEditor(scope) {
    if (!this.isAdmin) {
      return;
    }
    const open = () => {
      const config = this.entityConfig;
      if (config === null) {
        return;
      }
      this.entityOperation += 1;
      this.view?.openEntityEditor(scope, config);
    };
    if (this.entityConfig !== null) {
      open();
      return;
    }
    void this.loadEntityConfig().then(open);
  }
  /**
   * One Save of one group: only changed fields, `expected` from the configuration last read, one request,
   * nothing shown as saved until the backend answers. Success adopts the returned config, closes the
   * editor, reads the dashboard once and reopens Settings. A conflict adopts the returned config and
   * redraws the editor. A refused field is marked on the field; other refusals are one general message.
   */
  async saveEntities(scope, draft) {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    const read = this.entityConfig;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null || read === null) {
      return;
    }
    if (this.entitySaving) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityEditorNotice({ sentenceKey: "entity.error.notAdmin", code: null });
      return;
    }
    const change = entityChange(read, scope, draft);
    if (!change.ok) {
      view.markEntityFieldErrors(change.errors);
      view.setEntityEditorNotice({ sentenceKey: entityErrorKey(ENTITY_INVALID_VALUE), code: null });
      return;
    }
    view.markEntityFieldErrors([]);
    view.setEntityEditorNotice(null);
    if (!change.changed) {
      view.closeEntityEditor();
      view.openSettingsOverview();
      return;
    }
    const generation = this.generation;
    this.entityOperation += 1;
    this.entitySaving = true;
    view.setEntityEditorPending(true);
    const open = () => this.connected && generation === this.generation && this.view === view && view.entityEditorOpen() === scope;
    try {
      const raw = await updateEntityConfig(hass, config.charger, change.request);
      if (generation !== this.generation || !this.connected) {
        return;
      }
      const decoded = decodeEntityAnswer(raw);
      if (!decoded.ok) {
        if (open()) {
          view.setEntityEditorNotice({
            sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "entity.error.generic",
            code: null
          });
        }
        return;
      }
      const answer = decoded.value;
      if (answer.config !== null) {
        this.entityConfig = answer.config;
      }
      if (answer.ok) {
        await this.returnToSettingsAfterEntitySave(view, answer.config);
        return;
      }
      if (!open()) {
        return;
      }
      const notice = { sentenceKey: entityErrorKey(answer.code), code: answer.code };
      if (answer.code === "spotnav_conflict" && answer.config !== null) {
        view.openEntityEditor(scope, answer.config, notice);
        return;
      }
      view.markEntityFieldErrors(answer.fieldErrors);
      view.setEntityEditorNotice(notice);
    } catch (error) {
      if (open()) {
        const code = error instanceof SpotnavApiError ? error.code : null;
        view.setEntityEditorNotice({ sentenceKey: "entity.error.generic", code });
      }
    } finally {
      this.entitySaving = false;
      if (this.view === view && view.entityEditorOpen() === scope) {
        view.setEntityEditorPending(false);
      }
    }
  }
  async returnToSettingsAfterEntitySave(view, config) {
    this.reopenOverview = true;
    view.closeEntityEditor();
    if (config !== null) {
      view.setEntityState({ kind: "ready", config });
    }
    this.confirmReadFailed = false;
    await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
    if (this.reopenOverview) {
      this.reopenOverview = false;
      if (this.connected && this.view !== null && !this.view.anyDialogOpen()) {
        this.view.openSettingsOverview();
      }
    }
  }
  openVehicleEditor(vehicleId) {
    const listed = this.vehicleFacts().some((row) => row.id === vehicleId);
    if (!this.isAdmin || !(listed || this.entityConfig?.vehicles.some((entry) => entry.id === vehicleId) === true)) {
      return;
    }
    this.entityOperation += 1;
    this.view?.openVehicleEditor(vehicleId, this.entityConfig);
  }
  /**
   * One Save of one vehicle's dialog: the typed capacity and consumption are judged first (a refused field
   * is marked and nothing is sent), then a changed charge-level sensor is chosen
   * (`spotnav/choose_vehicle_soc`) and changed properties are written in one `spotnav/update_vehicle`
   * under compare-and-set. Never optimistic. Success closes the dialog, reads the dashboard once and
   * returns to Settings; a refusal stays in the dialog with its sentence.
   */
  async saveVehicle(vehicleId, draft) {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null) {
      return;
    }
    if (this.entitySaving) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityEditorNotice({ sentenceKey: "entity.error.notAdmin", code: null });
      return;
    }
    const row = this.vehicleFacts().find((entry) => entry.id === vehicleId);
    view.markEntityFieldErrors([]);
    view.setEntityEditorNotice(null);
    const errors = [];
    const changes = {};
    const expected = {};
    const judge = (text5, check, field2, code, current) => {
      if (text5 === void 0) {
        return;
      }
      const checked = check(text5);
      if (!checked.ok) {
        errors.push({ field: field2, code });
        return;
      }
      const value = Math.round(checked.value * 10) / 10;
      if (value !== current) {
        changes[field2] = value;
        expected[field2] = current;
      }
    };
    judge(draft["capacity"], checkCapacity, "capacity_kwh", "invalid_capacity", row?.capacity_kwh ?? null);
    judge(
      draft["consumption"],
      checkConsumption,
      "consumption_kwh_per_10km",
      "invalid_consumption",
      row?.consumption_kwh_per_10km ?? null
    );
    if (errors.length > 0) {
      view.markEntityFieldErrors(errors);
      return;
    }
    const read = this.entityConfig;
    const socRequests = read === null || draft["soc"] === void 0 ? [] : vehicleSocChanges(read.vehicles, { [vehicleId]: draft["soc"] }).filter((request) => request.vehicleId === vehicleId);
    const propertiesChanged = Object.keys(changes).length > 0;
    if (socRequests.length === 0 && !propertiesChanged) {
      view.closeEntityEditor();
      view.openSettingsOverview();
      return;
    }
    const generation = this.generation;
    this.entityOperation += 1;
    this.entitySaving = true;
    view.setEntityEditorPending(true);
    const open = () => this.connected && generation === this.generation && this.view === view && view.vehicleEditorOpen() === vehicleId;
    const refuse = (sentenceKey, code, fieldErrors = []) => {
      if (open()) {
        view.markEntityFieldErrors(fieldErrors);
        view.setEntityEditorNotice({ sentenceKey, code });
      }
    };
    const generic = (failure) => failure === "unsupported" ? "settings.error.version" : "entity.error.generic";
    try {
      let latest = read;
      for (const request of socRequests) {
        const decoded = decodeEntityAnswer(await setVehicleSoc(hass, config.charger, request));
        if (generation !== this.generation || !this.connected) {
          return;
        }
        if (!decoded.ok) {
          refuse(generic(decoded.failure), null);
          return;
        }
        const answer = decoded.value;
        if (answer.config !== null) {
          this.entityConfig = answer.config;
          latest = answer.config;
        }
        if (!answer.ok) {
          const notice = { sentenceKey: entityErrorKey(answer.code), code: answer.code };
          if (answer.config !== null && answer.fieldErrors.some((error) => error.code === "unknown_vehicle")) {
            if (open()) {
              view.openVehicleEditor(vehicleId, answer.config, notice);
            }
            return;
          }
          refuse(notice.sentenceKey, notice.code, answer.fieldErrors);
          return;
        }
      }
      if (propertiesChanged) {
        const decoded = decodeVehicleAnswer(await updateVehicle(hass, config.charger, { vehicleId, changes, expected }));
        if (generation !== this.generation || !this.connected) {
          return;
        }
        if (!decoded.ok) {
          refuse(generic(decoded.failure), null);
          return;
        }
        const answer = decoded.value;
        if (answer.config !== null) {
          this.entityConfig = answer.config;
          latest = answer.config;
        }
        if (!answer.ok) {
          if (answer.code === ENTITY_CONFLICT && answer.config !== null) {
            if (open()) {
              view.openVehicleEditor(
                vehicleId,
                answer.config,
                { sentenceKey: entityErrorKey(answer.code), code: answer.code },
                answer.vehicle ?? void 0
              );
              await this.confirmVehicleWrite();
            }
            return;
          }
          const fieldErrors = answer.fieldErrors.map((error) => ({
            field: error.field,
            code: error.code
          }));
          refuse(
            fieldErrors.length > 0 && answer.fieldErrors.every((error) => error.code !== "unknown_vehicle") ? "entity.error.invalid" : answer.fieldErrors.some((error) => error.code === "unknown_vehicle") ? "entity.error.field.unknownVehicle" : entityErrorKey(answer.code),
            answer.code,
            fieldErrors
          );
          return;
        }
      }
      await this.returnToSettingsAfterEntitySave(view, latest);
    } catch (error) {
      if (open()) {
        const code = error instanceof SpotnavApiError ? error.code : null;
        view.setEntityEditorNotice({ sentenceKey: "entity.error.generic", code });
      }
    } finally {
      this.entitySaving = false;
      if (this.view === view && view.vehicleEditorOpen() === vehicleId) {
        view.setEntityEditorPending(false);
      }
    }
  }
  async confirmVehicleWrite() {
    this.confirmReadFailed = false;
    await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
  }
  openSolarEditor() {
    if (!this.isAdmin || this.siteFacts() === null) {
      return;
    }
    this.view?.openSolarEditor();
  }
  /**
   * The Solar dialog's Save (`spotnav/update_site_settings`, admin only) under compare-and-set: only the
   * changed fields travel. Success closes the dialog, reads the dashboard once and returns to Settings. A
   * conflict adopts the current values by the same route and says so on the row outside the popover; other
   * refusals stay in the dialog with their sentence.
   */
  async saveSolar(draft) {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    const site = this.siteFacts();
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null || site === null) {
      return;
    }
    if (this.entitySaving) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityEditorNotice({ sentenceKey: "settings.error.readOnly", code: null });
      return;
    }
    const forecast = site.solarForecastChoices.filter((choice) => draft[`${SOLAR_FORECAST_PREFIX}${choice.id}`] === "true").map((choice) => choice.id);
    const request = solarSettingsChange(
      {
        priority: site.solarPriority,
        forecast: site.solarForecastChoices.filter((choice) => choice.selected).map((choice) => choice.id)
      },
      { priority: draft[SOLAR_PRIORITY_KEY] ?? site.solarPriority, forecast }
    );
    if (request === null) {
      view.closeEntityEditor();
      view.openSettingsOverview();
      return;
    }
    const generation = this.generation;
    const operation = ++this.siteOperation;
    this.entitySaving = true;
    view.setEntityEditorNotice(null);
    view.setEntityEditorPending(true);
    const open = () => this.connected && generation === this.generation && this.view === view && view.solarEditorOpen();
    const refuse = (sentenceKey, code) => {
      if (open()) {
        view.setEntityEditorNotice({ sentenceKey, code });
      }
    };
    try {
      const decoded = decodeSiteSettingsAnswer(await updateSiteSettings(hass, config.charger, request));
      if (generation !== this.generation || operation !== this.siteOperation || !this.connected) {
        return;
      }
      if (!decoded.ok) {
        refuse(decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic", null);
        return;
      }
      const answer = decoded.value;
      if (answer.ok || answer.code === SITE_SETTINGS_CONFLICT) {
        await this.returnToSettingsAfterEntitySave(view, null);
        if (!answer.ok && this.connected && !this.confirmReadFailed) {
          this.view?.setSettingsError({ sentenceKey: siteSettingsErrorKey(answer.code), code: answer.code });
        }
        return;
      }
      refuse(siteSettingsErrorKey(answer.code), answer.code);
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      refuse(siteSettingsErrorKey(code), code);
    } finally {
      this.entitySaving = false;
      if (this.view === view && view.solarEditorOpen()) {
        view.setEntityEditorPending(false);
      }
    }
  }
  /**
   * The active load-balancing switch: one admin-only write, never optimistic. The popover adopts the
   * returned site block and says what happened; a failed restore on disable is worded as "the charger may
   * still be limited". The dashboard is re-read afterwards; while the popover is open that render is
   * deferred, so the answer repaints the switch in place.
   */
  async setActiveControl(confirmed, chosen) {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (!this.connected || view === null || hass === null || config === null || config.charger === "" || !this.isAdmin || this.activeControlBusy) {
      return;
    }
    this.activeControlBusy = true;
    view.setActiveControlPending(true);
    const generation = this.generation;
    const operation = ++this.siteOperation;
    const language = this.languageOrFallback;
    const stale = () => generation !== this.generation || operation !== this.siteOperation || !this.connected;
    let fallback = null;
    try {
      const raw = await updateSiteSettings(hass, config.charger, activeControlChange(confirmed, chosen));
      if (stale()) {
        return;
      }
      const decoded = decodeSiteSettingsAnswer(raw);
      if (!decoded.ok) {
        fallback = {
          sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          code: null
        };
        view.adoptActiveControl(null, { tone: "warning", lines: [translate(language, fallback.sentenceKey)], code: null });
        return;
      }
      const answer = decoded.value;
      const notice = activeControlNotice(language, answer, !chosen);
      if (answer.restore?.outcome === "failed") {
        fallback = { sentenceKey: "site.activeControl.restore.failed.unknown", code: null };
      } else if (!answer.ok) {
        fallback = { sentenceKey: siteSettingsErrorKey(answer.code), code: answer.code };
      }
      view.adoptActiveControl(answer.site === null ? null : siteFactsFor(answer.site, language), notice);
      if (answer.site !== null) {
        this.confirmReadFailed = false;
        await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
        if (this.view !== view && this.connected && !this.confirmReadFailed && fallback !== null) {
          this.view?.setSettingsError(fallback);
        }
      }
    } catch (error) {
      if (stale()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      const key = siteSettingsErrorKey(code);
      view.adoptActiveControl(null, { tone: "warning", lines: [translate(language, key)], code });
    } finally {
      this.activeControlBusy = false;
      if (this.view === view) {
        view.setActiveControlPending(false);
      }
    }
  }
  cancelMarket() {
    if (this.marketEditor?.saving === true) {
      return false;
    }
    this.editorOperation += 1;
    this.marketEditor = null;
    return this.connected;
  }
  closeEditor() {
    this.editorOperation += 1;
    this.editor = null;
    this.marketEditor = null;
    this.view?.closeSettingsEditor();
    this.view?.closeMarketEditor();
  }
  async readDashboard(hass, charger) {
    return await getDashboard(hass, charger, API_VERSION);
  }
  async load(hass, charger, generation, attempt, purpose, confirm = ACTION_CONFIRM_NOTICE) {
    let next;
    let code = null;
    try {
      const answer = await this.readDashboard(hass, charger);
      const decoded = decodeDashboard(answer);
      next = decoded.ok ? { kind: "ready", dashboard: decoded.value } : { kind: decoded.failure };
    } catch (error) {
      code = error instanceof SpotnavApiError ? error.code : null;
      next = this.stateFromError(error);
    }
    if (!this.accepts(charger, generation, attempt)) {
      return;
    }
    if (purpose === "confirm" && next.kind !== "ready") {
      this.confirmReadFailed = true;
      this.showFailure(confirm, code);
      return;
    }
    this.cardState = next;
    this.render();
  }
  accepts(charger, generation, attempt) {
    return this.connected && this.config?.charger === charger && generation === this.generation && attempt === this.attempt;
  }
  stateFromError(error) {
    const code = error instanceof SpotnavApiError ? error.code : null;
    if (code === UNSUPPORTED_API_VERSION) {
      return { kind: "unsupported" };
    }
    if (code !== null && CHARGER_REFUSALS.includes(code)) {
      return { kind: "charger-missing" };
    }
    return { kind: "failed" };
  }
  releaseView() {
    const view = this.view;
    this.view = null;
    view?.destroy();
  }
  get languageOrFallback() {
    return this.language ?? resolveLanguage(null);
  }
  renderLocal() {
    this.render();
  }
  /**
   * One render: destroy the previous view, then mount the accepted view or show a quiet state. Everything
   * is built as nodes with `textContent`; no backend, config or exception value is put into markup.
   *
   * A refresh must never close or steal focus from an open dialog or an input being edited: while the
   * current view has a dialog open, the render is deferred and applied from `onDialogsClosed`. `force` is
   * for a config change only, where a new charger identity has no dialog worth preserving.
   */
  render(options = {}) {
    if (options.force !== true && this.view !== null && this.view.anyDialogOpen()) {
      this.renderPending = true;
      return;
    }
    this.renderPending = false;
    this.releaseView();
    const doc = this.ownerDocument;
    const language = this.languageOrFallback;
    const style = doc.createElement("style");
    style.textContent = VISUAL_STYLES;
    const host = doc.createElement("div");
    host.className = VISUAL_CLASSES.shell;
    this.root.replaceChildren(style, host);
    if (this.cardState.kind === "ready") {
      this.view = createCardView({
        model: buildModel({
          dashboard: this.cardState.dashboard,
          language,
          nowMs: Date.now()
        }),
        mount: host,
        idPrefix: this.idPrefix,
        onAction: (action, choice) => {
          void this.performAction(action, choice);
        },
        onOpenSettings: (kind) => {
          void this.openSettings(kind);
        },
        onSaveSettings: (kind, values) => {
          void this.saveSettings(kind, values);
        },
        onReloadSettings: (kind) => {
          void this.reloadSettings(kind);
        },
        onReapplySettings: (kind, values) => {
          void this.saveSettings(kind, values, true);
        },
        onOpenMarket: () => {
          void this.openMarket();
        },
        onSaveMarket: (values) => {
          void this.saveMarket(values);
        },
        onReloadMarket: () => {
          void this.reloadMarket();
        },
        onReapplyMarket: (values) => {
          void this.saveMarket(values, true);
        },
        onMarketAreaChange: (areaId, live) => {
          this.switchMarketArea(areaId, live);
        },
        isAdmin: this.isAdmin,
        onSelectVehicle: (vehicleId) => {
          void this.selectVehicle(vehicleId);
        },
        onSelectStrategy: (strategyId) => {
          void this.selectStrategy(strategyId);
        },
        onOpenSolarEditor: () => {
          this.openSolarEditor();
        },
        onSaveSolar: (draft) => {
          void this.saveSolar(draft);
        },
        onCancelSolar: () => !this.entitySaving,
        onSetActiveControl: (confirmed, chosen) => {
          void this.setActiveControl(confirmed, chosen);
        },
        onCancelMarket: () => this.cancelMarket(),
        hass: () => this.hassObject,
        onOpenHistory: () => {
          void this.loadHistory();
        },
        onExportHistory: (range) => {
          void this.exportHistory(range);
        },
        onSettingsOverviewOpened: () => {
          void this.loadEntityConfig();
        },
        onOpenEntityEditor: (scope) => {
          this.openEntityEditor(scope);
        },
        onDownloadDebug: () => {
          void this.downloadDebug();
        },
        onSaveEntities: (scope, draft) => {
          void this.saveEntities(scope, draft);
        },
        onCancelEntities: () => !this.entitySaving,
        onOpenVehicleEditor: (vehicleId) => {
          this.openVehicleEditor(vehicleId);
        },
        onSaveVehicle: (vehicleId, draft) => {
          void this.saveVehicle(vehicleId, draft);
        },
        onDialogsClosed: () => {
          if (this.renderPending) {
            this.render();
          }
        }
      });
      if (this.reopenOverview) {
        this.reopenOverview = false;
        this.view.openSettingsOverview();
      }
      return;
    }
    const state = this.cardState.kind;
    if (state === "unconfigured") {
      const identity2 = doc.createElement("div");
      identity2.className = VISUAL_CLASSES.header;
      identity2.append(brandMark(doc, this.idPrefix));
      host.append(identity2);
    }
    const sentence = doc.createElement("p");
    sentence.className = VISUAL_CLASSES.muted;
    sentence.textContent = translate(language, STATE_KEYS[state]);
    host.append(sentence);
    if (state === "charger-missing" || state === "failed") {
      const retry = doc.createElement("button");
      retry.type = "button";
      retry.className = VISUAL_CLASSES.iconButton;
      retry.textContent = translate(language, "state.retry");
      retry.setAttribute("aria-label", translate(language, "state.retry"));
      retry.addEventListener("click", () => {
        if (this.cardState.kind === "charger-missing" || this.cardState.kind === "failed") {
          this.cardState = { kind: "loading" };
          this.render();
        }
        void this.refresh({ force: true });
      });
      host.append(retry);
    }
  }
};

// src/dom.ts
function textParagraph(text5, className) {
  const element8 = document.createElement("p");
  if (className !== void 0) {
    element8.className = className;
  }
  element8.textContent = text5;
  return element8;
}
function labelledButton(label, accessibleName) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("aria-label", accessibleName);
  return button;
}
function chargerOption(label, value) {
  return new Option(label, value);
}

// src/editor.ts
var EDITOR_STYLES = `
  .card { padding: 12px 14px; background: var(--card-background-color, #fff);
          color: var(--primary-text-color, #212121); }
  label { display: block; font-size: 0.85rem; color: var(--secondary-text-color, #727272); }
  select { font: inherit; margin-top: 4px; max-width: 100%; padding: 6px;
           color: inherit; background: var(--card-background-color, #fff);
           border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px; }
  p { margin: 8px 0 0; font-size: 0.85rem; overflow-wrap: anywhere; }
  .error { color: var(--error-color, #db4437); }
  button { margin-top: 8px; font: inherit; color: inherit; background: transparent;
           border: 1px solid var(--divider-color, #e0e0e0); border-radius: 6px; padding: 6px 10px; }
  button:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; }
`;
var EDITOR_LOAD_FAILED_MESSAGE = "Could not load the SpotNav chargers. Check the connection and try again.";
var SpotnavCardEditor = class extends HTMLElement {
  constructor() {
    super();
    this.hassObject = null;
    this.config = { type: `custom:${CARD_TYPE}`, charger: "" };
    this.list = null;
    this.loadFailed = false;
    this.connected = false;
    this.assigned = false;
    this.generation = 0;
    this.inFlight = null;
    this.root = this.attachShadow({ mode: "open" });
  }
  setConfig(config) {
    this.config = parseCardConfig(config);
    this.render();
    this.startLoad();
  }
  /**
   * Every `hass` assignment only stores the snapshot for the next request; only the first may
   * start the initial load.
   */
  set hass(hass) {
    this.hassObject = hass;
    if (this.assigned) {
      return;
    }
    this.assigned = true;
    this.startLoad();
  }
  get hass() {
    return this.hassObject;
  }
  connectedCallback() {
    this.connected = true;
    this.render();
    this.startLoad();
  }
  disconnectedCallback() {
    this.connected = false;
    this.invalidate();
  }
  invalidate() {
    this.generation += 1;
    this.inFlight = null;
  }
  startLoad() {
    if (!this.connected || this.hassObject === null || this.list !== null) {
      return;
    }
    if (this.inFlight === this.generation) {
      return;
    }
    const generation = this.generation;
    const hass = this.hassObject;
    this.inFlight = generation;
    void this.load(hass, generation);
  }
  async load(hass, generation) {
    let loaded = null;
    let failed = false;
    try {
      loaded = await listChargers(hass);
    } catch {
      failed = true;
    }
    if (generation !== this.generation || !this.connected) {
      return;
    }
    this.inFlight = null;
    this.loadFailed = failed;
    this.list = failed ? null : loaded;
    this.render();
    this.autoSelect();
  }
  autoSelect() {
    if (this.list === null || this.config.charger !== "") {
      return;
    }
    const only = stubChargerId(this.list);
    if (only !== null) {
      this.config = editorConfig(only);
      this.render();
      this.emit(only);
    }
  }
  emit(chargerId) {
    this.dispatchEvent(
      new CustomEvent("config-changed", {
        // Home Assistant's card editor reads `ev.detail.config`.
        detail: { config: editorConfig(chargerId) },
        bubbles: true,
        composed: true
      })
    );
  }
  render() {
    const style = document.createElement("style");
    style.textContent = EDITOR_STYLES;
    const card = document.createElement("div");
    card.className = "card";
    const label = document.createElement("label");
    label.setAttribute("for", "spotnav-charger");
    label.textContent = "SpotNav charger";
    const select = document.createElement("select");
    select.id = "spotnav-charger";
    select.setAttribute("aria-label", "SpotNav charger");
    const placeholder = chargerOption("Select a charger", "");
    placeholder.selected = this.config.charger === "";
    select.append(placeholder);
    for (const choice of chargerOptions(this.list, this.config.charger)) {
      const item = chargerOption(
        `${choice.label}${choice.available ? "" : " (unavailable)"}`,
        choice.charger_id
      );
      item.selected = choice.charger_id === this.config.charger;
      select.append(item);
    }
    select.addEventListener("change", () => {
      this.config = editorConfig(select.value);
      this.emit(this.config.charger);
      this.render();
    });
    card.append(label, select, this.statusElement());
    if (this.loadFailed) {
      card.append(this.retryButton());
    }
    this.root.replaceChildren(style, card);
  }
  statusElement() {
    const status = (text5, className) => {
      const element8 = textParagraph(text5, className);
      element8.setAttribute("role", "status");
      return element8;
    };
    if (this.loadFailed) {
      return status(EDITOR_LOAD_FAILED_MESSAGE, "error");
    }
    const options = chargerOptions(this.list, this.config.charger);
    if (this.list !== null && options.length === 0) {
      const language = resolveLanguage(this.hassObject?.language);
      return status(`${translate(language, "state.noChargers")} ${translate(language, "state.addCharger")}`);
    }
    if (this.list === null) {
      return status("Loading chargers…");
    }
    return status(
      this.config.charger === "" ? "No charger selected yet." : `Selected: ${this.config.charger}`
    );
  }
  retryButton() {
    const button = labelledButton("Retry", "Retry loading the charger list");
    button.addEventListener("click", () => {
      this.list = null;
      this.loadFailed = false;
      this.invalidate();
      this.render();
      this.startLoad();
    });
    return button;
  }
};

// src/index.ts
var CARD_PICKER_ENTRY = {
  type: CARD_TYPE,
  name: "SpotNav",
  description: "Charging plan and electricity prices from SpotNav.",
  preview: true,
  documentationURL: "https://github.com/henrikekblad/spotnav-home-assistant"
};
function defineCard() {
  if (customElements.get(CARD_ELEMENT) === void 0) {
    customElements.define(CARD_ELEMENT, SpotnavCard);
  }
  if (customElements.get(CARD_EDITOR_ELEMENT) === void 0) {
    customElements.define(CARD_EDITOR_ELEMENT, SpotnavCardEditor);
  }
  const cards = window.customCards ?? [];
  if (!cards.some((entry) => entry.type === CARD_TYPE)) {
    cards.push({ ...CARD_PICKER_ENTRY });
  }
  window.customCards = cards;
}
defineCard();
export {
  CARD_PICKER_ENTRY,
  SpotnavCard,
  SpotnavCardEditor,
  defineCard
};
