// The house card's body: a header, the one chart with its legend and readout, one line of money and one line of how
// well the forecasts hit, and the details behind a tap (the battery's modes side by side, the reviewed days, the
// sources and what was assumed). Built fresh for each state with `textContent` only; every figure is the backend's.

import { clock, dateLabel, dayCue, formatFixed, formatNumber, hourLabel, type EntityWords, type FormatContext } from "./format";
import { houseChartHeight, houseShown, renderHouseChart, sunShown, type HouseChart, type HouseChartView } from "./house-chart";
import { MODES, type BatteryMode, type DayFlow, type HouseAnswer, type HouseHour } from "./house-model";
import { HOUSE_CLASSES as H } from "./house-styles";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

const HOUR_MS = 3_600_000;

export type HouseState =
  | { kind: "unconfigured" }
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null }
  | { kind: "ready"; answer: HouseAnswer };

export interface HouseUi {
  detailsOpen: boolean;
  /** The chart's view: the energy (default) or the price with the battery. */
  view?: HouseChartView;
  /** The hour shown in the readout (an index into `hours`), `null` for the hint. */
  selected: number | null;
}

export interface HouseHandlers {
  onDetails(open: boolean): void;
  onView?(view: HouseChartView): void;
  onSelect(index: number | null): void;
}

export interface HouseRenderInput {
  doc: Document;
  language: Language;
  state: HouseState;
  ui: HouseUi;
  handlers: HouseHandlers;
  /** The chart's width in CSS pixels (the card's measured content width). */
  width: number;
  nowMs: number;
  idPrefix: string;
  entityWords?: EntityWords;
}

function element<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, className?: string, content?: string): HTMLElementTagNameMap[K] {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (content !== undefined) {
    created.textContent = content;
  }
  return created;
}

// ------------------------------------------------------------------------------------------------ words

