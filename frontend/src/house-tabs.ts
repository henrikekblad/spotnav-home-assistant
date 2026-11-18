// The house card's tabs (SpotNav 3.0): Now, Energy flow, Energy chart, Earnings, Price and battery, in that fixed
// order.
//
// * A tab says its long name ("Energiflöde") while the whole row fits the card, else its short one ("Flöde"), else
//   an icon alone; the card measures the row as drawn (`fitTabs`), never guessing by language. Whatever is drawn, a
//   tab's accessible name is its long name, and a short name or an icon has it as its tooltip too.
// * The row is a WAI-ARIA tablist with automatic activation: the arrow keys move to the previous or next tab (and
//   wrap), Home and End to the first and last, and the tab moved to is shown at once; only the selected tab is in
//   the page's tab order (a roving tabindex), the panel follows it.
// * The tab chosen is remembered per card in this browser (by the site or charger the card names), guarded like the
//   chart preferences (`chart-preference.ts`): a storage that is missing or throws means the first tab.
// * The card's YAML may lock one tab (`tab: now | flow | graph | price`, the card editor's "Show only tab"): the row is
//   then not drawn and the choice is neither read nor written.
// * Today's label may say how far its figures reach ("I dag · till 17:00"); where the row has no room for that part
//   it is left out whole ("I dag"), never cut to "…".
// * The period picker the tabs Energy flow, Energy chart, Earnings and Price and battery share: "‹ 2026-11-17 › [Day ▾]", the
//   kind as a select, the arrows one period back or forward, the date (a day) or the month or year (a select) tapped
//   to choose; nothing after today is offered.

import { browserStore, type PreferenceStore } from "./chart-preference";
import { dateLabel } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  HISTORY_PERIODS,
  periodLabel,
  periodStart,
  pickerMonths,
  pickerYears,
  PICKER_YEARS,
  shiftPeriod,
  type HistoryPeriod,
} from "./period-history";

export type HouseTab = "now" | "flow" | "graph" | "earn" | "price";
export const HOUSE_TABS: readonly HouseTab[] = ["now", "flow", "graph", "earn", "price"];

/** How a tab is worded: its long name, its short name, or its icon alone. */
export type TabFit = "long" | "short" | "icons";
export const TAB_FITS: readonly TabFit[] = ["long", "short", "icons"];

export const TAB_CLASSES = {
  list: "spotnav-house-tabs",
  tab: "spotnav-house-tab",
  tabIcon: "spotnav-house-tab-icon",
  panel: "spotnav-house-panel",
  picker: "spotnav-house-picker",
  pickerDate: "spotnav-house-picker-date",
  pickerKind: "spotnav-house-picker-kind",
  pickerStep: "spotnav-house-picker-step",
  pickerRow: "spotnav-house-picker-row",
  pickerLabel: "spotnav-house-picker-label",
  pickerReach: "spotnav-house-picker-reach",
  pickerHidden: "spotnav-house-picker-hidden",
  pickerOver: "spotnav-house-picker-over",
  pickerKinds: "spotnav-house-picker-kinds",
} as const;

const T = TAB_CLASSES;

export function isHouseTab(value: unknown): value is HouseTab {
  return typeof value === "string" && (HOUSE_TABS as readonly string[]).includes(value);
}

export function tabKey(target: string): string {
  return `spotnav-house-card.tab.${target}`;
}

/** The tab this browser last showed on the card naming `target`; `null` when nothing usable is stored. */
export function readTab(target: string, store: () => PreferenceStore | null = browserStore): HouseTab | null {
  try {
    const value = store()?.getItem(tabKey(target)) ?? null;
    return isHouseTab(value) ? value : null;
  } catch {
    return null;
  }
}

export function writeTab(target: string, tab: HouseTab, store: () => PreferenceStore | null = browserStore): void {
  try {
    store()?.setItem(tabKey(target), tab);
  } catch {
    // Nothing is remembered in this browser; the choice holds while the card lives.
  }
}

const TAB_WORDS: Record<HouseTab, TranslationKey> = {
  now: "house.tab.now",
  flow: "house.tab.flow",
  graph: "house.tab.graph",
  earn: "house.tab.earn",
  price: "house.tab.price",
};

const TAB_SHORT_WORDS: Record<HouseTab, TranslationKey> = {
  now: "house.tab.short.now",
  flow: "house.tab.short.flow",
  graph: "house.tab.short.graph",
  earn: "house.tab.short.earn",
  price: "house.tab.short.price",
};

export function tabWord(language: Language, tab: HouseTab): string {
  return translate(language, TAB_WORDS[tab]);
}

