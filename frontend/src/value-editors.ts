// The Settings page in the app's compact style: a section heading with its icon, value rows (the label on the
// left, the value right-aligned at the same size, in the accent colour and tappable when it can be changed) and
// the few generic editors every value opens, one value per dialog: a number, one of a few, several, and on/off.
// What a value is for is always said above what to fill in. The card owns the writes: an editor hands its value
// to `save`, which answers `null` when it took (the dialog then closes) or a sentence that keeps it open.

import { formatNumber } from "./format";
import { translate, type Language } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** What an editor's Save does: `null` when the value was taken, else the sentence to show under it. */
export type SaveValue<T> = (value: T) => Promise<string | null>;

export interface EditorHandlers {
  /** The editor's value was taken: the card closes the dialog. */
  onDone: () => void;
  onCancel: () => void;
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

// ------------------------------------------------------------------------------------------------ icons

const ICON_PATHS = {
  price: { stroke: "M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1zM4 9h16M4 14h16M10 9v11" },
  charger: { fill: "M16 7V3h-2v4h-4V3H8v4c-1.1 0-2 .9-2 2v5.5L9.5 18v3h5v-3l3.5-3.5V9c0-1.1-.9-2-2-2z" },
  car: {
    fill:
      "M18.9 6C18.7 5.4 18.2 5 17.5 5h-11c-.7 0-1.2.4-1.4 1L3 12v8c0 .6.4 1 1 1h1c.6 0 1-.4 1-1v-1h12v1c0 .6.4 1 1 1h1c.6 0 1-.4 1-1v-8zM6.5 16a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zm11 0a1.5 1.5 0 1 1 0-3 1.5 1.5 0 0 1 0 3zM5 11l1.5-4.5h11L19 11z",
  },
  site: { stroke: "M3 11 12 4l9 7v9h-6v-6H9v6H3z" },
  solar: {
    stroke:
      "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  },
  notifications: {
    fill:
      "M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2zm6-6v-5c0-3.1-1.6-5.6-4.5-6.3V4a1.5 1.5 0 0 0-3 0v.7C7.6 5.4 6 7.9 6 11v5l-2 2v1h16v-1z",
  },
  support: { stroke: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v6M12 7.5v.5" },
} as const;

export type SectionIcon = keyof typeof ICON_PATHS;

function sectionIcon(doc: Document, kind: SectionIcon): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.dataset["icon"] = kind;
  const spec = ICON_PATHS[kind] as { fill?: string; stroke?: string };
  const path = doc.createElementNS(ns, "path");
  if (spec.fill !== undefined) {
    path.setAttribute("d", spec.fill);
    path.setAttribute("fill", "currentColor");
  } else {
    path.setAttribute("d", spec.stroke ?? "");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
  }
  svg.append(path);
  return svg;
}

/** A section heading: its icon and what it names ("Laddare · HALO Charger"). */
export function sectionHeading(doc: Document, kind: SectionIcon, title: string, name: string | null = null): HTMLElement {
  const heading = element(doc, "h4", C.settingsSectionHeading);
  heading.append(sectionIcon(doc, kind), doc.createTextNode(name === null || name.trim() === "" ? title : `${title} · ${name}`));
  return heading;
}

// ------------------------------------------------------------------------------------------------ rows

export interface SettingRowInput {
  key: string;
  label: string;
  value: string;
  /** A short muted line under the row. */
  help?: string;
  /** Opens this value's own editor; without it the value is read-only and drawn in the normal colour. */
  onTap?: () => void;
  /** Spoken as "label, value. Tap to change" for a changeable row. */
  changeableText?: string;
}

/**
 * One value row: the label on the left, the value right-aligned at the same size; in the accent colour and a
 * button when it can be changed, in the normal colour otherwise. Returns the row and its help line.
 */
export function settingRow(doc: Document, input: SettingRowInput): HTMLElement[] {
  const row = element(doc, "div", C.settingRow);
  row.dataset["row"] = input.key;
  row.append(element(doc, "span", C.settingRowLabel, input.label));
  if (input.onTap !== undefined) {
    const button = element(doc, "button", `${C.settingRowValue} ${C.settingRowEditable}`, input.value) as HTMLButtonElement;
    button.type = "button";
    button.dataset["edit"] = input.key;
    if (input.changeableText !== undefined) {
      button.setAttribute("aria-label", input.changeableText);
    }
    const tap = input.onTap;
    button.addEventListener("click", () => tap());
    row.append(button);
  } else {
    row.append(element(doc, "span", C.settingRowValue, input.value));
  }
  const nodes: HTMLElement[] = [row];
  if (input.help !== undefined) {
    const help = element(doc, "p", C.settingRowHelp, input.help);
    help.dataset["help"] = input.key;
    nodes.push(help);
  }
  return nodes;
}

// ------------------------------------------------------------------------------------------------ editors

interface EditorShell {
  form: HTMLFormElement;
  /** Where the editor puts its own parts: above the error line and the actions. */
  body: HTMLElement;
  error: HTMLElement;
  setError: (text: string | null) => void;
  /** Runs `attempt` with Save disabled; a sentence keeps the dialog open with it shown. */
  submit: (attempt: () => Promise<string | null>) => void;
}

function shell(doc: Document, language: Language, kind: string, handlers: EditorHandlers, positive?: string): EditorShell {
  const form = element(doc, "form") as HTMLFormElement;
  form.noValidate = true;
  form.dataset["valueEditor"] = kind;
  const error = element(doc, "p", C.settingsError);
  error.hidden = true;
  error.setAttribute("role", "alert");
  const actions = element(doc, "div", C.settingsActions);
  const save = element(doc, "button", `${C.button} ${C.settingsSave}`, positive ?? translate(language, "settings.save")) as HTMLButtonElement;
  save.type = "submit";
  const cancel = element(doc, "button", C.button, translate(language, "settings.cancel")) as HTMLButtonElement;
  cancel.type = "button";
  cancel.addEventListener("click", () => handlers.onCancel());
  actions.append(save, cancel);
  const setError = (text: string | null): void => {
    error.hidden = text === null;
    error.textContent = text ?? "";
  };
  let pending = false;
  let attemptNow: (() => Promise<string | null>) | null = null;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (pending || attemptNow === null) {
      return;
    }
    const attempt = attemptNow;
    pending = true;
    save.disabled = true;
    setError(null);
    void attempt()
      .then((message) => {
        if (message === null) {
          handlers.onDone();
        } else {
          setError(message);
        }
      })
      .finally(() => {
        pending = false;
        save.disabled = false;
      });
  });
  const body = element(doc, "div");
  form.append(body, error, actions);
  return {
    form,
    body,
    error,
    setError,
    submit: (attempt) => {
      attemptNow = attempt;
    },
  };
}

