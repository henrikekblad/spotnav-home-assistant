// The heat settings' editors, one setting per dialog, and the write they share: used by the shared settings popover
// (`settings-popover.ts`) from every card, and by the heat card's away button. Shadow only: what is written is
// SpotNav's own heat settings for the site (`spotnav/update_heat`), never a heating device.

import { updateHeat, type SiteTarget } from "./api";
import { decodeHeatWrite, isRelayPath, roomName, type HeatAnswer, type HeatChoice, type HeatConfig } from "./heat-model";
import { HEAT_CLASSES as X } from "./heat-styles";
import { instantToLocal, localToInstant, namesOf, type HeatSettingKey } from "./heat-view";
import { translate, type Language, type TranslationKey } from "./i18n";
import type { HomeAssistantLike } from "./types";
import { multiEditor, numberEditor, onOffEditor, shell, singleEditor, type ChoiceOption, type EditorHandlers } from "./value-editors";
import { VISUAL_CLASSES } from "./visual-styles";

/** The return time the away editor offers first. */
const DEFAULT_RETURN = "16:00";

export interface HeatEditorInput {
  doc: Document;
  language: Language;
  idPrefix: string;
  answer: HeatAnswer;
  /**
   * Writes `changes`; `null` when it took, else the sentence to show. `enable`: the person confirmed that the
   * control path's disabled entities are to be enabled with it.
   */
  write: (changes: Partial<Record<keyof HeatConfig, unknown>>, options?: HeatWriteOptions) => Promise<string | null>;
}

export interface HeatWriteOptions {
  enable?: boolean;
}

const NUMBERS = /^(number|input_number)\./;

/** A refused write in words. */
export function writeError(language: Language, error: string | null, fieldCodes: readonly string[]): string {
  if (error === "spotnav_conflict") {
    return translate(language, "heat.error.conflict");
  }
  if (error === "spotnav_not_admin") {
    return translate(language, "heat.error.notAdmin");
  }
  const byCode: Record<string, TranslationKey> = {
    out_of_range: "heat.error.range",
    not_a_step: "heat.error.step",
    away_in_past: "heat.error.past",
    away_too_far: "heat.error.far",
  };
  const known = fieldCodes.map((code) => byCode[code]).find((key) => key !== undefined);
  return translate(language, known ?? "heat.error.generic");
}

/**
 * Write `changes` with what `answer` last showed for the same keys (a compare-and-set): the sentence when refused,
 * else `null`, and the stored settings as answered.
 */
export async function writeHeat(
  hass: HomeAssistantLike,
  target: SiteTarget,
  answer: HeatAnswer,
  changes: Partial<Record<keyof HeatConfig, unknown>>,
  language: Language,
  options: HeatWriteOptions = {},
): Promise<{ error: string | null; config: HeatConfig | null }> {
  const expected: Record<string, unknown> = {};
  for (const key of Object.keys(changes) as (keyof HeatConfig)[]) {
    expected[key] = answer.config[key];
  }
  let raw: unknown;
  try {
    raw = await updateHeat(hass, target, changes, expected, options.enable === true);
  } catch {
    return { error: translate(language, "heat.error.generic"), config: null };
  }
  const decoded = decodeHeatWrite(raw);
  if (!decoded.ok) {
    return { error: translate(language, "heat.error.generic"), config: null };
  }
  const result = decoded.value;
  return {
    error: result.ok ? null : writeError(language, result.error, result.field_errors.map((item) => item.code)),
    config: result.ok ? result.config : null,
  };
}

