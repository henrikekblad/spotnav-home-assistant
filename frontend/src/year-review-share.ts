// The year review's picture to share (1080 × 1350): "My 2026 with SpotNav" over up to four tiles (saved, the car's
// cheap share, the sun's kWh, the battery's money: the first four present), each tinted from the energy palette with
// its illustration drawn from a `data:` URL at 600 × 600 (no foreignObject, so the canvas is never tainted), the
// label at the top, the figure at the bottom sized to fit. Only money, kWh, % and "since {month}": never a name, an
// address or a time. The layout is pure (`shareTiles`, `tileBoxes`, `wrapLines`, `fitFontSize`) and tested without a
// canvas; the drawing takes the fonts Home Assistant shows the card in, after `document.fonts.ready`.
//
// Shared through the Web Share API with the file where the browser can (`navigator.canShare`), else saved through a
// link (`download.ts`); either way the picture is handed back to show in the story, so a webview that does neither
// (the iOS companion app) still lets the reader touch and hold it to save it.

import { ENERGY_PALETTE, type EnergyMeaning } from "./energy-palette";
import { formatNumber } from "./format";
import { translate, type Language } from "./i18n";
import { saveBlobFile } from "./download";
import { artDataUrl, type ArtId } from "./year-review-art";
import type { YearReview } from "./year-review-model";
import { moneyText, monthName } from "./year-review-pages";

export const SHARE_WIDTH = 1080;
export const SHARE_HEIGHT = 1350;
export const ART_PX = 600;
const TILE_W = 440;
const TILE_H = 360;
const TILE_GAP = 40;
const TILE_X = [80, 560] as const;
const TILE_TOP = 400;
const VALUE_MAX_PX = 84;
const VALUE_MIN_PX = 48;
const LABEL_PX = 32;

export type ShareOutcome = { kind: "shared" | "saved"; preview: string } | { kind: "failed" };

export interface ShareTile {
  art: ArtId;
  label: string;
  value: string;
  colour: string;
}

function tint(meaning: EnergyMeaning): string {
  return ENERGY_PALETTE[meaning].light;
}

/** The picture's tiles: what was saved, the car's cheap share, the sun's kWh, the battery's money (first four present). */
export function shareTiles(language: Language, review: YearReview): ShareTile[] {
  const tiles: ShareTile[] = [];
  const approx = (estimated: boolean, text: string): string => (estimated ? `≈ ${text}` : text);
  for (const page of review.pages) {
    if (page.id === "saved") {
      tiles.push({ art: "saved", label: translate(language, "yearReview.share.saved"), value: approx(page.estimated, moneyText(language, page.total, review.money)), colour: tint("export") });
    } else if (page.id === "car" && page.cheap_share_pct !== null) {
      tiles.push({ art: "car", label: translate(language, "yearReview.share.car"), value: approx(page.estimated, `${formatNumber(language, page.cheap_share_pct, 0)} %`), colour: tint("car") });
    } else if (page.id === "sun") {
      tiles.push({ art: "sun", label: translate(language, "yearReview.share.sun"), value: `${formatNumber(language, page.kwh, 0)} kWh`, colour: tint("sun") });
    } else if (page.id === "battery" && page.earned !== null) {
      tiles.push({ art: "battery", label: translate(language, "yearReview.share.battery"), value: moneyText(language, page.earned, review.money), colour: tint("battery") });
    }
  }
  return tiles.slice(0, 4);
}

/** "with SpotNav", or "with SpotNav, since October" for a part of the year (the month only). */
export function shareSubtitle(language: Language, review: YearReview): string {
  return review.since === null
    ? translate(language, "yearReview.share.sub")
    : translate(language, "yearReview.share.subSince", { month: monthName(language, review.since) });
}

export interface TileBox {
  x: number;
  y: number;
  width: number;
  height: number;
  art: { x: number; y: number; size: number };
  label: { x: number; y: number; maxWidth: number; lineHeight: number };
  value: { x: number; y: number; maxWidth: number };
}

