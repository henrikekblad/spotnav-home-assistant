// The house card's body: a header and the tabs (Now, Energy flow, Energy chart, Earnings, Price and battery), each
// with its picture and its lines of money, and the sources with what was assumed for the foot of the history dialog
// (`house-history.ts`, which holds the analysis of a period). Built fresh for each state with `textContent` only;
// every figure is the backend's.

import { attachScrub } from "./chart-scrub";
import { clock, dateLabel, dayCue, formatFixed, formatNumber, hourLabel, type EntityWords, type FormatContext } from "./format";
import { heatShown, houseChartHeight, houseShown, renderHouseChart, sunShown, type HouseChart, type HouseChartView } from "./house-chart";
import { MODES, type BatteryMode, type HouseAnswer, type HouseHour } from "./house-model";
import { flowHeight, renderFlow, type LoadsKind, type PillMeasure } from "./house-flow";
import type { BatteryFlow, HouseHistory } from "./house-history";
import { planSentence, renderSchematic, schematicHeight, type HouseNow } from "./house-now";
import { energyChart, flowWords, kwhText, tilesBlock, PERIOD_VIEW_CLASSES } from "./house-period-view";
import { earnBars, earnLegend, earnNotes, earnParts, earnTotal } from "./house-earnings";
import { dayOnly, periodPicker, tabList, tabPanel, type HouseTab, type PeriodChoice, type TabFit } from "./house-tabs";
import type { HistoryPeriod } from "./period-history";
import { hiddenIn, type HiddenSeries } from "./house-series";
import { cardHeader, headerButton, headerTitle } from "./card-header";
import { statusLine, testStatus } from "./heat-view";
import { HOUSE_CLASSES as H } from "./house-styles";
import { daysLeft, translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

const HOUR_MS = 3_600_000;
/** The Energy flow tab's diagram at most (px) outside a box; its blocks' words still fit (`house-flow.ts`). */
const TAB_FLOW_PX = 320;

export type HouseState =
  | { kind: "unconfigured" }
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null }
  | { kind: "ready"; answer: HouseAnswer };

/** A read of the tabs' own data: on its way, failed, or in. */
export type Loadable<A> = { kind: "loading" } | { kind: "failed"; code: string | null } | { kind: "ready"; answer: A };

/** What the tabs show besides the card's own answer. */
export interface HouseTabsData {
  /** The live flows (`spotnav/get_house_now`); `null` before the first or from a backend without them. */
  live: HouseNow | null;
  /** The period shown in the period tabs (`spotnav/get_house_history`), where the tab needs it; else `null`. */
  history: Loadable<HouseHistory> | null;
  /** A past day's own answer (`spotnav/get_house` with its date) for its hours; else `null`. */
  day: Loadable<HouseAnswer> | null;
  /** The site's local date. */
  today: string;
}

export interface HouseUi {
  /** The tab shown (default the first). */
  tab?: HouseTab;
  /** The tab the card's YAML locks: the row is not drawn. */
  locked?: HouseTab | null;
  /** How the tab row was last worded (`house-tabs.ts`: long names, short ones or icons), so a redraw starts there. */
  tabFit?: TabFit;
  /** The period the period tabs show (default today). */
  choice?: PeriodChoice;
  /** In a box of fixed height: the height of the shown tab's main picture (its spare room given to it). */
  fill?: number | null;
  /** The bar chosen in the period tabs' bars. */
  bar?: string | null;
  /** The series hidden from the chart's legend, per view (absent: each view's defaults, `house-series.ts`). */
  hidden?: HiddenSeries;
  /** The Earnings tab's parts the viewer hid (`house-earnings.ts`); none by default. */
  earnHidden?: readonly string[];
  /** The chart's view: the energy (default) or the price with the battery. */
  view?: HouseChartView;
  /** The hour shown in the readout (an index into `hours`), `null` for the hint. */
  selected: number | null;
  /** The header's test chip opened to its explanation. */
  testOpen?: boolean;
}

export interface HouseHandlers {
  onView?(view: HouseChartView): void;
  /** The viewer hid or showed a series: the view's whole hidden list after the change. */
  onLegend?(view: HouseChartView, hidden: string[]): void;
  onSelect(index: number | null): void;
  /** The "i" in the header: the details, the history dialog (`history-dialog.ts`). Without it the card has none. */
  onHistory?(opener: HTMLElement): void;
  /** An administrator's settings gear: the shared settings popover. Without it the card has no gear. */
  onSettings?(): void;
  /** The test chip tapped: its explanation shown or hidden. Without it the chip opens and closes in place. */
  onTest?(open: boolean): void;
  /** House in the energy flow tapped: what the house used (`house-loads.ts`) over the period the flow shows (`null`:
   * today). Without it the block is no button. */
  onHouseLoads?(opener: HTMLElement, kind: LoadsKind, period?: { period: HistoryPeriod; date: string; running: boolean } | null): void;
  /** Another tab chosen (`byKey`: by the arrow keys, Home or End: the card focuses it again). */
  onTab?(tab: HouseTab, byKey: boolean): void;
  /** Another period chosen in the period tabs. */
  onPeriod?(choice: PeriodChoice): void;
  /** A bar chosen in the period tabs' bars (kept over a redraw). */
  onBar?(key: string | null): void;
  /** The viewer hid or showed a part of the Earnings tab: the whole hidden list after the change. */
  onEarnPart?(hidden: string[]): void;
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
  /** The tabs' own data; without it the period tabs say they are loading and Now has no live figures. */
  tabs?: HouseTabsData;
  /** The energy flow's pill words measured as drawn (`house-flow.ts`); without it they are estimated. */
  measure?: PillMeasure;
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
  // Whole units from 10 (and nothing at all), one decimal below: "25 kr", "4,0 kr", "0,8 kr", "0 kr".
  return `${formatFixed(language, major, Math.abs(major) >= 10 || major === 0 ? 0 : 1)} ${money.major_unit}`;
}