export function tabShortWord(language: Language, tab: HouseTab): string {
  return translate(language, TAB_SHORT_WORDS[tab]);
}

/** The tabs' icons, stroked, in a 24-unit box: a bolt, two crossing flows, bars, coins, a price tag. */
const TAB_ICONS: Record<HouseTab, string[]> = {
  now: ["M13 2.5L4.5 13.5H11L10 21.5L19.5 10H13z"],
  flow: ["M3 6.5h3.5c5.5 0 5.5 11 11 11H21", "M3 17.5h3.5c5.5 0 5.5-11 11-11H21"],
  graph: ["M3 20.5h18", "M6.5 20.5v-8M12 20.5V4.5M17.5 20.5v-11"],
  earn: ["M4 7.5c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z", "M4 7.5V12c0 1.7 3.6 3 8 3s8-1.3 8-3V7.5", "M4 12v4.5c0 1.7 3.6 3 8 3s8-1.3 8-3V12"],
  price: ["M3.5 11.5V4h7.5l9.5 9.5-7.5 7.5z", "M8 8.4v.2"],
};

const SVG_NS = "http://www.w3.org/2000/svg";

function tabIcon(doc: Document, tab: HouseTab): SVGSVGElement {
  const icon = doc.createElementNS(SVG_NS, "svg");
  icon.setAttribute("class", T.tabIcon);
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("width", "22");
  icon.setAttribute("height", "22");
  icon.setAttribute("aria-hidden", "true");
  icon.setAttribute("focusable", "false");
  for (const d of TAB_ICONS[tab]) {
    const path = doc.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    icon.append(path);
  }
  return icon;
}

/** Word a drawn tab row another way (see the module): each tab's content and tooltip, its accessible name kept. */
export function setTabFit(list: HTMLElement, language: Language, fit: TabFit): void {
  list.dataset["fit"] = fit;
  for (const button of Array.from(list.querySelectorAll<HTMLElement>("[role='tab']"))) {
    const tab = button.dataset["tab"];
    if (!isHouseTab(tab)) {
      continue;
    }
    const long = tabWord(language, tab);
    button.setAttribute("aria-label", long);
    if (fit === "icons") {
      button.replaceChildren(tabIcon(button.ownerDocument, tab));
      delete button.dataset["text"];
    } else {
      button.textContent = fit === "short" ? tabShortWord(language, tab) : long;
      // The words once more for the styles, which hold each tab as wide as its selected (bolder) form: the row then
      // measures the same whichever tab is selected, and choosing a tab never changes the wording.
      button.dataset["text"] = button.textContent;
    }
    if (fit === "long") {
      button.removeAttribute("title");
    } else {
      button.title = long;
    }
  }
}

/** The widest wording the drawn row has room for: long names, then short ones, then icons, each measured as laid
 * out (a row not laid out, 0 px wide, keeps its long names). The fit chosen. */
export function fitTabs(list: HTMLElement, language: Language): TabFit {
  for (const fit of TAB_FITS) {
    if (list.dataset["fit"] !== fit) {
      setTabFit(list, language, fit);
    }
    if (fit === "icons" || list.scrollWidth <= list.clientWidth + 1) {
      return fit;
    }
  }
  return "icons";
}

export function tabId(idPrefix: string, tab: HouseTab): string {
  return `${idPrefix}-tab-${tab}`;
}

export function panelId(idPrefix: string, tab: HouseTab): string {
  return `${idPrefix}-panel-${tab}`;
}

/** The tab a key moves to from `current` (wrapping), or `null` for a key the tablist does not use. */
export function tabForKey(key: string, current: HouseTab): HouseTab | null {
  const index = HOUSE_TABS.indexOf(current);
  const last = HOUSE_TABS.length - 1;
  switch (key) {
    case "ArrowRight":
      return HOUSE_TABS[index === last ? 0 : index + 1]!;
    case "ArrowLeft":
      return HOUSE_TABS[index === 0 ? last : index - 1]!;
    case "Home":
      return HOUSE_TABS[0]!;
    case "End":
      return HOUSE_TABS[last]!;
    default:
      return null;
  }
}

/** The tab row, worded by `fit` (the card's last measure; `fitTabs` settles it once it is laid out). `onSelect` gets
 * the tab and whether the keyboard moved there (the card then focuses it again). */