/** Where each of `count` tiles goes: two columns, two rows, the picture top right, the label top left beside it. */
export function tileBoxes(count: number): TileBox[] {
  const boxes: TileBox[] = [];
  for (let k = 0; k < Math.min(4, count); k += 1) {
    const x = TILE_X[k % 2]!;
    const y = TILE_TOP + Math.floor(k / 2) * (TILE_H + TILE_GAP);
    boxes.push({
      x,
      y,
      width: TILE_W,
      height: TILE_H,
      art: { x: x + TILE_W - 160, y: y + 24, size: 136 },
      label: { x: x + 36, y: y + 64, maxWidth: TILE_W - 210, lineHeight: 40 },
      value: { x: x + 36, y: y + TILE_H - 48, maxWidth: TILE_W - 72 },
    });
  }
  return boxes;
}

/** `text` broken into at most `maxLines` lines of `maxWidth`, the last one ended "…" where words are left over. */
export function wrapLines(measure: (text: string) => number, text: string, maxWidth: number, maxLines = 2): string[] {
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(/\s+/).filter((part) => part !== "")) {
    const tried = line === "" ? word : `${line} ${word}`;
    if (line !== "" && measure(tried) > maxWidth) {
      lines.push(line);
      line = word;
    } else {
      line = tried;
    }
  }
  if (line !== "") {
    lines.push(line);
  }
  if (lines.length <= maxLines) {
    return lines;
  }
  const kept = lines.slice(0, maxLines);
  kept[maxLines - 1] = `${kept[maxLines - 1]}…`;
  return kept;
}

/** The largest size from `start` down in steps of 4 at which `text` fits `maxWidth`, never under `min`. */
export function fitFontSize(measure: (text: string, size: number) => number, text: string, maxWidth: number, start = VALUE_MAX_PX, min = VALUE_MIN_PX): number {
  let size = start;
  while (size > min && measure(text, size) > maxWidth) {
    size -= 4;
  }
  return size;
}

/** A small repeatable random (the stars fall the same for the same year). */
export function seeded(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** An image from a `data:` URL, `null` where it cannot be drawn. */
function loadImage(doc: Document, url: string): Promise<HTMLImageElement | null> {
  return new Promise((resolve) => {
    const view = doc.defaultView;
    if (view === null) {
      resolve(null);
      return;
    }
    const image = new view.Image();
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = url;
  });
}

export interface DrawOptions {
  /** The font family the card is shown in (Home Assistant's). */
  fontFamily: string;
  image?: (url: string) => Promise<CanvasImageSource | null>;
}

/** Draw the picture on `canvas` (sized here); `false` without a 2D context. */
export async function drawShare(canvas: HTMLCanvasElement, review: YearReview, language: Language, options: DrawOptions): Promise<boolean> {
  canvas.width = SHARE_WIDTH;
  canvas.height = SHARE_HEIGHT;
  const context = canvas.getContext("2d");
  if (context === null) {
    return false;
  }
  const font = options.fontFamily;
  const image = options.image ?? ((url: string) => loadImage(canvas.ownerDocument, url));
  const background = context.createLinearGradient(0, 0, SHARE_WIDTH, SHARE_HEIGHT);
  background.addColorStop(0, tint("house"));
  background.addColorStop(1, "#2a2f7a");
  context.fillStyle = background;
  context.fillRect(0, 0, SHARE_WIDTH, SHARE_HEIGHT);
  const random = seeded(review.year);
  for (let k = 0; k < 70; k += 1) {
    context.fillStyle = `rgba(255,255,255,${(random() * 0.5).toFixed(3)})`;
    context.beginPath();
    context.arc(random() * SHARE_WIDTH, random() * 420, random() * 3 + 1, 0, Math.PI * 2);
    context.fill();
  }
  context.textBaseline = "alphabetic";
  context.fillStyle = "#ffffff";
  context.font = `600 40px ${font}`;
  context.fillText("SpotNav", 80, 110);
  context.font = `700 92px ${font}`;
  context.fillText(translate(language, "yearReview.share.title", { year: String(review.year) }), 80, 240);
  context.font = `500 46px ${font}`;
  context.fillStyle = "rgba(255,255,255,0.9)";
  context.fillText(shareSubtitle(language, review), 80, 305);
  const tiles = shareTiles(language, review);
  const boxes = tileBoxes(tiles.length);
  for (let k = 0; k < boxes.length; k += 1) {
    const tile = tiles[k]!;
    const box = boxes[k]!;
    context.save();
    context.shadowColor = "rgba(0,0,0,0.3)";
    context.shadowBlur = 30;
    context.shadowOffsetY = 10;
    context.fillStyle = tile.colour;
    roundRect(context, box.x, box.y, box.width, box.height, 36);
    context.fill();
    context.restore();
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    const picture = await image(artDataUrl(tile.art, ART_PX));
    if (picture !== null) {
      context.drawImage(picture, box.art.x, box.art.y, box.art.size, box.art.size);
    }
    context.font = `500 ${LABEL_PX}px ${font}`;
    context.fillStyle = "rgba(255,255,255,0.92)";
    const lines = wrapLines((text) => context.measureText(text).width, tile.label, box.label.maxWidth);
    lines.forEach((line, index) => context.fillText(line, box.label.x, box.label.y + index * box.label.lineHeight));
    const size = fitFontSize(
      (text, px) => {
        context.font = `700 ${px}px ${font}`;
        return context.measureText(text).width;
      },
      tile.value,
      box.value.maxWidth,
    );
    context.font = `700 ${size}px ${font}`;
    context.fillStyle = "#ffffff";
    context.fillText(tile.value, box.value.x, box.value.y);
  }
  context.fillStyle = "rgba(255,255,255,0.92)";
  context.font = `600 34px ${font}`;
  context.fillText(translate(language, "yearReview.share.foot"), 80, 1270);
  return true;
}

function roundRect(context: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, radius: number): void {
  context.beginPath();
  if (typeof context.roundRect === "function") {
    context.roundRect(x, y, width, height, radius);
    return;
  }
  context.moveTo(x + radius, y);
  context.arcTo(x + width, y, x + width, y + height, radius);
  context.arcTo(x + width, y + height, x, y + height, radius);
  context.arcTo(x, y + height, x, y, radius);
  context.arcTo(x, y, x + width, y, radius);
  context.closePath();
}

function toBlob(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => {
    try {
      canvas.toBlob((blob) => resolve(blob), "image/png");
    } catch {
      resolve(null);
    }
  });
}

