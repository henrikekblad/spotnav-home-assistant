// The immutable view model: capabilities, figures and periods. (The status line and the issue list
// are `status.test.ts`: the card only words Home Assistant's status block.)

import { describe, expect, it } from "vitest";

import type { Language } from "../src/i18n";
import { buildModel, capabilitiesFor, planRelationOf } from "../src/model";
import { decodeDashboard, type Dashboard } from "../src/validate";
import { bare, quarterHourRows, rawDashboard } from "./dashboard-fixtures";
import { MIDNIGHT_MS, NEXT_DAY, SEPT, midnightModel } from "./visual-fixtures";

const NOW = Date.parse("2026-09-22T04:30:00Z");

function decoded(overrides: Record<string, unknown> = {}): Dashboard {
  const result = decodeDashboard(rawDashboard(overrides));
  if (!result.ok) {
    throw new Error(`fixture did not decode: ${result.failure}`);
  }
  return result.value;
}

function model(overrides: Record<string, unknown> = {}, language: Language = "en", nowMs = NOW) {
  return buildModel({ dashboard: decoded(overrides), language, nowMs });
}

function patchSection(section: string, patch: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
  const base = rawDashboard();
  return { ...overrides, [section]: { ...(base[section] as Record<string, unknown>), ...patch } };
}

function without(section: string): Record<string, unknown> {
  return rawDashboard({ [section]: null });
}

describe("capabilities and figures", () => {
  it("maps the capability booleans (explicit schedule execution is retired and not listed), with target SoC unavailable until a source and a battery size exist", () => {
    const items = capabilitiesFor(decoded());
    expect(items.map((item) => item.key)).toEqual([
      "auto_price",
      "current_limit",
      "load_balancing",
      "target_soc",
    ]);
    expect(items.find((item) => item.key === "auto_price")?.state).toBe("available");
    expect(items.find((item) => item.key === "load_balancing")?.state).toBe("unavailable");
    expect(items.find((item) => item.key === "target_soc")?.state).toBe("unavailable");
  });

  it("lists the dynamic current limit as available only when SpotNav can set the current, not when a number merely exists", () => {
    const capabilitiesWith = (changes: Record<string, boolean>) => {
      const base = rawDashboard();
      const charger = base.charger as { capabilities: Record<string, boolean> };
      const raw = patchSection("charger", { capabilities: { ...charger.capabilities, ...changes } });
      const result = decodeDashboard(rawDashboard(raw));
      if (!result.ok) {
        throw new Error("did not decode");
      }
      return capabilitiesFor(result.value).find((item) => item.key === "current_limit")?.state;
    };
    expect(capabilitiesWith({ current_limit: true, set_current: false })).toBe("unavailable");
    expect(capabilitiesWith({ current_limit: true, set_current: true })).toBe("available");
  });

  it("takes figures from the proposal only, and never borrows from the installed schedule", () => {
    const withProposal = model().figures;
    expect(withProposal.energy).toBe("20 kWh");
    expect(withProposal.cost).toBe("22.5 kr");
    expect(withProposal.distance).toBe("8.5 mil");
    const base = rawDashboard();
    const withoutProposal = model({ plan: { ...(base.plan as object), proposal: null } }).figures;
    expect(withoutProposal).toEqual({ cost: null, energy: null, distance: null, power: null });
  });

  it("takes the summary's Max, Min and Now from the day the clock is in, across local midnight", () => {
    // The capture holds the date the clock has left and the date it is in. The expired day's own single
    // price must not become the day's Max/Min, and the interval that really covers `now` is the price
    // the card calls Now -- this is the observed production defect, at the model's own boundary.
    const rolled = midnightModel();
    expect(rolled.summary.max).toBe("500 öre/kWh");
    expect(rolled.summary.min).toBe("200 öre/kWh");
    expect(rolled.summary.current).toBe("200 öre/kWh");
    expect(rolled.summary.current).not.toBe("unknown");
    expect(rolled.chart.current?.wallClock).toBe("00:00");
    expect(rolled.chart.current?.dayKey).toBe(NEXT_DAY);
    // The two dates keep their own means and their own roles: the new one is today's series and is not
    // drawn as the subdued one, while the date the clock has left is.
    expect(rolled.chart.days.map((day) => day.role)).toEqual(["past", "today"]);
    expect(rolled.chart.marks.filter((mark) => mark.dayKey === NEXT_DAY).every((mark) => !mark.tomorrow)).toBe(true);
    // The same capture read five minutes before midnight: the old day's own last quarter is then the
    // current interval and its price is the day's Max/Min too -- roles follow the clock both ways.
    const before = midnightModel(MIDNIGHT_MS - 5 * 60_000);
    expect(before.chart.days.map((day) => day.role)).toEqual(["today", "future"]);
    expect(before.chart.current?.dayKey).toBe(SEPT);
    expect(before.summary.current).toBe("100 öre/kWh");
    expect(before.summary.max).toBe("100 öre/kWh");
    // And where no accepted day is the day the clock is in, Max/Min are honestly unavailable rather
    // than borrowed from a coming day -- omitted (`null`), never shown as the word "unknown" (visual
    // fix a: the overlay hides the whole line for a fact nobody has).
    const later = midnightModel(MIDNIGHT_MS + 26 * 60 * 60_000);
    expect(later.summary.max).toBeNull();
    expect(later.summary.min).toBeNull();
    expect(later.summary.current).toBeNull();
  });

  it("keeps a zero cost and a negative price as real values", () => {
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    const proposal = plan.proposal as Record<string, unknown>;
    const zero = model({ plan: { ...plan, proposal: { ...proposal, cost: { value: 0, currency: "SEK" } } } });
    expect(zero.figures.cost).toBe("0 kr");
    const negative = model(patchSection("prices", { intervals: quarterHourRows("2026-09-22T04:00:00Z", [-42]) }));
    expect(negative.chart.marks[0]?.price).toBe(-42);
    expect(negative.summary.min).toBe("-42 öre/kWh");
  });
});

