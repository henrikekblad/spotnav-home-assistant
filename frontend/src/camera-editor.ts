// The camera for vehicle identification, on the Settings page: the frame editor (a fresh picture from Home
// Assistant with a frame to drag and resize, by finger or mouse, a preview of what is inside it, and Save), the
// reference picture editor of a car (a day and a night slot, each taken, retaken or deleted in place),
// and the words its rows show. The card owns every request: an editor asks for a picture through `load` and
// hands what was chosen to `save`, which answers `null` when it took or the sentence to show. No picture is
// processed here; Home Assistant crops and scales.

import { translate, type Language, type TranslationKey } from "./i18n";
import type { CameraFrame } from "./types";
import type { ReferencePicture } from "./validate";
import { shell, type EditorHandlers, type SaveValue } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** A frame narrower or lower than this (of the picture) is refused by Home Assistant: the editor keeps above it. */
export const FRAME_MIN_SIZE = 0.05;
export const WHOLE_PICTURE: CameraFrame = { x: 0, y: 0, w: 1, h: 1 };
/** Each arrow key moves (or with Shift resizes) the frame by this much of the picture. */
const KEY_STEP = 0.01;

/** A picture as Home Assistant sent it, ready for an `<img>`. */
export interface CameraPicture {
  url: string;
  width: number;
  height: number;
}

/** A picture answer (`{ok, picture: {content_type, data, width, height}}`), or the refusal's code. */
export function decodePicture(raw: unknown): { picture: CameraPicture } | { code: string | null } {
  if (typeof raw !== "object" || raw === null) {
    return { code: null };
  }
  const answer = raw as Record<string, unknown>;
  if (answer.ok !== true) {
    return { code: typeof answer.error === "string" ? answer.error : null };
  }
  const picture = answer.picture as Record<string, unknown> | null | undefined;
  if (
    typeof picture !== "object" ||
    picture === null ||
    picture.content_type !== "image/jpeg" ||
    typeof picture.data !== "string" ||
    !/^[A-Za-z0-9+/=]+$/.test(picture.data) ||
    typeof picture.width !== "number" ||
    typeof picture.height !== "number" ||
    picture.width <= 0 ||
    picture.height <= 0
  ) {
    return { code: null };
  }
  return { picture: { url: `data:image/jpeg;base64,${picture.data}`, width: picture.width, height: picture.height } };
}

// ------------------------------------------------------------------------------------------------ frame geometry

const round = (value: number): number => Math.round(value * 10000) / 10000;

/** The frame inside the picture and at least `FRAME_MIN_SIZE` each way, rounded as Home Assistant keeps it. */
export function clampFrame(frame: CameraFrame): CameraFrame {
  const w = Math.min(Math.max(frame.w, FRAME_MIN_SIZE), 1);
  const h = Math.min(Math.max(frame.h, FRAME_MIN_SIZE), 1);
  const x = Math.min(Math.max(frame.x, 0), 1 - w);
  const y = Math.min(Math.max(frame.y, 0), 1 - h);
  return { x: round(x), y: round(y), w: round(w), h: round(h) };
}

/** The frame moved by `dx`, `dy` (fractions of the picture), its size kept, never past an edge. */
export function moveFrame(frame: CameraFrame, dx: number, dy: number): CameraFrame {
  return clampFrame({ ...frame, x: frame.x + dx, y: frame.y + dy });
}

export type Corner = "nw" | "ne" | "sw" | "se";

/** The frame with `corner` dragged by `dx`, `dy`: the opposite corner stays, never past an edge or below the least. */
export function resizeFrame(frame: CameraFrame, corner: Corner, dx: number, dy: number): CameraFrame {
  let left = frame.x;
  let top = frame.y;
  let right = frame.x + frame.w;
  let bottom = frame.y + frame.h;
  if (corner === "nw" || corner === "sw") {
    left = Math.min(Math.max(left + dx, 0), right - FRAME_MIN_SIZE);
  } else {
    right = Math.max(Math.min(right + dx, 1), left + FRAME_MIN_SIZE);
  }
  if (corner === "nw" || corner === "ne") {
    top = Math.min(Math.max(top + dy, 0), bottom - FRAME_MIN_SIZE);
  } else {
    bottom = Math.max(Math.min(bottom + dy, 1), top + FRAME_MIN_SIZE);
  }
  return clampFrame({ x: left, y: top, w: right - left, h: bottom - top });
}

