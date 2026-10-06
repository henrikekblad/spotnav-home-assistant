// The compact action and strategy row: the backend's two axes, one request per click, no optimism.
//
// Every payload here is one of the backend-owned fixtures in `tests/fixtures/dashboard/`, so what
// is exercised is the real contract: the row does not decide anything, it renders `immediate_action`
// and `automatic_action` as two separate buttons, offers exactly `pause_choices` beside the automatic
// pause, and reports clicks which the card turns into one request each. Neither button is derived
// from the other axis, and neither can be clicked into the other's request.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import { LANGUAGES, translate, type TranslationKey } from "../src/i18n";
import { ACTION_API_VERSION } from "../src/types";
import { apiFailure, FakeHass, mountCard } from "./helpers";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const CONFIG = { type: "custom:spotnav-card", charger: "entry_a" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const text = (element: Element): string => shadow(element).textContent ?? "";
/** The immediate control: Start now / Stop. */
const actionButton = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>(".spotnav-action-button");
/** The automatic control: Pause automatic charging / Resume automatic charging. */
const plannerButton = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>(".spotnav-planner-button");
const strategyButton = (element: Element): HTMLButtonElement | null =>
  shadow(element).querySelector<HTMLButtonElement>(".spotnav-strategy-button");
const choiceButtons = (element: Element): HTMLButtonElement[] =>
  Array.from(shadow(element).querySelectorAll<HTMLButtonElement>(".spotnav-choice-button"));
const strategyRows = (element: Element): HTMLButtonElement[] =>
  Array.from(shadow(element).querySelectorAll<HTMLButtonElement>(".spotnav-strategy-row button"));
const actionError = (element: Element): HTMLElement | null =>
  shadow(element).querySelector<HTMLElement>(".spotnav-action-error:not([hidden])");
// The dialog nodes exist from the first render and are hidden while closed, so an open dialog is one
// that is not inside a hidden overlay -- the same fact the view reports through `dialogOpen`.
/** The exact confirmed pixels of one graph area, for identity comparisons that ignore nothing. */
const graphHtml = (element: Element): string =>
  shadow(element).querySelector(".spotnav-chart-viewport")?.innerHTML ?? "";

/** The decoded dashboard the card is holding, so a retained snapshot can be proved to be *that* one. */
const dashboardOf = (element: Element): unknown =>
  (element as unknown as { cardState: { dashboard?: unknown } }).cardState.dashboard;

const openDialogs = (element: Element): HTMLElement[] =>
  Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).filter(
    (dialog) => dialog.closest("[hidden]") === null,
  );

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(DASHBOARD_DIR, `${name}.json`), "utf8")) as Record<string, unknown>;
}

function withControl(name: string, control: Record<string, unknown>): Record<string, unknown> {
  const payload = fixture(name);
  payload.control = { ...(payload.control as Record<string, unknown>), ...control };
  return payload;
}

const started = (action: string, choice: string | null = null) => ({
  api_version: ACTION_API_VERSION,
  ok: true,
  error: null,
  action,
  choice,
});

const refused = (code: string) => ({
  api_version: ACTION_API_VERSION,
  ok: false,
  error: code,
  action: null,
  choice: null,
});

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

/** One mounted card, with its first dashboard answer resolved. */
async function mounted(
  payload: Record<string, unknown>,
  language = "en",
): Promise<{ hass: FakeHass; element: ReturnType<typeof mountCard> }> {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  element.hass = hass.snapshot("snapshot", language);
  await settle();
  hass.resolveNext(payload);
  await settle();
  return { hass, element };
}

const actionsOf = (hass: FakeHass): Record<string, unknown>[] =>
  hass.messages.filter((message) => message.type === "spotnav/manual_action");
