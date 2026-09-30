// Shared fixtures for the Stage-4A-2 visual tests.
//
// Everything here is deterministic: the models come from the accepted decoder and model layer, the
// chart is rendered from the accepted geometry, the "observer" is a fake with no timers, and the
// clock is a fixed instant. Nothing in these tests sleeps or waits.

import type { ChartObserverLike, ChartSize } from "../src/chart-interaction";
import {
  chartNowAt,
  chartScale,
  intervalIdentity,
  type ChartNow,
  type ChartSeries,
} from "../src/chart";
import { renderChart, type ChartLabels, type ChartRenderResult } from "../src/chart-render";
import { buildModel, type CardModel } from "../src/model";
import { decodeDashboard, type Dashboard } from "../src/validate";
import { bare, decoded, quarterHourRows, rawDashboard } from "./dashboard-fixtures";

/** The market zone the fixtures use, and the day the base fixture's rows live on. */
export const TZ = "Europe/Stockholm";
export const SEPT = "2026-09-22";
export const AUTUMN = "2026-10-25";
/** The date after the base fixture's: what a pre-midnight capture labels the coming day. */
export const NEXT_DAY = "2026-09-23";
/** Local midnight in this market: 22:00Z is 00:00 local on 2026-09-23. */
export const MIDNIGHT_MS = Date.parse("2026-09-22T22:00:00Z");
/** 00:11 local on the day after: the instant at which the production card showed `Now unknown`. */
export const AFTER_MIDNIGHT_MS = MIDNIGHT_MS + 11 * 60_000;

/** 04:30Z: inside the base fixture's second quarter-hour. */
export const NOW = Date.parse("2026-09-22T04:30:00Z");

/** A decoded dashboard with rows replaced, decoded through the real validator. */
export function dashboardWithRows(
  startIso: string,
  prices: readonly (number | null)[],
  options: { day?: string; duration?: number; overrides?: Record<string, unknown>; flags?: (index: number) => Partial<Record<string, unknown>> } = {},
): Dashboard {
  const day = options.day ?? SEPT;
  const duration = options.duration ?? 15;
  const payload = rawDashboard({
    ...options.overrides,
  });
  const rows = quarterHourRows(startIso, prices, { day, duration, ...(options.flags === undefined ? {} : { flags: options.flags }) });
  (payload.prices as Record<string, unknown>).intervals = rows;
  const result = decodeDashboard(payload);
  if (!result.ok) {
    throw new Error(`fixture did not decode: ${result.failure}`);
  }
  return result.value;
}

export function modelWith(
  overrides: Record<string, unknown> = {},
  language: CardModel["language"] = "en",
  nowMs: number = NOW,
): CardModel {
  return buildModel({ dashboard: decoded(overrides), language, nowMs });
}

export function baseModel(nowMs: number = NOW): CardModel {
  return buildModel({ dashboard: decoded(), language: "en", nowMs });
}

export function externalModel(nowMs: number = NOW): CardModel {
  return buildModel({ dashboard: bare(), language: "en", nowMs });
}

/** Deterministic, language-free labels: the SVG must not depend on any particular language. */
export const TEST_LABELS: ChartLabels = {
  time: (instantMs) => `t${instantMs}`,
};

/** The now fact a render request carries: the model's own current interval unless a test injects one. */
export function nowFor(model: CardModel, nowMs?: number): ChartNow {
  return nowMs === undefined
    ? { mark: model.chart.current, identity: intervalIdentity(model.chart.current) }
    : chartNowAt(model.chart.marks, nowMs);
}

export function renderFor(
  model: CardModel,
  options: {
    width?: number;
    height?: number;
    selected?: ChartSeries["marks"][number] | null;
    now?: ChartNow;
    nowMs?: number;
    title?: string;
    description?: string;
  } = {},
): ChartRenderResult {
  return renderChart({
    series: model.chart,
    bands: model.bands,
    now: options.now ?? nowFor(model, options.nowMs),
    width: options.width ?? 320,
    height: options.height ?? 150,
    selected: options.selected ?? null,
    title: options.title ?? "chart-title",
    description: options.description ?? "chart-description",
    labels: TEST_LABELS,
    ids: { title: "test-chart-title", description: "test-chart-description" },
  });
}

/** The geometry the interaction needs, from a model, without drawing anything. */
export function geometryFor(model: CardModel, width = 320, height = 150) {
  const scale = chartScale(width, height, model.chart.marks.map((mark) => mark.price));
  return { model, scale, width, height };
}

export interface FakeObserver extends ChartObserverLike {
  host: Element | null;
  disconnects: number;
  fire(): void;
}

