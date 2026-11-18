// The heat card's tabs (SpotNav 3.0): Now, Plan and Use, in that fixed order, on the house card's tab row
// (`house-tabs.ts`: its classes, styles and period picker).
//
// * A tab says its name while the whole row fits the card, else an icon alone; the card measures the row as drawn
//   (`fitHeatTabs`). Whatever is drawn, a tab's accessible name is its name, and an icon has it as its tooltip too.
// * The row is a WAI-ARIA tablist with automatic activation: the arrow keys move to the previous or next tab (and
//   wrap), Home and End to the first and last; only the selected tab is in the page's tab order.
// * The tab chosen is remembered per card in this browser (by the site or charger the card names), guarded like the
//   chart preferences: a storage that is missing or throws means the first tab.
// * The card's YAML may lock one tab (`tab: now | plan | use`): the row is then not drawn and the choice is neither
//   read nor written.
// * The Use tab's unit (kWh or money) and the parts hidden in its legend are remembered the same way.

import { browserStore, type PreferenceStore } from "./chart-preference";
import { TAB_CLASSES as T } from "./house-tabs";
import { translate, type Language, type TranslationKey } from "./i18n";

export type HeatTab = "now" | "plan" | "use";
export const HEAT_TABS: readonly HeatTab[] = ["now", "plan", "use"];

/** How a tab is worded: its name, or its icon alone. */
export type HeatTabFit = "long" | "icons";

export type UseUnit = "kwh" | "money";

export function isHeatTab(value: unknown): value is HeatTab {
  return typeof value === "string" && (HEAT_TABS as readonly string[]).includes(value);
}

export function heatTabKey(target: string): string {
  return `spotnav-heat-card.tab.${target}`;
}

export function useUnitKey(target: string): string {
  return `spotnav-heat-card.use.unit.${target}`;
}

export function useHiddenKey(target: string): string {
  return `spotnav-heat-card.use.hidden.${target}`;
}