const readsOf = (hass: FakeHass): Record<string, unknown>[] =>
  hass.messages.filter((message) => message.type === "spotnav/get_dashboard");

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the two control buttons", () => {
  it("renders each axis the backend named, as its own button, and nothing for an axis with no action", async () => {
    // Both axes per fixture: an idle Auto charger is Start now *and* Pause automatic charging, which
    // is the state one folded button could not express.
    const expected: Record<
      string,
      {
        immediate: { action: string; label: string } | null;
        automatic: { action: string; label: string } | null;
      }
    > = {
      start_idle: {
        immediate: { action: "start", label: "action.start" },
        // Visual fix d: the button's own *visible* word is the short one; its full accessible name is
        // checked separately, below.
        automatic: { action: "pause", label: "action.pauseAutomaticShort" },
      },
      stop_charging: {
        immediate: { action: "stop", label: "action.stop" },
        automatic: { action: "pause", label: "action.pauseAutomaticShort" },
      },
      resume_active: {
        immediate: { action: "start", label: "action.start" },
        automatic: { action: "resume", label: "action.resumeShort" },
      },
      pause_clear_failed: {
        immediate: { action: "start", label: "action.start" },
        automatic: null,
      },
      action_pending: { immediate: null, automatic: null },
      no_settings: { immediate: null, automatic: null },
    };
    for (const [name, want] of Object.entries(expected)) {
      document.body.innerHTML = "";
      const { element } = await mounted(fixture(name));
      for (const [axis, button] of [
        [want.immediate, actionButton(element)],
        [want.automatic, plannerButton(element)],
      ] as const) {
        if (axis === null && name === "action_pending" && button === actionButton(element)) {
          // A start waiting for the charger: the Charging cell stays in place, greyed and busy.
          expect(button, `${name}: the cell stays`).not.toBeNull();
          expect(button?.disabled, name).toBe(true);
          expect(button?.getAttribute("aria-busy"), name).toBe("true");
          continue;
        }
        if (axis === null) {
          expect(button, `${name}: no button for that axis`).toBeNull();
          continue;
        }
        expect(button?.dataset["action"], name).toBe(axis.action);
        expect(button?.getAttribute("aria-label"), name).toContain(translate("en", axis.label as "action.start"));
        expect(button?.disabled, name).toBe(false);
      }
      // The automatic button's full sentence still reaches anyone who cannot see its short word.
      const automaticButton = plannerButton(element);
      if (automaticButton !== null) {
        const resume = automaticButton.dataset["action"] === "resume";
        const fullKey = resume ? "action.resume" : "action.pauseAutomatic";
        const stateKey = resume ? "bar.state.schedulePaused" : "bar.state.scheduleActive";
        expect(automaticButton.getAttribute("aria-label"), name).toBe(
          `${translate("en", "bar.schedule")}: ${translate("en", stateKey)}. ${translate("en", fullKey)}`,
        );
      }
    }
  });

  it("offers both buttons on an idle Auto charger, under names that cannot be confused", async () => {
    // The defect this split was raised for, at the DOM boundary: an idle charger with a plan for later
    // tonight shows Start now *and* the Pause that suspends that plan.
    const { element } = await mounted(fixture("start_idle"));
    const immediate = actionButton(element);
    const automatic = plannerButton(element);
    expect(immediate?.dataset["action"]).toBe("start");
    expect(automatic?.dataset["action"]).toBe("pause");
    const valueOf = (node: Element | null | undefined) => node?.querySelector(".spotnav-settings-value")?.textContent;
    expect(valueOf(immediate)).toBe(translate("en", "bar.start"));
    expect(valueOf(automatic)).toBe(translate("en", "action.pauseAutomaticShort"));
    // Axis + action in every accessible name: the captions alone would not be part of a button's name.
    expect(immediate?.getAttribute("aria-label")).toBe(
      `${translate("en", "bar.charging")}: ${translate("en", "bar.state.notCharging")}. ${translate("en", "action.start")}`,
    );
    expect(automatic?.getAttribute("aria-label")).toBe(
      `${translate("en", "bar.schedule")}: ${translate("en", "bar.state.scheduleActive")}. ${translate("en", "action.pauseAutomatic")}`,
    );
    expect(immediate?.getAttribute("aria-label")).not.toBe(automatic?.getAttribute("aria-label"));
  });

  it("offers the Start help, and never a duration promise", async () => {
    const { element } = await mounted(fixture("start_idle"));
    expect(text(element)).toContain(translate("en", "action.startHelp"));
    expect(text(element)).not.toMatch(/for \d+ (minutes|hours)/);
  });

  it("sends one request for one click, then exactly one forced dashboard read", async () => {
    const { hass, element } = await mounted(fixture("start_idle"));
    const readsBefore = readsOf(hass).length;

    actionButton(element)?.click();

    expect(actionsOf(hass)).toEqual([
      {
        type: "spotnav/manual_action",
        api_version: ACTION_API_VERSION,
        charger_id: "entry_a",
        action: "start",
      },
    ]);
    // Nothing optimistic: the button is disabled while the request is in flight, and the state on
    // screen is still the confirmed one (the backend has not said anything yet).
    expect(actionButton(element)?.disabled).toBe(true);
    expect(actionButton(element)?.dataset["action"]).toBe("start");
    // The cell stays where it is, marked busy, while the answer is awaited; the other cell is only greyed.
    expect(actionButton(element)?.getAttribute("aria-busy")).toBe("true");
    expect(plannerButton(element)?.disabled).toBe(true);
    expect(plannerButton(element)?.getAttribute("aria-busy")).toBeNull();
    expect(text(element)).not.toContain(translate("en", "control.pausedIndefinitely"));

    hass.resolveNext(started("start"));
    await settle();

    expect(readsOf(hass).length, "one fresh read, and only that").toBe(readsBefore + 1);
    hass.resolveNext(fixture("stop_charging"));
    await settle();

    // Only the confirmed answer changed the screen: the backend now says Stop.
    expect(actionButton(element)?.dataset["action"]).toBe("stop");
    expect(actionButton(element)?.disabled).toBe(false);
    expect(actionButton(element)?.getAttribute("aria-busy")).toBeNull();
  });

  it("cannot be made to send two requests by a double click", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));

    const button = plannerButton(element);
    button?.click();
    button?.click();

    expect(actionsOf(hass)).toHaveLength(1);
  });
});

describe("refusal, failures and honest states", () => {
  it("keeps the confirmed plan and shows one localized sentence above the status line", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    const plan = text(element);
    plannerButton(element)?.click();
    hass.resolveNext(refused("spotnav_action_unavailable"));
    await settle();

    expect(actionError(element)?.textContent).toBe(translate("en", "action.error.unavailable"));
    // Above the status sentence, and the graph and plan are the confirmed ones, untouched.
    const root = shadow(element);
    const error = root.querySelector(".spotnav-action-error:not([hidden])") as Element;
    const status = root.querySelector(".spotnav-status") as Element;
    expect(error.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(root.querySelector(".spotnav-chart-viewport svg")).not.toBeNull();
    expect(text(element)).toContain(plan.trim().slice(0, 20));
    // The backend's prose never reaches the card, and neither does the code for a known failure.
    expect(actionError(element)?.textContent).not.toContain("spotnav_");
    expect(actionsOf(hass)).toHaveLength(1);
  });

  it("says one generic sentence for a code it has no wording for, with the code as detail", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    plannerButton(element)?.click();
    hass.resolveNext(refused("spotnav_something_new"));
    await settle();

    expect(actionError(element)?.textContent).toBe(translate("en", "action.error.generic"));
    expect(actionError(element)?.dataset["code"]).toBe("spotnav_something_new");
  });

  it("turns a transport failure into the same localized treatment, and stays usable", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    plannerButton(element)?.click();
    hass.rejectNext({ code: "spotnav_action_failed", message: "backend prose that must not be shown" });
    await settle();

    expect(actionError(element)?.textContent).toBe(translate("en", "action.error.failed"));
    expect(text(element)).not.toContain("must not be shown");
    expect(plannerButton(element)?.disabled, "the automatic control works again").toBe(false);
  });

  it("renders three distinct honest states, none of them claiming charging", async () => {
    const seen: string[] = [];
    for (const name of ["action_pending", "pause_clear_failed", "no_settings"]) {
      document.body.innerHTML = "";
      const { element } = await mounted(fixture(name));
      const notice = text(element);
      expect(notice.toLowerCase(), name).not.toContain("charging now");
      for (const other of seen) {
        expect(other, name).not.toBe(notice);
      }
      seen.push(notice);
    }
  });
});

