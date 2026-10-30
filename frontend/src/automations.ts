// "Automations": the Home Assistant blueprints SpotNav ships (`blueprints/automation/spotnav/` in the repository),
// listed for an administrator from Settings. Each has a name, one sentence, and a button that opens Home Assistant's
// blueprint import (through my.home-assistant.io, which forwards to the reader's own Home Assistant) with the
// blueprint's raw file on the main branch. The list is the card's own; nothing is asked of the backend. Both are
// always listed: the blueprint's own inputs say what it needs (a site's solar surplus sensor, for one).

import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

/**
 * The import links, written out whole (not built at run time) so the compiled card carries exactly these addresses:
 * `https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=` and the URL-encoded raw file of
 * `https://raw.githubusercontent.com/henrikekblad/spotnav-home-assistant/main/blueprints/automation/spotnav/<file>`.
 */
export const BLUEPRINT_IMPORT_URLS = {
  priceSetpoint:
    "https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fhenrikekblad%2Fspotnav-home-assistant%2Fmain%2Fblueprints%2Fautomation%2Fspotnav%2Fprice_setpoint.yaml",
  solarSurplus:
    "https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2Fhenrikekblad%2Fspotnav-home-assistant%2Fmain%2Fblueprints%2Fautomation%2Fspotnav%2Fsolar_surplus_switch.yaml",
} as const;

export type BlueprintId = keyof typeof BLUEPRINT_IMPORT_URLS;

/** The blueprints, in the order they are listed. */
export const BLUEPRINTS: readonly { id: BlueprintId; name: TranslationKey; text: TranslationKey }[] = [
  { id: "priceSetpoint", name: "automations.priceSetpoint.name", text: "automations.priceSetpoint.text" },
  { id: "solarSurplus", name: "automations.solarSurplus.name", text: "automations.solarSurplus.text" },
];

/** The dialog's body: a line on what blueprints are, then one box per blueprint with its import button. */
export function automationsBody(doc: Document, language: Language, openExternal: (url: string) => void): HTMLElement {
  const say = (key: TranslationKey): string => translate(language, key);
  const body = doc.createElement("div");
  body.dataset["automations"] = "list";
  const intro = doc.createElement("p");
  intro.className = `${C.muted} ${C.dialogIntro}`;
  intro.textContent = say("automations.intro");
  body.append(intro);
  for (const blueprint of BLUEPRINTS) {
    const box = doc.createElement("div");
    box.className = C.suggestion;
    box.dataset["blueprint"] = blueprint.id;
    const name = doc.createElement("p");
    name.className = C.automationName;
    name.textContent = say(blueprint.name);
    const text = doc.createElement("p");
    text.className = C.suggestionText;
    text.textContent = say(blueprint.text);
    const answers = doc.createElement("div");
    answers.className = C.suggestionAnswers;
    const button = doc.createElement("button");
    button.type = "button";
    button.className = C.choiceButton;
    button.dataset["blueprintImport"] = blueprint.id;
    const label = doc.createElement("span");
    label.className = C.choiceLabel;
    label.textContent = say("automations.add");
    button.append(label);
    button.addEventListener("click", () => openExternal(BLUEPRINT_IMPORT_URLS[blueprint.id]));
    answers.append(button);
    box.append(name, text, answers);
    body.append(box);
  }
  return body;
}
