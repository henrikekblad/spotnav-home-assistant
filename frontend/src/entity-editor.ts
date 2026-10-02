// Entity-configuration surfaces: the read-only rows on the Settings page and one editor body per
// group (charger, site). Nothing here talks to the backend or judges a value: rows are drawn from a
// decoded config, and the editor reports the draft (`entityChange` in `entity-config.ts` judges it).
//
// Entities are picked with Home Assistant's `ha-selector` when defined (it lazy-loads), filtered by
// the domains and device classes the backend allows; otherwise a field is a plain entity-id text
// input, which is a real path and what the tests exercise.

import {
  APPLY_DETECTION,
  DERIVED_KIND_KEYS,
  DERIVED_OPTIONAL_KINDS,
  DETECT_WARNING_KEYS,
  GRID_TOTAL_FIELDS,
  INFORMATIONAL_DETECT_WARNINGS,
  batteryApplied,
  DERIVED_REQUIRED_KINDS,
  MEASUREMENT_DERIVED,
  MEASUREMENT_DIRECT,
  PHASES,
  automaticEntity,
  derivedFieldName,
  directFieldName,
  fieldHelpKey,
  isMissingEntity,
  draftFrom,
  fieldLabelKey,
  fieldErrorKey,
  fieldsOf,
  isPhaseField,
  phaseField,
  phaseFieldNames,
  storedMode,
  type EntityConfig,
  type EntityControl,
  type EntityDraft,
  type EntityField,
  type DetectedBattery,
  type DetectedMeter,
  type EntityFieldEntity,
  type EntityFieldError,
  type ControlConflict,
  type EntityFieldFlag,
  type EntityScope,
  type EntitySite,
  type SiteWarning,
  type VehicleSoc,
  vehicleChoice,
} from "./entity-config";
import { formatFixed, formatNumber } from "./format";
import {
  CAPACITY_MAX_KWH,
  CAPACITY_MIN_KWH,
  CONSUMPTION_MAX_KWH_PER_10KM,
  CONSUMPTION_MIN_KWH_PER_10KM,
} from "./settings";
import type { Vehicle } from "./validate";
import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";

function element<K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  className?: string,
  content?: string,
): HTMLElementTagNameMap[K] {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (content !== undefined) {
    node.textContent = content;
  }
  return node;
}

export function modeLabel(language: Language, mode: string): string {
  return translate(language, mode === MEASUREMENT_DERIVED ? "entity.mode.derived" : "entity.mode.direct");
}

function labelOf(language: Language, field: string): string {
  const key = fieldLabelKey(field);
  if (key !== null) {
    return translate(language, key);
  }
  const direct = /^direct_(L[123])$/.exec(field);
  if (direct !== null) {
    return translate(language, "entity.phase.direct", { phase: direct[1] ?? "" });
  }
  const derived = /^derived_(L[123])_(.+)$/.exec(field);
  if (derived !== null) {
    const kindKey = DERIVED_KIND_KEYS[derived[2] ?? ""];
    const kind = kindKey === undefined ? (derived[2] ?? "") : translate(language, kindKey);
    return `${derived[1] ?? ""} ${kind.toLowerCase()}`;
  }
  return field;
}

function warningText(language: Language, warning: SiteWarning): string[] {
  const integration = warning.integration ?? "";
  if (warning.code === "update_interval_exceeds_max_age") {
    const lines = [
      translate(language, "entity.warning.updateInterval", {
        integration,
        seconds: formatNumber(language, warning.intervalS ?? 0, 0),
      }),
    ];
    if (warning.option !== null) {
      lines.push(translate(language, "entity.warning.updateIntervalOption", { option: warning.option }));
    }
    return lines;
  }
  if (warning.code === "reports_on_change_only") {
    return [translate(language, "entity.warning.onChange", { integration })];
  }
  if (warning.code === "own_load_balancing") {
    return [translate(language, "entity.warning.ownBalancing", { name: warning.deviceName ?? integration, integration })];
  }
  if (warning.code === "external_current_balancer") {
    return [translate(language, "entity.warning.externalBalancer", { name: warning.deviceName ?? "", integration })];
  }
  return [translate(language, "entity.warning.unknown")];
}

/** The site's warnings, one worded row each (a code this card does not know gets a generic sentence). */
export function siteWarningRows(doc: Document, language: Language, site: EntitySite): HTMLElement[] {
  const rows: HTMLElement[] = [];
  for (const warning of site.warnings) {
    for (const line of warningText(language, warning)) {
      const row = element(doc, "p", C.entityWarning, line);
      row.dataset["warning"] = warning.code;
      rows.push(row);
    }
  }
  return rows;
}

/**
 * What the site's measurement wants said: that the current is estimated from power (with the power
 * factor assumed), sources that update more slowly than the maximum age, and devices that balance load
 * themselves. `null` when there is nothing to say.
 */
