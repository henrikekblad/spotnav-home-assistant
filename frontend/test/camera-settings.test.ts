// The camera for vehicle identification in the card: the frame's geometry, the settings field, the dashboard
// block, the Settings rows (camera, frame, AI task), the frame editor (drag and resize by pointer or keys, the
// preview, Save) and a car's reference pictures (a day and a night slot, each taken, retaken or deleted in place),
// in five languages.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clampFrame,
  decodePicture,
  FRAME_MIN_SIZE,
  isWholePicture,
  moveFrame,
  previewStyle,
  referenceText,
  resizeFrame,
} from "../src/camera-editor";
import { LANGUAGES, translate, type TranslationKey } from "../src/i18n";
import { cameraReplacement, decodeSettingsRecord, encodeBody } from "../src/settings";
import { SETTINGS_API_VERSION, type SettingsRecord } from "../src/types";
import { decodeDashboard } from "../src/validate";
import { FakeHass, mountCard } from "./helpers";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");
const CONFIG = { type: "custom:spotnav-card", charger: "soc_charger" };
const shadow = (element: Element): ShadowRoot => element.shadowRoot as ShadowRoot;
const read = (...parts: string[]): Record<string, any> =>
  JSON.parse(readFileSync(join(FIXTURES, ...parts), "utf8")) as Record<string, any>;

const EV6 = "vehicle_ev6";
const NIRO = "vehicle_niro";
const FRAME = { x: 0.5, y: 0.1, w: 0.4, h: 0.8 };
const PIXEL = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==";

function picture(width = 640, height = 240): Record<string, unknown> {
  return { api_version: 1, ok: true, error: null, picture: { content_type: "image/jpeg", data: PIXEL, width, height } };
}

/** The two-vehicle dashboard with a camera chosen (or not), and the camera block. */
function dashboard(camera: Record<string, unknown> | null = { camera_entity_id: "camera.norr", ai_task_entity_id: null, frame: FRAME }): Record<string, any> {
  const payload = read("dashboard", "target_soc_two_vehicles.json");
  payload["settings"]["identify_camera"] = camera;
  payload["camera_identification"] = {
    cameras: [{ entity_id: "camera.norr", name: "Norr" }, { entity_id: "camera.syd", name: "Syd" }],
    ai_tasks: [{ entity_id: "ai_task.ollama", name: "Ollama" }],
    references: {
      [EV6]: [
        { kind: "day", taken_at: "2026-10-07T12:00:00+00:00", colour: true },
        { kind: "night", taken_at: "2026-10-07T21:00:00+00:00", colour: false },
      ],
      [NIRO]: [],
    },
  };
  return payload;
}

async function settle(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0);
}

async function openSettings(payload: Record<string, unknown>, language = "en") {
  const hass = new FakeHass();
  const element = mountCard(CONFIG, hass);
  const snapshot = hass.snapshot("snapshot", language);
  snapshot.user = { is_admin: true };
  element.hass = snapshot;
  await settle();
  hass.resolveNext(payload);
  await settle();
  hass.entityHandler = async (message) =>
    message["type"] === "spotnav/get_entity_config" ? read("vehicle", "v1", "update_vehicle_success.json") : new Promise(() => undefined);
  hass.cameraHandler = async (message) => (message["type"] === "spotnav/camera_snapshot" ? picture() : picture(240, 90));
  shadow(element)
    .querySelector<HTMLButtonElement>(`[aria-label="${translate(language as "en", "header.settings")}"]`)!
    .click();
  await settle();
  return { hass, element };
}

/** A car's section on the Settings page, its tab opened first when the cars have tabs. */
function carSection(page: HTMLElement, id: string): HTMLElement {
  page.querySelector<HTMLButtonElement>(`[data-vehicle-tab='${id}'][aria-selected='false']`)?.click();
  return page.querySelector<HTMLElement>(`[data-section='vehicle'][data-vehicle='${id}']`)!;
}

function openDialog(element: Element): HTMLElement | null {
  const dialogs = Array.from(shadow(element).querySelectorAll<HTMLElement>("[role='dialog']"));
  return dialogs.find((dialog) => dialog.closest("[hidden]") === null) ?? null;
}

