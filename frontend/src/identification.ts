// Which car is plugged in, at a charger more than one car can charge at: the card's banner that answers the
// open question, the Settings section's rows and dialog (the cars at this charger and the mode), and the words
// for a car's plug and location sources. The card owns every request; these only build and report.

import { translate, type Language } from "./i18n";
import type { IdentificationChoice } from "./settings";
import { IDENTIFY_MODES, type IdentifyMode, type SettingsRecord } from "./types";
import type { Identification, IdentificationSource, Vehicle } from "./validate";
import { VISUAL_CLASSES as C } from "./visual-styles";

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

const MODE_KEYS = {
  automatic: "identify.mode.automatic",
  ask: "identify.mode.ask",
  off: "identify.mode.off",
} as const;
const MODE_HELP_KEYS = {
  automatic: "identify.mode.automaticHelp",
  ask: "identify.mode.askHelp",
  off: "identify.mode.offHelp",
} as const;

export interface IdentificationBannerInput {
  block: Identification;
  /** The name of the car kept when nobody answers; `null` when none is chosen. */
  currentName: string | null;
  /** Only an administrator's answer is accepted: a reader sees the question without buttons. */
  canAnswer: boolean;
  onAnswer: (vehicleId: string) => void;
}

/** The open question on the card: one button per car, likeliest first; one tap is the answer. */
export function identificationBanner(doc: Document, language: Language, input: IdentificationBannerInput): HTMLElement {
  const box = element(doc, "div", C.suggestion);
  box.dataset["banner"] = "identify";
  box.setAttribute("role", "status");
  box.append(element(doc, "p", C.suggestionText, translate(language, "identify.question")));
  if (input.currentName !== null) {
    box.append(element(doc, "p", `${C.suggestionText} ${C.muted}`, translate(language, "identify.keeps", { name: input.currentName })));
  }
  if (input.canAnswer) {
    const answers = element(doc, "div", C.suggestionAnswers);
    for (const candidate of input.block.candidates) {
      const answer = element(doc, "button", C.choiceButton, candidate.name) as HTMLButtonElement;
      answer.type = "button";
      answer.dataset["identify"] = candidate.vehicle_id;
      answer.dataset["likely"] = String(candidate.likely);
      answer.addEventListener("click", () => {
        for (const button of Array.from(answers.querySelectorAll<HTMLButtonElement>("button"))) {
          button.disabled = true;
        }
        input.onAnswer(candidate.vehicle_id);
      });
      answers.append(answer);
    }
    box.append(answers);
  }
  return box;
}

/** The Settings section's two rows: the mode, and the cars at this charger. */
export function identificationSummary(
  language: Language,
  record: SettingsRecord,
  vehicles: readonly Vehicle[],
): Array<{ key: string; label: string; value: string }> {
  const mode = record.identify_mode ?? "automatic";
  const ids = record.vehicle_ids ?? null;
  const listed = ids === null ? [] : vehicles.filter((row) => ids.includes(row.id));
  const all = ids === null || listed.length === vehicles.length;
  return [
    { key: "identify_mode", label: translate(language, "identify.mode.label"), value: translate(language, MODE_KEYS[mode]) },
    {
      key: "identify_vehicles",
      label: translate(language, "identify.vehicles.label"),
      value: all
        ? translate(language, "identify.vehicles.all")
        : listed.map((row) => row.name ?? row.id).join(", ") || translate(language, "settings.value.none"),
    },
  ];
}

export interface IdentificationEditorHandlers {
  onSave: (choice: IdentificationChoice) => void;
  onCancel: () => void;
}