export function siteNotices(doc: Document, language: Language, site: EntitySite): HTMLElement | null {
  const notices = element(doc, "div");
  notices.dataset["notices"] = "site";
  if (site.measurement.currentEstimated) {
    const estimated = element(
      doc,
      "p",
      C.entityWarning,
      translate(language, "entity.notice.estimated", {
        pf: formatNumber(language, site.measurement.assumedPowerFactor ?? 0.9, 1),
      }),
    );
    estimated.dataset["notice"] = "estimated";
    notices.append(estimated);
  }
  notices.append(...siteWarningRows(doc, language, site));
  return notices.childElementCount === 0 ? null : notices;
}

/**
 * Read-only rows of one group, by label and friendly name (or "not set"). The button that opens
 * the editor is the caller's, since only it knows whether the reader may edit.
 */
/**
 * Ask Home Assistant to define `ha-selector` before an entity editor opens (its pickers load
 * lazily; the entities card's editor pulls them in). Bounded and silent: on failure the editor
 * falls back to plain entity-id inputs.
 */
export async function ensureHaSelector(win: Window | null | undefined, timeoutMs = 3000): Promise<void> {
  const registry = win?.customElements;
  if (registry === undefined || registry.get("ha-selector") !== undefined) {
    return;
  }
  const load = (win as unknown as { loadCardHelpers?: () => Promise<unknown> }).loadCardHelpers;
  if (typeof load !== "function") {
    return;
  }
  try {
    const helpers = (await load()) as {
      createCardElement?: (config: Record<string, unknown>) => unknown;
    };
    const card = helpers.createCardElement?.({ type: "entities", entities: [] }) as
      | { constructor?: { getConfigElement?: () => Promise<unknown> } }
      | undefined;
    await card?.constructor?.getConfigElement?.();
    await Promise.race([
      registry.whenDefined("ha-selector"),
      new Promise((resolve) => setTimeout(resolve, timeoutMs)),
    ]);
  } catch {
  }
}

function controlStartStopText(language: Language, control: EntityControl): string {
  const path = control.startStop;
  if (path.kind === "select") {
    return translate(language, "control.startStop.select", {
      start: path.startOption ?? "",
      stop: path.stopOption ?? "",
    });
  }
  if (path.kind === "buttons") {
    return translate(language, "control.startStop.buttons");
  }
  if (path.kind === "easee") {
    return translate(language, "control.startStop.easee");
  }
  if (path.kind === "number_pause") {
    return translate(language, "control.startStop.numberPause");
  }
  if (path.kind === "other") {
    return translate(language, "control.startStop.other");
  }
  return translate(language, path.inverted ? "control.startStop.switchInverted" : "control.startStop.switch");
}

export function controlCurrentText(language: Language, control: EntityControl, name: string): string {
  const current = control.current;
  if (current.kind === "none") {
    return translate(language, "control.current.none");
  }
  if (!current.enabled) {
    return translate(language, "control.current.off");
  }
  if (current.kind === "ocpp") {
    return translate(language, "control.current.ocpp");
  }
  if (current.kind === "service") {
    return translate(language, "control.current.service");
  }
  return translate(language, "control.current.number", { name });
}

/** The write limits the policy states, one sentence each; a policy with none says so. */
function controlPolicyLines(language: Language, control: EntityControl): string[] {
  const policy = control.policy;
  const lines: string[] = [];
  if (policy.minIntervalS > 0) {
    lines.push(translate(language, "control.policy.interval", { seconds: formatNumber(language, policy.minIntervalS, 0) }));
  }
  if (policy.maxWritesPerMinute !== null) {
    lines.push(translate(language, "control.policy.perMinute", { count: formatNumber(language, policy.maxWritesPerMinute, 0) }));
  }
  if (policy.flashStored) {
    lines.push(translate(language, "control.policy.flash"));
  }
  if (policy.zeroPauses) {
    lines.push(translate(language, "control.policy.zeroPauses"));
  }
  if (policy.ignoredWhilePaused) {
    lines.push(translate(language, "control.policy.ignoredWhilePaused"));
  }
  if (policy.installationWide) {
    lines.push(translate(language, "control.policy.installation"));
  }
  if (policy.resendAfterPlugIn) {
    lines.push(translate(language, "control.policy.resend"));
  }
  return lines.length > 0 ? lines : [translate(language, "control.policy.free")];
}

/**
 * The chosen control path and its write policy, read only: how the charger is started and stopped, how
 * its current is set (and whether load balancing may change it during a charge), where its charging
 * state comes from, the limits every write obeys, and the charger's own modes that are on and would
 * fight SpotNav. Drawn from the decoded configuration; nothing here asks the backend.
 */
