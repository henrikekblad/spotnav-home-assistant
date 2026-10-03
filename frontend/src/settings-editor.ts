// The three focused planning editors as standards-based controls (`input type="number"`, `"time"`,
// `"checkbox"`; no private Home Assistant components). One module for each resting trigger and the
// dialog body it opens. Nothing here talks to the backend or judges a value; `settings.ts` and the
// card own every judgement and request.

import { ageSentence as sharedAgeSentence } from "./vehicle-line";
import { energyAmount, formatFixed, formatNumber, percentAmount } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import {
  CURRENT_SLIDER_STEP_A,
  ENERGY_MAX_KWH,
  ENERGY_MIN_KWH,
  ENERGY_SLIDER_MIN_KWH,
  ENERGY_SLIDER_STEP_KWH,
  PERIODS_MAX,
  PERIODS_MIN,
  TARGET_PERCENT_MAX,
  TARGET_PERCENT_MIN,
  energySliderMaximum,
  nominalPowerKw,
  sliderRepresents,
  type CurrentRange,
  type DepartureDays,
  type SettingsEditorKind,
  type SettingsFormValues,
} from "./settings";
import { VISUAL_CLASSES as C, summaryValueClass } from "./visual-styles";
import { chargeCeiling, effectiveTarget, pythonRoundedAbove, targetNeedKwh } from "./target-need";
import type { Soc, Vehicle } from "./validate";

export interface SettingsEditorForm {
  kind: SettingsEditorKind;
  readOnly: boolean;
  values: SettingsFormValues;
  energyReadOnly: boolean;
  /**
   * The accepted record's phase count, used to name the draft current's nominal power. Never rendered
   * as a value; `null` means unknown.
   */
  phases: number | null;
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

function numberInput(
  doc: Document,
  options: { min: number; max: number; step: number | "any"; value: string },
): HTMLInputElement {
  const input = doc.createElement("input") as HTMLInputElement;
  input.type = "number";
  input.min = String(options.min);
  input.max = String(options.max);
  input.step = String(options.step);
  input.value = options.value;
  input.inputMode = "decimal";
  input.className = C.settingsInput;
  return input;
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
 * One slider and its exact number field, synchronized. The pair is presentation only and sends
 * nothing. The number field is the authority a Save reads, so the slider never clamps or rounds: a
 * value it cannot represent leaves it disabled and marked while the field keeps the value.
 */
function pairedControls(
  doc: Document,
  language: Language,
  options: {
    id: string;
    labelText: string;
    sliderLabel: string;
    minimum: number;
    step: number;
    maximumOf: (value: number) => number;
    unit: string;
    input: HTMLInputElement;
    readOnly: boolean;
    onChange: () => void;
  },
): { field: HTMLElement; slider: HTMLInputElement; mark: HTMLElement } {
  const number = options.input;
  number.step = "any";
  number.min = String(options.minimum);
  const markId = `${options.id}-slider-mark`;
  const mark = element(doc, "p", C.settingsNote, translate(language, "settings.sliderOutOfRange"));
  mark.id = markId;
  const readNumber = (): number => Number(number.value.trim().replace(",", "."));
  const slider = rangeInput(doc, {
    min: options.minimum,
    max: options.maximumOf(readNumber()),
    step: options.step,
    value: options.minimum,
    label: options.sliderLabel,
  });
  slider.setAttribute("aria-describedby", markId);

  const paint = (): void => {
    const value = readNumber();
    const maximum = options.maximumOf(value);
    const represents = sliderRepresents(value, options.minimum, options.step) && value <= maximum;
    slider.max = String(maximum);
    slider.value = represents ? String(value) : String(options.minimum);
    slider.disabled = options.readOnly || !represents;
    mark.hidden = represents || options.readOnly;
    options.onChange();
  };

  slider.addEventListener("input", () => {
    number.value = slider.value;
    options.onChange();
  });
  number.addEventListener("input", paint);

  const pair = element(doc, "div", C.settingsPair);
  pair.append(slider, number, element(doc, "span", C.settingsUnit, options.unit));
  pair.append(mark);
  if (options.readOnly) {
    number.disabled = true;
  }
  const fieldNode = element(doc, "div", C.settingsField);
  fieldNode.setAttribute("role", "group");
  fieldNode.setAttribute("aria-labelledby", `${options.id}-label`);
  const label = element(doc, "label", C.settingsLabel, options.labelText);
  label.id = `${options.id}-label`;
  label.setAttribute("for", number.id);
  fieldNode.append(label, pair);
  paint();
  return { field: fieldNode, slider, mark };
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
  // `step="any"`: the exact field accepts every finite decimal.
  const energyInput = numberInput(doc, {
    min: ENERGY_MIN_KWH,
    max: ENERGY_MAX_KWH,
    step: "any",
    value: form.values.energy,
  });
  energyInput.inputMode = "decimal";
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
  const periodsInput = rangeInput(doc, {
    min: PERIODS_MIN,
    max: PERIODS_MAX,
    step: 1,
    value: Number(form.values.maxPeriods),
    label: translate(language, "settings.deadline.periods"),
  });
  const currentInput = numberInput(doc, {
    min: form.currentRange.minA,
    max: form.currentRange.maxA,
    step: 1,
    value: form.values.current,
  });

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
    into.append(
      pairedControls(doc, language, {
        id: `${idPrefix}-energy`,
        labelText: translate(language, "settings.energy.label"),
        sliderLabel: translate(language, "settings.energy.slider"),
        minimum: ENERGY_SLIDER_MIN_KWH,
        step: ENERGY_SLIDER_STEP_KWH,
        maximumOf: energySliderMaximum,
        unit: "kWh",
        input: energyInput,
        readOnly: form.readOnly,
        onChange: () => {},
      }).field,
    );
  };

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
    periodsInput.disabled = form.readOnly;
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
    const periodsValue = element(doc, "output", C.settingsUnit, periodsInput.value);
    periodsValue.dataset["periodsValue"] = "true";
    periodsInput.addEventListener("input", () => {
      periodsValue.textContent = periodsInput.value;
    });
    const periodsPair = element(doc, "div", C.settingsPair);
    periodsPair.append(periodsInput, periodsValue);
    periodsInput.id = `${idPrefix}-deadline-periods`;
    const periodsLabel = element(doc, "label", C.settingsLabel, translate(language, "settings.deadline.periods"));
    periodsLabel.setAttribute("for", periodsInput.id);
    const periodsBlock = element(doc, "div", C.settingsField);
    periodsBlock.append(periodsLabel, periodsPair);
    body.append(periodsBlock);
  };

