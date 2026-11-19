// The documentation screenshots of 3.0 (the house card, the heat card, the settings popover pages, the beta box
// and the hero picture), taken in the light theme against the throwaway Home Assistant of `run.sh`.
// Usage: node v3shots.mjs [shot-name ...]   Environment: HA_URL, REPO_ROOT, REUSE=1 (keep the installation of an
// earlier run of a live instance instead of setting it up again).
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import * as ha from "./ha.mjs";
import * as v3 from "./v3setup.mjs";
import { Browser, sleep } from "./browser.mjs";

const REPO = process.env.REPO_ROOT || path.resolve(import.meta.dirname, "../../..");
const OUT = path.join(REPO, "docs", "images");
const only = new Set(process.argv.slice(2));
const wanted = (name) => only.size === 0 || only.has(name);
const done = [], failed = [];

const api = await v3.signedIn();
let ids;
if (process.env.REUSE) {
  const entries = await api.entries("spotnav");
  ids = { garage: entries.find((e) => e.title === "Garage").entry_id, site: entries.find((e) => e.title === "Home").entry_id };
} else {
  ids = await v3.setupInstall(api);
}
// A site that has only just been set up has read nothing yet: its energy flow, the day's plan and the heat's figures come
// from the first minutes of reading, and an answer asked for earlier keeps the finished hours it saw (a house card
// answer asked for too early shows a day without its heat). Give it a minute and a half before the first question.
if (!process.env.REUSE) await sleep(90000);
for (let i = 0; i < 60; i++) {
  const heat = await api.ws({ type: "spotnav/get_heat", api_version: 1, site_id: ids.site });
  if (heat.today?.heat_kwh != null && heat.hot_water?.planned?.length) break;
  await sleep(3000);
}
const tokens = await ha.login();
const b = await Browser.launch({ width: 1280, height: 1800 });
await b.signIn(tokens);
await b.send("Page.addScriptToEvaluateOnNewDocument", { source: `try { localStorage.setItem("selectedTheme", '{"dark":false}'); } catch (e) {}` });
await b.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });

const dashboard = async (path_, title, cards, panel = false) => {
  try {
    await api.ws({ type: "lovelace/dashboards/create", url_path: path_, mode: "storage", title, icon: "mdi:home", show_in_sidebar: false, require_admin: false });
  } catch {}
  await api.ws({ type: "lovelace/config/save", url_path: path_, config: { title, views: [{ title, path: "view", panel, cards }] } });
};
const CARD = (tag) => `window.__docs.deepAll("${tag}").filter(window.__docs.visible)[0]`;
const TAGS = { charger: "spotnav-card", house: "spotnav-house-card", heat: "spotnav-heat-card" };

await dashboard("v3-house", "House", [{ type: "custom:spotnav-house-card", site: ids.site }]);
await dashboard("v3-heat", "Heat", [{ type: "custom:spotnav-heat-card", site: ids.site }]);
await dashboard("v3-charger", "Charging", [{ type: "custom:spotnav-card", charger: ids.garage }]);

async function open(urlPath, tag, width = 1280, height = 1800) {
  await b.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
  await b.gotoReady(`${ha.BASE}/${urlPath}/view`, `!!${CARD(tag)} && !!${CARD(tag)}.shadowRoot && ${CARD(tag)}.shadowRoot.innerHTML.length > 2000`);
  await sleep(2500);
}
async function shot(name, expr, pad = 0) {
  if (!wanted(name)) return;
  try {
    const clip = await b.shotElement(path.join(OUT, name + ".png"), expr, pad);
    done.push(name);
    console.log(`   ${name}.png ${clip.width}x${clip.height}`);
  } catch (err) { failed.push(`${name}: ${err.message}`); console.log(`   ${name} FAILED: ${err.message}`); }
}

export { api, b, ids, open, shot, CARD, TAGS, wanted, sleep, OUT, done, failed, REPO };


