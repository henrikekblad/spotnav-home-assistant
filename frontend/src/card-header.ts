// The header every SpotNav card shares, the charger's, the house's and the heat's: the product mark, the card's name
// with a muted status after it ("HALO Charger · Väntar", "Huset · Laddar från nätet", "Värme · Test"), the product's
// name for a screen reader, and the history and settings buttons at the end. One builder and one set of styles (the
// charger card's, `visual-styles.ts`), so the three headers have the same mark, title, row height and buttons.
// Built with `textContent` and `createElementNS` only.

import { historyIcon } from "./period-history";
import { VISUAL_CLASSES as C } from "./visual-styles";

const SVG_NS = "http://www.w3.org/2000/svg";

function element<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (text !== undefined) {
    created.textContent = text;
  }
  return created;
}

/**
 * The product mark: a dark rounded square with the red-yellow-green bolt, readable on light and
 * dark themes. Hidden from assistive technology and built with `createElementNS`; gradient and
 * filter ids carry the instance prefix so several cards never share a `url(#id)`.
 */
const MARK_PATH =
  "m214 42-6 62-18-10-23 49 47 3-86.00154 76.23385L142 232l-50 32 6-62 13.99846 9.52923L139 163l-47-3 " +
  "81.29385-75.527692-16.59077-9.06z";

export function brandMark(doc: Document, idPrefix: string): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const make = (tag: string, attributes: Record<string, string>): SVGElement => {
    const node = doc.createElementNS(ns, tag) as SVGElement;
    for (const [name, value] of Object.entries(attributes)) {
      node.setAttribute(name, value);
    }
    return node;
  };
  const stop = (offset: string, color: string): SVGElement => make("stop", { offset, "stop-color": color });
  const backgroundId = `${idPrefix}-mark-background`;
  const boltId = `${idPrefix}-mark-bolt`;
  const haloId = `${idPrefix}-mark-halo`;

  const svg = make("svg", {
    viewBox: "0 0 306 306",
    width: "22",
    height: "22",
    "aria-hidden": "true",
    focusable: "false",
  });
  svg.classList.add(C.identity);

  const background = make("radialGradient", {
    id: backgroundId,
    cx: "106.158",
    cy: "79.888",
    r: "261",
    gradientUnits: "userSpaceOnUse",
  });
  background.append(stop("0", "#454d55"), stop(".42", "#1b1e22"), stop(".76", "#101216"), stop("1", "#080a0d"));
  const bolt = make("linearGradient", {
    id: boltId,
    x1: "155.35",
    y1: "4.69",
    x2: "129.22",
    y2: "303.87",
    gradientUnits: "userSpaceOnUse",
  });
  bolt.append(
    stop("0", "#ff0000"),
    stop(".102", "#ff0000"),
    stop(".43", "#ffd22e"),
    stop(".57", "#ffe56a"),
    stop(".904", "#00ff06"),
    stop("1", "#00ff00"),
  );
  const halo = make("filter", { id: haloId, x: "-30%", y: "-30%", width: "160%", height: "160%" });
  halo.append(make("feGaussianBlur", { stdDeviation: "3.2" }));
  const defs = make("defs", {});
  defs.append(background, bolt, halo);

  svg.append(
    defs,
    make("rect", { x: "8", y: "8", width: "290", height: "290", rx: "70", fill: `url(#${backgroundId})` }),
    make("path", {
      d: MARK_PATH,
      fill: "none",
      stroke: "#ffe36b",
      "stroke-width": "8",
      "stroke-opacity": ".36",
      "stroke-linejoin": "round",
      filter: `url(#${haloId})`,
    }),
    make("path", {
      d: MARK_PATH,
      fill: `url(#${boltId})`,
      stroke: "#ffe36b",
      "stroke-width": "1.2",
      "stroke-opacity": ".52",
      "stroke-linejoin": "round",
    }),
  );
  return svg;
}

/** MDI `mdiCog` path data (`@mdi/js`, Apache License 2.0). */
const COG_PATH =
  "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43," +
  "12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 " +
  "21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5," +
  "2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95," +
  "5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.22,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 " +
  "4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.22,15.05 2.34,15.27L4.34,18.73C4.46," +
  "18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 " +
  "10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.68 16.04,18.34 16.56,17.94L19.05,18.95C" +
  "19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z";

