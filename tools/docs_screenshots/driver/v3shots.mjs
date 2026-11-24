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
const t0 = Date.now();
const phase = (name) => console.error(`[timing] driver ${name}: ${Math.round((Date.now() - t0) / 1000)} s since the driver started`);

const api = await v3.signedIn();
let ids;
if (process.env.REUSE) {
  const entries = await api.entries("spotnav");
  ids = { garage: entries.find((e) => e.title === "Garage").entry_id, site: entries.find((e) => e.title === "Home").entry_id };
} else {
  ids = await v3.setupInstall(api);
}
phase("signed in, installation ready");
// A site that has only just been set up has read nothing yet: its energy flow, the day's plan and the heat's figures come
// from the first minutes of reading, and an answer asked for earlier keeps the finished hours it saw (a house card
// answer asked for too early shows a day without its heat). So a new site is polled until its answers have what a
// finished reading gives and stop changing; a reused one has read for a long time and is ready at once.
if (!process.env.REUSE) {
  await v3.settleSite(api, ids.site);
  phase("new site settled");
} else {
  // A reused installation was not running since it was kept: the hours between are imported again.
  await v3.importHistory(api);
  phase("history topped up");
}
// The heat's figures (the hot-water picture waits for its own boost below).
for (let i = 0; i < 60; i++) {
  const { heat } = await v3.siteAnswers(api, ids.site);
  if (heat.today?.heat_kwh != null) break;
  await sleep(3000);
}
phase("site answers ready");
// Tonight's bath and sauna session, booked again at every run (a kept installation's would be another day's).
await v3.bookPoolSpa(api, ids.site);
if (process.env.DOCS_CONFIG) {
  // The finished installation: what run.sh keeps for the next run (--fresh throws it away).
  try { fs.writeFileSync(path.join(process.env.DOCS_CONFIG, ".docs_built"), new Date().toLocaleDateString("sv-SE") + "\n"); } catch {}
}
const tokens = await ha.login();
const b = await Browser.launch({ width: 1280, height: 1800 });
await b.signIn(tokens);
phase("browser ready");
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

// The house card's other tabs, one picture each (docs/house-card.md), taken first and with no scene pinned: the plan and
// the battery's level are the simulation's, so the level measured and the level planned meet at now (a pinned level
// would jump there until the battery is planned again). Each is today, or the month where a day has too little
// (the earnings count a day only after its night's review; the month's chart has the house, the heat and the sun).
const HOUSE_TAB_SHOTS = [
  ["house-card-plan", "plan"], ["house-card-flow", "flow"], ["house-card-chart", "graph", "Month"],
  ["house-card-earnings", "earn", "Month"], ["house-card-price", "price"],
];
/** Open the Plan tab's folded sections ("Per load", "In words") so that the picture shows them. */
const unfoldPlan = async (tag) => {
  await b.eval(`[...${CARD(tag)}.shadowRoot.querySelectorAll(".spotnav-plan-toggle[aria-expanded=false]")].forEach((e) => e.click())`);
  await sleep(800);
};
if (HOUSE_TAB_SHOTS.some(([name]) => wanted(name))) {
  // The card reads its answer once on opening: wait until the battery plan has priced the hours (a minute or so after
  // the start), or Price and battery has no chart.
  for (let i = 0; i < 60; i++) {
    const house = await api.ws({ type: "spotnav/get_house", api_version: 1, site_id: ids.site }).catch(() => null);
    if ((house?.hours ?? []).some((hour) => hour.buy != null)) break;
    await sleep(3000);
  }
  await open("v3-house", TAGS.house, 700, 2400);
  for (const [name, tabName, period] of HOUSE_TAB_SHOTS) {
    if (!wanted(name)) continue;
    await tab(TAGS.house, tabName);
    if (tabName === "plan") await unfoldPlan(TAGS.house);
    // The period is shared between the tabs: each picture sets its own (Day for the rest).
    await b.eval(`[...${CARD(TAGS.house)}.shadowRoot.querySelectorAll("button")].find((e) => (e.innerText || "").trim() === "${period ?? "Day"}")?.click()`);
    await sleep(2500);
    await shot(name, CARD(TAGS.house));
  }
}
// The Plan tab stepped back to yesterday: planned against what ran, from the decision history seed_house.py gives.
if (wanted("house-card-plan-past")) {
  await open("v3-house", TAGS.house, 700, 2400);
  await tab(TAGS.house, "plan");
  for (let step = 0; step < 2; step++) {
    await b.eval(`${CARD(TAGS.house)}.shadowRoot.querySelector("[data-step=back]")?.click()`);
    await sleep(3000);
  }
  await unfoldPlan(TAGS.house);
  await shot("house-card-plan-past", CARD(TAGS.house));
}
if (wanted("house-card-now")) {
  await scene();
  await open("v3-house", TAGS.house, 700);
  // The card remembers its tab in the browser: an earlier picture's tab would stay.
  await tab(TAGS.house, "now");
  await shot("house-card-now", CARD(TAGS.house));
}
if (wanted("heat-card-now")) {
  await scene();
  await open("v3-heat", TAGS.heat, 700);
  // The card remembers its tab in the browser: an earlier picture's tab would stay.
  await tab(TAGS.heat, "now");
  await shot("heat-card-now", CARD(TAGS.heat));
}
// The heat card's Plan and Use tabs (docs/heat-pumps.md), on today.
for (const [name, tabName] of [["heat-card-plan", "plan"], ["heat-card-use", "use"]]) {
  if (!wanted(name)) continue;
  await scene();
  await open("v3-heat", TAGS.heat, 700);
  await tab(TAGS.heat, tabName);
  await shot(name, CARD(TAGS.heat));
}
// The heat card's Pool, Spa and Sauna tabs (docs/pool-spa.md): the demo pool filtering in the day's cheap hours, the
// spa heating for tonight's bath and the sauna waiting for this evening's session, all under SpotNav's control.
for (const [name, tabName] of [["heat-card-pool", "pool"], ["heat-card-spa", "spa"], ["heat-card-sauna", "sauna"]]) {
  if (!wanted(name)) continue;
  if (tabName === "pool") {
    // The water counts as the pool's only once the circulation has run five minutes since the start: waited for.
    for (let i = 0; i < 60; i++) {
      const heat = await api.ws({ type: "spotnav/get_heat", api_version: 1, site_id: ids.site });
      if (heat.pool?.water_c != null) break;
      await sleep(10000);
    }
  }
  await scene();
  await open("v3-heat", TAGS.heat, 700);
  await tab(TAGS.heat, tabName);
  await shot(name, CARD(TAGS.heat));
}

