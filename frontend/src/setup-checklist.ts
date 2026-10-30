// "Get started": what is still left to set up for this charger (the dashboard's `setup` block), shown to an
// administrator in the card's attention line and the "What needs attention" dialog. Each row says what is
// missing and has one button to the place where it is done; an optional row can also be answered "Not needed",
// which the integration keeps for the whole installation. The backend decides which rows are left; the words
// and the places are the card's.

import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { addIntegrationPath } from "./price-provider";
import type { SetupBlock } from "./validate";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** The rows this card knows, in the order it lists them. */
export const SETUP_ROWS = ["car_level", "site", "phone", "app"] as const;
export type SetupRowId = (typeof SETUP_ROWS)[number];

export interface SetupRow {
  id: SetupRowId;
  dismissible: boolean;
}

/** The SpotNav app's own page: what it does, and where to get it (Google Play, or a release on GitHub). */
export const APP_STORE_URL = "https://github.com/henrikekblad/spotnav";

const WORDS: Record<SetupRowId, { text: TranslationKey; action: TranslationKey }> = {
  car_level: { text: "setup.carLevel.text", action: "setup.carLevel.action" },
  site: { text: "setup.site.text", action: "setup.site.action" },
  phone: { text: "setup.phone.text", action: "setup.phone.action" },
  app: { text: "setup.app.text", action: "setup.app.action" },
};

/** The rows of the `setup` block this card can word, in its own order; a row it does not know is left out. */
export function setupRowsOf(block: SetupBlock | null): SetupRow[] {
  if (block === null) {
    return [];
  }
  return SETUP_ROWS.flatMap((id) => {
    const found = block.items.find((item) => item.id === id);
    return found === undefined ? [] : [{ id, dismissible: found.dismissible }];
  });
}

/** "Get started: 3 things left", in the language's own plural. */
export function setupLineText(language: Language, count: number): string {
  const key: TranslationKey = pluralForm(language, count) === "one" ? "setup.line.one" : "setup.line.other";
  return translate(language, key, { count: String(count) });
}

export interface SetupSectionInput {
  language: Language;
  rows: readonly SetupRow[];
  /** Open the Plan popover (where the car's charge-level sensor is chosen). */
  openPlan: () => void;
  /** Open Settings at its Notifications section. */
  openNotifications: () => void;
  /** Move to one of Home Assistant's own pages. */
  navigate: (path: string) => void;
  openExternal: (url: string) => void;
  /** "Not needed": the card asks the integration to keep the row away. */
  dismiss: (id: SetupRowId) => void;
}

/** The dialog's "Get started" section: a heading and one row per thing left. */
export function setupSection(doc: Document, input: SetupSectionInput): HTMLElement {
  const say = (key: TranslationKey): string => translate(input.language, key);
  const section = doc.createElement("section");
  section.dataset["section"] = "setup";
  const heading = doc.createElement("h3");
  heading.className = C.setupHeading;
  heading.textContent = say("setup.heading");
  section.append(heading);
  for (const row of input.rows) {
    const box = doc.createElement("div");
    box.className = C.suggestion;
    box.dataset["setup"] = row.id;
    const text = doc.createElement("p");
    text.className = C.suggestionText;
    text.textContent = say(WORDS[row.id].text);
    box.append(text);
    const answers = doc.createElement("div");
    answers.className = C.suggestionAnswers;
    const button = (label: TranslationKey, action: string, run: () => void): void => {
      const created = doc.createElement("button");
      created.type = "button";
      created.className = C.choiceButton;
      created.dataset["setupAction"] = action;
      const span = doc.createElement("span");
      span.className = C.choiceLabel;
      span.textContent = say(label);
      created.append(span);
      created.addEventListener("click", run);
      answers.append(created);
    };
    button(WORDS[row.id].action, "open", () => open(input, row.id));
    if (row.dismissible) {
      button("setup.dismiss", "dismiss", () => input.dismiss(row.id));
    }
    box.append(answers);
    section.append(box);
  }
  return section;
}

function open(input: SetupSectionInput, id: SetupRowId): void {
  switch (id) {
    case "car_level":
      input.openPlan();
      return;
    case "site":
      input.navigate(addIntegrationPath("spotnav"));
      return;
    case "phone":
      input.openNotifications();
      return;
    case "app":
      input.openExternal(APP_STORE_URL);
      return;
  }
}