describe("the pause sheet", () => {
  const sheetChoices = (): TranslationKey[] => [
    "pause.nextPeriod",
    "pause.untilTomorrow",
    "pause.untilResumed",
  ];

  it("offers exactly the supplied choices, in backend order, and sends the one chosen", async () => {
    const { hass, element } = await mounted(fixture("stop_charging"));
    // The sheet belongs to the *automatic* control: the immediate Stop is a bare stop and carries no
    // choice at all, so only this button can open a pause.
    plannerButton(element)?.click();
    await settle();

    expect(openDialogs(element)).toHaveLength(1);
    expect(choiceButtons(element).map((button) => button.dataset["choice"])).toEqual([
      "next_period",
      "until_tomorrow",
      "until_resumed",
    ]);
    expect(choiceButtons(element).map((button) => button.textContent)).toEqual(
      sheetChoices().map((key) => translate("en", key)),
    );

    choiceButtons(element)[1]?.click();
    await settle();

    expect(actionsOf(hass)).toEqual([
      {
        type: "spotnav/manual_action",
        api_version: ACTION_API_VERSION,
        charger_id: "entry_a",
        action: "stop",
        choice: "until_tomorrow",
      },
    ]);
    expect(openDialogs(element)).toHaveLength(0);
  });

  it("sends one request when a second choice is clicked before the answer arrives", async () => {
    const { hass, element } = await mounted(fixture("stop_charging"));
    plannerButton(element)?.click();
    await settle();

    // A second click -- however it arrives -- is not a second command.
    choiceButtons(element)[0]?.click();
    choiceButtons(element)[1]?.click();
    await settle();

    expect(actionsOf(hass)).toHaveLength(1);
    expect(actionsOf(hass)[0]?.["choice"]).toBe("next_period");
  });

  it("never opens an empty sheet and never invents a fallback choice", async () => {
    const { hass, element } = await mounted(
      withControl("stop_charging", { pause_choices: [] }),
    );
    // The automatic control is still a Pause -- this charger simply cannot honour any choice right
    // now -- so the sheet stays shut rather than opening empty, and nothing is sent.
    const pause = plannerButton(element);
    expect(pause?.dataset["action"]).toBe("pause");
    expect(pause?.disabled).toBe(false);
    pause?.click();
    await settle();

    expect(openDialogs(element)).toHaveLength(0);
    expect(actionsOf(hass)).toHaveLength(0);

    // And the immediate Stop is not that pause: it is the bare command, and it is still offered.
    const stop = actionButton(element);
    expect(stop?.dataset["action"]).toBe("stop");
    stop?.click();
    await settle();
    expect(actionsOf(hass)).toEqual([
      {
        type: "spotnav/manual_action",
        api_version: ACTION_API_VERSION,
        charger_id: "entry_a",
        action: "stop",
      },
    ]);
    expect(actionsOf(hass)[0], "a bare stop carries no choice").not.toHaveProperty("choice");
  });

  it("sends nothing at all when the backend says the caller may not act", async () => {
    const { hass, element } = await mounted(withControl("stop_charging", { can_act: false }));
    // Both axes: a read-only caller may command neither the charger nor Home Assistant's execution.
    expect(actionButton(element)?.disabled).toBe(true);
    expect(plannerButton(element)?.disabled).toBe(true);
    actionButton(element)?.click();
    plannerButton(element)?.click();
    await settle();

    const { element: resume } = await mounted(withControl("resume_active", { can_act: false }));
    plannerButton(resume)?.click();
    await settle();

    expect(actionsOf(hass)).toHaveLength(0);
    expect(openDialogs(element)).toHaveLength(0);
  });

  it("closes on Escape and returns focus to the Pause trigger", async () => {
    const { element } = await mounted(fixture("stop_charging"));
    const pause = plannerButton(element) as HTMLButtonElement;
    pause.focus();
    pause.click();
    await settle();

    const dialog = openDialogs(element)[0] as HTMLElement;
    // The dialog primitive owns one keydown listener on the document, in the capture phase, so this
    // is how Escape really arrives: at the document, not at a node inside the shadow root.
    dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, composed: true }));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();

    expect(openDialogs(element)).toHaveLength(0);
    expect(shadow(element).activeElement).toBe(pause);
  });

  it("keeps a hostile charger name as text inside both dialogs", async () => {
    const hostile = "<img src=x onerror=\"window.pwned=1\">";
    const payload = fixture("stop_charging");
    payload.charger = { ...(payload.charger as Record<string, unknown>), name: hostile };
    const { element } = await mounted(payload);

    plannerButton(element)?.click();
    await settle();
    strategyButton(element)?.click();
    await settle();

    expect(shadow(element).querySelector("img")).toBeNull();
    expect(shadow(element).querySelector("[onerror]")).toBeNull();
  });
});

