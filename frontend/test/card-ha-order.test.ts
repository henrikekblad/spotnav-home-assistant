// How Home Assistant's hui-card drives a custom card: `setConfig(config)` with the whole card config,
// then `hass`, `preview` and `layout`; a throw from any of those makes HA show its bare
// "Configuration error" card. These cases pin that none of them throws, in either order.

import { beforeEach, describe, expect, it } from "vitest";

import { FakeHass, defineElements } from "./helpers";
import type { SpotnavCard } from "../src/card";

const config = { type: "custom:spotnav-card", charger: "entry_a" };

function make(): SpotnavCard {
  defineElements();
  return document.createElement("spotnav-card") as SpotnavCard;
}

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("the order Home Assistant drives the card in", () => {
  it("setConfig before hass, then preview and layout, never throws", () => {
    const element = make();
    expect(() => {
      element.setConfig(config);
      element.hass = new FakeHass();
      (element as unknown as Record<string, unknown>)["preview"] = false;
      (element as unknown as Record<string, unknown>)["layout"] = "grid";
    }).not.toThrow();
  });

  it("hass before setConfig never throws and the card still works", () => {
    const element = make();
    expect(() => {
      element.hass = new FakeHass();
      element.setConfig(config);
      document.body.appendChild(element);
    }).not.toThrow();
  });

  it("a hass without a language, or assigned again before connecting, never throws", () => {
    const element = make();
    expect(() => {
      element.setConfig(config);
      element.hass = { callWS: () => new Promise(() => undefined) } as never;
      element.hass = new FakeHass();
      document.body.appendChild(element);
      element.hass = new FakeHass();
    }).not.toThrow();
  });

  it("setConfig with the keys Home Assistant adds to every card never throws", () => {
    const element = make();
    expect(() =>
      element.setConfig({
        ...config,
        grid_options: { columns: 12, rows: "auto" },
        visibility: [{ condition: "user", users: ["a"] }],
        view_layout: { column: 2 },
        layout_options: { grid_columns: 4 },
      }),
    ).not.toThrow();
  });

  it("an empty config (the picker's stub) never throws", () => {
    const element = make();
    expect(() => element.setConfig({ type: "custom:spotnav-card" })).not.toThrow();
  });
});
