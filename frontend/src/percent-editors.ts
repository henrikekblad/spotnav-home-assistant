// The editors the car's charge target, minimum charge level and own charge limit open: the value large, a slider
// under it, and Save and Cancel as every value editor has. Opening writes nothing; Save writes only a slider that
// was moved to something other than what is stored. The minimum's first stop is Off and it never goes past the
// target: the track beyond is hatched, with the target marked on it. The limit's slider has the range and step the
// car's integration takes.

import { formatNumber } from "./format";
import { translate, type Language } from "./i18n";
import { FLOOR_LAST_INDEX, clampFloorIndex, floorAt, floorCapIndex, floorIndex, limitOpening, limitStops } from "./percent-slider";
import { TARGET_PERCENT_MAX, TARGET_PERCENT_MIN } from "./settings";
import { shell, type EditorHandlers, type SaveValue } from "./value-editors";
import type { ChargeLimitRange } from "./validate";
import { VISUAL_CLASSES as C } from "./visual-styles";

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

function slider(
  doc: Document,
  id: string,
  label: string,
  min: number,
  max: number,
  value: number,
  step = 1,
): HTMLInputElement {
  const input = doc.createElement("input") as HTMLInputElement;
  input.type = "range";
  input.className = C.settingsSlider;
  input.id = id;
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = String(value);
  input.setAttribute("aria-label", label);
  return input;
}

const percent = (language: Language, value: number, decimals = 0): string =>
  `${formatNumber(language, value, decimals)} %`;

/** The value large, with the slider's spoken value kept the same. */
function amountLine(doc: Document, input: HTMLInputElement): { node: HTMLElement; show: (text: string, extra?: string) => void } {
  const node = element(doc, "p", C.valueAmount);
  node.dataset["part"] = "value-amount";
  node.setAttribute("aria-hidden", "true");
  return {
    node,
    show: (text, extra) => {
      node.replaceChildren(doc.createTextNode(text));
      if (extra !== undefined) {
        node.append(doc.createTextNode(" "), element(doc, "span", C.valueDefault, extra));
      }
      input.setAttribute("aria-valuetext", extra === undefined ? text : `${text} ${extra}`);
    },
  };
}

export interface TargetEditorInput {
  /** The stored target, `null` for none. */
  current: number | null;
  /** What a car with none stored is planned with: the slider opens there, marked as the default. */
  fallback: number;
  help: string;
  idPrefix: string;
}

/** The car's charge target, 0..100 % in whole percent. */
export function targetEditor(
  doc: Document,
  language: Language,
  input: TargetEditorInput,
  save: SaveValue<number>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "target", handlers);
  const opened = input.current ?? input.fallback;
  const range = slider(
    doc,
    `${input.idPrefix}-value-target`,
    translate(language, "settings.soc.slider"),
    TARGET_PERCENT_MIN,
    TARGET_PERCENT_MAX,
    Math.round(opened),
  );
  const amount = amountLine(doc, range);
  let moved = false;
  const paint = (): void => {
    if (moved) {
      amount.show(percent(language, Number(range.value)));
    } else {
      amount.show(
        percent(language, opened),
        input.current === null ? translate(language, "settings.vehicle.targetDefault") : undefined,
      );
    }
  };
  range.addEventListener("input", () => {
    moved = true;
    paint();
  });
  paint();
  const track = element(doc, "div", C.settingsTrack);
  track.append(range);
  editor.body.append(element(doc, "p", C.entityHelp, input.help), amount.node, track);
  editor.submit(async () => {
    const value = Number(range.value);
    // Never a write for the slider only having been drawn, or moved back to what is stored.
    if (!moved || value === input.current) {
      return null;
    }
    return await save(value);
  });
  return editor.form;
}

export interface FloorEditorInput {
  /** The stored minimum, `null` for Off. */
  current: number | null;
  /** The car's target (stored, or the default it is planned with): the minimum stops there. */
  target: number;
  help: string;
  idPrefix: string;
}

