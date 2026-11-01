// The Plan dialog's live preview: what a Save of the draft would plan, asked of `spotnav/plan_preview` while the
// person edits, with nothing saved. Three parts, each testable on its own:
//
// * `PreviewScheduler`: one request ~300 ms after the last change; an answer to an older draft is dropped (a
//   sequence id), so only the latest draft's plan is ever shown. While a newer one is awaited the old result stays,
//   dimmed; a failed request shows nothing new and is logged.
// * `previewLineText`: the result line, in the card's own number, money and status wording.
// * `previewStripModel` and `renderPlanPreview`: a thin strip of the hours to the departure, each quarter coloured
//   by price as the chart colours it (cheap or dear against its own day's mean), the planned ones marked.

import { localDayKey } from "./chart";
import { clock, hasZone, hourLabel, type FormatContext } from "./format";
import { translate } from "./i18n";
import { lineText, momentText, statusText } from "./status";
import { VISUAL_CLASSES as C } from "./visual-styles";
import type { PlanPreview, PlanPreviewDecode, PlanPreviewStrip, StatusLine } from "./validate";

/** How long the scheduler waits after the last change before it asks. */
export const PREVIEW_DEBOUNCE_MS = 300;

const QUARTER_MS = 15 * 60_000;
const HOUR_MS = 3_600_000;

/** What the dialog shows: nothing yet, or the latest preview (dimmed while a newer draft's is awaited). */
export type PreviewState = { kind: "none" } | { kind: "shown"; preview: PlanPreview; pending: boolean };

export interface PreviewSchedulerOptions<B> {
  /** Ask for one draft's preview; resolves to the raw answer. */
  ask: (body: B) => Promise<unknown>;
  decode: (raw: unknown) => PlanPreviewDecode;
  paint: (state: PreviewState) => void;
  log?: (message: string) => void;
  delayMs?: number;
  setTimer?: (callback: () => void, ms: number) => unknown;
  clearTimer?: (handle: unknown) => void;
}

/**
 * The debounce and the sequence: `request(body)` for each draft (`null` for one the card's own checks refuse, which
 * asks nothing), `dispose()` when the dialog closes. Only the latest draft's answer is ever painted.
 */
export class PreviewScheduler<B> {
  private readonly options: PreviewSchedulerOptions<B>;
  private timer: unknown = null;
  private sequence = 0;
  private shown: PlanPreview | null = null;
  private disposed = false;

  constructor(options: PreviewSchedulerOptions<B>) {
    this.options = options;
  }

  request(body: B | null): void {
    if (this.disposed) {
      return;
    }
    // A newer draft: whatever an older one is waiting for is no longer this dialog's.
    const sequence = ++this.sequence;
    this.cancelTimer();
    if (this.shown !== null) {
      this.options.paint({ kind: "shown", preview: this.shown, pending: true });
    }
    if (body === null) {
      return;
    }
    const set = this.options.setTimer ?? ((callback: () => void, ms: number) => setTimeout(callback, ms));
    this.timer = set(() => {
      this.timer = null;
      void this.send(body, sequence);
    }, this.options.delayMs ?? PREVIEW_DEBOUNCE_MS);
  }

  dispose(): void {
    this.disposed = true;
    this.sequence += 1;
    this.cancelTimer();
  }

  private cancelTimer(): void {
    if (this.timer !== null) {
      (this.options.clearTimer ?? ((handle: unknown) => clearTimeout(handle as ReturnType<typeof setTimeout>)))(
        this.timer,
      );
      this.timer = null;
    }
  }

  private async send(body: B, sequence: number): Promise<void> {
    let raw: unknown;
    try {
      raw = await this.options.ask(body);
    } catch (error) {
      if (this.current(sequence)) {
        this.options.log?.(`plan preview failed: ${error instanceof Error ? error.message : "unknown"}`);
      }
      return;
    }
    if (!this.current(sequence)) {
      return;
    }
    const decoded = this.options.decode(raw);
    if (!decoded.ok) {
      this.options.log?.(`plan preview answer refused: ${decoded.failure}`);
      return;
    }
    const answer = decoded.value;
    if (!answer.ok || answer.preview === null) {
      this.options.log?.(`plan preview refused: ${answer.error ?? "unknown"}`);
      return;
    }
    this.shown = answer.preview;
    this.options.paint({ kind: "shown", preview: answer.preview, pending: false });
  }