describe("the strategy dialog", () => {
  it("shows Cheapest selected, the placeholders disabled with their reason, and sends nothing", async () => {
    const { hass, element } = await mounted(fixture("start_idle"));
    const before = hass.messages.length;
    const trigger = strategyButton(element) as HTMLButtonElement;
    expect(trigger.querySelector(".spotnav-settings-value")?.textContent).toBe(translate("en", "strategy.cheapest"));
    expect(trigger.getAttribute("aria-label")).toBe(
      `${translate("en", "strategy.title")}: ${translate("en", "strategy.cheapest")}. ${translate("en", "bar.change")}`,
    );

    trigger.click();
    await settle();

    const rows = strategyRows(element);
    expect(rows.map((row) => row.dataset["strategy"])).toEqual(["cheapest", "solar", "hybrid"]);
    expect(rows.map((row) => row.textContent)).toEqual([
      translate("en", "strategy.cheapest"),
      translate("en", "strategy.solar"),
      translate("en", "strategy.hybrid"),
    ]);
    expect(rows.map((row) => row.disabled)).toEqual([true, true, true]);
    // Selected is a fact about the backend state, and the two future strategies say why not yet.
    expect(rows.map((row) => row.getAttribute("aria-pressed"))).toEqual(["true", "false", "false"]);
    expect(text(element)).toContain(translate("en", "strategy.reason.solar"));
    expect(text(element)).toContain(translate("en", "strategy.reason.hybrid"));

    // A disabled row cannot be pressed, so no path from this dialog reaches the backend at all.
    for (const row of rows) {
      row.click();
    }
    await settle();
    expect(hass.messages.length).toBe(before);
  });

  it("writes nothing when it is opened and closed again", async () => {
    const { hass, element } = await mounted(fixture("start_idle"));
    const before = hass.messages.length;
    strategyButton(element)?.click();
    await settle();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await settle();

    expect(openDialogs(element)).toHaveLength(0);
    expect(hass.messages.length).toBe(before);
  });
});

describe("the request lifecycle", () => {
  it("leaves a late action answer inert once the card is disconnected", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    const reads = readsOf(hass).length;
    plannerButton(element)?.click();
    element.remove();

    hass.resolveNext(started("resume"));
    await settle();

    expect(readsOf(hass).length, "no forced read for a card that is gone").toBe(reads);
    expect(element.shadowRoot?.querySelectorAll("[role='dialog']").length).toBe(0);
  });

  it("defers an ordinary read while an action is in flight, then reads exactly once", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    const reads = readsOf(hass).length;
    plannerButton(element)?.click();

    // The 30-second timer fires while the action is unanswered. A read started now could commit
    // *before* the action does and then be the newest attempt, leaving the card stale -- so the tick
    // is coalesced instead of started.
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    expect(readsOf(hass).length, "a pending action owns the read lifecycle").toBe(reads);
    expect(actionsOf(hass)).toHaveLength(1);

    hass.resolveNext(started("resume"));
    await settle();

    // Exactly one read, started after the success, and it answers the timer's tick as well.
    expect(readsOf(hass).length, "one confirmation read after the commit").toBe(reads + 1);
    hass.resolveNext(fixture("start_idle"));
    await settle();
    expect(actionButton(element)?.dataset["action"]).toBe("start");
    expect(actionError(element)).toBeNull();
  });

  it("does not let an older ordinary read overwrite the confirmed snapshot", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    const reads = readsOf(hass).length;

    // A read that began before the click is still unanswered, so it cannot be a confirmation of it.
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    expect(readsOf(hass).length).toBe(reads + 1);
    plannerButton(element)?.click();

    // Pending: [0] the older ordinary read, [1] the action request.
    hass.resolveAt(1, started("resume"));
    await settle();
    expect(readsOf(hass).length, "the confirmation is its own, newer attempt").toBe(reads + 2);

    // Pending: [0] the older ordinary read, [1] the confirmation. The confirmation answers first,
    // and the view is rebuilt from it.
    hass.resolveAt(1, fixture("stop_charging"));
    await settle();
    expect(actionButton(element)?.dataset["action"]).toBe("stop");
    const confirmed = dashboardOf(element);
    const confirmedGraph = shadow(element).querySelector(".spotnav-chart-viewport svg");

    // The older read now answers with what it read *before* the action: inert in every way.
    hass.resolveAt(0, fixture("resume_active"));
    await settle();
    expect(actionButton(element)?.dataset["action"]).toBe("stop");
    expect(dashboardOf(element)).toBe(confirmed);
    expect(shadow(element).querySelector(".spotnav-chart-viewport svg")).toBe(confirmedGraph);
  });

  it("starts no confirmation read and renders nothing after the card is disconnected", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    const reads = readsOf(hass).length;
    plannerButton(element)?.click();
    element.remove();

    hass.resolveNext(started("resume"));
    await settle();

    expect(readsOf(hass).length, "no read for a card that is gone").toBe(reads);
    expect(shadow(element).querySelector(".spotnav-chart-viewport svg")).toBeNull();
  });

  it("starts no confirmation read for a charger the card has left", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    plannerButton(element)?.click();
    element.setConfig({ type: "custom:spotnav-card", charger: "entry_b" });
    await settle();

    const readsFor = (charger: string) =>
      readsOf(hass).filter((message) => message.charger_id === charger).length;
    const before = readsFor("entry_a");
    hass.resolveAt(0, started("resume"));
    await settle();

    expect(readsFor("entry_a"), "the action's charger is no longer this card's").toBe(before);
  });

  it("leaves an action answer for a charger the card has left completely inert", async () => {
    // Not just the previous charger's read, but any read at all. With a
    // stale answer accepted, the card would treat it as a confirmation and start a read for whichever
    // charger it now shows -- work that belongs to a screen that no longer exists.
    const { hass, element } = await mounted(fixture("resume_active"));
    plannerButton(element)?.click();
    element.setConfig({ type: "custom:spotnav-card", charger: "entry_b" });
    await settle();
    const reads = readsOf(hass).length;

    hass.resolveAt(0, started("resume"));
    await settle();

    expect(readsOf(hass).length, "a stale answer owes nobody a read").toBe(reads);
  });

  it("reads back nothing for a refusal, and runs the coalesced timer read instead", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    const reads = readsOf(hass).length;
    plannerButton(element)?.click();
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);

    hass.resolveNext(refused("spotnav_action_failed"));
    await settle();

    // Nothing was confirmed, so the read the timer had to coalesce is still owed -- one of it.
    expect(readsOf(hass).length).toBe(reads + 1);
    expect(actionError(element)?.textContent).toBe(translate("en", "action.error.failed"));
    hass.resolveNext(fixture("resume_active"));
    await settle();
    expect(actionError(element)).toBeNull();
  });

  it("keeps two cards' actions, dialogs and errors apart", async () => {
    const first = await mounted(fixture("stop_charging"));
    const second = await mounted(fixture("resume_active"));

    // The first card opens its sheet (which is not a request yet).
    plannerButton(first.element)?.click();
    await settle();
    expect(openDialogs(first.element)).toHaveLength(1);
    expect(openDialogs(second.element)).toHaveLength(0);

    // The second card's own action -- its Resume -- goes out while the first is still choosing.
    plannerButton(second.element)?.click();
    await settle();
    expect(actionsOf(second.hass)).toHaveLength(1);

    // And the first card's choice is not blocked by the second card's request: one request per card,
    // no shared in-flight guard.
    choiceButtons(first.element)[0]?.click();
    await settle();
    expect(actionsOf(first.hass)).toHaveLength(1);

    second.hass.resolveNext(started("resume"));
    await settle();
    second.hass.resolveNext(fixture("resume_active"));
    await settle();
    first.hass.resolveNext(refused("spotnav_action_failed"));
    await settle();

    expect(actionError(first.element)?.textContent).toBe(translate("en", "action.error.failed"));
    expect(actionError(second.element)).toBeNull();
    expect(plannerButton(second.element)?.dataset["action"]).toBe("resume");
    expect(plannerButton(second.element)?.disabled).toBe(false);
  });
});

