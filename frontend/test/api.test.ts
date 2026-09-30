// The exact v2 dashboard and action messages, and how refusals are surfaced.

import { describe, expect, it } from "vitest";

import {
  REQUEST_FAILED_MESSAGE,
  SpotnavApiError,
  UNSUPPORTED_API_VERSION,
  getDashboard,
  listChargers,
  performAction,
} from "../src/api";
import {
  ACTION_API_VERSION,
  ACTION_RESUME,
  ACTION_START,
  ACTION_STOP,
  API_VERSION,
} from "../src/types";
import { FakeHass, apiFailure, dashboard } from "./helpers";
import { decodeDashboard } from "../src/validate";

describe("the two commands this prototype reads", () => {
  it("asks for the charger list with exactly api_version 2", async () => {
    const hass = new FakeHass();
    const pending = listChargers(hass);
    expect(hass.messages).toEqual([{ type: "spotnav/list_chargers", api_version: API_VERSION }]);
    hass.resolveNext({ api_version: 2, chargers: [] });
    await expect(pending).resolves.toEqual({ api_version: 2, chargers: [] });
  });

  it("asks for one charger's dashboard by config-entry id", async () => {
    const hass = new FakeHass();
    const pending = getDashboard(hass, "entry_a");
    expect(hass.messages).toEqual([
      { type: "spotnav/get_dashboard", api_version: API_VERSION, charger_id: "entry_a" },
    ]);
    hass.resolveNext(dashboard());
    await expect(pending).resolves.toMatchObject({ api_version: 1 });
  });

  it("carries the backend's stable refusal code, and nothing else from the rejection", async () => {
    const hass = new FakeHass();
    const refused = getDashboard(hass, "entry_a");
    hass.rejectNext(apiFailure("spotnav_unknown_charger", "No such charger"));
    const error = await refused.catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(SpotnavApiError);
    expect((error as SpotnavApiError).code).toBe("spotnav_unknown_charger");
    expect((error as SpotnavApiError).message).toBe(REQUEST_FAILED_MESSAGE);

    const broken = getDashboard(hass, "entry_a");
    hass.rejectNext(new Error("Connection lost"));
    const plain = await broken.catch((reason: unknown) => reason);
    expect(plain).toBeInstanceOf(SpotnavApiError);
    expect((plain as SpotnavApiError).code).toBeNull();
    expect((plain as SpotnavApiError).message).toBe(REQUEST_FAILED_MESSAGE);
  });

  it("keeps no part of a hostile rejection: no message text, no cause", async () => {
    const hostile =
      "Bearer eyJhbGciOiJIUzI1NiJ9.token </p><img src=x onerror=alert(1)> " +
      "/config/.storage/core.config_entries: KeyError('secret')";
    const hass = new FakeHass();
    const pending = getDashboard(hass, "entry_a");
    hass.rejectNext(apiFailure("spotnav_unknown_charger", hostile));
    const error = (await pending.catch((reason: unknown) => reason)) as SpotnavApiError;

    expect(error.message).toBe(REQUEST_FAILED_MESSAGE);
    expect(error.message).not.toContain("Bearer");
    expect(error.message).not.toContain("<img");
    expect(error.message).not.toContain("core.config_entries");
    expect(JSON.stringify(error, Object.getOwnPropertyNames(error))).not.toContain("Bearer");
    expect((error as Error & { cause?: unknown }).cause).toBeUndefined();
    expect("detail" in error).toBe(false);
  });

  it("hands the answer over untouched, so the decoder judges the version", async () => {
    const hass = new FakeHass();
    const pending = getDashboard(hass, "entry_a");
    const payload = dashboard({ api_version: 7 });
    hass.resolveNext(payload);
    // No cast, no field check and no refusal here: the transport layer carries `unknown`, and the
    // one decoder is what decides that another version -- the 7 this card does not speak -- is
    // `unsupported` rather than `malformed`.
    await expect(pending).resolves.toBe(payload);
    expect(decodeDashboard(payload)).toEqual({ ok: false, failure: "unsupported" });
  });
});

describe("the one action command this card writes with", () => {
  const answer = (over: Record<string, unknown> = {}) => ({
    api_version: ACTION_API_VERSION,
    ok: true,
    error: null,
    action: ACTION_START,
    choice: null,
    ...over,
  });

  it("sends exactly the contract's request for each action", async () => {
    const hass = new FakeHass();
    const start = performAction(hass, "entry_a", ACTION_START);
    expect(hass.messages).toEqual([
      {
        type: "spotnav/manual_action",
        api_version: ACTION_API_VERSION,
        charger_id: "entry_a",
        action: "start",
      },
    ]);
    hass.resolveNext(answer());
    await start;

    const resume = performAction(hass, "entry_a", ACTION_RESUME);
    expect(hass.messages[1]).toEqual({
      type: "spotnav/manual_action",
      api_version: ACTION_API_VERSION,
      charger_id: "entry_a",
      action: "resume",
    });
    hass.resolveNext(answer({ action: ACTION_RESUME }));
    await resume;

    // `choice` belongs to Stop and nowhere else, and the card adds it only there.
    const stop = performAction(hass, "entry_a", ACTION_STOP, "until_tomorrow");
    expect(hass.messages[2]).toEqual({
      type: "spotnav/manual_action",
      api_version: ACTION_API_VERSION,
      charger_id: "entry_a",
      action: "stop",
      choice: "until_tomorrow",
    });
    hass.resolveNext(answer({ action: ACTION_STOP, choice: "until_tomorrow" }));
    await expect(stop).resolves.toEqual(answer({ action: ACTION_STOP, choice: "until_tomorrow" }));
  });

  it("never sends a choice with an action that has none", async () => {
    const hass = new FakeHass();
    void performAction(hass, "entry_a", ACTION_START, "until_resumed");
    expect(hass.messages[0]).not.toHaveProperty("choice");
    hass.resolveNext(answer());
  });

  it("returns a refusal as the value it is, code and all", async () => {
    const hass = new FakeHass();
    const refused = performAction(hass, "entry_a", ACTION_STOP, "next_period");
    hass.resolveNext(
      answer({ ok: false, error: "invalid_pause", action: null, choice: null }),
    );
    await expect(refused).resolves.toEqual({
      api_version: ACTION_API_VERSION,
      ok: false,
      error: "invalid_pause",
      action: null,
      choice: null,
    });
  });

  it("refuses an answer that is not the exact envelope", async () => {
    const shapes: unknown[] = [
      null,
      [],
      "ok",
      { api_version: ACTION_API_VERSION, ok: true },
      { ...answer(), extra: 1 },
      { ...answer(), ok: "true" },
      { ...answer(), error: 7 },
      { ...answer(), action: 7 },
      { ...answer(), choice: {} },
    ];
    for (const shape of shapes) {
      const hass = new FakeHass();
      const pending = performAction(hass, "entry_a", ACTION_START);
      hass.resolveNext(shape);
      const error = await pending.catch((reason: unknown) => reason);
      expect(error, JSON.stringify(shape)).toBeInstanceOf(SpotnavApiError);
      expect((error as SpotnavApiError).message).toBe(REQUEST_FAILED_MESSAGE);
    }
  });
});