/** The price's unit per kWh, the area's minor unit ("öre/kWh"). */
function priceUnit(answer: HouseAnswer): string {
  return `${answer.money?.minor_unit ?? ""}/kWh`.replace(/^\//, "");
}

/** Today's headline, the figure the battery's own app shows (Sigenergy's, verified): what each kWh the battery gave back earned over its
 * stored cost (the backend's `arbitrage`; "Batteriet har tjänat 26 kr i dag"). */
export function todayLine(language: Language, answer: HouseAnswer): string | null {
  const today = answer.today;
  if (today === null) {
    return null;
  }
  const past = answer.day !== null && !answer.day.today;
  // A day the battery ran under its own control (SpotNav off or in test, Sigenergy's AI): its earnings are its own.
  const own = ownControl(answer);
  return translate(language, past ? (own ? "house.dayMoneyOwn" : "house.dayMoney") : own ? "house.todayOwn" : "house.today", {
    day: past ? dateLabel(language, answer.day!.date) : "",
    amount: amount(language, today.arbitrage, answer.money),
  });
}

/** Whether the shown day's battery ran under its own control all day (the backend's `saving.battery_share` 0). */
export function ownControl(answer: HouseAnswer): boolean {
  return answer.saving !== null && answer.saving.battery_share === 0;
}

/** Money in the major unit without the unit, for the parts after the total ("35", "4,0"). */
function bareAmount(language: Language, minor: number): string {
  const major = minor / 100;
  return formatFixed(language, major, Math.abs(major) >= 10 || major === 0 ? 0 : 1);
}

const SAVING_PARTS = ["battery", "car", "heat"] as const;

/** What SpotNav saved on the shown day, with its parts when there is more than one ("SpotNav sparade 54 kr i dag ·
 * batteri 35 · bil 17 · värme 2"); a part the day does not have is left out, never shown as 0. `null` without any. */
export function savingTotalLine(language: Language, answer: HouseAnswer): string | null {
  const saving = answer.saving;
  if (saving === null || saving.total === null) {
    return null;
  }
  const past = answer.day !== null && !answer.day.today;
  const head = translate(language, past ? "house.saving.totalDay" : "house.saving.total", {
    day: past ? dateLabel(language, answer.day!.date) : "",
    amount: amount(language, saving.total, answer.money),
  });
  const parts = SAVING_PARTS.filter((part) => saving[part] !== null);
  if (parts.length < 2) {
    return head;
  }
  return [head, ...parts.map((part) => translate(language, `house.saving.part.${part}`, { amount: bareAmount(language, saving[part]!) }))].join(" · ");
}


/** Under the headline: what the electricity bought cost, and the sun sold when it came to at least half a unit
 * ("Elkostnad 63 kr · sålt solel 4 kr"). */
export function todayDetail(language: Language, answer: HouseAnswer): string | null {
  const today = answer.today;
  if (today === null) {
    return null;
  }
  const parts = [translate(language, "house.todayCost", { amount: amount(language, today.bought, answer.money) })];
  if (Math.abs(today.sold) >= 50) {
    parts.push(translate(language, "house.todaySold", { amount: amount(language, today.sold, answer.money) }));
  }
  return parts.join(" · ");
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
export function hourFigures(
  language: Language,
  answer: HouseAnswer,
  hour: HouseHour,
  nowMs: number,
  view: HouseChartView = "energy",
  hidden: ReadonlySet<string> = hiddenIn(undefined, view),
): string {
  const ctx = context(language, answer);
  const parts = [`${clock(ctx, hour.startMs)}–${clock(ctx, hour.startMs + HOUR_MS)}`];
  const house = houseShown(hour);
  const visible = (series: string): boolean => !hidden.has(series);
  if (view === "price" && hour.buy !== null && hour.sell !== null) {
    const unit = priceUnit(answer);
    const buy = `${formatNumber(language, hour.buy, 0)} ${unit}`;
    const sell = `${formatNumber(language, hour.sell, 0)} ${unit}`;
    if (visible("buy") && visible("sellPrice")) {
      parts.push(translate(language, "house.readout.price", { buy, sell }));
    } else if (visible("buy")) {
      parts.push(translate(language, "house.readout.priceBuy", { buy }));
    } else if (visible("sellPrice")) {
      parts.push(translate(language, "house.readout.priceSell", { sell }));
    }
  }
  if (view === "energy" && house.kw !== null) {
    const forecast = hour.house_forecast_kw !== null && visible("forecast");
    if (visible("house") && house.measured && forecast) {
      parts.push(translate(language, "house.readout.houseBoth", { kw: kw(language, house.kw), forecast: kw(language, hour.house_forecast_kw as number) }));
    } else if (visible("house") && (!house.measured || !forecast)) {
      parts.push(translate(language, house.measured ? "house.readout.house" : "house.readout.houseForecast", { kw: kw(language, house.kw) }));
    } else if (house.measured && forecast) {
      parts.push(translate(language, "house.readout.houseForecast", { kw: kw(language, hour.house_forecast_kw as number) }));
    }
  }
  const heat = heatShown(hour);
  if (view === "energy" && heat !== null && visible("heat")) {
    parts.push(translate(language, house.measured ? "house.readout.heat" : "house.readout.heatForecast", { kw: kw(language, heat) }));
  }
  if (view === "energy" && hour.car_kw !== null && hour.car_kw > 0 && visible("car")) {
    parts.push(translate(language, "house.readout.car", { kw: kw(language, hour.car_kw) }));
  }
  const sun = sunShown(hour, nowMs);
  const past = hour.startMs + HOUR_MS <= nowMs;
  if (view === "energy" && visible("sun") && sun !== null && (sun > 0 || (hour.sun_forecast_kwh ?? 0) > 0)) {
    parts.push(translate(language, past ? "house.readout.sun" : "house.readout.sunForecast", { kwh: kwh(language, sun) }));
  }
  if (visible("battery")) {
    if (hour.soc_pct !== null) {
      parts.push(translate(language, "house.readout.soc", { pct: pct(language, hour.soc_pct) }));
    } else if (!past && hour.soc_plan_pct !== null) {
      parts.push(translate(language, "house.readout.socPlan", { pct: pct(language, hour.soc_plan_pct) }));
    }
  }
  const tint = hour.battery_action === "grid_charge" ? "charge" : hour.battery_action === "export_discharge" ? "sell" : null;
  if (hour.battery_action !== null && visible("battery") && (view === "energy" || tint === null || visible(tint))) {
    parts.push(translate(language, `house.readout.action.${hour.battery_action}`));
  }
  return parts.join(" · ");
}

/** The heat pump's line: the shown day's heat beside the house's whole load over the same hours; with more than one
 * meter that could be it, where to choose; `null` without a meter, or one found with no hour measured yet. */
export function heatLine(language: Language, answer: HouseAnswer, words?: EntityWords): string | null {
  const heat = answer.heat;
  if (heat === null) {
    return null;
  }
  if (heat.status === "ambiguous") {
    return translate(language, "house.heat.ambiguous", { names: heat.candidates.map((id) => entityName(words, id)).join(", ") });
  }
  if (heat.status === "unusable") {
    return translate(language, "house.heat.unusable");
  }
  const day = heat.day;
  if (heat.status !== "found" || day === null) {
    return null;
  }
  const past = answer.day !== null && !answer.day.today;
  return translate(language, past ? "house.heat.dayPast" : "house.heat.day", {
    heat: kwh(language, day.heat_kwh),
    house: kwh(language, day.house_kwh),
    share: pctOf(language, day.heat_kwh, day.house_kwh),
  });
}

/** The status lines of the learning periods that hold something back now, one each ("Nätladdning högst 1,4 kW medan
 * husets toppar lärs in (9 dygn kvar)."; with no grid charge at all "Lär sig husets toppar, 2 dygn kvar. Laddar inte
 * från nätet förrän dess."); none when nothing is held back. */
export function learningLines(language: Language, answer: HouseAnswer): string[] {
  const lines: string[] = [];
  for (const hold of answer.learning) {
    if (hold.what !== "phase_peaks") {
      continue;
    }
    const left = daysLeft(language, hold.days_left);
    lines.push(
      hold.cap_kw > 0
        ? translate(language, "house.learning.cappedWhile", { kw: formatNumber(language, hold.cap_kw, 1), left })
        : `${translate(language, "house.learning.peaksLine", { left })} ${translate(language, "house.learning.noGridUntil")}`,
    );
  }
  return lines;
}

// ------------------------------------------------------------------------------------------------ parts

/** Measured battery power below this (kW, either way) is the battery at rest. */
const REST_KW = 0.1;

/**
 * What the battery does now, as the house card's header says it after the name: while SpotNav controls it, the action
 * its control runs ("Laddar från nätet", "Håller", "Säljer till nätet", "Laddar ur"), else (the battery's own
 * self-consumption) what the measured power says ("Laddar med sol", "Laddar ur", "Vilar"); "Egen styrning" while the
 * battery's own optimiser runs it; `test` while SpotNav controls nothing yet; `null` without a battery or (controlling)
 * without a reading.
 */
export function batteryNow(language: Language, answer: HouseAnswer): { code: string; text: string } | "test" | null {
  const battery = answer.battery;
  if (battery === null) {
    return null;
  }
  if (battery.control !== "on") {
    return battery.management === "self_managed" ? { code: "own", text: translate(language, "house.now.own") } : "test";
  }
  const action = battery.now?.action ?? null;
  const said: Record<string, TranslationKey> = {
    grid_charge: "house.legend.charge",
    hold: "house.now.hold",
    export_discharge: "house.legend.sell",
    discharge_house: "house.now.discharge",
  };
  if (action !== null && action in said) {
    return { code: action, text: translate(language, said[action]!) };
  }
  const power = battery.now?.power_kw ?? null;
  if (power === null) {
    return null;
  }
  if (power >= REST_KW) {
    return { code: "sun", text: translate(language, "house.now.sun") };
  }
  if (power <= -REST_KW) {
    return { code: "discharge", text: translate(language, "house.now.discharge") };
  }
  return { code: "idle", text: translate(language, "house.now.idle") };
}

/** The header (`card-header.ts`), "Huset · Laddar från nätet": the only thing above the tabs (the day's money is in
 * Now, each period's in Price and battery). */
function header(input: HouseRenderInput, answer: HouseAnswer | null): HTMLElement {
  const { doc, language, handlers } = input;
  const onSettings = handlers.onSettings?.bind(handlers);
  const onHistory = handlers.onHistory?.bind(handlers);
  const now = answer === null ? null : batteryNow(language, answer);
  const test = now === "test" ? testStatus(doc, language, "house.test.explain", input.ui.testOpen === true, `${input.idPrefix}-test`, handlers.onTest?.bind(handlers)) : null;
  const status = test !== null ? test.status : now !== null && now !== "test" ? statusLine(doc, now.text, now.code) : null;
  const buttons: HTMLButtonElement[] = [];
  if (onHistory !== undefined) {
    const button = headerButton(doc, "info", translate(language, "house.details.open"));
    button.addEventListener("click", () => onHistory(button));
    buttons.push(button);
  }
  if (onSettings !== undefined) {
    const button = headerButton(doc, "settings", translate(language, "popover.open"));
    button.dataset["action"] = "settings";
    button.addEventListener("click", () => onSettings());
    buttons.push(button);
  }
  const { header: head } = cardHeader(doc, {
    idPrefix: input.idPrefix,
    productName: translate(language, "card.title"),
    title: headerTitle(doc, translate(language, "house.title"), status).block,
    buttons,
  });
  const wrap = element(doc, "div");
  wrap.append(head);
  if (test !== null) {
    wrap.append(test.explain);
  }
  return wrap;
}

/** The legend: one button per series the chart has (pressed: shown), each hides or shows its series. */
function legend(doc: Document, language: Language, answer: HouseAnswer, view: HouseChartView, hidden: ReadonlySet<string>, handlers: HouseHandlers): HTMLElement {
  const list = element(doc, "ul", H.legend);
  list.setAttribute("aria-label", translate(language, "house.legend"));
  const item = (series: string, key: TranslationKey): void => {
    const entry = element(doc, "li", H.legendItem);
    const button = element(doc, "button", H.legendButton);
    button.type = "button";
    button.dataset["legend"] = series;
    button.setAttribute("aria-pressed", String(!hidden.has(series)));
    const swatch = element(doc, "span", H.swatch);
    swatch.dataset["series"] = series;
    swatch.setAttribute("aria-hidden", "true");
    button.append(swatch, element(doc, "span", undefined, translate(language, key)));
    button.addEventListener("click", () => {
      const next = new Set(hidden);
      if (!next.delete(series)) {
        next.add(series);
      }
      handlers.onLegend?.(view, [...next]);
    });
    entry.append(button);
    list.append(entry);
  };
  if (view === "price") {
    item("buy", "house.legend.buy");
    item("sellPrice", "house.legend.sellPrice");
  } else {
    item("house", "house.legend.house");
    if (answer.hours.some((hour) => heatShown(hour) !== null)) {
      item("heat", "house.legend.heat");
    }
    if (answer.hours.some((hour) => hour.house_kw !== null && hour.house_forecast_kw !== null)) {
      item("forecast", "house.legend.forecast");
    }
    if (answer.sun !== null) {
      // The area is the measured sun, then SpotNav's corrected forecast; the dotted line ahead is the service's own.
      item("sun", answer.hours.some((hour) => hour.sun_forecast_kwh !== null) ? "house.legend.sunDay" : "house.legend.sun");
    }
    if (answer.hours.some((hour) => hour.car_kw !== null && hour.car_kw > 0)) {
      item("car", "house.legend.car");
    }
  }
  if (answer.battery !== null && view === "price") {
    item("battery", "house.legend.battery");
    if ( answer.hours.some((hour) => hour.battery_action === "grid_charge")) {
      item("charge", "house.legend.charge");
    }
    if (answer.hours.some((hour) => hour.battery_action === "export_discharge")) {
      item("sell", "house.legend.sell");
    }
  }
  return list;
}

/** On a day's price chart, said in the readout of a chosen hour: what the battery earned that hour (minor unit, `null`
 * unknown). Nothing is drawn for it: the Earnings tab shows the same figures. */
interface HourEarnings {
  values: Array<number | null>;
  /** What each hour's figure is made of, where kept (`HouseBucket.battery_flow`); same length as `values`. */
  flows?: Array<BatteryFlow | null>;
}

function chartBlock(
  input: HouseRenderInput,
  answer: HouseAnswer,
  forced: HouseChartView | null = null,
  fitHeight: number | null = null,
  earned: HourEarnings | null = null,
): HTMLElement {
  const { doc, language, ui, handlers, nowMs } = input;
  const ctx = context(language, answer);
  const surface = element(doc, "div", H.surface);
  surface.tabIndex = 0;
  const readout = element(doc, "p", `${C.muted} ${C.readout} ${H.readout}`);
  readout.setAttribute("aria-live", "polite");
  const width = Math.max(200, Math.round(input.width));
  const priced = answer.hours.some((hour) => hour.buy !== null);
  const view: HouseChartView = forced ?? (priced ? (ui.view ?? "energy") : "energy");
  const hidden = hiddenIn(ui.hidden, view);
  const chart: HouseChart = renderHouseChart({
    hours: answer.hours,
    view,
    timeZone: answer.time_zone,
    nowMs,
    width,
    height: fitHeight ?? houseChartHeight(width),
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
    hidden,
  });
  surface.setAttribute("role", "group");
  surface.setAttribute("aria-label", translate(language, "house.chart.label"));
  surface.append(chart.element);
  surface.style.height = `${chart.scale.height}px`;
  // Today's chart has the forecast ahead (lighter bars); a past day's has none to point out.
  const shownToday = answer.day === null || answer.day.today;
  // The hour a click, a tap, a drag or a key chose; a mouse hover or a drag under way only shows another one.
  let selected = ui.selected !== null && ui.selected < answer.hours.length ? ui.selected : null;
  const show = (index: number | null): void => {
    chart.select(index);
    const hour = index === null ? undefined : answer.hours[index];
    // In a box of fixed height (a wall screen) no hint: the line stays empty until an hour is chosen.
    const hint = fitHeight !== null ? "" : translate(language, view === "energy" && shownToday ? "house.hint.energy" : "house.hint");
    let words = hour === undefined ? hint : hourFigures(language, answer, hour, nowMs, view, hidden);
    const value = index === null || earned === null ? null : (earned.values[index] ?? null);
    const flow = index === null || earned?.flows === undefined || earned.flows[index] === undefined || earned.flows[index] === null ? null : flowWords(language, earned.flows[index]!, (minor) => amount(language, minor, answer.money));
    if (flow !== null) {
      words += ` · ${flow}`;
    } else if (value !== null) {
      words += ` · ${translate(language, value < 0 ? "house.value.hourCost" : "house.value.hour", { amount: amount(language, Math.abs(value), answer.money) })}`;
    }
    readout.textContent = words;
  };
  const indexAt = (event: PointerEvent | MouseEvent): number | null => {
    const box = chart.element.getBoundingClientRect();
    if (box.width <= 0) {
      return null;
    }
    return chart.hourAt(((event.clientX - box.left) / box.width) * chart.scale.width);
  };
  const choose = (index: number | null): void => {
    selected = index;
    show(index);
    handlers.onSelect(index);
  };
  // By the pointer's x alone, anywhere in the chart's height; a horizontal touch drag scrubs and keeps the
  // hour it ends on, a vertical one scrolls the page (`touch-action: pan-y`).
  let scrubbed: number | null = null;
  for (const target of [surface]) {
    attachScrub(target, {
      hover: (event) => show(indexAt(event) ?? selected),
      leave: () => show(selected),
      drag: (event) => {
        scrubbed = indexAt(event) ?? scrubbed;
        show(scrubbed);
      },
      end: () => {
        if (scrubbed !== null) {
          choose(scrubbed);
        }
        scrubbed = null;
      },
    });
    target.addEventListener("click", (event) => choose(indexAt(event)));
  }
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
      choose(null);
      return;
    } else {
      return;
    }
    event.preventDefault();
    choose(next);
  });
  show(selected);
  const block = element(doc, "div");
  if (priced && forced === null) {
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
  // The readout right under the chart, where the eye is when a finger or the pointer is on it; its lines are reserved
  // so nothing below moves when an hour is chosen.
  block.append(surface);
  block.append(readout, legend(doc, language, answer, view, hidden, handlers));
  return block;
}