/** A deterministic stand-in for `ResizeObserver`: no timers, and every call is recorded. */
export function fakeObservers(): { factory: (host: HTMLElement, onResize: () => void) => ChartObserverLike | null; created: FakeObserver[] } {
  const created: FakeObserver[] = [];
  return {
    created,
    factory: (host: HTMLElement, onResize: () => void) => {
      const observer: FakeObserver = {
        host: null,
        disconnects: 0,
        observe(element: Element) {
          observer.host = element;
        },
        disconnect() {
          observer.disconnects += 1;
        },
        fire() {
          onResize();
        },
      };
      created.push(observer);
      return observer;
    },
  };
}

/** A mount target plus deterministic ids, as the production card would pass them. */
export function mountPoint(prefix = "test"): { host: HTMLElement; root: ShadowRoot; idPrefix: string } {
  const host = document.createElement("div");
  document.body.append(host);
  const root = host.attachShadow({ mode: "open" });
  return { host, root, idPrefix: prefix };
}

/** A fixed measurement, so no test depends on layout. */
export function fixedMeasure(size: ChartSize) {
  return () => size;
}

/** A model whose rows are 60-minute intervals: the renderer must draw segments, not points. */
export function hourlyModel(nowMs: number = NOW): CardModel {
  return buildModel({
    dashboard: dashboardWithRows("2026-09-22T04:00:00+00:00", [100, 200], { duration: 60 }),
    language: "en",
    nowMs,
  });
}

/** A model whose rows are quarter-hours, with per-row flags for the band tests. */
export function quarterModel(
  flags: (index: number) => Partial<Record<string, unknown>>,
  prices: readonly (number | null)[] = [100, 200, 300, 400],
  nowMs: number = NOW,
): CardModel {
  return buildModel({
    dashboard: dashboardWithRows("2026-09-22T04:00:00+00:00", prices, { flags }),
    language: "en",
    nowMs,
  });
}

/** Two local days of quarter-hours: today (SEPT) and tomorrow, for the panel and tomorrow styling. */
export function twoDayModel(nowMs: number = NOW): CardModel {
  const today = dashboardWithRows("2026-09-22T04:00:00+00:00", [100, 200], { day: "2026-09-22" });
  const tomorrow = dashboardWithRows("2026-09-23T04:00:00+00:00", [300, 400], { day: "2026-09-23" });
  return buildModel({ dashboard: dashboardWithDayRows(today, tomorrow), language: "en", nowMs });
}

/**
 * A capture that spans local midnight: the date the clock has left, then the one it is in.
 *
 * This is what the card really accepts when a plan horizon was calculated before local midnight -- or
 * when a read published before it is still the newest accepted answer. The second date is the one the
 * clock is in by then, whatever role the rows were published with.
 */
export function midnightModel(nowMs: number = AFTER_MIDNIGHT_MS): CardModel {
  return buildModel({
    dashboard: dashboardWithDayRows(
      dashboardWithRows("2026-09-22T21:45:00+00:00", [100], { day: SEPT }),
      dashboardWithRows("2026-09-22T22:00:00+00:00", [200, 300, 400, 500], { day: NEXT_DAY }),
    ),
    language: "en",
    nowMs,
  });
}

/** Several decoded days' rows in one decoded dashboard, in instant order, through the real decoder. */
export function dashboardWithDayRows(...days: Dashboard[]): Dashboard {
  const rows = days.flatMap((day) => day.prices.intervals);
  const payload = rawDashboard();
  (payload.prices as Record<string, unknown>).intervals = rows.map((row) => ({
    start: new Date(row.startMs).toISOString(),
    end: new Date(row.endMs).toISOString(),
    day: row.day,
    duration_minutes: row.duration_minutes,
    raw_price: row.effective_price,
    fallback_price: null,
    effective_price: row.effective_price,
    known: true,
    estimated: false,
    basis_day: null,
    proposal_planned: false,
    installed_planned: false,
  }));
  const result = decodeDashboard(payload);
  if (!result.ok) {
    throw new Error("multi-day fixture did not decode");
  }
  return result.value;
}

/**
 * The narrow cross-client fixture, named exactly as the relay's `chart-focus.test.ts` names it.
 *
 * Same area, same two days, same prices, same clock instant, same selection and the same planned
 * span, so all three clients can be asserted against the *same numbers* rather than against
 * antialiased pixels: today's 00:15 is x fraction 15/1440, the selected 00:30 is 30/1440, the current
 * interval ends 8 minutes after the clock, and a 00:20–00:40 span lands on those same two minutes.
 * `frontend/test/chart-cross-client.test.ts` asserts them; Android and the web planner assert the
 * same fractions from the same values.
 */
