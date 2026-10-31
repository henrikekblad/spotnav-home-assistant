// The charger's own mode that works against SpotNav: one row in "What needs attention", with a button that
// sets it back for an administrator, the place to change it for everyone else, and never a button the
// backend did not name a value for.

import { describe, expect, it } from "vitest";

import { createCardView, type CardViewInput } from "../src/card-view";
import { decodeDashboard } from "../src/validate";
import { entityWordsOf } from "../src/entity-words";
import { buildModel } from "../src/model";
import { issuesOf, statusText } from "../src/status";
import type { HassStateLike, HomeAssistantLike } from "../src/types";
import type { Status } from "../src/validate";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { decoded, rawDashboard, statusLine } from "./dashboard-fixtures";
import { mountPoint } from "./visual-fixtures";

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

/** Home Assistant's frontend as the card sees it: the Peblar select, named and translated by its integration. */
function hassWith(language: "en" | "sv"): HomeAssistantLike {
  const options: Record<string, Record<string, string>> = {
    en: { default: "Default", pure_solar: "Pure solar", comfort_solar: "Comfort solar" },
    sv: { default: "Standard", pure_solar: "Endast sol", comfort_solar: "Komfortsol" },
  };
  const smart: HassStateLike = {
    entity_id: "select.peblar_smart_charging",
    state: "pure_solar",
    attributes: { friendly_name: language === "sv" ? "Peblar charger Smart laddning" : "Peblar charger Smart charging" },
  };
  return {
    callWS: async () => ({}) as never,
    states: { [smart.entity_id]: smart },
    entities: { [smart.entity_id]: { device_id: "dev1" } },
    devices: { dev1: { name: "Peblar charger" } },
    formatEntityState: (stateObj, state) => options[language]![state ?? stateObj.state] ?? state ?? stateObj.state,
  };
}

function dialog(
  lines: Array<ReturnType<typeof statusLine>>,
  overrides: Partial<CardViewInput> = {},
  language: "en" | "sv" = "en",
  hass: HomeAssistantLike | null = null,
) {
  const model = buildModel({
    dashboard: decoded({ status: { tone: "notice", lines } }),
    language,
    nowMs: NOW,
    entityWords: entityWordsOf(hass),
  });
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

  it("turns a switch mode off rather than setting it to off", () => {
    const fup = statusLine("charger_own_mode", { entity: "switch.goe_fup", label: "PV surplus", state: "on", value: "off" });
    const { card, rows, fixes } = dialog([fup]);
    const button = rows("charger_own_mode")[0]?.querySelector<HTMLButtonElement>("button");
    expect(button?.textContent).toBe("Turn off");
    button?.click();
    expect(fixes).toEqual([["switch.goe_fup", "off"]]);
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

describe("Home Assistant's own words for the entity", () => {
  it("name the entity and its states as Home Assistant shows them, in the reader's language", () => {
    const en = dialog([PEBLAR], {}, "en", hassWith("en"));
    const row = en.rows("charger_own_mode")[0];
    expect(row?.textContent).toContain("The charger's own smart charging is on (Pure solar). It works against SpotNav.");
    expect(row?.querySelector("button")?.textContent).toBe("Set to Default");
    expect(en.fixes).toEqual([]);
    row?.querySelector<HTMLButtonElement>("button")?.click();
    // What is sent is the raw option, whatever the button says.
    expect(en.fixes).toEqual([["select.peblar_smart_charging", "default"]]);
    en.card.destroy();

    const sv = dialog([PEBLAR], {}, "sv", hassWith("sv"));
    const svRow = sv.rows("charger_own_mode")[0];
    expect(svRow?.textContent).toContain("Laddboxens egen smart laddning är på (Endast sol). Den motverkar SpotNav.");
    expect(svRow?.querySelector("button")?.textContent).toBe("Ställ in Standard");
    sv.card.destroy();
  });

  it("prefer Home Assistant's own name formatter, and keep a name's capital in German and for an abbreviation", () => {
    const hass = { ...hassWith("en"), formatEntityName: () => "PV surplus" };
    expect(entityWordsOf(hass)?.name("select.peblar_smart_charging")).toBe("PV surplus");
    const model = buildModel({
      dashboard: decoded({ status: { tone: "notice", lines: [PEBLAR] } }),
      language: "en",
      nowMs: NOW,
      entityWords: entityWordsOf(hass),
    });
    expect(model.issues[0]?.params["label"]).toBe("PV surplus");
    const de = buildModel({
      dashboard: decoded({ status: { tone: "notice", lines: [PEBLAR] } }),
      language: "de",
      nowMs: NOW,
      entityWords: entityWordsOf(hassWith("en")),
    });
    expect(de.issues[0]?.params["label"]).toBe("Smart charging");
  });

  it("fall back to the profile's label and the raw values for an entity the frontend does not know", () => {
    const { card, rows } = dialog([NO_VALUE], {}, "en", hassWith("en"));
    expect(rows("charger_own_mode")[0]?.textContent).toContain("The charger's own schedule is on (schema).");
    card.destroy();
    expect(entityWordsOf(null)).toBeUndefined();
    expect(entityWordsOf({ callWS: async () => ({}) as never })).toBeUndefined();
  });
});

describe("the attention line while a charger setting works against SpotNav", () => {
  function line(lines: Array<ReturnType<typeof statusLine>>) {
    const base = buildModel({ dashboard: decoded({ status: { tone: "notice", lines } }), language: "en", nowMs: NOW });
    const model = { ...base, setup: [{ id: "vehicle" as const, dismissible: true }] };
    const mount = mountPoint("line");
    const card = createCardView({
      model,
      mount: mount.root,
      idPrefix: "line",
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
    });
    const banner = mount.root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.banner}`);
    const text = banner?.textContent ?? null;
    const setup = banner?.dataset["banner"] === "setup";
    card.destroy();
    return { text, setup, statusSaysIt: model.status?.includes("works against SpotNav") ?? false };
  }

  it("names the notice, not Get started, even though the status line says it too", () => {
    const own = line([PEBLAR]);
    expect(own.statusSaysIt).toBe(true);
    expect(own.setup).toBe(false);
    expect(own.text).toBe("Good to know.1 item to review");
    const disabled = line([DISABLED]);
    expect(disabled.setup).toBe(false);
    expect(disabled.text).toContain("1 item to review");
  });

  it("still leaves Get started in place for a notice the status line already says and nothing to act on", () => {
    const held = line([statusLine("held_by_charger")]);
    expect(held.setup).toBe(true);
  });
});
