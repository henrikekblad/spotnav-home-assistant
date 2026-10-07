// The three focused planning editors as standards-based controls (`input type="range"`, `"time"`,
// `"checkbox"`; no private Home Assistant components). One module for each resting trigger and the
// dialog body it opens. Nothing here talks to the backend or judges a value; `settings.ts` and the
// card own every judgement and request.

import { ageSentence as sharedAgeSentence } from "./vehicle-line";
import { energyAmount, formatFixed, formatNumber, percentAmount } from "./format";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import {
  CURRENT_SLIDER_STEP_A,
  ENERGY_SLIDER_MIN_KWH,
  ENERGY_SLIDER_STEP_KWH,
  TARGET_PERCENT_MAX,
  TARGET_PERCENT_MIN,
  energyFillTop,
  energySliderMaximum,
  nominalPowerKw,
  type CurrentRange,
  type DepartureDays,
  type SettingsEditorKind,
  type SettingsFormValues,
} from "./settings";
import { VISUAL_CLASSES as C, summaryValueClass } from "./visual-styles";
import { floorSegment, placeMarks, targetTicks } from "./percent-slider";
import { chargeCeiling, effectiveTarget, pythonRound, pythonRoundedAbove, targetNeedKwh } from "./target-need";
import type { Soc, Vehicle } from "./validate";

export interface SettingsEditorForm {
  kind: SettingsEditorKind;
  readOnly: boolean;
  values: SettingsFormValues;
  energyReadOnly: boolean;
  /** Whether the record has `fill_to_limit`, so the kWh slider's last step can be "Fill". */
  fillSupported?: boolean;
  /** Each car's own target (the same at every charger): choosing a car in the target mode shows its own. */
  vehicleTargets?: Readonly<Record<string, number>>;
  /**
   * The phases a charge uses (the record's `phases`, which the server fills in as the effective count): it
   * names the nominal power the draft current gives. Read-only; `null` means unknown.
   */
  phases: number | null;
  /** `"vehicle"` when the planned vehicle's onboard charger, not the charger's wiring, sets `phases`. */
  limitedBy?: "vehicle" | null;
  currentRange: CurrentRange;
  conflict: number | null;
  /**
   * The state of charge a target is planned against, from the last accepted dashboard (`null` when no
   * charge-level source resolves). It drives the Plan popover's target mode only.
   */
  soc: Soc | null;
  vehicles: readonly Vehicle[];
  /**
   * What the departure date picker offers (today..+7 in the market's zone, and where a date starts), or
   * `null` while the zone is unknown: then a date can be seen and cleared but not chosen.
   */
  days?: DepartureDays | null;
}

export interface SettingsEditorHandlers {
  onSave: (values: SettingsFormValues) => void;
  onCancel?: () => void;
  onReload: () => void;
  onReapply: (values: SettingsFormValues) => void;
}

export interface SettingsEditorBody {
  body: HTMLElement;
  values: () => SettingsFormValues;
}

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

/**
 * One trigger's icon: a small path-only SVG built with `createElementNS`, `aria-hidden` because the
 * button's label carries the meaning.
 */
export function settingsIcon(doc: Document, kind: SettingsEditorKind): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "18");
  svg.setAttribute("height", "18");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add(C.settingsIcon);
  const path = doc.createElementNS(ns, "path");
  if (kind === "energy") {
    path.setAttribute("d", "M3 8h15v8H3zM18 10h2v4h-2zM6 10h3v4H6z");
    path.setAttribute("fill", "currentColor");
  } else if (kind === "deadline") {
    path.setAttribute("d", "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zm0 3v5l3 2");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-linecap", "round");
  } else {
    path.setAttribute("d", "M13 2 4 14h6l-1 8 9-12h-6l1-8Z");
    path.setAttribute("fill", "currentColor");
  }
  svg.append(path);
  return svg;
}

/**
 * One resting trigger: icon, the value (`20 kWh`, `06:30`, `10 A`) and an accessible name that says
 * which field it is. No product name, phase value, revision or entity id belongs here.
 */
export function settingsTrigger(
  doc: Document,
  language: Language,
  kind: SettingsEditorKind,
  value: string,
): HTMLButtonElement {
  const button = doc.createElement("button") as HTMLButtonElement;
  button.type = "button";
  button.className = `${C.button} ${C.settingsTrigger}`;
  button.dataset["setting"] = kind;
  button.setAttribute(
    "aria-label",
    translate(language, `settings.${kind}.aria` as TranslationKey, { value }),
  );
  button.append(settingsIcon(doc, kind));
  button.append(element(doc, "span", C.settingsValue, value));
  return button;
}

function field(
  doc: Document,
  id: string,
  labelText: string,
  control: HTMLElement,
): HTMLElement {
  control.id = id;
  const wrapper = element(doc, "div", C.settingsField);
  const label = element(doc, "label", C.settingsLabel, labelText);
  label.setAttribute("for", id);
  wrapper.append(label, control);
  return wrapper;
}

function checkboxField(doc: Document, id: string, labelText: string, control: HTMLInputElement): HTMLElement {
  control.id = id;
  const wrapper = element(doc, "div", C.settingsCheckRow);
  const label = element(doc, "label", C.settingsLabel, labelText);
  label.setAttribute("for", id);
  wrapper.append(control, label);
  return wrapper;
}