function pctOf(language: Language, part: number, whole: number): string {
  return whole > 0 ? `${formatNumber(language, (part / whole) * 100, 0)} %` : "–";
}

/** The day's energy flow as a Sankey diagram (`house-flow.ts`), under the chart; `null` without one. Today before
 * anything can be drawn (its first minutes), a short line says it is on its way. */
function energyFlowBlock(input: HouseRenderInput, answer: HouseAnswer): HTMLElement | null {
  const flow = answer.energy_flow;
  if (flow === null) {
    return null;
  }
  const { doc, language } = input;
  const past = answer.day !== null && !answer.day.today;
  const onHouse = input.handlers.onHouseLoads?.bind(input.handlers) ?? null;
  const diagram = renderFlow(doc, language, flow, Math.max(260, Math.floor(input.width)), input.idPrefix, past, onHouse);
  if (diagram === null && past) {
    return null;
  }
  const section = element(doc, "section", `${H.section} ${H.flow}`);
  section.dataset["part"] = "energy-flow";
  section.append(
    element(doc, "h3", H.heading, past ? translate(language, "house.flow.headingDay", { day: dateLabel(language, flow.date) }) : translate(language, "house.flow.heading")),
  );
  if (diagram === null) {
    section.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, "house.flow.waiting")));
    return section;
  }
  if (!past) {
    const through = clock(context(language, answer), Date.parse(flow.through));
    section.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, "house.flow.through", { time: through })));
  }
  section.append(diagram);
  if (onHouse !== null && diagram.querySelector(".spotnav-house-flow-action") !== null) {
    const heat = diagram.querySelector(".spotnav-house-flow-action[data-role='heat']") !== null;
    section.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, heat ? "house.loads.tapHeat" : "house.loads.tap")));
  }
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
  capacity_from_ah: "house.estimate.capacity_from_ah",
  rated_from_current: "house.estimate.rated_from_current",
  capacity_learned: "house.estimate.capacity_learned",
  rated_learned: "house.estimate.rated_learned",
};

