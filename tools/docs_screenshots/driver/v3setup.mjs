// The 3.0 demo installation, shared by the documentation screenshots and the app's: onboarding, the demo
// integrations, a SpotNav charger ("Garage") and a site ("Home"), all through the config flow API.
import * as ha from "./ha.mjs";
import * as demo from "./demo.mjs";
import { sleep } from "./ha.mjs";

export const log = (text) => console.log("== " + text);

/** Answer a config flow form by form: each form gets the values `answer(step)` returns for it. */
export async function flow(api, handler, answer) {
  let r = await api.post("/api/config/config_entries/flow", { handler, show_advanced_options: false });
  for (let i = 0; i < 20 && (r.type === "form" || r.type === "menu"); i++) {
    const reply = answer(r);
    if (reply === undefined) throw new Error(`${handler}: no answer for step ${r.step_id}: ${JSON.stringify(r).slice(0, 400)}`);
    r = await api.post(`/api/config/config_entries/flow/${r.flow_id}`, reply);
    if (r.errors && Object.keys(r.errors).length) throw new Error(`${handler} ${r.step_id}: ${JSON.stringify(r.errors)}`);
  }
  if (r.type !== "create_entry") throw new Error(`${handler}: flow ended with ${JSON.stringify(r).slice(0, 400)}`);
  return r;
}

export async function waitForState(api, entityId, timeoutS = 90) {
  for (let i = 0; i < timeoutS * 2; i++) {
    const r = await fetch(`${ha.BASE}/api/states/${entityId}`, { headers: api.h });
    if (r.ok) return;
    await sleep(500);
  }
  throw new Error("no state for " + entityId);
}

export async function signedIn() {
  await ha.waitUp();
  await ha.onboard();
  return new ha.Api(await ha.login());
}

export const DEMOS = ["ocpp", "sigen", "kia_uvo", "demo_vision", "nordpool", "demo_house"];

/** Onboard, add the demo integrations and the SpotNav charger and site. Returns the helpers the pictures need. */
export async function setupInstall(api, { appUrl } = {}) {
  await ha.promoteHttp(api);
  await ha.setLocation(api);
  if (appUrl) await api.ws({ type: "config/core/update", internal_url: appUrl });
  for (const integration of DEMOS) {
    if ((await api.entries(integration)).length === 0) await api.flow(integration);
  }
  for (const entry of await api.entries("spotnav")) {
    await fetch(`${ha.BASE}/api/config/config_entries/entry/${entry.entry_id}`, { method: "DELETE", headers: api.h });
  }
  await waitForState(api, "sensor.sigen_plant_grid_phase_c_active");
  await seedHouse(api);
  const devices = await api.ws({ type: "config/device_registry/list" });
  const deviceId = (name) => {
    const found = devices.find((d) => d.name === name);
    if (!found) throw new Error("no demo device " + name);
    return found.id;
  };
  log("SpotNav charger");
  await flow(api, "spotnav", (s) => ({
    user: { entry_type: "charger" },
    charger: { mode: "detected" },
  }[s.step_id] ?? (s.data_schema?.some((f) => f.name === "device") ? { device: deviceId("Garage") }
    : s.data_schema?.some((f) => f.name === "charger_phases") ? { charger_phases: "3" } : {})));
  const garage = (await api.entries("spotnav")).find((e) => e.title === "Garage").entry_id;
  log("camera and the first car");
  await demo.setupCamera(api, garage);
  await demo.decideFamilyCar(api, garage);
  log("SpotNav site");
  await flow(api, "spotnav", (s) => {
    if (s.step_id === "user") return { entry_type: "site" };
    const names = (s.data_schema || []).map((f) => f.name);
    if (names.includes("main_fuse_a")) return { name: "Home", main_fuse_a: 25 };
    if (names.includes("phases")) return { phases: 3 };
    return {};
  });
  const entries = await api.entries("spotnav");
  const site = entries.find((e) => e.title === "Home").entry_id;
  await setupHeat(api, site);
  // A fully set-up installation: every "Get started" row left is answered "Not needed", as a person would.
  log("Get started rows answered");
  for (const item of (await demo.dashboard(api, garage)).setup?.items ?? []) {
    if (item.dismissible) await api.ws({ type: "spotnav/dismiss_setup_item", item: item.id });
  }
  return { garage, site };
}

/** The made-up house's history as recorder statistics, and the Energy dashboard's meters that point at it. */
async function seedHouse(api) {
  await waitForState(api, "sensor.demo_heat_pump_energy");
  await waitForState(api, "sensor.sigen_plant_ess_soc");
  await api.post("/api/services/demo_house/docs_seed", {});
  // The recorder imports in its own thread: the site must not read before the last hour and the last five-minute
  // rows are in, or it keeps what it read of a half-finished import.
  const start = new Date(Date.now() - 3 * 3600_000).toISOString(), end = new Date().toISOString();
  for (let i = 0; i < 120; i++) {
    const hours = await api.ws({ type: "recorder/statistics_during_period", start_time: start, end_time: end, statistic_ids: ["sensor.demo_heat_pump_energy", "sensor.demo_solar_energy"], period: "hour", types: ["change"] });
    const five = await api.ws({ type: "recorder/statistics_during_period", start_time: start, end_time: end, statistic_ids: ["sensor.demo_heat_pump_energy", "sensor.demo_solar_energy"], period: "5minute", types: ["change"] });
    if ((hours["sensor.demo_solar_energy"]?.length ?? 0) >= 2 && (five["sensor.demo_solar_energy"]?.length ?? 0) >= 20) break;
    await sleep(2000);
  }
  await sleep(3000);
  await api.ws({
    type: "energy/save_prefs",
    energy_sources: [
      { type: "grid", stat_energy_from: "sensor.demo_grid_import_energy", stat_energy_to: "sensor.demo_grid_export_energy", cost_adjustment_day: 0 },
      { type: "solar", stat_energy_from: "sensor.demo_solar_energy", stat_rate: "sensor.demo_solar_power", config_entry_solar_forecast: null },
      { type: "battery", stat_energy_from: "sensor.demo_battery_discharge_energy", stat_energy_to: "sensor.demo_battery_charge_energy", stat_soc: "sensor.sigen_plant_ess_soc" },
    ],
    device_consumption: [{ stat_consumption: "sensor.demo_heat_pump_energy", stat_rate: "sensor.demo_heat_pump_power", name: "Heat pump" }],
    device_consumption_water: [],
  });
}

/** Heating on the site: the pump's meter, the rooms, a relay that lowers the curve and a hot-water boost switch. */
async function setupHeat(api, site) {
  const current = (await api.ws({ type: "spotnav/get_heat", api_version: 1, site_id: site })).config;
  const changes = {
    indoor_sensors: ["sensor.living_room_temperature", "sensor.bedroom_temperature", "sensor.office_temperature"],
    outdoor_sensor: "sensor.outdoor_temperature",
    control_path: "relay", control_entity: "switch.heat_pump_curve_relay",
    hot_water_entity: "switch.hot_water_boost", heat_meter_entity: "sensor.demo_heat_pump_power",
  };
  const expected = Object.fromEntries(Object.keys(changes).map((k) => [k, current[k]]));
  const r = await api.ws({ type: "spotnav/update_heat", api_version: 1, site_id: site, changes, expected });
  if (!r.ok) throw new Error("heat settings: " + JSON.stringify(r).slice(0, 400));
}