export function controlRows(doc: Document, language: Language, control: EntityControl, nameOf: (entityId: string) => string): HTMLElement {
  const block = element(doc, "div", C.entityRow);
  block.dataset["control"] = "path";
  block.append(element(doc, "div", C.entityRowLabel, translate(language, "control.title")));
  const row = (key: string, label: TranslationKey, text: string): void => {
    const line = element(doc, "div");
    line.dataset["controlRow"] = key;
    line.append(element(doc, "span", C.entityRowLabel, translate(language, label)));
    line.append(element(doc, "span", `${C.settingsValue} ${C.entityRowValue}`, text));
    block.append(line);
  };
  row("start_stop", "control.startStop", controlStartStopText(language, control));
  if (control.startStop.kind === "easee") {
    // Its charge-control entity is only the charger's identity, so nothing is offered to pick.
    const fixed = element(doc, "p", C.entityHelp, translate(language, "control.startStop.easeeFixed"));
    fixed.dataset["controlRow"] = "start_stop_fixed";
    block.append(fixed);
  }
  const currentEntity = control.current.entityId;
  row(
    "current",
    "control.current",
    controlCurrentText(language, control, currentEntity === null ? "" : nameOf(currentEntity)),
  );
  row(
    "charging_state",
    "control.state",
    translate(language, control.chargingState.source === "status" ? "control.state.status" : "control.state.control"),
  );
  if (control.current.kind !== "none") {
    const policy = element(doc, "div");
    policy.dataset["controlRow"] = "policy";
    policy.append(element(doc, "span", C.entityRowLabel, translate(language, "control.policy")));
    for (const line of controlPolicyLines(language, control)) {
      policy.append(element(doc, "p", C.entityHelp, line));
    }
    block.append(policy);
    block.append(
      element(
        doc,
        "p",
        C.capabilityNote,
        translate(language, control.capabilities.regulatedCurrent ? "control.regulated.yes" : "control.regulated.no"),
      ),
    );
  }
  for (const conflict of control.conflicts) {
    const warning = element(doc, "p", C.entityWarning, conflictText(language, conflict, nameOf(conflict.entityId)));
    warning.dataset["conflict"] = conflict.entityId;
    block.append(warning);
  }
  return block;
}

/** The sentence for one conflict: a charger's own mode that is on, or its own enable switch that is off. */
export function conflictText(language: Language, conflict: ControlConflict, name: string): string {
  return conflict.kind === "disabled"
    ? translate(language, "control.disabled", { name })
    : translate(language, "control.conflict", { label: conflict.label, name });
}

/** An entity's friendly name when the configuration states it, else its id. */
export function entityNameIn(config: EntityConfig, entityId: string): string {
  for (const field of config.fields) {
    if (field.kind === "entity" && field.current !== null && field.current.entityId === entityId) {
      return field.current.friendlyName;
    }
  }
  return entityId;
}

export interface EntityEditorHandlers {
  onSave: (draft: EntityDraft) => void;
  onCancel: () => void;
}

export interface EntityEditorInput {
  scope: EntityScope;
  config: EntityConfig;
  hass: () => unknown;
  appliesText: string | null;
}

export interface EntityEditorBody {
  body: HTMLElement;
  draft: () => EntityDraft;
  markErrors: (errors: readonly EntityFieldError[]) => void;
  setNotice: (text: string | null, code: string | null) => void;
  setPending: (pending: boolean) => void;
  setHass: (hass: unknown) => void;
}

interface Control {
  node: HTMLElement;
  input: HTMLElement;
  picker: HTMLElement | null;
}

type PickerElement = HTMLElement & {
  hass?: unknown;
  selector?: unknown;
  value?: unknown;
  label?: string;
  required?: boolean;
  disabled?: boolean;
};

function selectorFor(field: EntityFieldEntity): { entity: Record<string, unknown> } {
  const entity: Record<string, unknown> = { domain: field.domains };
  if (field.deviceClasses.length > 0) {
    entity["device_class"] = field.deviceClasses;
  }
  return { entity };
}