/** Where the house's figures come from and what was assumed, at the foot of the history dialog. */
export function houseSources(doc: Document, language: Language, answer: HouseAnswer, words: EntityWords | undefined): HTMLElement {
  const section = element(doc, "section", H.section);
  section.append(element(doc, "h5", H.heading, translate(language, "house.sources.heading")));
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
    const names = sources.solar_forecasts.join(", ");
    const left = answer.sun?.status === "learning" ? answer.sun.days_left : null;
    add(
      left === null || left <= 0
        ? translate(language, "house.sources.solar", { names })
        : translate(language, "house.sources.solarLearning", { names, left: daysLeft(language, left) }),
    );
  }
  const heat = answer.heat;
  if (heat !== null && heat.status === "found" && heat.entity_id !== null) {
    const from = heat.source === "energy_dashboard" ? "house.sources.heatFromDashboard" : heat.source === "integration" ? "house.sources.heatFromIntegration" : "house.sources.heatFromSetting";
    add(translate(language, from, { name: heat.name ?? entityName(words, heat.entity_id) }));
    const comparison = heat.comparison;
    if (comparison !== null) {
      add(
        translate(language, "house.sources.heatSplit", {
          split: plusMinus(language, comparison.split_mae, "kW"),
          whole: plusMinus(language, comparison.house_mae, "kW"),
          hours: String(comparison.hours),
        }),
      );
    }
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
    notes.push(
      translate(
        language,
        battery.control === "on" ? "house.note.controlling" : battery.management === "self_managed" ? "house.note.selfManaged" : "house.note.shadow",
      ),
    );
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

// ------------------------------------------------------------------------------------------------ tabs

/** The site's local date at `nowMs`. */
export function localDate(timeZone: string, nowMs: number): string {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(nowMs));
    const get = (type: string): string => parts.find((part) => part.type === type)?.value ?? "";
    return `${get("year")}-${get("month")}-${get("day")}`;
  } catch {
    return new Date(nowMs).toISOString().slice(0, 10);
  }
}

