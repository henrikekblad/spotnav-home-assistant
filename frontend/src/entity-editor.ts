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
  DETECT_WARNING_KEYS,
  GRID_TOTAL_FIELDS,
  NONE_VALUE,
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
import { measurementProblemText, meterUnavailableText, negativeCurrentText } from "./status";
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

function warningText(language: Language, warning: SiteWarning): string {
  const integration = warning.integration ?? "";
  if (warning.code === "measurement_unhealthy" && warning.negativePhases.length > 0) {
    return negativeCurrentText(language, warning.negativePhases);
  }
  if (warning.code === "measurement_unhealthy" && warning.unavailableEntities.length > 0) {
    return meterUnavailableText(language, warning.unavailableEntities, warning.inverter);
  }
  if (warning.code === "measurement_unhealthy") {
    const of = (cause: "no_value" | "stale") => warning.phases.filter((item) => item.cause === cause);
    return measurementProblemText(language, {
      noValuePhases: of("no_value").map((item) => item.phase),
      noValueEntities: of("no_value").flatMap((item) => (item.entityId === null ? [] : [item.entityId])),
      stalePhases: of("stale").map((item) => item.phase),
      maxAgeS: warning.intervalS,
    });
  }
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
    return lines.join(" ");
  }
  if (warning.code === "meter_updates_slowly") {
    return translate(language, "entity.warning.slowMeter", {
      name: warning.deviceName ?? warning.entityId ?? integration,
      minutes: formatNumber(language, Math.max(1, Math.round((warning.intervalS ?? 0) / 60)), 0),
    });
  }
  if (warning.code === "reports_on_change_only") {
    return translate(language, "entity.warning.onChange", { integration });
  }
  if (warning.code === "own_load_balancing") {
    return translate(language, "entity.warning.ownBalancing", { name: warning.deviceName ?? integration, integration });
  }
  if (warning.code === "external_current_balancer") {
    return translate(language, "entity.warning.externalBalancer", { name: warning.deviceName ?? "", integration });
  }
  if (warning.code === "battery_import_limit_differs" && warning.limitsA !== null) {
    return translate(language, "entity.warning.batteryImportLimit", {
      battery: formatNumber(language, warning.limitsA.battery, 1),
      spotnav: formatNumber(language, warning.limitsA.spotnav, 1),
      integration,
    });
  }
  return translate(language, "entity.warning.unknown");
}

/** How much a site note matters: the lower the number, the earlier it is listed. */
const NOTE_RANK: Readonly<Record<string, number>> = {
  measurement_unhealthy: 0,
  own_load_balancing: 1,
  external_current_balancer: 2,
  meter_updates_slowly: 3,
  update_interval_exceeds_max_age: 4,
  reports_on_change_only: 5,
  estimated: 6,
};
const UNKNOWN_NOTE_RANK = 7;

interface SiteNote {
  code: string;
  kind: "notice" | "warning";
  text: string;
}

/** Every note the site has, most important first, each sentence once. */
function siteNotes(language: Language, site: EntitySite, includeEstimate = true): SiteNote[] {
  const notes: SiteNote[] = site.warnings.map((warning) => ({
    code: warning.code,
    kind: "warning",
    text: warningText(language, warning),
  }));
  if (includeEstimate && site.measurement.currentEstimated) {
    notes.push({
      code: "estimated",
      kind: "notice",
      text: translate(language, "entity.notice.estimated", {
        pf: formatNumber(language, site.measurement.assumedPowerFactor ?? 0.9, 1),
      }),
    });
  }
  const seen = new Set<string>();
  return notes
    .map((note, index) => ({ note, index }))
    .sort(
      (a, b) =>
        (NOTE_RANK[a.note.code] ?? UNKNOWN_NOTE_RANK) - (NOTE_RANK[b.note.code] ?? UNKNOWN_NOTE_RANK) || a.index - b.index,
    )
    .map(({ note }) => note)
    .filter((note) => {
      if (seen.has(note.text)) {
        return false;
      }
      seen.add(note.text);
      return true;
    });
}

/** The site's warnings (not the estimate notice), most important first, one worded row each. */
export function siteWarningRows(doc: Document, language: Language, site: EntitySite): HTMLElement[] {
  return siteNotes(language, site, false).map((note) => {
    const row = element(doc, "p", C.entityWarning, note.text);
    row.dataset["warning"] = note.code;
    return row;
  });
}

/** The one note that makes the measurement unusable: it stays on top, as a warning. */
const BLOCKING_NOTES: ReadonlySet<string> = new Set(["measurement_unhealthy"]);

/**
 * The site's blocking problems (phases that make the measurement unusable), as a list at the very top of
 * the dialog. `null` when there are none. Everything else is `siteChecks`.
 */
export function siteNotices(doc: Document, language: Language, site: EntitySite): HTMLElement | null {
  const notes = siteNotes(language, site).filter((note) => BLOCKING_NOTES.has(note.code));
  if (notes.length === 0) {
    return null;
  }
  const list = element(doc, "ul", C.entityNotices);
  list.dataset["notices"] = "blocking";
  for (const note of notes) {
    const item = element(doc, "li", C.entityWarning, note.text);
    item.dataset[note.kind] = note.code;
    list.append(item);
  }
  return list;
}

