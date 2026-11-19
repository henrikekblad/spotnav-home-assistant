// The Settings page's price and solar sections, shared by the charger card's Settings (`card-view.ts`) and the
// site's settings popover (`settings-popover.ts`, the house and heat cards): the electricity price the charger or
// site reads (its line, the same-area question, "Price from"), the area, where the prices come from, the three fees
// with the grid tariff's rows beside the grid fee, the supplier's two figures (the sold kWh only where something is
// sold) and the demand charge; and the solar forecast sources.
//
// Nothing here talks to the backend. The host draws its own rows and editors (`row`, `openValueEditor`, ...) and
// makes each write (`write`), so a charger writes through its own commands and a site through the site-keyed ones.

import type { FormatContext } from "./format";
import { numberWord, translate, type Language, type TranslationKey } from "./i18n";
import { marketAreaLabel } from "./market";
import type { PriceEntryFacts } from "./model";
import {
  gridTariffRows,
  highLoadEditor,
  supplierRows,
  withComponent,
  withHighLoad,
  withMarkup,
  withoutHighLoad,
  withPercent,
  withSellAdjustment,
  SUPPLIER_MAX,
  type GridTariffRowKey,
  type SupplierRow,
} from "./grid-tariff";
import { componentEditor, demandRows } from "./demand-charge";
import { keyEditor, priceProviderNodes } from "./price-provider";
import { fiscalRows } from "./settings";
import type { Fiscal, GridTariffBlock, PriceProvider } from "./validate";
import { sectionHeading, type ChoiceOption, type EditorHandlers, type NumberEditorInput } from "./value-editors";
import type { FiscalComponentName, ValueWrite } from "./value-writes";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

/** The writes the price section makes. */
export type PriceWrite = Extract<ValueWrite, { kind: "gridTariff" | "priceSource" | "priceDuplicate" | "entsoeKey" }>;

/** How the host draws rows and opens editors, the same in both. */
export interface SectionHost {
  doc: Document;
  language: Language;
  idPrefix: string;
  isAdmin: boolean;
  /** One summary row; `onTap` makes it changeable (for an administrator). */
  row(key: string, label: string, value: string, onTap?: () => void): HTMLElement;
  /** A line of help under a row. */
  rowHelp(key: string, text: string): HTMLElement;
  openValueEditor(title: string, build: (handlers: EditorHandlers) => HTMLFormElement): void;
  editNumber(
    title: string,
    spec: Omit<NumberEditorInput, "idPrefix" | "rangeMessage">,
    save: (value: number | null) => Promise<string | null>,
  ): void;
  editSingle(
    title: string,
    options: readonly ChoiceOption[],
    selected: string | null,
    save: (value: string) => Promise<string | null>,
    intro?: string,
  ): void;
  editMulti(
    title: string,
    options: readonly ChoiceOption[],
    checked: readonly string[],
    save: (values: string[]) => Promise<string | null>,
  ): void;
}

/** What the price section shows: the dashboard's price blocks, for a charger or read for a site. */
export interface PriceFacts {
  format: FormatContext;
  priceEntry: PriceEntryFacts | null;
  gridTariff: GridTariffBlock | null;
  fiscal: Fiscal | null;
  provider: PriceProvider | null;
  areaId: string | null;
  areaName: string | null;
  /** The "Price from" help: a charger without a site points itself, otherwise the site points. */
  onSite: boolean;
}

export interface PriceSectionInput extends SectionHost {
  facts: PriceFacts;
  write(write: PriceWrite): Promise<string | null>;
  /** The area's own dialog, with the catalogue. */
  openArea(): void;
  /** One fee's editor (it needs the area's suggestion, which the host reads). */
  editFiscal(component: FiscalComponentName): void;
  hasComponent(domain: string): boolean;
  navigate(path: string): void;
  openExternal(url: string): void;
}

/**
 * The price: where it lives (the electricity price entry and how many chargers use it), the same-area question,
 * the choice of entry when there are several, then the area (its own dialog, with the catalogue) and each fee
 * in its own number editor.
 */
