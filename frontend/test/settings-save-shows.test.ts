// Each value of the Settings page shows its new value as soon as its editor saved, also when the card's own
// periodic read came back while the editor was open (it carries the value from before the save, and must not be
// the page the reader returns to).

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { REFRESH_INTERVAL_MS } from "../src/card";
import { translate } from "../src/i18n";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, any>;
const PAUSE = { choice: null, admitted_at: null, expires_at: null };
const EV6 = "vehicle_ev6";

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

/** The two-car dashboard with a camera; `change` makes it the dashboard after a save. */
function dashboard(change: (payload: Record<string, any>) => void = () => undefined): Record<string, any> {
  const payload = read("dashboard", "target_soc_two_vehicles.json");
  payload["settings"]["identify_camera"] = { camera_entity_id: "camera.norr", ai_task_entity_id: "ai_task.a", frame: null };
  payload["camera_identification"] = {
    cameras: [{ entity_id: "camera.norr", name: "Norr" }],
    ai_tasks: [
      { entity_id: "ai_task.a", name: "Model A", model: null },
      { entity_id: "ai_task.b", name: "Model B", model: null },
    ],
    references: { [EV6]: [], vehicle_niro: [] },
  };
  change(payload);
  return payload;
}

interface Opened {
  hass: FakeHass;
  element: HTMLElement;
}

async function openSettings(): Promise<Opened> {
  const hass = new FakeHass();
  const element = mountCard({ type: "custom:spotnav-card", charger: "soc_charger" }, hass);
  const snapshot = hass.snapshot("snapshot", "en");
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(dashboard());
  await settle();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/update_vehicle"
      ? read("vehicle", "v1", "update_vehicle_success.json")
      : message["type"] === "spotnav/get_entity_config"
        ? read("vehicle", "v1", "update_vehicle_success.json")
        : new Promise(() => undefined);
  hass.cameraHandler = async () => ({ api_version: 1, ok: true, error: null, references: [], identify_camera: null });
  shadow(element).querySelector<HTMLButtonElement>(`[aria-label='${translate("en", "header.settings")}']`)!.click();
  await settle();
  return { hass, element };
}

function page(element: Element): HTMLElement {
  return Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']")).find((dialog) => dialog.closest("[hidden]") === null)!;
}

/** The card's periodic read comes back while the editor is open: the dashboard from before the save. */
async function periodicReadWhileEditing(hass: FakeHass): Promise<void> {
  await vi.advanceTimersByTimeAsync(REFRESH_INTERVAL_MS);
  hass.resolveNext(dashboard());
  await settle();
}

