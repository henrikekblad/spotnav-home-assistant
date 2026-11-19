// The one pointer policy every chart shares. A mouse follows the pointer as it moves (hover), with or
// without a button held. A touch or pen press starts a gesture that becomes a scrub once a small threshold
// proves it horizontal: the pointer is then captured and the moves are consumed. A gesture that proves
// vertical is left to the page's scroll, which the host allows with `touch-action: pan-y`. What a pointer
// picks is the caller's business; charts pick by the pointer's x alone.

/**
 * How far a gesture must travel before its direction is trusted, in CSS pixels. Small because a
 * quarter-hour slot is about 2.8 px wide on a 320 px graph; direction, not distance, protects the
 * dashboard's scroll.
 */
export const SCRUB_THRESHOLD = 4;

export interface ScrubHandlers {
  /** A mouse moved over the host. */
  hover?(event: PointerEvent): void;
  /** The mouse left the host. */
  leave?(event: PointerEvent): void;
  /** A press of any pointer. */
  press?(event: PointerEvent): void;
  /** A touch or pen press lifted without having become a scrub or a scroll: a tap. */
  tap?(event: PointerEvent): void;
  /** A move of a touch or pen scrub, once proven horizontal. */
  drag(event: PointerEvent): void;
  /** A scrub ended: lifted, or cancelled by the platform. Not called for a tap or a scroll. */
  end?(event: PointerEvent, cancelled: boolean): void;
}

export interface Scrub {
  /** Remove every listener; a gesture under way is forgotten. */
  detach(): void;
}

export function attachScrub(host: HTMLElement, handlers: ScrubHandlers, threshold = SCRUB_THRESHOLD): Scrub {
  let gesture: {
    pointerId: number;
    startX: number;
    startY: number;
    dragging: boolean;
    abandoned: boolean;
    captured: boolean;
  } | null = null;

  const isMouse = (event: PointerEvent): boolean => event.pointerType === "mouse";

  function onDown(event: PointerEvent): void {
    handlers.press?.(event);
    if (isMouse(event)) {
      return;
    }
    gesture = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      dragging: false,
      abandoned: false,
      captured: false,
    };
  }

  function onMove(event: PointerEvent): void {
    if (isMouse(event)) {
      handlers.hover?.(event);
      return;
    }
    if (gesture === null || gesture.pointerId !== event.pointerId || gesture.abandoned) {
      return;
    }
    if (!gesture.dragging) {
      const dx = event.clientX - gesture.startX;
      const dy = event.clientY - gesture.startY;
      if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) >= threshold) {
        gesture.abandoned = true; // A scroll: not consumed, and not a tap either.
        return;
      }
      if (Math.abs(dx) < threshold || Math.abs(dx) <= Math.abs(dy)) {
        return;
      }
      gesture.dragging = true;
      gesture.captured = capture(gesture.pointerId);
    }
    event.preventDefault();
    handlers.drag(event);
  }

  function capture(pointerId: number): boolean {
    if (typeof host.setPointerCapture !== "function") {
      return false;
    }
    try {
      host.setPointerCapture(pointerId);
      return true;
    } catch {
      return false;
    }
  }

  function finish(event: PointerEvent, cancelled: boolean): void {
    if (gesture === null || gesture.pointerId !== event.pointerId) {
      return;
    }
    const active = gesture;
    gesture = null;
    if (active.captured && typeof host.releasePointerCapture === "function") {
      try {
        host.releasePointerCapture(active.pointerId);
      } catch {
        // Already released by the platform.
      }
    }
    if (active.dragging) {
      handlers.end?.(event, cancelled);
    } else if (!cancelled && !active.abandoned) {
      handlers.tap?.(event);
    }
  }

  const onUp = (event: PointerEvent): void => finish(event, false);
  const onCancel = (event: PointerEvent): void => finish(event, true);
  const onLeave = (event: PointerEvent): void => {
    if (isMouse(event)) {
      handlers.leave?.(event);
    }
  };

  host.addEventListener("pointerdown", onDown);
  host.addEventListener("pointermove", onMove);
  host.addEventListener("pointerup", onUp);
  host.addEventListener("pointercancel", onCancel);
  host.addEventListener("pointerleave", onLeave);
  return {
    detach(): void {
      gesture = null;
      host.removeEventListener("pointerdown", onDown);
      host.removeEventListener("pointermove", onMove);
      host.removeEventListener("pointerup", onUp);
      host.removeEventListener("pointercancel", onCancel);
      host.removeEventListener("pointerleave", onLeave);
    },
  };
}

/** The index of the element whose horizontal span is nearest a client x; `null` for none. */
export function indexAtClientX(nodes: readonly Element[], clientX: number): number | null {
  let best: number | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  nodes.forEach((node, index) => {
    const box = node.getBoundingClientRect();
    const distance = clientX < box.left ? box.left - clientX : clientX > box.right ? clientX - box.right : 0;
    if (distance < bestDistance) {
      best = index;
      bestDistance = distance;
    }
  });
  return best;
}

/**
 * A row of bar buttons (the history charts): a horizontal touch drag over the row shows the bar under the
 * finger as it moves and keeps the one it ends on. A scrub the platform cancels (a system gesture) keeps
 * nothing: `restore` puts back what was shown before it. A mouse hovers the full-height buttons themselves.
 */
export function scrubBars(
  plot: HTMLElement,
  bars: readonly Element[],
  show: (index: number) => void,
  choose: (index: number) => void,
  restore?: () => void,
): Scrub {
  let scrubbed: number | null = null;
  return attachScrub(plot, {
    drag(event) {
      const index = indexAtClientX(bars, event.clientX);
      if (index !== null) {
        scrubbed = index;
        show(index);
      }
    },
    end(_event, cancelled) {
      if (cancelled) {
        restore?.();
      } else if (scrubbed !== null) {
        choose(scrubbed);
      }
      scrubbed = null;
    },
  });
}
