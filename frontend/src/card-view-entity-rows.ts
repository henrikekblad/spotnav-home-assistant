// The entity rows of the Settings page: the charger's, the cars' and the site's setup rows, read from the entity
// config the card holds and painted into the slots the page left for them.

import {
  chargePeriodsLabel,
  chargePeriodsOptions,
  chargePeriodsReplacement,
  chargePeriodsValue,
} from "./charge-periods";
import { identificationSummary, sourceChoice, sourceOption, sourceReadingText } from "./identification";
import { clock, formatNumber, kwText, hasZone, percentAmount, weekdayDate } from "./format";
import { translate, type TranslationKey } from "./i18n";
import { type SiteFacts } from "./model";
import { conflictText, entityNameIn, modeLabel, siteWarningRows } from "./entity-editor";
import {
  fieldsOf,
  isMissingEntity,
  storedMode,
  vehicleChoice,
  type EntityConfig,
  type EntityScope,
} from "./entity-config";
import { GUEST_KWH_MAX, GUEST_KWH_MIN, GUEST_VEHICLE_ID, type ReferencePicture, type Vehicle } from "./validate";
import { hourLine } from "./demand-charge";
import { suggestionAdvice } from "./target-suggestion";
import { floorEditor, limitEditor, targetEditor } from "./percent-editors";
import { defaultTargetPercent } from "./percent-slider";
import { vehicleSummary, type VehicleEdits, type VehicleReference } from "./vehicle-settings";
import {
  sectionHeading,
  settingRow,
  type ChoiceOption,
  type EditorHandlers,
  type NumberEditorInput,
} from "./value-editors";
import type { ValueWrite } from "./value-writes";
import type { IdentifyMode, SettingsRecord } from "./types";
import {
  entityLabels,
  frameEditor,
  isWholePicture,
  referenceEditor,
  referenceText,
  type CameraPicture,
  type PictureKind,
} from "./camera-editor";
import {
  CAPACITY_MAX_KWH,
  CAPACITY_MIN_KWH,
  CONSUMPTION_MAX_KWH_PER_10KM,
  CONSUMPTION_MIN_KWH_PER_10KM,
  cameraReplacement,
  identificationReplacement,
  usersChangePlanReplacement,
} from "./settings";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";
import { priorityEditor, prioritySummary } from "./priority-list";
import type { SitePriority } from "./site-model";
import type { EntityViewState } from "./card-view";
import type { PageHelpers, SettingsPageState, ViewContext } from "./card-view-context";

export interface EntityRowsDeps extends PageHelpers {
  siteFollowsSolar: (rows: ReadonlyArray<{ id: string; available: boolean }>) => boolean;
  importTargetShown: (demand: { available: boolean; reason: string | null } | null, value: number | null) => boolean;
  /** The site's priority list as the site sections last read it (`SiteSections.priority`). */
  sitePriority: () => SitePriority | null;
}

