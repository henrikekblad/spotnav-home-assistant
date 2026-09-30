// The settings contract on the client: strict decoding, one replacement per focused edit, and the
// summaries the resting row shows.
//
// The payloads here are hand-built *and* deliberately shaped exactly like the backend's v2 encoder
// (`settings_contract.py`): fifteen keys for a record, five for an envelope, three for a pause
// observation. The pure functions under test make no request, so everything here is a value in and a
// value out.

import { describe, expect, it } from "vitest";

import { LANGUAGES, translate } from "../src/i18n";
import {
  AMPS_MAX,
  AMPS_MIN,
  CURRENT_SLIDER_STEP_A,
  ENERGY_MAX_KWH,
  ENERGY_MIN_KWH,
  ENERGY_SLIDER_MAX_KWH,
  ENERGY_SLIDER_MIN_KWH,
  ENERGY_SLIDER_STEP_KWH,
  energySliderMaximum,
  checkCurrent,
  checkDeadlineTime,
  checkEnergy,
  checkMaxPeriods,
  decodeSettingsAnswer,
  encodeBody,
  formFromRecord,
  manualEnergyReadOnly,
  nominalPowerKw,
  replacementFor,
  settingsErrorKey,
  settingsSummaries,
  sliderRepresents,
  type SettingsFormValues,
} from "../src/settings";
import {
  SETTINGS_API_VERSION,
  SETTINGS_DRIVER_TARGET_SOC,
  type SettingsRecord,
} from "../src/types";

type Json = Record<string, unknown>;

function aRecord(overrides: Partial<SettingsRecord> = {}): SettingsRecord {
  return {
    revision: 7,
    area_id: "SE4",
    overrides: [
      {
        area_id: "SE4",
        vat: { enabled: true, value: null },
        tax: { enabled: true, value: 0.0 },
        transfer: { enabled: true, value: 8.75 },
      },
    ],
    phases: 3,
    amps: 10,
    requested_kwh: 20.5,
    max_periods: 4,
    departure_enabled: true,
    departure_time: "06:30",
    strategy: "cheapest",
    driver: "manual_kwh",
    target: { vehicle_id: null, target_percent: null },
    ...overrides,
  };
}

const aPause = { choice: null, admitted_at: null, expires_at: null };

function success(record: SettingsRecord = aRecord()): Json {
  return {
    api_version: SETTINGS_API_VERSION,
    ok: true,
    error: null,
    settings: { ...record },
    pause: { ...aPause },
  };
}

function refusal(code: string, record: SettingsRecord | null): Json {
  return {
    api_version: SETTINGS_API_VERSION,
    ok: false,
    error: code,
    settings: record === null ? null : { ...record },
    pause: record === null ? null : { ...aPause },
  };
}

function decode(raw: unknown) {
  return decodeSettingsAnswer(raw);
}

function form(overrides: Partial<SettingsFormValues> = {}): SettingsFormValues {
  return {
    energy: "20.5",
    deadlineEnabled: true,
    deadlineTime: "06:30",
    maxPeriods: "4",
    current: "10",
    driver: "manual_kwh",
    targetPercent: "",
    vehicleId: "",
    phases: "3",
    ...overrides,
  };
}

