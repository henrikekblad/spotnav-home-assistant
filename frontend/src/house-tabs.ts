// The house card's tabs (SpotNav 3.0): Now, Energy flow, Energy chart, Price and battery, in that fixed order.
//
// * The row is a WAI-ARIA tablist with automatic activation: the arrow keys move to the previous or next tab (and
//   wrap), Home and End to the first and last, and the tab moved to is shown at once; only the selected tab is in
//   the page's tab order (a roving tabindex), the panel follows it.
// * The tab chosen is remembered per card in this browser (by the site or charger the card names), guarded like the
//   chart preferences (`chart-preference.ts`): a storage that is missing or throws means the first tab.
// * The card's YAML may lock one tab (`tab: now | flow | graph | price`, the card editor's "Show only tab"): the row is
//   then not drawn and the choice is neither read nor written.
// * The period picker the tabs Energy flow, Energy chart and Price and battery share: "‹ 2026-11-17 › [Day ▾]", the
//   kind as a select, the arrows one period back or forward, the date (a day) or the month or year (a select) tapped
//   to choose; nothing after today is offered.

import { browserStore, type PreferenceStore } from "./chart-preference";
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

export type HouseTab = "now" | "flow" | "graph" | "price";
export const HOUSE_TABS: readonly HouseTab[] = ["now", "flow", "graph", "price"];

export const TAB_CLASSES = {
  list: "spotnav-house-tabs",
  tab: "spotnav-house-tab",
  panel: "spotnav-house-panel",
  picker: "spotnav-house-picker",
  pickerDate: "spotnav-house-picker-date",
  pickerKind: "spotnav-house-picker-kind",
  pickerStep: "spotnav-house-picker-step",
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
  price: "house.tab.price",
};

export function tabWord(language: Language, tab: HouseTab): string {
  return translate(language, TAB_WORDS[tab]);
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

/** The tab row. `onSelect` gets the tab and whether the keyboard moved there (the card then focuses it again). */
export function tabList(
  doc: Document,
  language: Language,
  selected: HouseTab,
  idPrefix: string,
  onSelect: (tab: HouseTab, byKey: boolean) => void,
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
    button.textContent = tabWord(language, tab);
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

/** The one-line picker (see the module). `today` is the site's local date. */
export function periodPicker(doc: Document, language: Language, choice: PeriodChoice, today: string, onChange: (next: PeriodChoice) => void): HTMLElement {
  const row = doc.createElement("div");
  row.className = T.picker;
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", translate(language, "hist.period.label"));
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
  let picker: HTMLElement;
  if (choice.period === "day") {
    const input = doc.createElement("input");
    input.type = "date";
    input.className = T.pickerDate;
    input.value = shown;
    input.max = today;
    input.min = earliest;
    input.dataset["pick"] = "day";
    input.setAttribute("aria-label", translate(language, pickKey));
    input.addEventListener("change", () => {
      if (DATE.test(input.value) && input.value <= today && input.value >= earliest) {
        onChange({ period: "day", date: input.value === today ? null : input.value });
      }
    });
    picker = input;
  } else {
    const select = doc.createElement("select");
    select.className = T.pickerDate;
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
    picker = select;
  }
  const kind = doc.createElement("select");
  kind.className = T.pickerKind;
  kind.dataset["pickKind"] = "true";
  kind.setAttribute("aria-label", translate(language, "hist.period.label"));
  for (const period of HISTORY_PERIODS) {
    const option = doc.createElement("option");
    option.value = period;
    option.textContent = translate(language, `hist.period.${period}`);
    option.selected = period === choice.period;
    kind.append(option);
  }
  kind.addEventListener("change", () => {
    const period = kind.value as HistoryPeriod;
    if ((HISTORY_PERIODS as readonly string[]).includes(period) && period !== choice.period) {
      onChange(kindChoice(choice, period, today));
    }
  });
  const before = shiftPeriod(choice.period, shown, -1);
  const after = shiftPeriod(choice.period, shown, 1);
  row.append(step("back", "‹", previousKey, before >= earliest, -1), picker, step("forward", "›", nextKey, after <= today, 1), kind);
  return row;
}

export const TAB_STYLES = `
  .${T.list} {
    display: flex;
    gap: 2px;
    margin: 10px 0 0;
    border-bottom: 1px solid var(--divider-color, #e0e0e0);
    overflow-x: auto;
    scrollbar-width: none;
  }
  .${T.list}::-webkit-scrollbar { display: none; }
  /* A tab shrinks to its longest word and wraps there, so a long language's four tabs stay in view at 360 px. */
  .${T.tab} {
    flex: 1 1 auto;
    min-height: 40px;
    padding: 6px 6px;
    font: inherit;
    font-size: 0.85rem;
    line-height: 1.2;
    white-space: normal;
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
    gap: 6px;
    min-width: 0;
  }
  .${T.picker} > * {
    min-height: 36px;
    box-sizing: border-box;
    font: inherit;
    font-size: 0.9rem;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    color-scheme: light dark;
  }
  .${T.pickerStep} {
    flex: 0 0 36px;
    width: 36px;
    padding: 0;
    font-size: 1.2rem;
    cursor: pointer;
  }
  .${T.pickerStep}:disabled { opacity: 0.4; cursor: default; }
  .${T.pickerDate} {
    flex: 1 1 auto;
    min-width: 0;
    padding: 0 8px;
    text-align: center;
  }
  .${T.pickerKind} {
    flex: 0 0 auto;
    padding: 0 6px;
  }
  .${T.picker} > *:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 1px;
  }
`;