describe("layout order", () => {
  it("keeps the graph first and the row after the plan content", async () => {
    // The area/currency line lives in the Settings popover, not in the always-visible layout.
    const { element } = await mounted(fixture("stop_charging"));
    const root = shadow(element);
    const order = [".spotnav-chart-viewport", ".spotnav-periods", ".spotnav-action-bar"];
    const nodes = order.map((selector) => root.querySelector(selector));
    for (let index = 0; index < nodes.length; index += 1) {
      expect(nodes[index], `${order[index]} is rendered`).not.toBeNull();
    }
    for (let index = 0; index < nodes.length - 1; index += 1) {
      const node = nodes[index] as Element;
      const next = nodes[index + 1] as Element;
      expect(
        node.compareDocumentPosition(next) & Node.DOCUMENT_POSITION_FOLLOWING,
        `${order[index]} precedes ${order[index + 1]}`,
      ).toBeTruthy();
    }
  });
});

describe("a confirmation that cannot be read", () => {
  const cases: Array<{
    name: string;
    code: string | null;
    prose: string | null;
    fail: (hass: FakeHass) => void;
  }> = [
    {
      name: "transport failure",
      code: null,
      prose: "the socket is gone",
      fail: (hass) => hass.rejectNext(new Error("the socket is gone")),
    },
    {
      name: "malformed payload",
      code: null,
      prose: null,
      fail: (hass) => hass.resolveNext({ api_version: 2, chargers: [] }),
    },
    {
      name: "unsupported payload",
      code: null,
      prose: null,
      fail: (hass) => hass.resolveNext({ api_version: 3 }),
    },
    {
      name: "charger refusal",
      code: "spotnav_unknown_charger",
      prose: "no such charger",
      fail: (hass) => hass.rejectNext(apiFailure("spotnav_unknown_charger", "no such charger")),
    },
  ];

  /** A confirmed snapshot, an accepted action, and then a confirmation read that fails. */
  async function confirmedThenFailed(fail: (hass: FakeHass) => void): Promise<{
    hass: FakeHass;
    element: ReturnType<typeof mountCard>;
    html: string;
    graph: Element | null;
    dashboard: unknown;
  }> {
    const { hass, element } = await mounted(fixture("resume_active"));
    const html = graphHtml(element);
    const graph = shadow(element).querySelector(".spotnav-chart-viewport svg");
    const dashboard = dashboardOf(element);
    const reads = readsOf(hass).length;

    plannerButton(element)?.click();
    hass.resolveNext(started("resume"));
    await settle();
    expect(readsOf(hass).length, "the confirmation read did start").toBe(reads + 1);

    fail(hass);
    await settle();
    return { hass, element, html, graph, dashboard };
  }

  for (const item of cases) {
    it(`keeps the confirmed graph and plan on a ${item.name}`, async () => {
      const result = await confirmedThenFailed(item.fail);

      // The exact confirmed snapshot, proved three ways: the same dashboard object, the same graph
      // node and the same serialized pixels.
      expect(dashboardOf(result.element)).toBe(result.dashboard);
      expect(shadow(result.element).querySelector(".spotnav-chart-viewport svg")).toBe(result.graph);
      expect(graphHtml(result.element)).toBe(result.html);

      // One localized sentence above the status line, and the stable code only as subdued detail.
      // It is the *confirmation* sentence: the envelope was accepted, and only the read failed, so
      // nothing here may claim the plan changed or that the charger started or stopped.
      const error = actionError(result.element);
      expect(error?.textContent).toBe(translate("en", "action.error.confirmationFailed"));
      expect(error?.textContent).not.toBe(translate("en", "action.error.reconcileFailed"));
      expect(error?.dataset["code"]).toBe(item.code === null ? undefined : item.code);
      if (item.prose !== null) {
        expect(text(result.element), "no backend prose").not.toContain(item.prose);
      }
      // Pending is cleared: both controls work again.
      expect(plannerButton(result.element)?.disabled).toBe(false);
      expect(actionButton(result.element)?.disabled).toBe(false);
    });
  }

  it("still shows the reconcile sentence for the backend's own reconcile failure", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    const reads = readsOf(hass).length;
    plannerButton(element)?.click();

    hass.resolveNext(refused("spotnav_action_reconcile_failed"));
    await settle();

    // No confirmation read: the envelope itself reported the failed reconcile, which is the one fact
    // the reconcile sentence is allowed to describe.
    expect(readsOf(hass).length).toBe(reads);
    const error = actionError(element);
    expect(error?.textContent).toBe(translate("en", "action.error.reconcileFailed"));
    expect(error?.dataset["code"]).toBe("spotnav_action_reconcile_failed");
  });

  it("never claims an outcome when only the confirmation read failed", () => {
    for (const language of LANGUAGES) {
      const confirmation = translate(language, "action.error.confirmationFailed");
      expect(confirmation, language).not.toBe(translate(language, "action.error.reconcileFailed"));
      expect(confirmation.trim(), language).not.toBe("");
    }
    // The English wording is the one the plan quotes, and it promises no outcome at all.
    expect(translate("en", "action.error.confirmationFailed")).not.toMatch(
      /plan|start|stop|charg|updat|changed/i,
    );
  });

  it("lets a later ordinary refresh replace the retained snapshot and clear the sentence", async () => {
    const result = await confirmedThenFailed((hass) => hass.rejectNext(new Error("gone")));
    expect(actionError(result.element)).not.toBeNull();

    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    result.hass.resolveNext(fixture("stop_charging"));
    await settle();

    expect(actionError(result.element)).toBeNull();
    expect(actionButton(result.element)?.dataset["action"]).toBe("stop");
    expect(shadow(result.element).querySelector(".spotnav-chart-viewport svg")).not.toBe(result.graph);
  });
});