  private current(sequence: number): boolean {
    return !this.disposed && sequence === this.sequence;
  }
}

/** A time, with its day when it is not today. */
function when(format: FormatContext, instantMs: number, nowMs: number): string {
  return momentText(format, instantMs, nowMs);
}

/** The planned periods, the first two in full and the rest counted ("03:30–05:15, 13:00–14:00 +2"). */
function periodsText(preview: PlanPreview, format: FormatContext, nowMs: number): string {
  const periods = preview.plan?.periods ?? [];
  const shown = periods.slice(0, 2).map((period, index) => {
    const start = index === 0 ? when(format, period.startMs, nowMs) : clock(format, period.startMs);
    const crosses = localDayKey(period.startMs, format.timeZone) !== localDayKey(period.endMs, format.timeZone);
    return `${start}–${crosses ? when(format, period.endMs, nowMs) : clock(format, period.endMs)}`;
  });
  const more = periods.length - shown.length;
  return more > 0 ? `${shown.join(", ")} +${more}` : shown.join(", ");
}

/**
 * The result line: `03:30–05:15 · 18.5 kWh · 72.40 kr · ready 05:15`, then the plan's own status lines (a departure
 * it does not make, a need capped at the battery's room, ...). Without a plan, why not, as the card's status words
 * those codes. `null` when there is nothing to say (no zone to tell times in, or nothing to preview).
 */
export function previewLineText(preview: PlanPreview, format: FormatContext, nowMs: number): string | null {
  const plan = preview.plan;
  if (plan === null) {
    return statusText(preview.status, format, nowMs);
  }
  if (!hasZone(format)) {
    return null;
  }
  const parts: string[] = [periodsText(preview, format, nowMs)];
  const kwh = plan.planned_kwh ?? plan.requested_kwh;
  if (kwh !== null) {
    parts.push(lineText({ code: "plan_energy", params: { kwh } }, format, nowMs));
  }
  if (plan.cost !== null) {
    const cost: StatusLine = {
      code: "plan_cost",
      params: { amount_minor: Math.round(plan.cost.value * 100), currency: plan.cost.currency },
    };
    parts.push(lineText(cost, format, nowMs));
  }
  parts.push(translate(format.language, "settings.preview.ready", { time: when(format, plan.finishMs, nowMs) }));
  const notes = statusText(preview.status, format, nowMs);
  return notes === null ? parts.join(" · ") : `${parts.join(" · ")} · ${notes}`;
}

export interface PreviewCell {
  /** Shares of the strip, 0..1. */
  left: number;
  width: number;
  planned: boolean;
  /** Against its own day's mean, as the chart colours a price; `null` for a quarter with no price. */
  cheap: boolean | null;
}

export interface PreviewTick {
  position: number;
  label: string;
}

export interface PreviewStripModel {
  cells: PreviewCell[];
  ticks: PreviewTick[];
}

/** The hours between ticks for a strip of `spanMs`: few enough to fit a phone's width. */
export function tickStepHours(spanMs: number): number {
  const hours = spanMs / HOUR_MS;
  if (hours <= 6) {
    return 1;
  }
  if (hours <= 12) {
    return 2;
  }
  if (hours <= 24) {
    return 4;
  }
  if (hours <= 48) {
    return 8;
  }
  return 12;
}