export function priceSectionBody(input: PriceSectionInput): HTMLElement {
  const { doc, language, facts } = input;
  const section = element(doc, "section", C.settingsSection);
  section.dataset["section"] = "market";
  section.append(sectionHeading(doc, "price", translate(language, "settings.heading.price")));
  section.append(...priceEntryNodes(input));
  const area = marketAreaLabel(language, facts.areaName, facts.areaId);
  // The area opens its own dialog, with the catalogue, for an administrator; anyone else reads it, as every other value.
  const areaLabel = translate(language, "context.area");
  const areaValue = area || translate(language, "settings.value.unset");
  const areaRow = input.row("area", areaLabel, areaValue, () => input.openArea());
  const areaButton = areaRow.querySelector<HTMLElement>("button");
  if (areaButton !== null) {
    areaButton.classList.add(C.settingsTrigger);
    areaButton.dataset["setting"] = "market";
  }
  section.append(areaRow);
  // Where the area's prices come from, and what an administrator can do when it has no source of its own.
  section.append(
    ...priceProviderNodes(doc, {
      language,
      provider: facts.provider,
      areaLabel: area || (facts.areaId ?? ""),
      areaId: facts.areaId,
      isAdmin: input.isAdmin,
      idPrefix: input.idPrefix,
      hasComponent: (domain) => input.hasComponent(domain),
      navigate: (path) => input.navigate(path),
      openExternal: (url) => input.openExternal(url),
      openArea: () => input.openArea(),
      openKeyEditor: () =>
        input.openValueEditor(translate(language, "price.key.title"), (handlers) =>
          keyEditor(
            doc,
            language,
            input.idPrefix,
            (key) => input.write({ kind: "entsoeKey", key, areaId: facts.areaId }),
            handlers,
          ),
        ),
    }),
  );
  // The fees that are set, in their usual order; the ones that are off behind "More fees", so a price with VAT, tax and
  // a grid fee is a short list.
  const more: HTMLElement[] = [];
  const place = (nodes: HTMLElement[], set: boolean): void => {
    if (set) {
      section.append(...nodes);
    } else {
      more.push(...nodes);
    }
  };
  const block = facts.gridTariff;
  for (const fiscal of fiscalRows(language, facts.fiscal)) {
    const component = fiscal.key as FiscalComponentName;
    const included = facts.fiscal?.[component].policy === "included";
    section.append(input.row(fiscal.key, fiscal.label, fiscal.value, included ? undefined : () => input.editFiscal(component)));
    if (component === "transfer") {
      for (const node of gridTariffNodes(input)) {
        const key = node.dataset["row"] ?? "";
        place([node], block === null || feeSet(key === "transfer_percent" ? block.transfer_percent : block.transfer_high));
      }
    }
  }
  for (const node of supplierNodes(input)) {
    const fee = node.dataset["row"] === "supplier_markup" ? block?.supplier_markup : block?.sell_adjustment;
    place([node], fee == null || feeSet(fee));
  }
  place(demandNodes(input), (block?.demand?.components.length ?? 0) > 0);
  if (more.length > 0) {
    const details = element(doc, "details", C.settingsMore);
    details.dataset["more"] = "fees";
    const summary = element(doc, "summary", C.linkButton, translate(language, "price.moreFees"));
    details.append(summary, ...more);
    section.append(details);
  }
  return section;
}

/** A fee counts as set while it is on (or held in the area's price); off, it waits under "More fees". */
function feeSet(fee: { policy: string; effective_value: number | null }): boolean {
  return fee.policy === "included" || (fee.policy !== "off" && fee.effective_value !== null);
}

/**
 * The demand charge's rows below the fees: "None" until a component is added, then each component with its
 * prices and this month's mean and peaks beneath, and a row adding another (at most three).
 */
function demandNodes(input: PriceSectionInput): HTMLElement[] {
  const block = input.facts.gridTariff;
  if (block === null || block.demand === null) {
    return [];
  }
  const nodes: HTMLElement[] = [];
  for (const row of demandRows(input.facts.format, block.demand)) {
    const key = row.key === "add" ? "demand_add" : `demand_${row.key}`;
    const index = row.key;
    nodes.push(input.row(key, row.label, row.value, block.writable ? () => editDemandComponent(input, block, index) : undefined));
    row.lines.forEach((line, at) => nodes.push(input.rowHelp(`${key}_${at}`, line)));
  }
  return nodes;
}