/** The car's minimum charge level: Off, then 10..80 % in fives, no higher than the target. */
export function floorEditor(
  doc: Document,
  language: Language,
  input: FloorEditorInput,
  save: SaveValue<number | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "floor", handlers);
  const off = translate(language, "settings.vehicle.minimumOff");
  const opened = clampFloorIndex(floorIndex(input.current), input.target);
  const range = slider(
    doc,
    `${input.idPrefix}-value-floor`,
    translate(language, "settings.vehicle.minimumSlider"),
    0,
    FLOOR_LAST_INDEX,
    opened,
  );
  const amount = amountLine(doc, range);
  let moved = false;
  const paint = (): void => {
    const level = floorAt(Number(range.value));
    amount.show(level === null ? off : percent(language, level));
  };
  range.addEventListener("input", () => {
    // Past the target the minimum cannot go: the thumb is held there.
    const held = clampFloorIndex(Number(range.value), input.target);
    if (String(held) !== range.value) {
      range.value = String(held);
    }
    moved = true;
    paint();
  });
  paint();
  const track = element(doc, "div", C.settingsTrack);
  track.append(range);
  const cap = floorCapIndex(input.target);
  if (cap < FLOOR_LAST_INDEX) {
    const from = String(cap / FLOOR_LAST_INDEX);
    const blocked = element(doc, "span", C.sliderBlocked);
    blocked.style.setProperty("--spotnav-from", from);
    blocked.setAttribute("aria-hidden", "true");
    const mark = element(
      doc,
      "span",
      C.settingsFullMark,
      translate(language, "settings.vehicle.minimumCap", { percent: formatNumber(language, input.target, 0) }),
    );
    mark.dataset["part"] = "target-mark";
    mark.style.setProperty("--spotnav-mark", from);
    mark.setAttribute("aria-hidden", "true");
    track.append(blocked, mark);
  }
  const ends = element(doc, "div", C.sliderEnds);
  ends.setAttribute("aria-hidden", "true");
  ends.append(element(doc, "span", undefined, off), element(doc, "span", undefined, percent(language, floorAt(FLOOR_LAST_INDEX) ?? 0)));
  editor.body.append(element(doc, "p", C.entityHelp, input.help), amount.node, track, ends);
  editor.submit(async () => {
    const level = floorAt(Number(range.value));
    if (!moved || level === input.current) {
      return null;
    }
    return await save(level);
  });
  return editor.form;
}

export interface LimitEditorInput {
  /** The limit the car reports now. */
  current: number;
  /** What the car's limit can be written to; `null` or absent when unknown (1..100 in whole percent). */
  range: ChargeLimitRange | null | undefined;
  help: string;
  idPrefix: string;
}

/** The car's own charge limit, over the range and step its integration takes. */
export function limitEditor(
  doc: Document,
  language: Language,
  input: LimitEditorInput,
  save: SaveValue<number>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "limit", handlers);
  const stops = limitStops(input.range);
  const decimals = Number.isInteger(stops.step) && Number.isInteger(stops.min) ? 0 : 1;
  const range = slider(
    doc,
    `${input.idPrefix}-value-limit`,
    translate(language, "settings.vehicle.limitSlider"),
    stops.min,
    stops.max,
    limitOpening(input.current, stops),
    stops.step,
  );
  const amount = amountLine(doc, range);
  let moved = false;
  const paint = (): void => {
    amount.show(percent(language, moved ? Number(range.value) : input.current, decimals));
  };
  range.addEventListener("input", () => {
    moved = true;
    paint();
  });
  paint();
  const track = element(doc, "div", C.settingsTrack);
  track.append(range);
  const ends = element(doc, "div", C.sliderEnds);
  ends.setAttribute("aria-hidden", "true");
  ends.append(
    element(doc, "span", undefined, percent(language, stops.min, decimals)),
    element(doc, "span", undefined, percent(language, limitOpening(stops.max, stops), decimals)),
  );
  editor.body.append(element(doc, "p", C.entityHelp, input.help), amount.node, track, ends);
  editor.submit(async () => {
    const value = Number(range.value);
    // Never a write for the slider only having been drawn, or moved back to the car's own limit.
    if (!moved || value === input.current) {
      return null;
    }
    return await save(value);
  });
  return editor.form;
}