/**
 * The preview's CSS: the picture scaled so the frame fills the box, and shifted to it. Background percentages
 * place the picture's point at that fraction on the box's: the frame's left edge lands on the box's left when
 * `x / (1 - w)` of the overflow is to the left.
 */
export function previewStyle(frame: CameraFrame, picture: { width: number; height: number }): {
  size: string;
  position: string;
  aspectRatio: string;
} {
  const along = (offset: number, size: number): string => (size >= 1 ? "0%" : `${round((offset / (1 - size)) * 100)}%`);
  return {
    size: `${round(100 / frame.w)}% ${round(100 / frame.h)}%`,
    position: `${along(frame.x, frame.w)} ${along(frame.y, frame.h)}`,
    aspectRatio: `${Math.max(1, Math.round(frame.w * picture.width))} / ${Math.max(1, Math.round(frame.h * picture.height))}`,
  };
}

/** Whether a frame is the whole picture (as good as). */
export function isWholePicture(frame: CameraFrame | null): boolean {
  return frame === null || (frame.x <= 0.0001 && frame.y <= 0.0001 && frame.w >= 0.9999 && frame.h >= 0.9999);
}

// ------------------------------------------------------------------------------------------------ the frame editor

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

export interface FrameEditorInput {
  /** A fresh picture from the camera, or the refusal's code. */
  load: () => Promise<{ picture: CameraPicture } | { code: string | null }>;
  frame: CameraFrame | null;
  idPrefix: string;
}

/**
 * The frame editor: the camera's picture now with the frame over it. The frame moves when dragged and resizes from
 * a corner, with a finger or the mouse (pointer events), and with the arrow keys (Shift resizes); the preview below
 * shows what will be compared. Save hands the frame (`null` for the whole picture) to `save`.
 */