describe("the settings-v2 decoder", () => {
  it("accepts exactly the contract's shapes and returns them as values", () => {
    const decoded = decode(success());
    expect(decoded.ok).toBe(true);
    if (!decoded.ok) {
      return;
    }
    expect(decoded.value.ok).toBe(true);
    if (!decoded.value.ok) {
      return;
    }
    expect(decoded.value.settings).toEqual(aRecord());
    expect(decoded.value.pause).toEqual(aPause);
  });

  it("reads a refusal, with or without the record that still stands", () => {
    const withRecord = decode(refusal("revision_conflict", aRecord({ revision: 9 })));
    expect(withRecord.ok && withRecord.value.ok === false).toBe(true);
    if (withRecord.ok && !withRecord.value.ok) {
      expect(withRecord.value.code).toBe("revision_conflict");
      expect(withRecord.value.settings?.revision).toBe(9);
    }
    const withoutRecord = decode({
      api_version: SETTINGS_API_VERSION,
      ok: false,
      error: "spotnav_settings_unavailable",
      settings: null,
      pause: null,
    });
    expect(withoutRecord.ok && withoutRecord.value.ok === false).toBe(true);
    if (withoutRecord.ok && !withoutRecord.value.ok) {
      expect(withoutRecord.value.settings).toBeNull();
      expect(withoutRecord.value.pause).toBeNull();
    }
  });

  it("refuses a different contract version as unsupported, never as a partial read", () => {
    expect(decode({ ...success(), api_version: 2 })).toEqual({ ok: false, failure: "unsupported" });
    // A missing or non-numeric version is not a version disagreement: the payload is not this shape.
    const noVersion = { ...success() };
    delete noVersion.api_version;
    expect(decode(noVersion)).toEqual({ ok: false, failure: "malformed" });
    expect(decode({ ...success(), api_version: "1" })).toEqual({ ok: false, failure: "malformed" });
  });

  it.each([
    ["not-an-object", "text"],
    ["null", null],
    ["array", []],
  ])("refuses %s as malformed", (_name, payload) => {
    expect(decode(payload)).toEqual({ ok: false, failure: "malformed" });
  });

  it.each([
    ["missing pause", { api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: aRecord() }],
    [
      "extra envelope key",
      { ...success(), extra: 1 },
    ],
    [
      "success with an error code",
      { ...success(), error: "spotnav_settings_not_committed" },
    ],
    ["success without a record", { ...success(), settings: null, pause: null }],
    [
      "success without a pause",
      { ...success(), pause: null },
    ],
    [
      "refusal without a code",
      { ...refusal("", aRecord()) },
    ],
    [
      "refusal with a record but no pause",
      { ...refusal("revision_conflict", aRecord()), pause: null },
    ],
    [
      "refusal with a pause but no record",
      { ...refusal("revision_conflict", null), pause: { ...aPause } },
    ],
  ])("refuses an incoherent envelope (%s)", (_name, payload) => {
    expect(decode(payload)).toEqual({ ok: false, failure: "malformed" });
  });

  it.each([
    ["missing field", (record: Json) => delete record.max_periods],
    ["unknown field", (record: Json) => (record.amperage = 10)],
    ["boolean revision", (record: Json) => (record.revision = true)],
    ["negative revision", (record: Json) => (record.revision = -1)],
    ["fractional revision", (record: Json) => (record.revision = 1.5)],
    // v4's vocabulary admits `solar` and `hybrid` too (see `strategyReplacement`); a strategy
    // this release cannot store at all is still refused.
    ["unknown strategy", (record: Json) => (record.strategy = "grid_only")],
    ["unknown driver", (record: Json) => (record.driver = "target_kwh")],
    ["phases as a string", (record: Json) => (record.phases = "3")],
    ["fractional amps", (record: Json) => (record.amps = 10.5)],
    ["fractional periods", (record: Json) => (record.max_periods = 2.5)],
    ["NaN energy", (record: Json) => (record.requested_kwh = Number.NaN)],
    ["infinite energy", (record: Json) => (record.requested_kwh = Number.POSITIVE_INFINITY)],
    ["string energy", (record: Json) => (record.requested_kwh = "20")],
    ["naive departure time", (record: Json) => (record.departure_time = "2026-09-22T06:30:00")],
    ["24-hour time", (record: Json) => (record.departure_time = "24:00")],
    ["single-digit time", (record: Json) => (record.departure_time = "6:30")],
    ["overrides not a list", (record: Json) => (record.overrides = {})],
    ["override with an unknown key", (record: Json) => ((record.overrides as Json[])[0]!.color = "red")],
    ["fiscal enabled as a string", (record: Json) => (((record.overrides as Json[])[0]!.vat as Json).enabled = "true")],
    ["fiscal value as a string", (record: Json) => (((record.overrides as Json[])[0]!.tax as Json).value = "0")],
    ["target missing a key", (record: Json) => delete (record.target as Json).vehicle_id],
  ])("refuses a record with %s", (_name, mutate) => {
    const record = { ...aRecord() } as unknown as Json;
    record.overrides = [
      {
        area_id: "SE4",
        vat: { enabled: true, value: null },
        tax: { enabled: true, value: 0.0 },
        transfer: { enabled: true, value: 8.75 },
      },
    ];
    record.target = { vehicle_id: null, target_percent: null };
    mutate(record);
    expect(decode(success(record as unknown as SettingsRecord))).toEqual({
      ok: false,
      failure: "malformed",
    });
  });

  it.each([
    ["all nulls with an instant", { choice: null, admitted_at: "2026-09-22T06:00:00+02:00", expires_at: null }],
    ["until_resumed with an expiry", { choice: "until_resumed", admitted_at: null, expires_at: "2026-09-22T06:00:00+02:00" }],
    ["a timed choice without an expiry", { choice: "until_tomorrow", admitted_at: null, expires_at: null }],
    ["an expiry before admission", { choice: "until_tomorrow", admitted_at: "2026-09-22T06:00:00+02:00", expires_at: "2026-09-22T05:00:00+02:00" }],
    ["a naive instant", { choice: "until_tomorrow", admitted_at: null, expires_at: "2026-09-22T06:00:00" }],
    ["an unknown choice", { choice: "forever", admitted_at: null, expires_at: null }],
    ["an extra key", { choice: null, admitted_at: null, expires_at: null, resume_at: null }],
  ])("refuses a pause observation with %s", (_name, pause) => {
    expect(decode({ ...success(), pause })).toEqual({ ok: false, failure: "malformed" });
  });

  it.each([0, 0.5, 80.5, 99.9, 100])('accepts a decimal target of %s percent', (percent) => {
    const decoded = decode(success(aRecord({ target: { vehicle_id: "car", target_percent: percent } })));

    expect(decoded.ok).toBe(true);
    if (!decoded.ok || !decoded.value.ok) {
      return;
    }
    // Exactly the value that arrived: a percentage is a decimal the backend stores without rounding.
    expect(decoded.value.settings.target.target_percent).toBe(percent);
  });

  it.each([
    ["a percentage past 100", 100.5],
    ["a negative percentage", -1],
    ["a boolean percentage", true],
    ["a string percentage", "80"],
    ["a NaN percentage", Number.NaN],
    ["an infinite percentage", Number.POSITIVE_INFINITY],
  ])("refuses %s", (_name, percent) => {
    expect(
      decode(success(aRecord({ target: { vehicle_id: null, target_percent: percent as number} }))),
    ).toEqual({ ok: false, failure: "malformed" });
  });

  it("accepts an indefinite pause and a migrated pause without an admission instant", () => {
    const indefinite = decode({ ...success(), pause: { choice: "until_resumed", admitted_at: null, expires_at: null } });
    expect(indefinite.ok).toBe(true);
    const migrated = decode({
      ...success(),
      pause: {
        choice: "until_tomorrow",
        admitted_at: null,
        expires_at: "2026-09-23T06:00:00+02:00",
      },
    });
    expect(migrated.ok).toBe(true);
  });
});

