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
import { clock, dayCue, hasZone, hourLabel, type FormatContext } from "./format";
import { translate } from "./i18n";
import { lineText } from "./status";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { STATUS_CODE_TABLE, type PlanPreview, type PlanPreviewDecode, type PlanPreviewStrip, type StatusLine } from "./validate";

/** How long the scheduler waits after the last change before it asks. */
export const PREVIEW_DEBOUNCE_MS = 300;

const QUARTER_MS = 15 * 60_000;

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

/**
 * Times along one line, each with its day only when the line has not named that day yet (today goes without):
 * "mån 2 00:00–01:48 · … · klar 01:48", "23:15–mån 2 00:30 · … · klar 00:30".
 */
class Times {
  private named: string;

  constructor(
    private readonly format: FormatContext,
    nowMs: number,
  ) {
    this.named = localDayKey(nowMs, format.timeZone);
  }

  at(instantMs: number): string {
    const day = localDayKey(instantMs, this.format.timeZone);
    if (day === this.named) {
      return clock(this.format, instantMs);
    }
    this.named = day;
    return `${dayCue(this.format, instantMs)} ${clock(this.format, instantMs)}`;
  }
}

/** The planned periods, the first two in full and the rest counted ("03:30–05:15, 13:00–14:00 +2"). */
function periodsText(preview: PlanPreview, times: Times): string {
  const periods = preview.plan?.periods ?? [];
  const shown = periods.slice(0, 2).map((period) => `${times.at(period.startMs)}–${times.at(period.endMs)}`);
  const more = periods.length - shown.length;
  return more > 0 ? `${shown.join(", ")} +${more}` : shown.join(", ");
}

/** One of the plan's own status lines, worded, and whether it warns (the card's notice or blocking tone). */
export interface PreviewNote {
  text: string;
  warning: boolean;
}

export interface PreviewLines {
  /** `03:30–05:15 · 18.5 kWh · 72.40 kr · ready 05:15`; `null` without a plan (or a zone to tell times in). */
  plan: string | null;
  /** The plan's own status lines, each on a line of its own: a departure it does not make, why there is no plan. */
  notes: PreviewNote[];
}

/** When a departure shortfall line names the departure, the instant it names. */
function shortfallAt(preview: PlanPreview): number | null {
  for (const line of preview.status.lines) {
    const departure = line.params["departure"];
    if (line.code === "departure_shortfall" && typeof departure === "string") {
      const at = Date.parse(departure);
      return Number.isFinite(at) ? at : null;
    }
  }
  return null;
}

/**
 * The result in words: the plan's line and, below it, its own status lines in the card's wording. "ready …" is left
 * out when the shortfall line already says that time, so nothing is said twice.
 */
export function previewLines(preview: PlanPreview, format: FormatContext, nowMs: number): PreviewLines {
  const notes = preview.status.lines
    .filter((line) => line.code !== "settings_suggested")
    .map((line): PreviewNote => ({
      text: lineText(line, format, nowMs).replace(/[.。]$/u, ""),
      warning: (STATUS_CODE_TABLE[line.code] as readonly [string, unknown])[0] !== "normal",
    }));
  const plan = preview.plan;
  if (plan === null || !hasZone(format)) {
    return { plan: null, notes };
  }
  const times = new Times(format, nowMs);
  const parts: string[] = [periodsText(preview, times)];
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
  const shortfall = shortfallAt(preview);
  if (shortfall === null || clock(format, shortfall) !== clock(format, plan.finishMs)) {
    parts.push(translate(format.language, "settings.preview.ready", { time: times.at(plan.finishMs) }));
  }
  return { plan: parts.join(" · "), notes };
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
  /** The strip's own start or end: its label stands inside the strip rather than centred on it. */
  edge?: "start" | "end";
}

/** One unbroken run of planned quarters, in shares of the strip. */
export interface PreviewRun {
  left: number;
  width: number;
}

