// The charger's status after its name: the word when it fits beside the name, else its symbol.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createCardView, statusFits, STATUS_GAP_PX, type CardViewInput, type StatusFitReading } from "../src/card-view";
import type { ChartObserverLike } from "../src/chart-interaction";
import type { CardModel } from "../src/model";
import type { ConnectionShownName } from "../src/types";
import { VISUAL_CLASSES as C, VISUAL_STYLES } from "../src/visual-styles";
import { baseModel, mountPoint } from "./visual-fixtures";

// The header's fit is written in an animation frame: a manual queue stands in for the browser's.
let frames = new Map<number, () => void>();
let nextFrame = 1;
const flushFrames = (): void => {
  const queued = Array.from(frames.values());
  frames = new Map();
  queued.forEach((callback) => callback());
};
beforeEach(() => {
  frames = new Map();
  nextFrame = 1;
  vi.stubGlobal("requestAnimationFrame", (callback: () => void) => {
    const handle = nextFrame++;
    frames.set(handle, callback);
    return handle;
  });
  vi.stubGlobal("cancelAnimationFrame", (handle: number) => {
    frames.delete(handle);
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function model(shown: ConnectionShownName, language: CardModel["language"] = "en"): CardModel {
  return {
    ...baseModel(),
    language,
    chargerName: "HALO Charger",
    chargeBar: null,
    connection: { state: shown === "waiting" ? "finished" : (shown as "finished"), source: null, shown },
  };
}

function headerView(
  cardModel: CardModel,
  read: (row: HTMLElement) => StatusFitReading,
  overrides: Partial<CardViewInput> = {},
) {
  const mount = mountPoint("header");
  let onResize: (() => void) | null = null;
  let observed: Element | null = null;
  let readings = 0;
  const view = createCardView({
    model: cardModel,
    mount: mount.root,
    idPrefix: "header",
    now: () => Date.parse("2026-09-22T04:30:00Z"),
    measure: () => ({ width: 300, height: 200 }),
    observe: () => null,
    observeBox: () => null,
    observeHeader: (_block, callback): ChartObserverLike => {
      onResize = callback;
      return {
        observe(element: Element) {
          observed = element;
        },
        disconnect() {
          onResize = null;
        },
      };
    },
    readStatusFit: (row) => {
      readings += 1;
      return read(row);
    },
    onAction: () => {},
    onOpenSettings: () => {},
    onSaveSettings: () => {},
    onReloadSettings: () => {},
    onReapplySettings: () => {},
    onOpenMarket: () => {},
    onSaveMarket: () => {},
    onReloadMarket: () => {},
    onReapplyMarket: () => {},
    onMarketAreaChange: () => {},
    isAdmin: true,
    onSelectStrategy: () => {},
    ...overrides,
  });
  const row = () => mount.root.querySelector<HTMLElement>(`.${C.nameRow}`);
  return {
    view,
    root: mount.root,
    row,
    /** The observer's callback only. */
    resize: () => onResize?.(),
    /** A resize and the frame that follows it. */
    fire: () => {
      onResize?.();
      flushFrames();
    },
    observed: () => observed,
    readings: () => readings,
  };
}

describe("statusFits", () => {
  it("fits when the name, the gap and the word fit the width beside the buttons", () => {
    expect(statusFits({ available: 200, name: 120, word: 200 - 120 - STATUS_GAP_PX })).toBe(true);
    expect(statusFits({ available: 200, name: 120, word: 200 - 120 - STATUS_GAP_PX + 1 })).toBe(false);
    // A name as wide as the block leaves no room for the word.
    expect(statusFits({ available: 150, name: 150, word: 40 })).toBe(false);
    // No name: the word alone.
    expect(statusFits({ available: 60, name: 0, word: 60 })).toBe(true);
    // Not laid out yet: the word, as before any reading.
    expect(statusFits({ available: 0, name: 0, word: 0 })).toBe(true);
  });
});

describe("the status in the header", () => {
  it("shows the word after the name when it fits, and its symbol right after the name when it does not", () => {
    let available = 400;
    const header = headerView(model("waiting", "sv"), () => ({ available, name: 120, word: 60 }));
    const row = header.row()!;
    expect(header.observed()).toBe(row.parentElement);
    // Before any reading: the word.
    expect(row.dataset["statusFit"]).toBe("word");
    header.fire();
    expect(row.dataset["statusFit"]).toBe("word");
    available = 150;
    header.fire();
    expect(row.dataset["statusFit"]).toBe("icon");
    const icon = row.querySelector<HTMLElement>(`.${C.connectionIcon}`)!;
    expect(icon.getAttribute("role")).toBe("img");
    expect(icon.title).toBe("Väntar");
    expect(icon.getAttribute("aria-label")).toBe("Väntar");
    // Right after the name, inside the status that follows it.
    expect(row.firstElementChild?.classList.contains(C.name)).toBe(true);
    expect(icon.parentElement?.previousElementSibling).toBe(row.firstElementChild);
    available = 400;
    header.fire();
    expect(row.dataset["statusFit"]).toBe("word");
  });

  it("measures the same whichever form shows, as the real layout does, so a second reading changes nothing", () => {
    const ICON = 16;
    const WORD = 60;
    // The word is hidden but keeps its width; the name shrinks by the icon and the gap when the icon shows.
    const layout = (natural: number, available: number) => (row: HTMLElement): StatusFitReading => ({
      available,
      name: row.dataset["statusFit"] === "icon" ? Math.min(natural, available - ICON - STATUS_GAP_PX) : Math.min(natural, available),
      word: WORD,
    });
    for (const [natural, available] of [
      [120, 400],
      [120, 150],
      [138, 200],
      [130, 200],
      [150, 150],
    ] as const) {
      const header = headerView(model("waiting"), layout(natural, available));
      header.fire();
      const first = header.row()!.dataset["statusFit"];
      for (let again = 0; again < 3; again += 1) {
        header.fire();
        expect(header.row()!.dataset["statusFit"], `${natural} in ${available}`).toBe(first);
      }
      expect(first).toBe(natural + STATUS_GAP_PX + WORD <= available + 0.5 ? "word" : "icon");
    }
  });

  it("writes the fit in the next frame, once for several resizes, and not after destroy", () => {
    const header = headerView(model("waiting"), () => ({ available: 150, name: 120, word: 60 }));
    const row = header.row()!;
    header.resize();
    header.resize();
    // Nothing is written inside the observer's callback.
    expect(row.dataset["statusFit"]).toBe("word");
    expect(header.readings()).toBe(0);
    expect(frames.size).toBe(1);
    flushFrames();
    expect(row.dataset["statusFit"]).toBe("icon");
    expect(header.readings()).toBe(1);
    header.resize();
    header.view.destroy();
    expect(frames.size).toBe(0);
    flushFrames();
    expect(header.readings()).toBe(1);
  });

  it("gives every shown state its own symbol", () => {
    const paths = new Map<string, string>();
    for (const shown of ["disconnected", "connected", "charging", "paused", "waiting", "finished", "error"] as const) {
      const header = headerView(model(shown), () => ({ available: 100, name: 120, word: 60 }));
      header.fire();
      const status = header.row()!.querySelector<HTMLElement>("[data-connection]")!;
      expect(status.dataset["connection"]).toBe(shown);
      paths.set(shown, status.querySelector(`.${C.connectionIcon} path`)!.getAttribute("d")!);
    }
    expect(new Set(paths.values()).size).toBe(7);
  });

  it("has no status, symbol or observer for an unknown state", () => {
    const header = headerView(model("unknown"), () => ({ available: 100, name: 120, word: 60 }));
    expect(header.root.querySelector("[data-connection]")).toBeNull();
    expect(header.observed()).toBeNull();
  });

  it("disconnects the header observer on destroy", () => {
    let disconnected = false;
    const header = headerView(model("waiting"), () => ({ available: 100, name: 120, word: 60 }), {
      observeHeader: () => ({
        observe() {},
        disconnect() {
          disconnected = true;
        },
      }),
    });
    header.view.destroy();
    expect(disconnected).toBe(true);
  });

  it("hides the symbol unless the word does not fit, and keeps the hidden word's width", () => {
    expect(VISUAL_STYLES).toMatch(/\.spotnav-connection-icon \{\s*display: none;/);
    expect(VISUAL_STYLES).toMatch(/\[data-status-fit="icon"\] \.spotnav-connection-icon \{\s*display: inline-flex;/);
    expect(VISUAL_STYLES).toMatch(/\[data-status-fit="icon"\] \.spotnav-connection-word \{\s*position: absolute;\s*inset-inline-start: 0;\s*top: 0;\s*visibility: hidden;/);
  });

  it("pins the hidden word inside the row, so it cannot stick out and scroll the card sideways", () => {
    const rule = VISUAL_STYLES.match(/\[data-status-fit="icon"\] \.spotnav-connection-word \{([^}]*)\}/)![1]!;
    expect(rule).toMatch(/position: absolute;/);
    expect(rule).toMatch(/inset-inline-start: 0;/);
    expect(rule).toMatch(/top: 0;/);
    // Its width still counts for the reading: it is hidden, not removed or collapsed.
    expect(rule).not.toMatch(/display: none|width: 0|clip/);
    expect(VISUAL_STYLES).toMatch(/\.spotnav-name-row \{\s*position: relative;/);
  });

  it("keeps the gap the fit adds for in step with the name row's column-gap", () => {
    const rule = VISUAL_STYLES.match(/\.spotnav-name-row \{\s*display: flex;([^}]*)\}/)![1]!;
    expect(rule).toContain(`column-gap: ${STATUS_GAP_PX}px;`);
  });
});