/** The editor for one setting: its title and form (`null` for a setting that cannot be changed here). */
export function heatEditor(input: HeatEditorInput, key: HeatSettingKey | "away", handlers: EditorHandlers): { title: string; body: HTMLElement } | null {
  const { doc, language, answer } = input;
  const config = answer.config;
  const choices = answer.choices;
  const prefix = `${input.idPrefix}-${key.replace(/[^a-z0-9]/gi, "-")}`;
  const t = (k: TranslationKey, params?: Record<string, string>): string => translate(language, k, params);
  const option = (choice: HeatChoice): ChoiceOption => ({ value: choice.entity_id, label: choice.name, ...(choice.value === null ? {} : { help: choice.value }) });
  const none: ChoiceOption = { value: "", label: t("heat.settings.none") };
  const pick = (title: string, options: ChoiceOption[], selected: string | null, save: (value: string | null) => Promise<string | null>, intro?: string) => ({
    title,
    body: singleEditor(doc, language, { ...(intro === undefined ? {} : { intro }), options, selected: selected ?? "", idPrefix: prefix }, (value) => save(value === "" ? null : value), handlers),
  });
  if (key === "away") {
    return { title: t("heat.away.title"), body: awayEditor(input, handlers) };
  }
  if (key === "awayDelta") {
    return { title: t("heat.settings.away"), body: awayDeltaEditor(input, handlers) };
  }
  if (key === "night") {
    return { title: t("heat.settings.night"), body: nightEditor(input, handlers) };
  }
  if (key === "steer") {
    // What turning it on means on this path, that a new path is untried, and the entities it turns on with it.
    const found = config.control_enabled ? null : (answer.control?.enable_entities ?? null);
    const parts = [t(isRelayPath(config.control_path) ? "heat.settings.steerHelp" : "heat.settings.steerHelpPath")];
    if (answer.control?.untested === true) {
      parts.push(t("heat.settings.untested"));
    }
    if (found !== null) {
      const names = new Intl.ListFormat(language, { type: "conjunction" }).format(found.entities.map((entity) => entity.name));
      parts.push(t("heat.settings.enableEntities", { integration: found.integration, names }));
    }
    return {
      title: t("heat.settings.steer"),
      body: onOffEditor(doc, language, { intro: parts.join(" "), on: config.control_enabled, idPrefix: prefix }, async (on) =>
        on === config.control_enabled ? null : await input.write({ control_enabled: on }, { enable: on && found !== null }),
      handlers),
    };
  }
  if (choices === null) {
    return null;
  }
  switch (key) {
    case "indoor":
      return {
        title: t("heat.settings.indoor"),
        body: multiEditor(doc, language, { intro: t("heat.settings.indoorHelp"), options: choices.indoor.map(option), checked: config.indoor_sensors }, (values) =>
          input.write({ indoor_sensors: values }),
        handlers),
      };
    case "outdoor":
      return pick(
        t("heat.settings.outdoor"),
        [{ value: "", label: t("heat.settings.outdoorAuto"), help: t("heat.settings.outdoorHelp") }, ...choices.outdoor.map(option)],
        config.outdoor_sensor,
        (value) => input.write({ outdoor_sensor: value }),
      );
    case "meter":
      return choices.meter === null
        ? null
        : pick(t("heat.settings.meter"), [none, ...choices.meter.map(option)], config.heat_meter_entity, (value) => input.write({ heat_meter_entity: value }), t("heat.settings.meterHelp"));
    case "path": {
      // A path the pump's own entities offer says so, and choosing it fills its entities in. SG Ready through the
      // integration is offered only where the pump has it (or it is chosen already).
      const found = answer.pump?.paths ?? [];
      const offered = ["none", "relay", "sg_ready", "sg_ready_api", "offset", "setpoint"].filter(
        (path) => path !== "sg_ready_api" || config.control_path === path || found.some((item) => item.path === path),
      );
      return pick(
        t("heat.settings.path"),
        offered.map((path) => ({
          value: path,
          label: t(`heat.path.${path}` as TranslationKey),
          ...(found.some((item) => item.path === path) ? { help: t("heat.settings.pathFound") } : {}),
        })),
        config.control_path,
        (value) => {
          const path = value ?? "none";
          const hit = path === config.control_path ? undefined : found.find((item) => item.path === path);
          return input.write(
            hit === undefined ? { control_path: path } : { control_path: path, control_entity: hit.control_entity, control_entity_b: hit.control_entity_b },
          );
        },
        t("heat.settings.pathHelp"),
      );
    }
    case "control":
    case "controlB": {
      const path = config.control_path;
      const api = path === "sg_ready_api";
      // The relay's list holds the pump's SG Ready entities too: the API's mode is a number, a contact is a switch.
      const group =
        path === "offset"
          ? choices.offset
          : path === "setpoint"
            ? choices.setpoint
            : choices.relay.filter((choice) => NUMBERS.test(choice.entity_id) === (api && key === "control"));
      const field = key === "control" ? "control_entity" : "control_entity_b";
      // Each entity's own job in plain words: on SG Ready A blocks or lowers and B raises; the API's mode and its
      // switch; the offset SpotNav moves and puts back.
      const helpKey: TranslationKey | undefined =
        path === "sg_ready"
          ? key === "control" ? "heat.settings.controlSgReadyHelp" : "heat.settings.controlBHelp"
          : api
            ? key === "control" ? "heat.settings.controlApiHelp" : "heat.settings.controlApiSwitchHelp"
            : path === "offset"
              ? "heat.settings.controlOffsetHelp"
              : undefined;
      const title = key === "control" ? "heat.settings.control" : api ? "heat.settings.controlApiSwitch" : "heat.settings.controlB";
      return pick(t(title), [none, ...group.map(option)], config[field], (value) =>
        input.write({ control_path: config.control_path, [field]: value }),
      helpKey === undefined ? undefined : t(helpKey));
    }
    case "hotWater":
      return pick(t("heat.settings.hotWater"), [none, ...choices.hot_water.map(option)], config.hot_water_entity, (value) => input.write({ hot_water_entity: value }), t("heat.settings.hotWaterHelp"));
    case "zones": {
      const kindWord = (kind: string | undefined): string => t(kind === "air" ? "heat.zone.air" : "heat.zone.floor");
      return {
        title: t("heat.settings.zones"),
        body: multiEditor(
          doc,
          language,
          { intro: t("heat.settings.zonesHelp"), options: choices.zones.map((choice) => ({ value: choice.entity_id, label: `${choice.name} · ${kindWord(choice.kind)}` })), checked: config.zones.map((zone) => zone.entity_id) },
          (values) => input.write({ zones: values.map((entity_id) => ({ entity_id, offset_c: config.zones.find((zone) => zone.entity_id === entity_id)?.offset_c ?? 0 })) }),
          handlers,
        ),
      };
    }
    default: {
      if (key.startsWith("sensor:")) {
        return sensorOffsetEditor(input, key.slice("sensor:".length), prefix, handlers);
      }
      const entityId = key.slice("zone:".length);
      const zone = config.zones.find((item) => item.entity_id === entityId);
      if (zone === undefined) {
        return null;
      }
      const air = answer.zones.find((item) => item.entity_id === entityId)?.kind === "air";
      const help = air ? `${t("heat.settings.offsetHelp")} ${t("heat.settings.airHelp")}` : t("heat.settings.offsetHelp");
      return {
        title: namesOf(answer).get(entityId) ?? entityId,
        body: numberEditor(
          doc,
          language,
          { help, unit: "°C", current: zone.offset_c, min: -3, max: 3, decimals: 1, rangeMessage: t("heat.error.range"), idPrefix: prefix },
          async (value) => {
            const offset = value ?? 0;
            if (Math.abs(offset * 2 - Math.round(offset * 2)) > 1e-9) {
              return t("heat.error.step");
            }
            return await input.write({ zones: config.zones.map((item) => (item.entity_id === entityId ? { ...item, offset_c: offset } : item)) });
          },
          handlers,
        ),
      };
    }
  }
}

