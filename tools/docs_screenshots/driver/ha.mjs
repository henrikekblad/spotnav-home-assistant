// Home Assistant side of the driver: onboarding, the login flow, REST and WebSocket helpers.
// Everything talks to the throwaway instance on 127.0.0.1 only.
export const BASE = process.env.HA_URL || "http://127.0.0.1:8129";
export const CLIENT_ID = BASE + "/";
export const USER = { name: "Demo", username: "demo", password: "demo-docs-pass-1" };

const json = (r) => r.json();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function waitUp(timeoutS = 240) {
  for (let i = 0; i < timeoutS * 2; i++) {
    try { if ((await fetch(BASE + "/api/onboarding")).ok) return; } catch {}
    await sleep(500);
  }
  throw new Error("Home Assistant did not come up");
}

async function tokenFromCode(code) {
  const body = new URLSearchParams({ grant_type: "authorization_code", code, client_id: CLIENT_ID });
  const r = await fetch(BASE + "/auth/token", { method: "POST", body });
  if (!r.ok) throw new Error("token exchange failed: " + r.status);
  return json(r);
}

/** Onboard a fresh instance (user, core config, analytics, integrations) through its onboarding API. */
export async function onboard() {
  const steps = await json(await fetch(BASE + "/api/onboarding"));
  if (steps.every((s) => s.done)) return;
  let r = await fetch(BASE + "/api/onboarding/users", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: CLIENT_ID, ...USER, language: "en" }),
  });
  if (!r.ok) throw new Error("onboarding user: " + r.status + " " + await r.text());
  const { access_token } = await tokenFromCode((await json(r)).auth_code);
  const auth = { Authorization: "Bearer " + access_token, "Content-Type": "application/json" };
  for (const [step, body] of [["core_config", {}], ["analytics", {}], ["integration", { client_id: CLIENT_ID, redirect_uri: CLIENT_ID + "?auth_callback=1" }]]) {
    r = await fetch(BASE + "/api/onboarding/" + step, { method: "POST", headers: auth, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`onboarding ${step}: ${r.status} ${await r.text()}`);
  }
}

/** The normal login flow of the local instance: user name and password in, tokens out. */
export async function login() {
  const hdr = { "Content-Type": "application/json" };
  let r = await json(await fetch(BASE + "/auth/login_flow", {
    method: "POST", headers: hdr,
    body: JSON.stringify({ client_id: CLIENT_ID, handler: ["homeassistant", null], redirect_uri: CLIENT_ID }),
  }));
  r = await json(await fetch(`${BASE}/auth/login_flow/${r.flow_id}`, {
    method: "POST", headers: hdr,
    body: JSON.stringify({ client_id: CLIENT_ID, username: USER.username, password: USER.password }),
  }));
  if (r.type !== "create_entry") throw new Error("login failed: " + JSON.stringify(r));
  return tokenFromCode(r.result);
}

export class Api {
  constructor(tokens) { this.tokens = tokens; this.h = { Authorization: "Bearer " + tokens.access_token, "Content-Type": "application/json" }; }
  async get(path) { const r = await fetch(BASE + path, { headers: this.h }); return r.headers.get("content-type")?.includes("json") ? r.json() : r.text(); }
  async post(path, body) { const r = await fetch(BASE + path, { method: "POST", headers: this.h, body: JSON.stringify(body ?? {}) }); return r.json(); }
  /** One WebSocket command. */
  async ws(msg) {
    const ws = new WebSocket(BASE.replace("http", "ws") + "/api/websocket");
    await new Promise((res) => (ws.onopen = res));
    return new Promise((resolve, reject) => {
      ws.onmessage = (e) => {
        const m = JSON.parse(e.data);
        if (m.type === "auth_required") ws.send(JSON.stringify({ type: "auth", access_token: this.tokens.access_token }));
        else if (m.type === "auth_ok") ws.send(JSON.stringify({ id: 1, ...msg }));
        else if (m.id === 1) { ws.close(); m.success === false ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result); }
        else if (m.type === "auth_invalid") reject(new Error("auth invalid"));
      };
    });
  }
  /** Drive a config flow over REST. `answers` are the submissions in order; returns the last result. */
  async flow(handler, answers = [], extra = {}) {
    let r = await this.post("/api/config/config_entries/flow", { handler, show_advanced_options: false, ...extra });
    for (const a of answers) {
      if (r.type !== "form" && r.type !== "menu") break;
      r = await this.post(`/api/config/config_entries/flow/${r.flow_id}`, r.type === "menu" ? { next_step_id: a } : a);
    }
    return r;
  }
  async abortFlow(id) { await fetch(`${BASE}/api/config/config_entries/flow/${id}`, { method: "DELETE", headers: this.h }); }
  async entries(domain) { return (await this.get("/api/config/config_entries/entry")).filter((e) => !domain || e.domain === domain); }
}

export async function setLocation(api) {
  await api.ws({
    type: "config/core/update", location_name: "Home", latitude: 55.6, longitude: 13.0, elevation: 20,
    unit_system: "metric", currency: "SEK", country: "SE", language: "en", time_zone: "Europe/Stockholm",
  });
}
/** Confirm the HTTP settings Home Assistant treats as a pending change on first start. */
export async function promoteHttp(api) {
  try { await api.ws({ type: "http/config/promote" }); } catch {}
}
export { sleep };
