// The market editor: the charger's price area and the three fiscal components its prices carry, as
// one module for the resting trigger and the dialog body. Nothing here talks to the backend or judges
// a value; `market.ts` and the card own every judgement and request. Controls are standards-based
// elements with real labels, and backend strings only ever go in as text.
//
// The three policies are the backend's: `Off` keeps any stated figure for later, `Suggested` stores
// no figure (`value: null`, so the backend applies the catalogue's suggestion), `Custom` stores the
// reader's finite, non-negative number. `0` is a value.

import { formatNumber } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  FISCAL_COMPONENTS,
  countryLabel,
  coversGreatBritain,
  figureEdited,
  fiscalUnit,
  groupAreasForPicker,
  marketAreaLabel,
  marketIncluded,
  marketStateKey,
  marketSuggestion,
  resetToSuggestion,
  setEnabled,
  type FiscalComponent,
  type FiscalFormValue,
  type MarketFormValues,
} from "./market";
import type { MarketOptionsV1 } from "./types";
import { VISUAL_CLASSES as C } from "./visual-styles";

export interface MarketEditorForm {
  readOnly: boolean;
  options: MarketOptionsV1;
  values: MarketFormValues;
  conflict: number | null;
}

export interface MarketEditorHandlers {
  onSave: (values: MarketFormValues) => void;
  onReload: () => void;
  onReapply: (values: MarketFormValues) => void;
  onCancel: () => void;
  /**
   * The reader picked another area. `live` is the form's state before the switch, so the caller can
   * keep it as that area's draft and rebuild the body for the new area.
   */
  onAreaChange: (areaId: string | null, live: MarketFormValues) => void;
  /**
   * Ask Home Assistant which Great Britain region a postcode is in (`spotnav/find_region`). Absent, no
   * postcode field is offered. The postcode goes to Home Assistant, which asks Octopus Energy; it is never
   * kept here.
   */
  onFindRegion?: (postcode: string) => Promise<RegionLookup>;
}

/** What a postcode lookup answered: the region to select, or why there is none. */
export interface RegionLookup {
  region: string | null;
  reason: string | null;
}

export interface MarketEditorBody {
  body: HTMLElement;
  values: () => MarketFormValues;
}

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function componentKey(component: FiscalComponent, suffix: "label" | "description"): TranslationKey {
  return `market.${component}.${suffix}` as TranslationKey;
}

/**
 * The trigger icon: a small path-only SVG (a plug) hidden from assistive technology, built with
 * `createElementNS`.
 */
export function marketIcon(doc: Document): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add(C.settingsIcon);
  const path = doc.createElementNS(ns, "path");
  path.setAttribute("d", "M9 3v6M15 3v6M6 9h12v3a6 6 0 0 1-12 0V9zm6 9v3");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  svg.append(path);
  return svg;
}

/**
 * The resting trigger: an icon and an action ("Edit price area and taxes"). The area is already on
 * the row above, so it appears only in the accessible name.
 */
export function marketTrigger(doc: Document, language: Language, value: string): HTMLButtonElement {
  const button = doc.createElement("button") as HTMLButtonElement;
  button.type = "button";
  button.className = `${C.button} ${C.settingsTrigger}`;
  button.dataset["setting"] = "market";
  button.setAttribute("aria-label", translate(language, "market.aria", { value }));
  button.append(marketIcon(doc));
  button.append(element(doc, "span", undefined, translate(language, "market.edit")));
  return button;
}

interface AreaChoice {
  id: string;
  name: string | null;
  published: boolean;
}

/**
 * The selector's choices: every published area in the relay's order, plus the charger's stored area
 * when the catalogue no longer lists it, so a stored choice never disappears (it has no name to show).
 */