export function entityEditorBody(
  doc: Document,
  language: Language,
  input: EntityEditorInput,
  handlers: EntityEditorHandlers,
  idPrefix: string,
): EntityEditorBody {
  const { config, scope } = input;
  const values: EntityDraft = draftFrom(config, scope);
  const pickers = new Set<PickerElement>();
  const disabledWhenPending: Array<HTMLElement & { disabled: boolean }> = [];
  const errorNodes = new Map<string, { node: HTMLElement; input: HTMLElement }>();
  let pending = false;

  const body = element(doc, "form");
  body.noValidate = true;
  body.dataset["entityEditor"] = scope;
  const notice = element(doc, "p", C.settingsNotice);
  notice.setAttribute("role", "status");
  notice.hidden = true;
  body.append(notice);
  if (input.appliesText !== null) {
    body.append(
      element(
        doc,
        "p",
        C.siteApplies,
        scope === "site"
          ? translate(language, "entity.site.intro", { applies: input.appliesText })
          : input.appliesText,
      ),
    );
  }
  if (scope === "charger" && config.control !== null) {
    body.append(controlRows(doc, language, config.control, (entityId) => entityNameIn(config, entityId)));
  }
  if (scope === "site" && config.site !== null) {
    const notices = siteNotices(doc, language, config.site);
    if (notices !== null) {
      body.append(notices);
    }
    const detection = detectionSection(config, config.site);
    if (detection !== null) {
      body.append(detection);
    }
  }

  const ctor = doc.defaultView?.customElements;
  const useSelector = ctor !== undefined && ctor.get("ha-selector") !== undefined;

  function entityControl(field: EntityFieldEntity, label: string): Control {
    const id = `${idPrefix}-entity-${field.field}`;
    if (useSelector) {
      const picker = doc.createElement("ha-selector") as PickerElement;
      picker.hass = input.hass();
      picker.selector = selectorFor(field);
      picker.value = values[field.field] === "" ? undefined : values[field.field];
      picker.label = label;
      picker.required = false;
      picker.dataset["field"] = field.field;
      picker.addEventListener("value-changed", (event) => {
        event.stopPropagation();
        const detail = (event as CustomEvent<{ value?: unknown }>).detail;
        const next = detail?.value;
        values[field.field] = typeof next === "string" ? next : "";
      });
      pickers.add(picker);
      return { node: picker, input: picker, picker };
    }
    const text = doc.createElement("input");
    text.type = "text";
    text.id = id;
    text.className = C.settingsInput;
    text.value = values[field.field] ?? "";
    text.dataset["field"] = field.field;
    text.autocomplete = "off";
    text.spellcheck = false;
    text.setAttribute("autocapitalize", "off");
    text.setAttribute("aria-label", label);
    text.addEventListener("input", () => {
      values[field.field] = text.value;
    });
    disabledWhenPending.push(text);
    return { node: text, input: text, picker: null };
  }

  function appendInfo(block: HTMLElement, field: EntityField): void {
    if (field.kind === "entity" && isMissingEntity(field)) {
      const warning = element(
        doc,
        "p",
        C.entityWarning,
        translate(language, field.required ? "entity.missing.required" : "entity.missing.optional"),
      );
      warning.dataset["missing"] = field.field;
      block.append(warning);
    }
    const automatic = field.kind === "entity" ? automaticEntity(field) : null;
    if (automatic !== null) {
      block.append(
        element(doc, "p", C.entityAutomatic, translate(language, "entity.automatic", { name: automatic.friendlyName })),
      );
    }
    const key = isPhaseField(field.field) ? null : fieldHelpKey(field.field);
    if (key !== null) {
      const help = element(doc, "p", C.entityHelp, translate(language, key));
      help.dataset["help"] = field.field;
      block.append(help);
    }
  }

  function fieldBlock(
    name: string,
    label: string,
    control: Control,
    showLabel: boolean,
    field: EntityField | null = null,
  ): HTMLElement {
    const block = element(doc, "div", C.settingsField);
    block.dataset["fieldBlock"] = name;
    if (showLabel && control.picker === null) {
      const caption = element(doc, "label", C.settingsLabel, label);
      caption.setAttribute("for", control.input.id);
      block.append(caption);
    }
    block.append(control.node);
    if (field !== null) {
      appendInfo(block, field);
    }
    const error = element(doc, "p", C.settingsError);
    error.hidden = true;
    error.dataset["fieldError"] = name;
    error.setAttribute("role", "alert");
    const errorId = `${idPrefix}-error-${name}`;
    error.id = errorId;
    block.append(error);
    errorNodes.set(name, { node: error, input: control.input });
    return block;
  }

  function numberControl(field: EntityField, label: string): Control {
    const id = `${idPrefix}-entity-${field.field}`;
    const number = doc.createElement("input");
    number.type = "number";
    number.id = id;
    number.className = C.settingsInput;
    number.inputMode = "decimal";
    number.step = "any";
    if (field.kind === "number") {
      number.min = String(field.minimum);
    }
    number.value = values[field.field] ?? "";
    number.dataset["field"] = field.field;
    number.setAttribute("aria-label", label);
    number.addEventListener("input", () => {
      values[field.field] = number.value;
    });
    disabledWhenPending.push(number);
    return { node: number, input: number, picker: null };
  }

  function entityField(field: EntityFieldEntity, label: string, showLabel = true): HTMLElement {
    return fieldBlock(field.field, label, entityControl(field, label), showLabel, field);
  }

  function flagField(field: EntityFieldFlag, label: string): HTMLElement {
    const block = element(doc, "div", C.settingsField);
    block.dataset["fieldBlock"] = field.field;
    const row = element(doc, "label", C.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.id = `${idPrefix}-entity-${field.field}`;
    box.checked = values[field.field] === "true";
    box.dataset["field"] = field.field;
    box.addEventListener("change", () => {
      values[field.field] = box.checked ? "true" : "false";
    });
    disabledWhenPending.push(box);
    row.append(box, doc.createTextNode(label));
    block.append(row);
    appendInfo(block, field);
    const error = element(doc, "p", C.settingsError);
    error.hidden = true;
    error.dataset["fieldError"] = field.field;
    error.setAttribute("role", "alert");
    error.id = `${idPrefix}-error-${field.field}`;
    block.append(error);
    errorNodes.set(field.field, { node: error, input: box });
    return block;
  }

  function detectionSection(full: EntityConfig, site: EntitySite): HTMLElement | null {
    const batteryInUse = new Map(site.batteries.map((battery) => [battery.id, batteryApplied(full, battery)]));
    const differs =
      site.meters.some((meter) => !meter.applied) || site.batteries.some((battery) => batteryInUse.get(battery.id) !== true);
    if (!differs) {
      return null;
    }
    const section = element(doc, "fieldset", C.siteFieldset);
    section.dataset["detection"] = "site";
    section.append(element(doc, "legend", C.siteLegend, translate(language, "entity.detect.title")));
    section.append(element(doc, "p", C.entityHelp, translate(language, "entity.detect.intro")));

    const apply = (id: string, name: string): HTMLButtonElement => {
      const button = element(doc, "button", C.button, translate(language, "entity.detect.use"));
      button.setAttribute("aria-label", `${translate(language, "entity.detect.use")}: ${name}`);
      button.type = "button";
      button.dataset["apply"] = id;
      button.addEventListener("click", () => {
        if (!pending) {
          handlers.onSave({ [APPLY_DETECTION]: id });
        }
      });
      disabledWhenPending.push(button);
      return button;
    };
    const line = (text: string, code: string): HTMLElement => {
      const node = element(doc, "p", C.entityHelp, text);
      node.dataset["detect"] = code;
      return node;
    };
    const enableLine = (count: number): HTMLElement | null =>
      count === 0 ? null : line(translate(language, "entity.detect.enable", { count: String(count) }), "enable");

    if (site.meters.length > 0) {
      section.append(element(doc, "strong", undefined, translate(language, "entity.detect.meters")));
    }
    for (const meter of site.meters) {
      section.append(meterCard(meter, apply, line, enableLine));
    }
    if (site.batteries.length > 0) {
      section.append(element(doc, "strong", undefined, translate(language, "entity.detect.batteries")));
    }
    for (const battery of site.batteries) {
      section.append(batteryCard(battery, batteryInUse.get(battery.id) === true, apply, line, enableLine));
    }
    return section;
  }

  function meterCard(
    meter: DetectedMeter,
    apply: (id: string, name: string) => HTMLButtonElement,
    line: (text: string, code: string) => HTMLElement,
    enableLine: (count: number) => HTMLElement | null,
  ): HTMLElement {
    const card = element(doc, "div", C.entityRow);
    card.dataset["detectedMeter"] = meter.id;
    card.append(element(doc, "span", C.entityRowLabel, `${meter.title} (${meter.integration})`));
    card.append(
      line(
        translate(language, meter.mode === MEASUREMENT_DERIVED ? "entity.detect.derived" : "entity.detect.direct"),
        "mode",
      ),
    );
    if (meter.currentSigned) {
      card.append(line(translate(language, "entity.detect.signed"), "signed"));
    }
    if (meter.powerInverted) {
      card.append(line(translate(language, "entity.detect.inverted"), "inverted"));
    }
    if (meter.entities.some((entity) => entity.role === "grid_power")) {
      card.append(line(translate(language, "entity.detect.gridPower"), "gridPower"));
    }
    if (meter.estimated) {
      const estimated = line(translate(language, "entity.detect.estimated"), "estimated");
      estimated.className = C.entityWarning;
      card.append(estimated);
    }
    if (meter.confidence === "low") {
      card.append(line(translate(language, "entity.detect.confidence.low"), "confidence"));
    }
    for (const code of meter.warnings) {
      const key = DETECT_WARNING_KEYS[code];
      if (key !== undefined) {
        const warning = line(translate(language, key), code);
        // Only a note that asks for a check is a warning; the rest is plain information.
        warning.className = INFORMATIONAL_DETECT_WARNINGS.has(code) ? C.entityHelp : C.entityWarning;
        card.append(warning);
      }
    }
    const enable = enableLine(meter.disabledEntities.length);
    if (enable !== null) {
      card.append(enable);
    }
    if (meter.applied) {
      card.append(element(doc, "span", C.entityAutomatic, translate(language, "entity.detect.inUse")));
    } else {
      card.append(apply(meter.id, meter.title));
    }
    return card;
  }

  function batteryCard(
    battery: DetectedBattery,
    inUse: boolean,
    apply: (id: string, name: string) => HTMLButtonElement,
    line: (text: string, code: string) => HTMLElement,
    enableLine: (count: number) => HTMLElement | null,
  ): HTMLElement {
    const card = element(doc, "div", C.entityRow);
    card.dataset["detectedBattery"] = battery.id;
    card.append(element(doc, "span", C.entityRowLabel, `${battery.friendlyName} (${battery.integration})`));
    if (battery.inverted) {
      card.append(line(translate(language, "entity.detect.battery.inverted"), "inverted"));
    }
    if (battery.dischargeEntityId !== null) {
      card.append(line(translate(language, "entity.detect.battery.pair"), "pair"));
    }
    const enable = enableLine(battery.disabledEntities.length);
    if (enable !== null) {
      card.append(enable);
    }
    if (inUse) {
      card.append(element(doc, "span", C.entityAutomatic, translate(language, "entity.detect.inUse")));
    } else {
      card.append(apply(battery.id, battery.friendlyName));
    }
    return card;
  }

  for (const field of fieldsOf(config, scope)) {
    if (!field.writable || isPhaseField(field.field) || field.field === "measurement_mode") {
      continue;
    }
    const label = labelOf(language, field.field);
    if (field.kind === "entity") {
      body.append(entityField(field, label));
    } else if (field.kind === "flag") {
      body.append(flagField(field, label));
    } else if (field.kind === "number") {
      const row = fieldBlock(field.field, label, numberControl(field, label), true, field);
      const control = row.querySelector<HTMLElement>("input");
      if (control !== null) {
        const line = element(doc, "div", C.settingsRow);
        control.replaceWith(line);
        line.append(control, element(doc, "span", C.settingsUnit, field.field === "main_fuse_a" ? "A" : "s"));
      }
      body.append(row);
    }
  }

  if (scope === "site") {
    const modeField = fieldsOf(config, "site").find((entry) => entry.field === "measurement_mode");
    if (modeField !== undefined && modeField.kind === "enum") {
      const fieldset = element(doc, "fieldset", C.siteFieldset);
      fieldset.dataset["fieldBlock"] = "measurement_mode";
      fieldset.append(element(doc, "legend", C.siteLegend, labelOf(language, "measurement_mode")));
      const modeHelp = element(doc, "p", C.entityHelp, translate(language, "entity.help.measurementMode"));
      modeHelp.dataset["help"] = "measurement_mode";
      fieldset.append(modeHelp);
      const radioName = `${idPrefix}-mode`;
      const phases = element(doc, "div", C.entityMeters);
      phases.dataset["part"] = "phases";
      const currentMode = (): string => values["measurement_mode"] ?? storedMode(config) ?? MEASUREMENT_DIRECT;

      const paintPhases = (): void => {
        for (const name of phaseFieldNames(MEASUREMENT_DIRECT).concat(phaseFieldNames(MEASUREMENT_DERIVED))) {
          const gone = errorNodes.get(name);
          if (gone !== undefined) {
            errorNodes.delete(name);
          }
        }
        for (const picker of [...pickers]) {
          if (picker.dataset["field"] !== undefined && isPhaseField(picker.dataset["field"])) {
            pickers.delete(picker);
          }
        }
        phases.replaceChildren();
        const mode = currentMode();
        phases.dataset["mode"] = mode;
        // Each phase repeats the same fields, so their help is said once, under the last phase.
        const phaseHelp = element(doc, "div");
        phaseHelp.dataset["help"] = "phases";
        const helpKeys: TranslationKey[] =
          mode === MEASUREMENT_DERIVED
            ? ["entity.help.derivedPower", "entity.help.derivedVoltage"]
            : ["entity.help.phaseDirect"];
        // The meter's total grid power is what a site that reports current only reads for solar; a derived
        // site has its per-phase power and ignores it. The sign option applies to both.
        for (const name of GRID_TOTAL_FIELDS) {
          const total = body.querySelector<HTMLElement>(`[data-field-block="${name}"]`);
          if (total !== null) {
            total.hidden = mode === MEASUREMENT_DERIVED;
          }
        }
        for (const key of helpKeys) {
          phaseHelp.append(element(doc, "p", C.entityHelp, translate(language, key)));
        }
        for (const phase of PHASES) {
          const group = element(doc, "fieldset", C.entityLine);
          group.dataset["phase"] = phase;
          group.append(element(doc, "legend", C.siteLegend, phase));
          const cells = element(doc, "div", C.entityLineCells);
          const names =
            mode === MEASUREMENT_DERIVED
              ? DERIVED_REQUIRED_KINDS.map((kind) => derivedFieldName(phase, kind))
              : [directFieldName(phase)];
          for (const name of names) {
            const field = phaseField(config, name);
            if (values[name] === undefined) {
              values[name] = field.current === null ? "" : field.current.entityId;
            }
            cells.append(entityField(field, labelOf(language, name)));
          }
          group.append(cells);
          if (mode === MEASUREMENT_DERIVED) {
            // The optional sources only sharpen the fuse check; without any of current, apparent or
            // reactive power the current is estimated from power.
            const more = element(doc, "details");
            more.dataset["optionalSources"] = phase;
            more.append(element(doc, "summary", undefined, translate(language, "entity.phase.optional")));
            const optionalCells = element(doc, "div", C.entityLineCells);
            let anySet = false;
            for (const kind of DERIVED_OPTIONAL_KINDS) {
              const name = derivedFieldName(phase, kind);
              const field = phaseField(config, name);
              if (values[name] === undefined) {
                values[name] = field.current === null ? "" : field.current.entityId;
              }
              anySet = anySet || values[name] !== "";
              optionalCells.append(entityField(field, labelOf(language, name)));
            }
            more.open = anySet;
            more.append(optionalCells);
            group.append(more);
          }
          phases.append(group);
        }
        phases.append(phaseHelp);
      };

      for (const choice of modeField.choices) {
        const label = element(doc, "label", C.siteChoice);
        const radio = doc.createElement("input");
        radio.type = "radio";
        radio.name = radioName;
        radio.value = choice;
        radio.checked = currentMode() === choice;
        radio.dataset["mode"] = choice;
        radio.addEventListener("change", () => {
          if (radio.checked) {
            values["measurement_mode"] = choice;
            paintPhases();
          }
        });
        disabledWhenPending.push(radio);
        label.append(radio, doc.createTextNode(modeLabel(language, choice)));
        fieldset.append(label);
      }
      const modeError = element(doc, "p", C.settingsError);
      modeError.hidden = true;
      modeError.dataset["fieldError"] = "measurement_mode";
      modeError.setAttribute("role", "alert");
      errorNodes.set("measurement_mode", { node: modeError, input: fieldset });
      fieldset.append(modeError);
      body.append(fieldset);
      paintPhases();
      body.append(phases);
      // The battery meter and maximum age come after the phases they qualify, as the backend lists them.
      for (const name of [
        "site_current_signed",
        ...GRID_TOTAL_FIELDS,
        "grid_power_inverted",
        "battery_aggregate_power_entity",
        "battery_discharge_power_entity",
        "battery_power_inverted",
        "max_age_s",
      ]) {
        const block = body.querySelector(`[data-field-block="${name}"]`);
        if (block !== null) {
          body.append(block);
        }
      }
    }
  }

  const actions = element(doc, "div", C.settingsActions);
  const save = element(doc, "button", `${C.button} ${C.settingsSave}`, translate(language, "settings.save"));
  save.type = "submit";
  const cancel = element(doc, "button", C.button, translate(language, "settings.cancel"));
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  disabledWhenPending.push(save, cancel);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending) {
      handlers.onSave({ ...values });
    }
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });

  function applyPending(): void {
    for (const control of disabledWhenPending) {
      control.disabled = pending;
    }
    for (const picker of pickers) {
      picker.disabled = pending;
    }
  }

  return {
    body,
    draft: () => ({ ...values }),
    markErrors(errors) {
      for (const [, entry] of errorNodes) {
        entry.node.hidden = true;
        entry.node.textContent = "";
        entry.input.removeAttribute("aria-invalid");
        entry.input.removeAttribute("aria-describedby");
      }
      for (const error of errors) {
        const entry = errorNodes.get(error.field);
        if (entry === undefined) {
          continue;
        }
        const key: TranslationKey = fieldErrorKey(error.code);
        entry.node.hidden = false;
        entry.node.textContent = translate(language, key);
        entry.node.dataset["code"] = error.code;
        entry.input.setAttribute("aria-invalid", "true");
        if (entry.node.id !== "") {
          entry.input.setAttribute("aria-describedby", entry.node.id);
        }
      }
    },
    setNotice(text, code) {
      notice.hidden = text === null;
      notice.textContent = text ?? "";
      if (code === null) {
        notice.removeAttribute("data-code");
      } else {
        notice.dataset["code"] = code;
      }
    },
    setPending(next) {
      pending = next;
      applyPending();
    },
    setHass(hass) {
      for (const picker of pickers) {
        picker.hass = hass;
      }
    },
  };
}