/** One demand-charge component's editor (`add` adds one); Save writes the tariff with it, Remove without it. */
function editDemandComponent(input: PriceSectionInput, block: GridTariffBlock, key: number | "add"): void {
  const components = block.demand?.components ?? [];
  const index = key === "add" ? components.length : key;
  input.openValueEditor(translate(input.language, "demand.title"), (handlers) =>
    componentEditor(
      input.doc,
      input.language,
      { current: key === "add" ? null : (components[key] ?? null), idPrefix: input.idPrefix, unit: block.demand?.price_unit ?? null },
      (component) => input.write({ kind: "gridTariff", change: (current) => withComponent(current, index, component) }),
      handlers,
    ),
  );
}

/**
 * The grid tariff's rows beside the grid fee: the percentage of spot, the fee in high-load hours and, while that
 * fee is on or hours are stored, the hours. Locked where the price already includes the grid fee, and read-only
 * for a reader who may not change the electricity price.
 */
function gridTariffNodes(input: PriceSectionInput): HTMLElement[] {
  const block = input.facts.gridTariff;
  if (block === null) {
    return [];
  }
  const edit: Record<GridTariffRowKey, () => void> = {
    transfer_percent: () => editGridPercent(input, block),
    transfer_high: () => editGridHigh(input, block),
    high_load_window: () => editGridHigh(input, block),
  };
  return gridTariffRows(input.language, block).map((row) =>
    input.row(row.key, row.label, row.value, row.included || !block.writable ? undefined : edit[row.key]),
  );
}

/**
 * The electricity supplier's two figures below the fees: the markup on each bought kWh and what a sold kWh earns
 * beyond spot. Read-only for a reader who may not change the electricity price.
 */
function supplierNodes(input: PriceSectionInput): HTMLElement[] {
  const block = input.facts.gridTariff;
  if (block === null) {
    return [];
  }
  return supplierRows(input.language, block).map((row) =>
    input.row(row.key, row.label, row.value, block.writable ? () => editSupplier(input, block, row) : undefined),
  );
}

/** One supplier figure in a number editor with its line of help; "Off" keeps the figure for next time. */
function editSupplier(input: PriceSectionInput, block: GridTariffBlock, row: SupplierRow): void {
  const markup = row.key === "supplier_markup";
  const fee = markup ? block.supplier_markup : block.sell_adjustment;
  if (fee === null) {
    return;
  }
  input.editNumber(
    row.label,
    {
      help: row.help,
      unit: fee.unit ?? "",
      current: fee.effective_value,
      prefill: fee.explicit_value,
      // What a sold kWh earns may be a deduction; the markup is a cost.
      min: markup ? 0 : -SUPPLIER_MAX,
      max: SUPPLIER_MAX,
      decimals: 2,
      noneLabel: translate(input.language, "settings.value.off"),
    },
    (value) =>
      input.write({
        kind: "gridTariff",
        change: (current) => (markup ? withMarkup(current, value) : withSellAdjustment(current, value)),
      }),
  );
}

function editGridPercent(input: PriceSectionInput, block: GridTariffBlock): void {
  const fee = block.transfer_percent;
  input.editNumber(
    translate(input.language, "gridFee.percent"),
    {
      help: translate(input.language, "gridFee.percent.help"),
      unit: fee.unit ?? "%",
      current: fee.effective_value,
      prefill: fee.explicit_value,
      min: 0,
      max: 100,
      decimals: 2,
      noneLabel: translate(input.language, "settings.value.off"),
    },
    (value) => input.write({ kind: "gridTariff", change: (current) => withPercent(current, value) }),
  );
}

/**
 * The grid fee in high-load hours and its hours, in one dialog (`highLoadEditor`), from either row: Save writes the
 * fee and the hours in one write, Remove turns the fee off and clears the hours in one write.
 */
function editGridHigh(input: PriceSectionInput, block: GridTariffBlock): void {
  input.openValueEditor(translate(input.language, "gridFee.high"), (handlers) =>
    highLoadEditor(
      input.doc,
      input.language,
      { fee: block.transfer_high, window: block.high_load_window, idPrefix: input.idPrefix },
      (choice) =>
        input.write({
          kind: "gridTariff",
          change: (current) =>
            choice === null ? withoutHighLoad(current) : withHighLoad(current, choice.value, choice.window),
        }),
      handlers,
    ),
  );
}