export function kw(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} kW`;
}

function kwh(language: Language, value: number): string {
  return `${formatFixed(language, value, 1)} kWh`;
}

function pct(language: Language, value: number): string {
  return `${formatNumber(language, value, 0)} %`;
}

/** An error figure: two decimals below 1, one above ("±0,09 kW", "±1,2 kW"). */
function plusMinus(language: Language, value: number, unit: string): string {
  return `±${formatFixed(language, value, value < 1 ? 2 : 1)} ${unit}`;
}

/** Money given in the minor unit, shown in the major one: whole units from 10 up ("51 kr"), else two decimals. */
export function amount(language: Language, minor: number, money: HouseAnswer["money"]): string {
  const major = minor / 100;
  if (money === null || money.major_unit === null) {
    return `${formatNumber(language, minor, 0)}${money?.minor_unit ? ` ${money.minor_unit}` : ""}`;
  }
  return `${formatFixed(language, major, Math.abs(major) >= 10 ? 0 : 2)} ${money.major_unit}`;
}

/** The price's unit per kWh, the area's minor unit ("öre/kWh"). */
function priceUnit(answer: HouseAnswer): string {
  return `${answer.money?.minor_unit ?? ""}/kWh`.replace(/^\//, "");
}

/** Today's money so far in one line, as the battery's app splits it, with the mode in force. */
export function todayLine(language: Language, answer: HouseAnswer): string | null {
  const today = answer.today;
  if (today === null) {
    return null;
  }
  return translate(language, "house.today", {
    sold: amount(language, today.sold, answer.money),
    arbitrage: amount(language, today.arbitrage, answer.money),
    bought: amount(language, today.bought, answer.money),
    mode: modeName(language, today.mode),
  });
}

function context(language: Language, answer: HouseAnswer): FormatContext {
  return { language, timeZone: answer.time_zone, unit: "kWh", currency: answer.money?.currency ?? null, majorUnit: answer.money?.major_unit ?? null };
}

function modeName(language: Language, mode: string): string {
  return (MODES as readonly string[]).includes(mode) ? translate(language, `house.mode.${mode as BatteryMode}`) : mode;
}

function entityName(words: EntityWords | undefined, entityId: string): string {
  return words?.name(entityId) ?? entityId;
}

/** One hour's figures in words, for the readout and the hour's accessible name. */
export function hourFigures(language: Language, answer: HouseAnswer, hour: HouseHour, nowMs: number, view: HouseChartView = "energy"): string {
  const ctx = context(language, answer);
  const parts = [`${clock(ctx, hour.startMs)}–${clock(ctx, hour.startMs + HOUR_MS)}`];
  const house = houseShown(hour);
  if (view === "price" && hour.buy !== null && hour.sell !== null) {
    const unit = priceUnit(answer);
    parts.push(
      translate(language, "house.readout.price", {
        buy: `${formatNumber(language, hour.buy, 0)} ${unit}`,
        sell: `${formatNumber(language, hour.sell, 0)} ${unit}`,
      }),
    );
  }
  if (view === "energy" && house.kw !== null) {
    if (house.measured && hour.house_forecast_kw !== null) {
      parts.push(translate(language, "house.readout.houseBoth", { kw: kw(language, house.kw), forecast: kw(language, hour.house_forecast_kw) }));
    } else {
      parts.push(translate(language, house.measured ? "house.readout.house" : "house.readout.houseForecast", { kw: kw(language, house.kw) }));
    }
  }
  const sun = sunShown(hour, nowMs);
  const past = hour.startMs + HOUR_MS <= nowMs;
  if (view === "energy" && sun !== null && (sun > 0 || (hour.sun_forecast_kwh ?? 0) > 0)) {
    parts.push(translate(language, past ? "house.readout.sun" : "house.readout.sunForecast", { kwh: kwh(language, sun) }));
  }
  if (hour.soc_pct !== null) {
    parts.push(translate(language, "house.readout.soc", { pct: pct(language, hour.soc_pct) }));
  } else if (!past && hour.soc_plan_pct !== null) {
    parts.push(translate(language, "house.readout.socPlan", { pct: pct(language, hour.soc_plan_pct) }));
  }
  if (hour.battery_action !== null) {
    parts.push(translate(language, `house.readout.action.${hour.battery_action}`));
  }
  return parts.join(" · ");
}

/** The money line: the reviewed days side by side, else what the plan expects, else why there is no plan. */
export function savingLine(language: Language, answer: HouseAnswer): string | null {
  const battery = answer.battery;
  if (battery === null) {
    return null;
  }
  const review = battery.review;
  if (review !== null) {
    return translate(language, `house.saving.week.${pluralForm(language, review.days)}`, {
      days: String(review.days),
      plan: amount(language, review.plan_saving, answer.money),
      own: amount(language, review.own_saving, answer.money),
    });
  }
  const planned = (battery.modes as Partial<Record<string, { planned_saving: number | null }>>)[battery.mode]?.planned_saving ?? null;
  if (battery.status === "planned" && planned !== null && battery.horizon_end !== null) {
    const ctx = context(language, answer);
    const end = Date.parse(battery.horizon_end);
    return translate(language, "house.saving.ahead", {
      amount: amount(language, planned, answer.money),
      until: `${dayCue(ctx, end - 1)} ${clock(ctx, end)}`,
    });
  }
  return translate(language, battery.status === "waiting" ? "house.saving.waiting" : "house.saving.noPlan");
}

/** The review line: the last week's mean error of each forecast, or that the first review is tonight. */
export function reviewLine(language: Language, answer: HouseAnswer): string {
  const house = answer.house.review;
  if (house === null) {
    return translate(language, "house.review.learning");
  }
  const parts = [translate(language, "house.review.house", { error: plusMinus(language, house.mae, "kW") })];
  const sun = answer.sun;
  if (sun !== null && sun.review !== null) {
    parts.push(
      sun.review_corrected === null
        ? translate(language, "house.review.sun", { error: plusMinus(language, sun.review.mae, "kWh") })
        : translate(language, "house.review.sunCorrected", {
            error: plusMinus(language, sun.review.mae, "kWh"),
            corrected: plusMinus(language, sun.review_corrected.mae, "kWh"),
          }),
    );
  }
  const soc = answer.battery?.soc_review ?? null;
  if (soc !== null) {
    parts.push(translate(language, "house.review.soc", { error: `±${formatNumber(language, soc.mae, 0)} %` }));
  }
  return `${translate(language, "house.review.head")} ${parts.join(" · ")}`;
}

// ------------------------------------------------------------------------------------------------ parts

function header(doc: Document, language: Language, answer: HouseAnswer | null): HTMLElement {
  const head = element(doc, "div", H.head);
  head.append(element(doc, "h2", H.title, translate(language, "house.title")), element(doc, "span", H.chip, translate(language, "house.preview")));
  const wrap = element(doc, "div");
  wrap.append(head);
  const battery = answer?.battery ?? null;
  if (battery !== null) {
    const words = [];
    if (battery.soc_pct !== null) {
      words.push(translate(language, "house.sub.soc", { pct: pct(language, battery.soc_pct) }));
    }
    if (answer?.today === null) {
      words.push(translate(language, "house.sub.mode", { mode: modeName(language, battery.mode) }));
    }
    wrap.append(element(doc, "p", `${C.muted} ${H.sub}`, words.join(" · ")));
    const today = answer === null ? null : todayLine(language, answer);
    if (today !== null) {
      const line = element(doc, "p", H.today, today);
      line.dataset["line"] = "today";
      wrap.append(line);
    }
  }
  return wrap;
}

function legend(doc: Document, language: Language, answer: HouseAnswer, view: HouseChartView): HTMLElement {
  const list = element(doc, "ul", H.legend);
  list.setAttribute("aria-label", translate(language, "house.legend"));
  const item = (series: string, key: TranslationKey): void => {
    const entry = element(doc, "li", H.legendItem);
    const swatch = element(doc, "span", H.swatch);
    swatch.dataset["series"] = series;
    swatch.setAttribute("aria-hidden", "true");
    entry.append(swatch, element(doc, "span", undefined, translate(language, key)));
    list.append(entry);
  };
  if (view === "price") {
    item("buy", "house.legend.buy");
    item("sellPrice", "house.legend.sellPrice");
  } else {
    item("house", "house.legend.house");
    if (answer.hours.some((hour) => hour.house_kw !== null && hour.house_forecast_kw !== null)) {
      item("forecast", "house.legend.forecast");
    }
    if (answer.sun !== null) {
      item("sun", "house.legend.sun");
    }
  }
  if (answer.battery !== null) {
    item("battery", "house.legend.battery");
    if (answer.hours.some((hour) => hour.battery_action === "grid_charge")) {
      item("charge", "house.legend.charge");
    }
    if (answer.hours.some((hour) => hour.battery_action === "export_discharge")) {
      item("sell", "house.legend.sell");
    }
  }
  return list;
}

function chartBlock(input: HouseRenderInput, answer: HouseAnswer): HTMLElement {
  const { doc, language, ui, handlers, nowMs } = input;
  const ctx = context(language, answer);
  const surface = element(doc, "div", H.surface);
  surface.tabIndex = 0;
  const readout = element(doc, "p", `${C.muted} ${C.readout}`);
  readout.setAttribute("aria-live", "polite");
  const width = Math.max(200, Math.round(input.width));
  const priced = answer.hours.some((hour) => hour.buy !== null);
  const view: HouseChartView = priced ? (ui.view ?? "energy") : "energy";
  const chart: HouseChart = renderHouseChart({
    hours: answer.hours,
    view,
    timeZone: answer.time_zone,
    nowMs,
    width,
    height: houseChartHeight(width),
    labels: {
      kw: (value) => formatNumber(language, value, 1),
      price: (value) => formatNumber(language, value, 0),
      priceUnit: answer.money?.minor_unit ?? "",
      now: translate(language, "house.now"),
      hour: (instantMs) => hourLabel(ctx, instantMs),
      day: (instantMs) => dayCue(ctx, instantMs),
    },
    title: translate(language, view === "price" ? "house.chart.priceLabel" : "house.chart.label"),
    description: translate(language, view === "price" ? "house.chart.priceDescription" : "house.chart.description"),
    ids: { title: `${input.idPrefix}-title`, description: `${input.idPrefix}-desc` },
  });
  surface.style.height = `${chart.scale.height}px`;
  surface.setAttribute("role", "group");
  surface.setAttribute("aria-label", translate(language, "house.chart.label"));
  surface.append(chart.element);
  let selected = ui.selected !== null && ui.selected < answer.hours.length ? ui.selected : null;
  const show = (index: number | null): void => {
    selected = index;
    chart.select(index);
    const hour = index === null ? undefined : answer.hours[index];
    readout.textContent = hour === undefined ? translate(language, "house.hint") : hourFigures(language, answer, hour, nowMs, view);
  };
  const indexAt = (event: PointerEvent | MouseEvent): number | null => {
    const box = chart.element.getBoundingClientRect();
    if (box.width <= 0) {
      return null;
    }
    return chart.hourAt(((event.clientX - box.left) / box.width) * chart.scale.width);
  };
  surface.addEventListener("pointermove", (event) => {
    if (event.pointerType === "mouse") {
      show(indexAt(event));
    }
  });
  surface.addEventListener("pointerleave", () => show(selected === null ? null : selected));
  surface.addEventListener("click", (event) => {
    const index = indexAt(event);
    show(index);
    handlers.onSelect(index);
  });
  surface.addEventListener("keydown", (event) => {
    const last = answer.hours.length - 1;
    const current = answer.hours.findIndex((hour) => hour.startMs <= nowMs && nowMs < hour.startMs + HOUR_MS);
    let next: number | null = null;
    if (event.key === "ArrowRight") {
      next = Math.min(last, (selected ?? Math.max(0, current - 1)) + 1);
    } else if (event.key === "ArrowLeft") {
      next = Math.max(0, (selected ?? current + 1) - 1);
    } else if (event.key === "Home") {
      next = 0;
    } else if (event.key === "End") {
      next = last;
    } else if (event.key === "Escape") {
      show(null);
      handlers.onSelect(null);
      return;
    } else {
      return;
    }
    event.preventDefault();
    show(next);
    handlers.onSelect(next);
  });
  show(selected);
  const block = element(doc, "div");
  if (priced) {
    const views = element(doc, "div", H.views);
    views.setAttribute("role", "group");
    views.setAttribute("aria-label", translate(language, "house.views"));
    for (const [name, key] of [
      ["energy", "house.view.energy"],
      ["price", "house.view.price"],
    ] as const) {
      const button = element(doc, "button", C.historyToggleButton, translate(language, key));
      button.type = "button";
      button.dataset["view"] = name;
      button.setAttribute("aria-pressed", String(view === name));
      button.addEventListener("click", () => handlers.onView?.(name));
      views.append(button);
    }
    block.append(views);
  }
  block.append(surface, legend(doc, language, answer, view), readout);
  return block;
}

function pctOf(language: Language, part: number, whole: number): string {
  return whole > 0 ? `${formatNumber(language, (part / whole) * 100, 0)} %` : "–";
}

function flowBlock(doc: Document, language: Language, flow: DayFlow | null): HTMLElement | null {
  if (flow === null) {
    return null;
  }
  const section = element(doc, "section", `${H.section} ${H.flow}`);
  section.append(element(doc, "h3", H.heading, translate(language, "house.flow.heading")));
  const rows: Array<[TranslationKey, Array<[string, TranslationKey, number | null]>]> = [
    [
      "house.flow.from",
      [
        ["solar", "house.flow.solar", flow.sources.solar],
        ["battery", "house.flow.battery", flow.sources.battery],
        ["grid", "house.flow.grid", flow.sources.grid],
      ],
    ],
    [
      "house.flow.to",
      [
        ["battery", "house.flow.battery", flow.uses.battery],
        ["load", "house.flow.load", flow.uses.load],
        ["grid", "house.flow.grid", flow.uses.grid],
      ],
    ],
  ];
  for (const [label, parts] of rows) {
    const shown = parts.filter((part): part is [string, TranslationKey, number] => part[2] !== null);
    const whole = shown.reduce((sum, part) => sum + part[2], 0);
    const row = element(doc, "div", H.flowRow);
    const bar = element(doc, "div", H.flowBar);
    bar.setAttribute("aria-hidden", "true");
    for (const [name, , value] of shown) {
      if (value <= 0) {
        continue;
      }
      const piece = element(doc, "span", H.flowPart);
      piece.dataset["part"] = name;
      piece.style.flexGrow = String(value);
      bar.append(piece);
    }
    row.append(element(doc, "span", H.flowLabel, translate(language, label)), bar);
    const key = element(
      doc,
      "p",
      H.flowKey,
      shown.map(([, word, value]) => `${translate(language, word)} ${kwh(language, value)} (${pctOf(language, value, whole)})`).join(" · "),
    );
    section.append(row, key);
  }
  if (flow.sources.solar !== null && flow.sun_hours < flow.hours) {
    section.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, "house.flow.sunLater")));
  }
  return section;
}

function calendarBlock(doc: Document, language: Language, answer: HouseAnswer, nowMs: number): HTMLElement | null {
  const calendar = answer.calendar;
  if (calendar === null || calendar.days.length === 0) {
    return null;
  }
  const section = element(doc, "section", H.section);
  const [year, month] = calendar.month.split("-").map(Number) as [number, number];
  const first = new Date(Date.UTC(year, month - 1, 1, 12));
  const monthName = new Intl.DateTimeFormat(language, { month: "long", timeZone: "UTC" }).format(first);
  section.append(element(doc, "h3", H.heading, translate(language, "house.calendar.heading", { month: monthName })));
  const grid = element(doc, "div", H.calendar);
  const weekday = new Intl.DateTimeFormat(language, { weekday: "narrow", timeZone: "UTC" });
  for (let index = 0; index < 7; index += 1) {
    // 2024-01-01 was a Monday.
    const head = element(doc, "span", H.calendarHead, weekday.format(new Date(Date.UTC(2024, 0, 1 + index, 12))));
    head.setAttribute("aria-hidden", "true");
    grid.append(head);
  }
  const offset = (first.getUTCDay() + 6) % 7;
  for (let index = 0; index < offset; index += 1) {
    const blank = element(doc, "span", H.calendarDay);
    blank.dataset["empty"] = "true";
    grid.append(blank);
  }
  const byDate = new Map(calendar.days.map((day) => [day.date, day]));
  const length = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const todayKey = new Intl.DateTimeFormat("en-CA", { timeZone: answer.time_zone }).format(new Date(nowMs));
  const short = (minor: number): string => formatFixed(language, minor / 100, Math.abs(minor) >= 1000 ? 0 : 1);
  let plan = 0;
  let own = 0;
  let arbitrage = 0;
  let arbitrageDays = 0;
  for (let number = 1; number <= length; number += 1) {
    const date = `${calendar.month}-${String(number).padStart(2, "0")}`;
    const cell = element(doc, "span", H.calendarDay);
    cell.dataset["date"] = date;
    if (date === todayKey) {
      cell.dataset["today"] = "true";
    }
    cell.append(element(doc, "span", H.calendarDate, String(number)));
    const day = byDate.get(date);
    if (day !== undefined) {
      plan += day.plan_saving;
      own += day.own_saving;
      cell.dataset["result"] = day.plan_saving >= day.own_saving ? "plan" : "own";
      cell.append(element(doc, "span", H.calendarFigure, short(day.plan_saving)), element(doc, "span", H.calendarDate, short(day.own_saving)));
      const label = translate(language, "house.calendar.day", {
        date: dateLabel(language, date),
        plan: amount(language, day.plan_saving, answer.money),
        own: amount(language, day.own_saving, answer.money),
      });
      cell.setAttribute(
        "aria-label",
        day.arbitrage === null ? label : `${label} · ${translate(language, "house.calendar.arbitrage", { amount: amount(language, day.arbitrage, answer.money) })}`,
      );
      if (day.arbitrage !== null) {
        arbitrage += day.arbitrage;
        arbitrageDays += 1;
      }
      cell.setAttribute("role", "img");
    }
    grid.append(cell);
  }
  section.append(grid);
  section.append(
    element(
      doc,
      "p",
      `${C.muted} ${H.note}`,
      translate(language, "house.calendar.total", {
        plan: amount(language, plan, answer.money),
        own: amount(language, own, answer.money),
        days: String(calendar.days.length),
        unit: answer.money?.major_unit ?? "",
      }),
    ),
  );
  if (arbitrageDays > 0) {
    section.append(
      element(
        doc,
        "p",
        `${C.muted} ${H.note}`,
        translate(language, "house.calendar.arbitrageTotal", { amount: amount(language, arbitrage, answer.money), days: String(arbitrageDays) }),
      ),
    );
  }
  return section;
}

function modesTable(doc: Document, language: Language, answer: HouseAnswer): HTMLElement | null {
  const battery = answer.battery;
  if (battery === null) {
    return null;
  }
  const section = element(doc, "section", H.section);
  section.append(element(doc, "h3", H.heading, translate(language, "house.modes.heading")));
  const table = element(doc, "table", H.table);
  const head = element(doc, "thead");
  const headRow = element(doc, "tr");
  const reviewedDays = Math.max(0, ...MODES.map((mode) => battery.modes[mode]?.reviewed_days ?? 0));
  for (const key of ["house.modes.mode", "house.modes.ahead", "house.modes.reviewed"] as const) {
    headRow.append(element(doc, "th", undefined, translate(language, key, { days: String(reviewedDays) })));
  }
  head.append(headRow);
  const body = element(doc, "tbody");
  for (const mode of MODES) {
    const figures = battery.modes[mode];
    if (figures === undefined) {
      continue;
    }
    const row = element(doc, "tr");
    if (mode === battery.mode) {
      row.dataset["inForce"] = "true";
    }
    const name = mode === battery.mode ? translate(language, "house.modes.inForce", { mode: modeName(language, mode) }) : modeName(language, mode);
    row.append(
      element(doc, "td", undefined, name),
      element(doc, "td", undefined, figures.planned_saving === null ? "–" : amount(language, figures.planned_saving, answer.money)),
      element(doc, "td", undefined, figures.reviewed_saving === null ? "–" : amount(language, figures.reviewed_saving, answer.money)),
    );
    body.append(row);
  }
  if (battery.review !== null) {
    const own = element(doc, "tr");
    own.append(
      element(doc, "td", undefined, translate(language, battery.management === "self_managed" ? "house.modes.ownOptimiser" : "house.modes.own")),
      element(doc, "td", undefined, "–"),
      element(doc, "td", undefined, amount(language, battery.review.own_saving, answer.money)),
    );
    body.append(own);
  }
  table.append(head, body);
  section.append(table, element(doc, "p", `${C.muted} ${H.note}`, translate(language, "house.modes.note")));
  return section;
}

function daysTable(doc: Document, language: Language, answer: HouseAnswer): HTMLElement | null {
  if (answer.days.length === 0) {
    return null;
  }
  const section = element(doc, "section", H.section);
  section.append(element(doc, "h3", H.heading, translate(language, "house.days.heading")));
  const table = element(doc, "table", H.table);
  const columns: Array<{ key: TranslationKey; cell: (day: HouseAnswer["days"][number]) => string }> = [
    { key: "house.days.house", cell: (day) => (day.house_mae === null ? "–" : formatFixed(language, day.house_mae, 2)) },
  ];
  if (answer.sun !== null) {
    columns.push(
      { key: "house.days.sun", cell: (day) => (day.sun_mae === null ? "–" : formatFixed(language, day.sun_mae, 2)) },
      { key: "house.days.corrected", cell: (day) => (day.sun_corrected_mae === null ? "–" : formatFixed(language, day.sun_corrected_mae, 2)) },
    );
  }
  if (answer.battery !== null) {
    columns.push(
      { key: "house.days.plan", cell: (day) => (day.plan_saving === null ? "–" : amount(language, day.plan_saving, answer.money)) },
      { key: "house.days.own", cell: (day) => (day.own_saving === null ? "–" : amount(language, day.own_saving, answer.money)) },
    );
  }
  const head = element(doc, "thead");
  const headRow = element(doc, "tr");
  headRow.append(element(doc, "th", undefined, translate(language, "house.days.date")));
  columns.forEach((column) => headRow.append(element(doc, "th", undefined, translate(language, column.key))));
  head.append(headRow);
  const body = element(doc, "tbody");
  for (const day of answer.days) {
    const row = element(doc, "tr");
    row.append(element(doc, "td", undefined, dateLabel(language, day.date)));
    columns.forEach((column) => row.append(element(doc, "td", undefined, column.cell(day))));
    body.append(row);
  }
  table.append(head, body);
  section.append(table, element(doc, "p", `${C.muted} ${H.note}`, translate(language, answer.sun === null ? "house.days.noteHouse" : "house.days.note")));
  return section;
}

const ESTIMATE_KEYS: Record<string, TranslationKey> = {
  min_soc_assumed: "house.estimate.min_soc_assumed",
  round_trip_assumed: "house.estimate.round_trip_assumed",
  solar_in_house_history: "house.estimate.solar_in_house_history",
  battery_mode_unknown: "house.estimate.battery_mode_unknown",
  wear_currency_unknown: "house.estimate.wear_currency_unknown",
  no_solar_forecast: "house.estimate.no_solar_forecast",
  horizon_cut_by_forecast: "house.estimate.horizon_cut_by_forecast",
};

function sourcesBlock(doc: Document, language: Language, answer: HouseAnswer, words: EntityWords | undefined): HTMLElement {
  const section = element(doc, "section", H.section);
  section.append(element(doc, "h3", H.heading, translate(language, "house.sources.heading")));
  const list = element(doc, "ul", `${H.facts} ${C.muted}`);
  const add = (text: string): void => {
    list.append(element(doc, "li", undefined, text));
  };
  const sources = answer.sources;
  if (sources.weather_entity === null) {
    add(translate(language, "house.sources.weatherNone"));
  } else {
    const name = entityName(words, sources.weather_entity);
    add(translate(language, sources.weather === "ok" ? "house.sources.weather" : "house.sources.weatherNoForecast", { name }));
  }
  if (sources.outdoor.source === "sensor" && sources.outdoor.sensor !== null) {
    add(translate(language, "house.sources.outdoorSensor", { name: entityName(words, sources.outdoor.sensor) }));
  } else {
    add(translate(language, sources.outdoor.source === "weather" ? "house.sources.outdoorWeather" : "house.sources.outdoorNone"));
  }
  add(
    sources.holiday_calendar === null
      ? translate(language, "house.sources.holidaysNone")
      : translate(language, "house.sources.holidays", { name: entityName(words, sources.holiday_calendar) }),
  );
  const temperature = answer.house.temperature;
  if (temperature !== null) {
    add(
      temperature.active
        ? translate(language, "house.sources.temperatureOn")
        : translate(language, "house.sources.temperatureOff", {
            reason: translate(language, temperature.reason === "too_few_hours" ? "house.temperature.tooFew" : "house.temperature.tooLittle"),
          }),
    );
  }
  if (sources.solar_forecasts.length > 0) {
    add(translate(language, "house.sources.solar", { names: sources.solar_forecasts.join(", ") }));
  }
  const battery = answer.battery;
  if (battery !== null) {
    const name = battery.label ?? battery.brand ?? "";
    const size = battery.usable_kwh === null ? "" : ` · ${kwh(language, battery.usable_kwh)}`;
    add(translate(language, "house.sources.battery", { name: `${name}${size}` }));
    const today = answer.today;
    if (today !== null && today.stored_kwh !== null && today.average_cost !== null) {
      add(
        translate(language, today.estimated ? "house.sources.storedEstimated" : "house.sources.stored", {
          kwh: kwh(language, today.stored_kwh),
          cost: `${formatNumber(language, today.average_cost, 0)} ${priceUnit(answer)}`,
        }),
      );
    }
  }
  section.append(list);
  const notes: string[] = [];
  if (battery !== null) {
    notes.push(translate(language, battery.management === "self_managed" ? "house.note.selfManaged" : "house.note.shadow"));
    const assumed = battery.estimates.map((code) => ESTIMATE_KEYS[code]).filter((key): key is TranslationKey => key !== undefined);
    if (assumed.length > 0) {
      notes.push(translate(language, "house.note.assumed", { list: assumed.map((key) => translate(language, key)).join(", ") }));
    }
  }
  if (answer.house.counts_battery) {
    notes.push(translate(language, "house.note.countsBattery"));
  }
  for (const note of notes) {
    section.append(element(doc, "p", `${C.muted} ${H.note}`, note));
  }
  return section;
}

// ------------------------------------------------------------------------------------------------ body

/** The card's whole content for one state. */
export function houseBody(input: HouseRenderInput): HTMLElement {
  const { doc, language, state } = input;
  const card = element(doc, "div", `${C.card} ${H.card}`);
  card.dataset["state"] = state.kind;
  if (state.kind !== "ready") {
    card.append(header(doc, language, null));
    const key: TranslationKey =
      state.kind === "unconfigured" ? "house.unconfigured" : state.kind === "loading" ? "house.loading" : state.sentenceKey;
    const line = element(doc, "p", C.muted, translate(language, key));
    line.setAttribute("role", "status");
    if (state.kind === "failed" && state.code !== null) {
      line.dataset["code"] = state.code;
    }
    if (state.kind === "loading") {
      line.setAttribute("aria-busy", "true");
    }
    card.append(line);
    return card;
  }
  const answer = state.answer;
  card.append(header(doc, language, answer), chartBlock(input, answer));
  const saving = savingLine(language, answer);
  if (saving !== null) {
    const line = element(doc, "p", H.line, saving);
    line.dataset["line"] = "saving";
    card.append(line);
  }
  const review = element(doc, "p", `${H.line} ${C.muted}`, reviewLine(language, answer));
  review.dataset["line"] = "review";
  card.append(review);
  const toggle = element(doc, "button", `${C.historyToggleButton} ${H.detailsToggle}`, translate(language, input.ui.detailsOpen ? "house.details.close" : "house.details.open"));
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(input.ui.detailsOpen));
  const detailsId = `${input.idPrefix}-details`;
  toggle.setAttribute("aria-controls", detailsId);
  toggle.addEventListener("click", () => input.handlers.onDetails(!input.ui.detailsOpen));
  card.append(toggle);
  if (input.ui.detailsOpen) {
    const details = element(doc, "div", H.details);
    details.id = detailsId;
    for (const part of [
      flowBlock(doc, language, answer.flow),
      calendarBlock(doc, language, answer, input.nowMs),
      modesTable(doc, language, answer),
      daysTable(doc, language, answer),
      sourcesBlock(doc, language, answer, input.entityWords),
    ]) {
      if (part !== null) {
        details.append(part);
      }
    }
    card.append(details);
  }
  return card;
}
