// Takes the documentation screenshots of the SpotNav integration in a local, throwaway Home Assistant.
// Usage: node shots.mjs [shot-name ...]      (no names: every shot)
// Environment: HA_URL (default http://127.0.0.1:8129), REPO_ROOT (the repository, for docs/images).
import fs from "node:fs";
import path from "node:path";
import * as ha from "./ha.mjs";
import { Browser, sleep } from "./browser.mjs";

const REPO = process.env.REPO_ROOT || path.resolve(import.meta.dirname, "../../..");
const OUT = path.join(REPO, "docs", "images");
const only = new Set(process.argv.slice(2));
const wanted = (name) => only.size === 0 || only.has(name);
const done = [], skipped = [], failed = [];

const DIALOG = `(() => { const D = window.__docs; const l = D.deepAll("dialog[part~=dialog]").filter(D.visible); return l[l.length - 1] || null; })()`;

// ------------------------------------------------------------------------------------------------ set-up

await ha.waitUp();
await ha.onboard();
const tokens = await ha.login();
const api = new ha.Api(tokens);
await ha.promoteHttp(api);
await ha.setLocation(api);
for (const demo of ["ocpp", "sigen", "kia_uvo"]) {
  if ((await api.entries(demo)).length === 0) await api.flow(demo);
}
// A clean slate for the integration under test, so a re-run against a live instance starts the same way.
for (const entry of await api.entries("spotnav")) {
  await fetch(`${ha.BASE}/api/config/config_entries/entry/${entry.entry_id}`, { method: "DELETE", headers: api.h });
}
await waitForState("sensor.sigen_plant_grid_phase_c_active");

async function waitForState(entityId, timeoutS = 60) {
  for (let i = 0; i < timeoutS * 2; i++) {
    const r = await fetch(`${ha.BASE}/api/states/${entityId}`, { headers: api.h });
    if (r.ok) return;
    await sleep(500);
  }
  throw new Error("no state for " + entityId);
}

const devices = await api.ws({ type: "config/device_registry/list" });
const deviceId = (name) => {
  const found = devices.find((d) => d.name === name);
  if (!found) throw new Error("no demo device " + name);
  return found.id;
};

const b = await Browser.launch({ width: 1280, height: 1800 });
await b.signIn(tokens);

// ------------------------------------------------------------------------------------------------ helpers

async function shot(name, expr = DIALOG, pad = 0, { blur = true } = {}) {
  if (!wanted(name)) return;
  await sleep(700);
  if (blur && expr === DIALOG) await clickTitle();
  try {
    const clip = await b.shotElement(path.join(OUT, name + ".png"), expr, pad);
    done.push(name);
    console.log(`   ${name}.png ${clip.width}x${clip.height}`);
  } catch (err) {
    failed.push(`${name}: ${err.message}`);
    console.log(`   ${name} FAILED: ${err.message}`);
  }
}
/** Click the dialog's title so that no field keeps focus (no caret, no selection) in the picture. */
async function clickTitle() {
  const r = await b.rectOf(DIALOG);
  if (!r) return;
  const x = r.x + 200, y = r.y + 35;
  await b.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await b.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
  await sleep(300);
}
const submit = async () => { await b.click("ha-button", "^Submit$"); await sleep(1800); };
/** Close the flow dialog with its X button, which ends the flow without creating anything. */
async function cancelDialog() {
  const r = await b.rectOf(DIALOG);
  if (r) {
    await b.send("Input.dispatchMouseEvent", { type: "mousePressed", x: r.x + 32, y: r.y + 35, button: "left", clickCount: 1 });
    await b.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: r.x + 32, y: r.y + 35, button: "left", clickCount: 1 });
    await sleep(900);
  }
}
async function startSpotnavFlow() {
  await b.goto(ha.BASE + "/config/integrations/dashboard/add?domain=spotnav", 2500);
  await b.click("ha-button", "^OK$");
  await sleep(1500);
}
async function pick(text) {
  await b.click("ha-select", "");
  await b.click("ha-dropdown-item", text);
  await sleep(400);
}
/** Replace the text of the visible input that matches `selector` (and, when given, currently holds `current`). */
async function fillField(selector, value, current = null, nth = 0) {
  await b.eval(`(() => { const D = window.__docs; const i = D.deepAll(${JSON.stringify(selector)}).filter(D.visible)
    .filter((e) => ${JSON.stringify(current)} === null || e.value === ${JSON.stringify(current)})[${nth}]; i.focus(); i.select(); })()`);
  await b.type(String(value));
}
const step = (text) => console.log("== " + text);

// ------------------------------------------------------------------------------------------------ charger

step("charger flow");
await startSpotnavFlow();
await shot("add-type");
await submit();
await shot("charger-type");
await submit();
await b.click("ha-select", "");
await sleep(700);
await shot("charger-device", DIALOG, 0, { blur: false });
await b.click("ha-dropdown-item", "Garage charger");
await sleep(500);
await submit();
await shot("charger-found");
await b.click("ha-formfield, ha-switch, ha-checkbox", "Change these choices");
await submit();
await shot("charger-adjust");
await cancelDialog();

// The charger itself, added for real so that the site and the card have something to work with.
const garage = await api.flow("spotnav", [{ entry_type: "charger" }, { mode: "detected" }, { device: deviceId("Garage charger Connector 1") }, {}]);
if (garage.type !== "create_entry") throw new Error("could not add the demo charger: " + JSON.stringify(garage).slice(0, 300));

// ------------------------------------------------------------------------------------------------ site