function rangeInput(
  doc: Document,
  options: { min: number; max: number; step: number; value: number; label: string },
): HTMLInputElement {
  const input = doc.createElement("input") as HTMLInputElement;
  input.type = "range";
  input.className = C.settingsSlider;
  input.min = String(options.min);
  input.max = String(options.max);
  input.step = String(options.step);
  input.value = String(options.value);
  input.setAttribute("aria-label", options.label);
  return input;
}

/**
 * The value the energy editor states on its label row: a slider amount with one decimal (`20.0 kWh`), a
 * stored amount off the half-kWh step exactly (`7.3 kWh`, `20.25 kWh`), "Not set" for none.
 */
function energyRowValue(language: Language, value: number): string {
  if (!Number.isFinite(value)) {
    return translate(language, "settings.energy.unset");
  }
  const tenths = Math.abs(value * 10 - Math.round(value * 10)) < 1e-9;
  return `${tenths ? formatFixed(language, value, 1) : formatNumber(language, value, 3)} kWh`;
}

/** The slider's step nearest a value, within its own interval (its minimum for no value). */
function nearestStep(value: number, minimum: number, step: number, maximum: number): number {
  if (!Number.isFinite(value)) {
    return minimum;
  }
  const at = minimum + Math.round((value - minimum) / step) * step;
  const top = minimum + Math.floor((maximum - minimum) / step + 1e-9) * step;
  return Math.min(top, Math.max(minimum, at));
}

/**
 * A field whose only control is its slider (the requested energy, the charge target,
 * the planned current), as the app draws it:
 * the label and the value the slider stands for share one row, the value at its end, and the slider takes
 * the full width under them, in a track a mark can be drawn on. The slider opens at the step nearest
 * `value`; the caller keeps the exact value and writes the text with `show`.
 */
function sliderRow(
  doc: Document,
  options: {
    id: string;
    labelText: string;
    sliderLabel: string;
    minimum: number;
    step: number;
    maximum: number;
    value: number;
    readOnly: boolean;
    /** The value text's `data-part`. */
    part: string;
  },
): { group: HTMLElement; slider: HTMLInputElement; track: HTMLElement; show: (text: string) => void } {
  const slider = rangeInput(doc, {
    min: options.minimum,
    max: options.maximum,
    step: options.step,
    value: options.minimum,
    label: options.sliderLabel,
  });
  slider.id = options.id;
  slider.value = String(nearestStep(options.value, options.minimum, options.step, options.maximum));
  slider.disabled = options.readOnly;
  const label = element(doc, "label", C.settingsLabel, options.labelText);
  label.id = `${options.id}-label`;
  label.setAttribute("for", options.id);
  const amount = element(doc, "span", C.settingsAmount);
  amount.dataset["part"] = options.part;
  const head = element(doc, "div", C.settingsHead);
  head.append(label, amount);
  const track = element(doc, "div", C.settingsTrack);
  track.append(slider);
  const group = element(doc, "div", C.settingsField);
  group.setAttribute("role", "group");
  group.setAttribute("aria-labelledby", label.id);
  group.append(head, track);
  const show = (text: string): void => {
    amount.textContent = text;
    slider.setAttribute("aria-valuetext", text);
  };
  return { group, slider, track, show };
}

/**
 * The body of one focused editor: owned fields only (energy one, deadline three, current one). A
 * read-only form (non-administrator, or energy while the driver is target-SoC) has no Save at all.
 */