describe("the pure replacement builders", () => {
  it("never carries the revision, and copies every nested record into fresh objects", () => {
    const record = aRecord();
    const body = encodeBody(record);

    expect(Object.keys(body)).not.toContain("revision");
    expect(Object.keys(body).sort()).toEqual(
      [
        "amps",
        "area_id",
        "departure_enabled",
        "departure_time",
        "driver",
        "max_periods",
        "overrides",
        "phases",
        "requested_kwh",
        "strategy",
        "target",
      ].sort(),
    );
    // Nothing of the record is aliased into the body a request would carry.
    expect(body.overrides).not.toBe(record.overrides);
    expect(body.overrides[0]).not.toBe(record.overrides[0]!);
    expect(body.overrides[0]!.vat).not.toBe(record.overrides[0]!.vat);
    expect(body.target).not.toBe(record.target);
    expect(body.overrides).toEqual(record.overrides);
    expect(body.target).toEqual(record.target);
  });

  it.each([
    ["energy", "requested_kwh"],
    ["deadline", "departure_enabled,departure_time,max_periods"],
    ["current", "amps"],
  ] as const)("lets a %s Save change only its own fields", (kind, ownedList) => {
    const record = aRecord();
    const base = encodeBody(record);
    const owned = ownedList.split(",");
    const values = form({
      energy: "12.5",
      deadlineEnabled: false,
      deadlineTime: "05:15",
      maxPeriods: "8",
      current: "16",
    });

    const check = replacementFor(kind, record, values);

    expect(check.ok).toBe(true);
    if (!check.ok) {
      return;
    }
    expect(check.changed).toBe(true);
    for (const key of Object.keys(base) as Array<keyof typeof base>) {
      if (!owned.includes(key)) {
        expect(check.body[key], `${kind}/${key}`).toEqual(base[key]);
      }
    }
    expect(Object.keys(check.body).sort()).toEqual(Object.keys(base).sort());
    // The values the editor owns are the ones the person typed, and nothing was rounded.
    if (kind === "energy") {
      expect(check.body.requested_kwh).toBe(12.5);
    }
    if (kind === "current") {
      expect(check.body.amps).toBe(16);
      expect(check.body.phases).toBe(3);
    }
    if (kind === "deadline") {
      expect(check.body.departure_enabled).toBe(false);
      expect(check.body.departure_time).toBe("05:15");
      expect(check.body.max_periods).toBe(8);
    }
  });

  it.each(["energy", "deadline", "current"] as const)(
    "reproduces a decimal target byte for byte through a %s save",
    (kind) => {
      const record = aRecord({
        target: { vehicle_id: "car-1", target_percent: 80.5 },
      });
      const check = replacementFor(kind, record, form({ energy: "30", maxPeriods: "5", current: "16" }));

      expect(check.ok).toBe(true);
      if (!check.ok) {
        return;
      }
      expect(check.body.target).toEqual({ vehicle_id: "car-1", target_percent: 80.5 });
      expect(check.body.target.target_percent).toBe(80.5);
      expect(check.body.driver).toBe(record.driver);
    },
  );

  it("keeps a valid decimal exactly and preserves phases for 1, 3 and null", () => {
    const decimal = replacementFor("energy", aRecord({ requested_kwh: 20.25 }), form({ energy: "20.25" }));
    expect(decimal.ok && decimal.body.requested_kwh).toBe(20.25);
    expect(decimal.ok && decimal.changed).toBe(false);

    for (const phases of [1, 3, null] as const) {
      const check = replacementFor("current", aRecord({ phases, amps: 10 }), form({ current: "16" }));
      expect(check.ok, String(phases)).toBe(true);
      expect(check.ok && check.body.phases, String(phases)).toBe(phases);
    }
  });

  it("reapplies only the Plan fields the reader moved onto a newer record after a conflict", () => {
    const opened = aRecord();
    const newer = aRecord({ amps: 16, max_periods: 2, revision: opened.revision + 1 });
    const check = replacementFor("plan", newer, form({ energy: "30" }), null, opened);
    expect(check.ok).toBe(true);
    if (check.ok) {
      expect(check.body.requested_kwh).toBe(30);
      expect(check.body.amps).toBe(16);
      expect(check.body.max_periods).toBe(2);
      expect(check.changed).toBe(true);
    }
    // Without the opened record (an ordinary Save) every Plan field is written as shown.
    const plain = replacementFor("plan", newer, form({ energy: "30" }));
    expect(plain.ok && plain.body.amps).toBe(10);
  });

  it("sends nothing at all when the owned fields already match what was read", () => {
    const record = aRecord();
    expect(replacementFor("energy", record, form())).toMatchObject({ ok: true, changed: false });
    expect(replacementFor("deadline", record, form())).toMatchObject({ ok: true, changed: false });
    expect(replacementFor("current", record, form())).toMatchObject({ ok: true, changed: false });
    expect(replacementFor("deadline", record, form({ maxPeriods: "5" }))).toMatchObject({ ok: true, changed: true });
    expect(replacementFor("deadline", record, form({ deadlineEnabled: false }))).toMatchObject({
      ok: true,
      changed: true,
    });
  });

  it("turns only the Boolean off, keeping the stored time and period cap", () => {
    const off = replacementFor("deadline", aRecord(), form({ deadlineEnabled: false }));
    expect(off.ok).toBe(true);
    if (!off.ok) {
      return;
    }
    expect(off.body.departure_enabled).toBe(false);
    expect(off.body.departure_time).toBe("06:30");
    expect(off.body.max_periods).toBe(4);
    expect(off.body.departure_time).not.toBe("00:00");
  });

  it("refuses invalid input locally, without clamping it into range", () => {
    const checks: Array<[string, () => { ok: boolean }]> = [
      ["empty energy", () => checkEnergy("")],
      ["text energy", () => checkEnergy("twenty")],
      ["NaN energy", () => checkEnergy("NaN")],
      ["infinite energy", () => checkEnergy("Infinity")],
      ["negative energy", () => checkEnergy("-1")],
      ["too little energy", () => checkEnergy("0")],
      ["too much energy", () => checkEnergy(String(ENERGY_MAX_KWH + 1))],
      ["empty time", () => checkDeadlineTime("")],
      ["short time", () => checkDeadlineTime("6:30")],
      ["impossible time", () => checkDeadlineTime("24:00")],
      ["empty periods", () => checkMaxPeriods("")],
      ["fractional periods", () => checkMaxPeriods("2.5")],
      ["zero periods", () => checkMaxPeriods("0")],
      ["too many periods", () => checkMaxPeriods("9")],
      ["empty current", () => checkCurrent("")],
      ["fractional current", () => checkCurrent("10.5")],
      ["zero current", () => checkCurrent("0")],
      ["too much current", () => checkCurrent(String(AMPS_MAX + 1))],
    ];
    for (const [name, check] of checks) {
      expect(check().ok, name).toBe(false);
    }
    // And what *is* accepted is accepted exactly: a comma is a decimal a person may type, and the
    // bounds themselves are values.
    expect(checkEnergy("12,5")).toEqual({ ok: true, value: 12.5 });
    expect(checkEnergy("0.1")).toEqual({ ok: true, value: 0.1 });
    expect(checkEnergy("1000")).toEqual({ ok: true, value: 1000 });
    expect(checkMaxPeriods("1")).toEqual({ ok: true, value: 1 });
    expect(checkMaxPeriods("8")).toEqual({ ok: true, value: 8 });
    expect(checkCurrent("1")).toEqual({ ok: true, value: 1 });
    expect(checkCurrent(String(AMPS_MAX))).toEqual({ ok: true, value: AMPS_MAX });
    expect(checkDeadlineTime(" 06:30 ")).toEqual({ ok: true, value: "06:30" });
  });

  it("prefills the form from the record, with an unset current shown as empty", () => {
    expect(formFromRecord(aRecord())).toEqual({
      energy: "20.5",
      deadlineEnabled: true,
      deadlineTime: "06:30",
      maxPeriods: "4",
      current: "10",
      driver: "manual_kwh",
      targetPercent: "",
      vehicleId: "",
      phases: "3",
    });
    expect(formFromRecord(aRecord({ phases: null })).phases).toBe("");
    expect(formFromRecord(aRecord({ amps: null, requested_kwh: 20 })).current).toBe("");
    expect(formFromRecord(aRecord({ amps: null, requested_kwh: 20 })).energy).toBe("20");
  });

  it("marks manual energy read-only only for a target-SoC driver", () => {
    expect(manualEnergyReadOnly(aRecord())).toBe(false);
    expect(manualEnergyReadOnly(aRecord({ driver: SETTINGS_DRIVER_TARGET_SOC }))).toBe(true);
  });
});