/** The picture's file name. */
export function shareFileName(review: YearReview): string {
  return `spotnav-${review.year}.png`;
}

/**
 * Make the picture and share it (Web Share with the file), else save it; the picture comes back as a `data:` URL to
 * show either way.
 */
export async function shareReview(review: YearReview, language: Language, doc: Document, fontFamily = "Roboto, system-ui, sans-serif"): Promise<ShareOutcome> {
  const view = doc.defaultView;
  if (view === null) {
    return { kind: "failed" };
  }
  try {
    await (doc as Document & { fonts?: { ready?: Promise<unknown> } }).fonts?.ready;
  } catch {
    // Drawn in whatever fonts there are.
  }
  const canvas = doc.createElement("canvas");
  if (!(await drawShare(canvas, review, language, { fontFamily }))) {
    return { kind: "failed" };
  }
  let preview: string;
  try {
    preview = canvas.toDataURL("image/png");
  } catch {
    return { kind: "failed" };
  }
  const blob = await toBlob(canvas);
  if (blob === null) {
    return { kind: "failed" };
  }
  const name = shareFileName(review);
  const navigatorLike = view.navigator as Navigator & {
    canShare?: (data: ShareData) => boolean;
    share?: (data: ShareData) => Promise<void>;
  };
  const FileType = view.File;
  if (typeof FileType === "function" && typeof navigatorLike.share === "function" && typeof navigatorLike.canShare === "function") {
    const file = new FileType([blob], name, { type: "image/png" });
    const data: ShareData = { files: [file], title: translate(language, "yearReview.title") };
    let shareable = false;
    try {
      shareable = navigatorLike.canShare(data);
    } catch {
      shareable = false;
    }
    if (shareable) {
      try {
        await navigatorLike.share(data);
        return { kind: "shared", preview };
      } catch (error) {
        // The reader closed the share sheet: nothing more to do. Any other refusal falls through to saving.
        if ((error as { name?: unknown } | null)?.name === "AbortError") {
          return { kind: "shared", preview };
        }
      }
    }
  }
  try {
    saveBlobFile(doc, name, blob);
  } catch {
    // The picture is still shown to touch and hold.
  }
  return { kind: "saved", preview };
}