export function frameEditor(
  doc: Document,
  language: Language,
  input: FrameEditorInput,
  save: SaveValue<CameraFrame | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "frame", handlers);
  const body = editor.body;
  body.append(element(doc, "p", C.entityHelp, translate(language, "camera.frame.intro")));
  const status = element(doc, "p", C.settingsNote, translate(language, "camera.frame.loading"));
  status.setAttribute("role", "status");
  body.append(status);
  let frame: CameraFrame = input.frame === null ? { ...WHOLE_PICTURE } : clampFrame(input.frame);
  let loaded: CameraPicture | null = null;

  const stage = element(doc, "div", C.cameraStage);
  stage.dataset["cameraStage"] = "true";
  stage.hidden = true;
  const image = doc.createElement("img");
  image.className = C.cameraImage;
  image.alt = translate(language, "camera.frame.pictureAlt");
  image.draggable = false;
  const box = element(doc, "div", C.cameraFrame);
  box.dataset["cameraFrame"] = "true";
  box.tabIndex = 0;
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", translate(language, "camera.frame.area"));
  const corners: Corner[] = ["nw", "ne", "sw", "se"];
  for (const corner of corners) {
    const handle = element(doc, "div", C.cameraHandle);
    handle.dataset["corner"] = corner;
    box.append(handle);
  }
  stage.append(image, box);
  const previewLabel = element(doc, "p", C.settingRowHelp, translate(language, "camera.frame.preview"));
  previewLabel.hidden = true;
  const preview = element(doc, "div", C.cameraPreview);
  preview.dataset["cameraPreview"] = "true";
  preview.hidden = true;
  preview.setAttribute("role", "img");
  preview.setAttribute("aria-label", translate(language, "camera.frame.preview"));
  const tools = element(doc, "div", C.cameraTools);
  const whole = element(doc, "button", C.button, translate(language, "camera.frame.whole")) as HTMLButtonElement;
  whole.type = "button";
  whole.dataset["cameraWhole"] = "true";
  const retry = element(doc, "button", C.button, translate(language, "camera.frame.retry")) as HTMLButtonElement;
  retry.type = "button";
  retry.dataset["cameraRetry"] = "true";
  retry.hidden = true;
  tools.append(whole, retry);
  body.append(stage, tools, previewLabel, preview);

  const paint = (): void => {
    box.style.left = `${frame.x * 100}%`;
    box.style.top = `${frame.y * 100}%`;
    box.style.width = `${frame.w * 100}%`;
    box.style.height = `${frame.h * 100}%`;
    box.dataset["frame"] = `${frame.x},${frame.y},${frame.w},${frame.h}`;
    if (loaded !== null) {
      const style = previewStyle(frame, loaded);
      preview.style.backgroundImage = `url("${loaded.url}")`;
      preview.style.backgroundSize = style.size;
      preview.style.backgroundPosition = style.position;
      preview.style.aspectRatio = style.aspectRatio;
    }
  };

  // One drag at a time: the pointer that started it, where it started, and the frame then.
  let drag: { pointer: number; corner: Corner | null; startX: number; startY: number; start: CameraFrame } | null = null;
  const fraction = (dxPx: number, dyPx: number): [number, number] => {
    const rect = stage.getBoundingClientRect();
    return [rect.width > 0 ? dxPx / rect.width : 0, rect.height > 0 ? dyPx / rect.height : 0];
  };
  box.addEventListener("pointerdown", (event) => {
    if (drag !== null || loaded === null) {
      return;
    }
    const target = event.target as HTMLElement | null;
    const corner = (target?.dataset["corner"] as Corner | undefined) ?? null;
    drag = { pointer: event.pointerId, corner, startX: event.clientX, startY: event.clientY, start: frame };
    box.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  box.addEventListener("pointermove", (event) => {
    if (drag === null || event.pointerId !== drag.pointer) {
      return;
    }
    const [dx, dy] = fraction(event.clientX - drag.startX, event.clientY - drag.startY);
    frame = drag.corner === null ? moveFrame(drag.start, dx, dy) : resizeFrame(drag.start, drag.corner, dx, dy);
    paint();
    event.preventDefault();
  });
  const end = (event: PointerEvent): void => {
    if (drag !== null && event.pointerId === drag.pointer) {
      box.releasePointerCapture?.(event.pointerId);
      drag = null;
    }
  };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);
  box.addEventListener("keydown", (event) => {
    const steps: Record<string, [number, number]> = {
      ArrowLeft: [-KEY_STEP, 0],
      ArrowRight: [KEY_STEP, 0],
      ArrowUp: [0, -KEY_STEP],
      ArrowDown: [0, KEY_STEP],
    };
    const step = steps[event.key];
    if (step === undefined || loaded === null) {
      return;
    }
    frame = event.shiftKey ? resizeFrame(frame, "se", step[0], step[1]) : moveFrame(frame, step[0], step[1]);
    paint();
    event.preventDefault();
  });
  whole.addEventListener("click", () => {
    frame = { ...WHOLE_PICTURE };
    paint();
  });

  const fetchPicture = (): void => {
    status.hidden = false;
    status.textContent = translate(language, "camera.frame.loading");
    retry.hidden = true;
    void input.load().then((answer) => {
      if ("picture" in answer) {
        loaded = answer.picture;
        image.src = answer.picture.url;
        image.width = answer.picture.width;
        image.height = answer.picture.height;
        stage.hidden = false;
        preview.hidden = false;
        previewLabel.hidden = false;
        status.hidden = true;
        paint();
      } else {
        status.textContent = translate(language, cameraErrorKey(answer.code));
        retry.hidden = false;
      }
    });
  };
  retry.addEventListener("click", fetchPicture);
  paint();
  fetchPicture();

  editor.submit(async () => {
    if (loaded === null) {
      return translate(language, "camera.frame.loading");
    }
    return await save(isWholePicture(frame) ? null : clampFrame(frame));
  });
  return editor.form;
}

// ------------------------------------------------------------------------------------------------ entity choices

/**
 * How each camera or AI Task entity is told apart: its name alone when no other has it; otherwise the name with
 * what tells it apart (its model, else its entity id) as `detail`.
 */