/** Answer the save's own requests (in order), then the confirming read with `after`. */
async function answerSave(hass: FakeHass, replies: unknown[], after: Record<string, any>): Promise<void> {
  for (const reply of replies) {
    hass.resolveNext(reply);
    await settle();
  }
  hass.resolveNext(after);
  await settle();
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("a value shows its new value right after its editor saved", () => {
  it("one of a few (the AI task)", async () => {
    const { hass, element } = await openSettings();
    page(element).querySelector<HTMLButtonElement>("[data-edit='identify_ai_task']")!.click();
    await settle();
    await periodicReadWhileEditing(hass);
    const form = page(element).querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    form.querySelector<HTMLInputElement>("[data-value-option='ai_task.b']")!.click();
    form.requestSubmit();
    await settle();
    const after = dashboard((payload) => {
      payload["settings"]["identify_camera"]["ai_task_entity_id"] = "ai_task.b";
    });
    const before = dashboard();
    await answerSave(
      hass,
      [
        { api_version: 1, ok: true, error: null, settings: before["settings"], pause: PAUSE },
        { api_version: 1, ok: true, error: null, settings: after["settings"], pause: PAUSE },
      ],
      after,
    );
    expect(page(element).querySelector("[data-row='identify_ai_task']")?.textContent).toContain("Model B");
  });

  it("several of a few (the cars at this charger)", async () => {
    const { hass, element } = await openSettings();
    page(element).querySelector<HTMLButtonElement>("[data-edit='identify_vehicles']")!.click();
    await settle();
    await periodicReadWhileEditing(hass);
    const form = page(element).querySelector<HTMLFormElement>("form[data-value-editor='multi']")!;
    form.querySelector<HTMLInputElement>(`[data-value-option='${EV6}']`)!.click();
    form.requestSubmit();
    await settle();
    const after = dashboard((payload) => {
      payload["settings"]["vehicle_ids"] = ["vehicle_niro"];
    });
    await answerSave(
      hass,
      [
        { api_version: 1, ok: true, error: null, settings: dashboard()["settings"], pause: PAUSE },
        { api_version: 1, ok: true, error: null, settings: after["settings"], pause: PAUSE },
      ],
      after,
    );
    expect(page(element).querySelector("[data-row='identify_vehicles']")?.textContent).not.toContain(
      translate("en", "identify.vehicles.all"),
    );
  });

  it("a number (a car's battery size)", async () => {
    const { hass, element } = await openSettings();
    page(element).querySelector<HTMLButtonElement>(`[data-vehicle='${EV6}'] [data-edit='capacity']`)!.click();
    await settle();
    await periodicReadWhileEditing(hass);
    const form = page(element).querySelector<HTMLFormElement>("form[data-value-editor='number']")!;
    form.querySelector<HTMLInputElement>("[data-value-field='number']")!.value = "80";
    form.requestSubmit();
    await settle();
    const after = dashboard((payload) => {
      payload["vehicles"][0]["capacity_kwh"] = 80;
    });
    await answerSave(hass, [], after);
    expect(page(element).querySelector(`[data-vehicle='${EV6}'] [data-row='capacity']`)?.textContent).toContain("80.0 kWh");
  });

  it("the crop (the frame editor)", async () => {
    const { hass, element } = await openSettings();
    hass.cameraHandler = async (message) =>
      message["type"] === "spotnav/camera_snapshot"
        ? { api_version: 1, ok: true, error: null, picture: { content_type: "image/jpeg", data: "AAAA", width: 640, height: 240 } }
        : { api_version: 1, ok: true, error: null, identify_camera: null };
    page(element).querySelector<HTMLButtonElement>("[data-edit='identify_frame']")!.click();
    await settle();
    await periodicReadWhileEditing(hass);
    page(element).querySelector<HTMLFormElement>("form[data-value-editor='frame']")!.querySelector<HTMLButtonElement>("[data-camera-whole]")!.click();
    const box = page(element).querySelector<HTMLElement>("[data-camera-frame]")!;
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", shiftKey: true, bubbles: true }));
    const form = page(element).querySelector<HTMLFormElement>("form[data-value-editor='frame']")!;
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", shiftKey: true, bubbles: true }));
    form.requestSubmit();
    await settle();
    const after = dashboard((payload) => {
      payload["settings"]["identify_camera"]["frame"] = { x: 0, y: 0, w: 0.99, h: 1 };
    });
    await answerSave(hass, [], after);
    expect(page(element).querySelector("[data-row='identify_frame']")?.textContent).toContain(translate("en", "camera.frame.drawn"));
  });

  it("a reference picture taken now, once its editor is closed", async () => {
    const { hass, element } = await openSettings();
    page(element).querySelector<HTMLButtonElement>(`[data-vehicle='${EV6}'] [data-edit='reference']`)!.click();
    await settle();
    await periodicReadWhileEditing(hass);
    const form = page(element).querySelector<HTMLFormElement>("form[data-value-editor='reference']")!;
    form.querySelector<HTMLButtonElement>("[data-reference='day'] [data-reference-action='take']")!.click();
    await settle();
    // The picture shows in its slot at once; closing the editor reads the dashboard again for the Settings row.
    page(element).querySelector<HTMLButtonElement>(`.${VISUAL_CLASSES.dialogClose}`)!.click();
    await settle();
    const after = dashboard((payload) => {
      payload["camera_identification"]["references"][EV6] = [{ kind: "day", taken_at: "2026-10-07T12:00:00+00:00", colour: true }];
    });
    await answerSave(hass, [], after);
    expect(page(element).querySelector(`[data-vehicle='${EV6}'] [data-row='reference']`)?.textContent).toContain(
      translate("en", "reference.day"),
    );
  });
});