describe("the settings sentences", () => {
  it.each([
    [null, "settings.error.generic"],
    ["spotnav_settings_not_committed", "settings.error.notCommitted"],
    ["spotnav_settings_reconcile_failed", "settings.error.reconcileFailed"],
    ["spotnav_settings_unavailable", "settings.error.unavailable"],
    ["invalid_number", "settings.error.invalid"],
    ["invalid_energy", "settings.error.invalid"],
    ["invalid_strategy", "settings.error.invalid"],
    ["unauthorized", "settings.error.readOnly"],
    ["spotnav_unsupported_api_version", "settings.error.version"],
    ["spotnav_unknown_charger", "settings.error.charger"],
    ["spotnav_charger_unloaded", "settings.error.charger"],
    ["spotnav_something_new", "settings.error.generic"],
  ])("maps %s to its own sentence", (code, key) => {
    expect(settingsErrorKey(code)).toBe(key);
  });

  it("keeps a committed-but-unreconciled write apart from one that never committed", () => {
    expect(settingsErrorKey("spotnav_settings_reconcile_failed")).not.toBe(
      settingsErrorKey("spotnav_settings_not_committed"),
    );
    for (const language of LANGUAGES) {
      expect(translate(language, "settings.error.reconcileFailed")).not.toBe(
        translate(language, "settings.error.notCommitted"),
      );
      expect(translate(language, "settings.error.reconcileFailed")).not.toBe(
        translate(language, "settings.error.confirmationFailed"),
      );
    }
  });
});

