// The beta tester box and the hardware test (`beta.ts`), against the backend's own answers
// (`tests/fixtures/beta/`, written by `tests/test_beta.py`).

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  betaBox, blockedText, codeErrorText, codeWanted, decodeBetaAnswer, stepText, testBox, testHidden, testSummary, type BetaRequest,
} from "../src/beta";
import { translate } from "../src/i18n";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures", "beta");

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, name), "utf-8")) as Record<string, unknown>;
}

/** A battery control answer (`tests/fixtures/battery_control/`), for the hardware test box's tester parts. */
function battery(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(FIXTURES, "..", "battery_control", name), "utf-8")) as Record<string, unknown>;
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
    expect(names.sort()).toEqual(["ended.json", "off_idle.json", "on_done_failed.json", "on_done_partial.json", "on_running.json"]);
    for (const name of names) {
      expect(decodeBetaAnswer(fixture(name)), name).not.toBeNull();
    }
    const failed = decodeBetaAnswer(fixture("on_done_failed.json"))!;
    expect(failed.test?.result).toBe("failed");
    expect(failed.test?.sentRef).toBe("SN-7KQ4");
    expect(decodeBetaAnswer(fixture("ended.json"))!.beta).toMatchObject({ on: false, ended: true });
    const partial = decodeBetaAnswer(fixture("on_done_partial.json"))!;
    expect(partial.test).toMatchObject({ result: "partial", blocked: null });
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
    expect(stepText("en", { part: "battery", step: "deadman", status: "passed", reason: null })).toBe(
      "Battery: stops by itself when Home Assistant goes quiet: OK",
    );
    const idle = decodeBetaAnswer(fixture("off_idle.json"))!.test!;
    expect(blockedText("en", idle)).toBeNull();
    expect(blockedText("en", { ...idle, blocked: "nothing_to_test" })).toBe(translate("en", "test.blocked.nothing"));
    expect(blockedText("en", { ...idle, blocked: "waiting" })).toBeNull();
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

/** A box against canned answers: `spotnav/beta`'s in `answers`, a battery tester's (`action: "tester"`, kept apart in
 * `testerCalls`) in `testers` (none: the read fails, as from a site without the block). */
function box(
  answers: unknown[], build: typeof betaBox = betaBox, testers: unknown[] = [],
): { root: HTMLElement; calls: BetaRequest[]; testerCalls: BetaRequest[] } {
  const calls: BetaRequest[] = [];
  const testerCalls: BetaRequest[] = [];
  const root = build({
    doc: document,
    language: "sv",
    idPrefix: "t",
    call: async (request) => {
      if (request.action === "tester") {
        testerCalls.push(request);
        if (testers.length === 0) {
          throw new Error("no battery");
        }
        return testers.length > 1 ? testers.shift() : testers[0];
      }
      calls.push(request);
      return answers.length > 1 ? answers.shift() : answers[0];
    },
  });
  document.body.append(root);
  return { root, calls, testerCalls };
}

const q = (root: HTMLElement, selector: string): HTMLElement => root.querySelector(selector) as HTMLElement;
const test = (answers: unknown[], testers: unknown[] = []): ReturnType<typeof box> => box(answers, testBox, testers);

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

});