export function entityLabels(
  choices: ReadonlyArray<{ entity_id: string; name: string; model: string | null }>,
): Map<string, { name: string; detail: string | null }> {
  const counts = new Map<string, number>();
  for (const choice of choices) {
    counts.set(choice.name, (counts.get(choice.name) ?? 0) + 1);
  }
  const models = new Map<string, number>();
  for (const choice of choices) {
    if (choice.model !== null) {
      const key = `${choice.name}\u0000${choice.model}`;
      models.set(key, (models.get(key) ?? 0) + 1);
    }
  }
  return new Map<string, { name: string; detail: string | null }>(
    choices.map((choice): [string, { name: string; detail: string | null }] => {
      if ((counts.get(choice.name) ?? 0) < 2) {
        return [choice.entity_id, { name: choice.name, detail: null }];
      }
      // A model two of them share does not tell them apart: the entity id does.
      const model = choice.model;
      const unique = model !== null && (models.get(`${choice.name}\u0000${model}`) ?? 0) < 2;
      return [choice.entity_id, { name: choice.name, detail: unique ? model : choice.entity_id }];
    }),
  );
}

// ------------------------------------------------------------------------------------------------ reference pictures

/** The words a car's "Reference picture" row shows: which pictures it has. */
export function referenceText(language: Language, pictures: readonly ReferencePicture[]): string {
  const kinds = pictures.map((picture) => translate(language, picture.kind === "day" ? "reference.day" : "reference.night"));
  return kinds.length === 0 ? translate(language, "reference.none") : kinds.join(", ");
}

export type PictureKind = "day" | "night";

/** What a slot's button or delete answered: the car's pictures after it, or the sentence to show in that slot. */
export type ReferenceAnswer = { pictures: ReferencePicture[] } | { message: string };

export interface ReferenceEditorInput {
  pictures: readonly ReferencePicture[];
  /** A picture's thumbnail, or `null` when there is none. */
  thumbnail: (picture: ReferencePicture) => Promise<CameraPicture | null>;
  /** Take one kind's picture now, or delete that kind's picture. */
  act: (kind: PictureKind, action: "take" | "delete") => Promise<ReferenceAnswer>;
  formatTaken: (iso: string) => string;
  /** Close: `changed` when a picture was taken or deleted here. */
  onClose: (changed: boolean) => void;
}

const KINDS: readonly PictureKind[] = ["day", "night"];

/**
 * A car's reference pictures, one slot each for day and night: its thumbnail and when it was taken, or "No
 * picture"; one button that takes it ("Take day picture", "Take night picture", or "Retake" over a picture); and a
 * quiet Delete with a picture. A slot shows its own progress and failure, and a picture taken or deleted updates
 * its slot in place. The editor's one button is Close.
 */
