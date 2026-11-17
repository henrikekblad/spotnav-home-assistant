// The house card's body: a header, the one chart with its legend and readout, one line of money and one line of how
// well the forecasts hit, and the details behind a tap (the share of the perfect plan first, the battery's modes side
// by side, the reviewed days, the sources and what was assumed). Built fresh for each state with `textContent` only;
// every figure is the backend's.

import { clock, dateLabel, dayCue, formatFixed, formatNumber, hourLabel, type EntityWords, type FormatContext } from "./format";
import { heatShown, houseChartHeight, houseShown, renderHouseChart, sunShown, type HouseChart, type HouseChartView } from "./house-chart";
import { MODES, type BatteryMode, type DayFlow, type HouseAnswer, type HouseHour } from "./house-model";
import { renderFlow, type LoadsKind } from "./house-flow";
import { hasPerfect, perfectDetails, perfectHeadline, perfectNotes, perfectRows } from "./perfect-view";
import { hiddenIn, type HiddenSeries } from "./house-series";
import { settingsGearButton, testChip } from "./heat-view";
import { HEAT_CLASSES as X } from "./heat-styles";
import { historyHeaderButton } from "./period-history";
import { HOUSE_CLASSES as H } from "./house-styles";
import { daysLeft, pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

const HOUR_MS = 3_600_000;

export type HouseState =
  | { kind: "unconfigured" }
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null }
  | { kind: "ready"; answer: HouseAnswer };

export interface HouseUi {
  detailsOpen: boolean;
  /** The series hidden from the chart's legend, per view (absent: each view's defaults, `house-series.ts`). */
  hidden?: HiddenSeries;
  /** The chart's view: the energy (default) or the price with the battery. */
  view?: HouseChartView;
  /** The hour shown in the readout (an index into `hours`), `null` for the hint. */
  selected: number | null;
  /** The header's test chip opened to its explanation. */
  testOpen?: boolean;
}

export interface HouseHandlers {
  onDetails(open: boolean): void;
  onView?(view: HouseChartView): void;
  /** The viewer hid or showed a series: the view's whole hidden list after the change. */
  onLegend?(view: HouseChartView, hidden: string[]): void;
  onSelect(index: number | null): void;
  /** The clock in the header: the history dialog (`history-dialog.ts`). Without it the card has no clock. */
  onHistory?(opener: HTMLElement): void;
  /** An administrator's settings gear: the shared settings popover. Without it the card has no gear. */
  onSettings?(): void;
  /** The test chip tapped: its explanation shown or hidden. Without it the chip opens and closes in place. */
  onTest?(open: boolean): void;
  /** House in the energy flow tapped: what the house used (`house-loads.ts`). Without it the block is no button. */
  onHouseLoads?(opener: HTMLElement, kind: LoadsKind): void;
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
  return translate(language, past ? "house.dayMoney" : "house.today", {
    day: past ? dateLabel(language, answer.day!.date) : "",
    amount: amount(language, today.arbitrage, answer.money),
  });
}

/** The brands whose own app is verified to show the same measure as the headline, by name in the hint. */
const SAME_MEASURE_APPS: Record<string, string> = { sigenergy: "Sigenergy" };

