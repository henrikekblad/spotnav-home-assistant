// The control and strategy facts, as the model derives them from the backend's own fixtures.
//
// These are the backend's payloads (`tests/fixtures/dashboard/*.json`, generated and pinned by the
// Python suite) put through `buildModel` -- so what is asserted here is exactly what the row and the
// two dialogs will render, with no DOM in the way and no second decision rule anywhere.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { buildModel, type CardModel } from "../src/model";
import { decodeDashboard, type Dashboard } from "../src/validate";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const NOW_MS = Date.parse("2026-09-22T06:00:00+00:00");

function dashboardFor(name: string): Dashboard {
  const result = decodeDashboard(JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")));
  if (!result.ok) {
    throw new Error(`${name} did not decode: ${result.failure}`);
  }
  return result.value;
}

function modelFor(name: string, language: "en" | "sv" = "en"): CardModel {
  return buildModel({ dashboard: dashboardFor(name), language, nowMs: NOW_MS });
}

function names(): string[] {
  return readdirSync(DASHBOARD_DIR).filter((name) => name.endsWith(".json"));
}

/** Every fixture's two axes, as the buttons read them: [immediate, automatic], each with its label. */
const EXPECTED: Record<string, [[string, string | null], [string, string | null]]> = {
  "start_idle.json": [["start", "action.start"], ["pause", "action.pauseAutomatic"]],
  "stop_charging.json": [["stop", "action.stop"], ["pause", "action.pauseAutomatic"]],
  "resume_active.json": [["start", "action.start"], ["resume", "action.resume"]],
  "pause_clear_failed.json": [["start", "action.start"], ["none", null]],
  "action_pending.json": [["none", null], ["none", null]],
  "manual_stop.json": [["start", "action.start"], ["resume", "action.resume"]],
  "no_settings.json": [["none", null], ["none", null]],
};

/** Every other fixture is an idle Auto charger: Start now beside the automatic Pause. */
const IDLE_AUTO: [[string, string | null], [string, string | null]] = [
  ["start", "action.start"],
  ["pause", "action.pauseAutomatic"],
];

describe("the control facts", () => {
  it("names both axes the backend named, each with its own button label", () => {
    // The table is exhaustive, so a fixture that changed axis fails here by name rather than being
    // rendered as something subtly different.
    expect(Object.keys(EXPECTED).every((name) => names().includes(name))).toBe(true);
    for (const name of names()) {
      const model = modelFor(name);
      const expected = EXPECTED[name] ?? IDLE_AUTO;
      expect([model.control.immediate.action, model.control.immediate.labelKey], name).toEqual(
        expected[0],
      );
      expect([model.control.automatic.action, model.control.automatic.labelKey], name).toEqual(
        expected[1],
      );
    }
  });

  it("offers Pause on an idle Auto charger, which is the defect this split was raised for", () => {
    // `start_idle` is an idle charger with a plan installed: the immediate axis offers Start now, and
    // the automatic axis offers the pause beside it. A single folded action could not say both.
    const idle = modelFor("start_idle.json");
    expect(idle.control.immediate.action).toBe("start");
    expect(idle.control.automatic.action).toBe("pause");
    expect(idle.control.choices.map((choice) => choice.id)).toEqual([
      "next_period",
      "until_tomorrow",
      "until_resumed",
    ]);
    // The two buttons are distinct in label as well as in action, so neither can be mistaken for the
    // other on screen.
    expect(idle.control.immediate.labelKey).not.toBe(idle.control.automatic.labelKey);
  });

  it("does not re-decide either axis from charging, plans or a pause's instants", () => {
    // The same payload with the two facts a frontend *could* be tempted to infer from, flipped: the
    // model still says exactly what the backend said, on both axes.
    const raw = JSON.parse(readFileSync(join(DASHBOARD_DIR, "start_idle.json"), "utf8")) as Record<string, unknown>;
    (raw.live as Record<string, unknown>).charging = true;
    (raw.live as Record<string, unknown>).schedule_active = true;
    const result = decodeDashboard(raw);
    expect(result.ok).toBe(true);
    if (result.ok) {
      const model = buildModel({ dashboard: result.value, language: "en", nowMs: NOW_MS });
      expect(model.control.immediate.action).toBe("start");
      expect(model.control.immediate.labelKey).toBe("action.start");
      expect(model.control.automatic.action).toBe("pause");
    }
  });

  it("gives every axis that has no action its own sentence", () => {
    const sentences = new Map<string, string | null>();
    for (const name of names()) {
      sentences.set(name, modelFor(name).control.notice);
    }
    // Fixtures with an action on both axes have nothing to explain.
    for (const name of ["start_idle.json", "stop_charging.json", "resume_active.json"]) {
      expect(sentences.get(name), name).toBeNull();
    }
    // And the three no-action states each get a distinct sentence.
    const distinct = [
      sentences.get("pause_clear_failed.json"),
      sentences.get("action_pending.json"),
      sentences.get("no_settings.json"),
    ];
    expect(distinct.every((text) => typeof text === "string" && text !== "")).toBe(true);
    expect(new Set(distinct).size).toBe(3);
    for (const name of ["pause_clear_failed.json", "action_pending.json", "no_settings.json"]) {
      // A reason the card has a sentence for is *not* repeated as a code: the code is only rescued as
      // technical detail when there is no sentence at all.
      expect(modelFor(name).control.noticeCode, name).toBeNull();
    }
  });

  it("carries each axis's own reason, beside the axis it belongs to", () => {
    // The notice is one sentence for the moment; each axis keeps its own reason, so a surface that
    // speaks about one axis alone has the words for it.
    const failed = modelFor("pause_clear_failed.json");
    expect(failed.control.immediate.reason).toBeNull();
    expect(failed.control.immediate.reasonCode).toBeNull();
    expect(failed.control.automatic.reason).toContain("could not be cleared");
    expect(failed.control.automatic.reasonCode).toBeNull();

    // The one state that says the same thing on both axes, in its own sentence each.
    const pending = modelFor("action_pending.json");
    expect(pending.control.immediate.reason).toBe(pending.control.automatic.reason);
    expect(pending.control.immediate.reason).toContain("waiting for the charger");

    const empty = modelFor("no_settings.json");
    expect(empty.control.immediate.reason).toContain("no settings");
    expect(empty.control.automatic.reason).toContain("no settings");
  });

  it("offers the pause choices only beside an automatic pause, in the backend's order", () => {
    for (const name of ["start_idle.json", "stop_charging.json"]) {
      expect(modelFor(name).control.choices, name).toEqual([
        { id: "next_period", labelKey: "pause.nextPeriod" },
        { id: "until_tomorrow", labelKey: "pause.untilTomorrow" },
        { id: "until_resumed", labelKey: "pause.untilResumed" },
      ]);
    }
    // No choices anywhere the automatic axis is not a pause, so nothing can open an empty dialog or
    // invent a fallback for an immediate Stop.
    for (const name of names()) {
      const model = modelFor(name);
      expect(model.control.choices.length > 0, name).toBe(model.control.automatic.action === "pause");
    }
  });

  it("keeps the authority flag, and reports an execution failure it has no sentence for as a code", () => {
    expect(modelFor("start_idle.json").control.canAct).toBe(true);

    const raw = JSON.parse(readFileSync(join(DASHBOARD_DIR, "start_idle.json"), "utf8")) as Record<string, unknown>;
    (raw.control as Record<string, unknown>).execution_error = "something_new";
    const result = decodeDashboard(raw);
    expect(result.ok).toBe(true);
    if (result.ok) {
      const model = buildModel({ dashboard: result.value, language: "en", nowMs: NOW_MS });
      expect(model.control.notice).toBeTruthy();
      expect(model.control.noticeCode).toBe("something_new");
    }

    // A known execution failure speaks for itself, with no code beside it.
    const known = modelFor("pause_clear_failed.json");
    expect(known.control.notice).toContain("could not be cleared");
    expect(known.control.noticeCode).toBeNull();
  });
});

describe("the strategy facts", () => {
  it("names the selected strategy and explains the unavailable ones", () => {
    const model = modelFor("start_idle.json");
    expect(model.strategy.selected).toBe("Cheapest");
    expect(model.strategy.rows).toEqual([
      { id: "cheapest", labelKey: "strategy.cheapest", available: true, reason: null, reasonCode: null },
      {
        id: "solar",
        labelKey: "strategy.solar",
        available: false,
        reason: "Requires solar-surplus measurement",
        reasonCode: "needs_solar_surplus_measurement",
      },
      {
        id: "hybrid",
        labelKey: "strategy.hybrid",
        available: false,
        reason: "Requires solar and price control",
        reasonCode: "needs_solar_and_price_control",
      },
    ]);
  });

  it("words a direct site's missing total grid power, for solar and hybrid, and offers them on a site with it", () => {
    const held = modelFor("cheapest_direct_site_admin.json");
    expect(held.strategy.rows.map((row) => [row.id, row.available, row.reasonCode])).toEqual([
      ["cheapest", true, null],
      ["solar", false, "needs_total_grid_power"],
      ["hybrid", false, "needs_total_grid_power"],
    ]);
    expect(held.strategy.rows[1]?.reason).toBe("Solar needs the meter's total grid power");

    const offered = modelFor("solar_direct_site_with_total.json");
    expect(offered.strategy.rows.map((row) => [row.id, row.available, row.reason])).toEqual([
      ["cheapest", true, null],
      ["solar", true, null],
      ["hybrid", true, null],
    ]);
  });

  it("speaks the card's language, in all five locales", () => {
    const expected: Record<string, string> = {
      en: "Cheapest",
      sv: "Billigast",
      nb: "Billigst",
      da: "Billigst",
      fi: "Halvin",
    };
    for (const [language, name] of Object.entries(expected)) {
      const model = buildModel({
        dashboard: dashboardFor("start_idle.json"),
        language: language as "en",
        nowMs: NOW_MS,
      });
      expect(model.strategy.selected, language).toBe(name);
    }
  });

  it("has no selected strategy when there is no settings record", () => {
    const model = modelFor("no_settings.json");
    expect(model.strategy.selected).toBeNull();
    expect(model.strategy.rows.every((row) => !row.available)).toBe(true);
  });
});

describe("what is under way while an action is pending", () => {
  const build = (dashboard: Dashboard, sentAction: "start" | "stop" | null, shownAutomatic: string | null) =>
    buildModel({ dashboard, language: "en", nowMs: NOW_MS, sentAction, shownAutomatic }).control;
  const charging = (name: string, value: boolean): Dashboard => {
    const dashboard = dashboardFor(name);
    return { ...dashboard, live: { ...dashboard.live, charging: value } };
  };

  it("names the command sent, else what the charger's state calls for", () => {
    expect(build(charging("action_pending.json", false), null, null).pendingAction).toBe("starting");
    expect(build(charging("action_pending.json", true), null, null).pendingAction).toBe("stopping");
    expect(build(charging("action_pending.json", true), "start", null).pendingAction).toBe("starting");
    expect(build(charging("action_pending.json", false), "stop", null).pendingAction).toBe("stopping");
  });

  it("says nothing is under way, and holds no schedule caption, when nothing is pending", () => {
    for (const name of ["start_idle.json", "stop_charging.json", "no_settings.json", "pause_clear_failed.json"]) {
      const control = build(dashboardFor(name), "start", "pause");
      expect(control.pendingAction, name).toBeNull();
      expect(control.heldAutomatic, name).toBeNull();
    }
  });

  it("holds the schedule action last shown, and only a known one", () => {
    const pending = dashboardFor("action_pending.json");
    expect(build(pending, null, "pause").heldAutomatic).toBe("pause");
    expect(build(pending, null, "resume").heldAutomatic).toBe("resume");
    expect(build(pending, null, null).heldAutomatic).toBeNull();
    expect(build(pending, null, "none").heldAutomatic).toBeNull();
  });
});
