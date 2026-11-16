// The heat card (`custom:spotnav-heat-card`, SpotNav 3.0 preview): a third element in the same bundle, so nothing extra
// is installed. It reads `spotnav/get_heat` for a site, named by the site's id or a charger's in it, every
// `HEAT_REFRESH_MS`, and shows it (`heat-view.ts`). The dial and home/away write the site's heat settings through
// `spotnav/update_heat` (a compare-and-set); an administrator's settings button opens the "Heating" section. Shadow
// only: what the card writes is SpotNav's own settings, never a heating device.
//
// YAML: `type: custom:spotnav-heat-card` with `site: <site entry id>` or `charger: <charger entry id>`.

import { getHeat, listChargers, SpotnavApiError, UNSUPPORTED_API_VERSION, updateHeat } from "./api";
import { createDialog, type DialogHandle } from "./dialog";
import { decodeHeat, decodeHeatWrite, type HeatAnswer, type HeatChoice, type HeatConfig } from "./heat-model";
import { HEAT_CLASSES as X, HEAT_STYLES } from "./heat-styles";
import {
  TARGET_MAX,
  TARGET_MIN,
  instantToLocal,
  localToInstant,
  heatBody,
  heatSettingsSection,
  namesOf,
  type HeatSettingKey,
  type HeatState,
  type HeatUi,
} from "./heat-view";
import { HOUSE_STYLES } from "./house-styles";
import { isLanguagePending, loadLanguage, resolveLanguage, translate, type Language, type TranslationKey } from "./i18n";
import type { HomeAssistantLike } from "./types";
import { multiEditor, numberEditor, shell, singleEditor, type ChoiceOption, type EditorHandlers } from "./value-editors";
import { VISUAL_CLASSES, VISUAL_STYLES } from "./visual-styles";

export { instantToLocal, localToInstant };

export const HEAT_CARD_TYPE = "spotnav-heat-card";
export const HEAT_CARD_ELEMENT = "spotnav-heat-card";
export const HEAT_REFRESH_MS = 60_000;
/** A dial step is written this long after the last tap, so a run of taps is one write. */
export const DIAL_SETTLE_MS = 700;
export const HEAT_FALLBACK_WIDTH = 330;
const CARD_PADDING_PX = 28;
const STUB_TIMEOUT_MS = 3000;
/** The return time the away editor offers first. */
const DEFAULT_RETURN = "16:00";

export interface HeatCardConfig {
  type: string;
  site?: string;
  charger?: string;
}

/** The YAML, checked: `site` or `charger` as a non-empty string (the site wins when both are given). */
export function parseHeatConfig(raw: unknown): HeatCardConfig {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error("Invalid configuration");
  }
  const source = raw as Record<string, unknown>;
  const config: HeatCardConfig = { type: typeof source.type === "string" ? source.type : `custom:${HEAT_CARD_TYPE}` };
  for (const key of ["site", "charger"] as const) {
    const value = source[key];
    if (value === undefined || value === null || value === "") {
      continue;
    }
    if (typeof value !== "string") {
      throw new Error(`${key} must be text`);
    }
    config[key] = value;
  }
  return config;
}

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

let instances = 0;