describe("ordinary reads keep their own behaviour", () => {
  it("shows the failed state for an initial transport failure, with the retry action", async () => {
    const hass = new FakeHass();
    const element = mountCard(CONFIG, hass);
    element.hass = hass.snapshot("snapshot", "en");
    await settle();
    hass.rejectNext(new Error("no connection"));
    await settle();

    expect(text(element)).toContain(translate("en", "state.requestFailed"));
    expect(shadow(element).querySelector(".spotnav-chart-viewport svg")).toBeNull();
    expect(shadow(element).querySelector(".spotnav-icon-button")?.textContent).toBe(
      translate("en", "state.retry"),
    );
  });

  it("keeps that same full-card transition for a background failure", async () => {
    const { hass, element } = await mounted(fixture("start_idle"));
    await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
    hass.rejectNext(new Error("no connection"));
    await settle();

    expect(text(element)).toContain(translate("en", "state.requestFailed"));
    expect(shadow(element).querySelector(".spotnav-chart-viewport svg")).toBeNull();
  });
});

describe("admission against the rendered snapshot", () => {
  it("judges each request by the axis that owns it, and by nothing else", async () => {
    // `resume_active` is immediate Start + automatic Resume, so exactly one request is admissible
    // per axis: the immediate Start, and the automatic Resume. A pause is a request this snapshot
    // does not admit at all -- the automatic axis is not offering one -- and a choice beside a stop
    // is not a resume either.
    const { hass, element } = await mounted(fixture("resume_active"));

    // Each request is answered before the next can be sent: one action is in flight at a time (and
    // a refusal is still an *admitted* request -- it is the envelope that came back, not a shape the
    // card refused to send).
    void element.performAction("start");
    await settle();
    expect(actionsOf(hass), "the immediate axis admits start").toHaveLength(1);
    hass.resolveNext(refused("spotnav_action_failed"));
    await settle();

    void element.performAction("resume");
    await settle();
    expect(actionsOf(hass), "the automatic axis admits resume").toHaveLength(2);
    hass.resolveNext(refused("spotnav_action_failed"));
    await settle();

    await element.performAction("stop", "until_resumed");
    await element.performAction("resume", "until_tomorrow");
    expect(actionsOf(hass), "neither axis admits these").toHaveLength(2);
  });

  it("sends nothing for a snapshot that offers no action on either axis", async () => {
    const { hass, element } = await mounted(fixture("action_pending"));
    await element.performAction("start");
    await element.performAction("stop", null);
    await element.performAction("stop", "until_resumed");
    await element.performAction("resume");
    expect(actionsOf(hass)).toHaveLength(0);
  });

  it("sends a bare Stop for the immediate axis, and nothing for a choice it did not publish", async () => {
    const { hass, element } = await mounted(fixture("stop_charging"));
    // A choice-less stop is the *immediate* command: it goes out, and carries no choice.
    void element.performAction("stop", null);
    await settle();
    expect(actionsOf(hass)).toHaveLength(1);
    expect(actionsOf(hass)[0]).not.toHaveProperty("choice");

    // A stop *with* a choice is the automatic pause, and this snapshot published no such choice.
    await element.performAction("stop", "next_week");
    expect(actionsOf(hass)).toHaveLength(1);

    const empty = await mounted(withControl("stop_charging", { pause_choices: [] }));
    await empty.element.performAction("stop", "until_resumed");
    expect(actionsOf(empty.hass)).toHaveLength(0);
  });

  it("still accepts the choice the snapshot did publish", async () => {
    const { hass, element } = await mounted(fixture("stop_charging"));
    // Not awaited: the request is answered by whichever test owns the transport, and this one only
    // asks whether it went out at all.
    void element.performAction("stop", "until_tomorrow");
    await settle();

    expect(actionsOf(hass)).toHaveLength(1);
    expect(actionsOf(hass)[0]?.["choice"]).toBe("until_tomorrow");
  });

  it("never lets one axis answer for the other", async () => {
    // `stop_charging` offers immediate Stop *and* automatic Pause. The immediate axis must not admit
    // the pause (that is a `stop` with a choice, and the choice is judged by the automatic axis), and
    // the automatic axis must not admit a bare stop (that is the immediate command).
    const { hass, element } = await mounted(fixture("stop_charging"));
    await element.performAction("resume");
    await element.performAction("start");
    expect(actionsOf(hass)).toHaveLength(0);

    const { hass: idle, element: idleCard } = await mounted(fixture("start_idle"));
    // An idle Auto charger: Start now is admitted (the immediate axis), and the *pause* is the
    // automatic axis's own request -- the same battery of checks, from the charger's other state.
    void idleCard.performAction("start");
    await settle();
    expect(actionsOf(idle)).toHaveLength(1);
    idle.resolveNext(refused("spotnav_action_failed"));
    await settle();

    await idleCard.performAction("stop", null);
    expect(actionsOf(idle), "an idle charger cannot be stopped").toHaveLength(1);

    void idleCard.performAction("stop", "until_resumed");
    await settle();
    expect(actionsOf(idle)).toHaveLength(2);
    expect(actionsOf(idle)[1]?.["choice"]).toBe("until_resumed");
  });
});