/** The settings cog, one filled path, decorative. */
function cogIcon(doc: Document): SVGElement {
  const svg = doc.createElementNS(SVG_NS, "svg") as SVGElement;
  for (const [name, value] of Object.entries({ viewBox: "0 0 24 24", width: "16", height: "16", "aria-hidden": "true", focusable: "false" })) {
    svg.setAttribute(name, value);
  }
  svg.classList.add(C.actionIcon);
  const path = doc.createElementNS(SVG_NS, "path");
  path.setAttribute("d", COG_PATH);
  path.setAttribute("fill", "currentColor");
  svg.append(path);
  return svg;
}

/** An "i" in a ring: the house card's details (its history dialog), `size` px. */
export function infoIcon(doc: Document, size = 16, className?: string): SVGElement {
  const svg = doc.createElementNS("http://www.w3.org/2000/svg", "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  if (className !== undefined) {
    svg.classList.add(className);
  }
  for (const d of ["M12 3.5a8.5 8.5 0 1 0 0.01 0", "M12 11v5.5", "M12 7.6v0.2"]) {
    const path = doc.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.append(path);
  }
  return svg;
}

/**
 * A header button: the history clock or the details' "i" (both `data-history="open"`, they open a dialog) or the
 * settings cog (`data-settings="open"`), an icon with its label as its accessible name and its tooltip.
 */
export function headerButton(doc: Document, kind: "history" | "info" | "settings", label: string): HTMLButtonElement {
  const button = element(doc, "button", C.iconButton);
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.title = label;
  if (kind === "history" || kind === "info") {
    button.setAttribute("aria-haspopup", "dialog");
    button.dataset["history"] = "open";
    button.append(kind === "info" ? infoIcon(doc, 16, C.actionIcon) : historyIcon(doc, 16, C.actionIcon));
  } else {
    button.dataset["settings"] = "open";
    button.append(cogIcon(doc));
  }
  return button;
}

/**
 * The status after the name, muted: " · Väntar". `afterName` puts the separating dot first (a screen reader skips
 * it); `content` is the word, or a node holding it (the test status's button). Returns the line and its word.
 */
export function headerStatus(doc: Document, content: string | Node, afterName: boolean): { line: HTMLElement; word: HTMLElement } {
  const line = element(doc, "span", C.connectionLine);
  const word = element(doc, "span", C.connectionWord);
  if (afterName) {
    const dot = element(doc, "span", undefined, "·");
    dot.setAttribute("aria-hidden", "true");
    word.append(dot, doc.createTextNode(" "));
  }
  word.append(typeof content === "string" ? doc.createTextNode(content) : content);
  line.append(word);
  return { line, word };
}

/** The name and its status after it, in one row that wraps the status under a long name as a whole. */
export function headerTitle(doc: Document, name: string | null, status: HTMLElement | null): { block: HTMLElement; row: HTMLElement } {
  const block = element(doc, "div", C.nameBlock);
  const row = element(doc, "div", C.nameRow);
  if (name !== null) {
    row.append(element(doc, "h3", C.name, name));
  }
  if (status !== null) {
    row.append(status);
  }
  block.append(row);
  return { block, row };
}

/**
 * The header: the product mark, the title (`headerTitle`, or a bare name), the product's name clipped out of sight
 * for anyone who cannot see the mark, and the buttons at the end (wrapping only as a group).
 */
export function cardHeader(
  doc: Document,
  options: { idPrefix: string; productName: string; title: HTMLElement | null; buttons: HTMLButtonElement[] },
): { header: HTMLElement; actions: HTMLElement } {
  const header = element(doc, "div", C.header);
  header.append(brandMark(doc, options.idPrefix));
  if (options.title !== null) {
    header.append(options.title);
  }
  header.append(element(doc, "span", C.visuallyHidden, options.productName));
  const actions = element(doc, "div", C.headerActions);
  actions.append(...options.buttons);
  header.append(actions);
  return { header, actions };
}
