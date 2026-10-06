// The area/fiscal dialog, as the DOM a reader actually gets.
//
// The body builder makes no request and holds no state: every assertion here is about the controls it
// returns -- their labels, their descriptions, which of them are enabled, and what `values()` reports
// once a reader has changed them. Nothing is asserted about styling; the classes are checked in
// `visual-styles.test.ts`.

import { describe, expect, it, vi } from "vitest";

import {
  marketEditorBody,
  type MarketEditorForm,
  type MarketEditorHandlers,
} from "../src/market-editor";
import { fiscalFormFrom, type MarketFormValues } from "../src/market";
import { VISUAL_CLASSES as C } from "../src/visual-styles";
import type { AreaOverride, MarketOptionsV1, SettingsRecord } from "../src/types";

function options(overrides: Partial<MarketOptionsV1> = {}): MarketOptionsV1 {
  return {
    state: "ready",
    reason: null,
    configured_area: "SE4",
    areas: [
      {
        area_id: "SE4",
        name: "Malmö",
        countries: ["SE"],
        timezone: "Europe/Stockholm",
        currency: "SEK",
        major_unit: "kr",
        minor_unit: "öre",
        suggestions: { vat_percent: 25, tax_minor: 36.5, transfer_minor: null },
      },
      {
        area_id: "DE-LU",
        name: "Germany-Luxembourg",
        countries: ["DE", "LU"],
        timezone: "Europe/Berlin",
        currency: "EUR",
        major_unit: "€",
        minor_unit: "cent",
        suggestions: { vat_percent: null, tax_minor: null, transfer_minor: null },
      },
    ],
    ...overrides,
  };
}

function form(overrides: Partial<MarketEditorForm> = {}): MarketEditorForm {
  return {
    readOnly: false,
    options: options(),
    values: {
      areaId: "SE4",
      // One of each: the person's own figure, an off component retaining one, and an off component
      // that is still the catalogue's.
      vat: { enabled: true, value: "25", intent: "custom" },
      tax: { enabled: false, value: "36.5", intent: "custom" },
      transfer: { enabled: false, value: "", intent: "suggested" },
    },
    conflict: null,
    ...overrides,
  };
}

function handlers(overrides: Partial<MarketEditorHandlers> = {}): MarketEditorHandlers {
  return {
    onSave: vi.fn(),
    onReload: vi.fn(),
    onCancel: vi.fn(),
    onReapply: vi.fn(),
    onAreaChange: vi.fn(),
    ...overrides,
  };
}

function build(current: MarketEditorForm, hooks: MarketEditorHandlers = handlers(), region: string | null = null) {
  const built = marketEditorBody(document, "en", current, hooks, "card-a", region);
  document.body.replaceChildren(built.body);
  return built;
}

/** One published area, or a refusal: an index would be a claim this test cannot make. */
function areaById(id: string): MarketOptionsV1["areas"][number] {
  const found = options().areas.find((area) => area.area_id === id);
  if (found === undefined) {
    throw new Error(`no ${id} in the fixture`);
  }
  return found;
}

/** One component's checkbox. */
function checkbox(body: HTMLElement, component: string): HTMLInputElement {
  const found = body.querySelector<HTMLInputElement>(`#card-a-${component}-enabled`);
  if (found === null) {
    throw new Error(`no ${component} checkbox`);
  }
  return found;
}

/** One component's figure field. */
function figure(body: HTMLElement, component: string): HTMLInputElement {
  const found = body.querySelector<HTMLInputElement>(`#card-a-${component}-value`);
  if (found === null) {
    throw new Error(`no ${component} number field`);
  }
  return found;
}

/** One component's own block: where its suggestion line and its reset live. */
function block(body: HTMLElement, component: string): HTMLElement {
  const found = figure(body, component).closest<HTMLElement>(`.${C.marketComponent}`);
  if (found === null) {
    throw new Error(`no ${component} row`);
  }
  return found;
}

function reset(body: HTMLElement, component: string): HTMLButtonElement | null {
  return block(body, component).querySelector<HTMLButtonElement>(`.${C.marketReset}`);
}