/** One indoor sensor's offset against the house target, −3 to +3 °C in half degrees (as a zone's). */
function sensorOffsetEditor(input: HeatEditorInput, entityId: string, prefix: string, handlers: EditorHandlers): { title: string; body: HTMLElement } | null {
  const { doc, language, answer } = input;
  const config = answer.config;
  if (!config.indoor_sensors.includes(entityId)) {
    return null;
  }
  const current = config.indoor_offsets.find((item) => item.entity_id === entityId)?.offset_c ?? 0;
  return {
    title: roomName(namesOf(answer).get(entityId) ?? entityId),
    body: numberEditor(
      doc,
      language,
      { help: translate(language, "heat.settings.sensorOffsetHelp"), unit: "°C", current, min: -3, max: 3, decimals: 1, rangeMessage: translate(language, "heat.error.range"), idPrefix: prefix },
      async (value) => {
        const offset = value ?? 0;
        if (Math.abs(offset * 2 - Math.round(offset * 2)) > 1e-9) {
          return translate(language, "heat.error.step");
        }
        const others = config.indoor_offsets.filter((item) => item.entity_id !== entityId);
        const next = config.indoor_sensors
          .map((sensor) => (sensor === entityId ? { entity_id: sensor, offset_c: offset } : others.find((item) => item.entity_id === sensor)))
          .filter((item): item is { entity_id: string; offset_c: number } => item !== undefined && item.offset_c !== 0);
        return await input.write({ indoor_offsets: next });
      },
      handlers,
    ),
  };
}

function field(doc: Document, label: string, control: HTMLInputElement): HTMLElement {
  const wrap = doc.createElement("label");
  wrap.className = X.editorField;
  wrap.append(doc.createTextNode(label), control);
  return wrap;
}

