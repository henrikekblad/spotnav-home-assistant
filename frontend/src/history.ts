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

// ---------------------------------------------------------------------------------------- export range

export const HISTORY_RANGES = ["thisMonth", "lastMonth", "last12", "all"] as const;
export type HistoryRange = (typeof HISTORY_RANGES)[number];

function monthParts(period: string): [number, number] | null {
  const match = /^(\d{4})-(\d{2})$/.exec(period);
  return match === null ? null : [Number(match[1]), Number(match[2])];
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function monthStart(year: number, month: number): string {
  return `${year}-${pad(month)}-01`;
}

function monthEnd(year: number, month: number): string {
  return `${year}-${pad(month)}-${pad(new Date(Date.UTC(year, month, 0)).getUTCDate())}`;
}

/**
 * The local dates an export range covers, from the months the answer itself names (so the card needs
 * no clock of its own and the dates are the backend's own local days). `null` ends are open.
 */
export function exportDates(
  range: HistoryRange,
  answer: SessionsAnswer,
): { from: string | null; to: string | null } {
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

// ----------------------------------------------------------------------------------------------- state

export type HistoryState =
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null }
  | { kind: "ready"; answer: SessionsAnswer };

export interface HistoryUi {
  list: "days" | "months";
  range: HistoryRange;
  exporting: boolean;
  /** One sentence about the last export, or `null`. */
  notice: TranslationKey | null;
}

export interface HistoryHandlers {
  onList: (list: "days" | "months") => void;
  onRange: (range: HistoryRange) => void;
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

function bucketCard(doc: Document, language: Language, titleKey: TranslationKey, bucket: SessionBucket): HTMLElement {
  const card = element(doc, "section", C.historyTile);
  card.dataset["tile"] = titleKey === "history.thisMonth" ? "thisMonth" : "lastMonth";
  card.append(element(doc, "h4", C.historyTileHeading, translate(language, titleKey)));
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

function periodRow(doc: Document, language: Language, kind: "days" | "months", bucket: SessionBucket): HTMLElement {
  const row = element(doc, "li", C.historyRow);
  row.dataset["period"] = bucket.period;
  row.append(
    element(
      doc,
      "span",
      C.historyRowTitle,
      kind === "days" ? dateLabel(language, bucket.period) : monthLabel(language, bucket.period),
    ),
    element(doc, "span", C.historyRowFigures, figures(language, bucket)),
  );
  const notes = [sessionsCount(language, bucket.sessions)];
  if (bucket.solar_share !== null) {
    notes.push(translate(language, "history.solar", { percent: percent(language, bucket.solar_share) }));
  }
  const savings = savingsLine(language, bucket);
  if (savings !== null) {
    notes.push(savings);
  }
  row.append(element(doc, "span", `${C.muted} ${C.historyRowNote}`, notes.join(" · ")));
  return row;
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

function toggle(
  doc: Document,
  language: Language,
  ui: HistoryUi,
  handlers: HistoryHandlers,
): HTMLElement {
  const group = element(doc, "div", C.historyToggle);
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", translate(language, "history.listLabel"));
  for (const choice of ["days", "months"] as const) {
    const button = element(doc, "button", C.historyToggleButton, translate(language, choice === "days" ? "history.days" : "history.months"));
    button.type = "button";
    button.dataset["list"] = choice;
    button.setAttribute("aria-pressed", String(ui.list === choice));
    button.addEventListener("click", () => handlers.onList(choice));
    group.append(button);
  }
  return group;
}

function exportRow(doc: Document, language: Language, ui: HistoryUi, handlers: HistoryHandlers): HTMLElement {
  const row = element(doc, "div", C.historyExport);
  const label = element(doc, "label", C.historyExportLabel, translate(language, "history.export.period"));
  const select = element(doc, "select");
  select.dataset["exportRange"] = "true";
  for (const range of HISTORY_RANGES) {
    const option = new Option(translate(language, `history.range.${range}` as TranslationKey), range);
    option.selected = range === ui.range;
    select.append(option);
  }
  select.addEventListener("change", () => {
    const chosen = HISTORY_RANGES.find((range) => range === select.value);
    if (chosen !== undefined) {
      handlers.onRange(chosen);
    }
  });
  label.append(select);
  const button = element(doc, "button", C.button, translate(language, "history.export"));
  button.type = "button";
  button.dataset["action"] = "export";
  button.disabled = ui.exporting;
  button.addEventListener("click", () => handlers.onExport());
  row.append(label, button);
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
    const failed = element(doc, "p", C.settingsNotice, translate(language, state.sentenceKey));
    failed.setAttribute("role", "status");
    if (state.code !== null) {
      failed.dataset["code"] = state.code;
    }
    body.append(failed);
    return body;
  }
  const answer = state.answer;
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
  const tiles = element(doc, "div", C.historyTiles);
  tiles.append(
    bucketCard(doc, language, "history.thisMonth", answer.this_month),
    bucketCard(doc, language, "history.lastMonth", answer.last_month),
  );
  body.append(tiles);
  if (answer.sessions.length === 0 && answer.open === null) {
    body.append(element(doc, "p", C.muted, translate(language, "history.empty")));
  } else {
    body.append(toggle(doc, language, ui, handlers));
    const buckets = ui.list === "days" ? answer.days : answer.months;
    const periods = element(doc, "ul", C.historyList);
    periods.dataset["list"] = ui.list;
    for (const bucket of buckets) {
      periods.append(periodRow(doc, language, ui.list, bucket));
    }
    body.append(periods);
    body.append(element(doc, "h4", C.historyHeading, translate(language, "history.latest")));
    const latest = element(doc, "ul", C.historyList);
    latest.dataset["list"] = "sessions";
    for (const session of answer.sessions) {
      latest.append(sessionRow(doc, language, session));
    }
    body.append(latest);
  }
  body.append(element(doc, "p", `${C.muted} ${C.historyFootnote}`, translate(language, "history.savings.note")));
  body.append(exportRow(doc, language, ui, handlers));
  if (ui.notice !== null) {
    const notice = element(doc, "p", C.settingsNotice, translate(language, ui.notice));
    notice.setAttribute("role", "status");
    body.append(notice);
  }
  return body;
}