export interface PreviewStripModel {
  cells: PreviewCell[];
  /** The planned runs, drawn as a band in the accent colour under the price colours: the plan reads without them. */
  runs: PreviewRun[];
  ticks: PreviewTick[];
}

/** The most labels a strip carries, its two ends included: what fits a phone's width. */
export const MAX_TICKS = 7;
/** The least room between two labels, as a share of the strip, so they never run into each other. */
const MIN_TICK_GAP = 0.12;
const TICK_STEPS_HOURS = [1, 2, 3, 4, 6, 8, 12, 24] as const;

/**
 * The strip's labels: its start and its end as times ("23:30", "00:45"), and between them whole local hours ("00")
 * at the smallest even step that keeps the count at `MAX_TICKS` and every label clear of its neighbours.
 */
export function stripTickMarks(strip: PlanPreviewStrip, format: FormatContext): PreviewTick[] {
  const span = strip.endMs - strip.startMs;
  const edges: PreviewTick[] = [
    { position: 0, label: clock(format, strip.startMs), edge: "start" },
    { position: 1, label: clock(format, strip.endMs), edge: "end" },
  ];
  // Every whole quarter inside: a local hour starts on one, also across a change of the clocks.
  const hours: Array<{ position: number; hour: number; label: string }> = [];
  let lastLabel = "";
  for (let at = Math.ceil(strip.startMs / QUARTER_MS) * QUARTER_MS; at < strip.endMs; at += QUARTER_MS) {
    const label = hourLabel(format, at);
    if (label === "" || clock(format, at).slice(-2) !== "00" || label === lastLabel) {
      continue;
    }
    lastLabel = label;
    const position = (at - strip.startMs) / span;
    if (position >= MIN_TICK_GAP && position <= 1 - MIN_TICK_GAP) {
      hours.push({ position, hour: Number(label), label });
    }
  }
  for (const step of TICK_STEPS_HOURS) {
    const between = hours.filter((entry) => entry.hour % step === 0);
    const spaced = between.every(
      (entry, index) => index === 0 || entry.position - between[index - 1]!.position >= MIN_TICK_GAP,
    );
    if (between.length <= MAX_TICKS - 2 && spaced) {
      return [edges[0]!, ...between.map(({ position, label }) => ({ position, label })), edges[1]!];
    }
  }
  return edges;
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
  const runs: PreviewRun[] = [];
  for (const cell of cells) {
    const last = runs.at(-1);
    if (!cell.planned) {
      continue;
    }
    if (last !== undefined && Math.abs(last.left + last.width - cell.left) < 1e-9) {
      last.width += cell.width;
    } else {
      runs.push({ left: cell.left, width: cell.width });
    }
  }
  return { cells, runs, ticks: hasZone(format) ? stripTickMarks(strip, format) : [] };
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
  const band = doc.createElement("div");
  band.className = C.planPreviewBand;
  for (const run of model.runs) {
    const node = doc.createElement("span");
    node.className = C.planPreviewRun;
    node.style.left = percent(run.left);
    node.style.width = percent(run.width);
    band.append(node);
  }
  const labels = doc.createElement("div");
  labels.className = C.stripLabels;
  for (const tick of model.ticks) {
    const label = doc.createElement("span");
    label.className = C.planPreviewTick;
    if (tick.edge !== undefined) {
      label.dataset["edge"] = tick.edge;
    }
    label.style.left = percent(tick.position);
    label.textContent = tick.label;
    labels.append(label);
  }
  area.append(track, band, labels);
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
  const lines = previewLines(state.preview, format, nowMs);
  const nodes: HTMLElement[] = [];
  if (lines.plan !== null) {
    const line = doc.createElement("p");
    line.className = C.planPreviewLine;
    line.textContent = lines.plan;
    nodes.push(line);
  }
  for (const note of lines.notes) {
    const line = doc.createElement("p");
    line.className = C.planPreviewNote;
    line.dataset["tone"] = note.warning ? "warning" : "normal";
    line.textContent = note.text;
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
