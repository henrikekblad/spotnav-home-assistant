// The charge history: the decoded `spotnav/get_sessions` answer and the dialog body that shows it.
//
// What each charge cost is the backend's figure (the plan's own effective price with the person's tax,
// grid fee and VAT, at the time the energy was delivered); nothing is recomputed here. The savings line
// compares with the day's average price and is always worded as an estimate. Every text from the answer
// (a vehicle's name) is written with `textContent`, never parsed as markup.

import { SESSIONS_API_VERSION } from "./types";
import { dateLabel, energyAmount, formatNumber, money, pricePerKwh, type FormatContext } from "./format";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

export interface SessionRecord {
  id: string;
  start: string;
  end: string | null;
  energy_kwh: number;
  energy_source: string;
  estimated: boolean;
  cost: number | null;
  currency: string | null;
  major_unit: string | null;
  minor_unit: string | null;
  average_price_minor_per_kwh: number | null;
  started_by: string;
  strategy: string | null;
  vehicle: string | null;
  solar_share: number | null;
  reference_cost: number | null;
  savings: number | null;
}

export interface SessionBucket {
  period: string;
  sessions: number;
  energy_kwh: number;
  cost: number | null;
  currency: string | null;
  major_unit: string | null;
  minor_unit: string | null;
  average_price_minor_per_kwh: number | null;
  solar_share: number | null;
  reference_cost: number | null;
  savings: number | null;
  estimated: boolean;
}

export interface SessionsAnswer {
  this_month: SessionBucket;
  last_month: SessionBucket;
  months: SessionBucket[];
  days: SessionBucket[];
  open: SessionRecord | null;
  sessions: SessionRecord[];
  /** The month the `month_*` parts are for (`YYYY-MM`). */
  month: string;
  month_summary: SessionBucket;
  /** One bucket for every day of the month, oldest first, a day with no charge as a zero row. */
  month_days: SessionBucket[];
  month_sessions: SessionRecord[];
  /** The months that have data, newest first. */
  available_months: string[];
}

export type SessionsDecodeResult =
  | { ok: true; value: SessionsAnswer }
  | { ok: false; failure: "unsupported" | "malformed" };

class Malformed extends Error {}

function bad(): never {
  throw new Malformed();
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : bad();
}

function text(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : bad();
}

function textOrNull(source: Record<string, unknown>, key: string): string | null {
  const value = source[key];
  return value === null ? null : typeof value === "string" ? value : bad();
}

function number(source: Record<string, unknown>, key: string): number {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function numberOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  return value === null ? null : typeof value === "number" && Number.isFinite(value) ? value : bad();
}

function flag(source: Record<string, unknown>, key: string): boolean {
  const value = source[key];
  return typeof value === "boolean" ? value : bad();
}

function shareOrNull(source: Record<string, unknown>, key: string): number | null {
  const value = numberOrNull(source, key);
  return value !== null && (value < 0 || value > 1) ? bad() : value;
}

function month(source: Record<string, unknown>, key: string): string {
  const value = text(source, key);
  return monthParts(value) === null ? bad() : value;
}

function decodeBucket(raw: unknown): SessionBucket {
  const source = record(raw);
  const sessions = number(source, "sessions");
  if (!Number.isInteger(sessions) || sessions < 0) {
    bad();
  }
  return {
    period: text(source, "period"),
    sessions,
    energy_kwh: number(source, "energy_kwh"),
    cost: numberOrNull(source, "cost"),
    currency: textOrNull(source, "currency"),
    major_unit: textOrNull(source, "major_unit"),
    minor_unit: textOrNull(source, "minor_unit"),
    average_price_minor_per_kwh: numberOrNull(source, "average_price_minor_per_kwh"),
    solar_share: shareOrNull(source, "solar_share"),
    reference_cost: numberOrNull(source, "reference_cost"),
    savings: numberOrNull(source, "savings"),
    estimated: flag(source, "estimated"),
  };
}