function helpLine(doc: Document, text: string): HTMLElement {
  return element(doc, "p", C.entityHelp, text);
}

export interface NumberEditorInput {
  /** What the value is for: shown above the field. */
  help?: string;
  unit: string;
  current: number | null;
  min: number;
  max: number;
  decimals: number;
  /** A box above the field choosing "none" (Off, Not specified, Automatic): the value is then `null`. */
  noneLabel?: string;
  /** A figure to offer ("Förslag: 36 öre/kWh"); tapping it fills the field. */
  suggestion?: number | null;
  rangeMessage: string;
  idPrefix: string;
}

/** A number with its unit, an optional "none" box and a suggestion that fills the field. */
export function numberEditor(
  doc: Document,
  language: Language,
  input: NumberEditorInput,
  save: SaveValue<number | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "number", handlers);
  const body = editor.body;
  if (input.help !== undefined) {
    body.append(helpLine(doc, input.help));
  }
  const field = doc.createElement("input");
  field.type = "text";
  field.inputMode = input.decimals > 0 ? "decimal" : "numeric";
  field.className = C.settingsInput;
  field.id = `${input.idPrefix}-value-number`;
  field.dataset["valueField"] = "number";
  // As the row shows it ("36", "36,5"): no trailing zeros, no grouping.
  const shown = (value: number): string => formatNumber(language, value, input.decimals).replace(/\s/g, "");
  field.value = input.current === null ? "" : shown(input.current);
  let none: HTMLInputElement | null = null;
  if (input.noneLabel !== undefined) {
    const label = element(doc, "label", C.siteChoice);
    none = doc.createElement("input");
    none.type = "checkbox";
    none.checked = input.current === null;
    none.dataset["valueNone"] = "true";
    const box = none;
    box.addEventListener("change", () => {
      field.disabled = box.checked;
    });
    field.disabled = none.checked;
    label.append(none, doc.createTextNode(input.noneLabel));
    body.append(label);
  }
  if (input.suggestion !== undefined && input.suggestion !== null) {
    const figure = shown(input.suggestion);
    const unit = input.unit === "" ? "" : ` ${input.unit}`;
    const offer = element(doc, "button", C.settingRowSuggestion, translate(language, "settings.suggestion", { value: `${figure}${unit}` })) as HTMLButtonElement;
    offer.type = "button";
    offer.dataset["valueSuggestion"] = "true";
    offer.addEventListener("click", () => {
      if (none !== null) {
        none.checked = false;
      }
      field.disabled = false;
      field.value = figure;
      field.focus();
    });
    body.append(offer);
  }
  const line = element(doc, "div", C.settingsRow);
  line.append(field);
  if (input.unit !== "") {
    line.append(element(doc, "span", C.settingsUnit, input.unit));
  }
  body.append(line);
  editor.submit(async () => {
    if (none !== null && none.checked) {
      // Already none: nothing to write.
      return input.current === null ? null : await save(null);
    }
    const text = field.value.trim().replace(/\s/g, "").replace(",", ".");
    const number = text === "" ? Number.NaN : Number(text);
    if (!Number.isFinite(number)) {
      return translate(language, "settings.error.number");
    }
    // The range is judged on what was typed, then the figure is rounded to the editor's decimals.
    if (number < input.min || number > input.max) {
      return input.rangeMessage;
    }
    const scale = 10 ** input.decimals;
    const rounded = Math.round(number * scale) / scale;
    // The figure that is already stored: nothing to write.
    return rounded === input.current ? null : await save(rounded);
  });
  return editor.form;
}