describe("the state captions of the action bar", () => {
  const captionOf = (button: Element | null): string | null | undefined =>
    button?.querySelector(".spotnav-bar-caption")?.textContent;

  it.each([
    ["start_idle", "Charge now", "Start", "Schedule active", "Pause"],
    ["stop_charging", "Charging", "Stop", "Schedule active", "Pause"],
    ["resume_active", "Charge now", "Start", "Schedule paused", "Resume"],
  ])("%s: captions state the state and the lower line the action", async (name, charging, chargingAction, schedule, scheduleAction) => {
    const { element } = await mounted(fixture(name));
    const immediate = actionButton(element);
    const automatic = plannerButton(element);
    expect(captionOf(immediate)).toBe(charging);
    expect(immediate?.querySelector(".spotnav-settings-value")?.textContent).toBe(chargingAction);
    expect(captionOf(automatic)).toBe(schedule);
    expect(automatic?.querySelector(".spotnav-settings-value")?.textContent?.toLowerCase()).toContain(scheduleAction.toLowerCase());
  });

  it("keeps the Charge now caption on the busy cell while a start is pending", async () => {
    const { element } = await mounted(fixture("action_pending"));
    const immediate = actionButton(element);
    expect(captionOf(immediate)).toBe("Charge now");
    expect(immediate?.getAttribute("aria-busy")).toBe("true");
    expect(immediate?.getAttribute("aria-label")).toContain(translate("en", "bar.state.notCharging"));
  });

  it("offers no Schedule cell, so no state caption, when the automatic axis has no action", async () => {
    const { element } = await mounted(fixture("no_settings"));
    expect(plannerButton(element)).toBeNull();
  });

  it("has all eight caption keys, non-empty and distinct per axis, in every locale", () => {
    const keys = [
      "bar.chargeNow", "bar.chargingNow", "bar.scheduleActive", "bar.schedulePaused",
      "bar.state.notCharging", "bar.state.charging", "bar.state.scheduleActive", "bar.state.schedulePaused",
    ] as const;
    for (const language of ["en", "sv", "nb", "da", "fi"] as const) {
      for (const key of keys) {
        expect(translate(language, key), `${language} ${key}`).not.toBe("");
      }
      expect(translate(language, "bar.chargeNow"), language).not.toBe(translate(language, "bar.chargingNow"));
      expect(translate(language, "bar.scheduleActive"), language).not.toBe(translate(language, "bar.schedulePaused"));
    }
    expect(translate("sv", "bar.chargeNow")).toBe("Ladda nu");
    expect(translate("sv", "bar.chargingNow")).toBe("Laddar");
    expect(translate("sv", "bar.scheduleActive")).toBe("Schema aktivt");
    expect(translate("sv", "bar.schedulePaused")).toBe("Schema pausat");
  });
});