describe("testBox", () => {
  it("starts the test, shows the steps live and reads again while it runs", async () => {
    vi.useFakeTimers();
    const { root, calls } = test([fixture("off_idle.json"), fixture("on_running.json"), fixture("on_done_failed.json")]);
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

  it("says when a part was skipped or nothing ran, and leaves Run open", async () => {
    const partial = fixture("on_done_partial.json");
    const skipped = { ...partial, test: { ...(partial.test as Record<string, unknown>), result: "skipped" } };
    const { root } = test([partial]);
    await settle();
    expect(q(root, "[data-beta=result]").textContent).toContain(translate("sv", "test.result.partial"));
    expect((q(root, "[data-beta=runTest]") as HTMLButtonElement).disabled).toBe(false);
    expect(decodeBetaAnswer(skipped)!.test?.result).toBe("skipped");
    expect(decodeBetaAnswer({ ...partial, test: { ...(partial.test as Record<string, unknown>), result: "half" } })).toBeNull();
  });

  it("offers Send for a finished test when beta is off", async () => {
    const done = fixture("on_done_failed.json");
    const off = {
      ...done,
      beta: { on: false, started_at: null, ends_at: null, ended: false, email: null },
      test: { ...(done.test as Record<string, unknown>), sent_ref: null },
    };
    const sent = { ...off, test: { ...(off.test as Record<string, unknown>), sent_ref: "SN-ABCD" } };
    const { root, calls } = test([off, sent]);
    await settle();
    const send = q(root, "[data-beta=sendTest]");
    expect(send.hidden).toBe(false);
    send.click();
    await settle();
    expect(calls[1]).toEqual({ action: "send_test" });
    expect(send.hidden).toBe(true);
    expect(q(root, "[data-beta=result]").textContent).toContain("SN-ABCD");
  });

  it("hides the test where there is nothing to test and no test ran, and keeps an earlier test's result", async () => {
    const idle = fixture("off_idle.json") as { test: Record<string, unknown> };
    const nothing = { ...idle, test: { ...idle.test, state: "idle", blocked: "nothing_to_test", next_at: null } };
    const { root } = test([nothing]);
    await settle();
    expect(root.hidden).toBe(true);
    document.body.innerHTML = "";
    const done = fixture("on_done_failed.json") as { test: Record<string, unknown> };
    const after = test([{ ...done, test: { ...done.test, blocked: "nothing_to_test", next_at: null } }]);
    await settle();
    expect(after.root.hidden).toBe(false);
  });

  it("says when a pressed test will run by itself and asks nothing more, then reads again a minute later", async () => {
    vi.useFakeTimers();
    const idle = fixture("off_idle.json") as { test: Record<string, unknown> };
    const at = { ...idle, test: { ...idle.test, state: "waiting", blocked: "waiting", next_at: "2026-11-19T17:00:00+01:00",
      result: null, steps: [] } };
    const { root, calls } = test([at, fixture("on_running.json")]);
    await settle();
    const line = q(root, "[data-beta=result]").textContent ?? "";
    expect(line).toContain("Körs av sig själv, troligen från ungefär");
    expect(line).toContain("Inget behövs från dig");
    expect(q(root, "[data-beta=runTest]").hidden).toBe(true);
    expect(q(root, "[data-beta=stopTest]").hidden).toBe(false);
    expect(q(root, "[data-beta=blocked]").hidden).toBe(true);
    expect(testSummary("en", decodeBetaAnswer(at))).toBe("Waiting for a suitable moment");
    await vi.advanceTimersByTimeAsync(3000);
    expect(calls.length).toBe(1);
    await vi.advanceTimersByTimeAsync(60000);
    await settle();
    expect(calls[1]).toEqual({ action: "get" });
  });

  it("takes the control code for a battery tester and says where the tester stands", async () => {
    const status = (stage: string, error: string | null = null) => ({
      api_version: 1, ok: error === null, error,
      status: { ...(battery("checking.json")["status"] as object), tester: {
        stage, brand: "foxess", sent_at: "2026-11-25T10:00:00+01:00", result_sent_at: stage === "tested" ? "2026-11-25T12:00:00+01:00" : null,
        test_passed: stage !== "asked", code_entered_at: stage === "control_code" ? "2026-11-26T09:00:00+01:00" : null,
      } },
    });
    const { root, testerCalls } = test(
      [fixture("on_done_failed.json")], [status("tested"), status("tested", "spotnav_code_other_install"), status("control_code")],
    );
    await settle();
    expect(testerCalls[0]).toEqual({ action: "tester", tester: null });
    expect(q(root, "[data-beta=tester]").textContent).toBe(translate("sv", "test.tester.sent"));
    expect(q(root, "[data-beta=codeField]").hidden).toBe(false);
    const code = q(root, "[data-beta-field=code]") as HTMLInputElement;
    // Nothing pasted: said at once, nothing sent.
    q(root, "[data-beta=enterCode]").click();
    await settle();
    expect(testerCalls).toHaveLength(1);
    expect(q(root, "[data-beta=codeError]").textContent).toBe(translate("sv", "test.code.error.invalid"));
    code.value = "  aebcd-efgh ";
    q(root, "[data-beta=enterCode]").click();
    await settle();
    expect(testerCalls[1]).toEqual({ action: "tester", tester: { enter_code: { code: "aebcd-efgh" } } });
    expect(q(root, "[data-beta=codeError]").textContent).toBe(translate("sv", "test.code.error.otherInstall"));
    q(root, "[data-beta=enterCode]").click();
    await settle();
    expect(q(root, "[data-beta=codeError]").hidden).toBe(true);
    expect(q(root, "[data-beta=codeField]").hidden).toBe(true);
    expect(q(root, "[data-beta=tester]").textContent).toBe(translate("sv", "test.tester.code"));
  });

  it("shows no tester parts where the battery takes no code, and says the test waits for the sign-up", async () => {
    const plain = { ...(battery("checking.json")) };
    const { root } = test([fixture("off_idle.json")], [plain]);
    await settle();
    expect(q(root, "[data-beta=codeField]").hidden).toBe(true);
    expect(q(root, "[data-beta=tester]").hidden).toBe(true);
    const idle = decodeBetaAnswer(fixture("off_idle.json"))!.test!;
    expect(blockedText("sv", { ...idle, blocked: "tester_signup" })).toBe(translate("sv", "test.blocked.testerSignup"));
    expect(codeWanted({ stage: "none", brand: "foxess", sent_at: null, result_sent_at: null, test_passed: false, code_entered_at: null }))
      .toBe(false);
    expect(codeWanted({ stage: "none", brand: "foxess", sent_at: null, result_sent_at: null, test_passed: true, code_entered_at: null }))
      .toBe(true);
    expect(codeErrorText("en", null)).toBe(translate("en", "test.code.error.failed"));
  });

  it("says a step that could not be judged runs again by itself", async () => {
    const partial = fixture("on_done_partial.json") as { test: Record<string, unknown> };
    const retry = { ...partial, test: { ...partial.test, state: "waiting", blocked: "waiting", next_at: null } };
    const { root } = test([retry]);
    await settle();
    const line = q(root, "[data-beta=result]").textContent ?? "";
    expect(line).toContain(translate("sv", "test.waiting.retry"));
    expect(line).toContain(translate("sv", "test.waiting.soon"));
  });

  it("hides the test where the answer has no site", async () => {
    const { root } = test([{ ...fixture("off_idle.json"), test: null }]);
    await settle();
    expect(root.hidden).toBe(true);
  });
});

describe("the layout", () => {
  it("puts the e-mail address and its Save on one line", async () => {
    const { root } = box([fixture("off_idle.json")]);
    await settle();
    const save = q(root, "[data-beta=saveEmail]");
    expect(save.parentElement).toBe(q(root, "[data-beta-field=email]").parentElement);
    expect(save.parentElement?.classList.contains("spotnav-settings-input-row")).toBe(true);
  });

  it("keeps the test out of the beta box and puts Send under the result, named for what it does", async () => {
    const done = fixture("on_done_failed.json");
    const off = {
      ...done,
      beta: { on: false, started_at: null, ends_at: null, ended: false, email: null },
      test: { ...(done.test as Record<string, unknown>), sent_ref: null },
    };
    expect(box([off]).root.querySelector("[data-beta=runTest]")).toBeNull();
    const { root } = test([off]);
    await settle();
    const send = q(root, "[data-beta=sendTest]");
    expect(send.textContent).toBe("Skicka resultatet till utvecklaren");
    const result = q(root, "[data-beta=result]");
    expect(result.compareDocumentPosition(send) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(q(root, "[data-beta=runTest]").parentElement).not.toBe(send.parentElement);
  });
});

describe("the hardware test row", () => {
  it("shows the last result with its day, running, or not run, and hides where nothing can be tested", () => {
    const done = decodeBetaAnswer(fixture("on_done_failed.json"))!;
    expect(testSummary("sv", done)).toMatch(/^Problem · /);
    expect(testSummary("sv", decodeBetaAnswer(fixture("on_running.json")))).toBe("Pågår");
    const idle = decodeBetaAnswer(fixture("off_idle.json"))!;
    expect(testSummary("sv", { ...idle, test: { ...idle.test!, state: "idle", result: null } })).toBe("Inte kört");
    expect(testSummary("sv", null)).toBe("…");
    expect(testHidden(null)).toBe(true);
    expect(testHidden({ ...idle, test: null })).toBe(true);
    expect(testHidden({ ...idle, test: { ...idle.test!, state: "idle", blocked: "nothing_to_test" } })).toBe(true);
    expect(testHidden({ ...done, test: { ...done.test!, blocked: "nothing_to_test" } })).toBe(false);
    // A part that is there but busy is no "turn control on first".
    expect(blockedText("sv", { ...idle.test!, blocked: "not_ready" })).toBe(translate("sv", "test.blocked.later"));
  });
});