export function settingsEditorBody(
  doc: Document,
  language: Language,
  form: SettingsEditorForm,
  handlers: SettingsEditorHandlers,
  idPrefix: string,
): SettingsEditorBody {
  const body = element(doc, "div");
  const introKey = `settings.${form.kind}.intro` as TranslationKey;
  body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(language, introKey)));
  if (form.readOnly) {
    body.append(element(doc, "p", C.settingsReadOnly, translate(language, "settings.readOnly")));
  }

  const values: SettingsFormValues = { ...form.values };
  // The requested energy exactly as stored, until the slider moves: the energy editor has no number field,
  // so an amount off the slider's step (`7.3` from the app) is kept as it is by a Save that does not move it.
  let energyValue = form.values.energy;
  const enabledInput = doc.createElement("input") as HTMLInputElement;
  enabledInput.type = "checkbox";
  enabledInput.className = C.settingsInput;
  enabledInput.checked = form.values.deadlineEnabled;
  const timeInput = doc.createElement("input") as HTMLInputElement;
  timeInput.type = "time";
  timeInput.className = C.settingsInput;
  timeInput.value = form.values.deadlineTime;
  const dateInput = doc.createElement("input") as HTMLInputElement;
  dateInput.type = "date";
  dateInput.className = C.settingsInput;
  if (form.days != null) {
    dateInput.min = form.days.today;
    dateInput.max = form.days.max;
  }
  dateInput.value = form.values.departureDate;
  // The planned current exactly as stored, until its slider moves: like the energy, it has no number field,
  // and a Save that does not move it keeps it as it is. (The charge periods are a charger setting.)
  let currentValue = form.values.current;
  const storedNumber = (text: string): number => {
    const trimmed = text.trim();
    return trimmed === "" ? Number.NaN : Number(trimmed.replace(",", "."));
  };

  const appendEnergy = (into: HTMLElement = body): void => {
    if (form.energyReadOnly && form.kind !== "plan") {
      // The same stored amount the resting row states, spelled the same way; the exact machine text
      // stays on the dataset.
      const machine = form.values.energy.trim();
      const amount = machine === "" ? Number.NaN : Number(machine);
      const readOnlyValue = element(
        doc,
        "p",
        C.settingsReadOnly,
        Number.isFinite(amount) ? energyAmount(language, amount) : form.values.energy,
      );
      readOnlyValue.dataset["energy"] = form.values.energy;
      into.append(readOnlyValue);
      into.append(element(doc, "p", C.settingsNote, translate(language, "settings.energy.targetSoc")));
      return;
    }
    // The battery's room, when the dashboard states it: a "full" mark on the track, a top past it (twice the
    // room, at least 30 kWh, never past the battery to the car's limit) and a last step that is "Fill".
    const soc = form.soc ?? null;
    const room = soc?.room_kwh ?? null;
    const top = soc === null ? null : energyFillTop(soc);
    const fillable = top !== null && form.fillSupported === true;
    let fill = values.fill;
    const limit = soc?.vehicle_max_percent ?? null;
    const limitText =
      limit !== null && chargeCeiling(limit) < 100
        ? translate(language, "settings.energy.limitSuffix", { percent: formatNumber(language, chargeCeiling(limit), 0) })
        : "";
    const id = `${idPrefix}-energy`;
    const roomHelp = element(doc, "p", C.settingsNote);
    roomHelp.id = `${id}-room`;
    roomHelp.dataset["note"] = "energy-room";
    roomHelp.hidden = true;
    const fullMark = element(doc, "span", C.settingsFullMark, translate(language, "settings.energy.fullMark"));
    fullMark.dataset["part"] = "full-mark";
    fullMark.setAttribute("aria-hidden", "true");
    fullMark.hidden = true;
    const atFill = (): boolean => fillable && fill;
    const minimum = ENERGY_SLIDER_MIN_KWH;
    const stored = (): number => {
      const text = energyValue.trim();
      return text === "" ? Number.NaN : Number(text.replace(",", "."));
    };

    // Where the slider stands on opening: the last step for a stored Fill, else the step nearest the stored
    // amount, on a track that reaches a stored amount above its ordinary top. Moving it never shrinks that.
    const opensFilled = atFill() && top !== null;
    const maximum = opensFilled ? top : energySliderMaximum(stored(), top);
    const row = sliderRow(doc, {
      id,
      labelText: translate(language, "settings.energy.label"),
      // The slider's own range, its top the Fill top (or a stored amount above it) rather than 100 kWh.
      sliderLabel: translate(language, "settings.energy.slider", {
        min: formatNumber(language, minimum, 1),
        max: formatNumber(language, maximum, 1),
      }),
      minimum,
      step: ENERGY_SLIDER_STEP_KWH,
      maximum,
      value: opensFilled ? top : stored(),
      readOnly: form.readOnly,
      part: "energy-value",
    });
    const { slider } = row;
    slider.setAttribute("aria-describedby", roomHelp.id);
    row.track.append(fullMark);

    /** The value text, its accessible copy, the "full" mark and the line under the slider. */
    const describe = (): void => {
      const filling = atFill();
      row.show(filling ? translate(language, "settings.energy.fill") : energyRowValue(language, stored()));
      if (room === null || top === null) {
        fullMark.hidden = true;
        roomHelp.hidden = true;
        return;
      }
      const maximum = Number(slider.max);
      const at = maximum > minimum ? Math.min(1, Math.max(0, (room - minimum) / (maximum - minimum))) : 0;
      fullMark.style.setProperty("--spotnav-mark", String(at));
      fullMark.hidden = false;
      const kwh = formatNumber(language, room, 1);
      roomHelp.textContent = filling
        ? translate(language, "settings.energy.fillHelp", { kwh, limit: limitText })
        : translate(language, "settings.energy.roomHelp", { kwh, limit: limitText });
      roomHelp.hidden = false;
    };

    slider.addEventListener("input", () => {
      const value = Number(slider.value);
      // The last step of a slider whose top is the ordinary one (not a stored amount drawn above it).
      fill = top !== null && Number(slider.max) === top && value >= top - 1e-9;
      values.fill = fill;
      energyValue = slider.value;
      describe();
    });
    into.append(row.group, roomHelp);
    describe();
  };

  /**
   * The note under a slider at its top (a target at or above the car's own limit, or every kWh the battery
   * has room for): the car, not SpotNav, ends that charge. Hidden until shown.
   */
  function carEndsNote(limit: number | null): HTMLElement {
    const note = element(
      doc,
      "p",
      C.settingsNote,
      translate(language, "settings.carEndsCharge", { percent: formatNumber(language, chargeCeiling(limit), 0) }),
    );
    note.hidden = true;
    return note;
  }

  /**
   * The departure day below the time: "Every day" (no date) or "On a date" (up to seven days ahead). Choosing
   * the date starts at the next occurrence of the time. Appended to `into`.
   */
  const appendDate = (into: HTMLElement): void => {
    const days = form.days ?? null;
    const weekdays = weekdayGroup();
    if (days === null && dateInput.value === "") {
      into.append(weekdays);
      return;
    }
    const group = element(doc, "fieldset", C.siteFieldset);
    group.dataset["part"] = "departure-date";
    group.append(element(doc, "legend", C.siteLegend, translate(language, "settings.deadline.date")));
    const radioName = `${idPrefix}-departure-day`;
    const dailyRadio = doc.createElement("input") as HTMLInputElement;
    const dateRadio = doc.createElement("input") as HTMLInputElement;
    const choice = (radio: HTMLInputElement, value: string, labelKey: TranslationKey): HTMLElement => {
      radio.type = "radio";
      radio.name = radioName;
      radio.value = value;
      radio.disabled = form.readOnly;
      const label = element(doc, "label", C.siteChoice);
      label.append(radio, doc.createTextNode(translate(language, labelKey)));
      return label;
    };
    dailyRadio.dataset["departureDay"] = "daily";
    dateRadio.dataset["departureDay"] = "date";
    dateRadio.checked = dateInput.value !== "";
    dailyRadio.checked = !dateRadio.checked;
    // The date field sits on the "On a date" row, right-aligned, and is shown only while that row is chosen.
    const dateRow = element(doc, "div");
    dateRow.dataset["part"] = "departure-date-row";
    dateRow.style.cssText = "display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap";
    dateRow.append(choice(dateRadio, "date", "settings.deadline.dateOn"), dateInput);
    group.append(choice(dailyRadio, "daily", "settings.deadline.dateDaily"), dateRow);
    dateInput.id = `${idPrefix}-deadline-date`;
    dateInput.setAttribute("aria-label", translate(language, "settings.deadline.dateOn"));
    const note = element(doc, "p", C.settingsNote);
    note.dataset["departureDateNote"] = "true";
    const help = element(doc, "p", C.settingsNote, translate(language, "settings.deadline.dateHelp"));
    help.dataset["departureDateHelp"] = "true";
    const dated = element(doc, "div");
    dated.dataset["part"] = "departure-date-picker";
    dated.append(note, help);
    const paint = (): void => {
      const value = dateInput.value;
      dated.hidden = !dateRadio.checked;
      dateInput.hidden = !dateRadio.checked;
      dateInput.disabled = form.readOnly || days === null;
      // A date that has gone by is ignored by planning and forgotten by the next save.
      const gone = days !== null && dateRadio.checked && value !== "" && value < days.today;
      note.hidden = !gone;
      note.textContent = gone ? translate(language, "settings.deadline.datePast") : "";
    };
    dateRadio.addEventListener("change", () => {
      if (dateRadio.checked && dateInput.value === "" && days !== null) {
        dateInput.value = days.nextOccurrence(timeInput.value);
      }
      paint();
    });
    dailyRadio.addEventListener("change", () => {
      if (dailyRadio.checked) {
        dateInput.value = "";
      }
      paint();
    });
    dateInput.addEventListener("input", paint);
    dateInput.addEventListener("change", paint);
    into.append(group, dated, weekdays);
    // The weekdays belong to the daily departure: a chosen date overrides them.
    const paintWeekdays = (): void => {
      weekdays.hidden = dateRadio.checked;
    };
    dateRadio.addEventListener("change", paintWeekdays);
    dailyRadio.addEventListener("change", paintWeekdays);
    paintWeekdays();
    paint();
  };

  /** The weekdays a daily departure applies on: one toggle per day, Monday first, named in the card's language. */
  const weekdayChecks: HTMLInputElement[] = [];
  const weekdayGroup = (): HTMLElement => {
    const group = element(doc, "fieldset", C.siteFieldset);
    group.dataset["part"] = "departure-weekdays";
    group.append(element(doc, "legend", C.siteLegend, translate(language, "settings.deadline.weekdays")));
    const chosen = new Set([...form.values.departureWeekdays].map(Number));
    const row = element(doc, "div");
    row.style.cssText = "display:flex;flex-wrap:wrap;gap:4px 12px";
    for (let day = 1; day <= 7; day += 1) {
      const check = doc.createElement("input") as HTMLInputElement;
      check.type = "checkbox";
      check.value = String(day);
      check.checked = chosen.has(day);
      check.disabled = form.readOnly;
      check.dataset["weekday"] = String(day);
      weekdayChecks.push(check);
      const label = element(doc, "label", C.siteChoice);
      label.append(check, doc.createTextNode(weekdayName(language, day)));
      row.append(label);
    }
    group.append(row, element(doc, "p", C.settingsNote, translate(language, "settings.deadline.weekdaysHelp")));
    return group;
  };

  const appendDeadline = (): void => {
    enabledInput.disabled = form.readOnly;
    timeInput.disabled = form.readOnly;
    body.append(
      checkboxField(doc, `${idPrefix}-deadline-enabled`, translate(language, "settings.deadline.enabled"), enabledInput),
    );
    // The departure (time and day) only exists while the deadline is on; off, the time is today.
    const departure = element(doc, "div");
    departure.dataset["part"] = "departure";
    departure.append(field(doc, `${idPrefix}-deadline-time`, translate(language, "settings.deadline.time"), timeInput));
    appendDate(departure);
    departure.hidden = !enabledInput.checked;
    enabledInput.addEventListener("change", () => {
      departure.hidden = !enabledInput.checked;
    });
    body.append(departure);
  };

  // The phases a charge uses are not chosen here: the charger's wiring and the vehicle's onboard charger decide
  // them. The line beside the current says how many, with the power that gives, and why when the car limits it.
  const phasesLabel = (count: number): string =>
    translate(language, count === 1 ? "settings.phases.one" : "settings.phases.three");

  const appendCurrent = (): void => {
    const power = element(doc, "p", C.settingsPower);
    power.setAttribute("aria-live", "polite");
    const paintPower = (): void => {
      const amps = storedNumber(currentValue);
      const phases = form.phases;
      const nominal = nominalPowerKw(amps, phases);
      power.dataset["phases"] = phases === null ? "" : String(phases);
      if (phases !== 1 && phases !== 3) {
        // A count this release does not know: no phases and no power are named.
        power.textContent = translate(language, "settings.current.powerUnknown");
        return;
      }
      power.textContent =
        nominal === null
          ? translate(language, "settings.phases.lineUnknown", { phases: phasesLabel(phases) })
          : translate(language, "settings.phases.line", {
              phases: phasesLabel(phases),
              power: formatNumber(language, nominal, 1),
            });
    };
    const currentRow = sliderRow(doc, {
      id: `${idPrefix}-current`,
      labelText: translate(language, "settings.current.label"),
      sliderLabel: translate(language, "settings.current.slider", {
        min: formatNumber(language, form.currentRange.minA, 0),
        max: formatNumber(language, form.currentRange.maxA, 0),
      }),
      minimum: form.currentRange.minA,
      step: CURRENT_SLIDER_STEP_A,
      maximum: form.currentRange.maxA,
      value: storedNumber(currentValue),
      readOnly: form.readOnly,
      part: "current-value",
    });
    // The current as stored, exactly (`10 A`, a stored `80 A` outside the charger's range), or as the slider
    // sets it once moved.
    const showCurrent = (): void => {
      const amps = storedNumber(currentValue);
      currentRow.show(
        Number.isFinite(amps) ? `${formatNumber(language, amps, 3)} A` : translate(language, "settings.value.unset"),
      );
    };
    currentRow.slider.addEventListener("input", () => {
      currentValue = currentRow.slider.value;
      showCurrent();
      paintPower();
    });
    showCurrent();
    body.append(currentRow.group);
    body.append(power);
    if (form.limitedBy === "vehicle") {
      const reason = element(doc, "p", C.settingsNote, translate(language, "settings.phases.limitedByVehicle"));
      reason.dataset["part"] = "phases-reason";
      body.append(reason);
    }
    paintPower();
  };


  const socRadio = doc.createElement("input") as HTMLInputElement;
  const energyRadio = doc.createElement("input") as HTMLInputElement;
  // The charge target exactly as stored (80 % for none), until its slider moves: like the energy, it has no
  // number field, and a target off the slider's step is kept as it is by a Save that does not move it.
  let targetValue = form.values.targetPercent === "" ? "80" : form.values.targetPercent;

  let vehicleSelect: HTMLSelectElement | null = null;

  const socRow = (key: string, label: string, value: string): HTMLElement => {
    const row = element(doc, "div", C.capabilityItem);
    row.dataset["socRow"] = key;
    row.append(element(doc, "span", C.capabilityLabel, label), element(doc, "span", summaryValueClass(value), value));
    return row;
  };

  const ageSentence = (seconds: number): string => sharedAgeSentence(language, seconds);

  /**
   * The target mode's block: the vehicle, the slider with what is known about the charge, the energy
   * the target needs (recomputed as the slider moves) and what is missing.
   */
  const socBlock = (): HTMLElement => {
    const block = element(doc, "div");
    block.dataset["part"] = "soc";
    const soc = form.soc;
    const facts = element(doc, "p", C.settingsNote);
    facts.dataset["soc"] = "facts";
    const verdict = element(doc, "p", C.settingsNote);
    verdict.dataset["soc"] = "verdict";
    const carEnds = element(doc, "p", C.settingsNote);
    carEnds.dataset["soc"] = "car-ends";
    carEnds.hidden = true;
    const need = element(doc, "div");
    const reading = element(doc, "p", C.settingsNote);
    reading.dataset["soc"] = "reading";

    const pickedVehicle = (): string => {
      const picked = vehicleSelect === null ? "" : (vehicleSelect as HTMLSelectElement).value;
      return picked !== "" ? picked : values.vehicleId !== "" ? values.vehicleId : (soc?.vehicle_id ?? "");
    };
    const percent = (value: number): string => percentAmount(language, value);

    const paint = (): void => {
      facts.replaceChildren();
      verdict.replaceChildren();
      need.replaceChildren();
      reading.replaceChildren();
      facts.hidden = verdict.hidden = reading.hidden = true;
      if (soc === null) {
        nowMark.hidden = limitMark.hidden = true;
        arrangeMarks();
        return;
      }
      // The charge is the resolved vehicle's; another vehicle picked here has no reading until saved, so
      // its need is unknown, never the other vehicle's number.
      const picked = pickedVehicle();
      const other = picked !== "" && picked !== (soc.vehicle_id ?? "");
      const own = other ? form.vehicles.find((entry) => entry.id === picked) : undefined;
      const now = other ? null : soc.value;
      const limit = other ? (own?.max_percent ?? null) : soc.vehicle_max_percent;
      const capacity = other ? (own?.capacity_kwh ?? null) : soc.capacity_kwh;
      const draft = Number(targetValue.trim().replace(",", "."));
      const draftKnown = targetValue.trim() !== "" && Number.isFinite(draft);

      const parts: string[] = [];
      if (now !== null) {
        parts.push(translate(language, "settings.soc.factNow", { value: percent(now) }));
      }
      if (limit !== null) {
        parts.push(translate(language, "settings.soc.factLimit", { value: percent(chargeCeiling(limit)) }));
      }
      if (parts.length > 0) {
        facts.textContent = parts.join(" \u00b7 ");
        facts.hidden = false;
      }
      // The same two facts as ticks on the track, worded under it: "nu" (an estimate says so) and "gräns".
      const ticks = targetTicks(now, limit);
      nowMark.hidden = ticks.now === null;
      limitMark.hidden = ticks.limit === null;
      nowMark.style.setProperty("--spotnav-mark", String(ticks.now ?? 0));
      limitMark.style.setProperty("--spotnav-mark", String(ticks.limit ?? 0));
      nowMark.textContent = translate(language, soc.estimated ? "settings.soc.markNowEstimated" : "settings.soc.markNow");
      arrangeMarks();
      if (!other && now !== null && soc.age_s !== null) {
        const age = ageSentence(soc.age_s);
        const note = soc.estimated
          ? translate(language, "settings.soc.estimatedFrom", { age })
          : soc.age_s >= 90
            ? translate(language, "settings.soc.readAge", { age })
            : "";
        if (note !== "") {
          reading.textContent = note.charAt(0).toUpperCase() + note.slice(1);
          reading.dataset["estimated"] = String(soc.estimated);
          reading.hidden = false;
        }
      }
      // At or above the car's own limit (100 % when it states none) the car ends the charge itself.
      const ceiling = chargeCeiling(limit);
      carEnds.hidden = !(draftKnown && pythonRound(draft) >= ceiling);
      carEnds.textContent = translate(language, "settings.carEndsCharge", { percent: formatNumber(language, ceiling, 0) });
      if (draftKnown) {
        if (now !== null && effectiveTarget(draft, limit) <= now) {
          verdict.textContent = translate(language, "settings.soc.noNeed");
          verdict.dataset["verdict"] = "none";
          verdict.hidden = false;
        } else if (limit !== null && pythonRoundedAbove(draft, limit)) {
          verdict.textContent = translate(language, "settings.soc.toLimit", { value: percent(chargeCeiling(limit)) });
          verdict.dataset["verdict"] = "limit";
          verdict.hidden = false;
        }
      }

      let kwh: number | null = null;
      if (!other && draftKnown) {
        kwh =
          soc.target_percent !== null && draft === soc.target_percent && soc.need_kwh !== null
            ? soc.need_kwh
            : targetNeedKwh({
                soc: now,
                capacityKwh: capacity,
                targetPercent: draft,
                maxPercent: limit,
                efficiency: soc.efficiency,
              });
      }
      need.append(
        socRow(
          "need",
          translate(language, "settings.soc.need"),
          kwh === null ? translate(language, "settings.soc.unknown") : `${formatFixed(language, kwh, 1)} kWh`,
        ),
      );
      if (other) {
        need.append(element(doc, "p", C.settingsNote, translate(language, "settings.soc.needAfterVehicle")));
      }
    };

    if (soc !== null) {
      if (soc.vehicles.length > 1) {
        const select = doc.createElement("select") as HTMLSelectElement;
        select.dataset["soc"] = "vehicle-choice";
        select.disabled = form.readOnly;
        const none = doc.createElement("option") as HTMLOptionElement;
        none.value = "";
        none.textContent = translate(language, "settings.soc.vehicleUnknown");
        select.append(none);
        for (const vehicle of soc.vehicles) {
          const option = doc.createElement("option") as HTMLOptionElement;
          option.value = vehicle.id;
          option.textContent = vehicle.name;
          select.append(option);
        }
        select.value = values.vehicleId !== "" ? values.vehicleId : (soc.vehicle_id ?? "");
        select.addEventListener("change", paint);
        vehicleSelect = select;
        block.append(field(doc, `${idPrefix}-vehicle`, translate(language, "settings.soc.vehicle"), select));
      } else {
        block.append(
          socRow("vehicle", translate(language, "settings.soc.vehicle"), soc.vehicle_name ?? translate(language, "settings.soc.vehicleUnknown")),
        );
      }
      block.append(facts);
      block.append(reading);
    }
    const storedTarget = (): number => {
      const text = targetValue.trim();
      return text === "" ? Number.NaN : Number(text.replace(",", "."));
    };
    const targetRow = sliderRow(doc, {
      id: `${idPrefix}-target`,
      labelText: translate(language, "settings.soc.target"),
      sliderLabel: translate(language, "settings.soc.slider"),
      minimum: TARGET_PERCENT_MIN,
      step: 1,
      maximum: TARGET_PERCENT_MAX,
      value: storedTarget(),
      readOnly: form.readOnly,
      part: "target-value",
    });
    // The car's minimum charge level on the track: 0 to it in a darker tone, "min 30 %" under that part.
    const floorPart = element(doc, "span", C.settingsFloorSegment);
    floorPart.setAttribute("aria-hidden", "true");
    const floorMark = element(doc, "span", `${C.settingsFullMark} ${C.settingsFloorMark}`);
    floorMark.dataset["part"] = "floor-mark";
    floorMark.setAttribute("aria-hidden", "true");
    // The level now and the car's limit: ticks like the kWh slider's "fullt", their words beside "min 30 %".
    const tickMark = (part: string, text: string): HTMLElement => {
      const node = element(doc, "span", C.settingsFullMark, text);
      node.dataset["part"] = part;
      node.setAttribute("aria-hidden", "true");
      node.hidden = true;
      return node;
    };
    const nowMark = tickMark("now-mark", translate(language, "settings.soc.markNow"));
    const limitMark = tickMark("limit-mark", translate(language, "settings.soc.markLimit"));
    targetRow.track.append(floorPart, floorMark, nowMark, limitMark);
    /**
     * The words under the track, as the app places them: each under its tick and inside the track, a word that
     * would touch another a line lower. Measured once the track is laid out; until then all on the first line.
     */
    const arrangeMarks = (): void => {
      const track = targetRow.track;
      const width = track.clientWidth;
      const shown = [floorMark, nowMark, limitMark].filter((node) => !node.hidden);
      const words = shown.map((node) => ({
        node,
        key: node.dataset["part"] ?? "",
        // The track's own inset: half a thumb at each end, as in the marks' `left`.
        center: 8 + (width - 16) * Number(node.style.getPropertyValue("--spotnav-mark") || "0"),
        width: node.offsetWidth,
      }));
      const measured = width > 0 && words.every((word) => word.width > 0);
      const placed = measured ? placeMarks(words, width, 6) : [];
      let lines = 1;
      for (const word of words) {
        const at = placed.find((entry) => entry.key === word.key);
        const level = at?.level ?? 0;
        const shift = at === undefined ? 0 : at.left - (word.center - word.width / 2);
        word.node.style.setProperty("--spotnav-mark-level", String(level));
        word.node.style.setProperty("--spotnav-mark-shift", `${Math.round(shift * 100) / 100}px`);
        lines = Math.max(lines, level + 1);
      }
      track.style.setProperty("--spotnav-mark-lines", String(lines));
    };
    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => arrangeMarks()).observe(targetRow.track);
    }
    const showFloor = (): void => {
      const picked = pickedVehicle();
      const floor = form.vehicles.find((entry) => entry.id === picked)?.min_percent ?? null;
      const value = storedTarget();
      const segment = floorSegment(floor, Number.isFinite(value) ? value : null);
      floorPart.hidden = floorMark.hidden = segment === null;
      if (segment !== null) {
        floorPart.style.setProperty("--spotnav-mark", String(segment.end));
        floorMark.style.setProperty("--spotnav-mark", String(segment.label));
        floorMark.textContent = translate(language, "settings.soc.floorMark", {
          percent: formatNumber(language, segment.percent, 0),
        });
      }
      arrangeMarks();
    };
    // The target as stored, exactly (`80 %`, `80.5 %`), or as the slider sets it once moved.
    const showTarget = (): void => {
      const value = storedTarget();
      targetRow.show(Number.isFinite(value) ? `${formatNumber(language, value, 3)} %` : "");
      showFloor();
    };
    targetRow.slider.addEventListener("input", () => {
      targetValue = targetRow.slider.value;
      showTarget();
      paint();
    });
    showTarget();
    // The target follows the car: choosing another one shows the target it keeps here, when it has one.
    vehicleSelect?.addEventListener("change", () => {
      const kept = vehicleSelect === null ? undefined : form.vehicleTargets?.[vehicleSelect.value];
      if (kept !== undefined) {
        targetValue = String(kept);
        targetRow.slider.value = String(nearestStep(kept, TARGET_PERCENT_MIN, 1, TARGET_PERCENT_MAX));
        paint();
      }
      showTarget();
    });
    block.append(targetRow.group, carEnds);
    if (soc !== null) {
      block.append(verdict, need);
      paint();
      if (soc.missing.includes("capacity")) {
        const note = element(doc, "p", C.settingsNote, translate(language, "settings.soc.capacityMissing"));
        note.dataset["soc"] = "capacity-missing";
        block.append(note);
      }
      if (soc.missing.includes("soc")) {
        const note = element(doc, "p", C.settingsNote, translate(language, "settings.soc.needSensor"));
        note.dataset["soc"] = "need-sensor";
        block.append(note);
      }
    }
    return block;
  };

  const appendMode = (): void => {
    const group = element(doc, "fieldset", C.siteFieldset);
    group.dataset["part"] = "mode";
    group.append(element(doc, "legend", C.siteLegend, translate(language, "settings.plan.mode.legend")));
    const radioName = `${idPrefix}-mode`;
    const choice = (radio: HTMLInputElement, value: string, labelKey: TranslationKey): HTMLElement => {
      radio.type = "radio";
      radio.name = radioName;
      radio.value = value;
      radio.disabled = form.readOnly;
      const label = element(doc, "label", C.siteChoice);
      label.append(radio, doc.createTextNode(translate(language, labelKey)));
      return label;
    };
    energyRadio.dataset["mode"] = "manual_kwh";
    socRadio.dataset["mode"] = "target_soc";
    group.append(
      choice(energyRadio, "manual_kwh", "settings.plan.mode.energy"),
      choice(socRadio, "target_soc", "settings.plan.mode.soc"),
    );
    // A target needs a charge-level source; without one the switch stays on energy unless the record
    // already stands on the target.
    const targetSaved = form.values.driver === "target_soc";
    if (form.soc === null && !targetSaved) {
      socRadio.disabled = true;
    }
    socRadio.checked = form.values.driver === "target_soc";
    energyRadio.checked = !socRadio.checked;
    body.append(group);
    if (form.soc === null) {
      const note = element(doc, "p", C.settingsNote, translate(language, "settings.soc.needSensor"));
      note.dataset["soc"] = "need-sensor";
      body.append(note);
    }
    const energyPart = element(doc, "div");
    energyPart.dataset["part"] = "energy";
    appendEnergy(energyPart);
    // The target block is built the first time it is shown.
    let socPart: HTMLElement | null = null;
    body.append(energyPart);
    const paintMode = (): void => {
      energyPart.hidden = socRadio.checked;
      if (socRadio.checked && socPart === null) {
        socPart = socBlock();
        energyPart.after(socPart);
      }
      if (socPart !== null) {
        socPart.hidden = !socRadio.checked;
      }
    };
    energyRadio.addEventListener("change", paintMode);
    socRadio.addEventListener("change", paintMode);
    paintMode();
  };

  if (form.kind === "plan") {
    appendMode();
    appendDeadline();
    appendCurrent();
  } else if (form.kind === "energy") {
    appendEnergy();
  } else if (form.kind === "deadline") {
    appendDeadline();
  } else if (form.kind === "current") {
    appendCurrent();
  }

  const read = (): SettingsFormValues => {
    values.energy = energyValue;
    values.deadlineEnabled = enabledInput.checked;
    values.deadlineTime = timeInput.value;
    values.departureDate = dateInput.value;
    if (weekdayChecks.length > 0) {
      values.departureWeekdays = weekdayChecks
        .filter((check) => check.checked)
        .map((check) => check.value)
        .join("");
    }
    values.current = currentValue;
    if (form.kind === "plan") {
      values.driver = socRadio.checked ? "target_soc" : "manual_kwh";
      values.targetPercent = targetValue;
      // The picked vehicle, else the record's, else the resolved one: what a target-mode Save writes as
      // `target.vehicle_id`.
      const picked = vehicleSelect === null ? "" : (vehicleSelect as HTMLSelectElement).value;
      values.vehicleId = picked !== "" ? picked : values.vehicleId !== "" ? values.vehicleId : (form.soc?.vehicle_id ?? "");
    }
    return { ...values };
  };

  if (form.conflict !== null) {
    body.append(element(doc, "p", C.settingsConflict, translate(language, "settings.conflict.intro")));
  }

  // A read-only dialog, or an energy editor whose driver is target-SoC, has no Save.
  const editable =
    !form.readOnly && !(form.kind === "energy" && form.energyReadOnly);
  if (editable) {
    const actions = element(doc, "div", C.settingsActions);
    if (form.conflict === null) {
      const save = doc.createElement("button") as HTMLButtonElement;
      save.type = "button";
      save.className = `${C.button} ${C.settingsSave}`;
      save.textContent = translate(language, "settings.save");
      save.addEventListener("click", () => handlers.onSave(read()));
      const cancel = doc.createElement("button") as HTMLButtonElement;
      cancel.type = "button";
      cancel.className = C.button;
      cancel.dataset["action"] = "cancel";
      cancel.textContent = translate(language, "settings.cancel");
      cancel.addEventListener("click", () => handlers.onCancel?.());
      actions.append(save, cancel);
    } else {
      // A conflict offers only the two conflict choices: a plain Save would be built on the revision the
      // server already rejected, and only Reapply carries the server's updated base.
      const reapply = doc.createElement("button") as HTMLButtonElement;
      reapply.type = "button";
      reapply.className = `${C.button} ${C.settingsReapply}`;
      reapply.textContent = translate(language, "settings.reapply");
      reapply.addEventListener("click", () => handlers.onReapply(read()));
      const reload = doc.createElement("button") as HTMLButtonElement;
      reload.type = "button";
      reload.className = `${C.button} ${C.settingsReload}`;
      reload.textContent = translate(language, "settings.reload");
      reload.addEventListener("click", () => handlers.onReload());
      actions.append(reapply, reload);
    }
    body.append(actions);
  }

  return { body, values: read };
}

/** A weekday's short name, Monday (1) to Sunday (7), in the card's language. */
function weekdayName(language: Language, day: number): string {
  try {
    // 2024-01-01 was a Monday.
    return new Intl.DateTimeFormat(language, { weekday: "short", timeZone: "UTC" }).format(
      new Date(Date.UTC(2024, 0, day)),
    );
  } catch {
    return String(day);
  }
}