export function referenceEditor(
  doc: Document,
  language: Language,
  input: ReferenceEditorInput,
): { form: HTMLFormElement; close: () => void } {
  const say = (key: TranslationKey): string => translate(language, key);
  const form = element(doc, "form") as HTMLFormElement;
  form.noValidate = true;
  form.dataset["valueEditor"] = "reference";
  form.addEventListener("submit", (event) => event.preventDefault());
  form.append(element(doc, "p", C.entityHelp, say("reference.intro")));
  const help = element(doc, "p", C.settingRowHelp, say("reference.help"));
  help.dataset["referenceHelp"] = "true";
  const slots = element(doc, "div", C.referenceSlots);
  form.append(slots, help);
  let pictures: readonly ReferencePicture[] = input.pictures;
  let changed = false;
  const busy = new Set<PictureKind>();
  const paints = new Map<PictureKind, (error: string | null) => void>();

  for (const kind of KINDS) {
    const slot = element(doc, "section", C.referenceSlot);
    slot.dataset["reference"] = kind;
    const heading = element(doc, "h4", C.referenceSlotTitle, say(kind === "day" ? "reference.day" : "reference.night"));
    const frame = element(doc, "div", C.referenceSlotPicture);
    const image = doc.createElement("img");
    image.className = C.referenceSlotImage;
    image.alt = say(kind === "day" ? "reference.day" : "reference.night");
    const empty = element(doc, "span", C.referenceSlotEmpty, say("reference.empty"));
    frame.append(image, empty);
    const caption = element(doc, "p", C.settingRowHelp);
    caption.dataset["referenceTaken"] = kind;
    const status = element(doc, "p", C.referenceSlotStatus);
    status.setAttribute("role", "status");
    const take = element(doc, "button", C.button) as HTMLButtonElement;
    take.type = "button";
    take.dataset["referenceAction"] = "take";
    const remove = element(doc, "button", C.referenceDelete, say("reference.delete")) as HTMLButtonElement;
    remove.type = "button";
    remove.dataset["referenceAction"] = "delete";
    remove.setAttribute("aria-label", `${say("reference.delete")}: ${heading.textContent}`);
    const tools = element(doc, "div", C.referenceSlotTools);
    tools.append(take, remove);
    slot.append(heading, frame, caption, status, tools);
    slots.append(slot);

    // The thumbnail asked for last: an older answer for a picture since replaced is not shown.
    let shownFor: string | null = null;
    const paint = (error: string | null): void => {
      const picture = pictures.find((item) => item.kind === kind);
      const working = busy.has(kind);
      slot.dataset["state"] = working ? "busy" : picture === undefined ? "empty" : "taken";
      take.textContent = say(picture !== undefined ? "reference.retake" : kind === "day" ? "reference.takeDay" : "reference.takeNight");
      take.disabled = working;
      remove.hidden = picture === undefined;
      remove.disabled = working;
      // An empty slot keeps the caption's line, so both slots' buttons stand level.
      caption.textContent = picture === undefined ? "\u00a0" : input.formatTaken(picture.taken_at);
      caption.style.visibility = picture === undefined ? "hidden" : "";
      status.hidden = !working && error === null;
      status.textContent = working ? say("reference.taking") : (error ?? "");
      status.classList.toggle(C.referenceSlotError, !working && error !== null);
      if (picture === undefined) {
        shownFor = null;
        image.hidden = true;
        image.removeAttribute("src");
        empty.hidden = false;
        return;
      }
      if (shownFor !== picture.taken_at) {
        shownFor = picture.taken_at;
        const wanted = picture.taken_at;
        image.hidden = true;
        empty.hidden = true;
        void input.thumbnail(picture).then((thumbnail) => {
          if (shownFor === wanted && thumbnail !== null) {
            image.src = thumbnail.url;
            image.hidden = false;
          }
        });
      }
    };
    paints.set(kind, paint);
    const act = (action: "take" | "delete"): void => {
      if (busy.has(kind)) {
        return;
      }
      busy.add(kind);
      paint(null);
      void input.act(kind, action).then((answer) => {
        busy.delete(kind);
        if ("pictures" in answer) {
          changed = true;
          pictures = answer.pictures;
          help.hidden = pictures.length > 0;
          for (const [other, repaint] of paints) {
            if (other === kind || !busy.has(other)) {
              repaint(null);
            }
          }
        } else {
          paint(answer.message);
        }
      });
    };
    take.addEventListener("click", () => act("take"));
    remove.addEventListener("click", () => act("delete"));
    paint(null);
  }
  help.hidden = pictures.length > 0;

  const actions = element(doc, "div", C.settingsActions);
  const close = element(doc, "button", C.button, say("dialog.close")) as HTMLButtonElement;
  close.type = "button";
  close.dataset["referenceClose"] = "true";
  close.addEventListener("click", () => input.onClose(changed));
  actions.append(close);
  form.append(actions);
  // Escape, the backdrop and the dialog's cross close it as Close does.
  return { form, close: () => input.onClose(changed) };
}

/** The sentence for a camera refusal. */
export function cameraErrorKey(code: string | null): TranslationKey {
  if (code === "spotnav_no_camera") {
    return "camera.error.noCamera";
  }
  if (code === "spotnav_no_picture") {
    return "camera.error.noPicture";
  }
  if (code === "spotnav_not_admin") {
    return "settings.error.readOnly";
  }
  return "settings.error.generic";
}