function awayEditor(input: HeatEditorInput, handlers: EditorHandlers): HTMLFormElement {
  const { doc, language, answer } = input;
  const editor = shell(doc, language, "away", handlers);
  const help = doc.createElement("p");
  help.className = VISUAL_CLASSES.entityHelp;
  help.textContent = translate(language, "heat.away.help");
  const now = Date.parse(answer.now);
  const start = answer.config.away_until !== null
    ? instantToLocal(Date.parse(answer.config.away_until), answer.time_zone)
    : { date: instantToLocal(now + 86_400_000, answer.time_zone).date, time: DEFAULT_RETURN };
  const date = doc.createElement("input");
  date.type = "date";
  date.value = start.date;
  date.dataset["field"] = "date";
  const time = doc.createElement("input");
  time.type = "time";
  time.value = start.time;
  time.dataset["field"] = "time";
  editor.body.append(help, field(doc, translate(language, "heat.away.date"), date), field(doc, translate(language, "heat.away.time"), time));
  editor.submit(async () => {
    const instant = localToInstant(date.value, time.value, answer.time_zone);
    if (instant === null) {
      return translate(language, "heat.error.generic");
    }
    if (instant <= Date.now()) {
      return translate(language, "heat.error.past");
    }
    return await input.write({ mode: "away", away_until: new Date(instant).toISOString() });
  });
  return editor.form;
}

function nightEditor(input: HeatEditorInput, handlers: EditorHandlers): HTMLFormElement {
  const { doc, language, answer } = input;
  const night = answer.config.night;
  const editor = shell(doc, language, "night", handlers);
  const help = doc.createElement("p");
  help.className = VISUAL_CLASSES.entityHelp;
  help.textContent = translate(language, "heat.settings.nightHelp");
  const on = doc.createElement("input");
  on.type = "checkbox";
  on.checked = night.on;
  on.dataset["field"] = "on";
  const onLabel = doc.createElement("label");
  onLabel.className = VISUAL_CLASSES.siteChoice;
  onLabel.append(on, doc.createTextNode(translate(language, "settings.value.on")));
  const delta = doc.createElement("input");
  delta.type = "number";
  delta.min = "0.5";
  delta.max = "3";
  delta.step = "0.5";
  delta.value = String(night.delta_c);
  delta.dataset["field"] = "delta";
  const startInput = doc.createElement("input");
  startInput.type = "time";
  startInput.value = night.start;
  startInput.dataset["field"] = "start";
  const endInput = doc.createElement("input");
  endInput.type = "time";
  endInput.value = night.end;
  endInput.dataset["field"] = "end";
  editor.body.append(
    help,
    onLabel,
    field(doc, translate(language, "heat.settings.nightDelta"), delta),
    field(doc, translate(language, "heat.settings.nightStart"), startInput),
    field(doc, translate(language, "heat.settings.nightEnd"), endInput),
  );
  editor.submit(async () => {
    const value = Number(delta.value.replace(",", "."));
    if (!Number.isFinite(value) || value < 0.5 || value > 3) {
      return translate(language, "heat.error.range");
    }
    if (Math.abs(value * 2 - Math.round(value * 2)) > 1e-9) {
      return translate(language, "heat.error.step");
    }
    return await input.write({ night: { on: on.checked, delta_c: value, start: startInput.value, end: endInput.value } });
  });
  return editor.form;
}

function awayDeltaEditor(input: HeatEditorInput, handlers: EditorHandlers): HTMLFormElement {
  const { doc, language, answer } = input;
  const editor = shell(doc, language, "awayDelta", handlers);
  const help = doc.createElement("p");
  help.className = VISUAL_CLASSES.entityHelp;
  help.textContent = translate(language, "heat.settings.awayHelp");
  const delta = doc.createElement("input");
  delta.type = "number";
  delta.min = "1";
  delta.max = "6";
  delta.step = "0.5";
  delta.value = String(answer.config.away_delta_c);
  delta.dataset["field"] = "delta";
  editor.body.append(help, field(doc, translate(language, "heat.settings.awayDelta"), delta));
  editor.submit(async () => {
    const value = Number(delta.value.replace(",", "."));
    if (!Number.isFinite(value) || value < 1 || value > 6) {
      return translate(language, "heat.error.range");
    }
    if (Math.abs(value * 2 - Math.round(value * 2)) > 1e-9) {
      return translate(language, "heat.error.step");
    }
    return await input.write({ away_delta_c: value });
  });
  return editor.form;
}