function decodeRecord(raw: unknown): SessionRecord {
  const source = record(raw);
  return {
    id: text(source, "id"),
    start: text(source, "start"),
    end: textOrNull(source, "end"),
    energy_kwh: number(source, "energy_kwh"),
    energy_source: text(source, "energy_source"),
    estimated: flag(source, "estimated"),
    cost: numberOrNull(source, "cost"),
    currency: textOrNull(source, "currency"),
    major_unit: textOrNull(source, "major_unit"),
    minor_unit: textOrNull(source, "minor_unit"),
    average_price_minor_per_kwh: numberOrNull(source, "average_price_minor_per_kwh"),
    started_by: text(source, "started_by"),
    strategy: textOrNull(source, "strategy"),
    vehicle: textOrNull(source, "vehicle"),
    solar_share: shareOrNull(source, "solar_share"),
    reference_cost: numberOrNull(source, "reference_cost"),
    savings: numberOrNull(source, "savings"),
  };
}

function list<T>(source: Record<string, unknown>, key: string, decode: (raw: unknown) => T): T[] {
  const value = source[key];
  return Array.isArray(value) ? value.map(decode) : bad();
}

/** The answer, strictly typed; extra keys are allowed (additive fields), a missing or mistyped one is not. */
export function decodeSessions(raw: unknown): SessionsDecodeResult {
  try {
    const source = record(raw);
    if (source.api_version !== SESSIONS_API_VERSION) {
      return { ok: false, failure: typeof source.api_version === "number" ? "unsupported" : "malformed" };
    }
    return {
      ok: true,
      value: {
        this_month: decodeBucket(source.this_month),
        last_month: decodeBucket(source.last_month),
        months: list(source, "months", decodeBucket),
        days: list(source, "days", decodeBucket),
        open: source.open === null ? null : decodeRecord(source.open),
        sessions: list(source, "sessions", decodeRecord),
        month: month(source, "month"),
        month_summary: decodeBucket(source.month_summary),
        month_days: list(source, "month_days", decodeBucket),
        month_sessions: list(source, "month_sessions", decodeRecord),
        available_months: list(source, "available_months", (raw) => (typeof raw === "string" && monthParts(raw) !== null ? raw : bad())),
      },
    };
  } catch {
    return { ok: false, failure: "malformed" };
  }
}

/** The CSV answer: the text and the file name to save it under. */
export function decodeCsv(raw: unknown): { filename: string; csv: string } | null {
  try {
    const source = record(raw);
    if (source.api_version !== SESSIONS_API_VERSION || source.format !== "csv") {
      return null;
    }
    return { filename: text(source, "filename"), csv: text(source, "csv") };
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------------------------------- months

/** How many months back the backend answers for. */
export const MONTHS_BACK = 24;

function monthParts(period: string): [number, number] | null {
  const match = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(period);
  return match === null ? null : [Number(match[1]), Number(match[2])];
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** The month `offset` months from `period` (negative is earlier), `YYYY-MM`. */
export function shiftMonth(period: string, offset: number): string {
  const parts = monthParts(period);
  if (parts === null) {
    return period;
  }
  const index = parts[0] * 12 + parts[1] - 1 + offset;
  return `${Math.floor(index / 12)}-${pad((index % 12) + 1)}`;
}

/**
 * The months the picker offers, newest first: those with data, the current month (so the picker
 * always has "now") and the one shown.
 */
export function pickerMonths(answer: SessionsAnswer, shown: string): string[] {
  return Array.from(new Set([...answer.available_months, answer.this_month.period, shown]))
    .filter((period) => period >= shiftMonth(answer.this_month.period, -MONTHS_BACK) && period <= answer.this_month.period)
    .sort()
    .reverse();
}

/**
 * Each day's position between the month's cheapest (0) and dearest (1) average price, `null` for a day
 * with no price. A month whose days all cost the same is all cheap.
 */
export function dayPricePositions(days: SessionBucket[]): Array<number | null> {
  const priced = days
    .filter((day) => day.energy_kwh > 0 && day.average_price_minor_per_kwh !== null)
    .map((day) => day.average_price_minor_per_kwh as number);
  const low = Math.min(...priced);
  const high = Math.max(...priced);
  return days.map((day) =>
    day.energy_kwh > 0 && day.average_price_minor_per_kwh !== null
      ? high > low
        ? (day.average_price_minor_per_kwh - low) / (high - low)
        : 0
      : null,
  );
}

// ----------------------------------------------------------------------------------------------- state

export type HistoryState =
  | { kind: "loading" }
  /** `answer` is the last good one, so the month picker stays usable after a failed month. */
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null; answer?: SessionsAnswer | null }
  | { kind: "ready"; answer: SessionsAnswer };

export interface HistoryUi {
  /** The month the reader asked for (`YYYY-MM`), or `null` for the current one. */
  month: string | null;
  /** A month is being read: the old figures stay, dimmed. */
  pending: boolean;
  /** The day whose figures the chart's readout shows, or `null`. */
  day: string | null;
  exporting: boolean;
  /** One sentence about the last export, or `null`. */
  notice: TranslationKey | null;
}

export interface HistoryHandlers {
  onMonth: (month: string) => void;
  onExport: () => void;
}

// ------------------------------------------------------------------------------------------- rendering

const monthFormats = new Map<string, Intl.DateTimeFormat>();

/** `2026-09` as the reader's language writes a month. */
export function monthLabel(language: Language, period: string): string {
  const parts = monthParts(period);
  if (parts === null) {
    return period;
  }
  let format = monthFormats.get(language);
  if (format === undefined) {
    format = new Intl.DateTimeFormat(language, { month: "long", year: "numeric", timeZone: "UTC" });
    monthFormats.set(language, format);
  }
  return format.format(new Date(Date.UTC(parts[0], parts[1] - 1, 1, 12)));
}

function formatContext(
  language: Language,
  source: { currency: string | null; major_unit: string | null; minor_unit: string | null },
): FormatContext {
  return {
    language,
    timeZone: "",
    unit: source.minor_unit ?? "",
    currency: source.currency,
    majorUnit: source.major_unit,
  };
}

function element<K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  className?: string,
  content?: string,
): HTMLElementTagNameMap[K] {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (content !== undefined) {
    created.textContent = content;
  }
  return created;
}

/** The wall clock as the backend wrote it (Home Assistant's own zone): `2026-09-22T14:05:00+02:00` -> `14:05`. */
function clockOf(iso: string): string {
  return /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})/.exec(iso)?.[1] ?? "";
}