/**
 * An electricity price's name as shown: the name SpotNav gives one ("Electricity price SE4", or "Electricity price
 * SE4 (Home)" beside another for the same area) in the reader's language, the part in brackets as it is.
 */
export function priceTitle(language: Language, title: string): string {
  const given = /^Electricity price ([A-Za-z0-9-]+)( \(.+\))?$/.exec(title);
  return given === null ? title : `${translate(language, "price.title", { area: given[1]! })}${given[2] ?? ""}`;
}

/**
 * Each electricity price's name for a list of them. Two with the same name (as SpotNav names them) are told apart:
 * the one this charger uses says so, the other is "the other".
 */
function priceLabels(
  language: Language,
  entries: ReadonlyArray<{ entryId: string; title: string }>,
  current: string,
): Map<string, string> {
  const labels = new Map<string, string>();
  const shown = entries.map((entry) => ({ entryId: entry.entryId, title: priceTitle(language, entry.title) }));
  for (const entry of shown) {
    const same = shown.filter((other) => other.title === entry.title);
    if (same.length < 2) {
      labels.set(entry.entryId, entry.title);
    } else if (entry.entryId === current) {
      labels.set(entry.entryId, translate(language, "price.choice.here", { title: entry.title }));
    } else {
      const others = same.filter((other) => other.entryId !== current);
      const label = translate(language, "price.choice.other", { title: entry.title });
      labels.set(entry.entryId, others.length > 1 ? `${label} ${others.indexOf(entry) + 1}` : label);
    }
  }
  return labels;
}

/** The electricity price entry's line, the shared note, the same-area question and the "Price from" choice. */
function priceEntryNodes(input: PriceSectionInput): HTMLElement[] {
  const { doc, language } = input;
  const entry = input.facts.priceEntry;
  if (entry === null) {
    return [];
  }
  const nodes: HTMLElement[] = [];
  const count = entry.chargerCount;
  const title = priceTitle(language, entry.title);
  const sourceText =
    count <= 1
      ? title
      : translate(language, count >= entry.chargerTotal ? "price.source.usedByAll" : "price.source.usedBy", {
          title,
          countWord: numberWord(language, count),
        });
  // The entry's own name only says something when other chargers share it; alone it repeats the area.
  if (count > 1) {
    const sourceLine = element(doc, "p", C.siteApplies, sourceText);
    sourceLine.dataset["priceSource"] = entry.entryId;
    nodes.push(sourceLine);
  }
  if (count > 1) {
    const shared = element(doc, "p", C.siteApplies, translate(language, "price.shared"));
    shared.dataset["priceShared"] = "";
    nodes.push(shared);
  }
  if (input.isAdmin && entry.writable && entry.duplicate !== null) {
    nodes.push(priceDuplicateQuestion(input, entry.duplicate, entry.entryId));
  }
  if (entry.choices.length > 1) {
    const label = translate(language, "price.from.label");
    const labels = priceLabels(language, entry.choices, entry.entryId);
    const options: ChoiceOption[] = entry.choices.map((choice) => ({
      value: choice.entryId,
      label: labels.get(choice.entryId) ?? choice.title,
    }));
    const help = translate(language, input.facts.onSite ? "price.from.helpSite" : "price.from.help");
    nodes.push(
      input.row(
        "price_from",
        label,
        title,
        entry.writable
          ? () =>
              input.editSingle(
                label,
                options,
                entry.entryId,
                (chosen) =>
                  chosen === entry.entryId ? Promise.resolve(null) : input.write({ kind: "priceSource", priceEntryId: chosen }),
                help,
              )
          : undefined,
      ),
    );
  }
  return nodes;
}

/**
 * "Two electricity prices for SE4 with different fees. Use one?": one button per entry and one to keep them all.
 * Answered once; nothing is removed.
 */