export function tabList(
  doc: Document,
  language: Language,
  selected: HouseTab,
  idPrefix: string,
  onSelect: (tab: HouseTab, byKey: boolean) => void,
  fit: TabFit = "long",
): HTMLElement {
  const list = doc.createElement("div");
  list.className = T.list;
  list.setAttribute("role", "tablist");
  list.setAttribute("aria-label", translate(language, "house.tabs"));
  for (const tab of HOUSE_TABS) {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = T.tab;
    button.id = tabId(idPrefix, tab);
    button.dataset["tab"] = tab;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(tab === selected));
    button.setAttribute("aria-controls", panelId(idPrefix, tab));
    button.tabIndex = tab === selected ? 0 : -1;
    button.addEventListener("click", () => {
      if (tab !== selected) {
        onSelect(tab, false);
      }
    });
    button.addEventListener("keydown", (event) => {
      const next = tabForKey(event.key, tab);
      if (next === null) {
        return;
      }
      event.preventDefault();
      onSelect(next, true);
    });
    list.append(button);
  }
  setTabFit(list, language, fit);
  return list;
}

/** The panel of the selected tab, labelled by its tab. */
export function tabPanel(doc: Document, tab: HouseTab, idPrefix: string, labelled: boolean): HTMLElement {
  const panel = doc.createElement("div");
  panel.className = T.panel;
  panel.id = panelId(idPrefix, tab);
  panel.dataset["tab"] = tab;
  if (labelled) {
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tabId(idPrefix, tab));
  }
  return panel;
}

// ------------------------------------------------------------------------------------------------ the period

/** The period the tabs Energy flow, Energy chart and Price and battery show: its kind and a day inside it, `null`
 * for the one running now. */
export interface PeriodChoice {
  period: HistoryPeriod;
  date: string | null;
}

/** The first day of the period shown (`today` the site's local date). */
export function shownStart(choice: PeriodChoice, today: string): string {
  return periodStart(choice.period, choice.date ?? today);
}

/** `steps` periods from the shown one, never after today's (`null`: today's own). */
export function stepChoice(choice: PeriodChoice, today: string, steps: number): PeriodChoice {
  const target = shiftPeriod(choice.period, shownStart(choice, today), steps);
  return { period: choice.period, date: target >= periodStart(choice.period, today) ? null : target };
}

/** Another kind: the same moment in it (today's when the shown period holds today). */
export function kindChoice(choice: PeriodChoice, period: HistoryPeriod, today: string): PeriodChoice {
  if (choice.date === null) {
    return { period, date: null };
  }
  const start = periodStart(period, choice.date);
  return { period, date: start >= periodStart(period, today) ? null : start };
}

const STEP_KEYS: Record<HistoryPeriod, [TranslationKey, TranslationKey, TranslationKey]> = {
  day: ["hist.previous.day", "hist.next.day", "hist.pick.day"],
  month: ["hist.previous.month", "hist.next.month", "hist.pick.month"],
  year: ["hist.previous.year", "hist.next.year", "hist.pick.year"],
};

const DATE = /^\d{4}-\d{2}-\d{2}$/;

const shortMonths = new Map<string, Intl.DateTimeFormat>();

/** The period in words, the picker's label: "I dag", "I går", a date ("ons 11 nov"), a month ("nov 2026"), a year;
 * the reader's language through `Intl`, never the browser's own date format. */
export function choiceLabel(language: Language, choice: PeriodChoice, today: string): string {
  const shown = shownStart(choice, today);
  if (choice.period === "day") {
    if (shown === today) {
      return translate(language, "house.picker.today");
    }
    if (shown === shiftPeriod("day", today, -1)) {
      return translate(language, "house.picker.yesterday");
    }
    return dateLabel(language, shown);
  }
  if (choice.period === "month") {
    let format = shortMonths.get(language);
    if (format === undefined) {
      format = new Intl.DateTimeFormat(language === "en" ? "en-GB" : language, { month: "short", year: "numeric", timeZone: "UTC" });
      shortMonths.set(language, format);
    }
    return format.format(new Date(Date.UTC(Number(shown.slice(0, 4)), Number(shown.slice(5, 7)) - 1, 15, 12))).replace(".", "");
  }
  return shown.slice(0, 4);
}

/**
 * The one-line picker (see the module): "‹ [label] ›" and Day | Month | Year. The label is a button that opens the
 * native picker of a hidden control (a date input for a day, a list of months or years); `through` ("17:00") is told
 * after today's label ("I dag · till 17:00"). `today` is the site's local date.
 */