/** The battery's level and mode, under the schematic ("Batteri 48 % · läge Balanserad"); `null` without a battery. */
export function batterySub(language: Language, answer: HouseAnswer): string | null {
  const battery = answer.battery;
  if (battery === null) {
    return null;
  }
  const words: string[] = [];
  if (battery.soc_pct !== null) {
    words.push(translate(language, "house.sub.soc", { pct: pct(language, battery.soc_pct) }));
  }
  words.push(translate(language, "house.sub.mode", { mode: modeName(language, answer.today?.mode ?? battery.mode) }));
  return words.join(" · ");
}

function statusParagraph(doc: Document, language: Language, key: TranslationKey, line: string): HTMLElement {
  const paragraph = element(doc, "p", `${C.muted} ${H.line}`, translate(language, key));
  paragraph.setAttribute("role", "status");
  paragraph.dataset["line"] = line;
  return paragraph;
}

/** Today's money in Now, one line: "I dag: batteriet tjänade 11 kr · SpotNav sparade 17 kr", with "(egen styrning)" after
 * the battery's figure when it ran under its own control; `null` with neither. */
export function nowMoneyLine(language: Language, answer: HouseAnswer): string | null {
  const parts: string[] = [];
  if (answer.battery !== null && answer.today !== null) {
    // Under its own control (SpotNav off or in test, Sigenergy's AI) the battery's earnings are its own, not SpotNav's.
    const key = ownControl(answer) ? "house.nowTab.moneyBatteryOwn" : "house.nowTab.moneyBattery";
    parts.push(translate(language, key, { amount: amount(language, answer.today.arbitrage, answer.money) }));
  }
  if (answer.saving !== null && answer.saving.total !== null) {
    parts.push(translate(language, "house.nowTab.moneySaved", { amount: amount(language, answer.saving.total, answer.money) }));
  }
  return parts.length === 0 ? null : translate(language, "house.nowTab.money", { parts: parts.join(" · ") });
}