function areaChoices(options: MarketOptionsV1): AreaChoice[] {
  const choices: AreaChoice[] = options.areas.map((area) => ({
    id: area.area_id,
    name: area.name,
    published: true,
  }));
  const configured = options.configured_area;
  if (configured !== null && !options.areas.some((area) => area.area_id === configured)) {
    choices.push({ id: configured, name: null, published: false });
  }
  return choices;
}

function optionText(language: Language, choice: AreaChoice): string {
  const base = choice.name === null ? choice.id : marketAreaLabel(language, choice.name, choice.id);
  return choice.published ? base : `${base} \u00b7 ${translate(language, "market.area.unlisted")}`;
}

function figureText(language: Language, value: number): string {
  return formatNumber(language, value, 2);
}

function suggestionText(language: Language, suggestion: number | null): string {
  return suggestion === null ? "" : figureText(language, suggestion);
}

/**
 * The dialog body: catalogue state, one area selector, one fieldset per component. The number field
 * is enabled only for `Custom`; for `Off` it stays visible and disabled holding any stated figure.
 * Save is absent when there is no area to state.
 */
export function marketEditorBody(
  doc: Document,
  language: Language,
  form: MarketEditorForm,
  handlers: MarketEditorHandlers,
  idPrefix: string,
  region: string | null = null,
): MarketEditorBody {
  const body = element(doc, "div");
  body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(language, "market.intro")));
  const stateKey = marketStateKey(form.options.state, form.options.reason);
  if (stateKey !== null) {
    body.append(element(doc, "p", C.marketState, translate(language, stateKey)));
  }

  const areaId = `${idPrefix}-area`;
  const areaDescriptionId = `${areaId}-description`;
  const choices = areaChoices(form.options);
  const select = doc.createElement("select") as HTMLSelectElement;
  select.className = `${C.settingsInput} ${C.marketArea}`;
  const areaField = element(doc, "div", C.settingsField);
  const areaLabel = element(doc, "label", C.settingsLabel, translate(language, "market.area.label"));
  areaLabel.setAttribute("for", areaId);
  select.id = areaId;
  if (form.values.areaId === null) {
    // The stored truth may be "no area yet"; nothing is preselected for the reader.
    const placeholder = doc.createElement("option") as HTMLOptionElement;
    placeholder.value = "";
    placeholder.textContent = translate(language, "market.unset");
    placeholder.disabled = true;
    placeholder.selected = true;
    select.append(placeholder);
  }
  const optionFor = (choice: AreaChoice): HTMLOptionElement => {
    const option = doc.createElement("option") as HTMLOptionElement;
    option.value = choice.id;
    option.textContent = optionText(language, choice);
    return option;
  };
  // Grouped by country like the app's picker, the card's own country first. A stored area the catalogue
  // no longer lists has no country to sit under, so it follows the groups.
  const countriesOf = new Map(form.options.areas.map((area) => [area.area_id, area.countries] as const));
  const { groups, ungrouped } = groupAreasForPicker(
    choices.map((choice) => ({ choice, countries: countriesOf.get(choice.id) ?? [] })),
    region,
  );
  for (const group of groups) {
    const optgroup = doc.createElement("optgroup") as HTMLOptGroupElement;
    optgroup.label = countryLabel(language, group.heading);
    optgroup.append(...group.areas.map((entry) => optionFor(entry.choice)));
    select.append(optgroup);
  }
  select.append(...ungrouped.map((entry) => optionFor(entry.choice)));
  select.value = form.values.areaId ?? "";
  select.disabled = form.readOnly;
  const areaDescription = element(
    doc,
    "p",
    `${C.muted} ${C.settingsNote}`,
    translate(language, "market.area.description"),
  );
  areaDescription.id = areaDescriptionId;
  if (choices.length === 0 && form.values.areaId === null) {
    areaDescription.textContent = translate(language, "market.area.missing");
  }
  select.setAttribute("aria-describedby", areaDescriptionId);
  areaField.append(areaLabel, select, areaDescription);
  const selectedArea = form.options.areas.find((area) => area.area_id === form.values.areaId) ?? null;
  const attribution = selectedArea?.source ?? null;
  if (attribution !== null) {
    // The attribution, small and linked, beside the area it belongs to (never under the chart).
    areaField.append(sourceLine(doc, language, attribution));
  }
  body.append(areaField);

  // The builder for the area this body was built for, so a found region switches through the same path.
  const builtFor = form.values.areaId;
  const offerPostcode =
    handlers.onFindRegion !== undefined &&
    !form.readOnly &&
    form.options.areas.some((area) => coversGreatBritain(area.countries)) &&
    (coversGreatBritain(selectedArea?.countries) || (region ?? "").trim().toUpperCase() === "GB");
  if (offerPostcode && handlers.onFindRegion !== undefined) {
    body.append(
      postcodeField(doc, language, idPrefix, handlers.onFindRegion, (found) => {
        if (found !== builtFor) {
          handlers.onAreaChange(found, read());
        }
      }),
    );
  }

  // Each row's intent (own figure or still the catalogue's) is tracked here, not read from the DOM,
  // and numeric equality never stands in for it.
  const states: Record<FiscalComponent, FiscalFormValue> = {
    vat: form.values.vat,
    tax: form.values.tax,
    transfer: form.values.transfer,
  };

  const shown = (value: FiscalFormValue, suggestion: number | null): string =>
    value.intent === "suggested" ? suggestionText(language, suggestion) : value.value;

  const included = marketIncluded(form.options, form.values.areaId);
  for (const component of FISCAL_COMPONENTS) {
    if (included.includes(component)) {
      body.append(includedRow(doc, language, idPrefix, component));
      continue;
    }
    const current = states[component];
    const unit = fiscalUnit(form.options, form.values.areaId, component);
    const suggestion = marketSuggestion(form.options, form.values.areaId, component);
    const label = translate(language, componentKey(component, "label"));
    const suggestionId = `${idPrefix}-${component}-suggestion`;
    const descriptionId = `${idPrefix}-${component}-description`;

    const block = element(doc, "div", C.marketComponent);

    const row = element(doc, "div", C.marketValue);
    const checkbox = doc.createElement("input") as HTMLInputElement;
    checkbox.type = "checkbox";
    checkbox.className = C.marketCheckbox;
    checkbox.id = `${idPrefix}-${component}-enabled`;
    checkbox.checked = current.enabled;
    checkbox.disabled = form.readOnly;
    checkbox.setAttribute(
      "aria-label",
      translate(language, "market.enabled.aria", { component: label }),
    );
    checkbox.setAttribute("aria-describedby", `${suggestionId} ${descriptionId}`);
    const name = element(doc, "label", C.settingsLabel, label);
    name.setAttribute("for", checkbox.id);

    const figure = doc.createElement("input") as HTMLInputElement;
    figure.type = "number";
    // No step or maximum: a fiscal figure has no defined range, and a browser step would make an
    // ordinary decimal (7.13) `:invalid`. The backend judges the value.
    figure.step = "any";
    figure.min = "0";
    figure.inputMode = "decimal";
    figure.value = shown(current, suggestion);
    figure.className = C.settingsInput;
    figure.id = `${idPrefix}-${component}-value`;
    figure.disabled = form.readOnly;
    figure.setAttribute(
      "aria-label",
      translate(language, "market.value.aria", { component: label }),
    );
    figure.setAttribute("aria-describedby", `${suggestionId} ${descriptionId}`);
    row.append(checkbox, name, figure, element(doc, "span", C.settingsUnit, unit));
    block.append(row);

    const suggestionLine = element(
      doc,
      "p",
      C.marketSuggestion,
      suggestion === null
        ? translate(language, "market.suggestion.none")
        : translate(language, "market.suggestion.label", {
            value: figureText(language, suggestion),
            unit,
          }),
    );
    suggestionLine.id = suggestionId;
    const reset = doc.createElement("button") as HTMLButtonElement;
    reset.type = "button";
    reset.className = `${C.button} ${C.marketReset}`;
    reset.textContent = translate(language, "market.resetToSuggestion");
    reset.disabled = form.readOnly;
    reset.setAttribute("aria-describedby", suggestionId);
    suggestionLine.append(" ", reset);
    block.append(suggestionLine);

    const description = element(
      doc,
      "p",
      `${C.muted} ${C.settingsNote}`,
      translate(language, componentKey(component, "description")),
    );
    description.id = descriptionId;
    block.append(description);

    /** The reset is offered only when there is a suggestion and the figure is the person's own. */
    const paint = (): void => {
      reset.hidden = form.readOnly || suggestion === null || states[component].intent !== "custom";
    };
    checkbox.addEventListener("change", () => {
      states[component] = setEnabled(states[component], checkbox.checked);
      paint();
    });
    figure.addEventListener("input", () => {
      states[component] = figureEdited(states[component], figure.value);
      paint();
    });
    reset.addEventListener("click", () => {
      states[component] = resetToSuggestion(states[component]);
      figure.value = shown(states[component], suggestion);
      paint();
    });
    paint();
    body.append(block);
  }

  if (included.length > 0) {
    const note = element(doc, "p", `${C.muted} ${C.settingsNote}`, translate(language, "market.includedNote"));
    note.id = `${idPrefix}-included-note`;
    body.append(note);
  }

  // The area this body was built for, not `select.value`: by the time a change event fires the
  // selector already reads the new choice.
  const builtAreaId = form.values.areaId;
  const read = (): MarketFormValues => ({
    areaId: builtAreaId,
    vat: { ...states.vat },
    tax: { ...states.tax },
    transfer: { ...states.transfer },
  });

  select.addEventListener("change", () => {
    handlers.onAreaChange(select.value === "" ? null : select.value, read());
  });

  if (form.conflict !== null) {
    body.append(element(doc, "p", C.settingsConflict, translate(language, "settings.conflict.intro")));
  }

  // A read-only dialog, or a form with no area to state, has no Save.
  const editable = !form.readOnly && form.values.areaId !== null;
  if (editable) {
    const actions = element(doc, "div", C.settingsActions);
    if (form.conflict === null) {
      const save = doc.createElement("button") as HTMLButtonElement;
      save.type = "button";
      save.className = `${C.button} ${C.settingsSave}`;
      save.textContent = translate(language, "settings.save");
      save.addEventListener("click", () => handlers.onSave(read()));
      actions.append(save);
    } else {
      // A conflict offers only the two conflict choices: a plain Save would be built on the revision the
      // server already rejected.
      const reapply = doc.createElement("button") as HTMLButtonElement;
      reapply.type = "button";
      reapply.className = `${C.button} ${C.settingsReapply}`;
      reapply.textContent = translate(language, "settings.reapply");
      reapply.addEventListener("click", () => handlers.onReapply(read()));
      const reload = doc.createElement("button") as HTMLButtonElement;
      reload.type = "button";
      reload.className = `${C.button} ${C.settingsReload}`;
      reload.textContent = translate(language, "settings.reload");
      reload.addEventListener("click", () => handlers.onReload());
      actions.append(reapply, reload);
    }
    const cancel = doc.createElement("button") as HTMLButtonElement;
    cancel.type = "button";
    cancel.className = C.button;
    cancel.textContent = translate(language, "settings.cancel");
    cancel.addEventListener("click", () => handlers.onCancel());
    actions.append(cancel);
    body.append(actions);
  }

  return { body, values: read };
}