  // The phase count the plan assumes, chosen beside the current it turns into power.
  const phaseRadios: HTMLInputElement[] = [];
  const draftPhases = (): number | null => {
    const chosen = phaseRadios.find((radio) => radio.checked);
    return chosen === undefined ? form.phases : Number(chosen.value);
  };
  const appendPhases = (): void => {
    const group = element(doc, "fieldset", C.siteFieldset);
    group.dataset["part"] = "phases";
    group.append(element(doc, "legend", C.siteLegend, translate(language, "settings.phases.legend")));
    for (const count of [1, 3] as const) {
      const label = element(doc, "label", C.siteChoice);
      const radio = doc.createElement("input") as HTMLInputElement;
      radio.type = "radio";
      radio.name = `${idPrefix}-phases`;
      radio.value = String(count);
      radio.checked = form.values.phases === String(count);
      radio.disabled = form.readOnly;
      radio.dataset["phases"] = String(count);
      phaseRadios.push(radio);
      label.append(radio, doc.createTextNode(translate(language, count === 1 ? "settings.phases.one" : "settings.phases.three")));
      group.append(label);
    }
    body.append(group);
    if (form.values.phases === "") {
      body.append(element(doc, "p", C.settingsNote, translate(language, "settings.phases.unset")));
    }
    body.append(element(doc, "p", C.settingsNote, translate(language, "settings.phases.help")));
  };

  const appendCurrent = (): void => {
    currentInput.disabled = form.readOnly;
    const power = element(doc, "p", C.settingsPower);
    power.setAttribute("aria-live", "polite");
    const paintPower = (): void => {
      const amps = Number(currentInput.value.trim().replace(",", "."));
      const nominal = nominalPowerKw(amps, draftPhases());
      power.textContent =
        nominal === null
          ? translate(language, "settings.current.powerUnknown")
          : translate(language, "settings.current.power", {
              power: formatNumber(language, nominal, 1),
            });
    };
    const paired = pairedControls(doc, language, {
      id: `${idPrefix}-current`,
      labelText: translate(language, "settings.current.label"),
      sliderLabel: translate(language, "settings.current.slider", {
        min: String(form.currentRange.minA),
        max: String(form.currentRange.maxA),
      }),
      minimum: form.currentRange.minA,
      step: CURRENT_SLIDER_STEP_A,
      maximumOf: () => form.currentRange.maxA,
      unit: "A",
      input: currentInput,
      readOnly: form.readOnly,
      onChange: paintPower,
    });
    body.append(paired.field);
    if (form.kind === "plan") {
      appendPhases();
    }
    body.append(power);
    for (const radio of phaseRadios) {
      radio.addEventListener("change", paintPower);
    }
    paintPower();
  };


  const socRadio = doc.createElement("input") as HTMLInputElement;
  const energyRadio = doc.createElement("input") as HTMLInputElement;
  const targetInput = numberInput(doc, {
    min: TARGET_PERCENT_MIN,
    max: TARGET_PERCENT_MAX,
    step: "any",
    value: form.values.targetPercent === "" ? "80" : form.values.targetPercent,
  });

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
    targetInput.disabled = form.readOnly;
    const facts = element(doc, "p", C.settingsNote);
    facts.dataset["soc"] = "facts";
    const verdict = element(doc, "p", C.settingsNote);
    verdict.dataset["soc"] = "verdict";
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
      const draft = Number(targetInput.value.trim().replace(",", "."));
      const draftKnown = targetInput.value.trim() !== "" && Number.isFinite(draft);

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
    block.append(
      pairedControls(doc, language, {
        id: `${idPrefix}-target`,
        labelText: translate(language, "settings.soc.target"),
        sliderLabel: translate(language, "settings.soc.slider"),
        minimum: TARGET_PERCENT_MIN,
        step: 1,
        maximumOf: () => TARGET_PERCENT_MAX,
        unit: "%",
        input: targetInput,
        readOnly: form.readOnly,
        onChange: paint,
      }).field,
    );
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
    values.energy = energyInput.value;
    values.deadlineEnabled = enabledInput.checked;
    values.deadlineTime = timeInput.value;
    values.departureDate = dateInput.value;
    if (weekdayChecks.length > 0) {
      values.departureWeekdays = weekdayChecks
        .filter((check) => check.checked)
        .map((check) => check.value)
        .join("");
    }
    values.maxPeriods = periodsInput.value;
    values.current = currentInput.value;
    if (form.kind === "plan") {
      const chosen = phaseRadios.find((radio) => radio.checked);
      values.phases = chosen === undefined ? values.phases : chosen.value;
      values.driver = socRadio.checked ? "target_soc" : "manual_kwh";
      values.targetPercent = targetInput.value;
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
