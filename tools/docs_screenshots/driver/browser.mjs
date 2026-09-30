// A small Chrome DevTools Protocol client: headless chromium, shadow-DOM-piercing queries, real mouse clicks,
// element screenshots. No dependencies beyond Node and a chromium binary.
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { BASE, CLIENT_ID } from "./ha.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Helpers evaluated inside the page. `deepAll` walks every shadow root.
const PAGE_HELPERS = `
window.__docs = {
  deepAll(selector, root = document) {
    const out = [];
    const walk = (node) => {
      if (node.querySelectorAll) out.push(...node.querySelectorAll(selector));
      const all = node.querySelectorAll ? node.querySelectorAll("*") : [];
      for (const el of all) if (el.shadowRoot) walk(el.shadowRoot);
      if (node.shadowRoot) walk(node.shadowRoot);
    };
    walk(root);
    return out;
  },
  visible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  },
  text(el) { return (el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim(); },
};
`;

export class Browser {
  static async launch({ width = 1280, height = 1800 } = {}) {
    const b = new Browser();
    b.dir = fs.mkdtempSync(path.join(os.tmpdir(), "docs-shots-"));
        b.proc = spawn(process.env.CHROMIUM || "chromium", [
      "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--force-color-profile=srgb",
      "--lang=en-US", "--font-render-hinting=none",
      "--remote-debugging-port=0", `--user-data-dir=${b.dir}`, `--window-size=${width},${height}`, "about:blank",
    ], { stdio: process.env.DEBUG_CHROMIUM ? "inherit" : "ignore" });
    let targets, port;
    for (let i = 0; i < 150; i++) {
      try {
        port = fs.readFileSync(path.join(b.dir, "DevToolsActivePort"), "utf8").split("\n")[0]; targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (targets.find((t) => t.type === "page")) break; } catch {}
      await sleep(200);
    }
    b.ws = new WebSocket(targets.find((t) => t.type === "page").webSocketDebuggerUrl);
    await new Promise((r) => (b.ws.onopen = r));
    b.id = 0; b.pending = new Map(); b.problems = [];
    b.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && b.pending.has(m.id)) { b.pending.get(m.id)(m); b.pending.delete(m.id); }
      else if (m.method === "Runtime.exceptionThrown") b.problems.push("exception: " + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
    };
    await b.send("Runtime.enable"); await b.send("Page.enable");
    await b.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
    await b.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "light" }] });
    await b.send("Emulation.setLocaleOverride", { locale: "en-US" }).catch(() => {});
    await b.send("Emulation.setTimezoneOverride", { timezoneId: "Europe/Stockholm" }).catch(() => {});
    await b.send("Page.addScriptToEvaluateOnNewDocument", { source: PAGE_HELPERS });
    b.width = width; b.height = height;
    return b;
  }

  send(method, params = {}) {
    return new Promise((res, rej) => {
      const i = ++this.id;
      this.pending.set(i, (m) => (m.error ? rej(new Error(method + ": " + m.error.message)) : res(m.result)));
      this.ws.send(JSON.stringify({ id: i, method, params }));
    });
  }

  /** Run an expression in the page and return its value. */
  async eval(expression) {
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error("page: " + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    return r.result.value;
  }

  /** Put the tokens where the frontend looks for them, so the page opens signed in (English, light). */
  async signIn(tokens) {
    const stored = {
      access_token: tokens.access_token, token_type: "Bearer", expires_in: tokens.expires_in,
      hassUrl: BASE, clientId: CLIENT_ID, expires: Date.now() + tokens.expires_in * 1000,
      refresh_token: tokens.refresh_token,
    };
    await this.send("Page.addScriptToEvaluateOnNewDocument", { source: `
      try {
        localStorage.setItem("hassTokens", ${JSON.stringify(JSON.stringify(stored))});
        localStorage.setItem("selectedLanguage", '"en"');
        localStorage.setItem("selectedTheme", '{"dark":false}');
        localStorage.setItem("dockedSidebar", '"docked"');
      } catch (e) {}` });
  }

  async goto(url, settleMs = 1500) {
    await this.send("Page.navigate", { url });
    await sleep(settleMs);
  }

  /** Open a page and wait for `readyExpression`; a frontend that hangs on its loading screen is reloaded. */
  async gotoReady(url, readyExpression, { tries = 4, timeout = 15000 } = {}) {
    for (let i = 0; i < tries; i++) {
      await this.send("Page.navigate", { url });
      try { await this.waitFor(readyExpression, { timeout }); await sleep(800); return; } catch {}
    }
    throw new Error("page never became ready: " + url);
  }

  /** Poll until `fn` (an expression source) is truthy. */
  async waitFor(expression, { timeout = 20000, what = expression } = {}) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      try { const v = await this.eval(expression); if (v) return v; } catch {}
      await sleep(250);
    }
    throw new Error("timed out waiting for " + what);
  }

  /** The first visible element (through shadow roots) matching `selector` whose text matches `re` (source string), as JS. */
  finder(selector, textRe, nth = 0) {
    const re = textRe ? `new RegExp(${JSON.stringify(textRe)}, "i")` : "null";
    return `(() => { const D = window.__docs; const re = ${re};
      return D.deepAll(${JSON.stringify(selector)}).filter((e) => D.visible(e) && (!re || re.test(D.text(e)) || re.test(e.label || e.getAttribute("aria-label") || "")))[${nth}] || null; })()`;
  }

  

  async rectOf(expr) {
    return this.eval(`(() => { const e = ${expr}; if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; })()`);
  }

  /** A real mouse click at the centre of the element. */
  async click(selector, textRe, nth = 0) {
    const expr = this.finder(selector, textRe, nth);
    await this.waitFor(`!!${expr}`, { what: `${selector} ${textRe ?? ""}` });
    await this.eval(`${expr}.scrollIntoView({block:"center"})`);
    await sleep(100);
    const r = await this.rectOf(expr);
    const x = r.x + r.w / 2, y = r.y + r.h / 2;
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
    await sleep(400);
  }

  async type(text) {
    for (const ch of text) await this.send("Input.dispatchKeyEvent", { type: "char", text: ch });
    await sleep(300);
  }

  async press(key, code = key) {
    await this.send("Input.dispatchKeyEvent", { type: "keyDown", key, code });
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", key, code });
  }

  /** Screenshot of a rectangle (page coordinates of the current viewport) with a little padding. */
  async shotRect(file, r, pad = 0) {
    const clip = { x: Math.max(0, Math.floor(r.x - pad)), y: Math.max(0, Math.floor(r.y - pad)), width: Math.ceil(r.w + 2 * pad), height: Math.ceil(r.h + 2 * pad), scale: 1 };
    const shot = await this.send("Page.captureScreenshot", { format: "png", clip });
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, Buffer.from(shot.data, "base64"));
    return clip;
  }

  async shotElement(file, expr, pad = 0) {
    const r = await this.rectOf(expr);
    if (!r) throw new Error("no element for screenshot " + file);
    return this.shotRect(file, r, pad);
  }

  async shotViewport(file) { return this.shotRect(file, { x: 0, y: 0, w: this.width, h: this.height }); }

  async close() {
    try { this.ws.close(); } catch {}
    const exited = new Promise((r) => this.proc.once("exit", r));
    this.proc.kill();
    await Promise.race([exited, sleep(3000)]);
    try { fs.rmSync(this.dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch {}
  }
}
export { sleep };