function readText(key: string, store: () => PreferenceStore | null): string | null {
  try {
    return store()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function writeText(key: string, value: string, store: () => PreferenceStore | null): void {
  try {
    store()?.setItem(key, value);
  } catch {
    // Nothing is remembered in this browser; the choice holds while the card lives.
  }
}

/** The tab this browser last showed on the card naming `target`; `null` when nothing usable is stored. */
export function readHeatTab(target: string, store: () => PreferenceStore | null = browserStore): HeatTab | null {
  const value = readText(heatTabKey(target), store);
  return isHeatTab(value) ? value : null;
}

export function writeHeatTab(target: string, tab: HeatTab, store: () => PreferenceStore | null = browserStore): void {
  writeText(heatTabKey(target), tab, store);
}

/** The Use tab's unit as this browser last left it; kWh when nothing usable is stored. */
export function readUseUnit(target: string, store: () => PreferenceStore | null = browserStore): UseUnit {
  return readText(useUnitKey(target), store) === "money" ? "money" : "kwh";
}

export function writeUseUnit(target: string, unit: UseUnit, store: () => PreferenceStore | null = browserStore): void {
  writeText(useUnitKey(target), unit, store);
}

/** The Use tab's parts this browser last hid (their keys); none when nothing usable is stored. */
export function readUseHidden(target: string, store: () => PreferenceStore | null = browserStore): string[] {
  try {
    const raw = readText(useHiddenKey(target), store);
    const parsed: unknown = raw === null ? null : JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string" && item.length <= 200).slice(0, 20) : [];
  } catch {
    return [];
  }
}

export function writeUseHidden(target: string, hidden: readonly string[], store: () => PreferenceStore | null = browserStore): void {
  writeText(useHiddenKey(target), JSON.stringify(hidden), store);
}

const TAB_WORDS: Record<HeatTab, TranslationKey> = {
  now: "heat.tab.now",
  plan: "heat.tab.plan",
  use: "heat.tab.use",
};

export function heatTabWord(language: Language, tab: HeatTab): string {
  return translate(language, TAB_WORDS[tab]);
}

/** The tabs' icons, stroked, in a 24-unit box: a thermometer, a curve over a baseline, bars. */
const TAB_ICONS: Record<HeatTab, string[]> = {
  now: ["M10 13.6V5a2 2 0 0 1 4 0v8.6a4 4 0 1 1-4 0z", "M12 9v8"],
  plan: ["M3 20.5h18", "M3.5 15c3 0 3.5-8 7-8s3 6 6 6 2.500-3 4-3"],
  use: ["M3 20.5h18", "M6.5 20.5v-8M12 20.5V4.5M17.5 20.5v-11"],
};

const SVG_NS = "http://www.w3.org/2000/svg";

function tabIcon(doc: Document, tab: HeatTab): SVGSVGElement {
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

/** Word a drawn tab row another way: each tab's content and tooltip, its accessible name kept. */
export function setHeatTabFit(list: HTMLElement, language: Language, fit: HeatTabFit): void {
  list.dataset["fit"] = fit;
  for (const button of Array.from(list.querySelectorAll<HTMLElement>("[role='tab']"))) {
    const tab = button.dataset["tab"];
    if (!isHeatTab(tab)) {
      continue;
    }
    const name = heatTabWord(language, tab);
    button.setAttribute("aria-label", name);
    if (fit === "icons") {
      button.replaceChildren(tabIcon(button.ownerDocument, tab));
      delete button.dataset["text"];
      button.title = name;
    } else {
      button.textContent = name;
      // The words once more for the styles, which hold each tab as wide as its selected (bolder) form.
      button.dataset["text"] = name;
      button.removeAttribute("title");
    }
  }
}

/** The names where the drawn row has room for them, else icons (a row not laid out, 0 px wide, keeps its names). */
export function fitHeatTabs(list: HTMLElement, language: Language): HeatTabFit {
  if (list.dataset["fit"] !== "long") {
    setHeatTabFit(list, language, "long");
  }
  if (list.scrollWidth <= list.clientWidth + 1) {
    return "long";
  }
  setHeatTabFit(list, language, "icons");
  return "icons";
}

export function heatTabId(idPrefix: string, tab: HeatTab): string {
  return `${idPrefix}-tab-${tab}`;
}

export function heatPanelId(idPrefix: string, tab: HeatTab): string {
  return `${idPrefix}-panel-${tab}`;
}

/** The tab a key moves to from `current` (wrapping), or `null` for a key the tablist does not use. */
export function heatTabForKey(key: string, current: HeatTab): HeatTab | null {
  const index = HEAT_TABS.indexOf(current);
  const last = HEAT_TABS.length - 1;
  switch (key) {
    case "ArrowRight":
      return HEAT_TABS[index === last ? 0 : index + 1]!;
    case "ArrowLeft":
      return HEAT_TABS[index === 0 ? last : index - 1]!;
    case "Home":
      return HEAT_TABS[0]!;
    case "End":
      return HEAT_TABS[last]!;
    default:
      return null;
  }
}

/** The tab row. `onSelect` gets the tab and whether the keyboard moved there (the card then focuses it again). */
export function heatTabList(
  doc: Document,
  language: Language,
  selected: HeatTab,
  idPrefix: string,
  onSelect: (tab: HeatTab, byKey: boolean) => void,
  fit: HeatTabFit = "long",
): HTMLElement {
  const list = doc.createElement("div");
  list.className = T.list;
  list.setAttribute("role", "tablist");
  list.setAttribute("aria-label", translate(language, "heat.tabs"));
  for (const tab of HEAT_TABS) {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = T.tab;
    button.id = heatTabId(idPrefix, tab);
    button.dataset["tab"] = tab;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(tab === selected));
    button.setAttribute("aria-controls", heatPanelId(idPrefix, tab));
    button.tabIndex = tab === selected ? 0 : -1;
    button.addEventListener("click", () => {
      if (tab !== selected) {
        onSelect(tab, false);
      }
    });
    button.addEventListener("keydown", (event) => {
      const next = heatTabForKey(event.key, tab);
      if (next === null) {
        return;
      }
      event.preventDefault();
      onSelect(next, true);
    });
    list.append(button);
  }
  setHeatTabFit(list, language, fit);
  return list;
}

/** The panel of the selected tab, labelled by its tab (a locked tab has no row to label it). */
export function heatTabPanel(doc: Document, tab: HeatTab, idPrefix: string, labelled: boolean): HTMLElement {
  const panel = doc.createElement("div");
  panel.className = T.panel;
  panel.id = heatPanelId(idPrefix, tab);
  panel.dataset["tab"] = tab;
  if (labelled) {
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", heatTabId(idPrefix, tab));
  }
  return panel;
}