/** The strip's geometry: one cell per quarter, and an hour label at every `tickStepHours` local hour inside it. */
export function previewStripModel(strip: PlanPreviewStrip, format: FormatContext): PreviewStripModel {
  const span = strip.endMs - strip.startMs;
  const means = new Map<string, number>();
  if (hasZone(format)) {
    const sums = new Map<string, [number, number]>();
    for (const quarter of strip.quarters) {
      if (quarter.price !== null) {
        const key = localDayKey(quarter.startMs, format.timeZone);
        const [sum, count] = sums.get(key) ?? [0, 0];
        sums.set(key, [sum + quarter.price, count + 1]);
      }
    }
    for (const [key, [sum, count]] of sums) {
      means.set(key, sum / count);
    }
  }
  const cells = strip.quarters
    .filter((quarter) => quarter.startMs >= strip.startMs && quarter.startMs < strip.endMs)
    .map((quarter): PreviewCell => {
      const mean = hasZone(format) ? means.get(localDayKey(quarter.startMs, format.timeZone)) : undefined;
      return {
        left: (quarter.startMs - strip.startMs) / span,
        width: Math.min(QUARTER_MS, strip.endMs - quarter.startMs) / span,
        planned: quarter.planned,
        cheap: quarter.price === null || mean === undefined ? null : quarter.price <= mean,
      };
    });
  const ticks: PreviewTick[] = [];
  if (hasZone(format)) {
    const step = tickStepHours(span);
    // Every whole quarter inside: a local hour starts on one, also across a change of the clocks.
    const first = Math.ceil(strip.startMs / QUARTER_MS) * QUARTER_MS;
    let lastLabel = "";
    for (let at = first; at < strip.endMs; at += QUARTER_MS) {
      const label = hourLabel(format, at);
      const minutes = clock(format, at).slice(-2);
      if (minutes !== "00" || label === "" || Number(label) % step !== 0 || label === lastLabel) {
        continue;
      }
      const position = (at - strip.startMs) / span;
      // A label at an edge would be clipped: only those with room on both sides.
      if (position > 0.04 && position < 0.96) {
        ticks.push({ position, label: `${label}` });
      }
      lastLabel = label;
    }
  }
  return { cells, ticks };
}

function percent(share: number): string {
  return `${Math.round(share * 1e6) / 1e4}%`;
}

/** The strip as DOM: a track of quarter cells, and the hour labels under it. Decorative: the line says it all. */
function stripNode(doc: Document, strip: PlanPreviewStrip, format: FormatContext): HTMLElement {
  const model = previewStripModel(strip, format);
  const area = doc.createElement("div");
  area.className = `${C.stripArea} ${C.planPreviewStrip}`;
  area.setAttribute("aria-hidden", "true");
  const track = doc.createElement("div");
  track.className = C.stripTrack;
  for (const cell of model.cells) {
    const node = doc.createElement("span");
    node.className = C.planPreviewCell;
    node.dataset["planned"] = String(cell.planned);
    node.dataset["price"] = cell.cheap === null ? "none" : cell.cheap ? "cheap" : "dear";
    node.style.left = percent(cell.left);
    node.style.width = percent(cell.width);
    track.append(node);
  }
  const labels = doc.createElement("div");
  labels.className = C.stripLabels;
  for (const tick of model.ticks) {
    const label = doc.createElement("span");
    label.className = C.planPreviewTick;
    label.style.left = percent(tick.position);
    label.textContent = tick.label;
    labels.append(label);
  }
  area.append(track, labels);
  return area;
}

/** Paint `state` into the dialog's preview slot (replacing what was there). */
export function renderPlanPreview(
  doc: Document,
  slot: HTMLElement,
  state: PreviewState,
  format: FormatContext,
  nowMs: number,
): void {
  if (state.kind === "none") {
    slot.replaceChildren();
    slot.hidden = true;
    delete slot.dataset["pending"];
    return;
  }
  const text = previewLineText(state.preview, format, nowMs);
  const nodes: HTMLElement[] = [];
  if (text !== null) {
    const line = doc.createElement("p");
    line.className = C.planPreviewLine;
    line.textContent = text;
    nodes.push(line);
  }
  if (state.preview.strip !== null && state.preview.plan !== null) {
    nodes.push(stripNode(doc, state.preview.strip, format));
  }
  slot.replaceChildren(...nodes);
  slot.hidden = nodes.length === 0;
  slot.dataset["pending"] = String(state.pending);
  slot.setAttribute("aria-busy", String(state.pending));
}