const SCENE = { sun: 1.8, house: 1.1, heat: 1.7, battery: 0.5, soc: 64 };
/** Pin the live power sensors (the pictures do not depend on the hour of the day) and give them time to be read. */
async function scene(values = SCENE) {
  await api.post("/api/services/demo_house/docs_scene", values);
  // The battery's sensors are the plant's and tick every five seconds: wait until the pinned level has arrived.
  for (let i = 0; i < 20; i++) {
    const state = await api.get("/api/states/sensor.sigen_plant_ess_soc");
    if (Math.abs(Number(state.state) - values.soc) < 0.5) break;
    await sleep(1500);
  }
  await sleep(6000);
}
const tab = async (tag, name) => { await b.eval(`${CARD(tag)}.shadowRoot.querySelector("[role=tab][data-tab=${name}]").click()`); await sleep(2500); };
const gear = async (tag) => { await b.eval(`${CARD(tag)}.shadowRoot.querySelector("[data-settings=open]").click()`); await sleep(2500); };
const SECTION = (tag, name) => `${CARD(tag)}.shadowRoot.querySelector("section[data-section=${name}]")`;
const DIALOG = (tag) => `(() => { const l = [...${CARD(tag)}.shadowRoot.querySelectorAll(".spotnav-dialog")]; return l[l.length - 1] || null; })()`;
const closeDialogs = async (tag) => {
  for (let i = 0; i < 3; i++) {
    await b.eval(`(() => { const d = ${DIALOG(tag)}; const x = d && [...d.querySelectorAll("button")].find((e) => /^(Close|Cancel)$/i.test((e.innerText || "").trim()) || /^(Close|Cancel)$/i.test(e.getAttribute("aria-label") || "")); if (x) x.click(); })()`);
    await sleep(500);
  }
};

// ----------------------------------------------------------------------------------------------- the cards

if (wanted("house-card-now")) {
  await scene();
  await open("v3-house", TAGS.house, 700);
  await shot("house-card-now", CARD(TAGS.house));
}
if (wanted("heat-card-now")) {
  await scene();
  await open("v3-heat", TAGS.heat, 700);
  await shot("heat-card-now", CARD(TAGS.heat));
}

// The three cards as one picture: each is taken alone at its own width, then the three are set side by side on the
// dashboard's light background in a page of their own (a dashboard column would stretch them to equal heights).
if (wanted("readme-hero") || wanted("docs-home")) {
  await scene();
  const parts = [];
  for (const [page, tag] of [["v3-charger", TAGS.charger], ["v3-house", TAGS.house], ["v3-heat", TAGS.heat]]) {
    await open(page, tag, 700);
    const file = path.join(os.tmpdir(), `docs-hero-${process.pid}-${parts.length}.png`);
    await b.shotElement(file, CARD(tag), 0);
    parts.push(file);
  }
  const imgs = parts.map((f) => `<img src="data:image/png;base64,${fs.readFileSync(f).toString("base64")}">`).join("");
  const html = `<!doctype html><meta charset=utf-8><style>html{background:#f6f6f6}body{margin:0;width:1600px;height:660px;display:flex;gap:28px;
    justify-content:center;align-items:flex-start;padding-top:40px;box-sizing:border-box}img{display:block}</style>${imgs}`;
  await b.send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 660, deviceScaleFactor: 1, mobile: false });
  await b.goto("data:text/html;base64," + Buffer.from(html).toString("base64"), 1500);
  if (wanted("readme-hero")) {
    await b.shotRect(path.join(OUT, "readme-hero.png"), { x: 0, y: 0, w: 1600, h: 660 });
    done.push("readme-hero");
  }
  if (wanted("docs-home")) {
    fs.copyFileSync(path.join(OUT, "readme-hero.png"), path.join(OUT, "docs-home.png"));
    done.push("docs-home");
  }
  for (const f of parts) fs.rmSync(f, { force: true });
}

// ----------------------------------------------------------------------------------------------- the settings