describe("the control cells while a Start or Stop awaits the charger", () => {
  const captionOf = (button: Element | null): string | null | undefined =>
    button?.querySelector(".spotnav-bar-caption")?.textContent;
  const valueOf = (button: Element | null): string | null | undefined =>
    button?.querySelector(".spotnav-settings-value")?.textContent;
  const waiting = translate("en", "bar.waitingForCharger");

  /** The `action_pending` fixture with the charger's own report set to [charging]. */
  function pendingWith(charging: boolean): Record<string, unknown> {
    const payload = fixture("action_pending");
    payload.live = { ...(payload.live as Record<string, unknown>), charging };
    return payload;
  }

  it("says Starting… from the charger's state when this card sent nothing", async () => {
    const { element } = await mounted(pendingWith(false));
    const immediate = actionButton(element);
    expect(immediate?.disabled).toBe(true);
    expect(captionOf(immediate)).toBe("Charge now");
    expect(valueOf(immediate)).toBe("Starting…");
    expect(immediate?.getAttribute("aria-label")).toBe(`Charging: not charging. ${waiting}`);
    // Nothing was shown before, so there is no schedule caption to keep.
    expect(plannerButton(element)).toBeNull();
  });

  it("says Stopping… from the charger's state while it charges", async () => {
    const { element } = await mounted(pendingWith(true));
    const immediate = actionButton(element);
    expect(immediate?.disabled).toBe(true);
    expect(captionOf(immediate)).toBe("Charging");
    expect(valueOf(immediate)).toBe("Stopping…");
    expect(immediate?.getAttribute("aria-label")).toBe(`Charging: charging. ${waiting}`);
  });

  it("names the Stop it sent at once, and keeps the schedule cell's caption, disabled, until the outcome", async () => {
    const { hass, element } = await mounted(fixture("stop_charging"));
    actionButton(element)?.click();
    // Before any answer: the pressed cell already says what is under way, the other is greyed.
    expect(valueOf(actionButton(element))).toBe("Stopping…");
    expect(actionButton(element)?.disabled).toBe(true);
    expect(valueOf(plannerButton(element))).toBe(translate("en", "action.pauseAutomaticShort"));
    expect(plannerButton(element)?.disabled).toBe(true);

    hass.resolveNext(started("stop"));
    await settle();
    // Home Assistant says action_pending while the charger still reports the charge.
    hass.resolveNext(pendingWith(true));
    await settle();
    expect(captionOf(actionButton(element))).toBe("Charging");
    expect(valueOf(actionButton(element))).toBe("Stopping…");
    expect(actionButton(element)?.disabled).toBe(true);
    const held = plannerButton(element);
    expect(held, "the schedule cell stays").not.toBeNull();
    expect(captionOf(held)).toBe("Schedule active");
    expect(valueOf(held)).toBe(translate("en", "action.pauseAutomaticShort"));
    expect(held?.disabled).toBe(true);
    expect(held?.getAttribute("aria-label")).toBe(`Schedule: active. ${waiting}`);

    // The next read offers actions again: both cells are live.
    await vi.advanceTimersByTimeAsync(5_000);
    hass.resolveNext(fixture("start_idle"));
    await settle();
    expect(valueOf(actionButton(element))).toBe("Start");
    expect(actionButton(element)?.disabled).toBe(false);
    expect(plannerButton(element)?.disabled).toBe(false);
  });

  it("names the Start it sent even while the charger still reports charging", async () => {
    const { hass, element } = await mounted(fixture("resume_active"));
    actionButton(element)?.click();
    expect(valueOf(actionButton(element))).toBe("Starting…");
    hass.resolveNext(started("start"));
    await settle();
    hass.resolveNext(pendingWith(true));
    await settle();
    expect(captionOf(actionButton(element))).toBe("Charge now");
    expect(valueOf(actionButton(element))).toBe("Starting…");
    const held = plannerButton(element);
    expect(captionOf(held)).toBe("Schedule paused");
    expect(valueOf(held)).toBe(translate("en", "action.resumeShort"));
    expect(held?.disabled).toBe(true);
  });

  it("drops the kept schedule caption once nothing is pending", async () => {
    const { hass, element } = await mounted(fixture("start_idle"));
    actionButton(element)?.click();
    hass.resolveNext(started("start"));
    await settle();
    hass.resolveNext(pendingWith(false));
    await settle();
    expect(plannerButton(element)).not.toBeNull();
    await vi.advanceTimersByTimeAsync(5_000);
    hass.resolveNext(fixture("no_settings"));
    await settle();
    await vi.advanceTimersByTimeAsync(5_000);
    hass.resolveNext(pendingWith(false));
    await settle();
    // A pending read after one that offered no schedule control has no caption to keep.
    expect(plannerButton(element)).toBeNull();
  });

  it("gives the Start cell its own word back when the Start is refused", async () => {
    const { hass, element } = await mounted(fixture("start_idle"));
    actionButton(element)?.click();
    expect(valueOf(actionButton(element))).toBe("Starting…");
    hass.resolveNext(refused("spotnav_action_failed"));
    await settle();
    expect(valueOf(actionButton(element))).toBe("Start");
    expect(actionButton(element)?.getAttribute("aria-label")).toContain(translate("en", "action.start"));
    expect(actionButton(element)?.disabled).toBe(false);
  });

  it("leaves the Start cell's word alone while a pause or resume is in flight", async () => {
    const { element } = await mounted(fixture("resume_active"));
    plannerButton(element)?.click();
    expect(valueOf(actionButton(element))).toBe("Start");
    expect(actionButton(element)?.disabled).toBe(true);
  });

  it("has the words in every language", () => {
    const want: Record<string, [string, string]> = {
      en: ["Starting…", "Stopping…"],
      sv: ["Startar…", "Stoppar…"],
      nb: ["Starter…", "Stopper…"],
      da: ["Starter…", "Stopper…"],
      fi: ["Käynnistetään…", "Pysäytetään…"],
    };
    for (const language of ["en", "sv", "nb", "da", "fi"] as const) {
      expect(translate(language, "bar.starting"), language).toBe(want[language]?.[0]);
      expect(translate(language, "bar.stopping"), language).toBe(want[language]?.[1]);
    }
    expect(translate("en", "bar.waitingForCharger")).toBe("Waiting for the charger");
    expect(translate("sv", "bar.waitingForCharger")).toBe("Väntar på laddaren");
    expect(translate("nb", "bar.waitingForCharger")).toBe("Venter på laderen");
    expect(translate("da", "bar.waitingForCharger")).toBe("Venter på laderen");
    expect(translate("fi", "bar.waitingForCharger")).toBe("Odotetaan laturia");
  });
});
