// The charger's own mode that works against SpotNav: one row in "What needs attention", with a button that
// sets it back for an administrator, the place to change it for everyone else, and never a button the
// backend did not name a value for.

import { describe, expect, it } from "vitest";

import { createCardView, type CardViewInput } from "../src/card-view";
import { decodeDashboard } from "../src/validate";
import { issuesOf, statusText } from "../src/status";
import type { Status } from "../src/validate";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { rawDashboard, statusLine } from "./dashboard-fixtures";
import { modelWith, mountPoint } from "./visual-fixtures";

const NOW = Date.parse("2026-09-22T08:00:00Z");
const PEBLAR = statusLine("charger_own_mode", {
  entity: "select.peblar_smart_charging",
  label: "smart charging",
  state: "pure_solar",
  value: "default",
});
const NO_VALUE = statusLine("charger_own_mode", {
  entity: "select.garo_mode",
  label: "schedule",
  state: "schema",
  value: null,
});
const DISABLED = statusLine("charger_disabled", { entity: "switch.easee_is_enabled" });

function dialog(lines: Array<ReturnType<typeof statusLine>>, overrides: Partial<CardViewInput> = {}, language = "en") {
  const model = { ...modelWith({ status: { tone: "notice", lines } }), language } as ReturnType<typeof modelWith>;
  const mount = mountPoint("own");
  const fixes: Array<[string, string]> = [];
  const card = createCardView({
    model,
    mount: mount.root,
    idPrefix: "own",
    now: () => NOW,
    measure: () => ({ width: 320, height: 150 }),
    observe: () => null,
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
    isAdmin: true,
    onSelectStrategy: () => {},
    onFixOwnMode: (entity, value) => fixes.push([entity, value]),
    ...overrides,
  });
  card.openIssues();
  const rows = (code: string) =>
    Array.from(mount.root.querySelectorAll<HTMLElement>(`.${VISUAL_CLASSES.issueItem}`)).filter(
      (row) => row.dataset["code"] === code,
    );
  return { card, root: mount.root, rows, fixes };
}

describe("a charger's own mode that works against SpotNav", () => {
  it("is decoded with its facts, and refused with a param the card does not know", () => {
    expect(decodeDashboard(rawDashboard({ status: { tone: "notice", lines: [PEBLAR, DISABLED] } })).ok).toBe(true);
    const extra = { ...PEBLAR, params: { ...PEBLAR.params, other: "x" } };
    expect(decodeDashboard(rawDashboard({ status: { tone: "notice", lines: [extra] } })).ok).toBe(false);
  });

  it("is worded in the status line and as an issue, in English and Swedish", () => {
    const status = { tone: "notice", lines: [PEBLAR] } as unknown as Status;
    const format = (language: "en" | "sv") =>
      ({ language, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" }) as const;
    expect(statusText(status, format("en"), NOW)).toBe(
      "The charger's own smart charging is on (pure_solar). It works against SpotNav.",
    );
    expect(statusText(status, format("sv"), NOW)).toBe(
      "Laddboxens egen smart charging är på (pure_solar). Den motverkar SpotNav.",
    );
    const [issue] = issuesOf(status, "en");
    expect(issue?.fix).toEqual({
      entity: "select.peblar_smart_charging",
      value: "default",
      labelKey: "issue.ownMode.set",
      labelParams: { value: "default" },
    });
  });

  it("has a button for an administrator that sends exactly the entity and value the backend named", () => {
    const { card, rows, fixes } = dialog([PEBLAR]);
    const [row] = rows("charger_own_mode");
    const button = row?.querySelector<HTMLButtonElement>("button");
    expect(button?.textContent).toBe("Set to default");
    expect(row?.textContent).not.toContain("device page");
    button?.click();
    expect(fixes).toEqual([["select.peblar_smart_charging", "default"]]);
    expect(card.dialogOpen("issues")).toBe(false);
    card.destroy();
  });

  it("words the button in Swedish", () => {
    const { card, rows } = dialog([PEBLAR], {}, "sv");
    expect(rows("charger_own_mode")[0]?.querySelector("button")?.textContent).toBe("Ställ in default");
    card.destroy();
  });

  it("has no button for a reader who is not an administrator, only where to change it", () => {
    const { card, rows, fixes } = dialog([PEBLAR, DISABLED], { isAdmin: false });
    const [row] = rows("charger_own_mode");
    expect(row?.querySelector("button")).toBeNull();
    expect(row?.textContent).toContain("Change it on the charger's device page in Home Assistant.");
    expect(rows("charger_disabled")[0]?.querySelector("button")).toBeNull();
    expect(fixes).toEqual([]);
    card.destroy();
  });

  it("has no button where the backend named no single safe value", () => {
    const { card, rows } = dialog([NO_VALUE]);
    const [row] = rows("charger_own_mode");
    expect(row?.querySelector("button")).toBeNull();
    expect(row?.textContent).toBe(
      "The charger's own schedule is on (schema). It works against SpotNav. Change it on the charger's device page in Home Assistant.",
    );
    card.destroy();
  });

  it("turns a disabled charger's own enable switch on, from the same row that says it is off", () => {
    const { card, rows, fixes } = dialog([DISABLED]);
    const button = rows("charger_disabled")[0]?.querySelector<HTMLButtonElement>("button");
    expect(button?.textContent).toBe("Turn on");
    button?.click();
    expect(fixes).toEqual([["switch.easee_is_enabled", "on"]]);
    card.destroy();
  });

  it("has no button on a disabled charger whose switch the backend did not name", () => {
    const { card, rows } = dialog([statusLine("charger_disabled")]);
    expect(rows("charger_disabled")[0]?.querySelector("button")).toBeNull();
    card.destroy();
  });
});