describe("the nominal power and the slider rule", () => {
  it("mirrors the backend's own power arithmetic, term for term", () => {
    // The same literals are asserted against `planner.power_kw` in `tests/test_planner.py`, so the two
    // sides are pinned to one arithmetic rather than to two similar ones.
    const singlePhase: Array<[number, number]> = [
      [1, 0.23],
      [6, 1.38],
      [16, 3.68],
      [80, 18.4],
    ];
    for (const [amps, expected] of singlePhase) {
      expect(nominalPowerKw(amps, 1), `${amps} A`).toBe(expected);
    }
    const threePhase: Array<[number, number]> = [
      [1, 0.6928203230275509],
      [6, 4.156921938165306],
      [16, 11.085125168440815],
      [80, 55.42562584220407],
    ];
    for (const [amps, expected] of threePhase) {
      expect(nominalPowerKw(amps, 3), `${amps} A`).toBe(expected);
    }
    // The reviewed three-phase form is deliberately *not* the `230 x 3` shortcut: the two disagree at
    // ordinary currents, which is exactly why the card may not invent the simpler one.
    expect(nominalPowerKw(16, 3)).not.toBe((230 * 3 * 16) / 1000);
    expect(nominalPowerKw(16, 1)).toBe((230 * 16) / 1000);
  });

  it("names no power at all for an unknown phase count", () => {
    expect(nominalPowerKw(16, null)).toBeNull();
    for (const phases of [0, 2, 4, -1]) {
      expect(nominalPowerKw(16, phases), `${phases} phases`).toBeNull();
    }
    expect(nominalPowerKw(Number.NaN, 1)).toBeNull();
    expect(nominalPowerKw(Number.POSITIVE_INFINITY, 3)).toBeNull();
  });

  it("knows which exact values a slider can represent", () => {
    // The grid is anchored at the slider's own minimum, exactly as HTML anchors it.
    for (const value of [0.5, 20, 20.5, 100, 150, 1000]) {
      expect(sliderRepresents(value, ENERGY_SLIDER_MIN_KWH, ENERGY_SLIDER_STEP_KWH), String(value)).toBe(
        true,
      );
    }
    for (const value of [0.1, 0.25, 20.25, 20.3, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(sliderRepresents(value, ENERGY_SLIDER_MIN_KWH, ENERGY_SLIDER_STEP_KWH), String(value)).toBe(
        false,
      );
    }
    // Whole amperes, on the current slider's own grid.
    for (const value of [1, 16, 80]) {
      expect(sliderRepresents(value, AMPS_MIN, CURRENT_SLIDER_STEP_A), String(value)).toBe(true);
    }
    for (const value of [0.5, 10.5]) {
      expect(sliderRepresents(value, AMPS_MIN, CURRENT_SLIDER_STEP_A), String(value)).toBe(false);
    }
    // The predicate is about the *grid*; the domain is the form's own check. A current above the
    // contract's range sits on the grid and is still not offered by this slider.
    expect(sliderRepresents(200, AMPS_MIN, CURRENT_SLIDER_STEP_A)).toBe(true);
    expect(200 <= AMPS_MAX).toBe(false);
  });

  it("extends the energy slider only to include a representable value above the ordinary interval", () => {
    expect(energySliderMaximum(20.25)).toBe(ENERGY_SLIDER_MAX_KWH);
    expect(energySliderMaximum(ENERGY_SLIDER_MIN_KWH)).toBe(ENERGY_SLIDER_MAX_KWH);
    expect(energySliderMaximum(100)).toBe(100);
    expect(energySliderMaximum(150)).toBe(150);
    expect(energySliderMaximum(1000)).toBe(1000);
    expect(energySliderMaximum(Number.NaN)).toBe(ENERGY_SLIDER_MAX_KWH);
  });

  it("keeps the card's slider interval a different control from the app's target-SoC slider", () => {
    // `planner.ENERGY_SLIDER_MIN_KWH` is 1 (whole kWh, the Android target-SoC input); this card's
    // energy slider is the half-kWh convenience the plan asks for. Pinned so nobody unifies them by
    // accident with the wrong bound.
    expect(ENERGY_SLIDER_MIN_KWH).toBe(0.5);
    expect(ENERGY_SLIDER_MAX_KWH).toBe(100);
    expect(ENERGY_SLIDER_STEP_KWH).toBe(0.5);
    expect(ENERGY_MIN_KWH).toBe(0.1);
    expect(ENERGY_MAX_KWH).toBe(1000);
  });
});
