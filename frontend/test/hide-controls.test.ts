// What the cards leave out where it could not act: the rules as pure functions, on the backend's own fixtures where
// they read a payload.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { importTargetShown, siteFollowsSolar, strategyRowHidden } from "../src/card-view";
import { decodeHeat, heatFound, heatSteers, type HeatAnswer } from "../src/heat-model";
import { houseTabs, shownHouseTab, tabWord, type HouseTabFacts } from "../src/house-tabs";
import { translate } from "../src/i18n";
import { decodeBatteryControl, decodeSiteView } from "../src/site-model";

const ROOT = join(__dirname, "..", "..", "tests", "fixtures");
const read = (...parts: string[]): Record<string, unknown> =>
  JSON.parse(readFileSync(join(ROOT, ...parts), "utf8")) as Record<string, unknown>;

describe("the charger card's strategy rows", () => {
  const solar = { id: "solar", available: false, reasonCode: "solar_needs_site" };
  it("hide a solar strategy held back for no solar from a household member, never from an administrator", () => {
    expect(strategyRowHidden(solar, false)).toBe(true);
    expect(strategyRowHidden(solar, true)).toBe(false);
  });

  it("keep the total-grid-power case, Cheapest and an available row for everyone", () => {
    expect(strategyRowHidden({ ...solar, reasonCode: "needs_total_grid_power" }, false)).toBe(false);
    expect(strategyRowHidden({ id: "cheapest", available: true, reasonCode: null }, false)).toBe(false);
    expect(strategyRowHidden({ id: "hybrid", available: true, reasonCode: null }, false)).toBe(false);
  });

  it("say the site follows solar only while no solar strategy is held back", () => {
    expect(siteFollowsSolar([{ id: "cheapest", available: true }, { id: "solar", available: true }, { id: "hybrid", available: true }])).toBe(true);
    expect(siteFollowsSolar([{ id: "cheapest", available: true }, { id: "solar", available: false }])).toBe(false);
  });
});

describe("the import target row", () => {
  const unmeasured = { available: false, reason: "needs_total_grid_power" };
  it("is hidden where the site cannot measure it and none is set", () => {
    expect(importTargetShown(unmeasured, null)).toBe(false);
  });

  it("stays where a target is set, where the site measures it, and where the card does not know", () => {
    expect(importTargetShown(unmeasured, 6)).toBe(true);
    expect(importTargetShown({ available: true, reason: null }, null)).toBe(true);
    expect(importTargetShown(null, null)).toBe(true);
  });
});

describe("the site view and the battery status", () => {
  it("read the new fields, and a Home Assistant before them as before", () => {
    const view = decodeSiteView(read("site_view", "no_charger.json"));
    expect(view.ok && view.value.import_target).toEqual({ available: false, reason: "needs_total_grid_power" });
    const older = { ...read("site_view", "villa.json") };
    delete older["import_target"];
    const decoded = decodeSiteView(older);
    expect(decoded.ok && decoded.value.import_target).toEqual({ available: true, reason: null });
    const battery = read("battery_control", "live.json");
    const status = { ...(battery["status"] as Record<string, unknown>) };
    expect(status["battery_found"]).toBe(true);
    delete status["battery_found"];
    const before = decodeBatteryControl({ ...battery, status });
    expect(before.ok && before.value.status?.battery_found).toBe(true);
    const none = decodeBatteryControl(read("webhook", "battery_control_success.json"));
    expect(none.ok && none.value.status?.battery_found).toBe(false);
  });
});

describe("the heat rules", () => {
  const heat = (name: string): HeatAnswer => {
    const decoded = decodeHeat(read("heat_card", name));
    if (!decoded.ok) {
      throw new Error(`${name} does not decode`);
    }
    return decoded.value;
  };

  it("steer on the paths SpotNav steers, or while control is on", () => {
    expect(heatSteers({ control_path: "none", control_enabled: false })).toBe(false);
    expect(heatSteers({ control_path: "setpoint", control_enabled: false })).toBe(false);
    expect(heatSteers({ control_path: "offset", control_enabled: false })).toBe(true);
    expect(heatSteers({ control_path: "none", control_enabled: true })).toBe(true);
  });

  it("find heating from a pump, a meter, zones, sensors or a path, and nothing on an empty site", () => {
    expect(heatFound(heat("villa.json"))).toBe(true);
    expect(heatFound(heat("network_pump.json"))).toBe(true);
    const empty = heat("network_pump.json");
    expect(
      heatFound({
        ...empty,
        pump: null,
        meter: null,
        zones: [],
        choices: null,
        config: { ...empty.config, control_path: "none", indoor_sensors: [], heat_meter_entity: null },
      }),
    ).toBe(false);
  });
});

describe("the house card's tabs", () => {
  const bare: HouseTabFacts = { battery: null, energy_flow: null, heat: null, saving: null, hours: [] };

  it("keep Now, Plan and Energy chart alone on a site with nothing else to show", () => {
    expect(houseTabs(bare)).toEqual(["now", "plan", "graph"]);
  });

  it("add each tab where it can fill, by the app's rules", () => {
    expect(houseTabs({ ...bare, hours: [{ buy: 120, sell: null }] })).toEqual(["now", "plan", "graph", "price"]);
    expect(houseTabs({ ...bare, energy_flow: { nodes: [{ role: "car" }] } })).toEqual(["now", "plan", "flow", "graph", "earn"]);
    expect(houseTabs({ ...bare, energy_flow: { nodes: [{ role: "house" }] } })).toEqual(["now", "plan", "flow", "graph"]);
    expect(houseTabs({ ...bare, heat: { status: "found" } })).toEqual(["now", "plan", "graph", "earn"]);
    expect(houseTabs({ ...bare, heat: { status: "ambiguous" } })).toEqual(["now", "plan", "graph"]);
    expect(houseTabs({ ...bare, saving: {} })).toEqual(["now", "plan", "graph", "earn"]);
    expect(houseTabs({ ...bare, battery: {} })).toEqual(["now", "plan", "graph", "earn", "price"]);
    expect(houseTabs(read("house", "villa_battery_sun.json") as unknown as HouseTabFacts)).toEqual(["now", "plan", "flow", "graph", "earn", "price"]);
  });

  it("show the chosen tab while the site has it, else Now, and a locked one as it is", () => {
    expect(shownHouseTab(null, "price", ["now", "graph"])).toBe("now");
    expect(shownHouseTab(null, "graph", ["now", "graph"])).toBe("graph");
    expect(shownHouseTab(null, null, ["now", "graph"])).toBe("now");
    expect(shownHouseTab("price", "graph", ["now", "graph"])).toBe("price");
  });

  it("call Price and battery Price without a battery, in every language", () => {
    expect(tabWord("en", "price")).toBe("Price and battery");
    expect(tabWord("en", "price", false)).toBe("Price");
    expect(tabWord("sv", "price", false)).toBe(translate("sv", "house.tab.priceOnly"));
    expect(tabWord("en", "now", false)).toBe(translate("en", "house.tab.now"));
  });
});