export interface ChoiceOption {
  value: string;
  label: string;
  /** A muted line under the option. */
  help?: string;
}

/** One of a few, the current one marked, each with its help line when it has one. Choose saves a new choice. */
export function singleEditor(
  doc: Document,
  language: Language,
  input: { intro?: string; options: readonly ChoiceOption[]; selected: string | null; idPrefix: string },
  save: SaveValue<string>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "single", handlers, translate(language, "identify.choose"));
  const body = editor.body;
  if (input.intro !== undefined) {
    body.append(helpLine(doc, input.intro));
  }
  let chosen = input.selected;
  const group = element(doc, "fieldset", C.siteFieldset);
  for (const option of input.options) {
    const label = element(doc, "label", C.siteChoice);
    const radio = doc.createElement("input");
    radio.type = "radio";
    radio.name = `${input.idPrefix}-value-single`;
    radio.value = option.value;
    radio.checked = option.value === input.selected;
    radio.dataset["valueOption"] = option.value;
    radio.addEventListener("change", () => {
      if (radio.checked) {
        chosen = option.value;
      }
    });
    label.append(radio, doc.createTextNode(option.label));
    group.append(label);
    if (option.help !== undefined) {
      group.append(helpLine(doc, option.help));
    }
  }
  body.append(group);
  editor.submit(async () => {
    if (chosen === null || chosen === input.selected) {
      return null;
    }
    return await save(chosen);
  });
  return editor.form;
}

/** Several of a few; with `atLeastOne` none ticked is refused with that sentence. */
export function multiEditor(
  doc: Document,
  language: Language,
  input: { intro?: string; options: readonly ChoiceOption[]; checked: readonly string[]; atLeastOne?: string },
  save: SaveValue<string[]>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "multi", handlers);
  const body = editor.body;
  if (input.intro !== undefined) {
    body.append(helpLine(doc, input.intro));
  }
  const ticked = new Set(input.checked);
  const group = element(doc, "fieldset", C.siteFieldset);
  for (const option of input.options) {
    const label = element(doc, "label", C.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.checked = ticked.has(option.value);
    box.dataset["valueOption"] = option.value;
    box.addEventListener("change", () => {
      if (box.checked) {
        ticked.add(option.value);
      } else {
        ticked.delete(option.value);
      }
    });
    label.append(box, doc.createTextNode(option.label));
    group.append(label);
    if (option.help !== undefined) {
      group.append(helpLine(doc, option.help));
    }
  }
  body.append(group);
  editor.submit(async () => {
    const values = input.options.map((option) => option.value).filter((value) => ticked.has(value));
    if (input.atLeastOne !== undefined && values.length === 0) {
      return input.atLeastOne;
    }
    const before = input.options.map((option) => option.value).filter((value) => input.checked.includes(value));
    if (values.join("\u0000") === before.join("\u0000")) {
      return null;
    }
    return await save(values);
  });
  return editor.form;
}

/** On or off, as a choice of the two. */
export function onOffEditor(
  doc: Document,
  language: Language,
  input: { intro?: string; on: boolean; idPrefix: string },
  save: SaveValue<boolean>,
  handlers: EditorHandlers,
): HTMLFormElement {
  return singleEditor(
    doc,
    language,
    {
      ...(input.intro === undefined ? {} : { intro: input.intro }),
      options: [
        { value: "on", label: translate(language, "settings.value.on") },
        { value: "off", label: translate(language, "settings.value.off") },
      ],
      selected: input.on ? "on" : "off",
      idPrefix: input.idPrefix,
    },
    (value) => save(value === "on"),
    handlers,
  );
}
