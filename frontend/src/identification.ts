// Which car is plugged in, at a charger more than one car can charge at: the card's banner that answers the
// open question, the Settings section's rows and dialog (the cars at this charger and the mode), and the words
// for a car's plug and location sources. The card owns every request; these only build and report.

import { translate, type Language } from "./i18n";
import type { IdentificationChoice } from "./settings";
import type { SettingsRecord } from "./types";
import type { Identification, IdentificationSource } from "./validate";

/** A car the charger's car list can tick: every detected car (`vehicle_choices`). */
export type CarChoice = { id: string; name: string | null };
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

/** The question mark the banner (and the app's) opens with. */
function questionIcon(doc: Document): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.dataset["icon"] = "question";
  svg.style.verticalAlign = "-3px";
  const ring = doc.createElementNS(ns, "circle");
  ring.setAttribute("cx", "12");
  ring.setAttribute("cy", "12");
  ring.setAttribute("r", "9");
  ring.setAttribute("fill", "none");
  ring.setAttribute("stroke", "currentColor");
  ring.setAttribute("stroke-width", "2");
  const mark = doc.createElementNS(ns, "path");
  mark.setAttribute("d", "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6V14M12 17h.01");
  mark.setAttribute("fill", "none");
  mark.setAttribute("stroke", "currentColor");
  mark.setAttribute("stroke-width", "2");
  mark.setAttribute("stroke-linecap", "round");
  svg.append(ring, mark);
  return svg;
}

const METHOD_WORDS = {
  plug_sensor: "vehicleLine.method.plug_sensor",
  location: "vehicleLine.method.location",
  answered: "vehicleLine.method.answered",
  manual: "vehicleLine.method.manual",
  assumed: "vehicleLine.method.assumed",
} as const;

/** How the car on the car line was decided, in words, or `null` when nothing was identified. */
export function methodWords(language: Language, block: Identification | null): string | null {
  if (block === null) {
    return null;
  }
  if (block.state !== "decided") {
    return translate(language, "vehicleLine.method.identifying");
  }
  const key = block.method === null ? undefined : METHOD_WORDS[block.method as keyof typeof METHOD_WORDS];
  return key === undefined ? null : translate(language, key);
}

const HINT_KEYS = {
  plugged_in: "identify.hint.plugged_in",
  not_plugged_in: "identify.hint.not_plugged_in",
  away: "identify.hint.away",
  elsewhere: "identify.hint.elsewhere",
} as const;

export interface ChangeCarInput {
  block: Identification | null;
  vehicles: readonly CarChoice[];
  currentId: string | null;
  chargerName: string | null;
  idPrefix: string;
  onChoose: (vehicleId: string) => void;
  onCancel: () => void;
}

/** "Byt bil": a radio per car (with what its own sensors say), Cancel and Choose. Any signed-in user may. */
export function changeCarBody(doc: Document, language: Language, input: ChangeCarInput): HTMLFormElement {
  const form = element(doc, "form") as HTMLFormElement;
  form.noValidate = true;
  form.dataset["changeCarForm"] = "true";
  form.append(
    element(doc, "p", C.settingsNote, translate(language, "identify.changeCarPrompt", { charger: input.chargerName ?? "" })),
  );
  const cars =
    input.block !== null && input.block.candidates.length > 0
      ? input.block.candidates.map((item) => ({ id: item.vehicle_id, name: item.name }))
      : input.vehicles.map((row) => ({ id: row.id, name: row.name ?? row.id }));
  let chosen = input.currentId;
  const group = element(doc, "fieldset", C.siteFieldset);
  for (const car of cars) {
    const label = element(doc, "label", C.siteChoice);
    const radio = doc.createElement("input");
    radio.type = "radio";
    radio.name = `${input.idPrefix}-change-car`;
    radio.value = car.id;
    radio.checked = car.id === input.currentId;
    radio.dataset["identifyChoice"] = car.id;
    radio.addEventListener("change", () => {
      if (radio.checked) {
        chosen = car.id;
      }
    });
    label.append(radio, doc.createTextNode(car.name));
    group.append(label);
    const verdict = input.block?.evidence.find((item) => item.vehicle_id === car.id)?.verdict ?? null;
    const hint = verdict === null ? undefined : HINT_KEYS[verdict as keyof typeof HINT_KEYS];
    if (hint !== undefined) {
      group.append(element(doc, "p", C.entityHelp, translate(language, hint)));
    }
  }
  form.append(group);
  const actions = element(doc, "div", C.settingsActions);
  const choose = element(doc, "button", `${C.button} ${C.settingsSave}`, translate(language, "identify.choose")) as HTMLButtonElement;
  choose.type = "submit";
  const cancel = element(doc, "button", C.button, translate(language, "settings.cancel")) as HTMLButtonElement;
  cancel.type = "button";
  actions.append(choose, cancel);
  form.append(actions);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (chosen !== null) {
      input.onChoose(chosen);
    }
  });
  cancel.addEventListener("click", () => input.onCancel());
  return form;
}

export interface IdentificationBannerInput {
  block: Identification;
  /** The name of the car kept when nobody answers; `null` when none is chosen. */
  currentName: string | null;
  /** Whether the buttons are offered (any signed-in user may answer). */
  canAnswer: boolean;
  onAnswer: (vehicleId: string) => void;
}

/** The open question on the card: one button per car, likeliest first; one tap is the answer. */
export function identificationBanner(doc: Document, language: Language, input: IdentificationBannerInput): HTMLElement {
  const box = element(doc, "div", C.suggestion);
  box.dataset["banner"] = "identify";
  box.setAttribute("role", "status");
  const question = element(doc, "p", C.suggestionText);
  question.append(questionIcon(doc), doc.createTextNode(` ${translate(language, "identify.question")}`));
  box.append(question);
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
  vehicles: readonly CarChoice[],
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

/** A source as a summary row says it, in status words: "Present", "None", "Choose one" or "Missing". */
export function sourceText(language: Language, source: IdentificationSource): string {
  // In the app's words: the entity is there or not; its name is in the source's own choice.
  if (source.entity_id !== null) {
    return translate(language, "settings.status.present");
  }
  if (source.chosen) {
    return translate(language, "identify.source.none");
  }
  return translate(language, source.candidates.length > 1 ? "identify.source.choose" : "settings.status.missing");
}

/** A source's choice as the vehicle dialog's select holds it: `""` automatic, `"none"`, or an entity id. */
export function sourceChoice(source: IdentificationSource): string {
  if (!source.chosen) {
    return "";
  }
  return source.entity_id ?? "none";
}
