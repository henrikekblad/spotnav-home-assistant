// Settings → Support, drawn the same in every card: the charger card's Settings page and the shared popover of the
// house and heat cards both build it here, so it cannot drift apart again. In order: the heading with its icon, the
// line about a card out of date (hidden until a check says so), the value rows (Automations, Beta tester, Hardware
// test), then the actions as rows with a chevron like the rest of the page: Documentation first, then the host's
// (About this card, the debug download with what the file holds under it, sending it, Disconnect all phones).

import { translate, type Language } from "./i18n";
import { sectionHeading, settingRow } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

export interface SupportSectionParts {
  doc: Document;
  language: Language;
  /** The card-out-of-date line (`cardOutdatedNote`), kept by the host to show or hide. */
  note: HTMLElement;
  /** The value rows (Automations, Beta tester, Hardware test). */
  rows: HTMLElement[];
  /** The action rows (`supportAction`), after the documentation's row, in order. */
  actions: SupportAction[];
}

/** One of Support's actions drawn as a settings row: the label, a chevron, an optional line under it. */
export interface SupportAction {
  /** The row and its help line, to put in the section. */
  nodes: HTMLElement[];
  /** The row's tappable part (it covers the whole row): its `dataset` names the action, its text a state. */
  button: HTMLButtonElement;
}

export function supportSection(parts: SupportSectionParts): HTMLElement {
  const { doc, language } = parts;
  const section = element(doc, "section", C.settingsSection);
  section.dataset["section"] = "support";
  section.append(sectionHeading(doc, "support", translate(language, "settings.section.support")), parts.note, ...parts.rows);
  // The documentation first, for everyone, opened in a new tab.
  const docs = supportAction(doc, "docs", translate(language, "settings.docs"), () => {
    doc.defaultView?.open(DOCS_URL, "_blank", "noopener,noreferrer");
  });
  docs.button.dataset["docs"] = "open";
  for (const action of [docs, ...parts.actions]) {
    section.append(...action.nodes);
  }
  return section;
}

/** SpotNav's documentation for Home Assistant, on its own site. */
export const DOCS_URL = "https://spotnav.sensnology.se/docs/home-assistant/";

/** "The card in this browser or app is older than SpotNav…": hidden until a check finds it so. */
export function cardOutdatedNote(doc: Document, language: Language, outdated: boolean): HTMLElement {
  const note = element(doc, "p", C.muted, translate(language, "debug.cardOutdated"));
  note.dataset["cardOutdated"] = "true";
  note.setAttribute("role", "status");
  note.hidden = !outdated;
  return note;
}

/**
 * One of Support's actions as a row like every other row on the page: the label on the left, the chevron on the
 * right (no value: the row is the action), `help` as the muted line under it.
 */
export function supportAction(doc: Document, key: string, label: string, onTap: () => void, help?: string): SupportAction {
  const nodes = settingRow(doc, { key, label, value: "", onTap, changeableText: label, ...(help === undefined ? {} : { help }) });
  const button = nodes[0]!.querySelector<HTMLButtonElement>(`[data-edit='${key}']`)!;
  return { nodes, button };
}