export interface VehicleEditorInput {
  vehicleId: string;
  /** The dashboard's row for this vehicle; `null` when only its sensors are known (no capacity or consumption to edit). */
  row: Vehicle | null;
  /** The charge-level sensors this vehicle can choose from; `null` when the configuration lists none for it. */
  sensor: VehicleSoc | null;
}

/**
 * One vehicle's dialog: the charge-level sensor (one radio per sensor it has plus one for automatic
 * detection, which is all the backend accepts), battery capacity and consumption. Reports the draft as
 * text: `soc` (entity id, `""` for automatic; absent without a sensor block), `capacity` (absent when the
 * vehicle reports it itself) and `consumption`. The card judges it and sends the changes.
 */
export function vehicleEditorBody(
  doc: Document,
  language: Language,
  input: VehicleEditorInput,
  handlers: EntityEditorHandlers,
  idPrefix: string,
): EntityEditorBody {
  const { row, sensor } = input;
  const values: EntityDraft = {};
  const controls: Array<HTMLElement & { disabled: boolean }> = [];
  const errorNodes = new Map<string, { node: HTMLElement; input: HTMLElement }>();
  let pending = false;

  const body = element(doc, "form");
  body.noValidate = true;
  body.dataset["entityEditor"] = "vehicle";
  body.dataset["vehicle"] = input.vehicleId;
  const notice = element(doc, "p", C.settingsNotice);
  notice.setAttribute("role", "status");
  notice.hidden = true;
  body.append(notice);

  const errorFor = (name: string, control: HTMLElement): HTMLElement => {
    const node = element(doc, "p", C.settingsError);
    node.hidden = true;
    node.dataset["fieldError"] = name;
    node.setAttribute("role", "alert");
    errorNodes.set(name, { node, input: control });
    return node;
  };

  if (sensor !== null) {
    values["soc"] = vehicleChoice(sensor);
    const group = element(doc, "fieldset", C.siteFieldset);
    group.dataset["part"] = "soc";
    group.append(element(doc, "legend", C.siteLegend, translate(language, "settings.vehicle.sensorLegend")));
    const radioName = `${idPrefix}-vehicle-soc`;
    const choose = (value: string, label: string, title: string | null): void => {
      const line = element(doc, "label", C.siteChoice);
      const radio = doc.createElement("input");
      radio.type = "radio";
      radio.name = radioName;
      radio.value = value;
      radio.checked = values["soc"] === value;
      radio.dataset["vehicleChoice"] = value === "" ? "automatic" : value;
      radio.addEventListener("change", () => {
        if (radio.checked) {
          values["soc"] = value;
        }
      });
      controls.push(radio);
      line.append(radio, doc.createTextNode(label));
      if (title !== null) {
        line.title = title;
      }
      group.append(line);
    };
    for (const candidate of sensor.candidates) {
      choose(candidate.entityId, candidate.friendlyName, candidate.entityId);
    }
    choose("", translate(language, "entity.vehicle.automatic"), null);
    // Only when automatic detection really has nothing to pick: a health, 12 V or target sensor beside
    // the charge level is among the candidates but does not stop it from choosing.
    if (sensor.candidates.length > 1 && sensor.selected === null) {
      group.append(element(doc, "p", C.entityHelp, translate(language, "entity.vehicle.several")));
    }
    group.append(errorFor("vehicle_soc", group));
    body.append(group);
  }

  const numberField = (
    name: "capacity" | "consumption",
    errorField: string,
    labelKey: TranslationKey,
    unit: string,
    current: number | null,
    range: { min: number; max: number },
  ): void => {
    const block = element(doc, "div", C.settingsField);
    block.dataset["part"] = name;
    const id = `${idPrefix}-vehicle-${name}`;
    const control = doc.createElement("input");
    control.type = "number";
    control.step = "0.1";
    control.min = String(range.min);
    control.max = String(range.max);
    control.inputMode = "decimal";
    control.className = C.settingsInput;
    control.id = id;
    values[name] = current === null ? "" : current.toFixed(1);
    control.value = values[name] ?? "";
    control.placeholder = current === null ? translate(language, "settings.capacity.unset") : "";
    control.addEventListener("input", () => {
      values[name] = control.value;
    });
    controls.push(control);
    const label = element(doc, "label", C.settingsLabel, translate(language, labelKey));
    label.setAttribute("for", id);
    const line = element(doc, "div", C.settingsRow);
    line.append(control, element(doc, "span", C.settingsUnit, unit));
    block.append(label, line, errorFor(errorField, control));
    body.append(block);
  };

  if (row === null) {
    // Only the sensor can be chosen for a vehicle the dashboard does not list.
  } else if (row.capacity_source === "reported" && row.capacity_kwh !== null) {
    const line = element(doc, "div", C.capabilityItem);
    line.dataset["row"] = "capacity";
    line.append(
      element(doc, "span", C.capabilityLabel, translate(language, "settings.capacity.label")),
      element(doc, "span", C.settingsValue, `${formatFixed(language, row.capacity_kwh, 1)} kWh`),
    );
    body.append(line, element(doc, "p", C.settingsNote, translate(language, "settings.vehicle.capacityReported")));
  } else {
    numberField("capacity", "capacity_kwh", "settings.capacity.label", "kWh", row.capacity_kwh, {
      min: CAPACITY_MIN_KWH,
      max: CAPACITY_MAX_KWH,
    });
  }
  if (row !== null) {
    numberField(
      "consumption",
      "consumption_kwh_per_10km",
      "settings.consumption.label",
      translate(language, "settings.consumption.unit"),
      row.consumption_kwh_per_10km,
      { min: CONSUMPTION_MIN_KWH_PER_10KM, max: CONSUMPTION_MAX_KWH_PER_10KM },
    );
  }

  const actions = element(doc, "div", C.settingsActions);
  const save = element(doc, "button", `${C.button} ${C.settingsSave}`, translate(language, "settings.save"));
  save.type = "submit";
  const cancel = element(doc, "button", C.button, translate(language, "settings.cancel"));
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  controls.push(save, cancel);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending) {
      handlers.onSave({ ...values });
    }
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });

  return {
    body,
    draft: () => ({ ...values }),
    markErrors(errors) {
      for (const [, entry] of errorNodes) {
        entry.node.hidden = true;
        entry.node.textContent = "";
        entry.node.removeAttribute("data-code");
        entry.input.removeAttribute("aria-invalid");
      }
      for (const error of errors) {
        const entry = errorNodes.get(error.field);
        if (entry === undefined) {
          continue;
        }
        entry.node.hidden = false;
        entry.node.textContent = translate(language, fieldErrorKey(error.code));
        entry.node.dataset["code"] = error.code;
        entry.input.setAttribute("aria-invalid", "true");
      }
    },
    setNotice(text, code) {
      notice.hidden = text === null;
      notice.textContent = text ?? "";
      if (code === null) {
        notice.removeAttribute("data-code");
      } else {
        notice.dataset["code"] = code;
      }
    },
    setPending(next) {
      pending = next;
      for (const control of controls) {
        control.disabled = pending;
      }
    },
    setHass() {},
  };
}