function priceDuplicateQuestion(
  input: PriceSectionInput,
  duplicate: NonNullable<PriceEntryFacts["duplicate"]>,
  current: string,
): HTMLElement {
  const { doc, language } = input;
  const question = element(doc, "div", C.suggestion);
  question.dataset["banner"] = "price";
  question.dataset["priceDuplicate"] = duplicate.areaId;
  const many = duplicate.entries.length > 2;
  question.append(
    element(
      doc,
      "p",
      C.suggestionText,
      translate(language, many ? "price.duplicate.questionMany" : "price.duplicate.question", {
        area: duplicate.areaId,
        countWord: numberWord(language, duplicate.entries.length, true),
      }),
    ),
  );
  const answers = element(doc, "div", C.suggestionAnswers);
  const failure = element(doc, "p", C.settingsError);
  failure.hidden = true;
  failure.setAttribute("role", "alert");
  const buttons: HTMLButtonElement[] = [];
  const choose = (keepEntryId: string | null): void => {
    for (const button of buttons) {
      button.disabled = true;
    }
    failure.hidden = true;
    void input.write({ kind: "priceDuplicate", areaId: duplicate.areaId, keepEntryId }).then((sentence) => {
      if (sentence !== null) {
        failure.textContent = sentence;
        failure.hidden = false;
        for (const button of buttons) {
          button.disabled = false;
        }
      }
    });
  };
  const answer = (text: string, keepEntryId: string | null, fees: string | null = null): void => {
    const button = element(doc, "button", C.choiceButton);
    button.append(element(doc, "span", C.choiceLabel, text));
    if (fees !== null) {
      const detail = element(doc, "span", C.choiceDetail, fees);
      detail.dataset["fees"] = "";
      button.append(detail);
    }
    button.type = "button";
    button.dataset["keep"] = keepEntryId ?? "";
    button.addEventListener("click", () => choose(keepEntryId));
    buttons.push(button);
    answers.append(button);
  };
  const labels = priceLabels(language, duplicate.entries, current);
  for (const choice of duplicate.entries) {
    answer(
      translate(language, "price.duplicate.use", { title: labels.get(choice.entryId) ?? choice.title }),
      choice.entryId,
      priceFeesLine(language, choice.fees),
    );
  }
  answer(translate(language, many ? "price.duplicate.keepAll" : "price.duplicate.keepBoth"), null);
  question.append(answers, failure);
  return question;
}

/**
 * One electricity price's fees on a line, "VAT 25 % · tax 36 öre/kWh · grid 30 öre/kWh", each figure as the fee
 * rows show it; `null` when they cannot be read.
 */
function priceFeesLine(language: Language, fees: Fiscal | null): string | null {
  if (fees === null) {
    return null;
  }
  const keys = {
    vat: "price.duplicate.fee.vat",
    tax: "price.duplicate.fee.tax",
    transfer: "price.duplicate.fee.transfer",
  } as const satisfies Record<FiscalComponentName, TranslationKey>;
  return fiscalRows(language, fees)
    .map((row) => translate(language, keys[row.key as FiscalComponentName], { value: row.value }))
    .join(" · ");
}

// ------------------------------------------------------------------------------------------------ solar

/** What the solar section shows: the site's `site` block's facts. */
export interface SolarFacts {
  chargerCount: number;
  /** "Applies to every charger on the site" (shown with more than one charger). */
  appliesToText: string;
  writable: boolean;
  choices: ReadonlyArray<{ id: string; title: string; selected: boolean }>;
}

export interface SolarSectionInput extends SectionHost {
  facts: SolarFacts;
  /** The chosen forecast sources (`spotnav/update_site_settings`). */
  writeForecast(forecast: string[]): Promise<string | null>;
}

/** Solar: the forecast sources the site's plans read, one multi-choice editor. */
export function solarSectionBody(input: SolarSectionInput): HTMLElement {
  const { doc, language, facts } = input;
  const section = element(doc, "section", C.settingsSection);
  section.dataset["section"] = "solar";
  section.append(sectionHeading(doc, "solar", translate(language, "settings.section.solar")));
  if (facts.chargerCount > 1) {
    section.append(element(doc, "p", C.siteApplies, facts.appliesToText));
  }
  const forecastLabel = translate(language, "site.solarForecast.title");
  const sources = facts.choices.filter((choice) => choice.selected);
  section.append(
    input.row(
      "solar_forecast",
      forecastLabel,
      sources.length === 0 ? translate(language, "settings.value.none") : sources.map((choice) => choice.title).join(", "),
      facts.writable && facts.choices.length > 0
        ? () =>
            input.editMulti(
              forecastLabel,
              facts.choices.map((choice) => ({ value: choice.id, label: choice.title })),
              sources.map((choice) => choice.id),
              (values) => input.writeForecast(values),
            )
        : undefined,
    ),
  );
  return section;
}