/**
 * The rest of what the site's measurement wants said, grouped under one "To check" heading in a normal
 * tone, one line each, most important first: devices that balance load themselves, meters seen to update
 * too seldom for load balancing, sources that update more slowly than the maximum age, and a current
 * estimated from power. `null` when there is nothing.
 */
export function siteChecks(doc: Document, language: Language, site: EntitySite): HTMLElement | null {
  const notes = siteNotes(language, site).filter((note) => !BLOCKING_NOTES.has(note.code));
  if (notes.length === 0) {
    return null;
  }
  const section = element(doc, "fieldset", C.siteFieldset);
  section.dataset["notices"] = "site";
  section.append(element(doc, "legend", C.siteLegend, translate(language, "entity.checks.title")));
  const list = element(doc, "ul", C.entityChecks);
  for (const note of notes) {
    const item = element(doc, "li", undefined, note.text);
    item.dataset[note.kind] = note.code;
    list.append(item);
  }
  section.append(list);
  return section;
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

/**
 * The restrictions the charger's control description puts on the current limit that the person should
 * know about, one sentence each; none for a charger whose current can be written freely (an OCPP
 * charger with ChangeConfiguration). A charger whose current SpotNav does not set has none to name.
 */
export function currentRestrictions(language: Language, control: EntityControl): string[] {
  if (control.current.kind === "none") {
    return [];
  }
  const { policy, capabilities } = control;
  const lines: string[] = [];
  if (policy.minIntervalS >= 60) {
    const wholeMinutes = policy.minIntervalS % 60 === 0 && policy.minIntervalS >= 120;
    lines.push(
      wholeMinutes
        ? translate(language, "control.limit.minutes", { count: formatNumber(language, policy.minIntervalS / 60, 0) })
        : translate(language, "control.limit.seconds", { count: formatNumber(language, policy.minIntervalS, 0) }),
    );
  }
  if (policy.flashStored || !policy.regulatorWrites) {
    lines.push(translate(language, "control.limit.flash"));
  }
  if (capabilities.startStop && !capabilities.regulatedCurrent) {
    lines.push(translate(language, "control.limit.stopOnly"));
  }
  if (policy.installationWide) {
    lines.push(translate(language, "control.limit.installation"));
  }
  return lines;
}

/**
 * What stays at the top of the charger dialog from the control description: the fixed-path sentence
 * for Easee and the charger's own modes that are on (or its enable switch off) and would fight
 * SpotNav. `null` when there is neither. Drawn from the decoded configuration; nothing here asks the
 * backend.
 */
export function controlNotes(
  doc: Document,
  language: Language,
  control: EntityControl,
  nameOf: (entityId: string) => string,
): HTMLElement | null {
  const nodes: HTMLElement[] = [];
  if (control.startStop.kind === "easee") {
    // Its charge-control entity is only the charger's identity, so nothing is offered to pick.
    const fixed = element(doc, "p", C.entityHelp, translate(language, "control.startStop.easeeFixed"));
    fixed.dataset["controlRow"] = "start_stop_fixed";
    nodes.push(fixed);
  }
  for (const conflict of control.conflicts) {
    const warning = element(doc, "p", C.entityWarning, conflictText(language, conflict, nameOf(conflict.entityId)));
    warning.dataset["conflict"] = conflict.entityId;
    nodes.push(warning);
  }
  if (nodes.length === 0) {
    return null;
  }
  const block = element(doc, "div", C.entityRow);
  block.dataset["control"] = "path";
  block.append(...nodes);
  return block;
}

/** The sentence for one conflict: a charger's own mode that is on, or its own enable switch that is off. */
export function conflictText(language: Language, conflict: ControlConflict, name: string): string {
  if (conflict.kind === "duplicate_charger") {
    return translate(language, "issue.duplicateCharger", { other: conflict.state });
  }
  if (conflict.kind === "other_controller") {
    return translate(language, "control.otherController", { name: conflict.label });
  }
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
  /** A charger in a site: open the site's entities, where the wiring is held. */
  onOpenSite?: () => void;
}

export interface EntityEditorInput {
  scope: EntityScope;
  config: EntityConfig;
  hass: () => unknown;
  appliesText: string | null;
  readOnly?: boolean;
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
  // A reader who may not change entities sees the same layout, disabled (only Cancel stays usable).
  const locked = input.readOnly === true;
  const keepEnabled = new Set<HTMLElement>();

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
    const notes = controlNotes(doc, language, config.control, (entityId) => entityNameIn(config, entityId));
    if (notes !== null) {
      body.append(notes);
    }
  }
  // Dismissible: the site's power entities match a detected meter whose sign the stored flag contradicts.
  const signNotice = element(doc, "div", C.entityWarning);
  signNotice.dataset["signNotice"] = "grid";
  signNotice.hidden = true;
  let signDismissed = false;
  function refreshSignNotice(): void {
    const site = config.site;
    const mismatch = scope === "site" && site !== null && !signDismissed ? signMismatch(site) : null;
    signNotice.hidden = mismatch === null;
    if (mismatch === null) {
      signNotice.replaceChildren();
      return;
    }
    const dismiss = element(doc, "button", C.button, translate(language, "entity.signNotice.dismiss"));
    dismiss.type = "button";
    dismiss.addEventListener("click", () => {
      signDismissed = true;
      refreshSignNotice();
    });
    signNotice.dataset["want"] = mismatch.inverted ? "on" : "off";
    signNotice.replaceChildren(
      element(
        doc,
        "span",
        undefined,
        translate(language, mismatch.inverted ? "entity.signNotice.on" : "entity.signNotice.off", {
          title: mismatch.title,
        }),
      ),
      dismiss,
    );
  }
  function signMismatch(site: EntitySite): { title: string; inverted: boolean } | null {
    const chosen = (name: string): string => {
      const typed = values[name];
      if (typed !== undefined) {
        return typed.trim();
      }
      const field = config.fields.find((entry) => entry.field === name);
      return field !== undefined && field.kind === "entity" && field.current !== null ? field.current.entityId : "";
    };
    const used = new Set(
      [...PHASES.map((phase) => derivedFieldName(phase, "power")), GRID_TOTAL_FIELDS[0]]
        .map(chosen)
        .filter((entityId) => entityId !== ""),
    );
    const stored = values["grid_power_inverted"] === undefined
      ? config.fields.some((entry) => entry.field === "grid_power_inverted" && entry.kind === "flag" && entry.value)
      : isOn("grid_power_inverted");
    for (const meter of site.meters) {
      const powers = meter.entities.filter((entity) => entity.role === "power" || entity.role === "grid_power");
      if (powers.length > 0 && powers.every((entity) => used.has(entity.entityId)) && meter.powerInverted !== stored) {
        return { title: meter.title, inverted: meter.powerInverted };
      }
    }
    return null;
  }
  if (scope === "site" && config.site !== null) {
    const notices = siteNotices(doc, language, config.site);
    if (notices !== null) {
      body.append(notices);
    }
    body.append(signNotice);
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

  /**
   * The charger fields whose radios say Automatic, Choose (and for the current limit None): the current
   * limit always, the energy register only when something is found for it.
   */
  function isChoiceGrouped(field: EntityFieldEntity): boolean {
    return (
      scope === "charger" &&
      field.writable &&
      (field.field === "current_limit" || (field.field === "energy_register_entity" && automaticEntity(field) !== null))
    );
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
    if (field.kind === "entity" && isChoiceGrouped(field)) {
      // Said once, by the group's radios.
      return;
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
    const control = entityControl(field, label);
    if (!showLabel && control.picker !== null) {
      // The heading above says it; the picker would say it a second time.
      (control.picker as PickerElement).label = "";
    }
    return fieldBlock(field.field, label, control, showLabel, field);
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
      refreshSignNotice();
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

  // Fields that belong to one choice (grid power, battery, energy) are built here like any other, kept
  // aside and placed by their group: only the chosen variant's fields are in the dialog.
  const MANAGED_SITE = new Set<string>([
    "site_current_signed",
    ...GRID_TOTAL_FIELDS,
    "grid_power_inverted",
    "battery_aggregate_power_entity",
    "battery_discharge_power_entity",
    "battery_power_inverted",
    "max_age_s",
  ]);
  const MANAGED_CHARGER = new Set<string>(["current_limit", "energy_register_entity", "power_entity"]);
  const managed = new Map<string, HTMLElement>();
  const isManaged = (name: string): boolean =>
    scope === "site" ? MANAGED_SITE.has(name) : MANAGED_CHARGER.has(name);

  for (const field of fieldsOf(config, scope)) {
    if (!field.writable || isPhaseField(field.field) || field.field === "measurement_mode") {
      continue;
    }
    const label = labelOf(language, field.field);
    let block: HTMLElement | null = null;
    if (field.kind === "entity" && scope === "charger" && field.field === "charge_control") {
      // Headed like the Current limit and Energy groups: one heading style in the dialog.
      block = element(doc, "fieldset", C.siteFieldset);
      block.dataset["part"] = "charge-control";
      block.append(element(doc, "legend", C.siteLegend, label), entityField(field, label, false));
    } else if (field.kind === "entity") {
      block = entityField(field, label);
    } else if (field.kind === "flag") {
      block = flagField(field, label);
    } else if (field.kind === "number") {
      const row = fieldBlock(field.field, label, numberControl(field, label), true, field);
      row.querySelector("label")?.classList.add(C.entityNumberLabel);
      const control = row.querySelector<HTMLElement>("input");
      if (control !== null) {
        const line = element(doc, "div", C.settingsRow);
        control.replaceWith(line);
        line.append(control, element(doc, "span", C.settingsUnit, field.field === "main_fuse_a" || field.field === "safety_margin_a" ? "A" : "s"));
      }
      block = row;
    }
    if (block === null) {
      continue;
    }
    if (isManaged(field.field)) {
      managed.set(field.field, block);
    } else {
      body.append(block);
    }
  }

  const blocksOf = (...names: string[]): HTMLElement[] =>
    names.flatMap((name) => {
      const block = managed.get(name);
      return block === undefined ? [] : [block];
    });

  interface ChoiceGroup {
    fieldset: HTMLElement;
    fields: HTMLElement;
    sync: () => void;
    showNote: (mixed: boolean) => void;
  }

  /**
   * One radio group: choose the kind first, then `fields` holds only that kind's fields. `title` is the
   * legend (none for a group nested under another's option), `intro` a line under it, `fieldsAfter`
   * the option whose line the fields follow (default: after the last).
   */
  function choiceGroup(
    part: string,
    title: TranslationKey | null,
    options: ReadonlyArray<{ value: string; label: TranslationKey; text?: string }>,
    get: () => string,
    set: (value: string) => void,
    extra: { intro?: HTMLElement; fieldsAfter?: string } = {},
  ): ChoiceGroup {
    const fieldset = element(doc, "fieldset", C.siteFieldset);
    fieldset.dataset["part"] = part;
    if (title !== null) {
      fieldset.append(element(doc, "legend", C.siteLegend, translate(language, title)));
    }
    if (extra.intro !== undefined) {
      fieldset.append(extra.intro);
    }
    const fields = element(doc, "div");
    fields.dataset["choiceFields"] = part;
    const radios: HTMLInputElement[] = [];
    for (const option of options) {
      const line = element(doc, "label", C.siteChoice);
      const radio = doc.createElement("input");
      radio.type = "radio";
      radio.name = `${idPrefix}-choice-${part}`;
      radio.value = option.value;
      radio.dataset["choice"] = option.value;
      radio.checked = get() === option.value;
      radio.addEventListener("change", () => {
        if (radio.checked) {
          set(option.value);
        }
      });
      disabledWhenPending.push(radio);
      radios.push(radio);
      line.append(radio, doc.createTextNode(option.text ?? translate(language, option.label)));
      fieldset.append(line);
      if (extra.fieldsAfter === option.value) {
        fieldset.append(fields);
      }
    }
    const note = element(doc, "p", C.entityHelp, translate(language, "entity.choice.mixed"));
    note.dataset["choiceNote"] = part;
    note.hidden = true;
    fieldset.append(note);
    if (fields.parentElement === null) {
      fieldset.append(fields);
    }
    return {
      fieldset,
      fields,
      sync() {
        for (const radio of radios) {
          radio.checked = get() === radio.value;
        }
      },
      showNote(mixed) {
        note.hidden = !mixed;
      },
    };
  }

  /** A grouped field's error line stays with its group, whichever radio is chosen. */
  function keepErrorWith(group: ChoiceGroup, name: string): void {
    const entry = errorNodes.get(name);
    if (entry !== undefined) {
      group.fieldset.append(entry.node);
    }
  }

  const fieldHelp = (name: string, key: TranslationKey): HTMLElement => {
    const help = element(doc, "p", C.entityHelp, translate(language, key));
    help.dataset["help"] = name;
    return help;
  };

  // What a save clears: the fields of every variant that is not chosen, so nothing lingers.
  const clearers: Array<(draft: EntityDraft) => void> = [];
  // What a save refuses: fields the resulting draft must fill, each shown with its own error.
  const requirers: Array<(draft: EntityDraft) => string[]> = [];

  const isSet = (name: string): boolean => (values[name] ?? "").trim() !== "";
  const isOn = (name: string): boolean => values[name] === "true";

  if (scope === "charger") {
    // The current limit: the one SpotNav finds itself, an entity of the person's choice, or none.
    const limitField = config.fields.find((entry) => entry.field === "current_limit");
    if (limitField !== undefined && limitField.kind === "entity" && limitField.writable && managed.has("current_limit")) {
      const automatic = automaticEntity(limitField);
      const noneChosen = limitField.none !== null && limitField.none.chosen;
      const noneOffered = limitField.none !== null && (limitField.none.allowed || noneChosen);
      // The draft holds the picked entity; "None" is only said by the radio, on save.
      if (values["current_limit"] === NONE_VALUE) {
        values["current_limit"] = "";
      }
      // Without an automatic value or a stored entity, "none" is what is already so, and it is not
      // sent (storing it would also switch the current control off).
      const impliedNone = !noneChosen && automatic === null && limitField.current === null;
      let limitKind = noneChosen
        ? "none"
        : limitField.current !== null
          ? "choose"
          : automatic !== null
            ? "automatic"
            : "choose";
      const options: Array<{ value: string; label: TranslationKey; text?: string }> = [];
      if (automatic !== null) {
        options.push({
          value: "automatic",
          label: "entity.automatic",
          text: translate(language, "entity.automatic", { name: automatic.friendlyName }),
        });
      }
      options.push({ value: "choose", label: "entity.choice.choose" });
      if (noneOffered) {
        options.push({ value: "none", label: "entity.limit.none" });
      }
      const paintLimit = (): void => {
        limitGroup.fields.replaceChildren(...(limitKind === "choose" ? blocksOf("current_limit") : []));
        applyPending();
      };
      const limitGroup = choiceGroup(
        "current-limit",
        "entity.field.currentLimit",
        options,
        () => limitKind,
        (value) => {
          limitKind = value;
          paintLimit();
        },
        { intro: fieldHelp("current_limit", "entity.help.currentLimit"), fieldsAfter: "choose" },
      );
      clearers.push((draft) => {
        if (limitKind === "automatic") {
          draft["current_limit"] = "";
        } else if (limitKind === "none") {
          draft["current_limit"] = impliedNone ? "" : NONE_VALUE;
        }
      });
      keepErrorWith(limitGroup, "current_limit");
      const restrictions = config.control === null ? [] : currentRestrictions(language, config.control);
      if (restrictions.length > 0) {
        const line = element(doc, "p", C.entityHelp, restrictions.join(" "));
        line.dataset["limitRestrictions"] = "current";
        limitGroup.fieldset.querySelector("[data-help='current_limit']")?.after(line);
      }
      body.append(limitGroup.fieldset);
      paintLimit();
    }

    // The energy the charger has delivered: its own kWh register, a smart plug's power, or nothing.
    const hasRegister = managed.has("energy_register_entity");
    const hasPlug = managed.has("power_entity");
    const registerField = config.fields.find((entry) => entry.field === "energy_register_entity");
    const found = registerField !== undefined && registerField.kind === "entity" && automaticEntity(registerField) !== null;
    const energyKind = (): string =>
      isSet("energy_register_entity") || found ? "meter" : hasPlug && isSet("power_entity") ? "power" : "none";
    let energy = energyKind();
    const energyMixed = isSet("energy_register_entity") && isSet("power_entity");
    if (hasRegister || hasPlug) {
      const options: Array<{ value: string; label: TranslationKey }> = [];
      if (hasRegister) {
        options.push({ value: "meter", label: "entity.energy.meter" });
      }
      if (hasPlug) {
        options.push({ value: "power", label: "entity.energy.power" });
      }
      options.push({ value: "none", label: "entity.energy.none" });
      // Under the meter: the register SpotNav finds itself, or one the person chooses.
      let registerKind = isSet("energy_register_entity") ? "choose" : "automatic";
      const paintRegister = (): void => {
        registerGroup?.fields.replaceChildren(...(registerKind === "choose" ? blocksOf("energy_register_entity") : []));
        applyPending();
      };
      const foundRegister = registerField !== undefined && registerField.kind === "entity" ? automaticEntity(registerField) : null;
      const registerGroup =
        hasRegister && foundRegister !== null
          ? choiceGroup(
              "energy-source",
              null,
              [
                {
                  value: "automatic",
                  label: "entity.automatic",
                  text: translate(language, "entity.automatic", { name: foundRegister.friendlyName }),
                },
                { value: "choose", label: "entity.choice.choose" },
              ],
              () => registerKind,
              (value) => {
                registerKind = value;
                paintRegister();
              },
              { intro: fieldHelp("energy_register_entity", "entity.help.energyRegister"), fieldsAfter: "choose" },
            )
          : null;
      if (registerGroup !== null) {
        keepErrorWith(registerGroup, "energy_register_entity");
        paintRegister();
      }
      const paintEnergy = (): void => {
        group.fields.replaceChildren(
          ...(energy === "meter"
            ? registerGroup !== null
              ? [registerGroup.fieldset]
              : blocksOf("energy_register_entity")
            : energy === "power"
              ? blocksOf("power_entity")
              : []),
        );
        applyPending();
      };
      const group = choiceGroup("energy", "entity.energy.title", options, () => energy, (value) => {
        energy = value;
        paintEnergy();
      });
      group.showNote(energyMixed);
      clearers.push((draft) => {
        if (energy !== "meter" || (registerGroup !== null && registerKind === "automatic")) {
          draft["energy_register_entity"] = "";
        }
        if (energy !== "power") {
          draft["power_entity"] = "";
        }
      });
      body.append(group.fieldset);
      paintEnergy();
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
      const head = element(doc, "div");
      head.dataset["part"] = "head";
      const phases = element(doc, "div", C.entityMeters);
      phases.dataset["part"] = "phases";
      const tail = element(doc, "div");
      tail.dataset["part"] = "tail";
      const currentMode = (): string => values["measurement_mode"] ?? storedMode(config) ?? MEASUREMENT_DIRECT;
      const phaseValue = (name: string): string => values[name] ?? phaseField(config, name).current?.entityId ?? "";
      const anyPhase = (kind: string): boolean =>
        PHASES.some((phase) => phaseValue(derivedFieldName(phase, kind)).trim() !== "");

      // The radios are read from what is stored; a mixed state opens on the variant in use by precedence.
      const CURRENT_KINDS: ReadonlyArray<readonly [string, string]> = [
        ["measured", "current"],
        ["apparent", "apparent_power"],
        ["reactive", "reactive_power"],
      ];
      const usedKinds = CURRENT_KINDS.filter(([, kind]) => anyPhase(kind));
      let currentKind = usedKinds[0]?.[0] ?? "estimated";
      const currentMixed = usedKinds.length > 1;
      const gridFromValues = (): string =>
        (currentMode() === MEASUREMENT_DERIVED ? anyPhase("power_export") : phaseValue(GRID_TOTAL_FIELDS[1]).trim() !== "")
          ? "two"
          : "one";
      let gridKind = gridFromValues();
      let batteryKind = isSet("battery_discharge_power_entity")
        ? "two"
        : isSet("battery_aggregate_power_entity")
          ? "one"
          : "none";

      const paintPhases = (): void => {
        for (const name of phaseFieldNames(MEASUREMENT_DIRECT).concat(phaseFieldNames(MEASUREMENT_DERIVED))) {
          errorNodes.delete(name);
        }
        for (const picker of [...pickers]) {
          if (picker.dataset["field"] !== undefined && isPhaseField(picker.dataset["field"])) {
            pickers.delete(picker);
          }
        }
        phases.replaceChildren();
        const mode = currentMode();
        phases.dataset["mode"] = mode;
        const derived = mode === MEASUREMENT_DERIVED;
        const currentName = CURRENT_KINDS.find(([choice]) => choice === currentKind)?.[1] ?? null;
        // Each phase repeats the same fields, so their help is said once, under the last phase.
        const phaseHelp = element(doc, "div");
        phaseHelp.dataset["help"] = "phases";
        // In derived mode the help sits under the grid power and current choices, not repeated here.
        if (!derived) {
          phaseHelp.append(element(doc, "p", C.entityHelp, translate(language, "entity.help.phaseDirect")));
        }
        for (const phase of PHASES) {
          const group = element(doc, "fieldset", C.entityLine);
          group.dataset["phase"] = phase;
          group.append(element(doc, "legend", C.siteLegend, phase));
          const cells = element(doc, "div", C.entityLineCells);
          const names = derived
            ? [
                ...DERIVED_REQUIRED_KINDS.map((kind) => derivedFieldName(phase, kind)),
                ...(gridKind === "two" ? [derivedFieldName(phase, "power_export")] : []),
                ...(currentName === null ? [] : [derivedFieldName(phase, currentName)]),
              ]
            : [directFieldName(phase)];
          for (const name of names) {
            const field = phaseField(config, name);
            if (values[name] === undefined) {
              values[name] = field.current === null ? "" : field.current.entityId;
            }
            cells.append(entityField(field, labelOf(language, name)));
          }
          group.append(cells);
          phases.append(group);
        }
        phases.append(phaseHelp);
        applyPending();
      };

      // Grid power: one sensor with a direction, or import and export as two. Per phase in derived mode,
      // the meter's total (for solar) in direct mode.
      const grid = choiceGroup(
        "grid",
        "entity.grid.title",
        [
          { value: "one", label: "entity.grid.one" },
          { value: "two", label: "entity.grid.two" },
        ],
        () => gridKind,
        (value) => {
          gridKind = value;
          paintGrid();
          if (currentMode() === MEASUREMENT_DERIVED) {
            paintPhases();
          }
        },
      );
      // The sign of the grid current applies to measured currents: always in direct mode, in derived mode
      // only when the meter's own current is what is read.
      const signedShown = (): boolean => currentMode() !== MEASUREMENT_DERIVED || currentKind === "measured";
      const paintGrid = (): void => {
        const derived = currentMode() === MEASUREMENT_DERIVED;
        const help = element(
          doc,
          "p",
          C.entityHelp,
          [
            translate(language, "entity.help.derivedPower"),
            translate(language, "entity.help.derivedVoltage"),
            ...(gridKind === "two"
              ? [translate(language, "entity.help.derivedPowerExport"), translate(language, "entity.help.derivedTwoSensors")]
              : []),
          ].join(" "),
        );
        help.dataset["help"] = "grid-phases";
        grid.fields.replaceChildren(
          ...(derived ? [help] : blocksOf(GRID_TOTAL_FIELDS[0], ...(gridKind === "two" ? [GRID_TOTAL_FIELDS[1]] : []))),
          ...(gridKind === "one" ? blocksOf("grid_power_inverted") : []),
        );
        grid.showNote(gridKind === "two" && isOn("grid_power_inverted"));
        refreshSignNotice();
        applyPending();
      };

      // Current, derived mode only: one kind for every phase.
      const current = choiceGroup(
        "current-source",
        "entity.current.title",
        [
          { value: "measured", label: "entity.current.measured" },
          { value: "apparent", label: "entity.current.apparent" },
          { value: "reactive", label: "entity.current.reactive" },
          { value: "estimated", label: "entity.current.estimated" },
        ],
        () => currentKind,
        (value) => {
          currentKind = value;
          paintCurrent();
          paintPhases();
        },
      );
      current.showNote(currentMixed);
      const paintCurrent = (): void => {
        const kind = CURRENT_KINDS.find(([choice]) => choice === currentKind)?.[1] ?? null;
        const helpKey = kind === null ? "entity.current.estimatedNote" : fieldHelpKey(derivedFieldName("L1", kind));
        const help = element(doc, "p", C.entityHelp, helpKey === null ? "" : translate(language, helpKey));
        help.dataset["help"] = kind === null ? "current-estimated" : "current-source";
        current.fields.replaceChildren(help, ...(signedShown() ? blocksOf("site_current_signed") : []));
      };

      const battery = choiceGroup(
        "battery",
        "entity.battery.title",
        [
          { value: "none", label: "entity.battery.none" },
          { value: "one", label: "entity.battery.one" },
          { value: "two", label: "entity.battery.two" },
        ],
        () => batteryKind,
        (value) => {
          batteryKind = value;
          paintBattery();
        },
      );
      const paintBattery = (): void => {
        battery.fields.replaceChildren(
          ...(batteryKind === "none" ? [] : blocksOf("battery_aggregate_power_entity")),
          ...(batteryKind === "two" ? blocksOf("battery_discharge_power_entity") : []),
          ...(batteryKind === "one" ? blocksOf("battery_power_inverted") : []),
        );
        battery.showNote(batteryKind !== "one" && isOn("battery_power_inverted"));
        applyPending();
      };

      const layoutMode = (): void => {
        const derived = currentMode() === MEASUREMENT_DERIVED;
        gridKind = gridFromValues();
        grid.sync();
        paintGrid();
        paintCurrent();
        head.replaceChildren(...(derived ? [grid.fieldset, current.fieldset] : []));
        tail.replaceChildren(
          ...(derived ? [] : [grid.fieldset, ...blocksOf("site_current_signed")]),
          battery.fieldset,
          ...blocksOf("max_age_s"),
        );
        paintPhases();
      };

      // With "two sensors" chosen, every shown export field must be filled: an empty one would drop the
      // sign of a single sensor and store a pair that is not one.
      const missingExports = (draft: EntityDraft): string[] => {
        if (gridKind !== "two") {
          return [];
        }
        const names =
          currentMode() === MEASUREMENT_DERIVED
            ? PHASES.map((phase) => derivedFieldName(phase, "power_export"))
            : [GRID_TOTAL_FIELDS[1]];
        return names.filter((name) => (draft[name] ?? "").trim() === "");
      };
      requirers.push(missingExports);

      clearers.push((draft) => {
        const derived = currentMode() === MEASUREMENT_DERIVED;
        if (derived) {
          const keep = CURRENT_KINDS.find(([choice]) => choice === currentKind)?.[1] ?? null;
          for (const phase of PHASES) {
            for (const [, kind] of CURRENT_KINDS) {
              if (kind !== keep) {
                draft[derivedFieldName(phase, kind)] = "";
              }
            }
            if (gridKind === "one") {
              draft[derivedFieldName(phase, "power_export")] = "";
            }
          }
        } else if (gridKind === "one") {
          draft[GRID_TOTAL_FIELDS[1]] = "";
        }
        if (gridKind === "two" && missingExports(draft).length === 0) {
          // Only a complete pair has no sign to keep: an incomplete one is refused before this runs.
          draft["grid_power_inverted"] = "false";
        }
        if (!signedShown()) {
          draft["site_current_signed"] = "false";
        }
        if (batteryKind === "none") {
          draft["battery_aggregate_power_entity"] = "";
        }
        if (batteryKind !== "two") {
          draft["battery_discharge_power_entity"] = "";
        }
        if (batteryKind !== "one") {
          draft["battery_power_inverted"] = "false";
        }
      });

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
            layoutMode();
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
      body.append(fieldset, head, phases, tail);
      paintBattery();
      layoutMode();
    }
    refreshSignNotice();
  }

  // The voltage between two phases: 400 V (the usual TN network) or 230 V (an IT network, as in much of
  // Norway). A site holds it for its chargers; a charger in no site holds its own. Chosen like a type.
  const voltageField = fieldsOf(config, scope).find((entry) => entry.field === "voltage_between_phases_v");
  if (voltageField !== undefined && voltageField.kind === "enum" && voltageField.writable) {
    const voltageOptions = voltageField.choices.map((choice) => ({
      value: choice,
      label: (choice === "230" ? "entity.voltage.it" : "entity.voltage.tn") as TranslationKey,
    }));
    const voltage = choiceGroup(
      "voltage",
      "entity.field.voltageBetweenPhases",
      voltageOptions,
      () => values["voltage_between_phases_v"] ?? voltageField.value ?? "400",
      (value) => {
        values["voltage_between_phases_v"] = value;
      },
      { intro: fieldHelp("voltage_between_phases_v", "entity.help.voltageBetweenPhases") },
    );
    const voltageError = element(doc, "p", C.settingsError);
    voltageError.hidden = true;
    voltageError.dataset["fieldError"] = "voltage_between_phases_v";
    voltageError.setAttribute("role", "alert");
    errorNodes.set("voltage_between_phases_v", { node: voltageError, input: voltage.fieldset });
    voltage.fieldset.append(voltageError);
    body.append(voltage.fieldset);
  }

  // The phases the charger is wired for, for a charger in no site (a site holds the wiring of its chargers).
  // A charge uses the smaller of this and the car's onboard charger. Chosen like a type.
  const phasesField = fieldsOf(config, scope).find((entry) => entry.field === "charger_phases");
  if (phasesField !== undefined && phasesField.kind === "enum" && phasesField.writable) {
    const phasesOptions = phasesField.choices.map((choice) => ({
      value: choice,
      label: (choice === "1" ? "settings.phases.one" : "settings.phases.three") as TranslationKey,
    }));
    const wired = choiceGroup(
      "charger-phases",
      "entity.field.chargerPhases",
      phasesOptions,
      () => values["charger_phases"] ?? phasesField.value ?? "3",
      (value) => {
        values["charger_phases"] = value;
      },
      { intro: fieldHelp("charger_phases", "entity.help.chargerPhases") },
    );
    const phasesError = element(doc, "p", C.settingsError);
    phasesError.hidden = true;
    phasesError.dataset["fieldError"] = "charger_phases";
    phasesError.setAttribute("role", "alert");
    errorNodes.set("charger_phases", { node: phasesError, input: wired.fieldset });
    wired.fieldset.append(phasesError);
    body.append(wired.fieldset);
  }

  // A charger in a site: the site's wiring says how many phases it is wired for. Read-only here, with the way
  // to where it is changed.
  if (phasesField !== undefined && phasesField.kind === "enum" && !phasesField.writable) {
    const wiredLine = element(doc, "p", C.siteApplies);
    wiredLine.dataset["wiredPhases"] = phasesField.value ?? "3";
    wiredLine.append(
      translate(language, "entity.phases.fromSite", {
        phases: translate(language, phasesField.value === "1" ? "settings.phases.one" : "settings.phases.three"),
      }),
    );
    if (handlers.onOpenSite !== undefined && !locked) {
      const link = element(doc, "button", C.strategyLink, translate(language, "entity.phases.openSite"));
      link.type = "button";
      link.dataset["action"] = "open-site";
      link.addEventListener("click", () => handlers.onOpenSite?.());
      wiredLine.append(" ", link);
      disabledWhenPending.push(link);
    }
    body.append(wiredLine);
  }

  // Where the site reads this charger's own measured current from, or that it reads none (then a regulator
  // and solar have nothing to size the charge against). Read-only: it is set in the site's wiring.
  const sourceField = fieldsOf(config, scope).find((entry) => entry.field === "measured_current_source");
  if (sourceField !== undefined && sourceField.kind === "enum") {
    const sourceLine = element(doc, "p", C.siteApplies);
    sourceLine.dataset["measuredSource"] = sourceField.value ?? "";
    sourceLine.append(
      sourceField.value === null || sourceField.value === ""
        ? translate(language, "entity.measuredSource.none")
        : translate(language, "entity.measuredSource.from", { source: sourceField.value }),
    );
    body.append(sourceLine);
  }

  // The charger's place in its site's allocation order: first, normal (the default) or last.
  const priorityField = fieldsOf(config, scope).find((entry) => entry.field === "charger_priority");
  if (priorityField !== undefined && priorityField.kind === "enum" && priorityField.writable) {
    const priorityOptions = priorityField.choices.map((choice) => ({
      value: choice,
      label: (choice === "first"
        ? "entity.priority.first"
        : choice === "last"
          ? "entity.priority.last"
          : "entity.priority.normal") as TranslationKey,
    }));
    const priority = choiceGroup(
      "priority",
      "entity.field.chargerPriority",
      priorityOptions,
      () => values["charger_priority"] ?? priorityField.value ?? "normal",
      (value) => {
        values["charger_priority"] = value;
      },
      { intro: fieldHelp("charger_priority", "entity.help.chargerPriority") },
    );
    const priorityError = element(doc, "p", C.settingsError);
    priorityError.hidden = true;
    priorityError.dataset["fieldError"] = "charger_priority";
    priorityError.setAttribute("role", "alert");
    errorNodes.set("charger_priority", { node: priorityError, input: priority.fieldset });
    priority.fieldset.append(priorityError);
    body.append(priority.fieldset);
  }

  // The site's other notes, after the measurement fields.
  if (scope === "site" && config.site !== null) {
    const checks = siteChecks(doc, language, config.site);
    if (checks !== null) {
      body.append(checks);
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
  keepEnabled.add(cancel);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending && !locked) {
      const missing = requirers.flatMap((require) => require({ ...values }));
      if (missing.length > 0) {
        markErrors(missing.map((field) => ({ field, code: "required" })));
        return;
      }
      handlers.onSave(resolvedDraft());
    }
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });

  function markErrors(errors: readonly EntityFieldError[]): void {
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
  }

  /** The draft as a save sends it: every field of an unchosen variant cleared. */
  function resolvedDraft(): EntityDraft {
    const draft: EntityDraft = { ...values };
    for (const clear of clearers) {
      clear(draft);
    }
    return draft;
  }

  function applyPending(): void {
    for (const control of disabledWhenPending) {
      control.disabled = pending || (locked && !keepEnabled.has(control));
    }
    for (const picker of pickers) {
      picker.disabled = pending || locked;
    }
  }
  applyPending();

  return {
    body,
    draft: resolvedDraft,
    markErrors,
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
 * detection, which is all the backend accepts), battery capacity, consumption and onboard charger. Reports the draft as
 * text: `soc` (entity id, `""` for automatic; absent without a sensor block), `capacity` (absent when the
 * vehicle reports it itself), `consumption` and `onboard` (`"1"` or `"3"`). The card judges it and sends the changes.
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

  if (row !== null) {
    // The car's own charger: 1 or 3 phases. A charge uses the smaller of this and the charger's wiring.
    values["onboard"] = String(row.onboard_phases);
    const group = element(doc, "fieldset", C.siteFieldset);
    group.dataset["part"] = "onboard";
    group.append(
      element(doc, "legend", C.siteLegend, translate(language, "settings.vehicle.onboardLegend")),
      element(doc, "p", C.entityHelp, translate(language, "settings.vehicle.onboardHelp")),
    );
    for (const count of ["1", "3"] as const) {
      const line = element(doc, "label", C.siteChoice);
      const radio = doc.createElement("input");
      radio.type = "radio";
      radio.name = `${idPrefix}-vehicle-onboard`;
      radio.value = count;
      radio.checked = values["onboard"] === count;
      radio.dataset["onboard"] = count;
      radio.addEventListener("change", () => {
        if (radio.checked) {
          values["onboard"] = count;
        }
      });
      controls.push(radio);
      line.append(
        radio,
        doc.createTextNode(translate(language, count === "1" ? "settings.vehicle.onboardOne" : "settings.vehicle.onboardThree")),
      );
      group.append(line);
    }
    group.append(errorFor("onboard_phases", group));
    body.append(group);
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