/** Whether the card is drawn into a box of fixed height (its hints and footers then go). */
function boxed(input: HouseRenderInput): boolean {
  return input.ui.fill !== null && input.ui.fill !== undefined;
}

/** Now: the live schematic, the plan's sentence, today's money, the mode and a learning period, and a heat meter to
 * choose. */
function nowPanel(input: HouseRenderInput, answer: HouseAnswer, panel: HTMLElement): void {
  const { doc, language } = input;
  const live = input.tabs?.live ?? null;
  const wrap = element(doc, "div", "spotnav-now");
  wrap.dataset["part"] = "now";
  wrap.append(renderSchematic({ doc, language, answer, live, width: input.width, height: input.ui.fill ?? null, idPrefix: input.idPrefix }));
  panel.append(wrap);
  const plan = planSentence(language, answer, input.nowMs);
  if (plan !== null) {
    const line = element(doc, "p", H.line, plan);
    line.dataset["line"] = "plan";
    panel.append(line);
  }
  const money = nowMoneyLine(language, answer);
  if (money !== null) {
    const line = element(doc, "p", H.line, money);
    line.dataset["line"] = "today";
    panel.append(line);
  }
  // Small and grey: the mode, a learning period that holds something back, and an older backend's missing live figures.
  const notes: string[] = answer.battery === null ? [] : [translate(language, "house.sub.mode", { mode: modeName(language, answer.today?.mode ?? answer.battery.mode) })];
  if (live === null && !boxed(input)) {
    notes.push(translate(language, "house.nowTab.noLive"));
  }
  if (notes.length > 0) {
    const joined = notes.join(" · ");
    const line = element(doc, "p", `${C.muted} ${H.sub}`, joined.charAt(0).toLocaleUpperCase(language) + joined.slice(1));
    line.dataset["line"] = "battery-sub";
    panel.append(line);
  }
  for (const text of learningLines(language, answer)) {
    const line = element(doc, "p", `${C.muted} ${H.sub}`, text);
    line.setAttribute("role", "status");
    line.dataset["line"] = "learning";
    panel.append(line);
  }
  const heat = answer.heat;
  if (heat !== null && (heat.status === "ambiguous" || heat.status === "unusable")) {
    const line = element(doc, "p", H.line, heatLine(language, answer, input.entityWords) ?? "");
    line.dataset["line"] = "heat";
    panel.append(line);
  }
}

/** The period tabs' picker, shared by the three; `through` the time today's figures reach ("I dag · till 17:00"). */
function picker(input: HouseRenderInput, today: string, through: string | null = null): HTMLElement {
  const choice = input.ui.choice ?? { period: "day", date: null };
  return periodPicker(input.doc, input.language, choice, today, (next) => input.handlers.onPeriod?.(next), through);
}

/** The shown period's history, or the line saying it is on its way or could not be read. */
function historyOrStatus(input: HouseRenderInput, panel: HTMLElement): HouseHistory | null {
  const history = input.tabs?.history ?? null;
  if (history === null || history.kind === "loading") {
    const line = statusParagraph(input.doc, input.language, "hist.loading", "loading");
    line.setAttribute("aria-busy", "true");
    panel.append(line);
    return null;
  }
  if (history.kind === "failed") {
    const line = statusParagraph(input.doc, input.language, history.code === "unknown_command" || history.code === "spotnav_unsupported_api_version" ? "hist.unsupported" : "hist.failed", "failed");
    if (history.code !== null) {
      line.dataset["code"] = history.code;
    }
    panel.append(line);
    return null;
  }
  return history.answer;
}

/** The energy flow of the period (today's: the card's own answer, up to its last read); its tiles only where no
 * diagram can be drawn. */