beforeEach(() => {
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the frame's geometry", () => {
  it("keeps a frame inside the picture and above the least size", () => {
    expect(clampFrame({ x: -0.2, y: 0.9, w: 0.5, h: 0.5 })).toEqual({ x: 0, y: 0.5, w: 0.5, h: 0.5 });
    expect(clampFrame({ x: 0.5, y: 0.5, w: 0.001, h: 2 })).toEqual({ x: 0.5, y: 0, w: FRAME_MIN_SIZE, h: 1 });
    expect(clampFrame({ x: 0.123456, y: 0, w: 0.5, h: 0.5 }).x).toBe(0.1235);
  });

  it("moves without changing its size and stops at the edges", () => {
    expect(moveFrame(FRAME, -0.2, 0)).toEqual({ ...FRAME, x: 0.3 });
    expect(moveFrame(FRAME, 0.5, 0.5)).toEqual({ x: 0.6, y: 0.2, w: 0.4, h: 0.8 });
  });

  it("resizes from a corner while the opposite corner stays", () => {
    expect(resizeFrame(FRAME, "se", 0.05, 0.05)).toEqual({ x: 0.5, y: 0.1, w: 0.45, h: 0.85 });
    expect(resizeFrame(FRAME, "nw", -0.1, -0.5)).toEqual({ x: 0.4, y: 0, w: 0.5, h: 0.9 });
    expect(resizeFrame(FRAME, "ne", 0.5, 0)).toEqual({ x: 0.5, y: 0.1, w: 0.5, h: 0.8 });
    expect(resizeFrame(FRAME, "sw", 0.9, -0.9)).toEqual({ x: 0.85, y: 0.1, w: FRAME_MIN_SIZE, h: FRAME_MIN_SIZE });
  });

  it("previews exactly the frame", () => {
    expect(previewStyle({ x: 0.5, y: 0, w: 0.5, h: 1 }, { width: 640, height: 240 })).toEqual({
      size: "200% 100%",
      position: "100% 0%",
      aspectRatio: "320 / 240",
    });
    expect(previewStyle({ x: 0.25, y: 0.25, w: 0.5, h: 0.5 }, { width: 100, height: 100 }).position).toBe("50% 50%");
    expect(isWholePicture(null) && isWholePicture({ x: 0, y: 0, w: 1, h: 1 }) && !isWholePicture(FRAME)).toBe(true);
  });

  it("reads a picture answer and nothing else", () => {
    expect(decodePicture(picture())).toEqual({ picture: { url: `data:image/jpeg;base64,${PIXEL}`, width: 640, height: 240 } });
    expect(decodePicture({ ok: false, error: "spotnav_no_picture" })).toEqual({ code: "spotnav_no_picture" });
    expect(decodePicture({ ok: true, picture: { content_type: "image/png", data: PIXEL, width: 1, height: 1 } })).toEqual({ code: null });
    expect(decodePicture({ ok: true, picture: { content_type: "image/jpeg", data: "\"><script>", width: 1, height: 1 } })).toEqual({ code: null });
  });
});

describe("the settings field", () => {
  it("is decoded, kept by every other replacement, and absent on an older backend", () => {
    const record: SettingsRecord = decodeSettingsRecord(dashboard()["settings"]);
    expect(record.identify_camera).toEqual({ camera_entity_id: "camera.norr", ai_task_entity_id: null, frame: FRAME });
    expect(encodeBody(record).identify_camera).toEqual(record.identify_camera);
    const older = read("dashboard", "target_soc_two_vehicles.json")["settings"];
    delete older["identify_camera"];
    expect("identify_camera" in encodeBody(decodeSettingsRecord(older))).toBe(false);
    expect(() => decodeSettingsRecord({ ...dashboard()["settings"], identify_camera: { camera_entity_id: "sensor.x", ai_task_entity_id: null, frame: null } })).toThrow();
    expect(() => decodeSettingsRecord({ ...dashboard()["settings"], identify_camera: { camera_entity_id: "camera.x", ai_task_entity_id: null, frame: { x: 0.9, y: 0, w: 0.5, h: 1 } } })).toThrow();
  });

  it("keeps the frame with its camera, starts another camera with the whole picture, and takes the camera away", () => {
    const record = decodeSettingsRecord(dashboard()["settings"]);
    const ai = cameraReplacement(record, { cameraEntityId: "camera.norr", aiTaskEntityId: "ai_task.ollama" });
    expect(ai.ok && ai.changed && ai.body.identify_camera).toEqual({ camera_entity_id: "camera.norr", ai_task_entity_id: "ai_task.ollama", frame: FRAME });
    const other = cameraReplacement(record, { cameraEntityId: "camera.syd", aiTaskEntityId: null });
    expect(other.ok && other.body.identify_camera).toEqual({ camera_entity_id: "camera.syd", ai_task_entity_id: null, frame: null });
    const none = cameraReplacement(record, { cameraEntityId: null, aiTaskEntityId: null });
    expect(none.ok && none.body.identify_camera).toBeNull();
    const same = cameraReplacement(record, { cameraEntityId: "camera.norr", aiTaskEntityId: null });
    expect(same.ok && same.changed).toBe(false);
  });
});