/** "Price source: <a>Octopus Energy (Agile)</a>", in small text, opening in a new tab. */
export function sourceLine(doc: Document, language: Language, source: { name: string; url: string }): HTMLElement {
  const line = element(doc, "p", `${C.muted} ${C.settingsNote} ${C.marketSource}`);
  line.append(`${translate(language, "market.source")} `);
  const link = doc.createElement("a") as HTMLAnchorElement;
  link.href = source.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = source.name;
  line.append(link);
  return line;
}

/** A component the published price already holds: checked, locked, and saying so. No figure is asked for. */
function includedRow(doc: Document, language: Language, idPrefix: string, component: FiscalComponent): HTMLElement {
  const label = translate(language, componentKey(component, "label"));
  const block = element(doc, "div", C.marketComponent);
  block.dataset["included"] = component;
  const row = element(doc, "div", C.marketValue);
  const checkbox = doc.createElement("input") as HTMLInputElement;
  checkbox.type = "checkbox";
  checkbox.className = C.marketCheckbox;
  checkbox.id = `${idPrefix}-${component}-enabled`;
  checkbox.checked = true;
  checkbox.disabled = true;
  checkbox.setAttribute("aria-describedby", `${idPrefix}-included-note`);
  const name = element(doc, "label", C.settingsLabel, label);
  name.setAttribute("for", checkbox.id);
  row.append(checkbox, name, element(doc, "span", C.marketIncluded, translate(language, "market.included")));
  block.append(row);
  return block;
}