function flowPanel(input: HouseRenderInput, answer: HouseAnswer, panel: HTMLElement, today: string): void {
  const { doc, language } = input;
  const choice = input.ui.choice ?? { period: "day", date: null };
  const isToday = choice.period === "day" && choice.date === null;
  const through = isToday && answer.energy_flow !== null ? clock(context(language, answer), Date.parse(answer.energy_flow.through)) : null;
  panel.append(picker(input, today, through));
  let flow: HouseAnswer["energy_flow"];
  let running: boolean;
  let date: string;
  if (isToday) {
    flow = answer.energy_flow;
    running = true;
    date = today;
  } else {
    const history = historyOrStatus(input, panel);
    if (history === null) {
      return;
    }
    flow = history.energy_flow;
    running = history.today >= history.date && history.today < history.end;
    date = history.date;
  }
  if (flow === null) {
    panel.append(statusParagraph(doc, language, "hist.house.noMeters", "no-meters"));
    return;
  }
  const onHouse = input.handlers.onHouseLoads?.bind(input.handlers) ?? null;
  // Today's day as before (no date asked); another period by its own.
  const loads = onHouse === null ? null : (opener: HTMLElement, kind: LoadsKind) => onHouse(opener, kind, isToday ? null : { period: choice.period, date, running });
  // Lower than the old front's own diagram, the tiles under it carry the figures; a box gives it its spare room.
  const flowWidth = Math.max(260, Math.floor(input.width));
  const diagram = renderFlow(doc, language, flow, flowWidth, input.idPrefix, !running || choice.period !== "day", loads, input.ui.fill ?? Math.min(flowHeight(flowWidth, 5), TAB_FLOW_PX), input.measure ?? null);
  const section = element(doc, "section", `${H.flow}`);
  section.dataset["part"] = "energy-flow";
  if (diagram === null) {
    // No diagram to carry the figures (nothing flowed between two blocks yet): the tiles do, where any has energy.
    section.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, running && choice.period === "day" ? "house.flow.waiting" : "hist.house.noEnergy")));
    panel.append(section);
    const tiles = tilesBlock(doc, language, flow);
    if (tiles !== null) {
      panel.append(tiles);
    }
    return;
  }
  section.append(diagram);
  panel.append(section);
  // The diagram carries the figures; in a box of fixed height it takes the room, elsewhere the hint follows it.
  if (boxed(input)) {
    return;
  }
  if (loads !== null && diagram.querySelector(".spotnav-house-flow-action") !== null) {
    const heat = diagram.querySelector(".spotnav-house-flow-action[data-role='heat']") !== null;
    panel.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, heat ? "house.loads.tapHeat" : "house.loads.tap")));
  }
}

/** The day's hours a tab draws: today's from the card's answer, a past day's from its own read; `undefined` while
 * that is on its way (the line is in `panel`), `null` when the day has none (too far back: the history's bars). */
function dayHours(
  input: HouseRenderInput,
  answer: HouseAnswer,
  panel: HTMLElement,
  choice: PeriodChoice = input.ui.choice ?? { period: "day", date: null },
): HouseAnswer | null | undefined {
  if (choice.date === null) {
    return answer;
  }
  const first = answer.day?.first ?? null;
  if (first !== null && choice.date < first) {
    return null;
  }
  const day = input.tabs?.day ?? null;
  if (day === null || day.kind === "loading") {
    const line = statusParagraph(input.doc, input.language, "hist.loading", "loading");
    line.setAttribute("aria-busy", "true");
    panel.append(line);
    return undefined;
  }
  if (day.kind === "failed") {
    return null;
  }
  return day.answer.day !== null && !day.answer.day.has_data ? null : day.answer;
}

/** The bars' height: the spare room in a box, else by the width. */
function barsHeight(input: HouseRenderInput): number {
  return Math.round(input.ui.fill ?? Math.min(200, Math.max(130, input.width * 0.36)));
}

/** Our energy chart: a day by the hour (today with the forecast ahead), a month by the day, a year by the month. */
function graphPanel(input: HouseRenderInput, answer: HouseAnswer, panel: HTMLElement, today: string): void {
  const choice = input.ui.choice ?? { period: "day", date: null };
  panel.append(picker(input, today));
  if (choice.period === "day") {
    const shown = dayHours(input, answer, panel);
    if (shown === undefined) {
      return;
    }
    if (shown !== null) {
      panel.append(chartBlock({ ...input, nowMs: shown === answer ? input.nowMs : Date.parse(`${choice.date}T23:59:59Z`) + 86_400_000 }, shown, "energy", input.ui.fill ?? null));
      return;
    }
  }
  const history = historyOrStatus(input, panel);
  if (history === null) {
    return;
  }
  const width = Math.max(240, Math.round(input.width));
  const chart = energyChart(input.doc, input.language, history, width, input.ui.fill ?? houseChartHeight(width), input.ui.bar ?? null, input.handlers.onBar?.bind(input.handlers), boxed(input));
  panel.append(chart ?? statusParagraph(input.doc, input.language, "hist.house.noEnergy", "no-energy"));
}

/** A past day's hours with the history's prices where the day's own answer has none (the battery plan keeps only the
 * prices around now; the history prices every period it holds). */
export function withPrices(day: HouseAnswer, history: HouseHistory | null): HouseAnswer {
  if (history === null || history.period !== "day" || day.hours.some((hour) => hour.buy !== null)) {
    return day;
  }
  const byStart = new Map(history.buckets.map((bucket) => [Date.parse(bucket.key), bucket]));
  return {
    ...day,
    hours: day.hours.map((hour) => {
      const bucket = byStart.get(hour.startMs);
      return bucket === undefined ? hour : { ...hour, buy: bucket.buy, sell: bucket.sell };
    }),
  };
}

/** The day's hourly figures of what the battery earned, on the chart's hours (`null` where the history has none). */
function earnedOnHours(hours: readonly HouseHour[], history: HouseHistory | null): Array<number | null> {
  if (history === null || history.period !== "day") {
    return hours.map(() => null);
  }
  const byStart = new Map(history.buckets.map((bucket) => [Date.parse(bucket.key), bucket.arbitrage]));
  return hours.map((hour) => byStart.get(hour.startMs) ?? null);
}

/** What each of the chart's hours' figure is made of (`null` where the history has none). */
function flowOnHours(hours: readonly HouseHour[], history: HouseHistory | null): Array<BatteryFlow | null> {
  if (history === null || history.period !== "day") {
    return hours.map(() => null);
  }
  const byStart = new Map(history.buckets.map((bucket) => [Date.parse(bucket.key), bucket.battery_flow]));
  return hours.map((hour) => byStart.get(hour.startMs) ?? null);
}