describe("the dashboard block", () => {
  it("is read, and hides only the camera rows when it cannot be", () => {
    const decoded = decodeDashboard(dashboard());
    expect(decoded.ok && decoded.value.camera_identification?.references[EV6]?.map((item) => item.kind)).toEqual(["day", "night"]);
    const bad = dashboard();
    bad["camera_identification"] = { cameras: "camera.norr" };
    const still = decodeDashboard(bad);
    expect(still.ok && still.value.camera_identification).toBeNull();
    const older = read("dashboard", "target_soc_two_vehicles.json");
    const plain = decodeDashboard(older);
    expect(plain.ok && plain.value.camera_identification).toBeNull();
  });
});

describe("the Settings rows", () => {
  it("name the camera, the frame and the AI task in the charger's section", async () => {
    const { element } = await openSettings(dashboard());
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='entities']")!;
    expect(section.querySelector("[data-row='identify_camera']")?.textContent).toContain("Norr");
    expect(section.querySelector("[data-row='identify_frame']")?.textContent).toContain(translate("en", "camera.frame.drawn"));
    expect(section.querySelector("[data-row='identify_ai_task']")?.textContent).toContain(translate("en", "camera.aiTask.default"));
    // The privacy note is in the camera's editor, not under the rows.
    expect(section.querySelector("[data-help='identify_camera']")).toBeNull();
  });

  it("offer only the camera while none is chosen, and nothing where Home Assistant offers none", async () => {
    const { element } = await openSettings(dashboard(null));
    const section = openDialog(element)!.querySelector<HTMLElement>("[data-section='entities']")!;
    expect(section.querySelector("[data-row='identify_camera']")?.textContent).toContain(translate("en", "camera.none"));
    expect(section.querySelector("[data-row='identify_frame'], [data-row='identify_ai_task']")).toBeNull();
    expect(openDialog(element)!.querySelector("[data-row='reference']")).toBeNull();

    const plain = read("dashboard", "target_soc_two_vehicles.json");
    const again = await openSettings(plain);
    expect(openDialog(again.element)!.querySelector("[data-row='identify_camera']")).toBeNull();
  });

  it("choose the camera as a settings replacement", async () => {
    const payload = dashboard(null);
    const { hass, element } = await openSettings(payload);
    openDialog(element)!.querySelector<HTMLButtonElement>("[data-edit='identify_camera']")!.click();
    await settle();
    const form = openDialog(element)!.querySelector<HTMLFormElement>("form[data-value-editor='single']")!;
    expect(Array.from(form.querySelectorAll<HTMLInputElement>("[data-value-option]")).map((option) => option.value)).toEqual([
      "camera.norr", "camera.syd", "",
    ]);
    form.querySelector<HTMLInputElement>("[data-value-option='camera.syd']")!.click();
    form.requestSubmit();
    await settle();
    hass.resolveNext({
      api_version: SETTINGS_API_VERSION, ok: true, error: null, settings: payload["settings"],
      pause: { choice: null, admitted_at: null, expires_at: null },
    });
    await settle();
    const update = hass.messages.find((message) => message.type === "spotnav/update_settings") as Record<string, any>;
    expect(update["settings"]["identify_camera"]).toEqual({ camera_entity_id: "camera.syd", ai_task_entity_id: null, frame: null });
  });
});

