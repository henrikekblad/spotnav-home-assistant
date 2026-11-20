// Characterisation of `createCardView`: the serialized DOM it builds, for a broad set of models, viewers and
// opened dialogs. It was recorded from the function as it stood before it was split by section, and must not
// change when the function is moved around. Each case is kept as a short hash; set `CARD_VIEW_DUMP` to a
// directory to write the full markup of every case there for a diff.

import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { createCardView, type CardView, type CardViewInput } from "../src/card-view";
import type { Language } from "../src/i18n";
import { LANGUAGES } from "../src/i18n";
import { buildModel, type CardModel } from "../src/model";
import { decodeDashboard } from "../src/validate";
import { decoded, rawDashboard, statusLine } from "./dashboard-fixtures";
import {
  autumnModel,
  baseModel,
  externalModel,
  midnightModel,
  modelWith,
  mountPoint,
  quarterModel,
  twoDayModel,
  twoDayPlanModel,
  waitingProposalModel,
} from "./visual-fixtures";

const DASHBOARD_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const NOW = Date.parse("2026-09-22T06:00:00+00:00");
const HASHES = join(__dirname, "card-view-snapshot.json");

function fromFixture(name: string, language: Language = "en"): CardModel {
  const result = decodeDashboard(JSON.parse(readFileSync(join(DASHBOARD_DIR, name), "utf8")));
  if (!result.ok) {
    throw new Error(`${name} did not decode: ${result.failure}`);
  }
  return buildModel({ dashboard: result.value, language, nowMs: NOW });
}

function build(model: CardModel, isAdmin: boolean, overrides: Partial<CardViewInput> = {}): { card: CardView; root: ShadowRoot } {
  const mount = mountPoint("snap");
  const card = createCardView({
    model,
    mount: mount.root,
    idPrefix: "snap",
    now: () => Date.parse("2026-09-22T04:30:00Z"),
    measure: () => ({ width: 320, height: 150 }),
    observe: () => null,
    observeBox: () => null,
    observeHeader: () => null,
    onAction: () => {},
    onOpenSettings: () => {},
    onSaveSettings: () => {},
    onReloadSettings: () => {},
    onReapplySettings: () => {},
    onOpenMarket: () => {},
    onSaveMarket: () => {},
    onReloadMarket: () => {},
    onReapplyMarket: () => {},
    onMarketAreaChange: () => {},
    isAdmin,
    onSelectStrategy: () => {},
    ...overrides,
  });
  return { card, root: mount.root };
}

type Step = (card: CardView) => void;

const DIALOGS: Array<[string, Step]> = [
  ["issues", (card) => card.openIssues()],
  ["capabilities", (card) => card.openCapabilities()],
  ["pause", (card) => card.openPause()],
  ["strategy", (card) => card.openStrategy()],
  ["settings", (card) => card.openSettingsOverview()],
  ["history", (card) => card.openHistory()],
  ["plan-editor", (card) => card.openSettingsEditor("plan")],
  ["market-editor", (card) => card.openMarketEditor()],
  [
    "failures",
    (card) => {
      card.setActionError({ sentenceKey: "action.error.confirmationFailed", code: "boom" });
      card.setSettingsError({ sentenceKey: "settings.error.confirmationFailed", code: "boom" });
      card.setActionPending(true, "start", null);
    },
  ],
  [
    "entities",
    (card) => {
      card.setEntityState({ kind: "failed", failure: { sentenceKey: "settings.error.confirmationFailed", code: "x" } });
      card.setHomeOffered(true);
      card.setCardOutdated(true);
      card.openSettingsOverview();
    },
  ],
];

const cases: Array<[string, () => string]> = [];

function snapshot(name: string, model: () => CardModel, isAdmin: boolean, steps: Step[] = [], overrides: Partial<CardViewInput> = {}): void {
  cases.push([
    name,
    () => {
      const { card, root } = build(model(), isAdmin, overrides);
      for (const step of steps) {
        step(card);
      }
      const html = root.innerHTML;
      card.destroy();
      return html;
    },
  ]);
}

// Every dashboard the backend pins, as an administrator and as a reader, opened and not.
const fixtures = readdirSync(DASHBOARD_DIR)
  .filter((name) => name.endsWith(".json"))
  .sort();
for (const name of fixtures) {
  snapshot(`${name} admin`, () => fromFixture(name), true);
  snapshot(`${name} reader`, () => fromFixture(name), false);
  snapshot(`${name} admin settings`, () => fromFixture(name), true, [(card) => card.openSettingsOverview()]);
  snapshot(`${name} reader issues+strategy`, () => fromFixture(name), false, [(card) => card.openIssues(), (card) => card.openStrategy()]);
}

// The visual models.
const models: Array<[string, () => CardModel]> = [
  ["base", () => baseModel()],
  ["external", () => externalModel()],
  ["quarter", () => quarterModel(() => ({ proposal_planned: true }))],
  ["two-day", () => twoDayModel()],
  ["two-day-plan", () => twoDayPlanModel()],
  ["midnight", () => midnightModel()],
  ["autumn", () => autumnModel()],
  ["waiting", () => waitingProposalModel()],
  ["waiting-pending", () => waitingProposalModel({ pending: true })],
  ["no-market", () => modelWith({ market: null })],
  ["no-plan", () => modelWith({ status: { tone: "normal", lines: [statusLine("no_plan")] } })],
  [
    "notice",
    () => modelWith({ status: { tone: "notice", lines: [statusLine("charging_without_prices")] } }),
  ],
  [
    "execution-error",
    () => modelWith({ control: { ...(rawDashboard().control as Record<string, unknown>), execution_error: "pause_clear_failed" } }),
  ],
];
for (const [name, model] of models) {
  snapshot(`${name} admin`, model, true);
  snapshot(`${name} reader`, model, false);
  snapshot(`${name} collapsed`, model, true, [], { chartCollapsed: true, chartDetail: true });
  for (const [dialog, step] of DIALOGS) {
    snapshot(`${name} admin ${dialog}`, model, true, [step]);
  }
}

// Every language.
for (const language of LANGUAGES) {
  snapshot(`language ${language}`, () => buildModel({ dashboard: decoded(), language, nowMs: NOW }), true);
  snapshot(`language ${language} settings`, () => buildModel({ dashboard: decoded(), language, nowMs: NOW }), true, [
    (card) => card.openSettingsOverview(),
    (card) => card.openStrategy(),
  ]);
}

describe("the card view's serialized DOM", () => {
  it("is the same for every recorded case", () => {
    const record = process.env["CARD_VIEW_RECORD"] === "1";
    const dump = process.env["CARD_VIEW_DUMP"];
    if (dump !== undefined) {
      mkdirSync(dump, { recursive: true });
    }
    const seen: Record<string, string> = {};
    for (const [name, run] of cases) {
      const html = run();
      seen[name] = createHash("sha256").update(html).digest("hex").slice(0, 16);
      if (dump !== undefined) {
        writeFileSync(join(dump, `${name.replace(/[^a-z0-9.+-]+/gi, "_")}.html`), html.replace(/></g, ">\n<"));
      }
    }
    if (record) {
      writeFileSync(HASHES, `${JSON.stringify(seen, null, 1)}\n`);
    }
    const expected = JSON.parse(readFileSync(HASHES, "utf8")) as Record<string, string>;
    expect(Object.keys(seen)).toEqual(Object.keys(expected));
    expect(seen).toEqual(expected);
    expect(cases.length).toBeGreaterThan(290);
  }, 120_000);
});
