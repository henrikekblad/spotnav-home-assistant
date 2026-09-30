// Pointer, touch, keyboard and resize ownership for one rendered chart. The controller owns the
// selection, its listeners and the size it last drew for, and no data. Listeners attach once to the
// stable wrapper (not the SVG a redraw replaces), a vertical gesture is left to page scroll until
// a small threshold proves it horizontal, and `destroy()` releases everything and makes every
// callback inert.

import {
  hitTestMarks,
  nearestMark,
  walkMarks,
  type ChartHitTarget,
  type ChartMark,
  type ChartPoint,
  type ChartScale,
} from "./chart";
import { chartHeightForWidth, chartLocalPoint } from "./chart-render";

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

/**
 * How far a gesture must travel before its direction is trusted, in CSS pixels. Small because a
 * quarter-hour slot is about 2.8 px wide on a 320 px graph; direction, not distance, protects the
 * dashboard's scroll (`touch-action: pan-y`).
 */
const DEFAULT_DIRECTION_THRESHOLD = 4;

/**
 * Default measurement: the viewport's width, with the height computed from it by the chart-height
 * policy so the drawn box, viewBox and policy cannot disagree.
 */
function defaultMeasure(viewport: HTMLElement): ChartSize {
  const rect = viewport.getBoundingClientRect();
  const width = rect.width > 0 ? rect.width : FALLBACK_SIZE.width;
  return { width, height: chartHeightForWidth(width) };
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
  const threshold = options.directionThreshold ?? DEFAULT_DIRECTION_THRESHOLD;
  const measure = options.measure ?? defaultMeasure;
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

  let gesture: {
    pointerId: number;
    startX: number;
    startY: number;
    dragging: boolean;
    captured: boolean;
  } | null = null;

  function notify(): void {
    if (destroyed) {
      return;
    }
    options.onSelectionChange(selected);
  }

  function select(mark: ChartMark | null, notifyChange = true): void {
    if (destroyed) {
      return;
    }
    if (selected === null && mark === null) {
      return;
    }
    if (selected !== null && mark !== null && selected.startMs === mark.startMs) {
      return;
    }
    selected = mark;
    if (notifyChange) {
      notify();
    }
  }

  function localPoint(event: PointerEvent): ChartPoint | null {
    if (geometry === null) {
      return null;
    }
    const surface = surfaceOf();
    if (surface === null) {
      return null;
    }
    return chartLocalPoint(surface, geometry.scale, event.clientX, event.clientY);
  }

  function onPointerDown(event: PointerEvent): void {
    if (destroyed || geometry === null) {
      return;
    }
    const point = localPoint(event);
    if (point === null) {
      return;
    }
    gesture = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      dragging: false,
      captured: false,
    };
    const hit = hitTestMarks(geometry.targets, point);
    if (hit !== null) {
      select(hit);
    }
  }

  function onPointerMove(event: PointerEvent): void {
    if (destroyed || geometry === null || gesture === null || gesture.pointerId !== event.pointerId) {
      return;
    }
    const dx = event.clientX - gesture.startX;
    const dy = event.clientY - gesture.startY;
    if (!gesture.dragging) {
      if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) >= threshold) {
        gesture = null;
        return;
      }
      if (Math.abs(dx) < threshold || Math.abs(dx) <= Math.abs(dy)) {
        return;
      }
      gesture.dragging = true;
      gesture.captured = capture(event, gesture.pointerId);
    }
    event.preventDefault();
    const point = localPoint(event);
    if (point === null) {
      return;
    }
    const walked = nearestMark(geometry.targets, point);
    if (walked !== null) {
      select(walked);
    }
  }

  function capture(event: PointerEvent, pointerId: number): boolean {
    const target = event.currentTarget as Element | null;
    if (target !== null && typeof target.setPointerCapture === "function") {
      try {
        target.setPointerCapture(pointerId);
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }

  function endGesture(event: PointerEvent, cancelled: boolean): void {
    if (gesture === null || gesture.pointerId !== event.pointerId) {
      return;
    }
    const active = gesture;
    gesture = null;
    if (active.captured) {
      const target = event.currentTarget as Element | null;
      if (target !== null && typeof target.releasePointerCapture === "function") {
        try {
          target.releasePointerCapture(active.pointerId);
        } catch {
        }
      }
    }
    if (cancelled && !active.dragging) {
      return;
    }
  }

  function onPointerUp(event: PointerEvent): void {
    endGesture(event, false);
  }

  function onPointerCancel(event: PointerEvent): void {
    endGesture(event, true);
  }

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

  options.host.addEventListener("pointerdown", onPointerDown);
  options.host.addEventListener("pointermove", onPointerMove);
  options.host.addEventListener("pointerup", onPointerUp);
  options.host.addEventListener("pointercancel", onPointerCancel);
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
    isObserving(): boolean {
      return observing;
    },
    destroy(): void {
      if (destroyed) {
        return;
      }
      destroyed = true;
      gesture = null;
      selected = null;
      options.host.removeEventListener("pointerdown", onPointerDown);
      options.host.removeEventListener("pointermove", onPointerMove);
      options.host.removeEventListener("pointerup", onPointerUp);
      options.host.removeEventListener("pointercancel", onPointerCancel);
      options.host.removeEventListener("keydown", onKeydown);
      options.host.ownerDocument.removeEventListener("pointerdown", onOutsidePointerDown, true);
      observer?.disconnect();
      observer = null;
      observing = false;
      geometry = null;
    },
  };
}
