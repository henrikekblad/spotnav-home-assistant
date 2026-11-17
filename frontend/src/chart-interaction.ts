// Pointer, touch, keyboard and resize ownership for one rendered chart. The controller owns the
// selection, its listeners and the size it last drew for, and no data. Listeners attach once to the
// stable wrapper (not the SVG a redraw replaces), and `destroy()` releases everything and makes every
// callback inert. The gesture policy is the shared one in `chart-scrub.ts`: a mouse previews the
// interval under its x as it moves and a click keeps it; a touch tap selects, a horizontal drag scrubs
// and a vertical one is left to the page's scroll. Every pointer picks by x alone, anywhere in the
// plot's height.

import {
  markAtX,
  walkMarks,
  type ChartHitTarget,
  type ChartMark,
  type ChartPoint,
  type ChartScale,
} from "./chart";
import { chartHeightForWidth, chartLocalPoint } from "./chart-render";
import { attachScrub, SCRUB_THRESHOLD } from "./chart-scrub";
import type { ChartDensityStyle } from "./visual-styles";

export interface ChartGeometry {
  scale: ChartScale;
  targets: readonly ChartHitTarget[];
  marks: readonly ChartMark[];
  current: ChartMark | null;
}

export interface ChartSize {
  width: number;
  height: number;
}

export interface ChartObserverLike {
  observe?(host: Element): void;
  disconnect(): void;
}

export interface ChartInteractionOptions {
  /**
   * The stable element every listener lives on: the whole graph section. Not what is measured,
   * since the text nodes would inflate the chart's height.
   */
  host: HTMLElement;
  /** The chart viewport: the one element measured and observed, containing the SVG only. */
  viewport: HTMLElement;
  onSelectionChange: (mark: ChartMark | null) => void;
  onResize: (size: ChartSize) => void;
  surface: () => SVGSVGElement | null;
  measure?: (viewport: HTMLElement) => ChartSize;
  /** The chart's density, whose height policy the default measurement uses; the ordinary card when omitted. */
  density?: ChartDensityStyle;
  observe?: (host: HTMLElement, onResize: () => void) => ChartObserverLike | null;
  fallbackSize?: ChartSize;
  isInsideInteractive?: (node: EventTarget | null) => boolean;
  now?: () => number;
  directionThreshold?: number;
}

export interface ChartInteraction {
  setGeometry(geometry: ChartGeometry): void;
  select(mark: ChartMark | null): void;
  selection(): ChartMark | null;
  handleKeydown(event: KeyboardEvent): void;
  size(): ChartSize;
  /** Measure the viewport again now, as an observed resize would; an equal size is not a redraw. */
  remeasure(): void;
  isObserving(): boolean;
  destroy(): void;
}

export function nearestIntervalTo(
  marks: readonly ChartMark[],
  nowMs: number,
): ChartMark | null {
  let best: ChartMark | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const mark of marks) {
    const distance = nowMs < mark.startMs ? mark.startMs - nowMs : nowMs < mark.endMs ? 0 : nowMs - mark.endMs;
    if (distance < bestDistance) {
      best = mark;
      bestDistance = distance;
    }
  }
  return best;
}

export const FALLBACK_SIZE: ChartSize = { width: 320, height: 150 };

/** How far past the plot's left and right edges a pointer still picks the edge interval, in SVG units. */
const EDGE_SLOP = 12;

/**
 * Default measurement: the viewport's layout width, with the height computed from it by the chart-height
 * policy so the drawn box, viewBox and policy cannot disagree. Layout width, not the bounding rectangle:
 * the SVG is drawn in layout pixels, and a rectangle includes any CSS transform or zoom on an ancestor, so
 * in a scaled container (a wall screen's fullscreen view) it would squeeze the chart sideways.
 */
export function measureChart(viewport: HTMLElement, density?: ChartDensityStyle): ChartSize {
  const width = viewport.clientWidth > 0 ? viewport.clientWidth : FALLBACK_SIZE.width;
  return { width, height: chartHeightForWidth(width, density) };
}

function defaultObserve(host: HTMLElement, onResize: () => void): ChartObserverLike | null {
  const Constructor = (globalThis as { ResizeObserver?: new (callback: () => void) => ChartObserverLike })
    .ResizeObserver;
  if (typeof Constructor !== "function") {
    return null;
  }
  return new Constructor(onResize);
}