/** Price and battery: a day's prices with the battery's level and plan, under a line with the cost.
 * A day only: a month's or a year's kr bars were the Earnings tab's figures, and the prices are hourly. */
function pricePanel(input: HouseRenderInput, answer: HouseAnswer, panel: HTMLElement, today: string): void {
  const { doc, language } = input;
  const choice = dayOnly(input.ui.choice ?? { period: "day", date: null }, today);
  panel.append(periodPicker(doc, language, choice, today, (next) => input.handlers.onPeriod?.(next), null, ["day"]));
  const loaded = input.tabs?.history ?? null;
  const history = loaded !== null && loaded.kind === "ready" ? loaded.answer : null;
  const isToday = choice.period === "day" && choice.date === null;
  const sub: string[] = [];
  const cost = history?.cost ?? null;
  if (isToday && answer.today !== null) {
    sub.push(todayDetail(language, answer) ?? "");
  } else if (cost !== null && cost.priced > 0) {
    sub.push(translate(language, "house.todayCost", { amount: amount(language, cost.bought, answer.money) }));
    if (Math.abs(cost.sold) >= 50) {
      sub.push(translate(language, "house.todaySold", { amount: amount(language, cost.sold, answer.money) }));
    }
  }
  const subline = sub.filter((part) => part !== "").join(" · ");
  if (subline !== "" && (answer.battery !== null ? history !== null : true)) {
    const line = element(doc, "p", `${C.muted} spotnav-house-total-sub`, subline);
    line.dataset["line"] = "earned-sub";
    panel.append(line);
  }
  const shown = dayHours(input, answer, panel, choice);
  if (shown === undefined) {
    return;
  }
  const priced = shown === null || shown === answer ? shown : withPrices(shown, history);
  if (priced !== null && priced.hours.some((hour) => hour.buy !== null)) {
    const values = answer.battery === null ? null : earnedOnHours(priced.hours, history);
    const flows = flowOnHours(priced.hours, history);
    const earned = values !== null && (values.some((value) => value !== null) || flows.some((flow) => flow !== null)) ? { values, flows } : null;
    panel.append(chartBlock({ ...input, nowMs: shown === answer ? input.nowMs : Date.parse(`${choice.date}T23:59:59Z`) + 86_400_000 }, priced, "price", input.ui.fill ?? null, earned));
    return;
  }
  if (loaded === null || loaded.kind !== "ready") {
    historyOrStatus(input, panel);
  }
}

/** Earnings: the heading with the sum of the parts drawn, the parts as toggles with their sums, the period's bars (an
 * hour, a day or a month each, a stack per part) and what each figure counts (`house-earnings.ts`). */
function earnPanel(input: HouseRenderInput, answer: HouseAnswer, panel: HTMLElement, today: string): void {
  const { doc, language } = input;
  panel.append(picker(input, today));
  const history = historyOrStatus(input, panel);
  if (history === null) {
    return;
  }
  const money = (minor: number): string => amount(language, minor, history.money ?? answer.money);
  const hidden = new Set(input.ui.earnHidden ?? []);
  if (earnParts(history).length === 0) {
    panel.append(statusParagraph(doc, language, "house.earn.none", "no-earnings"));
    return;
  }
  const total = earnTotal(history, hidden);
  const heading = element(doc, "p", PERIOD_VIEW_CLASSES.total, total === null ? translate(language, "house.earn.noneShown") : translate(language, "house.earn.total", { amount: money(total) }));
  heading.dataset["line"] = "earn-total";
  panel.append(heading, earnLegend(doc, language, history, hidden, money, (next) => input.handlers.onEarnPart?.(next)));
  const bars = earnBars({
    doc,
    language,
    answer: history,
    hidden,
    height: barsHeight(input),
    money,
    selected: input.ui.bar ?? null,
    ...(input.handlers.onBar === undefined ? {} : { onSelect: input.handlers.onBar.bind(input.handlers) }),
    boxed: boxed(input),
  });
  if (bars !== null) {
    panel.append(bars);
  }
  if (boxed(input)) {
    return;
  }
  const notes = earnNotes(language, history, hidden, money);
  if (notes.length > 0) {
    const line = element(doc, "p", `${C.muted} spotnav-house-earn-notes`, notes.join(" "));
    line.dataset["line"] = "earn-notes";
    panel.append(line);
  }
}

// ------------------------------------------------------------------------------------------------ body

/** The card's whole content for one state. */
export function houseBody(input: HouseRenderInput): HTMLElement {
  const { doc, language, state } = input;
  const card = element(doc, "div", `${C.card} ${H.card}`);
  card.dataset["state"] = state.kind;
  if (state.kind !== "ready") {
    card.append(header(input, null));
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
  card.append(header(input, answer));
  const locked = input.ui.locked ?? null;
  const tab: HouseTab = locked ?? input.ui.tab ?? "now";
  card.dataset["tab"] = tab;
  if (locked === null) {
    card.append(tabList(doc, language, tab, input.idPrefix, (next, byKey) => input.handlers.onTab?.(next, byKey), input.ui.tabFit ?? "long"));
  }
  const panel = tabPanel(doc, tab, input.idPrefix, locked === null);
  card.append(panel);
  const today = input.tabs?.today ?? (answer.day?.today === true ? answer.day.date : localDate(answer.time_zone, input.nowMs));
  if (tab === "now") {
    if (answer.day !== null && !answer.day.has_data) {
      panel.append(statusParagraph(doc, language, "house.day.noData", "no-data"));
    }
    nowPanel(input, answer, panel);
  } else if (tab === "flow") {
    flowPanel(input, answer, panel, today);
  } else if (tab === "graph") {
    graphPanel(input, answer, panel, today);
  } else if (tab === "earn") {
    earnPanel(input, answer, panel, today);
  } else {
    pricePanel(input, answer, panel, today);
  }
  return card;
}

export { kwhText, schematicHeight };