export function periodPicker(
  doc: Document,
  language: Language,
  choice: PeriodChoice,
  today: string,
  onChange: (next: PeriodChoice) => void,
  through: string | null = null,
): HTMLElement {
  const wrap = doc.createElement("div");
  wrap.className = T.picker;
  const shown = shownStart(choice, today);
  const [previousKey, nextKey, pickKey] = STEP_KEYS[choice.period];
  const earliest = periodStart(choice.period, periodStart("year", shiftPeriod("year", today, -PICKER_YEARS)));
  const step = (which: "back" | "forward", glyph: string, label: TranslationKey, allowed: boolean, steps: number): HTMLButtonElement => {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = T.pickerStep;
    button.dataset["step"] = which;
    button.textContent = glyph;
    button.setAttribute("aria-label", translate(language, label));
    button.disabled = !allowed;
    button.addEventListener("click", () => onChange(stepChoice(choice, today, steps)));
    return button;
  };
  // The label, and over it (for a month or a year) the list itself, see-through, so a tap opens the native list.
  const holder = doc.createElement("div");
  holder.className = T.pickerDate;
  const label = doc.createElement("button");
  label.type = "button";
  label.className = T.pickerLabel;
  label.dataset["pickLabel"] = "true";
  const words = choiceLabel(language, choice, today);
  const whole = through !== null && choice.period === "day" && choice.date === null ? translate(language, "house.picker.through", { label: words, time: through }) : words;
  if (whole.startsWith(words) && whole.length > words.length) {
    // The period's own words, then how far today's figures reach as a part of its own: where the row has no room
    // for both (a phone) the styles drop that part whole, never cutting the words; the name for a screen reader and
    // the tooltip keep it.
    const main = doc.createElement("span");
    main.textContent = words;
    const reach = doc.createElement("span");
    reach.className = T.pickerReach;
    reach.textContent = whole.slice(words.length);
    label.append(main, reach);
    label.title = whole;
  } else {
    label.textContent = whole;
  }
  label.setAttribute("aria-label", `${translate(language, pickKey)}: ${whole}`);
  holder.append(label);
  if (choice.period === "day") {
    const input = doc.createElement("input");
    input.type = "date";
    input.className = T.pickerHidden;
    input.tabIndex = -1;
    input.setAttribute("aria-hidden", "true");
    input.value = shown;
    input.max = today;
    input.min = earliest;
    input.dataset["pick"] = "day";
    input.addEventListener("change", () => {
      if (DATE.test(input.value) && input.value <= today && input.value >= earliest) {
        onChange({ period: "day", date: input.value === today ? null : input.value });
      }
    });
    label.addEventListener("click", () => {
      try {
        (input as HTMLInputElement & { showPicker?: () => void }).showPicker?.();
      } catch {
        input.focus();
      }
    });
    holder.append(input);
  } else {
    const select = doc.createElement("select");
    select.className = `${T.pickerHidden} ${T.pickerOver}`;
    select.dataset["pick"] = choice.period;
    select.setAttribute("aria-label", translate(language, pickKey));
    const options = choice.period === "month" ? pickerMonths(periodStart("month", today), shown) : pickerYears(periodStart("year", today), shown);
    for (const first of options) {
      const option = doc.createElement("option");
      option.value = first;
      option.textContent = periodLabel(language, choice.period, first);
      option.selected = first === shown;
      select.append(option);
    }
    select.addEventListener("change", () => {
      if (DATE.test(select.value)) {
        onChange({ period: choice.period, date: select.value >= periodStart(choice.period, today) ? null : select.value });
      }
    });
    // The list takes the taps and the keyboard; the label under it only shows the words.
    label.tabIndex = -1;
    label.setAttribute("aria-hidden", "true");
    holder.append(select);
  }
  const before = shiftPeriod(choice.period, shown, -1);
  const after = shiftPeriod(choice.period, shown, 1);
  const row = doc.createElement("div");
  row.className = T.pickerRow;
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", translate(language, "hist.period.label"));
  row.append(step("back", "‹", previousKey, before >= earliest, -1), holder, step("forward", "›", nextKey, after <= today, 1));
  // Day | Month | Year, one pressed.
  const kinds = doc.createElement("div");
  kinds.className = T.pickerKinds;
  kinds.setAttribute("role", "group");
  kinds.setAttribute("aria-label", translate(language, "hist.period.label"));
  for (const period of HISTORY_PERIODS) {
    const button = doc.createElement("button");
    button.type = "button";
    button.dataset["kind"] = period;
    button.textContent = translate(language, `hist.period.${period}`);
    button.setAttribute("aria-pressed", String(period === choice.period));
    button.addEventListener("click", () => {
      if (period !== choice.period) {
        onChange(kindChoice(choice, period, today));
      }
    });
    kinds.append(button);
  }
  wrap.append(row, kinds);
  return wrap;
}

