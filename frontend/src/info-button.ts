// A small info button (ⓘ) that shows and hides a help paragraph, collapsed by default. Any settings part can use it:
// build the paragraph, pass it with an id, and put the returned button beside the field's label.

import { translate, type Language } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

export interface InfoButton {
  readonly button: HTMLButtonElement;
  /** Fold the help away (the button's state follows). */
  collapse: () => void;
}

function infoIcon(doc: Document): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "24");
  svg.setAttribute("height", "24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = doc.createElementNS(ns, "path");
  path.setAttribute("d", "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 7v6m0-9v.01");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  svg.append(path);
  return svg;
}

/**
 * An icon button that toggles `help`. The paragraph gets `id` and starts hidden; the button carries `aria-expanded`,
 * `aria-controls` and a label that says what a press does ("Show help" / "Hide help").
 */
export function infoButton(doc: Document, language: Language, help: HTMLElement, id: string): InfoButton {
  help.id = id;
  const button = doc.createElement("button") as HTMLButtonElement;
  button.type = "button";
  button.className = C.infoButton;
  button.dataset["info"] = id;
  button.setAttribute("aria-controls", id);
  button.append(infoIcon(doc));
  const show = (open: boolean): void => {
    help.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    button.setAttribute("aria-label", translate(language, open ? "settings.info.hide" : "settings.info.show"));
  };
  button.addEventListener("click", () => show(Boolean(help.hidden)));
  show(false);
  return { button, collapse: () => show(false) };
}