export function createChartInteraction(options: ChartInteractionOptions): ChartInteraction {
  const threshold = options.directionThreshold ?? SCRUB_THRESHOLD;
  const measure = options.measure ?? ((viewport: HTMLElement) => measureChart(viewport, options.density));
  const surfaceOf = options.surface;
  const now = options.now ?? (() => Date.now());
  const isInsideInteractive =
    options.isInsideInteractive ?? ((node: EventTarget | null) => node instanceof Node && options.host.contains(node));

  let geometry: ChartGeometry | null = null;
  let selected: ChartMark | null = null;
  let size: ChartSize = options.fallbackSize ?? FALLBACK_SIZE;
  let destroyed = false;
  let observer: ChartObserverLike | null = null;
  let observing = false;

  // The selection a click, a tap, a drag, a key or the card made; a mouse hover only previews over it
  // and leaving the chart shows it again.
  let pinned: ChartMark | null = null;

  function notify(): void {
    if (destroyed) {
      return;
    }
    options.onSelectionChange(selected);
  }

  function select(mark: ChartMark | null, pin = true): void {
    if (destroyed) {
      return;
    }
    if (pin) {
      pinned = mark;
    }
    if (selected === null && mark === null) {
      return;
    }
    if (selected !== null && mark !== null && selected.startMs === mark.startMs) {
      return;
    }
    selected = mark;
    notify();
  }

  /** The interval under the pointer's x, or `null` when the pointer is outside the plot. */
  function markAt(event: PointerEvent): ChartMark | null {
    if (geometry === null) {
      return null;
    }
    const surface = surfaceOf();
    // A folded chart picks nothing: its strip shares the host.
    if (surface === null || options.viewport.hidden) {
      return null;
    }
    const scale = geometry.scale;
    const point: ChartPoint = chartLocalPoint(surface, scale, event.clientX, event.clientY);
    if (point.x < scale.left - EDGE_SLOP || point.x > scale.right + EDGE_SLOP || point.y < 0 || point.y > scale.height) {
      return null;
    }
    return markAtX(geometry.targets, point);
  }

  const scrub = attachScrub(
    options.host,
    {
      press(event) {
        // A mouse click keeps what it points at; a touch waits to be a tap, so a scroll selects nothing.
        if (!destroyed && event.pointerType === "mouse") {
          const mark = markAt(event);
          if (mark !== null) {
            select(mark);
          }
        }
      },
      tap(event) {
        const mark = destroyed ? null : markAt(event);
        if (mark !== null) {
          select(mark);
        }
      },
      hover(event) {
        if (!destroyed) {
          select(markAt(event) ?? pinned, false);
        }
      },
      leave() {
        if (!destroyed) {
          select(pinned, false);
        }
      },
      drag(event) {
        const mark = destroyed ? null : markAt(event);
        if (mark !== null) {
          select(mark);
        }
      },
    },
    threshold,
  );

  function onOutsidePointerDown(event: PointerEvent): void {
    if (destroyed || selected === null) {
      return;
    }
    if (isInsideInteractive(event.target)) {
      return;
    }
    select(null);
  }

  function onKeydown(event: KeyboardEvent): void {
    if (destroyed || geometry === null) {
      return;
    }
    const target = event.target;
    if (target instanceof Node && !options.viewport.contains(target)) {
      return;
    }
    const marks = geometry.marks;
    const step = (direction: number): ChartMark | null => {
      if (direction > 0 && selected === null) {
        return walkMarks(marks, null, 1);
      }
      if (direction < 0 && selected === null) {
        return walkMarks(marks, null, -1);
      }
      return walkMarks(marks, selected, direction);
    };
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        select(step(1));
        break;
      case "ArrowLeft":
      case "ArrowUp":
        select(step(-1));
        break;
      case "Home":
        select(walkMarks(marks, null, 1));
        break;
      case "End":
        select(walkMarks(marks, null, -1));
        break;
      case "Escape":
        select(null);
        break;
      case "Enter":
      case " ":
        // The current interval when there is one, else the nearest visible one to now (never announced
        // as "the current price"). An existing selection stands.
        select(selected ?? geometry.current ?? nearestIntervalTo(marks, now()));
        break;
      default:
        return;
    }
    event.preventDefault();
  }

  function applySize(width: number, height: number): void {
    if (destroyed) {
      return;
    }
    if (width === size.width && height === size.height) {
      return; // Coalesced: an equal size is not a redraw.
    }
    size = { width, height };
    options.onResize(size);
  }

  function onObservedResize(): void {
    if (destroyed) {
      return;
    }
    const measured = measure(options.viewport);
    applySize(measured.width, measured.height);
  }

  options.host.addEventListener("keydown", onKeydown);
  // Capture phase, so a handler inside the card cannot swallow the clear.
  options.host.ownerDocument.addEventListener("pointerdown", onOutsidePointerDown, true);

  const created = (options.observe ?? defaultObserve)(options.viewport, onObservedResize);
  if (created === null) {
    observing = false;
    const measured = measure(options.viewport);
    applySize(measured.width, measured.height);
  } else {
    observing = true;
    observer = created;
    created.observe?.(options.viewport);
  }

  return {
    setGeometry(next: ChartGeometry): void {
      if (destroyed) {
        return;
      }
      geometry = next;
    },
    select(mark: ChartMark | null): void {
      select(mark);
    },
    selection(): ChartMark | null {
      return selected;
    },
    handleKeydown(event: KeyboardEvent): void {
      onKeydown(event);
    },
    size(): ChartSize {
      return size;
    },
    remeasure(): void {
      onObservedResize();
    },
    isObserving(): boolean {
      return observing;
    },
    destroy(): void {
      if (destroyed) {
        return;
      }
      destroyed = true;
      selected = null;
      pinned = null;
      scrub.detach();
      options.host.removeEventListener("keydown", onKeydown);
      options.host.ownerDocument.removeEventListener("pointerdown", onOutsidePointerDown, true);
      observer?.disconnect();
      observer = null;
      observing = false;
      geometry = null;
    },
  };
}