export const TAB_STYLES = `
  .${T.list} {
    display: flex;
    gap: 2px;
    margin: 10px 0 0;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
  }
  /* A tab is one line, never narrower than its words: a row too wide for the card is worded shorter (\`fitTabs\`),
     so every tab stays in view without a scroll and no word is broken. */
  .${T.tab} {
    flex: 1 0 auto;
    display: inline-flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 40px;
    padding: 6px 7px;
    font: inherit;
    font-size: 0.8rem;
    line-height: 1.2;
    white-space: nowrap;
    color: var(--secondary-text-color, #727272);
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    cursor: pointer;
  }
  .${T.tab}[aria-selected="true"] {
    color: var(--primary-text-color, #212121);
    font-weight: 500;
    border-bottom-color: var(--primary-color, #03a9f4);
  }
  /* As wide as its selected form, unseen (see \`setTabFit\`). */
  .${T.tab}::after {
    content: attr(data-text);
    height: 0;
    overflow: hidden;
    visibility: hidden;
    font-weight: 500;
    pointer-events: none;
    user-select: none;
  }
  .${T.tabIcon} {
    display: block;
    fill: none;
    stroke: currentColor;
    stroke-width: 1.8;
    stroke-linecap: round;
    stroke-linejoin: round;
  }
  .${T.list}[data-fit="icons"] .${T.tab} {
    flex: 1 1 0;
    min-width: 0;
    padding: 6px 0;
  }
  .${T.tab}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: -2px;
    border-radius: 6px;
  }
  .${T.panel} {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-top: 10px;
    min-width: 0;
  }
  .${T.picker} {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .${T.pickerRow} {
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    gap: 2px;
    min-width: 0;
  }
  .${T.pickerStep} {
    flex: 0 0 34px;
    width: 34px;
    height: 34px;
    padding: 0;
    font: inherit;
    font-size: 1.3rem;
    line-height: 1;
    color: var(--primary-text-color, #212121);
    background: transparent;
    border: 0;
    border-radius: 50%;
    cursor: pointer;
  }
  .${T.pickerStep}:hover:not(:disabled) { background: var(--secondary-background-color, #eee); }
  .${T.pickerStep}:disabled { opacity: 0.3; cursor: default; }
  .${T.pickerDate} {
    position: relative;
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    justify-content: center;
  }
  .${T.pickerLabel} {
    max-width: 100%;
    height: 34px;
    padding: 0 8px;
    font: inherit;
    font-weight: 500;
    line-height: 34px;
    color: var(--primary-text-color, #212121);
    background: transparent;
    border: 0;
    border-radius: 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;
  }
  /* The label's parts sit on one line of the label's height; a part with no room on it (how far today's figures
     reach, on a phone) wraps to a second line, which is out of sight: dropped whole, never cut. */
  .${T.pickerLabel}:has(.${T.pickerReach}) {
    display: inline-flex;
    flex-wrap: wrap;
    justify-content: center;
    white-space: pre;
  }
  .${T.pickerLabel} > span {
    flex: none;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .${T.pickerLabel}:hover { background: var(--secondary-background-color, #eee); }
  /* The native control: out of sight under the label (a day's date input, opened by the label), or over it,
     see-through (a month's or a year's list), so a tap opens it and the keyboard reaches it. */
  .${T.pickerHidden} {
    position: absolute;
    left: 50%;
    bottom: 0;
    width: 1px;
    height: 1px;
    opacity: 0;
    border: 0;
    padding: 0;
    pointer-events: none;
  }
  .${T.pickerOver} {
    left: 0;
    top: 0;
    width: 100%;
    height: 100%;
    font: inherit;
    cursor: pointer;
    pointer-events: auto;
  }
  .${T.pickerKinds} {
    flex: 0 0 auto;
    display: inline-flex;
    padding: 2px;
    gap: 2px;
    border-radius: 9px;
    background: var(--secondary-background-color, #eee);
  }
  .${T.pickerKinds} > button {
    min-height: 30px;
    padding: 0 7px;
    font: inherit;
    font-size: 0.85rem;
    color: var(--secondary-text-color, #727272);
    background: transparent;
    border: 0;
    border-radius: 7px;
    cursor: pointer;
  }
  .${T.pickerKinds} > button[aria-pressed="true"] {
    color: var(--primary-text-color, #212121);
    background: var(--ha-card-background, var(--card-background-color, #fff));
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.18);
    font-weight: 500;
  }
  .${T.picker} button:focus-visible, .${T.pickerOver}:focus-visible + *, .${T.pickerOver}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
  .${T.pickerDate}:has(.${T.pickerOver}:focus-visible) .${T.pickerLabel} {
    outline: 2px solid var(--primary-color, #03a9f4);
  }
`;