export const CROSS_TZ = "Europe/Stockholm";
export const CROSS_TODAY = "2026-09-12";
export const CROSS_TOMORROW = "2026-09-13";
/** Four quarter-hour prices per day, in both directions. */
export const CROSS_PRICES = [100, 200, 300, 400] as const;
/** 00:22 local: inside the interval that starts at 00:15, 8 minutes before it ends. */
export const CROSS_NOW = Date.parse("2026-09-12T00:22:00+02:00");
/** 00:37 local: the interval that starts at 00:30. */
export const CROSS_SELECTED = Date.parse("2026-09-12T00:37:00+02:00");
export const CROSS_CURRENT_START = Date.parse("2026-09-12T00:15:00+02:00");
export const CROSS_SELECTED_START = Date.parse("2026-09-12T00:30:00+02:00");
/** The two x fractions every client must agree on, as fractions of one 24-hour day. */
export const CROSS_CURRENT_FRACTION = 15 / 1440;
export const CROSS_SELECTED_FRACTION = 30 / 1440;
export const CROSS_BOUNDARY_MS = 8 * 60_000;

/** Today and tomorrow of the cross-client fixture, decoded through the real validator. */
export function crossClientModel(
  options: { planned?: boolean } = {},
): CardModel {
  // The backend flags whole published intervals, so the fixture's planned span is carried as the two
  // quarter-hours the relay states as 00:20-00:40: 00:15 to 00:45 on each day.
  const flags = (index: number): Partial<Record<string, unknown>> =>
    options.planned === true && (index === 1 || index === 2) ? { installed_planned: true } : {};
  const today = dashboardWithRows("2026-09-11T22:00:00+00:00", CROSS_PRICES, {
    day: CROSS_TODAY,
    flags,
  });
  const tomorrow = dashboardWithRows("2026-09-12T22:00:00+00:00", CROSS_PRICES, {
    day: CROSS_TOMORROW,
    flags,
  });
  const rows = [...today.prices.intervals, ...tomorrow.prices.intervals];
  const payload = rawDashboard();
  (payload.prices as Record<string, unknown>).intervals = rows.map((row) => ({
    start: new Date(row.startMs).toISOString(),
    end: new Date(row.endMs).toISOString(),
    day: row.day,
    duration_minutes: row.duration_minutes,
    raw_price: row.effective_price,
    fallback_price: null,
    effective_price: row.effective_price,
    known: true,
    estimated: false,
    basis_day: null,
    proposal_planned: row.proposal_planned,
    installed_planned: row.installed_planned,
  }));
  const result = decodeDashboard(payload);
  if (!result.ok) {
    throw new Error(`cross-client fixture did not decode: ${result.failure}`);
  }
  return buildModel({ dashboard: result.value, language: "en", nowMs: CROSS_NOW });
}

/**
 * The autumn night: 01:00 local happens twice, so the walk must reach both instants.
 *
 * 00:00Z and 01:00Z are the first and second pass of 02:00 local on 2026-10-25 in Stockholm.
 */
export function autumnModel(nowMs: number = Date.parse("2026-10-25T00:30:00Z")): CardModel {
  const first = dashboardWithRows("2026-10-25T00:00:00+00:00", [10, 11], { day: "2026-10-25" });
  const second = dashboardWithRows("2026-10-25T01:00:00+00:00", [20, 21], { day: "2026-10-25" });
  const rows = [...first.prices.intervals, ...second.prices.intervals];
  const payload = rawDashboard();
  (payload.prices as Record<string, unknown>).intervals = rows.map((row) => ({
    start: row.start,
    end: row.end,
    day: row.day,
    duration_minutes: row.duration_minutes,
    raw_price: row.raw_price,
    fallback_price: null,
    effective_price: row.effective_price,
    known: true,
    estimated: false,
    basis_day: null,
    proposal_planned: false,
    installed_planned: false,
  }));
  const result = decodeDashboard(payload);
  if (!result.ok) {
    throw new Error(`autumn fixture did not decode: ${result.failure}`);
  }
  return buildModel({ dashboard: result.value, language: "en", nowMs });
}

/** A host with the chart drawn into it, as the card view would build it. */
export function chartHost(
  model: CardModel,
  options: { width?: number; height?: number; prefix?: string } = {},
): { host: HTMLElement; result: ChartRenderResult } {
  const host = document.createElement("div");
  document.body.append(host);
  const result = renderFor(model, { width: options.width ?? 320, height: options.height ?? 150 });
  host.append(result.element);
  return { host, result };
}