// The three cards as one picture: each is taken alone at its own width, then the three are set side by side on the
// dashboard's light background in a page of their own (a dashboard column would stretch them to equal heights).
if (wanted("readme-hero") || wanted("docs-home")) {
  await scene();
  const parts = [];
  for (const [page, tag] of [["v3-charger", TAGS.charger], ["v3-house", TAGS.house], ["v3-heat", TAGS.heat]]) {
    await open(page, tag, 700);
    if (tag !== TAGS.charger) await tab(tag, "now");
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
if (wanted("settings-support")) {
  // Support, drawn the same in every card (support-section.ts): the value rows, then the actions as rows.
  await open("v3-house", TAGS.house, 900, 1800);
  await gear(TAGS.house);
  await b.eval(`${SECTION(TAGS.house, "support")}?.scrollIntoView({ block: "nearest" })`);
  await sleep(1500);
  await shot("settings-support", SECTION(TAGS.house, "support"));
}
// The site's priority list (docs/site-and-load-balancing.md): the editor an administrator opens from Site → Priority.
if (wanted("settings-priority")) {
  await open("v3-house", TAGS.house, 900, 1800);
  await gear(TAGS.house);
  await b.eval(`${CARD(TAGS.house)}.shadowRoot.querySelector("[data-edit=priority]").scrollIntoView({ block: "nearest" })`);
  await b.eval(`${CARD(TAGS.house)}.shadowRoot.querySelector("[data-edit=priority]").click()`);
  await sleep(1500);
  await shot("settings-priority", DIALOG(TAGS.house));
}
// Notifications (docs/notifications.md): the phones, then one phone's events grouped Charging, Pool, Spa and Sauna.
if (wanted("settings-notifications") || wanted("settings-notification-phone")) {
  await open("v3-house", TAGS.house, 900, 2400);
  await gear(TAGS.house);
  await b.eval(`${SECTION(TAGS.house, "notifications")}?.scrollIntoView({ block: "nearest" })`);
  await sleep(1500);
  await shot("settings-notifications", SECTION(TAGS.house, "notifications"));
  if (wanted("settings-notification-phone")) {
    await b.eval(`${SECTION(TAGS.house, "notifications")}.querySelector("[data-edit^='phone:']").click()`);
    await sleep(1500);
    await shot("settings-notification-phone", DIALOG(TAGS.house));
  }
}
// The heat card's gear on the Pool, Spa and Sauna tabs opens at that load's section (docs/pool-spa.md#the-settings).
for (const [name, kind] of [["settings-pool", "pool"], ["settings-spa", "spa"], ["settings-sauna", "sauna"]]) {
  if (!wanted(name)) continue;
  await open("v3-heat", TAGS.heat, 900, 2400);
  await tab(TAGS.heat, kind);
  await gear(TAGS.heat);
  await b.eval(`${SECTION(TAGS.heat, kind)}?.scrollIntoView({ block: "nearest" })`);
  await sleep(1000);
  await shot(name, SECTION(TAGS.heat, kind), 8);
}
if (wanted("settings-home-battery")) {
  // Battery control on: the plant is checked step by step (about two minutes here: the demo battery obeys and demo_fast.py
  // shortens the check's waits; the driver polls for the result) and the section shows each step's outcome. The battery has to be discharging for the
  // check to start, so the scene is a cloudy evening.
  await scene({ sun: 0.4, house: 1.1, heat: 1.7, battery: -1.2, soc: 64 });
  const change = { type: "spotnav/battery_control", api_version: 1, site_id: ids.site };
  await api.ws({ ...change, changes: { battery_control_enabled: true }, enable_control_entities: true });
  let state = null;
  for (let i = 0; i < 540 && state !== "on"; i++) {
    await sleep(5000);
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

// The README's hero picture in the dark theme: the three cards side by side on a transparent background,
// docs/images/readme-hero-dark.png. Only on request (`run.sh readme-hero-dark`), and after every light picture, since
// it switches the page to the dark theme.
if (only.has("readme-hero-dark")) {
  await scene();
  await b.send("Page.addScriptToEvaluateOnNewDocument", { source: `try { localStorage.setItem("selectedTheme", '{"dark":true}'); } catch (e) {}` });
  await b.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  const parts = [];
  for (const [page, tag] of [["v3-charger", TAGS.charger], ["v3-house", TAGS.house], ["v3-heat", TAGS.heat]]) {
    await open(page, tag, 700);
    if (tag !== TAGS.charger) await tab(tag, "now");
    // The house card's heat figure can arrive a little after the card has drawn: wait for it rather than show a dash.
    for (let i = 0; i < 20 && tag === TAGS.house && (await b.eval(`/HEAT TODAY\\s*–/i.test(${CARD(tag)}.shadowRoot.textContent.replace(/\\s+/g, " "))`)); i++) await sleep(1500);
    const file = path.join(os.tmpdir(), `docs-hero-dark-${process.pid}-${parts.length}.png`);
    await b.shotElement(file, CARD(tag), 0);
    parts.push(file);
  }
  const imgs = parts.map((f) => `<img src="data:image/png;base64,${fs.readFileSync(f).toString("base64")}">`).join("");
  const html = `<!doctype html><meta charset=utf-8><style>html{background:transparent}body{margin:0;width:1600px;height:660px;display:flex;gap:28px;
    justify-content:center;align-items:flex-start;padding-top:40px;box-sizing:border-box}img{display:block}</style>${imgs}`;
  await b.send("Emulation.setDeviceMetricsOverride", { width: 1600, height: 660, deviceScaleFactor: 1, mobile: false });
  await b.goto("data:text/html;base64," + Buffer.from(html).toString("base64"), 1500);
  // A transparent background, so the cards sit on the page's own colour (GitHub's, the website's) without a grey box.
  await b.send("Emulation.setDefaultBackgroundColorOverride", { color: { r: 0, g: 0, b: 0, a: 0 } });
  await b.shotRect(path.join(OUT, "readme-hero-dark.png"), { x: 0, y: 0, w: 1600, h: 660 });
  await b.send("Emulation.setDefaultBackgroundColorOverride", {});
  done.push("readme-hero-dark");
  for (const f of parts) fs.rmSync(f, { force: true });
}

// The house card in the dark theme at twice the pixels, one picture per tab (Now, Energy flow, Earnings), for the
// banner's overlapping cards: tools/social/card-dark-<tab>.png. Only on request: `run.sh social-dark`. Last, since it
// switches the page to the dark theme.
if (only.has("social-dark")) {
  await scene();
  await b.send("Page.addScriptToEvaluateOnNewDocument", { source: `try { localStorage.setItem("selectedTheme", '{"dark":true}'); } catch (e) {}` });
  await b.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  await b.send("Emulation.setDeviceMetricsOverride", { width: 700, height: 1800, deviceScaleFactor: 2, mobile: false });
  await b.gotoReady(`${ha.BASE}/v3-house/view`, `!!${CARD(TAGS.house)} && ${CARD(TAGS.house)}.shadowRoot.innerHTML.length > 2000`);
  await sleep(2500);
  for (const name of ["now", "flow", "earn"]) {
    try {
      await tab(TAGS.house, name);
      // The month reads better than a morning's day: the earnings have days behind them, the flow has the battery in it.
      if (name !== "now") {
        await b.eval(`[...${CARD(TAGS.house)}.shadowRoot.querySelectorAll("button")].find((e) => (e.innerText || "").trim() === "Month")?.click()`);
        await sleep(2500);
      }
      const clip = await b.shotElement(path.join(REPO, "tools", "social", `card-dark-${name}.png`), CARD(TAGS.house), 0);
      console.log(`   card-dark-${name} ${clip.width}x${clip.height} (twice the pixels)`);
    } catch (err) { failed.push(`social-dark ${name}: ${err.message}`); }
  }
  done.push("social-dark");
}

await b.close();
console.log(`\nwritten: ${done.length}  failed: ${failed.length}`);
for (const f of failed) console.log("FAILED " + f);
process.exit(failed.length ? 1 : 0);
