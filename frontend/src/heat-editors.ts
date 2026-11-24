// The heat settings' editors, one setting per dialog, and the write they share: used by the shared settings popover
// (`settings-popover.ts`) from every card, and by the heat card's away button. Shadow only: what is written is
// SpotNav's own heat settings for the site (`spotnav/update_heat`), never a heating device.

import { updateHeat, type SiteTarget } from "./api";
import { decodeHeatWrite, isRelayPath, roomName, type HeatAnswer, type HeatChoice, type HeatConfig } from "./heat-model";
import { HEAT_CLASSES as X } from "./heat-styles";
import { instantToLocal, localToInstant, namesOf, offsetValue, type HeatSettingKey } from "./heat-view";
import { onceFields, weekdayShort, type DeadlineKind } from "./spa-view";
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
  /** The answer to the stored offset's line (`HeatControl.offset_home`). */
  offsetHome?: "restore" | "keep";
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
    raw = await updateHeat(hass, target, changes, expected, options.enable === true, options.offsetHome ?? null);
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
  if (key === "waterHeaterControl") {
    // The opt-in's confirmation says the one thing SpotNav cannot see: the thermostat must be at 60 °C or more.
    const heater = config.water_heater;
    if (heater === null) {
      return null;
    }
    return {
      title: t("heat.settings.waterHeaterControl"),
      body: onOffEditor(doc, language, { intro: t("heat.settings.waterHeaterControlHelp"), on: heater.control, idPrefix: prefix }, async (on) =>
        on === heater.control ? null : await input.write({ water_heater: { entity_id: heater.entity_id, control: on } }),
      handlers),
    };
  }
  if (key.startsWith("zone:")) {
    // A zone's own opt-in: SpotNav sets the thermostat from the person's own setting.
    const entityId = key.slice("zone:".length);
    const zone = config.zones.find((item) => item.entity_id === entityId);
    if (zone === undefined) {
      return null;
    }
    const air = answer.zones.find((item) => item.entity_id === entityId)?.kind === "air";
    const intro = air ? `${t("heat.settings.zoneControlHelp")} ${t("heat.settings.airHelp")}` : t("heat.settings.zoneControlHelp");
    return {
      title: `${namesOf(answer).get(entityId) ?? entityId}: ${t("heat.settings.zoneControl")}`,
      body: onOffEditor(doc, language, { intro, on: zone.control, idPrefix: prefix }, async (on) =>
        on === zone.control
          ? null
          : await input.write({ zones: config.zones.map((item) => (item.entity_id === entityId ? { ...item, control: on } : item)) }),
      handlers),
    };
  }
  if (key === "offsetHome") {
    // The stored offset reads other than the household's own value: put it back, or keep it. Nothing is written
    // without this answer.
    const line = answer.control?.offset_home ?? null;
    if (line === null) {
      return null;
    }
    const words = { value: offsetValue(language, line.value), home: offsetValue(language, line.home) };
    return {
      title: t("heat.settings.offsetHome"),
      body: singleEditor(
        doc,
        language,
        {
          intro: t("heat.settings.offsetHomeHelp", words),
          options: [
            { value: "restore", label: t("heat.settings.offsetHomeRestore", words) },
            { value: "keep", label: t("heat.settings.offsetHomeKeep", words) },
          ],
          selected: null,
          idPrefix: prefix,
        },
        (value) => input.write({}, { offsetHome: value === "keep" ? "keep" : "restore" }),
        handlers,
      ),
    };
  }
  if (choices === null) {
    return null;
  }
  const loads = loadEditor(input, key, prefix, handlers, pick);
  if (loads !== undefined) {
    return loads;
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
    case "waterHeater":
      return pick(
        t("heat.settings.waterHeater"),
        // One switch is never both: the hot-water boost's pulse would switch a water heater off.
        [none, ...choices.water_heater.filter((choice) => choice.entity_id !== config.hot_water_entity).map(option)],
        config.water_heater?.entity_id ?? null,
        (value) => input.write({ water_heater: value === null ? null : { entity_id: value } }),
        t("heat.settings.waterHeaterHelp"),
      );
    case "tankSensor": {
      const heater = config.water_heater;
      if (heater === null) {
        return null;
      }
      return pick(
        t("heat.settings.tankSensor"),
        [none, ...choices.tank_sensor.map(option)],
        heater.tank_sensor,
        (value) => input.write({ water_heater: { entity_id: heater.entity_id, tank_sensor: value } }),
        t("heat.settings.tankSensorHelp"),
      );
    }
    case "hotWater":
      return pick(
        t("heat.settings.hotWater"),
        [none, ...choices.hot_water.filter((choice) => choice.entity_id !== config.water_heater?.entity_id).map(option)],
        config.hot_water_entity,
        (value) => input.write({ hot_water_entity: value }),
        t("heat.settings.hotWaterHelp"),
      );
    case "zones": {
      const kindWord = (kind: string | undefined): string => t(kind === "air" ? "heat.zone.air" : "heat.zone.floor");
      return {
        title: t("heat.settings.zones"),
        body: multiEditor(
          doc,
          language,
          { intro: t("heat.settings.zonesHelp"), options: choices.zones.map((choice) => ({ value: choice.entity_id, label: `${choice.name} · ${kindWord(choice.kind)}` })), checked: config.zones.map((zone) => zone.entity_id) },
          (values) =>
            input.write({
              zones: values.map((entity_id) => {
                const kept = config.zones.find((zone) => zone.entity_id === entity_id);
                return { entity_id, offset_c: kept?.offset_c ?? 0, control: kept?.control ?? false };
              }),
            }),
          handlers,
        ),
      };
    }
    default: {
      if (key.startsWith("sensor:")) {
        return sensorOffsetEditor(input, key.slice("sensor:".length), prefix, handlers);
      }
      return null;
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

type Pick = (
  title: string,
  options: ChoiceOption[],
  selected: string | null,
  save: (value: string | null) => Promise<string | null>,
  intro?: string,
) => { title: string; body: HTMLElement };

/** A kW figure's editor ("Inte angiven" as its none). */
function kwEditor(
  input: HeatEditorInput,
  prefix: string,
  handlers: EditorHandlers,
  title: string,
  help: string,
  current: number | null,
  range: [number, number],
  decimals: number,
  save: (value: number | null) => Promise<string | null>,
): { title: string; body: HTMLElement } {
  const { doc, language } = input;
  return {
    title,
    body: numberEditor(
      doc,
      language,
      {
        help,
        unit: "kW",
        current,
        min: range[0],
        max: range[1],
        decimals,
        noneLabel: translate(language, "heat.settings.kwNone"),
        rangeMessage: translate(language, "heat.error.range"),
        idPrefix: prefix,
      },
      save,
      handlers,
    ),
  };
}

/** A figure in half degrees or half hours: the step's sentence when it is not one. */
function halfStep(language: Language, value: number | null): string | null {
  return value !== null && Math.abs(value * 2 - Math.round(value * 2)) > 1e-9 ? translate(language, "heat.error.step") : null;
}

/**
 * The pool's, the spa's, the sauna's and the heat card's phones' editors (`undefined` for another key). The detected
 * pool is confirmed once as a group; a new heat pump, circulation, spa or sauna starts watched only (`pool/config.py`,
 * `spa/config.py`, `sauna/config.py`).
 */
function loadEditor(input: HeatEditorInput, key: HeatSettingKey | "away", prefix: string, handlers: EditorHandlers, pick: Pick): { title: string; body: HTMLElement } | null | undefined {
  const { doc, language, answer } = input;
  const config = answer.config;
  const choices = answer.choices!;
  const t = (k: TranslationKey, params?: Record<string, string>): string => translate(language, k, params);
  const names = namesOf(answer);
  const nameOf = (id: string | null): string => (id === null ? t("heat.settings.none") : names.get(id) ?? id);
  const none: ChoiceOption = { value: "", label: t("heat.settings.none") };
  const ranked = (choice: HeatChoice & { ask?: boolean; cloud?: boolean }): ChoiceOption => {
    const help = [choice.value, choice.ask === true ? t("heat.settings.unsure") : null, choice.cloud === true ? t("heat.settings.cloud") : null].filter((part): part is string => part !== null && part !== "");
    return { value: choice.entity_id, label: choice.name, ...(help.length === 0 ? {} : { help: help.join(" · ") }) };
  };
  const pool = config.pool;
  const poolChoices = choices.pool;
  switch (key) {
    case "poolSuggested": {
      const found = poolChoices?.suggested ?? null;
      if (found === null) {
        return null;
      }
      const parts = [t("heat.settings.poolFoundPump", { name: nameOf(found.heat_pump.entity_id) })];
      if (found.circulation_id !== null) {
        parts.push(t("heat.settings.poolFoundCirculation", { name: nameOf(found.circulation_id) }));
      }
      if (found.water_sensor !== null) {
        parts.push(t("heat.settings.poolFoundSensor", { name: nameOf(found.water_sensor) }));
      }
      return {
        title: t("heat.settings.pool"),
        body: singleEditor(
          doc,
          language,
          {
            intro: `${parts.join(" ")} ${t("heat.settings.poolConfirm")}`,
            options: [
              { value: "yes", label: t("heat.settings.poolYes") },
              { value: "no", label: t("heat.settings.poolNo") },
            ],
            selected: null,
            idPrefix: prefix,
          },
          async (value) =>
            value === "yes"
              ? await input.write({ pool: { heat_pump: found.heat_pump, circulation_id: found.circulation_id, water_sensor: found.water_sensor } })
              : null,
          handlers,
        ),
      };
    }
    case "poolHeatPump": {
      const options = poolChoices?.heat_pump ?? [];
      // A pool not set up yet starts from the detected heat pump.
      const selected = pool?.heat_pump?.entity_id ?? poolChoices?.suggested?.heat_pump.entity_id ?? null;
      return pick(t("heat.settings.poolHeatPump"), [none, ...options.map(ranked)], selected, async (value) => {
        if (value === null) {
          return pool === null ? null : await input.write({ pool: { heat_pump: null } });
        }
        const chosen = options.find((choice) => choice.entity_id === value);
        if (chosen === undefined) {
          return null;
        }
        const heatPump = { entity_id: chosen.entity_id, shape: chosen.shape, switch_id: chosen.switch_id, number_id: chosen.number_id };
        const found = poolChoices?.suggested ?? null;
        // The first choice of a pool brings the circulation and the sensor detected with it.
        const group = pool === null && found !== null && found.heat_pump.entity_id === value ? { circulation_id: found.circulation_id, water_sensor: found.water_sensor } : {};
        return await input.write({ pool: { heat_pump: heatPump, ...group } });
      }, t("heat.settings.poolHeatPumpHelp"));
    }
    case "poolCirculation":
      return pool === null
        ? null
        : pick(t("heat.settings.poolCirculation"), [none, ...(poolChoices?.circulation ?? []).map(ranked)], pool.circulation_id, (value) =>
            input.write({ pool: { circulation_id: value } }),
          t("heat.settings.poolCirculationHelp"));
    case "poolSensor":
      return pool === null
        ? null
        : pick(
            t("heat.settings.poolSensor"),
            [{ value: "", label: t("heat.settings.poolSensorPump"), help: t("heat.settings.poolSensorPumpHelp") }, ...(poolChoices?.water_sensor ?? []).map(ranked)],
            pool.water_sensor,
            (value) => input.write({ pool: { water_sensor: value } }),
            t("heat.settings.poolSensorHelp"),
          );
    case "poolFilterHours":
      return pool === null
        ? null
        : {
            title: t("heat.settings.poolFilterHours"),
            body: numberEditor(
              doc,
              language,
              {
                help: t("heat.settings.poolFilterHelp"),
                unit: "h",
                current: pool.filter_hours,
                min: 1,
                max: 24,
                decimals: 1,
                noneLabel: t("heat.settings.poolFilterAuto"),
                rangeMessage: t("heat.error.range"),
                idPrefix: prefix,
              },
              async (value) => halfStep(language, value) ?? (await input.write({ pool: { filter_hours: value } })),
              handlers,
            ),
          };
    case "poolHeatPumpKw":
      return pool === null
        ? null
        : kwEditor(input, prefix, handlers, t("heat.settings.poolHeatPumpKw"), t("heat.settings.kwHelp"), pool.heat_pump_kw, [0.3, 15], 1, (value) =>
            input.write({ pool: { heat_pump_kw: value } }),
          );
    case "poolCirculationKw":
      return pool === null
        ? null
        : kwEditor(input, prefix, handlers, t("heat.settings.poolCirculationKw"), t("heat.settings.kwHelp"), pool.circulation_kw, [0.05, 3], 2, (value) =>
            input.write({ pool: { circulation_kw: value } }),
          );
    case "spa":
      return pick(
        t("heat.settings.spa"),
        [none, ...choices.spa.map((choice) => {
          const option = ranked(choice);
          return choice.controllable ? option : { ...option, help: [option.help, t("heat.settings.watchOnly")].filter(Boolean).join(" · ") };
        })],
        config.spa?.entity_id ?? null,
        async (value) => {
          if (value === null) {
            return config.spa === null ? null : await input.write({ spa: null });
          }
          const chosen = choices.spa.find((choice) => choice.entity_id === value);
          return chosen === undefined ? null : await input.write({ spa: { entity_id: chosen.entity_id, family: chosen.family } });
        },
        t("heat.settings.spaHelp"),
      );
    case "spaFloor": {
      const spa = config.spa;
      return spa === null
        ? null
        : {
            title: t("heat.settings.spaFloor"),
            body: numberEditor(
              doc,
              language,
              { help: t("heat.settings.spaFloorHelp"), unit: "°C", current: spa.floor_c, min: 5, max: 30, decimals: 1, rangeMessage: t("heat.error.range"), idPrefix: prefix },
              async (value) => (value === null ? t("heat.error.range") : halfStep(language, value) ?? (await input.write({ spa: { floor_c: value } }))),
              handlers,
            ),
          };
    }
    case "spaKw":
      return config.spa === null
        ? null
        : kwEditor(input, prefix, handlers, t("heat.settings.spaKw"), t("heat.settings.kwHelp"), config.spa.heater_kw, [0.5, 12], 1, (value) =>
            input.write({ spa: { heater_kw: value } }),
          );
    case "sauna":
      return pick(
        t("heat.settings.sauna"),
        [none, ...choices.sauna.map(ranked)],
        config.sauna?.entity_id ?? null,
        async (value) => {
          if (value === null) {
            return config.sauna === null ? null : await input.write({ sauna: null });
          }
          const chosen = choices.sauna.find((choice) => choice.entity_id === value);
          return chosen === undefined ? null : await input.write({ sauna: { entity_id: chosen.entity_id, family: chosen.family, lock_id: chosen.lock_id } });
        },
        t("heat.settings.saunaHelp"),
      );
    case "saunaKw":
      return config.sauna === null
        ? null
        : kwEditor(input, prefix, handlers, t("heat.settings.saunaKw"), t("heat.settings.kwHelp"), config.sauna.heater_kw, [2, 15], 1, (value) =>
            input.write({ sauna: { heater_kw: value } }),
          );
    case "saunaSafety": {
      const sauna = config.sauna;
      if (sauna === null || sauna.family !== "relay") {
        return null;
      }
      return {
        title: t("heat.settings.saunaSafety"),
        body: singleEditor(
          doc,
          language,
          {
            intro: t("heat.settings.saunaSafetyHelp"),
            options: [
              { value: "yes", label: t("heat.settings.saunaSafetyYes") },
              { value: "no", label: t("heat.settings.saunaSafetyNo") },
            ],
            selected: sauna.safety_declared ? "yes" : "no",
            idPrefix: prefix,
          },
          (value) => input.write({ sauna: { safety_declared: value === "yes" } }),
          handlers,
        ),
      };
    }
    case "poolHeatPumpControl":
    case "poolCirculationControl": {
      if (pool === null) {
        return null;
      }
      const field = key === "poolHeatPumpControl" ? "control_heat_pump" : "control_circulation";
      const on = pool[field];
      return {
        title: t(`heat.settings.${key}` as TranslationKey),
        body: onOffEditor(
          doc,
          language,
          { intro: `${t(`heat.settings.${key}Help` as TranslationKey)} ${t("heat.settings.controlUntested")}`, on, idPrefix: prefix },
          async (value) => (value === on ? null : await input.write({ pool: { [field]: value } })),
          handlers,
        ),
      };
    }
    case "spaControl": {
      const spa = config.spa;
      if (spa === null) {
        return null;
      }
      return {
        title: t("heat.settings.spaControl"),
        body: onOffEditor(
          doc,
          language,
          { intro: `${t("heat.settings.spaControlHelp")} ${t("heat.settings.controlUntested")}`, on: spa.control, idPrefix: prefix },
          async (value) => (value === spa.control ? null : await input.write({ spa: { control: value } })),
          handlers,
        ),
      };
    }
    case "saunaControl": {
      const sauna = config.sauna;
      if (sauna === null) {
        return null;
      }
      return {
        title: t("heat.settings.saunaControl"),
        body: onOffEditor(
          doc,
          language,
          { intro: `${t("heat.settings.saunaControlHelp")} ${t("heat.settings.controlUntested")}`, on: sauna.control, idPrefix: prefix },
          async (value) => (value === sauna.control ? null : await input.write({ sauna: { control: value } })),
          handlers,
        ),
      };
    }
    default:
      return undefined;
  }
}

/** The most weekly times a spa or sauna takes (`spa/config.py`). */
export const MAX_SESSIONS = 4;

/** What the times editor saves: the weekly times and the one-off (an ISO instant, or `null`). */
export interface SessionsChoice {
  weekly: { weekdays: number[]; time: string }[];
  once: string | null;
}

/**
 * The spa's bath times or the sauna's sessions, like a car's departures: up to four weekly times, each on its weekdays
 * (Monday first), and one one-off date and time. Written by the household's own command (`spotnav/spa`,
 * `spotnav/sauna`), not the administrator's settings.
 */
export function sessionsEditor(
  doc: Document,
  language: Language,
  answer: HeatAnswer,
  kind: DeadlineKind,
  current: SessionsChoice,
  save: (choice: SessionsChoice) => Promise<string | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const t = (k: TranslationKey, params?: Record<string, string>): string => translate(language, k, params);
  const editor = shell(doc, language, `${kind}-times`, handlers);
  const help = doc.createElement("p");
  help.className = VISUAL_CLASSES.entityHelp;
  help.textContent = t(`heat.${kind}.timesHelp` as TranslationKey);
  const list = doc.createElement("div");
  list.dataset["part"] = "weekly";
  type Row = { time: HTMLInputElement; days: HTMLInputElement[]; node: HTMLElement };
  const rows: Row[] = [];
  const add = doc.createElement("button");
  add.type = "button";
  add.className = VISUAL_CLASSES.button;
  add.dataset["action"] = "add-time";
  add.textContent = t(`heat.${kind}.addTime` as TranslationKey);
  const paintAdd = (): void => {
    add.disabled = rows.length >= MAX_SESSIONS;
  };
  const addRow = (item: { weekdays: number[]; time: string }): void => {
    const node = doc.createElement("fieldset");
    node.className = VISUAL_CLASSES.siteFieldset;
    node.dataset["part"] = "weekly-time";
    const legend = doc.createElement("legend");
    legend.className = VISUAL_CLASSES.siteLegend;
    legend.textContent = t(`heat.${kind}.weeklyTime` as TranslationKey);
    const time = doc.createElement("input");
    time.type = "time";
    time.value = item.time;
    time.dataset["field"] = "time";
    time.setAttribute("aria-label", t("heat.spa.time"));
    const dayRow = doc.createElement("div");
    dayRow.style.cssText = "display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:4px";
    const days: HTMLInputElement[] = [];
    for (let day = 1; day <= 7; day += 1) {
      const check = doc.createElement("input");
      check.type = "checkbox";
      check.checked = item.weekdays.includes(day);
      check.dataset["weekday"] = String(day);
      const label = doc.createElement("label");
      label.className = VISUAL_CLASSES.siteChoice;
      label.append(check, doc.createTextNode(weekdayShort(language, day)));
      dayRow.append(label);
      days.push(check);
    }
    const remove = doc.createElement("button");
    remove.type = "button";
    remove.className = VISUAL_CLASSES.button;
    remove.dataset["action"] = "remove-time";
    remove.textContent = t("heat.spa.removeTime");
    const row: Row = { time, days, node };
    remove.addEventListener("click", () => {
      rows.splice(rows.indexOf(row), 1);
      node.remove();
      paintAdd();
    });
    node.append(legend, field(doc, t("heat.spa.time"), time), dayRow, remove);
    rows.push(row);
    list.append(node);
    paintAdd();
  };
  for (const item of current.weekly) {
    addRow(item);
  }
  add.addEventListener("click", () => addRow({ weekdays: [1, 2, 3, 4, 5, 6, 7], time: kind === "spa" ? "19:00" : "18:00" }));
  paintAdd();
  // The one-off: a date and a time, or none.
  const fields = onceFields(answer, current.once);
  const onceBox = doc.createElement("input");
  onceBox.type = "checkbox";
  onceBox.checked = fields !== null;
  onceBox.dataset["field"] = "once";
  const onceLabel = doc.createElement("label");
  onceLabel.className = VISUAL_CLASSES.siteChoice;
  onceLabel.append(onceBox, doc.createTextNode(t(`heat.${kind}.onceLabel` as TranslationKey)));
  const date = doc.createElement("input");
  date.type = "date";
  date.dataset["field"] = "date";
  const time = doc.createElement("input");
  time.type = "time";
  time.dataset["field"] = "once-time";
  const tomorrow = instantToLocal(Date.parse(answer.now) + 86_400_000, answer.time_zone).date;
  date.value = fields?.date ?? tomorrow;
  time.value = fields?.time ?? (kind === "spa" ? "19:00" : "18:00");
  const onceFieldsBlock = doc.createElement("div");
  onceFieldsBlock.append(field(doc, t("heat.away.date"), date), field(doc, t("heat.spa.time"), time));
  const paintOnce = (): void => {
    onceFieldsBlock.hidden = !onceBox.checked;
  };
  onceBox.addEventListener("change", paintOnce);
  paintOnce();
  editor.body.append(help, list, add, onceLabel, onceFieldsBlock);
  editor.submit(async () => {
    const weekly: { weekdays: number[]; time: string }[] = [];
    for (const row of rows) {
      const weekdays = row.days.filter((check) => check.checked).map((check) => Number(check.dataset["weekday"]));
      if (!/^\d{2}:\d{2}$/.test(row.time.value) || weekdays.length === 0) {
        return t("heat.spa.timesInvalid");
      }
      weekly.push({ weekdays, time: row.time.value });
    }
    let once: string | null = null;
    if (onceBox.checked) {
      const instant = localToInstant(date.value, time.value, answer.time_zone);
      if (instant === null) {
        return t("heat.spa.timesInvalid");
      }
      if (instant <= Date.now()) {
        return t("heat.error.past");
      }
      once = new Date(instant).toISOString();
    }
    return await save({ weekly, once });
  });
  return editor.form;
}

/** The spa's idle temperature between baths (10 to 40 °C in half degrees; never below its lowest, `floor_c`). */
export function idleEditor(
  doc: Document,
  language: Language,
  idPrefix: string,
  current: number,
  save: (value: number) => Promise<string | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  return numberEditor(
    doc,
    language,
    {
      help: translate(language, "heat.spa.idleHelp"),
      unit: "°C",
      current,
      min: 10,
      max: 40,
      decimals: 1,
      rangeMessage: translate(language, "heat.error.range"),
      idPrefix: `${idPrefix}-spa-idle`,
    },
    async (value) => (value === null ? translate(language, "heat.error.range") : halfStep(language, value) ?? (await save(value))),
    handlers,
  );
}