function dayOf(iso: string): string {
  return iso.slice(0, 10);
}

function percent(language: Language, share: number): string {
  return `${formatNumber(language, share * 100, 0)} %`;
}

function sessionsCount(language: Language, count: number): string {
  const key: TranslationKey = `history.sessions.${pluralForm(language, count)}`;
  return translate(language, key, { count: formatNumber(language, count, 0) });
}

function startedByKey(startedBy: string): TranslationKey {
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

/** The savings as a sentence, or `null` when there is nothing to compare (or no difference worth a line). */
export function savingsLine(
  language: Language,
  source: { savings: number | null; currency: string | null; major_unit: string | null; minor_unit: string | null },
): string | null {
  if (source.savings === null || Math.abs(source.savings) < 0.005) {
    return null;
  }
  const amount = money(formatContext(language, source), Math.abs(source.savings));
  return translate(language, source.savings > 0 ? "history.savings.saved" : "history.savings.extra", { amount });
}

function figures(language: Language, bucket: SessionBucket | SessionRecord): string {
  const context = formatContext(language, bucket);
  const cost = bucket.cost === null ? translate(language, "history.noCost") : money(context, bucket.cost);
  const parts = [energyAmount(language, bucket.energy_kwh), cost];
  if (bucket.average_price_minor_per_kwh !== null && bucket.minor_unit !== null) {
    parts.push(pricePerKwh(context, bucket.average_price_minor_per_kwh));
  }
  return parts.join(" · ");
}

function monthSummary(doc: Document, language: Language, bucket: SessionBucket): HTMLElement {
  const card = element(doc, "section", C.historyTile);
  card.dataset["tile"] = "month";
  if (bucket.sessions === 0) {
    card.append(element(doc, "p", C.muted, translate(language, "history.noneInMonth")));
    return card;
  }
  card.append(element(doc, "p", C.historyFigures, figures(language, bucket)));
  const notes = [sessionsCount(language, bucket.sessions)];
  if (bucket.solar_share !== null) {
    notes.push(translate(language, "history.solar", { percent: percent(language, bucket.solar_share) }));
  }
  if (bucket.estimated) {
    notes.push(translate(language, "history.estimated"));
  }
  card.append(element(doc, "p", C.muted, notes.join(" · ")));
  const savings = savingsLine(language, bucket);
  if (savings !== null) {
    card.append(element(doc, "p", `${C.muted} ${C.historySavings}`, savings));
  }
  return card;
}

/** The figures one day's bar stands for, as one line. */
export function dayFigures(language: Language, day: SessionBucket): string {
  const date = dateLabel(language, day.period);
  if (day.sessions === 0) {
    return translate(language, "history.chart.noCharge", { date });
  }
  const parts = [figures(language, day)];
  if (day.solar_share !== null) {
    parts.push(translate(language, "history.solar", { percent: percent(language, day.solar_share) }));
  }
  return `${date}: ${parts.join(" · ")}`;
}

/**
 * One bar per day of the month: height is the day's energy, colour its average price against the
 * month (the card's cheap green to expensive red). Hover, focus or a tap shows that day's figures in
 * the readout under the chart; nothing is repainted, so a hover never loses its place.
 */
function dayChart(doc: Document, language: Language, days: SessionBucket[], ui: HistoryUi): HTMLElement {
  const wrap = element(doc, "div", C.historyChart);
  const top = Math.max(0, ...days.map((day) => day.energy_kwh));
  const positions = dayPricePositions(days);
  const readout = element(doc, "p", `${C.muted} ${C.historyReadout}`);
  readout.setAttribute("aria-live", "polite");
  const hint = translate(language, "history.chart.hint");
  const bars: HTMLButtonElement[] = [];
  const show = (day: SessionBucket | null): void => {
    readout.textContent = day === null ? hint : dayFigures(language, day);
    bars.forEach((bar) => bar.setAttribute("aria-pressed", String(day !== null && bar.dataset["day"] === day.period)));
  };
  const plot = element(doc, "div", C.historyBars);
  plot.setAttribute("role", "group");
  plot.setAttribute("aria-label", translate(language, "history.chart.label"));
  days.forEach((day, index) => {
    const bar = element(doc, "button", C.historyBar);
    bar.type = "button";
    bar.dataset["day"] = day.period;
    bar.setAttribute("aria-label", dayFigures(language, day));
    const fill = element(doc, "span", C.historyBarFill);
    const position = positions[index] ?? null;
    if (day.energy_kwh > 0 && top > 0) {
      fill.style.height = `${Math.max(4, (day.energy_kwh / top) * 100)}%`;
      if (position === null) {
        fill.dataset["price"] = "none";
      } else {
        fill.style.setProperty("--spotnav-day-dear", `${Math.round(position * 100)}%`);
      }
    } else {
      fill.dataset["empty"] = "true";
    }
    bar.append(fill);
    const choose = (): void => {
      ui.day = day.period;
      show(day);
    };
    bar.addEventListener("mouseenter", () => show(day));
    bar.addEventListener("focus", () => show(day));
    bar.addEventListener("click", choose);
    bars.push(bar);
    plot.append(bar);
  });
  plot.addEventListener("mouseleave", () => show(days.find((day) => day.period === ui.day) ?? null));
  const axis = element(doc, "div", C.historyAxis);
  axis.setAttribute("aria-hidden", "true");
  days.forEach((day, index) => {
    const number = index + 1;
    const labelled = number === 1 || number === days.length || (number % 5 === 0 && days.length - number >= 3);
    axis.append(element(doc, "span", undefined, labelled ? String(number) : ""));
  });
  const scale = element(doc, "span", `${C.muted} ${C.historyScale}`, energyAmount(language, top));
  scale.setAttribute("aria-hidden", "true");
  wrap.append(scale, plot, axis, readout);
  show(days.find((day) => day.period === ui.day) ?? null);
  return wrap;
}

function sessionRow(doc: Document, language: Language, session: SessionRecord): HTMLElement {
  const row = element(doc, "li", C.historyRow);
  row.dataset["session"] = session.id;
  const end = session.end === null ? "" : clockOf(session.end);
  const crosses = session.end !== null && dayOf(session.end) !== dayOf(session.start);
  const when = `${dateLabel(language, dayOf(session.start))} ${clockOf(session.start)}–${crosses ? `${dateLabel(language, dayOf(session.end as string))} ` : ""}${end}`;
  row.append(
    element(doc, "span", C.historyRowTitle, when),
    element(doc, "span", C.historyRowFigures, figures(language, session)),
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
  row.append(element(doc, "span", `${C.muted} ${C.historyRowNote}`, notes.join(" · ")));
  return row;
}

function monthPicker(
  doc: Document,
  language: Language,
  answer: SessionsAnswer,
  shown: string,
  handlers: HistoryHandlers,
): HTMLElement {
  const row = element(doc, "div", C.historyMonthPicker);
  const current = answer.this_month.period;
  const step = (label: TranslationKey, glyph: string, target: string, allowed: boolean): HTMLButtonElement => {
    const button = element(doc, "button", C.historyToggleButton, glyph);
    button.type = "button";
    button.dataset["month"] = target;
    button.setAttribute("aria-label", translate(language, label));
    button.disabled = !allowed;
    button.addEventListener("click", () => handlers.onMonth(target));
    return button;
  };
  const earliest = shiftMonth(current, -MONTHS_BACK);
  const select = element(doc, "select");
  select.dataset["monthSelect"] = "true";
  select.setAttribute("aria-label", translate(language, "history.month.label"));
  for (const period of pickerMonths(answer, shown)) {
    const option = new Option(monthLabel(language, period), period);
    option.selected = period === shown;
    select.append(option);
  }
  select.addEventListener("change", () => {
    if (monthParts(select.value) !== null) {
      handlers.onMonth(select.value);
    }
  });
  row.append(
    step("history.month.previous", "‹", shiftMonth(shown, -1), shown > earliest),
    select,
    step("history.month.next", "›", shiftMonth(shown, 1), shown < current),
  );
  return row;
}

function exportRow(doc: Document, language: Language, ui: HistoryUi, handlers: HistoryHandlers): HTMLElement {
  const row = element(doc, "div", C.historyExport);
  const button = element(doc, "button", C.button, translate(language, "history.export"));
  button.type = "button";
  button.dataset["action"] = "export";
  button.disabled = ui.exporting || ui.pending;
  button.addEventListener("click", () => handlers.onExport());
  row.append(button);
  return row;
}

/** The dialog body for one state. Built fresh each time; the caller replaces the dialog's content. */
export function historyBody(
  doc: Document,
  language: Language,
  state: HistoryState,
  ui: HistoryUi,
  handlers: HistoryHandlers,
): HTMLElement {
  const body = element(doc, "div", C.historyBody);
  if (state.kind === "loading") {
    const loading = element(doc, "p", C.muted, translate(language, "history.loading"));
    loading.setAttribute("role", "status");
    body.append(loading);
    return body;
  }
  if (state.kind === "failed") {
    if (state.answer !== undefined && state.answer !== null) {
      body.append(monthPicker(doc, language, state.answer, ui.month ?? state.answer.month, handlers));
    }
    const failed = element(doc, "p", C.settingsNotice, translate(language, state.sentenceKey));
    failed.setAttribute("role", "status");
    if (state.code !== null) {
      failed.dataset["code"] = state.code;
    }
    body.append(failed);
    return body;
  }
  const answer = state.answer;
  const shown = ui.month ?? answer.month;
  body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(language, "history.intro")));
  if (answer.open !== null) {
    const open = element(
      doc,
      "p",
      C.historyOpen,
      translate(language, "history.open", {
        time: clockOf(answer.open.start),
        energy: energyAmount(language, answer.open.energy_kwh),
      }),
    );
    open.setAttribute("role", "status");
    body.append(open);
  }
  body.append(monthPicker(doc, language, answer, shown, handlers));
  const month = element(doc, "div", C.historyMonth);
  month.dataset["month"] = answer.month;
  if (ui.pending) {
    month.setAttribute("aria-busy", "true");
    month.dataset["pending"] = "true";
  }
  month.append(monthSummary(doc, language, answer.month_summary));
  if (answer.month_summary.sessions > 0) {
    month.append(dayChart(doc, language, answer.month_days, ui));
  }
  if (answer.month_sessions.length > 0) {
    month.append(element(doc, "h4", C.historyHeading, translate(language, "history.sessionsHeading")));
    const latest = element(doc, "ul", C.historyList);
    latest.dataset["list"] = "sessions";
    for (const session of answer.month_sessions) {
      latest.append(sessionRow(doc, language, session));
    }
    month.append(latest);
  }
  body.append(month);
  body.append(element(doc, "p", `${C.muted} ${C.historyFootnote}`, translate(language, "history.savings.note")));
  body.append(exportRow(doc, language, ui, handlers));
  if (ui.notice !== null) {
    const notice = element(doc, "p", C.settingsNotice, translate(language, ui.notice));
    notice.setAttribute("role", "status");
    body.append(notice);
  }
  return body;
}