describe("periods", () => {
  it("labels installed periods and marks the backend's own active one", () => {
    const result = model();
    expect(result.installedPeriods).toHaveLength(2);
    expect(result.installedPeriods[0]?.activeNow).toBe(true);
    expect(result.installedPeriods[1]?.activeNow).toBe(false);
    expect(result.installedPeriods[0]?.label).toContain("06:00");
  });

  it("keeps the installed periods visible with no proposal to borrow from", () => {
    const base = rawDashboard();
    const result = model({
      plan: { ...(base.plan as object), proposal: null },
    });
    expect(result.proposalPeriods).toEqual([]);
    expect(result.installedPeriods).toHaveLength(2);
    expect(result.figures.cost).toBeNull();
  });
});

describe("the model is immutable and honest", () => {
  it("does not mutate the decoded dashboard it was given", () => {
    const dashboard = decoded();
    const snapshot = JSON.stringify(dashboard);
    buildModel({ dashboard, language: "en", nowMs: NOW });
    expect(JSON.stringify(dashboard)).toBe(snapshot);
  });

  it("is identical for identical inputs", () => {
    expect(JSON.stringify(model())).toBe(JSON.stringify(model()));
  });

  it("answers in another language from the same decoded facts", () => {
    const english = model();
    const swedish = model({}, "sv");
    expect(swedish.status).toBe("Planerat från 06:00 · 20 kWh · 22,5 kr · 8,5 mil");
    expect(swedish.chart.marks.length).toBe(english.chart.marks.length);
    expect(swedish.figures.energy).toBe(english.figures.energy);
  });

  it("reports no times when the market names no zone, instead of pretending", () => {
    const result = model(without("market"));
    expect(result.timesAvailable).toBe(false);
    expect(result.installedPeriods[0]?.label).toBe("");
    expect(result.summary.max).toBeNull();
  });

  it("draws a retained installed schedule beside a refused calculation, and says why", () => {
    // The field case, at the model boundary: an installed external schedule is still what the charger
    // does, the failed Auto calculation proposes nothing, and the card neither hides the installed
    // bands nor implies they are the Auto proposal.
    const plan = {
      proposal: null,
      installed: {
        identity: null,
        origin: "external",
        periods: [
          { start: "2026-09-22T04:00:00+00:00", end: "2026-09-22T05:00:00+00:00" },
          { start: "2026-09-22T05:00:00+00:00", end: "2026-09-22T05:15:00+00:00" },
        ],
        amps: 10,
        phases: 3,
        power_kw: 2.3,
        active_period_index: 0,
      },
      relation: { applied: false, applied_identity: null, pending_identity: null },
      delivered_kwh: null,
      remaining_kwh: null,
    };
    const section = patchSection(
      "planning",
      { state: "planning_unavailable", reason: "insufficient_price_horizon" },
      { plan },
    );
    const built = model(section);

    expect(built.installedPeriods).toHaveLength(2);
    expect(built.installedPeriods[0]?.label).not.toBe("");
    expect(built.proposalPeriods).toHaveLength(0);
    expect(built.planRelation).toBe("installed_only");
  });

});

describe("the plan relation", () => {
  it("is explicit, from `plan.relation.applied` and real section presence", () => {
    expect(decoded().plan.relation.applied).toBe(true);
    expect(model().planRelation).toBe("applied_same");
    expect(planRelationOf(decoded())).toBe("applied_same");

    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    const relation = plan.relation as Record<string, unknown>;

    // Both sections, an unapplied proposal: two blocks, installed first.
    expect(model({ plan: { ...plan, relation: { ...relation, applied: false } } }).planRelation).toBe(
      "pending_beside_installed",
    );
    // Both sections and no snapshot at all cannot claim the proposal is what is installed.
    expect(
      model({ plan: { ...plan, relation: { ...relation, applied: null } } }).planRelation,
    ).toBe("pending_beside_installed");
    // A proposal alone, a schedule alone, and neither.
    expect(model({ plan: { ...plan, installed: null } }).planRelation).toBe("proposal_only");
    expect(model({ plan: { ...plan, proposal: null } }).planRelation).toBe("installed_only");
    expect(model({ plan: { ...plan, proposal: null, installed: null } }).planRelation).toBe("none");
    // An empty section is an absent one, whatever the object says.
    expect(
      model({
        plan: { ...plan, installed: { ...(plan.installed as Record<string, unknown>), periods: [] } },
      }).planRelation,
    ).toBe("proposal_only");
  });

  it("reads the external default as an installed-only or empty relation, never as a proposal", () => {
    const model = buildModel({ dashboard: bare(), language: "en", nowMs: NOW });
    expect(["installed_only", "none"]).toContain(model.planRelation);
    expect(model.proposalPeriods).toEqual([]);
  });
});