if (wanted("settings-beta") || wanted("settings-home-battery")) {
  // Beta on: the box says so, and a plant not yet tested on site may be checked.
  await api.ws({ type: "spotnav/beta", api_version: 1, action: "set", changes: { on: true } });
}
if (wanted("settings-beta")) {
  await open("v3-house", TAGS.house, 900, 1400);
  await gear(TAGS.house);
  await b.eval(`${SECTION(TAGS.house, "support")}?.scrollIntoView({ block: "nearest" })`);
  await b.eval(`${CARD(TAGS.house)}.shadowRoot.querySelector("[data-edit=beta]").click()`);
  await sleep(2000);
  await b.eval(`(() => { const d = ${DIALOG(TAGS.house)}; [...d.querySelectorAll("button")].find((e) => /^Read more/i.test((e.innerText || "").trim())).click(); })()`);
  await sleep(1200);
  await shot("settings-beta", DIALOG(TAGS.house));
}
if (wanted("settings-home-battery")) {
  // Battery control on: the plant is checked step by step (about twenty minutes here: the demo battery obeys, the
  // driver waits for the result) and the section shows each step's outcome. The battery has to be discharging for the
  // check to start, so the scene is a cloudy evening.
  await scene({ sun: 0.4, house: 1.1, heat: 1.7, battery: -1.2, soc: 64 });
  const change = { type: "spotnav/battery_control", api_version: 1, site_id: ids.site };
  await api.ws({ ...change, changes: { battery_control_enabled: true }, enable_control_entities: true });
  let state = null;
  for (let i = 0; i < 180 && state !== "on"; i++) {
    await sleep(15000);
    state = (await api.ws(change)).status?.state;
  }
  console.log(`   battery control: ${state}`);
  await open("v3-house", TAGS.house, 900, 1400);
  await gear(TAGS.house);
  await b.eval(`${SECTION(TAGS.house, "battery")}?.scrollIntoView({ block: "nearest" })`);
  await sleep(500);
  if (state === "on") await shot("settings-home-battery", SECTION(TAGS.house, "battery"), 8);
  else failed.push(`settings-home-battery: the check did not finish (state ${state})`);
}

// ----------------------------------------------------------------------------------------------- the hot-water plan

if (wanted("heat-card-hot-water")) {
  await scene();
  // A boost is planned only for the hours still to come today, so the picture needs one: waited for (it comes with
  // midnight at the latest, when tomorrow's cheap hours are today's).
  const left = async () => {
    const heat = await api.ws({ type: "spotnav/get_heat", api_version: 1, site_id: ids.site });
    const day = (iso) => new Date(iso).toLocaleDateString("sv-SE", { timeZone: heat.time_zone });
    return (heat.hot_water?.planned ?? []).filter((p) => day(p.start) === heat.band?.date).length;
  };
  const patience = Number(process.env.HOT_WATER_WAIT_MIN ?? 150);
  for (let i = 0; i < patience / 2 && (await left()) === 0; i++) {
    if (i === 0) console.log("   heat-card-hot-water: no run is planned for the rest of today yet; waiting");
    await sleep(120000);
  }
  if ((await left()) === 0) {
    failed.push("heat-card-hot-water: no hot-water run was planned for the rest of today (HOT_WATER_WAIT_MIN)");
  } else {
    await open("v3-heat", TAGS.heat, 700);
    await tab(TAGS.heat, "plan");
    await shot("heat-card-hot-water", CARD(TAGS.heat));
  }
}

// The house card in the light theme at twice the pixels, for the banner and the README's card and app picture
// (tools/social/). Not a docs picture, so it goes to tools/social/card-light.png. Only on request:
// `run.sh social-card` (the driver is `DRIVER=v3shots.mjs`, as for every picture here).
if (only.has("social-card")) {
  await scene();
  await b.send("Emulation.setDeviceMetricsOverride", { width: 700, height: 1800, deviceScaleFactor: 2, mobile: false });
  await b.gotoReady(`${ha.BASE}/v3-house/view`, `!!${CARD(TAGS.house)} && ${CARD(TAGS.house)}.shadowRoot.innerHTML.length > 2000`);
  await sleep(2500);
  try {
    const file = path.join(REPO, "tools", "social", "card-light.png");
    const clip = await b.shotElement(file, CARD(TAGS.house), 0);
    done.push("social-card");
    console.log(`   social-card ${clip.width}x${clip.height} (twice the pixels)`);
  } catch (err) { failed.push(`social-card: ${err.message}`); }
}

await b.close();
console.log(`\nwritten: ${done.length}  failed: ${failed.length}`);
for (const f of failed) console.log("FAILED " + f);
process.exit(failed.length ? 1 : 0);