describe("the frame editor", () => {
  async function openFrame() {
    const opened = await openSettings(dashboard());
    openDialog(opened.element)!.querySelector<HTMLButtonElement>("[data-edit='identify_frame']")!.click();
    await settle();
    const form = openDialog(opened.element)!.querySelector<HTMLFormElement>("form[data-value-editor='frame']")!;
    const stage = form.querySelector<HTMLElement>("[data-camera-stage]")!;
    stage.getBoundingClientRect = () => ({ left: 0, top: 0, width: 400, height: 150, right: 400, bottom: 150, x: 0, y: 0, toJSON: () => ({}) });
    return { ...opened, form, stage, box: form.querySelector<HTMLElement>("[data-camera-frame]")! };
  }

  function pointer(target: Element, type: string, x: number, y: number): void {
    const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y });
    Object.defineProperty(event, "pointerId", { value: 7 });
    target.dispatchEvent(event);
  }

  it("shows a fresh picture with the stored frame and a preview of it", async () => {
    const { hass, form, stage, box } = await openFrame();
    expect(hass.cameraMessages.map((message) => message.type)).toContain("spotnav/camera_snapshot");
    expect(stage.hidden).toBe(false);
    expect(form.querySelector<HTMLImageElement>("img")!.src).toContain("data:image/jpeg;base64,");
    expect(box.dataset["frame"]).toBe("0.5,0.1,0.4,0.8");
    const preview = form.querySelector<HTMLElement>("[data-camera-preview]")!;
    expect(preview.style.backgroundSize).toBe("250% 125%");
    expect(form.querySelectorAll("[data-corner]")).toHaveLength(4);
  });

  it("moves the frame by dragging it and resizes it from a corner, then saves it", async () => {
    const { hass, form, box } = await openFrame();
    pointer(box, "pointerdown", 300, 75);
    pointer(box, "pointermove", 260, 75);
    pointer(box, "pointerup", 260, 75);
    expect(box.dataset["frame"]).toBe("0.4,0.1,0.4,0.8");
    const corner = box.querySelector<HTMLElement>("[data-corner='se']")!;
    pointer(corner, "pointerdown", 320, 135);
    pointer(box, "pointermove", 340, 150);
    pointer(box, "pointerup", 340, 150);
    expect(box.dataset["frame"]).toBe("0.4,0.1,0.45,0.9");
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    expect(box.dataset["frame"]).toBe("0.39,0.1,0.45,0.9");
    form.requestSubmit();
    await settle();
    const saved = hass.cameraMessages.find((message) => message.type === "spotnav/save_camera_frame");
    expect(saved).toEqual({
      type: "spotnav/save_camera_frame", api_version: 1, charger_id: "soc_charger",
      frame: { x: 0.39, y: 0.1, w: 0.45, h: 0.9 },
    });
  });

  it("saves the whole picture as no frame", async () => {
    const { hass, form } = await openFrame();
    form.querySelector<HTMLButtonElement>("[data-camera-whole]")!.click();
    form.requestSubmit();
    await settle();
    expect(hass.cameraMessages.find((message) => message.type === "spotnav/save_camera_frame")?.["frame"]).toBeNull();
  });

  it("says when the camera gives no picture, and tries again", async () => {
    const opened = await openSettings(dashboard());
    opened.hass.cameraHandler = async () => ({ api_version: 1, ok: false, error: "spotnav_no_picture" });
    openDialog(opened.element)!.querySelector<HTMLButtonElement>("[data-edit='identify_frame']")!.click();
    await settle();
    const form = openDialog(opened.element)!.querySelector<HTMLFormElement>("form[data-value-editor='frame']")!;
    expect(form.textContent).toContain(translate("en", "camera.error.noPicture"));
    expect(form.querySelector<HTMLElement>("[data-camera-stage]")!.hidden).toBe(true);
    opened.hass.cameraHandler = async () => picture();
    form.querySelector<HTMLButtonElement>("[data-camera-retry]")!.click();
    await settle();
    expect(form.querySelector<HTMLElement>("[data-camera-stage]")!.hidden).toBe(false);
  });
});

