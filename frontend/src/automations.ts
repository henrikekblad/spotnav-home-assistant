// "Automations": the Home Assistant blueprints SpotNav installs in `blueprints/automation/spotnav/` and keeps up to
// date (`blueprint_install.py`), listed for an administrator from Settings. Each has a name, one sentence, how many
// automations use it (from `spotnav/get_blueprints`, once the list is open; nothing when none or unknown, which then
// links to Home Assistant's automation list filtered by it) and a button to Home Assistant's blueprint page, where
// "Create automation" on it starts one. Home Assistant has no address that opens a new automation from a given
// blueprint, so the button opens the page that lists the blueprints. Both are always listed: the blueprint's own inputs
// say what it needs (a site's solar surplus sensor, for one).

import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** The blueprints, by the file SpotNav installs them under, in the order they are listed. */
export const BLUEPRINTS: readonly { file: string; name: TranslationKey; text: TranslationKey }[] = [
  { file: "price_setpoint.yaml", name: "automations.priceSetpoint.name", text: "automations.priceSetpoint.text" },
  { file: "solar_surplus_switch.yaml", name: "automations.solarSurplus.name", text: "automations.solarSurplus.text" },
];

/** Home Assistant's page of blueprints. */
export const BLUEPRINTS_PATH = "/config/blueprint/dashboard";

/** Home Assistant's automation list, showing the automations made from one of SpotNav's blueprints. */
export function automationsUsingPath(file: string): string {
  return `/config/automation/dashboard?blueprint=${encodeURIComponent(`spotnav/${file}`)}`;
}

/** How many automations use each blueprint, by file name; `null` when the answer is not a usable one. */
export type BlueprintUse = Record<string, number>;

/** `spotnav/get_blueprints`'s answer as counts by file; anything unexpected is `null` (and the list shows no counts). */
export function decodeBlueprintUse(raw: unknown): BlueprintUse | null {
  if (typeof raw !== "object" || raw === null) {
    return null;
  }
  const answer = raw as Record<string, unknown>;
  if (answer["api_version"] !== 1 || answer["ok"] !== true || !Array.isArray(answer["blueprints"])) {
    return null;
  }
  const use: BlueprintUse = {};
  for (const row of answer["blueprints"] as unknown[]) {
    if (typeof row !== "object" || row === null) {
      return null;
    }
    const { file, automations } = row as Record<string, unknown>;
    if (typeof file !== "string" || typeof automations !== "number" || !Number.isInteger(automations) || automations < 0) {
      return null;
    }
    use[file] = automations;
  }
  return use;
}

export interface AutomationsList {
  body: HTMLElement;
  /** Show how many automations use each blueprint (a count of 0 shows nothing). */
  showUse: (use: BlueprintUse) => void;
}

/** The dialog's body: a line on the blueprints, then one box per blueprint with its use and its button. */
export function automationsBody(doc: Document, language: Language, navigate: (path: string) => void): AutomationsList {
  const say = (key: TranslationKey): string => translate(language, key);
  const body = doc.createElement("div");
  body.dataset["automations"] = "list";
  const intro = doc.createElement("p");
  intro.className = `${C.muted} ${C.dialogIntro}`;
  intro.textContent = say("automations.intro");
  body.append(intro);
  const useSlots = new Map<string, HTMLElement>();
  for (const blueprint of BLUEPRINTS) {
    const box = doc.createElement("div");
    box.className = C.suggestion;
    box.dataset["blueprint"] = blueprint.file;
    const name = doc.createElement("p");
    name.className = C.automationName;
    name.textContent = say(blueprint.name);
    const text = doc.createElement("p");
    text.className = C.suggestionText;
    text.textContent = say(blueprint.text);
    const used = doc.createElement("p");
    used.className = C.automationUse;
    used.hidden = true;
    useSlots.set(blueprint.file, used);
    const answers = doc.createElement("div");
    answers.className = C.suggestionAnswers;
    const button = doc.createElement("button");
    button.type = "button";
    button.className = C.choiceButton;
    button.dataset["blueprintCreate"] = blueprint.file;
    const label = doc.createElement("span");
    label.className = C.choiceLabel;
    label.textContent = say("automations.add");
    button.append(label);
    button.addEventListener("click", () => navigate(BLUEPRINTS_PATH));
    answers.append(button);
    box.append(name, text, used, answers);
    body.append(box);
  }
  const showUse = (use: BlueprintUse): void => {
    for (const blueprint of BLUEPRINTS) {
      const slot = useSlots.get(blueprint.file);
      const count = use[blueprint.file] ?? 0;
      if (slot === undefined) {
        continue;
      }
      slot.replaceChildren();
      slot.hidden = count === 0;
      if (count === 0) {
        continue;
      }
      const link = doc.createElement("a");
      link.href = automationsUsingPath(blueprint.file);
      link.dataset["blueprintUse"] = blueprint.file;
      const key: TranslationKey = pluralForm(language, count) === "one" ? "automations.usedBy.one" : "automations.usedBy.other";
      link.textContent = translate(language, key, { count: String(count) });
      link.addEventListener("click", (event) => {
        event.preventDefault();
        navigate(automationsUsingPath(blueprint.file));
      });
      slot.append(link);
    }
  };
  return { body, showUse };
}