/** Under the headline: "the same measure as the Sigenergy app" for a brand verified to match, else what it counts. */
export function todayHint(language: Language, answer: HouseAnswer): string {
  const brand = SAME_MEASURE_APPS[answer.battery?.brand ?? ""];
  return brand === undefined ? translate(language, "house.todayHint") : translate(language, "house.todayHintApp", { brand });
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

/** A signed saving: whole units from one up, one decimal below ("+25 kr", "−4 kr", "+0,4 kr"). */
function signedAmount(language: Language, minor: number, money: HouseAnswer["money"]): string {
  if (money === null || money.major_unit === null) {
    return `${minor > 0 ? "+" : ""}${amount(language, minor, money)}`;
  }
  const major = minor / 100;
  const shown = formatFixed(language, major, Math.abs(major) >= 1 ? 0 : 1);
  return `${major > 0 ? "+" : ""}${shown} ${money.major_unit}`;
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

/** The status lines of the learning periods that hold something back now, one each ("Laddar inte från nätet än · lär
 * sig husets toppar per fas, 13 dygn kvar"); none when nothing is held back. */
export function learningLines(language: Language, answer: HouseAnswer): string[] {
  const lines: string[] = [];
  for (const hold of answer.learning) {
    if (hold.what !== "phase_peaks") {
      continue;
    }
    const held =
      hold.cap_kw > 0
        ? translate(language, "house.learning.gridChargeCapped", { kw: formatNumber(language, hold.cap_kw, 1) })
        : translate(language, "house.learning.noGridCharge");
    lines.push(`${held} · ${translate(language, "house.learning.phasePeaks", { left: daysLeft(language, hold.days_left) })}`);
  }
  return lines;
}

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

function header(input: HouseRenderInput, answer: HouseAnswer | null): HTMLElement {
  const { doc, language, handlers } = input;
  const onSettings = handlers.onSettings?.bind(handlers);
  const onHistory = handlers.onHistory?.bind(handlers);
  const head = element(doc, "div", H.head);
  // "Test" while SpotNav does not control the battery (off, test only, still checking it); once control is on and
  // checked, a quiet chip that says so instead.
  const controlling = answer?.battery?.control === "on";
  const test = testChip(doc, language, "house.test.explain", input.ui.testOpen === true, `${input.idPrefix}-test`, handlers.onTest?.bind(handlers));
  const chip = controlling ? element(doc, "span", H.chip, translate(language, "house.controlling")) : test.chip;
  if (controlling) {
    chip.dataset["line"] = "controlling";
  }
  head.append(element(doc, "h2", H.title, translate(language, "house.title")), chip);
  if (onHistory !== undefined) {
    const button = historyHeaderButton(doc, translate(language, "hist.open"), X.settingsButton, () => onHistory(button));
    head.append(button);
  }
  if (onSettings !== undefined) {
    head.append(settingsGearButton(doc, translate(language, "popover.open"), onSettings));
  }
  const wrap = element(doc, "div");
  wrap.append(head);
  if (!controlling) {
    wrap.append(test.explain);
  }
  const battery = answer?.battery ?? null;
  if (battery !== null) {
    const words = [];
    if (answer !== null && answer.day !== null && !answer.day.today) {
      // A past day: the level logged at its last hour's end, said to be that day's.
      const logged = [...answer.hours].reverse().find((hour) => hour.soc_pct !== null)?.soc_pct ?? null;
      if (logged !== null) {
        words.push(translate(language, "house.sub.socDayEnd", { pct: pct(language, logged) }));
      }
    } else if (battery.soc_pct !== null) {
      words.push(translate(language, "house.sub.soc", { pct: pct(language, battery.soc_pct) }));
    }
    words.push(translate(language, "house.sub.mode", { mode: modeName(language, answer?.today?.mode ?? battery.mode) }));
    wrap.append(element(doc, "p", `${C.muted} ${H.sub}`, words.join(" · ")));
    const today = answer === null ? null : todayLine(language, answer);
    if (today !== null) {
      const line = element(doc, "p", H.today, today);
      line.dataset["line"] = "today";
      // Said once, right under it: the battery app's own measure where that is verified, else what it counts.
      const hint = element(doc, "p", `${C.muted} ${H.todayDetail}`, todayHint(language, answer!));
      hint.dataset["line"] = "today-hint";
      wrap.append(line, hint);
      const detail = todayDetail(language, answer!);
      if (detail !== null) {
        const sub = element(doc, "p", `${C.muted} ${H.todayDetail}`, detail);
        sub.dataset["line"] = "today-detail";
        wrap.append(sub);
      }
    }
  }
  return wrap;
}

/** The comparison in the main body before the first perfect plan (from then on the perfect share in the details stands
 * in for it), its baseline in its title: each of SpotNav's modes over the reviewed nights and the battery's own control
 * (its optimiser named where known), against the battery only storing its own sun (self-consumption, the zero line and
 * so not a row), the chosen mode marked, a short bar each so the order shows. */
function compareBlock(doc: Document, language: Language, answer: HouseAnswer): HTMLElement | null {
  const battery = answer.battery;
  if (battery === null) {
    return null;
  }
  const rows: Array<{ key: string; name: string; value: number | null; days: number; inForce: boolean }> = [];
  const optimiser = battery.management === "self_managed" ? OPTIMISER_NAMES[battery.brand ?? ""] : undefined;
  const ownName = `${translate(language, "house.compare.own")}${optimiser === undefined ? "" : ` (${optimiser})`}`;
  let heading: string;
  if (answer.day !== null && !answer.day.today) {
    // A past day: its own review, or plainly none.
    const review = answer.day_review;
    for (const mode of SAVING_MODES) {
      const value = review?.modes[mode] ?? null;
      rows.push({ key: mode, name: spotnavMode(language, mode), value, days: value === null ? 0 : 1, inForce: review !== null && mode === review.mode });
    }
    rows.push({ key: "own", name: ownName, value: review === null ? null : review.own_saving, days: review === null ? 0 : 1, inForce: false });
    heading = review === null ? translate(language, "house.compare.headingNone") : translate(language, "house.compare.headingDay", { day: dateLabel(language, review.date) });
  } else {
    for (const mode of SAVING_MODES) {
      const figures = battery.modes[mode];
      if (figures !== undefined) {
        rows.push({ key: mode, name: spotnavMode(language, mode), value: figures.reviewed_days > 0 ? figures.reviewed_saving : null, days: figures.reviewed_days, inForce: mode === battery.mode });
      }
    }
    if (rows.length === 0) {
      return null;
    }
    const review = battery.review;
    rows.push({ key: "own", name: ownName, value: review === null ? null : review.own_saving, days: review === null ? 0 : review.days, inForce: false });
    const most = Math.max(...rows.map((row) => row.days));
    heading = most > 0 ? translate(language, `house.compare.heading.${pluralForm(language, most)}`, { days: String(most) }) : translate(language, "house.compare.headingNone");
  }
  const days = Math.max(...rows.map((row) => row.days));
  const section = element(doc, "section", H.compare);
  section.dataset["part"] = "modes";
  section.append(element(doc, "p", `${C.muted} ${H.compareHead}`, heading));
  const largest = Math.max(1, ...rows.map((row) => Math.abs(row.value ?? 0)));
  const grid = element(doc, "div", H.compareGrid);
  for (const row of rows) {
    const name = element(doc, "span", H.compareName, row.name);
    if (row.inForce) {
      name.append(" ", element(doc, "span", H.chip, translate(language, "house.compare.inForce")));
    }
    const track = element(doc, "span", H.compareTrack);
    track.setAttribute("aria-hidden", "true");
    if (row.value !== null && row.value !== 0) {
      const fill = element(doc, "span", H.compareFill);
      fill.style.width = `${Math.round((Math.abs(row.value) / largest) * 100)}%`;
      fill.dataset["sign"] = row.value < 0 ? "minus" : "plus";
      track.append(fill);
    }
    let figure = row.value === null ? translate(language, "house.compare.none") : signedAmount(language, row.value, answer.money);
    if (row.value !== null && row.days < days) {
      figure += ` (${translate(language, `house.compare.days.${pluralForm(language, row.days)}`, { days: String(row.days) })})`;
    }
    const value = element(doc, "span", row.value === null ? `${H.compareValue} ${C.muted}` : H.compareValue, figure);
    const line = element(doc, "div", H.compareRow);
    line.dataset["row"] = row.key;
    if (row.inForce) {
      line.dataset["inForce"] = "true";
    }
    line.append(name, track, value);
    grid.append(line);
  }
  section.append(grid);
  return section;
}

/** The battery maker's own optimiser by brand, named where the battery runs it. */
const OPTIMISER_NAMES: Record<string, string> = { sigenergy: "Sigenergy AI" };

/** The battery's own control as the comparison names it: its optimiser where the battery runs one ("Sigenergy AI"). */
function ownControlName(language: Language, answer: HouseAnswer): string {
  const battery = answer.battery;
  const optimiser = battery?.management === "self_managed" ? OPTIMISER_NAMES[battery.brand ?? ""] : undefined;
  return optimiser ?? translate(language, "house.compare.own");
}

/** The perfect plan's figures shown: a past day's own, else the reviewed week's; `null` before the first. */
function perfectShown(answer: HouseAnswer) {
  const past = answer.day !== null && !answer.day.today;
  const perfect = past ? (answer.day_review?.perfect ?? null) : (answer.battery?.review?.perfect ?? null);
  return hasPerfect(perfect) ? { perfect, past } : null;
}

/** The comparison as each way's share of the perfect plan in hindsight (`perfect-view.ts`): the headline, a bar per
 * way and the plain notes; the money behind it is in the details. `null` before the first perfect plan. */
function perfectBlock(doc: Document, language: Language, answer: HouseAnswer): HTMLElement | null {
  const shown = perfectShown(answer);
  if (shown === null) {
    return null;
  }
  const { perfect, past } = shown;
  const ownName = ownControlName(language, answer);
  const section = element(doc, "section", H.compare);
  section.dataset["part"] = "modes";
  section.dataset["view"] = "perfect";
  const heading = past
    ? translate(language, "perfect.headingDay", { day: dateLabel(language, answer.day!.date) })
    : translate(language, `perfect.heading.${pluralForm(language, perfect.days + perfect.partial)}`, { days: String(perfect.days + perfect.partial) });
  section.append(element(doc, "p", `${C.muted} ${H.compareHead}`, heading));
  const headline = perfectHeadline(language, perfect, ownName);
  if (headline !== null) {
    const line = element(doc, "p", H.perfectHead, headline);
    line.dataset["line"] = "perfect-headline";
    section.append(line);
  }
  const inForce = past ? (answer.day_review?.mode ?? null) : (answer.battery?.mode ?? null);
  section.append(perfectRows(doc, language, perfect, ownName, inForce), ...perfectNotes(doc, language, perfect, ownName, past));
  return section;
}

/** The modes the comparison shows: self-consumption is what they are measured against. */
const SAVING_MODES = MODES.filter((mode) => mode !== "self_consumption");

function spotnavMode(language: Language, mode: BatteryMode): string {
  return translate(language, "house.compare.spotnav", { mode: modeName(language, mode) });
}

/** The day (or the week) in the battery app's terms: what the battery saved against no battery at all, what the sun
 * gave, and both together, each with a line saying what it is compared with. A past day says how much of it the figures
 * cover; today shows the reviewed days' sums. `null` where nothing is known yet. */
function baselinesBlock(doc: Document, language: Language, answer: HouseAnswer): HTMLElement | null {
  const battery = answer.battery;
  if (battery === null) {
    return null;
  }
  let heading: string;
  let figures: Array<{ key: string; value: number | null }>;
  let coverage = "";
  if (answer.day !== null && !answer.day.today) {
    const review = answer.day_review;
    if (review === null || (review.battery_saving === null && review.sun_gave === null)) {
      return null;
    }
    heading = translate(language, "house.baselines.headingDay", { day: dateLabel(language, review.date) });
    figures = [
      { key: "battery", value: review.battery_saving },
      { key: "sun", value: review.sun_gave },
      { key: "both", value: review.against_nothing },
    ];
    if (review.day_quarters > 0 && review.covered_quarters < review.day_quarters) {
      coverage = translate(language, "house.baselines.partial", { covered: String(review.covered_quarters), total: String(review.day_quarters) });
    }
    if (review.sun_gave !== null && review.sun_quarters < review.covered_quarters) {
      coverage = `${coverage} ${translate(language, "house.baselines.sunPartial", { sun: String(review.sun_quarters), covered: String(review.covered_quarters) })}`.trim();
    }
  } else {
    const review = battery.review;
    if (review === null || (review.battery_saving === null && review.sun_gave === null)) {
      return null;
    }
    heading = translate(language, `house.baselines.headingWeek.${pluralForm(language, review.days)}`, { days: String(review.days) });
    figures = [
      { key: "battery", value: review.battery_saving },
      { key: "sun", value: review.sun_gave },
    ];
  }
  const section = element(doc, "section", H.base);
  section.dataset["part"] = "baselines";
  section.append(element(doc, "p", `${C.muted} ${H.compareHead}`, heading));
  for (const figure of figures) {
    if (figure.value === null) {
      continue;
    }
    const row = element(doc, "div", H.baseRow);
    row.dataset["row"] = figure.key;
    row.append(
      element(doc, "span", undefined, translate(language, `house.baselines.${figure.key}` as TranslationKey)),
      element(doc, "span", H.baseValue, amount(language, figure.value, answer.money)),
    );
    section.append(row, element(doc, "p", `${C.muted} ${H.baseNote}`, translate(language, `house.baselines.${figure.key}Note` as TranslationKey)));
  }
  if (coverage !== "") {
    const partial = element(doc, "p", `${C.muted} ${H.baseNote}`, coverage);
    partial.dataset["line"] = "baselines-coverage";
    section.append(partial);
  }
  return section;
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
      item("sun", "house.legend.sun");
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

function chartBlock(input: HouseRenderInput, answer: HouseAnswer): HTMLElement {
  const { doc, language, ui, handlers, nowMs } = input;
  const ctx = context(language, answer);
  const surface = element(doc, "div", H.surface);
  surface.tabIndex = 0;
  const readout = element(doc, "p", `${C.muted} ${C.readout} ${H.readout}`);
  readout.setAttribute("aria-live", "polite");
  const width = Math.max(200, Math.round(input.width));
  const priced = answer.hours.some((hour) => hour.buy !== null);
  const view: HouseChartView = priced ? (ui.view ?? "energy") : "energy";
  const hidden = hiddenIn(ui.hidden, view);
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
    hidden,
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
    readout.textContent = hour === undefined ? translate(language, view === "energy" ? "house.hint.energy" : "house.hint") : hourFigures(language, answer, hour, nowMs, view, hidden);
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
  // The readout right under the chart, where the eye is when a finger or the pointer is on it; its lines are reserved
  // so nothing below moves when an hour is chosen.
  block.append(surface, readout, legend(doc, language, answer, view, hidden, handlers));
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

/** How the energy flow is counted, in the details: the fixed rule the split follows and where the car or the heat pump
 * could not be read; `null` without a drawn flow (`drawn`: the front has its diagram). */
function flowNotes(doc: Document, language: Language, answer: HouseAnswer, drawn: boolean): HTMLElement | null {
  const flow = answer.energy_flow;
  if (flow === null || !drawn) {
    return null;
  }
  const section = element(doc, "section", H.section);
  section.dataset["part"] = "flow-notes";
  section.append(
    element(doc, "h3", H.heading, translate(language, "house.flow.how")),
    element(doc, "p", `${C.muted} ${H.note}`, translate(language, flow.step_min < 60 ? "house.flow.ruleQuarter" : "house.flow.rule")),
  );
  if (flow.car_hours < flow.hours && flow.nodes.some((node) => node.role === "car")) {
    section.append(element(doc, "p", `${C.muted} ${H.note}`, translate(language, "house.flow.carPart")));
  }
  if (flow.nodes.some((node) => node.role === "heat")) {
    section.append(
      element(doc, "p", `${C.muted} ${H.note}`, translate(language, flow.heat_hours < flow.hours ? "house.flow.heatPart" : "house.flow.heatRule")),
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

/** The money behind the shares, the battery's work and the cells' temperature, in the details. */
function perfectDetailsBlock(doc: Document, language: Language, answer: HouseAnswer): HTMLElement | null {
  const past = answer.day !== null && !answer.day.today;
  const perfect = past ? (answer.day_review?.perfect ?? null) : (answer.battery?.review?.perfect ?? null);
  if (perfect === null || ((perfect.days === 0 || perfect.saving === null) && perfect.temperature === null)) {
    return null;
  }
  return perfectDetails(doc, language, perfect, answer.money, ownControlName(language, answer));
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
  capacity_from_ah: "house.estimate.capacity_from_ah",
  rated_from_current: "house.estimate.rated_from_current",
  capacity_learned: "house.estimate.capacity_learned",
  rated_learned: "house.estimate.rated_learned",
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
  for (const text of learningLines(language, answer)) {
    const line = element(doc, "p", `${H.line} ${C.muted}`, text);
    line.setAttribute("role", "status");
    line.dataset["line"] = "learning";
    card.append(line);
  }
  if (answer.day !== null && !answer.day.has_data) {
    const none = element(doc, "p", `${C.muted} ${H.line}`, translate(language, "house.day.noData"));
    none.setAttribute("role", "status");
    none.dataset["line"] = "no-data";
    card.append(none);
    return card;
  }
  card.append(chartBlock(input, answer));
  // The perfect share lives in the details, first there, so the front stays the earnings, the chart, the heat and the
  // flow; before the first perfect plan the modes' comparison stands on the front.
  const perfect = perfectShown(answer) !== null;
  const comparison = perfect ? null : compareBlock(doc, language, answer);
  if (comparison !== null) {
    card.append(comparison);
  }
  // The week's sentence is the comparison's own once a night was reviewed; before, the line says what is to come.
  const saving = (perfect || comparison !== null) && answer.battery?.review !== null ? null : savingLine(language, answer);
  if (saving !== null) {
    const line = element(doc, "p", H.line, saving);
    line.dataset["line"] = "saving";
    card.append(line);
  }
  const heat = heatLine(language, answer, input.entityWords);
  if (heat !== null) {
    const line = element(doc, "p", H.line, heat);
    line.dataset["line"] = "heat";
    card.append(line);
  }
  const energyFlow = energyFlowBlock(input, answer);
  if (energyFlow !== null) {
    card.append(energyFlow);
  }
  const toggle = element(doc, "button", `${C.linkButton} ${H.detailsToggle}`, translate(language, input.ui.detailsOpen ? "details.hide" : "details.show"));
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(input.ui.detailsOpen));
  const detailsId = `${input.idPrefix}-details`;
  toggle.setAttribute("aria-controls", detailsId);
  toggle.addEventListener("click", () => input.handlers.onDetails(!input.ui.detailsOpen));
  card.append(toggle);
  if (input.ui.detailsOpen) {
    const details = element(doc, "div", H.details);
    details.id = detailsId;
    const review = element(doc, "p", `${H.line} ${C.muted}`, reviewLine(language, answer));
    review.dataset["line"] = "review";
    for (const part of [
      perfectBlock(doc, language, answer),
      energyFlow === null ? flowBlock(doc, language, answer.flow) : flowNotes(doc, language, answer, energyFlow.querySelector("svg") !== null),
      baselinesBlock(doc, language, answer),
      modesTable(doc, language, answer),
      perfectDetailsBlock(doc, language, answer),
      review,
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
