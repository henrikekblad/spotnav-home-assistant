// Settings → Support, drawn the same in every card: the charger card's Settings page and the shared popover of the
// house and heat cards both build it here, so it cannot drift apart again. In order: the heading with its icon, the
// line about a card out of date (hidden until a check says so), the value rows (Automations, Beta tester, Hardware
// test), then the actions as rows with a chevron like the rest of the page: Documentation first, then the host's
// (About this card, the debug download with what the file holds under it, sending it, Disconnect all phones), and
// last "Support the SpotNav project" with the sponsor heart, to GitHub Sponsors.

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
  // Last, for everyone: the project's GitHub Sponsors page, with the sponsor heart before the words.
  const sponsor = supportAction(doc, "sponsor", translate(language, "support.sponsor.label"), () => {
    doc.defaultView?.open(SPONSOR_URL, "_blank", "noopener,noreferrer");
  }, translate(language, "support.sponsor.help"));
  sponsor.button.dataset["sponsor"] = "open";
  sponsor.nodes[0]!.querySelector(`.${C.settingRowLabel}`)?.prepend(sponsorHeart(doc));
  for (const action of [docs, ...parts.actions, sponsor]) {
    section.append(...action.nodes);
  }
  return section;
}

/** SpotNav's GitHub Sponsors page. */
export const SPONSOR_URL = "https://github.com/sponsors/henrikekblad";

/** The sponsor heart (Material Design Icons' `heart`) in GitHub Sponsors' pink, before the row's words. */
function sponsorHeart(doc: Document): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add(C.sponsorHeart);
  const path = doc.createElementNS(ns, "path");
  path.setAttribute(
    "d",
    "M12,21.35L10.55,20.03C5.4,15.36 2,12.27 2,8.5C2,5.41 4.42,3 7.5,3C9.24,3 10.91,3.81 12,5.08C13.09,3.81 14.76,3 16.5,3C19.58,3 22,5.41 22,8.5C22,12.27 18.6,15.36 13.45,20.04L12,21.35Z",
  );
  path.setAttribute("fill", "currentColor");
  svg.append(path);
  return svg;
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
