// Where the chart sits: over the car button and the action bar, or under them (`CHART_POSITION`, a layout trial).

import { describe, expect, it } from "vitest";

import { CHART_POSITION, createCardView, type CardViewInput } from "../src/card-view";
import { mountPoint, modelWith, NOW } from "./visual-fixtures";

const SOC = {
  value: 62, age_s: 600, source: "vehicle", estimated: false, target_percent: null, need_kwh: null,
  capacity_kwh: 77, vehicle_name: "EV6", vehicle_id: "ev6", vehicle_max_percent: 80, room_kwh: 14,
  efficiency: 0.9, vehicles: [], missing: [],
};

function view(chartPosition?: "top" | "bottom"): ShadowRoot {
  const mount = mountPoint("layout");
  const input: CardViewInput = {
    model: modelWith({ soc: SOC }),
    mount: mount.root,
    idPrefix: "layout",
    now: () => NOW,
    measure: () => ({ width: 320, height: 150 }),
    observe: () => null,
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
    ...(chartPosition === undefined ? {} : { chartPosition }),
  };
  createCardView(input);
  return mount.root;
}

/** The card's own blocks in order: the chart (`chart`), the car with the cells (`bar`). */
function order(root: ShadowRoot): string[] {
  const card = root.querySelector(".spotnav-card")!;
  return Array.from(card.children).flatMap((child) => {
    if (child.classList.contains("spotnav-graph")) {
      return ["chart"];
    }
    if (child.classList.contains("spotnav-action-bar-wrap")) {
      return ["bar"];
    }
    return [];
  });
}

describe("the chart's place", () => {
  it("is over the car and the cells by default", () => {
    expect(CHART_POSITION).toBe("top");
    expect(order(view())).toEqual(["chart", "bar"]);
    expect(order(view("top"))).toEqual(["chart", "bar"]);
  });

  it("is under them at the bottom, the car still first in the bar's block", () => {
    const root = view("bottom");
    expect(order(root)).toEqual(["bar", "chart"]);
    const wrap = root.querySelector(".spotnav-action-bar-wrap")!;
    expect(wrap.firstElementChild?.hasAttribute("data-car-button")).toBe(true);
    // The same one chart either way: one tab stop, one image.
    expect(root.querySelectorAll("[role='img'][tabindex='0']")).toHaveLength(1);
  });
});
