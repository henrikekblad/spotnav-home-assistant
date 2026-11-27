// The year review's illustrations, one per page, as inline SVG text (no external asset): a rocket for the cover, a
// stack of 3D coins (no text on them) for what was saved, the car, the sun, the battery, a shield for the fuse, the
// price peak and a heart. Drawn in the story from constant text (`artElement`, parsed, never markup from data) and
// on the share picture from a `data:` URL at 600 × 600 (`artDataUrl`), so the canvas is never tainted.
//
// Gradient ids carry the `spotnav-yr-` prefix; each card's shadow root holds one story, so they never meet.

import type { YearReviewPageId } from "./year-review-model";

export type ArtId = YearReviewPageId;

const SVG_NS = "http://www.w3.org/2000/svg";
/** The classes the story animates (`year-review-view.ts`); still under reduced motion. */
export const ART_PULSE = "spotnav-yr-pulse";
export const ART_SPIN = "spotnav-yr-spin";

function coins(): string {
  let out = "";
  for (let k = 0; k < 4; k += 1) {
    const y = 78 - k * 13;
    const dx = k % 2 ? 3 : -2;
    let ridges = "";
    for (let r = 0; r < 13; r += 1) {
      const x = 22 + r * 4.6;
      ridges += `<line x1="${x.toFixed(1)}" y1="${y + 2}" x2="${x.toFixed(1)}" y2="${y + 8}" stroke="#7a4f00" stroke-width=".8" opacity=".45"/>`;
    }
    out +=
      `<g transform="translate(${dx},0)">` +
      `<path d="M20 ${y} V${y + 7} A30 9 0 0 0 80 ${y + 7} V${y} Z" fill="url(#spotnav-yr-gside)"/>` +
      ridges +
      `<ellipse cx="50" cy="${y}" rx="30" ry="9" fill="url(#spotnav-yr-gtop)" stroke="#b07a00" stroke-width="1.2"/>` +
      `<ellipse cx="50" cy="${y}" rx="22" ry="6" fill="none" stroke="#fff6c8" stroke-width="1.4" opacity=".7"/></g>`;
  }
  return out;
}

function rays(): string {
  let out = "";
  for (let i = 0; i < 12; i += 1) {
    out += `<rect x="48" y="4" width="4" height="16" rx="2" fill="#fff3a6" transform="rotate(${i * 30} 50 50)"/>`;
  }
  return out;
}

const OPEN = `<svg xmlns="${SVG_NS}" viewBox="0 0 100 100">`;