/**
 * The optional "Find my region" field: a postcode, one button, and one line saying what happened. The
 * postcode is handed to `find` and forgotten; a found region is selected through `select`.
 */
function postcodeField(
  doc: Document,
  language: Language,
  idPrefix: string,
  find: (postcode: string) => Promise<RegionLookup>,
  select: (region: string) => void,
): HTMLElement {
  const field = element(doc, "div", `${C.settingsField} ${C.marketPostcode}`);
  const inputId = `${idPrefix}-postcode`;
  const label = element(doc, "label", C.settingsLabel, translate(language, "market.findRegion.label"));
  label.setAttribute("for", inputId);
  const row = element(doc, "div", C.marketPostcodeRow);
  const input = doc.createElement("input") as HTMLInputElement;
  input.type = "text";
  input.id = inputId;
  input.className = C.settingsInput;
  input.autocomplete = "postal-code";
  input.maxLength = 10;
  input.spellcheck = false;
  const button = doc.createElement("button") as HTMLButtonElement;
  button.type = "button";
  button.className = C.button;
  button.textContent = translate(language, "market.findRegion.button");
  row.append(input, button);
  const description = element(doc, "p", `${C.muted} ${C.settingsNote}`, translate(language, "market.findRegion.description"));
  description.id = `${inputId}-description`;
  input.setAttribute("aria-describedby", description.id);
  const outcome = element(doc, "p", C.settingsNote);
  outcome.setAttribute("role", "status");
  outcome.hidden = true;
  const say = (key: TranslationKey, region?: string): void => {
    outcome.textContent = translate(language, key, region === undefined ? {} : { region });
    outcome.hidden = false;
  };
  const run = async (): Promise<void> => {
    const postcode = input.value.trim();
    if (postcode === "") {
      say("market.findRegion.invalid");
      return;
    }
    button.disabled = true;
    try {
      const answer = await find(postcode);
      if (answer.region !== null) {
        say("market.findRegion.found", answer.region);
        select(answer.region);
        return;
      }
      say(
        answer.reason === "invalid_postcode"
          ? "market.findRegion.invalid"
          : answer.reason === "not_found"
            ? "market.findRegion.notFound"
            : "market.findRegion.unavailable",
      );
    } catch {
      say("market.findRegion.unavailable");
    } finally {
      button.disabled = false;
    }
  };
  button.addEventListener("click", () => {
    void run();
  });
  input.addEventListener("keydown", (event) => {
    if ((event as KeyboardEvent).key === "Enter") {
      event.preventDefault();
      void run();
    }
  });
  field.append(label, row, description, outcome);
  return field;
}