step("site flow");
await startSpotnavFlow();
await pick("site");
await submit();
await fillField("input[type=text]", "Home", "Site");
await fillField("input[type=number]", 25, "0");
await shot("site-basic");
await submit();
await shot("site-meter");
await submit();
await shot("site-found");
await submit();

// ------------------------------------------------------------------------------------------------ join site

step("join site");
await startSpotnavFlow();
await submit();
await submit();
await b.click("ha-select", "");
await b.click("ha-dropdown-item", "Workshop charger");
await sleep(500);
await submit();
await submit();
await shot("join-site");
await cancelDialog();

// ------------------------------------------------------------------------------------------------ entry pages

step("site options");
await b.goto(ha.BASE + "/config/integrations/integration/spotnav", 3000);
await b.click("ha-icon-button", "^Configure$", 1);
await sleep(1500);
await shot("site-options");
await cancelDialog();

step("diagnostics menu");
await b.goto(ha.BASE + "/config/integrations/integration/spotnav", 3000);
await b.click("ha-icon-button", "^Menu$", 1);
await sleep(900);
const MENU = `(() => { const D = window.__docs; const items = D.deepAll("ha-dropdown-item").filter(D.visible);
  if (!items.length) return null; const rs = items.map((e) => e.getBoundingClientRect());
  const x = Math.min(...rs.map((r) => r.x)), y = Math.min(...rs.map((r) => r.y));
  return { getBoundingClientRect: () => ({ x: x - 6, y: y - 6, width: Math.max(...rs.map((r) => r.right)) - x + 12, height: Math.max(...rs.map((r) => r.bottom)) - y + 12 }) }; })()`;
await shot("diagnostics", MENU, 6);
await b.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });

// ------------------------------------------------------------------------------------------------ dashboard

step("dashboard");
const charger = (await api.entries("spotnav")).find((e) => e.title.startsWith("Garage"));
try {
  await api.ws({ type: "lovelace/dashboards/create", url_path: "spotnav-demo", mode: "storage", title: "Charging", icon: "mdi:ev-station", show_in_sidebar: true, require_admin: false });
} catch {}
await api.ws({
  type: "lovelace/config/save", url_path: "spotnav-demo",
  config: { title: "Charging", views: [{ title: "Charging", path: "charging", cards: [{ type: "custom:spotnav-card", charger: charger.entry_id }] }] },
});
const CARD = `window.__docs.deepAll("spotnav-card").filter(window.__docs.visible)[0]`;
const CARD_DIALOG = `(() => { const D = window.__docs; const c = ${CARD}; const l = [...c.shadowRoot.querySelectorAll(".spotnav-dialog")].filter(D.visible); return l[l.length - 1] || null; })()`;
const cardReady = `!!${b.finder("spotnav-card")} && !!${CARD}.shadowRoot.querySelector(".spotnav-bar-cell")`;
const openCard = () => b.gotoReady(ha.BASE + "/spotnav-demo/charging", cardReady);
const cardButton = (label) => b.click("button", label);
async function closeCardDialog() { await b.click("button", "^(Cancel|Close)$"); await sleep(500); }
async function cardShot(name, expr, pad = 0) { await shot(name, expr, pad, { blur: false }); }

// Saving the plan settings is what confirms the first-run suggestions and lets the plan be installed.
async function saveAmps(amps) {
  await cardButton("^Plan:");
  await sleep(800);
  await fillField("input[type=number]", amps, null, 1);
  await b.click("button", "^Save$");
  await sleep(2500);
}
await openCard();
await sleep(1000);
await saveAmps(15);
await saveAmps(16);
await sleep(1500);
await cardShot("card-hero", CARD, 0);

step("card picker");
await b.gotoReady(ha.BASE + "/spotnav-demo/charging?edit=1", `!!${b.finder("ha-button", "^Add card$")}`);
await b.click("ha-button", "^Add card$");
await sleep(1500);
await b.click("*", "^By card$").catch(() => {});
await sleep(1000);
await b.eval(`(() => { const D = window.__docs; const i = D.deepAll("input").filter(D.visible).find((e) => /search/i.test(e.placeholder || e.getAttribute("aria-label") || "") || e.type === "search" || e.type === "text"); if (i) i.focus(); })()`);
await b.type("SpotNav");
await sleep(3000);
await shot("card-picker", DIALOG);
await b.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Escape", code: "Escape" });

step("card dialogs");
await openCard();
await cardButton("^Card settings$");
await sleep(800);
const HOME_SECTION = `(() => { const D = window.__docs; const c = ${CARD}; return [...c.shadowRoot.querySelectorAll("section.spotnav-settings-section")].find((e) => D.visible(e) && /Active load balancing/.test(D.text(e))) || null; })()`;
await shot("card-settings-site", HOME_SECTION, 8);
await cardButton("Edit price area and taxes");
await sleep(800);
await cardShot("card-settings-price", CARD_DIALOG);
await closeCardDialog();
await cardButton("Change vehicle");
await sleep(800);
await b.click("input[type=radio]", "", 0).catch(() => {});
await cardShot("card-settings-vehicle", CARD_DIALOG);
await closeCardDialog();
await closeCardDialog();
await openCard();
await cardButton("^Plan:");
await sleep(800);
await b.click("label, input[type=radio]", "Target SoC").catch(() => {});
await sleep(600);
await cardShot("card-settings-plan", CARD_DIALOG);
await closeCardDialog();

await b.close();
console.log(`\nwritten: ${done.length}  skipped: ${skipped.length}  failed: ${failed.length}`);
for (const f of failed) console.log("FAILED " + f);
process.exit(failed.length ? 1 : 0);