/** Typing: the act that makes a figure the person's own, whatever the digits happen to equal. */
function type(body: HTMLElement, component: string, text: string): void {
  const field = figure(body, component);
  field.value = text;
  field.dispatchEvent(new Event("input", { bubbles: true }));
}

/** The checkbox, as a person toggles it. */
function toggle(body: HTMLElement, component: string, enabled: boolean): void {
  const box = checkbox(body, component);
  box.checked = enabled;
  box.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("the compact fiscal row", () => {
  it("has one checkbox and one number field per component, and no radio group anywhere", () => {
    const body = build(form()).body;
    expect(body.querySelectorAll("input[type='radio']")).toHaveLength(0);
    expect(body.querySelectorAll("fieldset")).toHaveLength(0);
    expect(body.querySelectorAll("legend")).toHaveLength(0);
    for (const component of ["vat", "tax", "transfer"] as const) {
      expect(body.querySelectorAll(`#card-a-${component}-enabled`)).toHaveLength(1);
      expect(body.querySelectorAll(`#card-a-${component}-value`)).toHaveLength(1);
      expect(checkbox(body, component).type).toBe("checkbox");
      expect(figure(body, component).type).toBe("number");
      expect(figure(body, component).step).toBe("any");
    }
  });

  it("labels the checkbox with the component's own name, and describes both controls", () => {
    const body = build(form()).body;
    const box = checkbox(body, "vat");
    expect(box.getAttribute("aria-label")).toBe("VAT: enabled");
    expect(body.querySelector<HTMLLabelElement>(`label[for="${box.id}"]`)?.textContent).toBe("VAT");
    expect(box.getAttribute("aria-describedby")).toContain("-vat-suggestion");
    expect(box.getAttribute("aria-describedby")).toContain("-vat-description");
    const field = figure(body, "vat");
    expect(field.getAttribute("aria-label")).toBe("VAT: my own value");
    expect(field.getAttribute("aria-describedby")).toContain("-vat-description");
    // One description per component, and one suggestion line: nothing anonymous is left in the row.
    expect(body.querySelector("#card-a-vat-description")?.textContent).toBe("Value added tax, as a percentage.");
    expect(body.querySelector("#card-a-vat-suggestion")?.textContent).toContain("Suggested: 25 %");
  });

  it("shows the catalogue's figure while a component is still the suggestion, and states `null`", () => {
    const suggested = build(
      form({
        values: {
          areaId: "SE4",
          vat: { enabled: true, value: "", intent: "suggested" },
          tax: { enabled: true, value: "", intent: "suggested" },
          transfer: { enabled: false, value: "", intent: "suggested" },
        },
      }),
    );
    // The field is *filled in* with what the catalogue says, which is what makes the row readable.
    expect(figure(suggested.body, "vat").value).toBe("25");
    expect(figure(suggested.body, "tax").value).toBe("36.5");
    // SE4 publishes no grid transfer: unchecked and following the catalogue shows nothing at all.
    expect(checkbox(suggested.body, "transfer").checked).toBe(false);
    expect(figure(suggested.body, "transfer").value).toBe("");
    // And what the row *means* is the suggestion, with no figure of its own -- the displayed text is
    // never mistaken for one, whatever the catalogue happens to say.
    expect(suggested.values()).toEqual({
      areaId: "SE4",
      vat: { enabled: true, value: "", intent: "suggested" },
      tax: { enabled: true, value: "", intent: "suggested" },
      transfer: { enabled: false, value: "", intent: "suggested" },
    });
  });

  it("shows a stored figure exactly, whether or not its component is on", () => {
    const body = build(form()).body;
    expect(figure(body, "vat").value).toBe("25");
    expect(checkbox(body, "vat").checked).toBe(true);
    // Off, and the figure it is retaining is still what the row shows: nothing is discarded.
    expect(figure(body, "tax").value).toBe("36.5");
    expect(checkbox(body, "tax").checked).toBe(false);
  });

  it("turns editing into the person's own figure, even when it equals the suggestion", () => {
    const built = build(
      form({
        values: {
          areaId: "SE4",
          vat: { enabled: true, value: "", intent: "suggested" },
          tax: { enabled: false, value: "", intent: "suggested" },
          transfer: { enabled: false, value: "", intent: "suggested" },
        },
      }),
    );
    // Exactly the suggestion's own digits, typed on purpose: the intent follows the *act*, not the
    // value, so a later catalogue change cannot move a number the person stated themselves.
    type(built.body, "vat", "25");
    expect(built.values().vat).toEqual({ enabled: true, value: "25", intent: "custom" });
    // Typing while unchecked is the same act: the figure becomes theirs and stays theirs.
    type(built.body, "tax", "36.5");
    expect(built.values().tax).toEqual({ enabled: false, value: "36.5", intent: "custom" });
  });

  it("offers the reset only while there is a suggestion to go back to", () => {
    const built = build(
      form({
        values: {
          areaId: "SE4",
          vat: { enabled: true, value: "", intent: "suggested" },
          tax: { enabled: false, value: "", intent: "suggested" },
          transfer: { enabled: false, value: "", intent: "suggested" },
        },
      }),
    );
    // Following the suggestion: nothing to reset to.
    expect(reset(built.body, "vat")?.hidden).toBe(true);
    type(built.body, "vat", "9.5");
    expect(reset(built.body, "vat")?.hidden).toBe(false);
    expect(reset(built.body, "vat")?.textContent).toBe("Reset to suggestion");
    reset(built.body, "vat")?.click();
    // Visually the catalogue's figure again, and internally the suggestion rather than a copy of it.
    expect(figure(built.body, "vat").value).toBe("25");
    expect(built.values().vat).toEqual({ enabled: true, value: "", intent: "suggested" });
    expect(reset(built.body, "vat")?.hidden).toBe(true);

    // Where the area publishes nothing there is nothing to reset to, however the row is stated.
    const unlisted = build(
      form({
        options: options({ areas: [options().areas[1] as MarketOptionsV1["areas"][number]], configured_area: "DE-LU" }),
        values: {
          areaId: "DE-LU",
          vat: { enabled: true, value: "5", intent: "custom" },
          tax: { enabled: false, value: "", intent: "suggested" },
          transfer: { enabled: false, value: "", intent: "suggested" },
        },
      }),
    );
    expect(reset(unlisted.body, "vat")?.hidden).toBe(true);
  });

  it("keeps a custom figure across off and on again", () => {
    const built = build(form());
    type(built.body, "vat", "12.5");
    toggle(built.body, "vat", false);
    expect(built.values().vat).toEqual({ enabled: false, value: "12.5", intent: "custom" });
    expect(figure(built.body, "vat").value).toBe("12.5");
    toggle(built.body, "vat", true);
    expect(built.values().vat).toEqual({ enabled: true, value: "12.5", intent: "custom" });
  });

  it("states the unit as a percent for VAT and the area's own minor unit otherwise", () => {
    const body = build(form()).body;
    const units = Array.from(body.querySelectorAll(`.${C.settingsUnit}`)).map((node) => node.textContent);
    expect(units).toEqual(["%", "öre", "öre"]);
  });

  it("lists the published areas in the relay's order, and keeps the stored one selectable", () => {
    const listed = build(form()).body;
    const select = listed.querySelector<HTMLSelectElement>("select");
    expect(Array.from(select?.options ?? []).map((option) => option.value)).toEqual(["SE4", "DE-LU"]);
    expect(select?.value).toBe("SE4");
    expect(Array.from(select?.options ?? []).map((option) => option.textContent)).toEqual([
      "Malmö · SE4",
      "Germany-Luxembourg · DE-LU",
    ]);

    const stale = options({
      areas: [areaById("DE-LU")],
      configured_area: "SE4",
      state: "stale",
      reason: "offline",
    });
    const unlisted = build(form({ options: stale }));
    expect(Array.from(unlisted.body.querySelectorAll("option")).map((option) => option.textContent)).toEqual([
      "Germany-Luxembourg · DE-LU",
      "SE4 · no longer published",
    ]);
    expect(unlisted.body.querySelector(`.${C.marketState}`)?.textContent).toBe(
      "This is the last area list Home Assistant received; the relay could not be reached since.",
    );
  });

  it("groups the areas by country with optgroups, the same option values as before", () => {
    const flat = Array.from(build(form()).body.querySelectorAll("option")).map((option) => option.value);
    const grouped = build(form(), handlers(), "DE");
    const select = grouped.body.querySelector<HTMLSelectElement>("select");
    const groups = Array.from(select?.querySelectorAll("optgroup") ?? []);
    expect(groups.map((group) => group.label)).toEqual(["Germany", "Sweden"]);
    expect(groups.map((group) => Array.from(group.querySelectorAll("option")).map((option) => option.value))).toEqual([
      ["DE-LU"],
      ["SE4"],
    ]);
    expect(Array.from(select?.options ?? []).map((option) => option.value).sort()).toEqual([...flat].sort());
    expect(select?.value).toBe("SE4");
    expect(select?.disabled).toBe(false);
    // Without a region the catalogue's own order stands.
    const plain = build(form()).body.querySelectorAll("optgroup");
    expect(Array.from(plain).map((group) => group.label)).toEqual(["Sweden", "Germany"]);
  });

  it("lists a stored area the catalogue no longer publishes after the groups, outside any group", () => {
    const stale = options({ areas: [areaById("DE-LU")], configured_area: "SE4", state: "stale", reason: "offline" });
    const select = build(form({ options: stale })).body.querySelector<HTMLSelectElement>("select");
    expect(Array.from(select?.children ?? []).map((node) => node.tagName)).toEqual(["OPTGROUP", "OPTION"]);
    expect(select?.value).toBe("SE4");
  });

  it("begins without an area when none is stored, and offers no Save until one is chosen", () => {
    const none: MarketFormValues = {
      areaId: null,
      vat: { enabled: false, value: "", intent: "suggested" },
      tax: { enabled: false, value: "", intent: "suggested" },
      transfer: { enabled: false, value: "", intent: "suggested" },
    };
    const built = build(form({ values: none }));
    const select = built.body.querySelector<HTMLSelectElement>("select");
    expect(select?.value).toBe("");
    expect(built.body.querySelector(`.${C.settingsSave}`)).toBeNull();
    expect(built.values().areaId).toBeNull();

    const empty = build(
      form({
        options: options({ areas: [], configured_area: null, state: "unavailable", reason: "offline" }),
        values: none,
      }),
    );
    expect(empty.body.querySelector("#card-a-area-description")?.textContent).toBe(
      "No areas are published right now.",
    );
    expect(empty.body.querySelector(`.${C.settingsSave}`)).toBeNull();
  });

  it("reports a Save, a Reapply and a Reload, and never an ordinary Save while conflicted", () => {
    const hooks = handlers();
    const built = build(form(), hooks);
    const save = built.body.querySelector<HTMLButtonElement>(`.${C.settingsSave}`);
    expect(save?.textContent).toBe("Save");
    save?.click();
    expect(hooks.onSave).toHaveBeenCalledTimes(1);
    expect((hooks.onSave as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]).toEqual(form().values);

    const conflicted = build(form({ conflict: 9 }), hooks);
    expect(conflicted.body.querySelector(`.${C.settingsSave}`)).toBeNull();
    expect(conflicted.body.querySelector(`.${C.settingsConflict}`)?.textContent).toBe(
      "These settings changed after this dialog was opened. Nothing was saved, and your values are still here.",
    );
    conflicted.body.querySelector<HTMLButtonElement>(`.${C.settingsReapply}`)?.click();
    conflicted.body.querySelector<HTMLButtonElement>(`.${C.settingsReload}`)?.click();
    expect(hooks.onReapply).toHaveBeenCalledTimes(1);
    expect(hooks.onReload).toHaveBeenCalledTimes(1);
  });

  it("renders a read-only form for a non-administrator: no Save, and every control disabled", () => {
    const built = build(form({ readOnly: true }));
    const body = built.body;
    expect(body.querySelector(`.${C.settingsSave}`)).toBeNull();
    expect(body.querySelector<HTMLSelectElement>("select")?.disabled).toBe(true);
    for (const component of ["vat", "tax", "transfer"] as const) {
      expect(checkbox(body, component).disabled).toBe(true);
      expect(figure(body, component).disabled).toBe(true);
      expect(reset(body, component)?.disabled).toBe(true);
      expect(reset(body, component)?.hidden).toBe(true);
    }
  });

  it("reports an area change with the live values, so the caller can keep that draft", () => {
    const hooks = handlers();
    const built = build(form(), hooks);
    type(built.body, "vat", "12.5");
    toggle(built.body, "tax", true);
    const select = built.body.querySelector<HTMLSelectElement>("select");
    if (select === null) {
      throw new Error("no selector");
    }
    select.value = "DE-LU";
    select.dispatchEvent(new Event("change"));
    // The area reported is the one this body was *built* for, not the selector's new value: by the
    // time a change event fires the selector already reads the new choice, and calling these figures
    // the new area's would file one area's draft under another area's name.
    expect(hooks.onAreaChange).toHaveBeenCalledWith("DE-LU", {
      areaId: "SE4",
      vat: { enabled: true, value: "12.5", intent: "custom" },
      tax: { enabled: true, value: "36.5", intent: "custom" },
      transfer: { enabled: false, value: "", intent: "suggested" },
    });
    expect(built.values().areaId).toBe("SE4");
  });

  it("renders a hostile area name as text, never as markup", () => {
    const hostile = '<img src=x onerror=alert(1)>';
    const catalogue = options({
      areas: [{ ...areaById("SE4"), area_id: "XX", name: hostile }],
      configured_area: "XX",
    });
    const built = build(form({ options: catalogue, values: { ...form().values, areaId: "XX" } }));
    expect(built.body.querySelector("img")).toBeNull();
    expect(built.body.querySelector("option")?.textContent).toBe(`${hostile} · XX`);
  });
});

describe("a form built from an accepted record's four states", () => {
  const stated = (vat: AreaOverride["vat"]): MarketEditorForm =>
    form({
      values: {
        areaId: "SE4",
        vat: fiscalFormFrom({ area_id: "SE4", vat, tax: vat, transfer: vat }, "vat"),
        tax: { enabled: false, value: "", intent: "suggested" },
        transfer: { enabled: false, value: "", intent: "suggested" },
      },
    });

  it("renders `enabled, value: null` as checked, showing the suggestion, meaning the suggestion", () => {
    const built = build(stated({ enabled: true, value: null }));
    expect(checkbox(built.body, "vat").checked).toBe(true);
    expect(figure(built.body, "vat").value).toBe("25");
    expect(built.values().vat).toEqual({ enabled: true, value: "", intent: "suggested" });
  });

  it("renders `enabled, value: N` as checked, showing `N`, meaning the person's own", () => {
    const built = build(stated({ enabled: true, value: 12.5 }));
    expect(checkbox(built.body, "vat").checked).toBe(true);
    expect(figure(built.body, "vat").value).toBe("12.5");
    expect(built.values().vat).toEqual({ enabled: true, value: "12.5", intent: "custom" });
  });

  it("renders `disabled, value: N` as unchecked, showing `N`, and still the person's own", () => {
    const built = build(stated({ enabled: false, value: 30 }));
    expect(checkbox(built.body, "vat").checked).toBe(false);
    expect(figure(built.body, "vat").value).toBe("30");
    expect(built.values().vat).toEqual({ enabled: false, value: "30", intent: "custom" });
    // Checking it adopts the person's figure rather than the catalogue's.
    toggle(built.body, "vat", true);
    expect(built.values().vat).toEqual({ enabled: true, value: "30", intent: "custom" });
  });

  it("renders `disabled, value: null` as unchecked, showing the suggestion, and off", () => {
    const built = build(stated({ enabled: false, value: null }));
    expect(checkbox(built.body, "vat").checked).toBe(false);
    expect(figure(built.body, "vat").value).toBe("25");
    expect(built.values().vat).toEqual({ enabled: false, value: "", intent: "suggested" });
    // Checking it adopts the suggestion, unless the person edits first.
    toggle(built.body, "vat", true);
    expect(built.values().vat).toEqual({ enabled: true, value: "", intent: "suggested" });
    type(built.body, "vat", "7");
    expect(built.values().vat).toEqual({ enabled: true, value: "7", intent: "custom" });
  });
});