/** The dialog: the mode (one radio each, with what it does) and a checkbox per car. */
export function identificationEditorBody(
  doc: Document,
  language: Language,
  record: SettingsRecord,
  vehicles: readonly Vehicle[],
  idPrefix: string,
  handlers: IdentificationEditorHandlers,
): { body: HTMLFormElement; setError: (text: string | null) => void } {
  let mode: IdentifyMode = record.identify_mode ?? "automatic";
  const allIds = vehicles.map((row) => row.id);
  const stored = record.vehicle_ids ?? null;
  const ticked = new Set(stored === null ? allIds : allIds.filter((id) => stored.includes(id)));

  const body = element(doc, "form") as HTMLFormElement;
  body.noValidate = true;
  body.dataset["identificationEditor"] = "true";

  const modes = element(doc, "fieldset", C.siteFieldset);
  modes.dataset["part"] = "mode";
  modes.append(element(doc, "legend", C.siteLegend, translate(language, "identify.mode.label")));
  for (const value of IDENTIFY_MODES) {
    const label = element(doc, "label", C.siteChoice);
    const radio = doc.createElement("input");
    radio.type = "radio";
    radio.name = `${idPrefix}-identify-mode`;
    radio.value = value;
    radio.checked = mode === value;
    radio.dataset["identifyMode"] = value;
    radio.addEventListener("change", () => {
      if (radio.checked) {
        mode = value;
      }
    });
    label.append(radio, doc.createTextNode(translate(language, MODE_KEYS[value])));
    modes.append(label, element(doc, "p", C.entityHelp, translate(language, MODE_HELP_KEYS[value])));
  }
  body.append(modes);

  const cars = element(doc, "fieldset", C.siteFieldset);
  cars.dataset["part"] = "vehicles";
  cars.append(element(doc, "legend", C.siteLegend, translate(language, "identify.vehicles.label")));
  for (const row of vehicles) {
    const label = element(doc, "label", C.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.checked = ticked.has(row.id);
    box.dataset["identifyVehicle"] = row.id;
    box.addEventListener("change", () => {
      if (box.checked) {
        ticked.add(row.id);
      } else {
        ticked.delete(row.id);
      }
    });
    label.append(box, doc.createTextNode(row.name ?? row.id));
    cars.append(label);
  }
  const error = element(doc, "p", C.settingsError);
  error.hidden = true;
  error.setAttribute("role", "alert");
  cars.append(error);
  body.append(cars);

  const actions = element(doc, "div", C.settingsActions);
  const save = element(doc, "button", `${C.button} ${C.settingsSave}`, translate(language, "settings.save")) as HTMLButtonElement;
  save.type = "submit";
  const cancel = element(doc, "button", C.button, translate(language, "settings.cancel")) as HTMLButtonElement;
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  const setError = (text: string | null): void => {
    error.hidden = text === null;
    error.textContent = text ?? "";
  };
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (ticked.size === 0) {
      setError(translate(language, "identify.error.noVehicle"));
      return;
    }
    handlers.onSave({ mode, vehicleIds: allIds.filter((id) => ticked.has(id)), allVehicleIds: allIds });
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });
  return { body, setError };
}

/** A source as a summary row says it: the entity's name, "None", "Choose one" or "None found". */
export function sourceText(language: Language, source: IdentificationSource): string {
  if (source.entity_id !== null) {
    return source.name ?? source.entity_id;
  }
  if (source.chosen) {
    return translate(language, "identify.source.none");
  }
  return translate(language, source.candidates.length > 1 ? "identify.source.choose" : "identify.source.notFound");
}

/** A source's choice as the vehicle dialog's select holds it: `""` automatic, `"none"`, or an entity id. */
export function sourceChoice(source: IdentificationSource): string {
  if (!source.chosen) {
    return "";
  }
  return source.entity_id ?? "none";
}

/** The select for one source: automatic, each candidate, and none. */
export function sourceSelect(
  doc: Document,
  language: Language,
  kind: "plug" | "location",
  source: IdentificationSource,
  id: string,
  onChange: (value: string) => void,
): HTMLElement {
  const block = element(doc, "div", C.settingsField);
  block.dataset["part"] = kind;
  const label = element(doc, "label", C.settingsLabel, translate(language, kind === "plug" ? "identify.source.plug" : "identify.source.location"));
  label.setAttribute("for", id);
  const select = doc.createElement("select");
  select.id = id;
  select.className = C.settingsInput;
  const option = (value: string, text: string): void => {
    const node = doc.createElement("option");
    node.value = value;
    node.textContent = text;
    select.append(node);
  };
  option("", translate(language, "identify.source.automatic"));
  for (const candidate of source.candidates) {
    option(candidate.entity_id, candidate.name ?? candidate.entity_id);
  }
  option("none", translate(language, "identify.source.none"));
  select.value = sourceChoice(source);
  select.addEventListener("change", () => {
    onChange(select.value);
  });
  block.append(label, select);
  return block;
}