export function createEntityRowsSection(ctx: ViewContext, settingsPage: SettingsPageState, deps: EntityRowsDeps) {
  const { input, model, doc, idPrefix } = ctx;
  const {
    writeValue,
    overviewRow,
    rowHelp,
    entityRow,
    openValueEditor,
    editNumber,
    editSingle,
    editMulti,
    importTargetShown,
    sitePriority,
  } = deps;

  /** The vehicle's charge as the header line spells it: `~36 %` for an estimate, `No reading` for none. */
  function chargeFor(row: Vehicle): string {
    const soc = model.soc;
    if (soc !== null && soc.vehicle_id === row.id && soc.value !== null) {
      return `${soc.estimated ? "~" : ""}${percentAmount(model.language, soc.value)}`;
    }
    return row.soc_percent === null
      ? translate(model.language, "vehicleLine.noReading")
      : percentAmount(model.language, row.soc_percent);
  }

  /** A section's heading, then a muted line saying why its rows are not shown (or its rows). */
  function unreadableLine(state: EntityViewState): HTMLElement | null {
    if (state.kind === "loading") {
      return element(doc, "p", C.muted, translate(model.language, "entity.loading"));
    }
    if (state.kind === "adminOnly") {
      return element(doc, "p", C.muted, translate(model.language, "entity.adminOnly"));
    }
    if (state.kind === "failed") {
      const sentence = element(doc, "p", C.settingsNotice, translate(model.language, state.failure.sentenceKey));
      if (state.failure.code !== null) {
        sentence.dataset["code"] = state.failure.code;
      }
      return sentence;
    }
    return null;
  }

  /** The friendly name an entity field states, or `null` when it names none. */
  function fieldEntityName(config: EntityConfig, scope: EntityScope, name: string): string | null {
    const field = fieldsOf(config, scope).find((entry) => entry.field === name);
    if (field === undefined || field.kind !== "entity" || field.current === null) {
      return null;
    }
    return field.current.friendlyName;
  }

  /**
   * The charger's setup in short words (never entity ids): start and stop, the current, the energy register and
   * a smart plug's power open the charger's entity dialog; the priority opens its own choice.
   */
  function chargerRows(config: EntityConfig): HTMLElement[] {
    const control = config.control;
    const nodes: HTMLElement[] = [];
    const chargeControl = fieldsOf(config, "charger").find((entry) => entry.field === "charge_control");
    const startStopMissing =
      (chargeControl !== undefined && chargeControl.kind === "entity" && isMissingEntity(chargeControl)) ||
      ((control?.startStop.entityIds.length ?? 0) === 0 && fieldEntityName(config, "charger", "charge_control") === null);
    nodes.push(
      entityRow(
        "start_stop",
        translate(model.language, "control.startStop"),
        translate(model.language, startStopMissing ? "settings.status.missing" : "settings.status.active"),
        "charger",
        "charge-control",
      ),
    );
    if (chargeControl !== undefined && chargeControl.kind === "entity" && isMissingEntity(chargeControl)) {
      const warning = element(doc, "p", C.entityWarning, translate(model.language, "entity.missing.required"));
      warning.dataset["missing"] = "charge_control";
      nodes.push(warning);
    }
    nodes.push(
      entityRow("current", translate(model.language, "control.current"), currentWord(config), "charger", "current-limit"),
    );
    const energyField = fieldsOf(config, "charger").find((entry) => entry.field === "energy_register_entity");
    let energy = translate(model.language, "settings.status.foundAutomatically");
    if (energyField !== undefined && energyField.kind === "entity") {
      if (energyField.current !== null) {
        energy = translate(model.language, "settings.status.chosen");
      } else if (energyField.none !== null && energyField.none.chosen) {
        energy = translate(model.language, "settings.value.none");
      }
    }
    nodes.push(
      entityRow("energy_register", translate(model.language, "entity.field.energyRegister"), energy, "charger", "energy"),
    );
    // Only a charger behind a smart plug has a power sensor; the others get no row.
    const powerField = fieldsOf(config, "charger").find((entry) => entry.field === "power_entity");
    if (powerField !== undefined && powerField.kind === "entity" && powerField.current !== null) {
      nodes.push(
        entityRow(
          "power_entity",
          translate(model.language, "entity.field.powerEntity"),
          translate(model.language, "settings.status.present"),
          "charger",
          "energy",
        ),
      );
    }
    nodes.push(...wiringRows(config));
    nodes.push(...phaseSwitchingRows(config));
    for (const conflict of control?.conflicts ?? []) {
      const warning = element(
        doc,
        "p",
        C.entityWarning,
        conflictText(model.language, conflict, entityNameIn(config, conflict.entityId)),
      );
      warning.dataset["conflict"] = conflict.entityId;
      nodes.push(warning);
    }
    return nodes;
  }

  /**
   * How the current is set, in the app's words: OCPP, Easee, Controlled (a number entity) or Not controlled. A
   * path that is not enabled is not controlled, as the dashboard's own summary says.
   */
  function currentWord(config: EntityConfig): string {
    const current = config.control?.current;
    if (current === undefined) {
      return translate(
        model.language,
        fieldEntityName(config, "charger", "current_limit") === null
          ? "settings.status.notControlled"
          : "settings.status.controlled",
      );
    }
    if (current.kind === "none" || !current.enabled) {
      return translate(model.language, "settings.status.notControlled");
    }
    if (current.kind === "ocpp") {
      return "OCPP";
    }
    if (current.kind === "service") {
      return "Easee";
    }
    return translate(model.language, "settings.status.controlled");
  }

  /**
   * A charger in no site holds its own wiring: the phases it is wired for and the voltage between phases, each
   * its own choice (a site holds them for its chargers, in the site's dialog).
   */
  function wiringRows(config: EntityConfig): HTMLElement[] {
    const nodes: HTMLElement[] = [];
    const choice = (
      name: "charger_phases" | "voltage_between_phases_v",
      labelKey: TranslationKey,
      helpKey: TranslationKey,
      optionLabel: (value: string) => string,
      shown: (value: string) => string,
      fallback: string,
    ): void => {
      const field = fieldsOf(config, "charger").find((entry) => entry.field === name);
      if (field === undefined || field.kind !== "enum" || !field.writable) {
        return;
      }
      const label = translate(model.language, labelKey);
      const current = field.value ?? fallback;
      const options: ChoiceOption[] = field.choices.map((value) => ({ value, label: optionLabel(value) }));
      nodes.push(
        overviewRow(name, label, shown(current), () =>
          editSingle(
            label,
            options,
            current,
            (chosen) => writeValue({ kind: "entity", scope: "charger", draft: { [name]: chosen } }),
            translate(model.language, helpKey),
          ),
        ),
      );
    };
    const phases = (value: string): string =>
      translate(model.language, value === "1" ? "settings.phases.one" : "settings.phases.three");
    choice("charger_phases", "entity.field.chargerPhases", "entity.help.chargerPhases", phases, phases, "3");
    choice(
      "voltage_between_phases_v",
      "entity.field.voltageBetweenPhases",
      "entity.help.voltageBetweenPhases",
      (value) => translate(model.language, value === "230" ? "entity.voltage.it" : "entity.voltage.tn"),
      (value) => `${value} V`,
      "400",
    );
    return nodes;
  }

  /**
   * Phase switching for solar: Off or Automatic. Listed by Home Assistant only for a charger whose integration can
   * switch its phases, wired for three phases, on a site with a solar source; anywhere else there is no row.
   */
  function phaseSwitchingRows(config: EntityConfig): HTMLElement[] {
    const field = fieldsOf(config, "charger").find((entry) => entry.field === "phase_switching");
    if (field === undefined || field.kind !== "flag" || !field.writable) {
      return [];
    }
    const label = translate(model.language, "entity.field.phaseSwitching");
    const options: ChoiceOption[] = [
      { value: "false", label: translate(model.language, "settings.value.off") },
      { value: "true", label: translate(model.language, "entity.phaseSwitching.automatic") },
    ];
    const current = field.value ? "true" : "false";
    const value = options.find((option) => option.value === current)?.label ?? current;
    return [
      overviewRow("phase_switching", label, value, () =>
        editSingle(
          label,
          options,
          current,
          (chosen) => writeValue({ kind: "entity", scope: "charger", draft: { phase_switching: chosen } }),
          translate(model.language, "entity.help.phaseSwitching"),
        ),
      ),
    ];
  }

  /** The charge periods, an administrator's charger setting: automatic, or at most 1 to 8 (`charge-periods.ts`). */
  function chargePeriodsRows(): HTMLElement[] {
    const record = model.dashboardSettings;
    if (record === null || !input.isAdmin) {
      return [];
    }
    const label = translate(model.language, "settings.periods.label");
    return [
      overviewRow("charge_periods", label, chargePeriodsLabel(model.language, record.max_periods), () =>
        editSingle(
          label,
          chargePeriodsOptions(model.language),
          chargePeriodsValue(record),
          (chosen) => writeValue({ kind: "settings", build: (fresh) => chargePeriodsReplacement(fresh, chosen) }),
          translate(model.language, "settings.periods.help"),
        ),
      ),
    ];
  }

  /**
   * Whether users who are not administrators may change this charger's strategy and plan: an administrator's
   * choice, Off by default, for a wall screen signed in as an ordinary user. Absent on a backend without it.
   */
  function usersChangePlanRows(): HTMLElement[] {
    const record = model.dashboardSettings;
    if (record === null || record.users_change_plan === undefined || !input.isAdmin) {
      return [];
    }
    const label = translate(model.language, "settings.usersPlan.label");
    const onOff = (on: boolean): string => translate(model.language, on ? "settings.value.on" : "settings.value.off");
    const options: ChoiceOption[] = [
      { value: "on", label: onOff(true) },
      { value: "off", label: onOff(false) },
    ];
    return [
      overviewRow("users_change_plan", label, onOff(record.users_change_plan), () =>
        editSingle(
          label,
          options,
          record.users_change_plan === true ? "on" : "off",
          (chosen) =>
            writeValue({ kind: "settings", build: (fresh) => usersChangePlanReplacement(fresh, chosen === "on") }),
          translate(model.language, "settings.usersPlan.help"),
        ),
      ),
    ];
  }

  /**
   * Which car is plugged in, for a charger more than one car can charge at: the cars at this charger (at least
   * one) and how the plugged-in one is found, each in its own editor. Absent on a backend without the fields.
   */
  function identificationRows(): HTMLElement[] {
    const record = model.dashboardSettings;
    if (record === null || record.identify_mode === undefined || model.vehicleChoices.length < 2) {
      return [];
    }
    const summary = identificationSummary(model.language, record, model.vehicleChoices);
    const carsLabel = translate(model.language, "identify.vehicles.label");
    const modeLabel = translate(model.language, "identify.mode.label");
    const cars = model.vehicleChoices.map((car) => ({ value: car.id, label: car.name ?? car.id }));
    const allIds = model.vehicleChoices.map((car) => car.id);
    const stored = record.vehicle_ids ?? null;
    const ticked = stored === null ? allIds : allIds.filter((id) => stored.includes(id));
    const mode = record.identify_mode;
    const modes: ChoiceOption[] = (["automatic", "ask", "off"] as const).map((value) => ({
      value,
      label: translate(model.language, `identify.mode.${value}`),
      help: translate(model.language, `identify.mode.${value}Help`),
    }));
    const nodes: HTMLElement[] = [element(doc, "hr", C.settingsDivider)];
    nodes.push(
      overviewRow("identify_vehicles", carsLabel, summary[1]?.value ?? "", () =>
        editMulti(
          carsLabel,
          cars,
          ticked,
          (values) =>
            writeValue({
              kind: "settings",
              build: (fresh) => identificationReplacement(fresh, { mode: fresh.identify_mode ?? mode, vehicleIds: values, allVehicleIds: allIds }),
            }),
          { atLeastOne: translate(model.language, "identify.error.noVehicle") },
        ),
      ),
      overviewRow("identify_mode", modeLabel, summary[0]?.value ?? "", () =>
        editSingle(modeLabel, modes, mode, (chosen) =>
          writeValue({
            kind: "settings",
            build: (fresh) => {
              const freshIds = fresh.vehicle_ids ?? null;
              return identificationReplacement(fresh, {
                mode: chosen as IdentifyMode,
                vehicleIds: freshIds === null ? allIds : allIds.filter((id) => freshIds.includes(id)),
                allVehicleIds: allIds,
              });
            },
          }),
        ),
      ),
    );
    nodes.push(...cameraRows(record));
    return nodes;
  }

  /**
   * The camera that helps tell the cars apart (shown where Home Assistant offers a camera and an AI Task entity):
   * the camera, then with one chosen its frame (the frame editor) and its AI task.
   */
  function cameraRows(record: SettingsRecord): HTMLElement[] {
    const block = model.cameraIdentification;
    const chosen = record.identify_camera;
    if (block === null || chosen === undefined) {
      return [];
    }
    const say = (key: TranslationKey): string => translate(model.language, key);
    const cameraLabels = entityLabels(block.cameras);
    const aiLabels = entityLabels(block.ai_tasks);
    // A row names the entity, with what tells it apart when another has its name ("Ollama AI Task · qwen3-vl").
    const nameIn = (labels: ReturnType<typeof entityLabels>, id: string): string => {
      const label = labels.get(id);
      return label === undefined ? id : label.detail === null ? label.name : `${label.name} · ${label.detail}`;
    };
    const optionsOf = (labels: ReturnType<typeof entityLabels>, list: ReadonlyArray<{ entity_id: string }>): ChoiceOption[] =>
      list.map((item) => {
        const label = labels.get(item.entity_id);
        return {
          value: item.entity_id,
          label: label?.name ?? item.entity_id,
          ...(label?.detail == null ? {} : { help: label.detail }),
        };
      });
    const cameraLabel = say("camera.label");
    const nodes: HTMLElement[] = [];
    nodes.push(
      overviewRow(
        "identify_camera",
        cameraLabel,
        chosen === null ? say("camera.none") : nameIn(cameraLabels, chosen.camera_entity_id),
        () =>
          editSingle(
            cameraLabel,
            [
              ...optionsOf(cameraLabels, block.cameras),
              { value: "", label: say("camera.none") },
            ],
            chosen?.camera_entity_id ?? "",
            (value) =>
              writeValue({
                kind: "settings",
                build: (fresh) =>
                  cameraReplacement(fresh, {
                    cameraEntityId: value === "" ? null : value,
                    aiTaskEntityId: fresh.identify_camera?.ai_task_entity_id ?? null,
                  }),
              }),
            `${say("camera.intro")} ${say("camera.help")}`,
          ),
      ),
    );
    if (chosen !== null) {
      const frameLabel = say("camera.frame.label");
      nodes.push(
        overviewRow(
          "identify_frame",
          frameLabel,
          say(isWholePicture(chosen.frame) ? "camera.frame.whole" : "camera.frame.drawn"),
          () =>
            openValueEditor(frameLabel, (handlers) =>
              frameEditor(
                doc,
                model.language,
                {
                  load: () => input.onCameraPicture?.({ kind: "snapshot" }) ?? Promise.resolve({ code: null }),
                  frame: chosen.frame,
                  idPrefix,
                },
                (frame) => writeValue({ kind: "cameraFrame", frame }),
                handlers,
              ),
            ),
        ),
      );
      const aiLabel = say("camera.aiTask.label");
      nodes.push(
        overviewRow(
          "identify_ai_task",
          aiLabel,
          chosen.ai_task_entity_id === null ? say("camera.aiTask.default") : nameIn(aiLabels, chosen.ai_task_entity_id),
          () =>
            editSingle(
              aiLabel,
              [
                { value: "", label: say("camera.aiTask.default") },
                ...optionsOf(aiLabels, block.ai_tasks),
              ],
              chosen.ai_task_entity_id ?? "",
              (value) =>
                writeValue({
                  kind: "settings",
                  build: (fresh) =>
                    cameraReplacement(fresh, {
                      cameraEntityId: fresh.identify_camera?.camera_entity_id ?? chosen.camera_entity_id,
                      aiTaskEntityId: value === "" ? null : value,
                    }),
                }),
              say("camera.aiTask.intro"),
            ),
        ),
      );
    }
    return nodes;
  }

  /** A car's reference pictures from the chosen camera, when a camera is chosen and the car is this charger's. */
  function vehicleReference(row: Vehicle): VehicleReference | undefined {
    const block = model.cameraIdentification;
    const chosen = model.dashboardSettings?.identify_camera;
    const pictures = block?.references[row.id];
    if (block === null || chosen === undefined || chosen === null || pictures === undefined) {
      return undefined;
    }
    const carName = row.name ?? translate(model.language, "settings.vehicle.unnamed");
    const thumbnailOf = (picture: ReferencePicture): Promise<CameraPicture | null> => {
      if (input.onCameraPicture === undefined) {
        return Promise.resolve(null);
      }
      return input
        .onCameraPicture({ kind: "reference", vehicleId: row.id, pictureKind: picture.kind, takenAt: picture.taken_at })
        .then((answer) => ("picture" in answer ? answer.picture : null));
    };
    const thumbnail = (kind: PictureKind): Promise<CameraPicture | null> => {
      const picture = pictures.find((item) => item.kind === kind);
      return picture === undefined ? Promise.resolve(null) : thumbnailOf(picture);
    };
    const formatTaken = (iso: string): string => {
      const instant = Date.parse(iso);
      return Number.isFinite(instant) && hasZone(model.format)
        ? `${weekdayDate(model.format, instant)} ${clock(model.format, instant)}`
        : iso.slice(0, 16).replace("T", " ");
    };
    return {
      text: referenceText(model.language, pictures),
      kinds: pictures.map((picture) => picture.kind),
      thumbnail,
      ...(input.isAdmin
        ? {
            onTap: () =>
              openValueEditor(translate(model.language, "reference.title", { name: carName }), (handlers) =>
                referenceEditor(doc, model.language, {
                  pictures,
                  thumbnail: thumbnailOf,
                  act: (kind, action) =>
                    input.onReferencePicture?.(row.id, kind, action) ??
                    Promise.resolve({ message: translate(model.language, "settings.error.generic") }),
                  formatTaken,
                  onClose: (changed) => {
                    if (changed && input.onReferencesChanged !== undefined) {
                      input.onReferencesChanged();
                    } else {
                      handlers.onCancel();
                    }
                  },
                }),
              ),
          }
        : {}),
    };
  }

  /** A car's editors: each property its own number or choice, written as the vehicle dialog wrote it. */
  function vehicleEdits(row: Vehicle, config: EntityConfig | null): VehicleEdits {
    const edits: VehicleEdits = {};
    /**
     * One property's Save, under compare-and-set on what the row showed; after a conflict the next Save expects
     * what the answer says is stored now (the editor says it changed elsewhere and keeps what was typed).
     */
    const propertyWrite = <
      K extends
        | "capacity_kwh"
        | "consumption_kwh_per_10km"
        | "onboard_phases"
        | "target_percent"
        | "min_percent"
        | "wake_for_reading",
    >(
      field: K,
    ): ((value: Vehicle[K]) => Promise<string | null>) => {
      let expected: Vehicle[K] = row[field];
      return (value) =>
        writeValue({
          kind: "vehicle",
          vehicleId: row.id,
          changes: { [field]: value },
          expected: { [field]: expected },
          onConflict: (fresh) => {
            expected = fresh[field];
          },
        });
    };
    const sensor = config?.vehicles.find((entry) => entry.id === row.id) ?? null;
    if (sensor !== null && sensor.candidates.length > 0) {
      const label = translate(model.language, "entity.field.vehicleSoc");
      edits.sensor = () =>
        editSingle(
          label,
          [
            ...sensor.candidates.map((candidate) => ({ value: candidate.entityId, label: candidate.friendlyName })),
            { value: "", label: translate(model.language, "entity.vehicle.automatic") },
          ],
          vehicleChoice(sensor),
          (chosen) => writeValue({ kind: "vehicleSoc", vehicleId: row.id, entityId: chosen === "" ? null : chosen }),
          // Only when automatic detection really has nothing to pick among several sensors.
          sensor.candidates.length > 1 && sensor.selected === null
            ? translate(model.language, "entity.vehicle.several")
            : undefined,
        );
    }
    // The target is set with a slider; a car with none stored opens at the target it is planned with.
    const plannedTarget = row.target_percent ?? defaultTargetPercent(row.max_percent);
    if (row.target_percent !== undefined) {
      const current = row.target_percent;
      const save = propertyWrite("target_percent");
      edits.target = () =>
        openValueEditor(translate(model.language, "settings.soc.target"), (handlers) =>
          targetEditor(
            doc,
            model.language,
            {
              current,
              fallback: plannedTarget,
              help: translate(model.language, "settings.vehicle.targetHelp"),
              idPrefix,
            },
            save,
            handlers,
          ),
        );
    }
    if (row.min_percent !== undefined) {
      const current = row.min_percent;
      const save = propertyWrite("min_percent");
      edits.minimum = () =>
        openValueEditor(translate(model.language, "settings.vehicle.minimum"), (handlers) =>
          floorEditor(
            doc,
            model.language,
            {
              current,
              target: plannedTarget,
              help: translate(model.language, "settings.vehicle.minimumHelp"),
              idPrefix,
            },
            save,
            handlers,
          ),
        );
    }
    const wake = row.wake_for_reading;
    if (wake !== undefined && wake !== null) {
      const save = propertyWrite("wake_for_reading");
      edits.wake = () =>
        editSingle(
          translate(model.language, "settings.vehicle.wake"),
          [
            { value: "on", label: translate(model.language, "settings.value.on") },
            { value: "off", label: translate(model.language, "settings.value.off") },
          ],
          wake ? "on" : "off",
          (chosen) => save(chosen === "on"),
          translate(model.language, "settings.vehicle.wakeHelp"),
        );
    }
    edits.capacity = () =>
      editNumber(
        translate(model.language, "settings.capacity.label"),
        {
          unit: "kWh",
          current: row.capacity_kwh,
          min: CAPACITY_MIN_KWH,
          max: CAPACITY_MAX_KWH,
          decimals: 1,
          noneLabel: translate(model.language, "entity.notSet"),
          // The size learned from charging, offered and never saved unless chosen.
          ...(row.capacity_kwh === null && (row.learned_capacity_kwh ?? null) !== null
            ? { suggestion: row.learned_capacity_kwh }
            : {}),
        },
        propertyWrite("capacity_kwh"),
      );
    edits.consumption = () =>
      editNumber(
        translate(model.language, "settings.consumption.label"),
        {
          unit: translate(model.language, "settings.consumption.unit"),
          current: row.consumption_kwh_per_10km,
          min: CONSUMPTION_MIN_KWH_PER_10KM,
          max: CONSUMPTION_MAX_KWH_PER_10KM,
          decimals: 1,
        },
        propertyWrite("consumption_kwh_per_10km"),
      );
    const limit = row.max_percent;
    if (limit !== null && model.setChargeLimit) {
      // A slider over what the car's integration takes (1-100 in whole percent when it says nothing).
      const range = row.charge_limit_range;
      edits.chargeLimit = () =>
        openValueEditor(translate(model.language, "settings.vehicle.limit"), (handlers) =>
          limitEditor(
            doc,
            model.language,
            { current: limit, range, help: translate(model.language, "settings.vehicle.limitHelp"), idPrefix },
            (value) => writeValue({ kind: "chargeLimit", vehicleId: row.id, percent: value }),
            handlers,
          ),
        );
    }
    edits.onboard = () =>
      editSingle(
        translate(model.language, "settings.vehicle.onboardLegend"),
        [
          { value: "1", label: translate(model.language, "settings.vehicle.onboardOne") },
          { value: "3", label: translate(model.language, "settings.vehicle.onboardThree") },
        ],
        String(row.onboard_phases),
        (() => {
          const save = propertyWrite("onboard_phases");
          return (chosen: string) => save(chosen === "1" ? 1 : 3);
        })(),
        translate(model.language, "settings.vehicle.onboardHelp"),
      );
    const sources = row.identification;
    if (sources !== undefined) {
      for (const kind of ["plug", "location"] as const) {
        const source = sources[kind];
        edits[kind] = () =>
          editSingle(
            translate(model.language, kind === "plug" ? "identify.source.plug" : "identify.source.location"),
            [
              { value: "", label: translate(model.language, "identify.source.automatic") },
              ...source.candidates.map((candidate) => sourceOption(candidate)),
              ...source.others.map((other, index) =>
                sourceOption(other, index === 0 ? translate(model.language, "identify.source.others") : undefined),
              ),
              { value: "none", label: translate(model.language, "identify.source.none") },
            ],
            sourceChoice(source),
            (chosen) =>
              writeValue({ kind: "vehicleSource", vehicleId: row.id, source: kind, entityId: chosen === "" ? null : chosen }),
            translate(model.language, "identify.source.help"),
            sourceReadingText(model.language, source),
          );
      }
    }
    return edits;
  }

  /**
   * The site's setup: the main fuse in its own number editor; the measurement and the battery in short words,
   * opening the site's entity dialog.
   */
  /**
   * The site's priority list (`priority-list.ts`), an administrator's: read with the site's settings view when
   * Settings opens (`SiteSections.loadSite`), then the top row as the value and the whole list in its editor (up and
   * down buttons). Nothing before that read, or where the site states none.
   */
  function sitePriorityRows(): HTMLElement[] {
    const block = input.isAdmin && model.site !== null ? sitePriority() : null;
    if (block === null || block.order.length === 0) {
      return [];
    }
    const label = translate(model.language, "site.priority.title");
    const value = prioritySummary(model.language, block);
    const confirmed = block.order.map((row) => row.id);
    const writable = block.writable && block.order.length > 1;
    return [
      overviewRow(
        "priority",
        label,
        value,
        writable
          ? () =>
              openValueEditor(label, (handlers) =>
                priorityEditor(
                  doc,
                  model.language,
                  block,
                  idPrefix,
                  async (order) => await writeValue({ kind: "priority", confirmed, order }),
                  handlers,
                ),
              )
          : undefined,
      ),
      rowHelp("priority", translate(model.language, "site.priority.intro")),
    ];
  }

  function siteRows(config: EntityConfig): HTMLElement[] {
    const nodes: HTMLElement[] = [];
    const fuse = fieldsOf(config, "site").find((entry) => entry.field === "main_fuse_a");
    if (fuse !== undefined && fuse.kind === "number" && fuse.value !== null) {
      const label = translate(model.language, "entity.field.mainFuse");
      const current = fuse.value;
      nodes.push(
        overviewRow("main_fuse_a", label, `${formatNumber(model.language, current, 1)} A`, fuse.writable
          ? () =>
              editNumber(label, { unit: "A", current, min: fuse.minimum, max: 1000, decimals: 1 }, (value) =>
                writeValue({ kind: "entity", scope: "site", draft: { main_fuse_a: value === null ? "" : String(value) } }),
              )
          : undefined),
      );
    }
    // Beside the fuse: the import target, and the hour running now against it.
    const target = fieldsOf(config, "site").find((entry) => entry.field === "import_target_kw");
    if (
      target !== undefined &&
      target.kind === "number" &&
      importTargetShown(model.gridTariff?.demand ?? null, target.value)
    ) {
      const label = translate(model.language, "site.importTarget");
      const current = target.value;
      const shown = current === null ? translate(model.language, "settings.value.off") : kwText(model.language, current);
      nodes.push(
        overviewRow("import_target_kw", label, shown, target.writable
          ? () =>
              editNumber(
                label,
                {
                  help: translate(model.language, "site.importTarget.help"),
                  unit: "kW",
                  current,
                  min: target.minimum,
                  max: 1000,
                  decimals: 1,
                  noneLabel: translate(model.language, "settings.value.off"),
                  ...(input.onSuggestImportTarget === undefined
                    ? {}
                    : {
                        advice: {
                          loading: translate(model.language, "target.suggest.loading"),
                          use: translate(model.language, "target.suggest.use"),
                          answer: input.onSuggestImportTarget().then((raw) => suggestionAdvice(model.language, raw)),
                        },
                      }),
                },
                (value) =>
                  writeValue({ kind: "entity", scope: "site", draft: { import_target_kw: value === null ? "" : String(value) } }),
              )
          : undefined),
      );
      const hour = hourLine(model.language, model.gridTariff?.demand ?? null);
      if (hour !== null) {
        nodes.push(rowHelp("import_target_hour", hour));
      }
    }
    const mode = storedMode(config);
    if (mode !== null) {
      nodes.push(
        entityRow("measurement_mode", translate(model.language, "entity.field.measurementMode"), modeLabel(model.language, mode), "site"),
      );
    }
    nodes.push(
      entityRow(
        "battery",
        translate(model.language, "site.row.battery"),
        translate(
          model.language,
          fieldEntityName(config, "site", "battery_aggregate_power_entity") === null ? "settings.value.none" : "settings.status.present",
        ),
        "site",
      ),
    );
    // Offered only while SpotNav cannot find the heat pump's meter by itself (or once one is chosen).
    if (fieldsOf(config, "site").some((entry) => entry.field === "heat_meter_entity")) {
      const label = translate(model.language, "entity.field.heatMeter");
      nodes.push(
        entityRow(
          "heat_meter",
          label,
          fieldEntityName(config, "site", "heat_meter_entity") ?? translate(model.language, "settings.value.none"),
          "site",
          "heat-meter",
        ),
      );
    }
    nodes.push(...sitePriorityRows());
    if (config.site !== null) {
      nodes.push(...siteWarningRows(doc, model.language, config.site));
    }
    return nodes;
  }

  /** Where the open car's tab is kept when the card keeps nothing across renders. */
  const localTabs: { vehicle?: string | null } = {};

  /** The cars' tabs (and the guest's): compact text tabs, the open one marked; a tap opens that car's rows. */
  function vehicleTabs(
    cars: ReadonlyArray<{ id: string; name: string | null }>,
    openId: string,
    holder: { vehicle?: string | null },
  ): HTMLElement {
    const list = element(doc, "div", C.vehicleTabs);
    list.setAttribute("role", "tablist");
    list.setAttribute("aria-label", translate(model.language, "settings.heading.car"));
    for (const car of cars) {
      const tab = element(
        doc,
        "button",
        C.vehicleTab,
        car.name ?? translate(model.language, "settings.vehicle.unnamed"),
      ) as HTMLButtonElement;
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(car.id === openId));
      tab.tabIndex = car.id === openId ? 0 : -1;
      tab.dataset["vehicleTab"] = car.id;
      tab.addEventListener("click", () => {
        holder.vehicle = car.id;
        paintEntities();
        settingsPage.vehicleListSlot?.querySelector<HTMLButtonElement>(`[data-vehicle-tab="${car.id}"]`)?.focus();
      });
      tab.addEventListener("keydown", (event) => {
        const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (step === 0) {
          return;
        }
        const index = cars.findIndex((item) => item.id === car.id);
        const next = cars[(index + step + cars.length) % cars.length];
        if (next !== undefined) {
          event.preventDefault();
          holder.vehicle = next.id;
          paintEntities();
          settingsPage.vehicleListSlot?.querySelector<HTMLButtonElement>(`[data-vehicle-tab="${next.id}"]`)?.focus();
        }
      });
      list.append(tab);
    }
    return list;
  }

  /**
   * The Guest tab's section: the "Car" heading with the tabs, and one row, the guest amount a guest plan starts from
   * (`spotnav/update_guest`). No picture, sensor, capacity or target: a guest car has none here.
   */
  function guestSummary(amountKwh: number, tabs: HTMLElement): HTMLElement {
    const card = element(doc, "section", C.settingsSection);
    card.dataset["section"] = "vehicle";
    card.dataset["vehicle"] = GUEST_VEHICLE_ID;
    card.append(sectionHeading(doc, "car", translate(model.language, "settings.heading.car")), tabs);
    const label = translate(model.language, "settings.guest.amount");
    const value = `${formatNumber(model.language, amountKwh, 1)} kWh`;
    card.append(
      ...settingRow(doc, {
        key: "guest_amount",
        label,
        value,
        help: translate(model.language, "settings.guest.amountHelp"),
        onTap: () =>
          editNumber(
            label,
            { unit: "kWh", current: amountKwh, min: GUEST_KWH_MIN, max: GUEST_KWH_MAX, decimals: 1 },
            (chosen) =>
              chosen === null
                ? Promise.resolve(translate(model.language, "settings.error.required"))
                : writeValue({ kind: "guest", amountKwh: chosen }),
          ),
        changeableText: translate(model.language, "settings.row.changeable", { label, value }),
      }),
    );
    return card;
  }

  /** The charger's section: its heading, its setup rows and the identification rows. */
  function paintEntitySlot(state: EntityViewState, slot: HTMLElement): void {
    slot.replaceChildren(
      sectionHeading(doc, "charger", translate(model.language, "settings.heading.charger"), model.chargerName),
    );
    const line = unreadableLine(state);
    if (line !== null) {
      slot.append(line);
    }
    if (state.kind === "ready") {
      slot.append(...chargerRows(state.config));
    }
    slot.append(...chargePeriodsRows());
    slot.append(...usersChangePlanRows());
    slot.append(...identificationRows());
  }

  function paintEntities(): void {
    const state = settingsPage.entityState;
    if (settingsPage.vehicleListSlot !== null) {
      const root = settingsPage.vehicleListSlot.getRootNode() as Document | ShadowRoot;
      const active = root.activeElement;
      const focusId =
        active !== null && settingsPage.vehicleListSlot.contains(active) ? (active as HTMLElement).dataset["editVehicle"] : undefined;
      settingsPage.vehicleListSlot.replaceChildren();
      if (settingsPage.vehicleRows.length === 0 && !(state.kind === "ready" && state.config.vehicles.length > 0)) {
        const none = element(doc, "section", C.settingsSection);
        none.dataset["section"] = "vehicle";
        none.append(
          element(doc, "h4", C.settingsSectionHeading, translate(model.language, "settings.section.vehicle")),
          element(doc, "p", C.muted, translate(model.language, "settings.vehicle.none")),
        );
        settingsPage.vehicleListSlot.append(none);
      }
      // A vehicle the configuration lists but the dashboard does not: its sensor can still be chosen.
      const extra: Vehicle[] =
        state.kind === "ready"
          ? state.config.vehicles
              .filter((entry) => !settingsPage.vehicleRows.some((row) => row.id === entry.id))
              .map((entry) => ({
                id: entry.id,
                name: entry.name,
                soc_entity_id: null,
                capacity_kwh: null,
                capacity_source: null,
                consumption_kwh_per_10km: null,
                max_percent: null,
                soc_percent: null,
                onboard_phases: 3,
                suggested_onboard_phases: null,
              }))
          : [];
      // With two or more cars: one "Car" section, a tab per car in its header, the open car's rows below. An
      // administrator also has the Guest tab after the household's cars: the guest amount, nothing else.
      const cars = [...settingsPage.vehicleRows, ...extra];
      const guest = input.isAdmin && model.guest !== null && cars.length > 0 ? model.guest : null;
      const tabItems: Array<{ id: string; name: string | null }> = [
        ...cars,
        ...(guest === null ? [] : [{ id: GUEST_VEHICLE_ID, name: translate(model.language, "identify.guest") }]),
      ];
      const holder = input.overviewPlace ?? localTabs;
      const remembered = holder.vehicle ?? null;
      const guestOpen = guest !== null && remembered === GUEST_VEHICLE_ID;
      const open = guestOpen
        ? undefined
        : (cars.find((row) => row.id === remembered) ??
          cars.find((row) => row.id === model.targetVehicleId) ??
          cars[0]);
      const openId = guestOpen ? GUEST_VEHICLE_ID : open?.id;
      const tabs = tabItems.length > 1 && openId !== undefined ? vehicleTabs(tabItems, openId, holder) : undefined;
      if (guestOpen && guest !== null && tabs !== undefined) {
        settingsPage.vehicleListSlot.append(guestSummary(guest.amount_kwh, tabs));
      }
      for (const row of cars) {
        if (tabs !== undefined && row !== open) {
          continue;
        }
        settingsPage.vehicleListSlot.append(
          vehicleSummary(doc, model.language, {
            row,
            properties: !extra.includes(row),
            planned: row.id === model.targetVehicleId,
            charge: chargeFor(row),
            edits: input.isAdmin ? vehicleEdits(row, state.kind === "ready" ? state.config : null) : {},
            reference: vehicleReference(row),
            ...(tabs === undefined ? {} : { tabs }),
          }),
        );
      }
      if (focusId !== undefined) {
        settingsPage.vehicleListSlot
          .querySelector<HTMLElement>(`[data-vehicle="${focusId}"] button`)
          ?.focus();
      }
    }
    if (settingsPage.entitySlot !== null) {
      // A row of this slot that has focus (the one an editor returned to) keeps it across the repaint.
      const root = settingsPage.entitySlot.getRootNode() as Document | ShadowRoot;
      const focused = root.activeElement as HTMLElement | null;
      const focusedEdit = focused !== null && settingsPage.entitySlot.contains(focused) ? (focused.dataset["edit"] ?? null) : null;
      paintEntitySlot(state, settingsPage.entitySlot);
      if (focusedEdit !== null) {
        Array.from(settingsPage.entitySlot.querySelectorAll<HTMLElement>("[data-edit]"))
          .find((node) => node.dataset["edit"] === focusedEdit)
          ?.focus({ preventScroll: true });
      }
    }
    if (settingsPage.siteEntitySlot !== null) {
      settingsPage.siteEntitySlot.replaceChildren();
      const line = unreadableLine(state);
      if (line !== null) {
        settingsPage.siteEntitySlot.append(line);
      }
      if (state.kind === "ready" && state.config.site !== null) {
        settingsPage.siteEntitySlot.append(...siteRows(state.config));
      }
    }

  }

  return { paintEntities };
}