/** Each page's illustration (viewBox 0 0 100 100). */
export const ART: Record<ArtId, string> = {
  cover:
    `${OPEN}<defs><linearGradient id="spotnav-yr-rb" x1="0" x2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#c9cfe0"/></linearGradient>` +
    `<linearGradient id="spotnav-yr-fl" y1="0" y2="1"><stop offset="0" stop-color="#ffe066"/><stop offset="1" stop-color="#ff6a3d" stop-opacity="0"/></linearGradient></defs>` +
    `<g transform="rotate(35 50 50)"><path d="M50 8 C64 22 66 44 62 64 H38 C34 44 36 22 50 8Z" fill="url(#spotnav-yr-rb)"/>` +
    `<circle cx="50" cy="36" r="7" fill="#5b9bff" stroke="#2a2f7a" stroke-width="2.5"/>` +
    `<path d="M38 52 L26 70 L38 66Z M62 52 L74 70 L62 66Z" fill="#ec6fb5"/>` +
    `<path class="${ART_PULSE}" d="M42 66 Q50 98 58 66Z" fill="url(#spotnav-yr-fl)"/></g></svg>`,
  saved:
    `${OPEN}<defs><linearGradient id="spotnav-yr-gtop" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff0a8"/><stop offset=".55" stop-color="#ffd23f"/><stop offset="1" stop-color="#e0a100"/></linearGradient>` +
    `<linearGradient id="spotnav-yr-gside" x1="0" x2="1"><stop offset="0" stop-color="#a86f00"/><stop offset=".35" stop-color="#e8b21f"/><stop offset=".7" stop-color="#c58a00"/><stop offset="1" stop-color="#8a5a00"/></linearGradient></defs>` +
    `${coins()}</svg>`,
  car:
    `${OPEN}<defs><linearGradient id="spotnav-yr-cb" y1="0" y2="1"><stop offset="0" stop-color="#ffd0ea"/><stop offset="1" stop-color="#ec6fb5"/></linearGradient></defs>` +
    `<g class="${ART_PULSE}" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".8"><line x1="4" y1="50" x2="18" y2="50"/><line x1="8" y1="60" x2="20" y2="60"/></g>` +
    `<path d="M22 66 V56 L32 40 Q34 37 38 37 H66 Q70 37 72 40 L82 56 V66 Q82 70 78 70 H26 Q22 70 22 66Z" fill="url(#spotnav-yr-cb)" stroke="#fff" stroke-width="2.5"/>` +
    `<path d="M36 42 H64 L70 54 H30Z" fill="#6b1e52" opacity=".5"/>` +
    `<circle cx="34" cy="70" r="7" fill="#2b1022" stroke="#fff" stroke-width="2.5"/><circle cx="70" cy="70" r="7" fill="#2b1022" stroke="#fff" stroke-width="2.5"/>` +
    `<path d="M52 44 L46 56 H53 L49 66 L59 51 H52Z" fill="#fff"/></svg>`,
  sun:
    `${OPEN}<defs><radialGradient id="spotnav-yr-sg" cx=".4" cy=".35"><stop offset="0" stop-color="#fff6c2"/><stop offset="1" stop-color="#f2b705"/></radialGradient></defs>` +
    `<g class="${ART_SPIN}">${rays()}</g><circle cx="50" cy="50" r="22" fill="url(#spotnav-yr-sg)"/></svg>`,
  battery:
    `${OPEN}<defs><linearGradient id="spotnav-yr-bf" y1="1" y2="0"><stop offset="0" stop-color="#7ff0de"/><stop offset="1" stop-color="#22c3b0"/></linearGradient></defs>` +
    `<rect x="30" y="18" width="40" height="70" rx="8" fill="rgba(255,255,255,.15)" stroke="#fff" stroke-width="3"/><rect x="42" y="10" width="16" height="8" rx="3" fill="#fff"/>` +
    `<rect class="${ART_PULSE}" x="36" y="40" width="28" height="42" rx="4" fill="url(#spotnav-yr-bf)"/><path d="M53 46 L44 62 H52 L47 76 L58 58 H50Z" fill="#fff"/></svg>`,
  fuse:
    `${OPEN}<defs><linearGradient id="spotnav-yr-sh" y1="0" y2="1"><stop offset="0" stop-color="#cfe2ff"/><stop offset="1" stop-color="#5b9bff"/></linearGradient></defs>` +
    `<path d="M50 8 L84 20 V48 C84 70 68 84 50 92 C32 84 16 70 16 48 V20Z" fill="url(#spotnav-yr-sh)" stroke="#fff" stroke-width="3"/>` +
    `<path d="M34 50 L46 62 L68 38" fill="none" stroke="#1d3f8a" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  expensive:
    `${OPEN}<polyline points="8,78 24,70 36,74 48,30 58,72 72,66 92,70" fill="none" stroke="#fff" stroke-width="4" stroke-linejoin="round"/>` +
    `<circle cx="48" cy="30" r="6" fill="#ffd1d1" stroke="#fff" stroke-width="2"/>` +
    `<path class="${ART_PULSE}" d="M70 14 V40 M60 32 L70 42 L80 32" fill="none" stroke="#ffe066" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  thanks:
    `${OPEN}<defs><radialGradient id="spotnav-yr-hg" cx=".35" cy=".3"><stop offset="0" stop-color="#ffd6ec"/><stop offset="1" stop-color="#ec6fb5"/></radialGradient></defs>` +
    `<path class="${ART_PULSE}" d="M50 84 C20 62 10 46 18 30 C26 16 44 18 50 32 C56 18 74 16 82 30 C90 46 80 62 50 84Z" fill="url(#spotnav-yr-hg)" stroke="#fff" stroke-width="3"/></svg>`,
};

/** A page's illustration as an SVG element of `doc`, from the constant text above; `null` where it cannot be parsed. */
export function artElement(doc: Document, id: ArtId): SVGSVGElement | null {
  const view = doc.defaultView;
  if (view === null || typeof view.DOMParser !== "function") {
    return null;
  }
  const parsed = new view.DOMParser().parseFromString(ART[id], "image/svg+xml");
  const root = parsed.documentElement;
  if (root.namespaceURI !== SVG_NS || root.localName !== "svg") {
    return null;
  }
  const svg = doc.importNode(root, true) as unknown as SVGSVGElement;
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  return svg;
}

/** A page's illustration at `size` × `size` as a `data:` URL, for the share picture's canvas. */
export function artDataUrl(id: ArtId, size = 600): string {
  const svg = ART[id].replace(`<svg xmlns="${SVG_NS}" `, `<svg xmlns="${SVG_NS}" width="${size}" height="${size}" `);
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