describe("a car's reference pictures", () => {
  it("are listed with their thumbnails in the car's section", async () => {
    const { hass, element } = await openSettings(dashboard());
    const ev6 = carSection(openDialog(element)!, EV6);
    expect(ev6.querySelector("[data-row='reference']")?.textContent).toContain("Day, Night");
    const thumbs = ev6.querySelectorAll<HTMLImageElement>("[data-reference-thumbs] img");
    expect(thumbs).toHaveLength(2);
    expect(thumbs[0]!.hidden).toBe(false);
    expect(hass.cameraMessages.filter((message) => message.type === "spotnav/reference_picture")).toHaveLength(2);
    const niro = carSection(openDialog(element)!, NIRO);
    expect(niro.querySelector("[data-row='reference']")?.textContent).toContain(translate("en", "reference.none"));
  });

  /** The car's reference editor, opened from its row. */
  async function openEditor(element: Element, vehicle = EV6): Promise<HTMLFormElement> {
    carSection(openDialog(element)!, vehicle)
      .querySelector<HTMLButtonElement>("[data-edit='reference']")!
      .click();
    await settle();
    return openDialog(element)!.querySelector<HTMLFormElement>("form[data-value-editor='reference']")!;
  }
  const slot = (form: HTMLElement, kind: string): HTMLElement => form.querySelector<HTMLElement>(`[data-reference='${kind}']`)!;

  it("show a day and a night slot, each with its own button and a quiet Delete only over a picture", async () => {
    const payload = dashboard();
    payload["camera_identification"]["references"][EV6] = [{ kind: "day", taken_at: "2026-10-07T12:00:00+00:00", colour: true }];
    const { element } = await openSettings(payload, "sv");
    const form = await openEditor(element);
    expect(openDialog(element)!.querySelector("h2, h3")?.textContent).toBe("Referensbild — EV6");
    expect(form.textContent).toContain("Ta bilden när bilen står vid laddaren.");
    const day = slot(form, "day");
    const night = slot(form, "night");
    expect(day.dataset["state"]).toBe("taken");
    expect(day.querySelector("[data-reference-action='take']")?.textContent).toBe("Ta om");
    expect(day.querySelector<HTMLElement>("[data-reference-action='delete']")!.hidden).toBe(false);
    expect(day.querySelector<HTMLImageElement>("img")!.hidden).toBe(false);
    expect(night.dataset["state"]).toBe("empty");
    expect(night.textContent).toContain("Ingen bild");
    expect(night.querySelector("[data-reference-action='take']")?.textContent).toBe("Ta nattbild");
    expect(night.querySelector<HTMLElement>("[data-reference-action='delete']")!.hidden).toBe(true);
    // A car with a picture is recognised: the warning is for a car with none.
    expect(form.querySelector<HTMLElement>("[data-reference-help]")!.hidden).toBe(true);
    // The only button is Close.
    expect(form.querySelector("button[type='submit']")).toBeNull();
    expect(form.querySelector("[data-reference-close]")?.textContent).toBe("Stäng");
  });

  it("take and delete one kind in place, the dialog staying open, and read the dashboard again on Close", async () => {
    const { hass, element } = await openSettings(dashboard());
    const form = await openEditor(element, NIRO);
    expect(form.querySelector<HTMLElement>("[data-reference-help]")!.hidden).toBe(false);
    let answer: (value: unknown) => void = () => undefined;
    hass.cameraHandler = async (message) =>
      message["type"] === "spotnav/reference_picture" ? picture(240, 90) : await new Promise((resolve) => (answer = resolve));
    slot(form, "night").querySelector<HTMLButtonElement>("[data-reference-action='take']")!.click();
    await settle();
    expect(hass.cameraMessages.at(-1)).toEqual({
      type: "spotnav/take_reference_picture", api_version: 1, charger_id: "soc_charger", vehicle_id: NIRO, kind: "night",
    });
    expect(slot(form, "night").dataset["state"]).toBe("busy");
    expect(slot(form, "night").textContent).toContain(translate("en", "reference.taking"));
    expect(slot(form, "day").dataset["state"]).toBe("empty");
    answer({ api_version: 1, ok: true, error: null, vehicle_id: NIRO, references: [{ kind: "night", taken_at: "2026-10-07T21:00:00+00:00", colour: false }] });
    await settle();
    expect(openDialog(element)!.querySelector("form[data-value-editor='reference']")).toBe(form);
    expect(slot(form, "night").dataset["state"]).toBe("taken");
    expect(slot(form, "night").querySelector("[data-reference-action='take']")?.textContent).toBe(translate("en", "reference.retake"));
    expect(form.querySelector<HTMLElement>("[data-reference-help]")!.hidden).toBe(true);
    slot(form, "night").querySelector<HTMLButtonElement>("[data-reference-action='delete']")!.click();
    await settle();
    expect(hass.cameraMessages.at(-1)).toEqual({
      type: "spotnav/delete_reference_picture", api_version: 1, charger_id: "soc_charger", vehicle_id: NIRO, kind: "night",
    });
    answer({ api_version: 1, ok: true, error: null, vehicle_id: NIRO, references: [] });
    await settle();
    expect(slot(form, "night").dataset["state"]).toBe("empty");
    const reads = hass.messages.length;
    form.querySelector<HTMLButtonElement>("[data-reference-close]")!.click();
    await settle();
    expect(openDialog(element)?.querySelector("form[data-value-editor='reference']") ?? null).toBeNull();
    expect(hass.messages.length).toBeGreaterThan(reads);
    hass.resolveNext(dashboard());
    await settle();
    expect(openDialog(element)?.querySelector("[data-section='vehicle']")).not.toBeNull();
  });

  it("close on Close without a read when nothing changed", async () => {
    const { hass, element } = await openSettings(dashboard());
    const form = await openEditor(element);
    const reads = hass.messages.length;
    form.querySelector<HTMLButtonElement>("[data-reference-close]")!.click();
    await settle();
    expect(hass.messages.length).toBe(reads);
    expect(openDialog(element)?.querySelector("[data-section='vehicle']")).not.toBeNull();
  });

  it("never call a picture out of date: it is kept whole and cropped with the selection drawn now", async () => {
    const payload = dashboard();
    payload["camera_identification"]["references"][EV6][0]["stale"] = true;
    const { element } = await openSettings(payload, "sv");
    const ev6 = carSection(openDialog(element)!, EV6);
    expect(ev6.querySelector("[data-row='reference']")?.textContent).toContain("Dag, Natt");
    expect(ev6.querySelector("[data-help='reference']")).toBeNull();
  });

  it("say in the slot why a picture could not be taken", async () => {
    const { hass, element } = await openSettings(dashboard());
    hass.cameraHandler = async (message) =>
      message["type"] === "spotnav/take_reference_picture" ? { api_version: 1, ok: false, error: "spotnav_no_picture" } : picture(240, 90);
    const form = await openEditor(element, NIRO);
    slot(form, "day").querySelector<HTMLButtonElement>("[data-reference-action='take']")!.click();
    await settle();
    expect(slot(form, "day").textContent).toContain(translate("en", "camera.error.noPicture"));
    expect(slot(form, "day").dataset["state"]).toBe("empty");
    expect(slot(form, "night").textContent).not.toContain(translate("en", "camera.error.noPicture"));
  });

  it("are worded as the owner chose, in every language", () => {
    expect(translate("sv", "reference.takeDay")).toBe("Ta dagbild");
    expect(translate("sv", "reference.takeNight")).toBe("Ta nattbild");
    expect(translate("sv", "reference.retake")).toBe("Ta om");
    expect(translate("sv", "reference.empty")).toBe("Ingen bild");
    expect(translate("sv", "reference.delete")).toBe("Ta bort");
    expect(translate("sv", "reference.title", { name: "EV6" })).toBe("Referensbild — EV6");
    expect(translate("sv", "camera.frame.label")).toBe("Beskär bild laddplats");
    expect(translate("sv", "camera.frame.drawn")).toBe("Beskuren");
    expect(translate("en", "camera.frame.label")).toBe("Crop parking spot");
    for (const language of LANGUAGES) {
      expect(translate(language, "camera.frame.intro").toLowerCase()).not.toMatch(/\bruta\b|\bframe\b/);
    }
    expect(translate("sv", "camera.aiTask.label")).toBe("AI-uppgift");
    expect(translate("sv", "camera.label")).toBe("Kamera");
    const keys: TranslationKey[] = ["camera.label", "camera.frame.intro", "reference.intro", "vehicleLine.method.camera"];
    for (const language of LANGUAGES) {
      for (const key of keys) {
        expect(translate(language, key)).not.toBe("");
      }
      expect(referenceText(language, [])).toBe(translate(language, "reference.none"));
    }
  });
});