export class SpotnavHeatCard extends HTMLElement {
  private readonly root: ShadowRoot;
  private readonly mount: HTMLDivElement;
  private readonly layer: HTMLDivElement;
  private hassObject: HomeAssistantLike | null = null;
  private config: HeatCardConfig | null = null;
  private state: HeatState = { kind: "unconfigured" };
  private ui: HeatUi = { pendingTarget: null, error: null };
  private language: Language | null = null;
  private wantedLanguage: Language | null = null;
  private timer: number | null = null;
  private settle: ReturnType<typeof setTimeout> | null = null;
  private connected = false;
  private generation = 0;
  private width = HEAT_FALLBACK_WIDTH;
  private observer: ResizeObserver | null = null;
  private dialog: DialogHandle | null = null;
  /** What the open dialog shows: the settings section, or one editor and where it returns. */
  private page: { kind: "settings" } | { kind: "editor"; key: HeatSettingKey | "away"; back: boolean } | null = null;
  private readonly idPrefix: string;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    instances += 1;
    this.idPrefix = `spotnav-heat-${instances}`;
    const doc = this.ownerDocument;
    const style = doc.createElement("style");
    style.textContent = VISUAL_STYLES + HOUSE_STYLES + HEAT_STYLES;
    this.mount = doc.createElement("div");
    this.mount.className = VISUAL_CLASSES.shell;
    this.layer = doc.createElement("div");
    this.layer.className = X.card;
    this.root.append(style, this.mount, this.layer);
  }

  setConfig(config: unknown): void {
    this.config = parseHeatConfig(config);
    this.generation += 1;
    this.ui = { pendingTarget: null, error: null };
    this.state = this.target() === null ? { kind: "unconfigured" } : { kind: "loading" };
    this.render();
    this.start();
  }

  set hass(hass: HomeAssistantLike) {
    const first = this.hassObject === null;
    this.hassObject = hass;
    this.followLanguage(resolveLanguage(hass.locale?.language ?? hass.language));
    if (first) {
      this.start();
    }
  }

  get hass(): HomeAssistantLike | null {
    return this.hassObject;
  }

  connectedCallback(): void {
    this.connected = true;
    if (typeof ResizeObserver !== "undefined" && this.observer === null) {
      this.observer = new ResizeObserver(() => this.measure());
      this.observer.observe(this);
    }
    this.measure();
    this.start();
  }

  disconnectedCallback(): void {
    this.connected = false;
    this.generation += 1;
    this.observer?.disconnect();
    this.observer = null;
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
    if (this.settle !== null) {
      clearTimeout(this.settle);
      this.settle = null;
    }
    this.dialog?.destroy();
    this.dialog = null;
    this.page = null;
  }

  getCardSize(): number {
    return 7;
  }

  getGridOptions(): { rows: "auto"; columns: number; min_columns: number } {
    return { rows: "auto", columns: 6, min_columns: 6 };
  }

  /** The card picker's preview: the one charger's site, else an unconfigured card. */
  static async getStubConfig(hass?: HomeAssistantLike): Promise<HeatCardConfig> {
    const stub: HeatCardConfig = { type: `custom:${HEAT_CARD_TYPE}` };
    if (hass === undefined) {
      return stub;
    }
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const list = await Promise.race([
        listChargers(hass),
        new Promise<null>((resolve) => {
          timer = setTimeout(() => resolve(null), STUB_TIMEOUT_MS);
        }),
      ]);
      const first = list?.chargers[0];
      if (first !== undefined) {
        return { ...stub, charger: first.charger_id };
      }
    } catch {
      // An unconfigured card, never an error in the picker.
    } finally {
      clearTimeout(timer);
    }
    return stub;
  }

  private target(): { site: string } | { charger: string } | null {
    const config = this.config;
    if (config?.site !== undefined) {
      return { site: config.site };
    }
    return config?.charger !== undefined ? { charger: config.charger } : null;
  }

  private cardLanguage(): Language {
    return this.language ?? resolveLanguage(null);
  }

  private answer(): HeatAnswer | null {
    return this.state.kind === "ready" ? this.state.answer : null;
  }

  private followLanguage(next: Language): void {
    if (this.wantedLanguage === next) {
      return;
    }
    this.wantedLanguage = next;
    if (!isLanguagePending(next)) {
      this.language = next;
      this.render();
      return;
    }
    void loadLanguage(next).then(() => {
      if (this.wantedLanguage === next) {
        this.language = next;
        this.render();
      }
    });
  }

  private measure(): void {
    const measured = this.getBoundingClientRect().width - CARD_PADDING_PX;
    const width = measured > 0 ? measured : HEAT_FALLBACK_WIDTH;
    if (Math.abs(width - this.width) >= 4) {
      this.width = width;
      this.render();
    }
  }

  private start(): void {
    if (!this.connected || this.hassObject === null || this.target() === null) {
      return;
    }
    if (this.timer === null) {
      this.timer = window.setInterval(() => void this.refresh(), HEAT_REFRESH_MS);
    }
    void this.refresh();
  }

  /** Read the answer once; an older read that comes back after a newer config is dropped. */
  async refresh(): Promise<void> {
    const hass = this.hassObject;
    const target = this.target();
    if (hass === null || target === null) {
      return;
    }
    const generation = this.generation;
    let next: HeatState;
    try {
      const decoded = decodeHeat(await getHeat(hass, target));
      next = decoded.ok
        ? { kind: "ready", answer: decoded.value }
        : { kind: "failed", sentenceKey: decoded.failure === "unsupported" ? "heat.unsupported" : "heat.failed", code: null };
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      const unsupported = code === UNSUPPORTED_API_VERSION || code === "unknown_command";
      next = { kind: "failed", sentenceKey: unsupported ? "heat.unsupported" : "heat.failed", code };
    }
    if (generation !== this.generation) {
      return;
    }
    if (next.kind === "failed" && this.state.kind === "ready") {
      return;
    }
    const before = this.answer()?.config;
    this.state = next;
    this.render();
    // The open settings follow a change of the stored settings only, not every read.
    if (this.page?.kind === "settings" && next.kind === "ready" && JSON.stringify(before) !== JSON.stringify(next.answer.config)) {
      this.showSettings();
    }
  }

  /** Write `changes` with what was last seen for the same keys; `null` when it took, else the sentence. */
  async write(changes: Partial<Record<keyof HeatConfig, unknown>>): Promise<string | null> {
    const hass = this.hassObject;
    const target = this.target();
    const answer = this.answer();
    const language = this.cardLanguage();
    if (hass === null || target === null || answer === null) {
      return translate(language, "heat.error.generic");
    }
    const expected: Record<string, unknown> = {};
    for (const key of Object.keys(changes) as (keyof HeatConfig)[]) {
      expected[key] = answer.config[key];
    }
    let raw: unknown;
    try {
      raw = await updateHeat(hass, target, changes, expected);
    } catch {
      return translate(language, "heat.error.generic");
    }
    const decoded = decodeHeatWrite(raw);
    if (!decoded.ok) {
      return translate(language, "heat.error.generic");
    }
    const result = decoded.value;
    if (result.ok && result.config !== null && this.state.kind === "ready") {
      // The stored settings at once; the rest of the view follows with the read.
      this.state = { kind: "ready", answer: { ...this.state.answer, config: result.config } };
    }
    await this.refresh();
    return result.ok ? null : writeError(language, result.error, result.field_errors.map((item) => item.code));
  }

  private step(delta: number): void {
    const answer = this.answer();
    if (answer === null) {
      return;
    }
    const base = this.ui.pendingTarget ?? answer.config.target_c;
    const next = Math.min(TARGET_MAX, Math.max(TARGET_MIN, Math.round((base + delta) * 2) / 2));
    this.ui = { pendingTarget: next, error: null };
    this.render();
    if (this.settle !== null) {
      clearTimeout(this.settle);
    }
    this.settle = setTimeout(() => {
      this.settle = null;
      const wanted = this.ui.pendingTarget;
      if (wanted === null || wanted === answer.config.target_c) {
        this.ui = { pendingTarget: null, error: null };
        this.render();
        return;
      }
      void this.write({ target_c: wanted }).then((error) => {
        this.ui = { pendingTarget: null, error };
        this.render();
      });
    }, DIAL_SETTLE_MS);
  }

  private ensureDialog(): DialogHandle {
    if (this.dialog === null) {
      this.dialog = createDialog({
        owner: this.layer,
        idPrefix: this.idPrefix,
        labels: { close: translate(this.cardLanguage(), "dialog.close") },
        background: () => this.mount,
        onClose: () => {
          this.page = null;
        },
      });
    }
    return this.dialog;
  }

  showSettings(): void {
    const answer = this.answer();
    if (answer === null || !answer.writable) {
      return;
    }
    const language = this.cardLanguage();
    this.page = { kind: "settings" };
    const body = heatSettingsSection(this.ownerDocument, language, answer, (key) => this.showEditor(key, true));
    this.ensureDialog().show({ title: translate(language, "heat.settings.open"), body });
  }

  showEditor(key: HeatSettingKey | "away", back: boolean): void {
    const answer = this.answer();
    if (answer === null) {
      return;
    }
    const form = this.editor(key, answer, {
      onDone: () => (back ? this.showSettings() : this.ensureDialog().hide()),
      onCancel: () => (back ? this.showSettings() : this.ensureDialog().hide()),
    });
    if (form === null) {
      return;
    }
    this.page = { kind: "editor", key, back };
    this.ensureDialog().show({ title: form.title, body: form.body });
  }

  /** The editor for one setting: its title and form (`null` for a setting that cannot be changed here). */
  private editor(key: HeatSettingKey | "away", answer: HeatAnswer, handlers: EditorHandlers): { title: string; body: HTMLElement } | null {
    const doc = this.ownerDocument;
    const language = this.cardLanguage();
    const config = answer.config;
    const choices = answer.choices;
    const prefix = `${this.idPrefix}-${key.replace(/[^a-z0-9]/gi, "-")}`;
    const t = (k: TranslationKey, params?: Record<string, string>): string => translate(language, k, params);
    const option = (choice: HeatChoice): ChoiceOption => ({ value: choice.entity_id, label: choice.name, ...(choice.value === null ? {} : { help: choice.value }) });
    const none: ChoiceOption = { value: "", label: t("heat.settings.none") };
    const pick = (title: string, options: ChoiceOption[], selected: string | null, save: (value: string | null) => Promise<string | null>, intro?: string) => ({
      title,
      body: singleEditor(doc, language, { ...(intro === undefined ? {} : { intro }), options, selected: selected ?? "", idPrefix: prefix }, (value) => save(value === "" ? null : value), handlers),
    });
    if (key === "away") {
      return { title: t("heat.away.title"), body: this.awayEditor(answer, handlers) };
    }
    if (key === "night") {
      return { title: t("heat.settings.night"), body: this.nightEditor(answer, handlers) };
    }
    if (choices === null) {
      return null;
    }
    switch (key) {
      case "indoor":
        return {
          title: t("heat.settings.indoor"),
          body: multiEditor(doc, language, { intro: t("heat.settings.indoorHelp"), options: choices.indoor.map(option), checked: config.indoor_sensors }, (values) =>
            this.write({ indoor_sensors: values }),
          handlers),
        };
      case "outdoor":
        return pick(
          t("heat.settings.outdoor"),
          [{ value: "", label: t("heat.settings.outdoorAuto"), help: t("heat.settings.outdoorHelp") }, ...choices.outdoor.map(option)],
          config.outdoor_sensor,
          (value) => this.write({ outdoor_sensor: value }),
        );
      case "meter":
        return choices.meter === null
          ? null
          : pick(t("heat.settings.meter"), [none, ...choices.meter.map(option)], config.heat_meter_entity, (value) => this.write({ heat_meter_entity: value }), t("heat.settings.meterHelp"));
      case "path":
        return pick(
          t("heat.settings.path"),
          ["none", "relay", "sg_ready", "offset", "setpoint"].map((path) => ({ value: path, label: t(`heat.path.${path}` as TranslationKey) })),
          config.control_path,
          (value) => this.write({ control_path: value ?? "none" }),
          t("heat.settings.pathHelp"),
        );
      case "control":
      case "controlB": {
        const group = config.control_path === "offset" ? choices.offset : config.control_path === "setpoint" ? choices.setpoint : choices.relay;
        const field = key === "control" ? "control_entity" : "control_entity_b";
        return pick(t(key === "control" ? "heat.settings.control" : "heat.settings.controlB"), [none, ...group.map(option)], config[field], (value) =>
          this.write({ control_path: config.control_path, [field]: value }),
        );
      }
      case "hotWater":
        return pick(t("heat.settings.hotWater"), [none, ...choices.hot_water.map(option)], config.hot_water_entity, (value) => this.write({ hot_water_entity: value }), t("heat.settings.hotWaterHelp"));
      case "zones": {
        const kindWord = (kind: string | undefined): string => t(kind === "air" ? "heat.zone.air" : "heat.zone.floor");
        return {
          title: t("heat.settings.zones"),
          body: multiEditor(
            doc,
            language,
            { intro: t("heat.settings.zonesHelp"), options: choices.zones.map((choice) => ({ value: choice.entity_id, label: `${choice.name} · ${kindWord(choice.kind)}` })), checked: config.zones.map((zone) => zone.entity_id) },
            (values) => this.write({ zones: values.map((entity_id) => ({ entity_id, offset_c: config.zones.find((zone) => zone.entity_id === entity_id)?.offset_c ?? 0 })) }),
            handlers,
          ),
        };
      }
      default: {
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
              return await this.write({ zones: config.zones.map((item) => (item.entity_id === entityId ? { ...item, offset_c: offset } : item)) });
            },
            handlers,
          ),
        };
      }
    }
  }

  private field(doc: Document, label: string, input: HTMLInputElement): HTMLElement {
    const wrap = doc.createElement("label");
    wrap.className = X.editorField;
    wrap.append(doc.createTextNode(label), input);
    return wrap;
  }

  private awayEditor(answer: HeatAnswer, handlers: EditorHandlers): HTMLFormElement {
    const doc = this.ownerDocument;
    const language = this.cardLanguage();
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
    editor.body.append(help, this.field(doc, translate(language, "heat.away.date"), date), this.field(doc, translate(language, "heat.away.time"), time));
    editor.submit(async () => {
      const instant = localToInstant(date.value, time.value, answer.time_zone);
      if (instant === null) {
        return translate(language, "heat.error.generic");
      }
      if (instant <= Date.now()) {
        return translate(language, "heat.error.past");
      }
      return await this.write({ mode: "away", away_until: new Date(instant).toISOString() });
    });
    return editor.form;
  }

  private nightEditor(answer: HeatAnswer, handlers: EditorHandlers): HTMLFormElement {
    const doc = this.ownerDocument;
    const language = this.cardLanguage();
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
      this.field(doc, translate(language, "heat.settings.nightDelta"), delta),
      this.field(doc, translate(language, "heat.settings.nightStart"), startInput),
      this.field(doc, translate(language, "heat.settings.nightEnd"), endInput),
    );
    editor.submit(async () => {
      const value = Number(delta.value.replace(",", "."));
      if (!Number.isFinite(value) || value < 0.5 || value > 3) {
        return translate(language, "heat.error.range");
      }
      if (Math.abs(value * 2 - Math.round(value * 2)) > 1e-9) {
        return translate(language, "heat.error.step");
      }
      return await this.write({ night: { on: on.checked, delta_c: value, start: startInput.value, end: endInput.value } });
    });
    return editor.form;
  }

  private render(): void {
    if (this.config === null) {
      return;
    }
    const doc = this.ownerDocument;
    const language = this.cardLanguage();
    this.mount.replaceChildren(
      heatBody({
        doc,
        language,
        state: this.state,
        ui: this.ui,
        width: this.width,
        nowMs: Date.now(),
        handlers: {
          onStep: (delta) => this.step(delta),
          onHome: () => {
            if (this.answer()?.config.mode === "home") {
              return;
            }
            void this.write({ mode: "home" }).then((error) => {
              this.ui = { ...this.ui, error };
              this.render();
            });
          },
          onAway: () => this.showEditor("away", false),
          onSettings: () => this.showSettings(),
        },
      }),
    );
  }
}
