// Settings → Support, drawn the same in every card: the charger card's Settings page and the shared popover of the
// house and heat cards both build it here, so it cannot drift apart again. In order: the heading with its icon, the
// line about a card out of date (hidden until a check says so), the rows (Automations, Beta tester, Hardware test),
// the line on what the debug file holds (when it can be downloaded here), then the buttons side by side.

import { translate, type Language } from "./i18n";
import { sectionHeading } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

export interface SupportSectionParts {
  doc: Document;
  language: Language;
  /** The card-out-of-date line (`cardOutdatedNote`), kept by the host to show or hide. */
  note: HTMLElement;
  rows: HTMLElement[];
  /** The debug file can be downloaded here: say what it holds above the buttons. */
  debugIntro: boolean;
  buttons: HTMLElement[];
}

export function supportSection(parts: SupportSectionParts): HTMLElement {
  const { doc, language } = parts;
  const section = element(doc, "section", C.settingsSection);
  section.dataset["section"] = "support";
  section.append(sectionHeading(doc, "support", translate(language, "settings.section.support")), parts.note, ...parts.rows);
  if (parts.debugIntro) {
    section.append(element(doc, "p", C.muted, translate(language, "debug.intro")));
  }
  if (parts.buttons.length > 0) {
    const actions = element(doc, "div", C.settingsSupportActions);
    actions.append(...parts.buttons);
    section.append(actions);
  }
  return section;
}

/** "The card in this browser or app is older than SpotNav…": hidden until a check finds it so. */
export function cardOutdatedNote(doc: Document, language: Language, outdated: boolean): HTMLElement {
  const note = element(doc, "p", C.muted, translate(language, "debug.cardOutdated"));
  note.dataset["cardOutdated"] = "true";
  note.setAttribute("role", "status");
  note.hidden = !outdated;
  return note;
}

/** One of Support's buttons. */
export function supportButton(doc: Document, label: string, onClick: () => void): HTMLButtonElement {
  const button = element(doc, "button", `${C.button} ${C.settingsSectionConfigure}`, label) as HTMLButtonElement;
  button.type = "button";
  button.addEventListener("click", () => onClick());
  return button;
}
