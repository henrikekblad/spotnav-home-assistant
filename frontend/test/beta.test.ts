// The beta tester box and the hardware test (`beta.ts`), against the backend's own answers
// (`tests/fixtures/beta/`, written by `tests/test_beta.py`).

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { betaBox, blockedText, decodeBetaAnswer, stepText, type BetaRequest } from "../src/beta";
import { translate } from "../src/i18n";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "beta");

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf-8")) as Record<string, unknown>;
}

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i += 1) {
    await Promise.resolve();
  }
}

afterEach(() => {
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe("decodeBetaAnswer", () => {
  it("reads every fixture the backend wrote", () => {
    const names = readdirSync(FIXTURES).filter((name) => name.endsWith(".json"));
    expect(names.sort()).toEqual(["ended.json", "off_idle.json", "on_done_failed.json", "on_running.json"]);
    for (const name of names) {
      expect(decodeBetaAnswer(fixture(name)), name).not.toBeNull();
    }
    const failed = decodeBetaAnswer(fixture("on_done_failed.json"))!;
    expect(failed.test?.result).toBe("failed");
    expect(failed.test?.sentRef).toBe("SN-7KQ4");
    expect(decodeBetaAnswer(fixture("ended.json"))!.beta).toMatchObject({ on: false, ended: true });
  });

  it("refuses an unknown key, another version or a wrong type", () => {
    const answer = fixture("on_running.json");
    expect(decodeBetaAnswer({ ...answer, extra: 1 })).toBeNull();
    expect(decodeBetaAnswer({ ...answer, api_version: 2 })).toBeNull();
    const beta = answer.beta as Record<string, unknown>;
    expect(decodeBetaAnswer({ ...answer, beta: { ...beta, on: "yes" } })).toBeNull();
    expect(decodeBetaAnswer({ ...answer, beta: { ...beta, phone: null } })).toBeNull();
    const test = answer.test as Record<string, unknown>;
    expect(decodeBetaAnswer({ ...answer, test: { ...test, state: "paused" } })).toBeNull();
    expect(decodeBetaAnswer({ ...answer, test: { ...test, steps: [{ part: "battery" }] } })).toBeNull();
    expect(decodeBetaAnswer({ ...answer, retry_after_s: -1 })).toBeNull();
  });
});

describe("the words", () => {
  it("names each step and status, and why a test cannot start", () => {
    expect(stepText("sv", { part: "battery", step: "charge", status: "failed", reason: null })).toBe(
      "Batteri: liten laddning från nätet: misslyckades",
    );
    expect(stepText("en", { part: "heat", step: "engage", status: "passed", reason: null })).toBe("Heating: engage: OK");
    const idle = decodeBetaAnswer(fixture("off_idle.json"))!.test!;
    expect(blockedText("en", idle)).toBeNull();
    expect(blockedText("en", { ...idle, blocked: "nothing_to_test" })).toBe(translate("en", "test.blocked.nothing"));
    expect(blockedText("sv", { ...idle, blocked: "once_a_day", nextAt: "2026-11-20T20:15:00+01:00" })).toMatch(/^Nästa test kan köras /);
  });

  it("uses the agreed name of the box in each language", () => {
    expect(translate("sv", "beta.label")).toBe("Betatestare");
    expect(translate("en", "beta.label")).toBe("Beta tester");
    expect(translate("nb", "beta.label")).toBe("Betatester");
    expect(translate("da", "beta.label")).toBe("Betatester");
    expect(translate("fi", "beta.label")).toBe("Beetatestaaja");
    expect(translate("sv", "beta.ended")).toBe("Betaperioden är slut – inget skickas längre");
  });
});

function box(answers: unknown[]): { root: HTMLElement; calls: BetaRequest[] } {
  const calls: BetaRequest[] = [];
  const root = betaBox({
    doc: document,
    language: "sv",
    idPrefix: "t",
    call: async (request) => {
      calls.push(request);
      return answers.length > 1 ? answers.shift() : answers[0];
    },
  });
  document.body.append(root);
  return { root, calls };
}

const q = (root: HTMLElement, selector: string): HTMLElement => root.querySelector(selector) as HTMLElement;

describe("betaBox", () => {
  it("reads the state, ticks the box and shows the period", async () => {
    const on = fixture("on_done_failed.json");
    const { root, calls } = box([fixture("off_idle.json"), on]);
    await settle();
    const check = q(root, "[data-beta-field=on]") as HTMLInputElement;
    expect(check.checked).toBe(false);
    expect(q(root, "[data-beta=status]").hidden).toBe(true);
    check.checked = true;
    check.dispatchEvent(new Event("change"));
    await settle();
    expect(calls[1]).toEqual({ action: "set", changes: { on: true } });
    expect(check.checked).toBe(true);
    expect(q(root, "[data-beta=status]").textContent).toMatch(/^Betaperiod till /);
    expect((q(root, "[data-beta-field=email]") as HTMLInputElement).value).toBe("me@example.com");
  });

  it("says once that the period ended, and nothing more", async () => {
    const { root } = box([fixture("ended.json")]);
    await settle();
    expect(q(root, "[data-beta=status]").textContent).toBe("Betaperioden är slut – inget skickas längre");
    expect((q(root, "[data-beta-field=on]") as HTMLInputElement).checked).toBe(false);
    expect(root.querySelector("[role=alert]")?.textContent).toBe("");
  });

  it("opens the read-more text with what is sent and kept", async () => {
    const { root } = box([fixture("off_idle.json")]);
    await settle();
    const more = q(root, "[data-beta-more]");
    expect(more.hidden).toBe(true);
    q(root, "[data-beta=more]").click();
    expect(more.hidden).toBe(false);
    expect(more.textContent).toContain("14 dagar");
    expect(more.textContent).toContain("90 dagar");
    expect(more.textContent).toContain("några kronor");
  });

  it("refuses an address that is no address before anything is sent", async () => {
    const { root, calls } = box([fixture("off_idle.json")]);
    await settle();
    const email = q(root, "[data-beta-field=email]") as HTMLInputElement;
    email.value = "inte en adress";
    email.dispatchEvent(new Event("input"));
    q(root, "[data-beta=saveEmail]").click();
    await settle();
    expect(calls).toHaveLength(1);
    expect(q(root, "[data-beta=error]").textContent).toBe(translate("sv", "sendDebug.error.email"));
  });

  it("starts the test, shows the steps live and reads again while it runs", async () => {
    vi.useFakeTimers();
    const { root, calls } = box([fixture("off_idle.json"), fixture("on_running.json"), fixture("on_done_failed.json")]);
    await settle();
    q(root, "[data-beta=runTest]").click();
    await settle();
    expect(calls[1]).toEqual({ action: "start_test" });
    const steps = [...root.querySelectorAll("[data-beta=steps] li")];
    expect(steps.map((step) => (step as HTMLElement).dataset["status"])).toEqual([
      "running", "pending", "pending", "pending", "passed", "running",
    ]);
    expect(q(root, "[data-beta=runTest]").hidden).toBe(true);
    expect(q(root, "[data-beta=stopTest]").hidden).toBe(false);
    await vi.advanceTimersByTimeAsync(3000);
    await settle();
    expect(calls[2]).toEqual({ action: "get" });
    expect(q(root, "[data-beta=result]").textContent).toContain(translate("sv", "test.result.failed"));
    // Beta was on: the test went by itself; no Send button.
    expect(q(root, "[data-beta=sendTest]").hidden).toBe(true);
    expect(q(root, "[data-beta=runTest]").hidden).toBe(false);
    expect((q(root, "[data-beta=runTest]") as HTMLButtonElement).disabled).toBe(true);
    expect(q(root, "[data-beta=blocked]").textContent).toMatch(/^Nästa test kan köras /);
  });

  it("offers Send for a finished test when beta is off", async () => {
    const done = fixture("on_done_failed.json");
    const off = {
      ...done,
      beta: { on: false, started_at: null, ends_at: null, ended: false, email: null },
      test: { ...(done.test as Record<string, unknown>), sent_ref: null },
    };
    const sent = { ...off, test: { ...(off.test as Record<string, unknown>), sent_ref: "SN-ABCD" } };
    const { root, calls } = box([off, sent]);
    await settle();
    const send = q(root, "[data-beta=sendTest]");
    expect(send.hidden).toBe(false);
    send.click();
    await settle();
    expect(calls[1]).toEqual({ action: "send_test" });
    expect(send.hidden).toBe(true);
    expect(q(root, "[data-beta=result]").textContent).toContain("SN-ABCD");
  });

  it("hides the test where the answer has no site", async () => {
    const { root } = box([{ ...fixture("off_idle.json"), test: null }]);
    await settle();
    expect(q(root, "[data-beta=test]").hidden).toBe(true);
  });
});
