// The charger's status after its name: the word when it fits beside the name, else its symbol.

import { describe, expect, it } from "vitest";

import { createCardView, statusFits, STATUS_GAP_PX, type CardViewInput, type StatusFitReading } from "../src/card-view";
import type { ChartObserverLike } from "../src/chart-interaction";
import type { CardModel } from "../src/model";
import type { ConnectionShownName } from "../src/types";
import { VISUAL_CLASSES as C, VISUAL_STYLES } from "../src/visual-styles";
import { baseModel, mountPoint } from "./visual-fixtures";

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
  return { view, root: mount.root, row, fire: () => onResize?.(), observed: () => observed, readings: () => readings };
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

  it("measures the same whichever form shows, so a second reading changes nothing", () => {
    const header = headerView(model("waiting"), () => ({ available: 150, name: 120, word: 60 }));
    header.fire();
    const first = header.row()!.dataset["statusFit"];
    header.fire();
    expect(header.row()!.dataset["statusFit"]).toBe(first);
    expect(header.readings()).toBe(2);
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
    expect(VISUAL_STYLES).toMatch(/\[data-status-fit="icon"\] \.spotnav-connection-word \{\s*position: absolute;\s*visibility: hidden;/);
  });
});
