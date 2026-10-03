// The card element: a standards-based custom element, no Home Assistant frontend internals.
//
// - Decoding: the WebSocket answer stays `unknown` until `decodeDashboard()`, whose verdict is the only
//   thing that becomes a model. Unreadable answers are `malformed` (nothing rendered); a different
//   `api_version` is `unsupported`.
// - Refresh: a fetch happens on connect, config change, the bounded timer, the visible retry and as an
//   action's confirmation, never from the `hass` setter. Each attempt is stamped with purpose, generation,
//   charger id and a monotonic attempt number; only the newest started attempt may update the card.
//   Concurrent attempts for the same generation and charger coalesce; the visible retry forces a new one.
// - `hass` is a state snapshot, not a lifecycle event; only a changed `language` rerenders locally.
// - Disconnect stops the timer, bumps the generation, forgets the in-flight attempt and destroys the view.
// - Actions: one request per click, only for an action and choice the rendered snapshot offered and only
//   when `can_act`. Nothing is rendered optimistically. While an action is in flight, ordinary reads are
//   deferred; a success ends in one confirmation read started after it. A failed confirmation keeps the
//   confirmed dashboard and shows one sentence. Answers stamped with an old generation, charger or attempt
//   are inert.
// - The element owns one `CardView | null`, destroyed before replacement, before error/loading states and
//   on disconnect. Chart sizing lives in the view.

import {
  SpotnavApiError,
  UNSUPPORTED_API_VERSION,
  getDashboard,
  getEntityConfig,
  getSessions,
  getSessionsCsv,
  getMarketOptions,
  getSettings,
  listChargers,
  performAction,
  setVehicleSoc,
  updateEntityConfig,
  updateSettings,
  updateSiteSettings,
  updateVehicle,
} from "./api";
import {
  brandMark,
  createCardView,
  type ActionId,
  type CardView,
  type FailureSentence,
} from "./card-view";
import {
  ENTITY_CONFLICT,
  ENTITY_INVALID_VALUE,
  ENTITY_NOT_ADMIN,
  decodeEntityAnswer,
  decodeVehicleAnswer,
  entityChange,
  entityErrorKey,
  vehicleSocChanges,
  type EntityConfig,
  type EntityDraft,
  type EntityFieldError,
  type EntityScope,
} from "./entity-config";
import { saveTextFile } from "./download";
import { ensureHaSelector } from "./entity-editor";
import { decodeCsv, decodeSessions, exportDates, type HistoryRange, type SessionsAnswer } from "./history";
import {
  SETTINGS_EDITOR_KINDS,
  decodeSettingsAnswer,
  departureDays,
  formFromRecord,
  manualEnergyReadOnly,
  checkCapacity,
  checkConsumption,
  replacementFor,
  settingsErrorKey,
  strategyReplacement,
  vehicleReplacement,
  type ReplacementCheck,
  type CurrentRange,
  type DepartureDays,
  type SettingsEditorKind,
  type SettingsFormValues,
} from "./settings";
import {
  activeControlChange,
  activeControlNotice,
  decodeSiteSettingsAnswer,
  siteSettingsErrorKey,
  solarSettingsChange,
  SITE_SETTINGS_CONFLICT,
} from "./site-settings";
import { resolveLanguage, translate, type Language, type TranslationKey } from "./i18n";
import {
  decodeMarketOptions,
  marketEditable,
  marketFormFor,
  marketReplacement,
  marketStateKey,
  switchArea,
  type MarketDrafts,
  type MarketFormValues,
} from "./market";
import {
  DEFAULT_CURRENT_RANGE,
  actionErrorKey,
  admittedAction,
  buildModel,
  currentRangeFor,
  socFor,
  vehiclesFor,
  siteFactsFor,
  type SiteFacts,
} from "./model";
import { SOLAR_FORECAST_PREFIX, SOLAR_PRIORITY_KEY } from "./solar-editor";
import {
  API_VERSION,
  CARD_EDITOR_ELEMENT,
  CARD_TYPE,
  SETTINGS_NOT_COMMITTED,
  SETTINGS_RECONCILE_FAILED,
  SETTINGS_REVISION_CONFLICT,
  type HomeAssistantLike,
  type MarketOptionsV1,
  type SettingsRecord,
} from "./types";
import { decodeDashboard, type Dashboard, type Soc, type Vehicle } from "./validate";
import { parseCardConfig, type CardConfig } from "./view";
import { VISUAL_CLASSES, VISUAL_STYLES } from "./visual-styles";

/** One bounded refresh cycle; the retry action exists because a timer alone is not a promise. */
export const REFRESH_INTERVAL_MS = 30_000;

/**
 * Deterministic per-instance id prefixes (module-local counter): two cards on one dashboard get distinct
 * ARIA and dialog ids. Never derived from user-controlled text and never random.
 */
let cardInstanceCounter = 0;

const CHARGER_REFUSALS = [
  "spotnav_unknown_charger",
  "spotnav_charger_unloaded",
  "spotnav_site_not_charger",
];

/**
 * Why a read was started, which decides what a failure may do to the card. An `ordinary` read owns the
 * whole card (its failures are the card's error states). A `confirm` read follows an accepted action or
 * save and may only add a newer snapshot; its failure leaves the confirmed card and shows one sentence.
 */
type RefreshPurpose = "ordinary" | "confirm";

interface ConfirmNotice {
  sentenceKey: TranslationKey;
  target: "action" | "settings";
}

const ACTION_CONFIRM_NOTICE: ConfirmNotice = {
  sentenceKey: "action.error.confirmationFailed",
  target: "action",
};

const SETTINGS_CONFIRM_NOTICE: ConfirmNotice = {
  sentenceKey: "settings.error.confirmationFailed",
  target: "settings",
};

type CardState =
  | { kind: "unconfigured" }
  | { kind: "loading" }
  | { kind: "ready"; dashboard: Dashboard }
  | { kind: "unsupported" }
  | { kind: "malformed" }
  | { kind: "charger-missing" }
  | { kind: "failed" };

/** How long the picker's stub waits for the charger list before settling for an empty card. */
const STUB_TIMEOUT_MS = 3000;

const STATE_KEYS = {
  unconfigured: "state.unconfigured",
  loading: "state.loading",
  unsupported: "state.unsupported",
  malformed: "state.malformed",
  "charger-missing": "state.chargerMissing",
  failed: "state.requestFailed",
} as const;

export class SpotnavCard extends HTMLElement {
  private root: ShadowRoot;
  private hassObject: HomeAssistantLike | null = null;
  private config: CardConfig | null = null;
  private cardState: CardState = { kind: "unconfigured" };
  private timer: number | null = null;
  private connected = false;
  private assigned = false;
  private generation = 0;
  private attempt = 0;
  private language: Language | null = null;
  private view: CardView | null = null;
  private actionInFlight: { generation: number; attempt: number; action: ActionId } | null = null;
  private editor: {
    kind: SettingsEditorKind;
    record: SettingsRecord;
    conflict: SettingsRecord | null;
    generation: number;
    operation: number;
    saving: boolean;
  } | null = null;
  /**
   * The open area/fiscal editor: the accepted record and catalogue context (always adopted together, as
   * they arrive in one operation), the per-area drafts, and its own operation. `values` is the draft for
   * the area currently shown.
   */
  private marketEditor: {
    record: SettingsRecord;
    options: MarketOptionsV1;
    drafts: MarketDrafts;
    values: MarketFormValues;
    conflict: SettingsRecord | null;
    generation: number;
    operation: number;
    saving: boolean;
  } | null = null;
  private editorOperation = 0;
  private strategyOperation = 0;
  private siteOperation = 0;
  private activeControlBusy = false;
  private entityConfig: EntityConfig | null = null;
  private entityOperation = 0;
  private historyOperation = 0;
  private history: SessionsAnswer | null = null;
  private entitySaving = false;
  private reopenOverview = false;
  private confirmReadFailed = false;
  private deferredRefresh = false;
  private readonly idPrefix: string;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    cardInstanceCounter += 1;
    this.idPrefix = `spotnav-card-${cardInstanceCounter}`;
  }

  setConfig(config: unknown): void {
    this.config = parseCardConfig(config);
    this.generation += 1;
    this.attempt += 1;
    this.inFlight = null;
    this.actionInFlight = null;
    this.deferredRefresh = false;
    this.marketEditor = null;
    this.entityConfig = null;
    this.entityOperation += 1;
    this.historyOperation += 1;
    this.history = null;
    this.entitySaving = false;
    this.closeEditor();
    this.cardState = this.config.charger === "" ? { kind: "unconfigured" } : { kind: "loading" };
    this.render({ force: true });
    this.startWork();
  }

  set hass(hass: HomeAssistantLike) {
    const nextLanguage = resolveLanguage(hass.language);
    const changed = this.language !== null && this.language !== nextLanguage;
    this.hassObject = hass;
    this.view?.setEntityHass(hass);
    this.language = nextLanguage;
    if (!this.assigned) {
      this.assigned = true;
      this.startWork();
      return;
    }
    if (changed) {
      this.renderLocal();
    }
  }

  get hass(): HomeAssistantLike | null {
    return this.hassObject;
  }

  connectedCallback(): void {
    this.connected = true;
    this.startWork();
  }

  disconnectedCallback(): void {
    this.connected = false;
    this.stopTimer();
    this.generation += 1;
    this.attempt += 1;
    this.inFlight = null;
    this.actionInFlight = null;
    this.deferredRefresh = false;
    this.editor = null;
    this.marketEditor = null;
    this.entityOperation += 1;
    this.historyOperation += 1;
    this.entitySaving = false;
    this.editorOperation += 1;
    this.releaseView();
  }

  getCardSize(): number {
    return 3;
  }

  getGridOptions(): { rows: "auto"; columns: "full" } {
    return { rows: "auto", columns: "full" };
  }

  static getConfigElement(): HTMLElement {
    return document.createElement(CARD_EDITOR_ELEMENT);
  }

  /**
   * What the card picker adds: the one charger there is, so the preview shows the real card, else
   * an unconfigured card. Bounded: a slow or failing backend gives the unconfigured one too.
   */
  static async getStubConfig(hass?: HomeAssistantLike): Promise<CardConfig> {
    const stub: CardConfig = { type: `custom:${CARD_TYPE}`, charger: "" };
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
      if (list !== null && list.chargers.length === 1) {
        return { ...stub, charger: list.chargers[0]!.charger_id };
      }
    } catch {
      // Any failure is an unconfigured card, never an error in the picker.
    } finally {
      clearTimeout(timer);
    }
    return stub;
  }

  private inFlight: {
    generation: number;
    charger: string;
    attempt: number;
    forced: boolean;
    promise: Promise<void>;
  } | null = null;

  private startWork(): void {
    if (!this.connected || this.hassObject === null) {
      return;
    }
    const config = this.config;
    if (config === null || config.charger === "") {
      return;
    }
    this.startTimer();
    void this.refresh();
  }

  private startTimer(): void {
    if (this.timer !== null) {
      return;
    }
    this.timer = window.setInterval(() => void this.refresh(), REFRESH_INTERVAL_MS);
  }

  private stopTimer(): void {
    if (this.timer !== null) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
  }

  /**
   * Coalesced, generation-, attempt- and purpose-guarded refresh.
   *
   * While an action is in flight it owns the read lifecycle: an ordinary read is deferred to one read that
   * runs when the action finishes, so it cannot become the newest attempt and supersede the action's
   * confirmation. Timer and connect paths coalesce into an in-flight attempt. The visible Retry and a
   * confirmation read force a fresh attempt (Retry may click in the window where `inFlight` has not yet
   * cleared; a confirmation must be newer than its action). A forced refresh coalesces into an in-flight
   * forced one.
   */
  refresh(
    options: { force?: boolean; purpose?: RefreshPurpose; confirm?: ConfirmNotice } = {},
  ): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return Promise.resolve();
    }
    const purpose = options.purpose ?? "ordinary";
    const confirm = options.confirm ?? ACTION_CONFIRM_NOTICE;
    if (purpose === "confirm") {
      this.confirmReadFailed = false;
    }
    if (purpose === "ordinary" && this.actionInFlight !== null) {
      this.deferredRefresh = true;
      return Promise.resolve();
    }
    const current = this.inFlight;
    const sameAttempt =
      current !== null && current.generation === this.generation && current.charger === config.charger;
    const fresh = options.force === true || purpose === "confirm";
    if (current !== null && sameAttempt && (!fresh || current.forced)) {
      return current.promise;
    }
    const generation = this.generation;
    const charger = config.charger;
    this.attempt += 1;
    const attempt = this.attempt;
    const forced = options.force === true;
    const promise = this.load(hass, charger, generation, attempt, purpose, confirm).finally(() => {
      if (this.inFlight !== null && this.inFlight.promise === promise) {
        this.inFlight = null;
      }
    });
    this.inFlight = { generation, charger, attempt, forced, promise };
    return promise;
  }

  /**
   * One action, one request, one confirmed answer. The guards run synchronously before the first await:
   * a disabled attribute only takes effect on the next paint, so this is what stops a double click.
   */
  async performAction(action: ActionId, choice: string | null = null): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    const state = this.cardState;
    if (state.kind !== "ready") {
      return;
    }
    const control = state.dashboard.control;
    if (!control.can_act) {
      return;
    }
    // // The request must be admitted by the axis that owns it (`admittedAction`), so a programmatic call
    // // cannot ask for something the rendered snapshot never offered.
    if (!admittedAction(control, action, choice)) {
      return;
    }
    if (this.actionInFlight !== null) {
      return;
    }
    const generation = this.generation;
    const attempt = this.attempt;
    let confirmed = false;
    this.actionInFlight = { generation, attempt, action };
    this.view?.setActionError(null);
    this.view?.setActionPending(true, action, choice);
    try {
      const result = await performAction(hass, config.charger, action, choice);
      if (!this.actionAnswerIsCurrent(generation, attempt)) {
        return;
      }
      if (result.ok) {
        confirmed = true;
        this.deferredRefresh = false;
        await this.refresh({ purpose: "confirm" });
        return;
      }
      this.view?.setActionError({ sentenceKey: actionErrorKey(result.error), code: result.error });
    } catch (error) {
      if (!this.actionAnswerIsCurrent(generation, attempt)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.view?.setActionError({ sentenceKey: actionErrorKey(code), code });
    } finally {
      if (this.actionInFlight !== null && this.actionInFlight.generation === generation) {
        this.actionInFlight = null;
        const deferred = this.deferredRefresh;
        this.deferredRefresh = false;
        this.view?.setActionPending(false);
        if (deferred && !confirmed) {
          void this.refresh({ purpose: "ordinary" });
        }
      }
    }
  }

  private actionAnswerIsCurrent(generation: number, attempt: number): boolean {
    return this.connected && generation === this.generation && attempt === this.attempt;
  }

  private showFailure(confirm: ConfirmNotice, code: string | null): void {
    const failure: FailureSentence = { sentenceKey: confirm.sentenceKey, code };
    if (confirm.target === "settings") {
      this.view?.setSettingsError(failure);
      return;
    }
    this.view?.setActionError(failure);
  }

  private get isAdmin(): boolean {
    return this.hassObject?.user?.is_admin === true;
  }

  private editorIsCurrent(generation: number, operation: number): boolean {
    return this.connected && generation === this.generation && operation === this.editorOperation;
  }

  private showEditorNotice(kind: SettingsEditorKind, sentenceKey: TranslationKey, code: string | null): void {
    const view = this.view;
    if (view === null || view.settingsEditorOpen() !== kind) {
      return;
    }
    view.setSettingsEditorNotice({ sentenceKey, code });
  }

  /**
   * Open one editor from one `get_settings` read, never from the dashboard's reduced section. A
   * non-administrator gets a read-only form; the backend's `require_admin` is the security boundary.
   */
  private async openSettings(kind: SettingsEditorKind): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    this.editor = null;
    this.view?.openSettingsEditor(kind);
    try {
      const raw: unknown = await getSettings(hass, config.charger);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showEditorNotice(
          kind,
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.read",
          null,
        );
        return;
      }
      const answer = decoded.value;
      if (answer.settings === null) {
        this.showEditorNotice(
          kind,
          answer.ok ? "settings.error.read" : settingsErrorKey(answer.code),
          answer.ok ? null : answer.code,
        );
        return;
      }
      const record = answer.settings;
      this.editor = { kind, record, conflict: null, operation, generation, saving: false };
      this.view?.showSettingsEditorForm({
        kind,
        readOnly: !this.isAdmin,
        values: formFromRecord(record),
        energyReadOnly: manualEnergyReadOnly(record),
        phases: record.phases,
        currentRange: this.currentRange(),
        conflict: null,
        soc: this.socFacts(),
        vehicles: this.vehicleFacts(),
        days: this.departureDays(),
      });
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showEditorNotice(kind, settingsErrorKey(code), code);
    }
  }

  /**
   * Read the record and the catalogue context together (parallel) and adopt them only as a pair: the
   * guard is checked once after both settle, so a late half is inert. If either half is unreadable the
   * whole pair is refused, each with its own sentence.
   */
  private async readMarketPair(
    hass: HomeAssistantLike,
    charger: string,
    generation: number,
    operation: number,
  ): Promise<
    | { ok: true; record: SettingsRecord; options: MarketOptionsV1 }
    | { ok: false; sentenceKey: TranslationKey; code: string | null }
  > {
    const [settingsOutcome, marketOutcome] = await Promise.allSettled([
      getSettings(hass, charger),
      getMarketOptions(hass, charger),
    ]);
    if (!this.editorIsCurrent(generation, operation)) {
      return { ok: false, sentenceKey: "settings.error.generic", code: null };
    }
    if (settingsOutcome.status === "rejected") {
      const code = settingsOutcome.reason instanceof SpotnavApiError ? settingsOutcome.reason.code : null;
      return { ok: false, sentenceKey: settingsErrorKey(code), code };
    }
    const decodedSettings = decodeSettingsAnswer(settingsOutcome.value);
    if (!decodedSettings.ok) {
      return {
        ok: false,
        sentenceKey: decodedSettings.failure === "unsupported" ? "settings.error.version" : "settings.error.read",
        code: null,
      };
    }
    const settings = decodedSettings.value;
    if (settings.settings === null) {
      return {
        ok: false,
        sentenceKey: settings.ok ? "settings.error.read" : settingsErrorKey(settings.code),
        code: settings.ok ? null : settings.code,
      };
    }
    if (marketOutcome.status === "rejected") {
      const code = marketOutcome.reason instanceof SpotnavApiError ? marketOutcome.reason.code : null;
      return { ok: false, sentenceKey: settingsErrorKey(code), code };
    }
    const decodedMarket = decodeMarketOptions(marketOutcome.value);
    if (!decodedMarket.ok) {
      return {
        ok: false,
        sentenceKey: decodedMarket.failure === "unsupported" ? "market.error.version" : "market.error.read",
        code: null,
      };
    }
    return { ok: true, record: settings.settings, options: decodedMarket.value };
  }

  /**
   * Open the area/fiscal editor: two reads, and a form only once both are accepted. Non-administrators
   * get the form without Save; `control.can_act` is not consulted because it says nothing about settings
   * permission.
   */
  private async openMarket(): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    this.marketEditor = null;
    this.view?.openMarketEditor();
    const pair = await this.readMarketPair(hass, config.charger, generation, operation);
    if (!pair.ok) {
      if (this.editorIsCurrent(generation, operation)) {
        this.showMarketNotice(pair.sentenceKey, pair.code);
      }
      return;
    }
    this.marketEditor = {
      record: pair.record,
      options: pair.options,
      drafts: {},
      values: marketFormFor(pair.record, pair.record.area_id),
      conflict: null,
      generation,
      operation,
      saving: false,
    };
    this.showMarketForm();
  }

  private showMarketForm(): void {
    const editor = this.marketEditor;
    if (editor === null || !this.connected) {
      return;
    }
    this.view?.showMarketEditorForm({
      readOnly: !marketEditable(editor.options.state, this.isAdmin),
      options: editor.options,
      values: editor.values,
      conflict: editor.conflict === null ? null : editor.conflict.revision,
    });
  }

  private switchMarketArea(areaId: string | null, live: MarketFormValues): void {
    const editor = this.marketEditor;
    if (editor === null || !this.connected) {
      return;
    }
    const switched = switchArea(editor.record, editor.drafts, live, areaId);
    editor.drafts = switched.drafts;
    editor.values = switched.values;
    this.showMarketForm();
  }

  private async reloadMarket(): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.marketEditor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.saving) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    this.view?.setMarketEditorPending(true);
    try {
      const pair = await this.readMarketPair(hass, config.charger, generation, operation);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      if (!pair.ok) {
        this.showMarketNotice(pair.sentenceKey, pair.code);
        return;
      }
      editor.record = pair.record;
      editor.options = pair.options;
      editor.drafts = {};
      editor.values = marketFormFor(pair.record, pair.record.area_id);
      editor.conflict = null;
      this.showMarketForm();
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showMarketNotice(settingsErrorKey(code), code);
    } finally {
      if (this.marketEditor !== null && this.marketEditor.operation === operation) {
        this.marketEditor.saving = false;
        this.view?.setMarketEditorPending(false);
      }
    }
  }

  /**
   * One market Save: a single full replacement under compare-and-set. The body (`marketReplacement`)
   * states only `area_id` and the selected area's row; everything else travels as the accepted record
   * holds it. A Save that would state nothing sends nothing. Price subscriptions and the graph move only
   * after the confirmation read.
   */
  private async saveMarket(values: MarketFormValues, reapply = false): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.marketEditor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.saving) {
      return;
    }
    if (!this.isAdmin) {
      this.showMarketNotice("settings.error.readOnly", null);
      return;
    }
    if (!marketEditable(editor.options.state, this.isAdmin)) {
      const sentence = marketStateKey(editor.options.state, editor.options.reason);
      if (sentence !== null) {
        this.showMarketNotice(sentence, null);
      }
      return;
    }
    const base = reapply ? editor.conflict : editor.record;
    if (base === null) {
      return;
    }
    const check = marketReplacement(base, values, editor.options);
    if (!check.ok) {
      this.showMarketNotice(check.errorKey, null);
      return;
    }
    if (!check.changed) {
      this.closeEditor();
      this.view?.openSettingsOverview();
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    editor.values = values;
    this.view?.setMarketEditorPending(true);
    try {
      const raw: unknown = await updateSettings(hass, config.charger, base.revision, check.body);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showMarketNotice(
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          null,
        );
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        await this.adoptThenOverview(answer.settings, null);
        return;
      }
      if (answer.code === SETTINGS_REVISION_CONFLICT && answer.settings !== null) {
        editor.conflict = answer.settings;
        this.view?.showMarketEditorConflict(answer.settings.revision);
        return;
      }
      if (answer.code === SETTINGS_RECONCILE_FAILED) {
        if (answer.settings !== null) {
          await this.adoptThenOverview(answer.settings, {
            sentenceKey: "settings.error.reconcileFailed",
            code: answer.code,
          });
          return;
        }
        this.showMarketNotice("settings.error.reconcileFailed", answer.code);
        return;
      }
      if (answer.code === SETTINGS_NOT_COMMITTED) {
        this.showMarketNotice("settings.error.notCommitted", answer.code);
        return;
      }
      this.showMarketNotice(settingsErrorKey(answer.code), answer.code);
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showMarketNotice(settingsErrorKey(code), code);
    } finally {
      if (this.marketEditor !== null && this.marketEditor.operation === operation) {
        this.marketEditor.saving = false;
        this.view?.setMarketEditorPending(false);
      }
    }
  }

  private async adoptThenOverview(record: SettingsRecord, notice: FailureSentence | null): Promise<void> {
    this.reopenOverview = true;
    await this.adoptSettings(record, notice, "market");
    if (this.reopenOverview) {
      this.reopenOverview = false;
      if (this.connected && this.view !== null && !this.view.anyDialogOpen()) {
        this.view.openSettingsOverview();
      }
    }
  }

  private showMarketNotice(sentenceKey: TranslationKey, code: string | null): void {
    const view = this.view;
    if (view === null || !view.marketEditorOpen()) {
      return;
    }
    view.setMarketEditorNotice({ sentenceKey, code });
  }

  /**
   * One Save: local judgement, then at most one full replacement under compare-and-set, built from the
   * accepted record (`replacementFor`). A Save that changes nothing writes nothing, since it would still
   * cost a revision and a reconcile.
   */
  private currentRange(): CurrentRange {
    return this.cardState.kind === "ready"
      ? currentRangeFor(this.cardState.dashboard)
      : DEFAULT_CURRENT_RANGE;
  }

  /** What the departure date picker offers now, in the market's own zone; `null` while that is unknown. */
  private departureDays(): DepartureDays | null {
    return this.cardState.kind === "ready"
      ? departureDays(this.cardState.dashboard.market?.timezone ?? "", Date.now())
      : null;
  }

  private socFacts(): Soc | null {
    return this.cardState.kind === "ready" ? socFor(this.cardState.dashboard) : null;
  }

  private siteFacts(): SiteFacts | null {
    return this.cardState.kind === "ready" ? siteFactsFor(this.cardState.dashboard.site, this.languageOrFallback) : null;
  }

  private vehicleFacts(): readonly Vehicle[] {
    return this.cardState.kind === "ready" ? vehiclesFor(this.cardState.dashboard) : [];
  }

  private async saveSettings(
    kind: SettingsEditorKind,
    values: SettingsFormValues,
    reapply = false,
  ): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.editor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.kind !== kind || editor.saving) {
      return;
    }
    if (!this.isAdmin) {
      this.showEditorNotice(kind, "settings.error.readOnly", null);
      return;
    }
    const base = reapply ? editor.conflict : editor.record;
    if (base === null) {
      return;
    }
    const check = replacementFor(
      kind,
      base,
      values,
      this.currentRange(),
      reapply ? editor.record : null,
      this.departureDays(),
    );
    if (!check.ok) {
      this.showEditorNotice(kind, check.errorKey, null);
      return;
    }
    if (!check.changed) {
      this.closeEditor();
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    this.view?.setSettingsEditorPending(true);
    try {
      const raw: unknown = await updateSettings(hass, config.charger, base.revision, check.body);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showEditorNotice(
          kind,
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          null,
        );
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        await this.adoptSettings(answer.settings, null);
        return;
      }
      if (answer.code === SETTINGS_REVISION_CONFLICT && answer.settings !== null) {
        editor.conflict = answer.settings;
        this.view?.showSettingsEditorConflict(answer.settings.revision, answer.settings.phases);
        return;
      }
      if (answer.code === SETTINGS_RECONCILE_FAILED) {
        if (answer.settings !== null) {
          await this.adoptSettings(answer.settings, {
            sentenceKey: "settings.error.reconcileFailed",
            code: answer.code,
          });
          return;
        }
        this.showEditorNotice(kind, "settings.error.reconcileFailed", answer.code);
        return;
      }
      if (answer.code === SETTINGS_NOT_COMMITTED) {
        this.showEditorNotice(kind, "settings.error.notCommitted", answer.code);
        return;
      }
      this.showEditorNotice(kind, settingsErrorKey(answer.code), answer.code);
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showEditorNotice(kind, settingsErrorKey(code), code);
    } finally {
      if (this.editor !== null && this.editor.operation === operation) {
        this.editor.saving = false;
        this.view?.setSettingsEditorPending(false);
      }
    }
  }

  private async reloadSettings(kind: SettingsEditorKind): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const editor = this.editor;
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    if (editor === null || editor.kind !== kind || editor.saving) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.editorOperation;
    editor.operation = operation;
    editor.saving = true;
    this.view?.setSettingsEditorPending(true);
    try {
      const raw: unknown = await getSettings(hass, config.charger);
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.showEditorNotice(
          kind,
          decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.read",
          null,
        );
        return;
      }
      const answer = decoded.value;
      if (answer.settings === null) {
        this.showEditorNotice(
          kind,
          answer.ok ? "settings.error.read" : settingsErrorKey(answer.code),
          answer.ok ? null : answer.code,
        );
        return;
      }
      const record = answer.settings;
      editor.record = record;
      editor.conflict = null;
      this.view?.showSettingsEditorForm({
        kind,
        readOnly: !this.isAdmin,
        values: formFromRecord(record),
        energyReadOnly: manualEnergyReadOnly(record),
        phases: record.phases,
        currentRange: this.currentRange(),
        conflict: null,
        soc: this.socFacts(),
        vehicles: this.vehicleFacts(),
        days: this.departureDays(),
      });
    } catch (error) {
      if (!this.editorIsCurrent(generation, operation)) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.showEditorNotice(kind, settingsErrorKey(code), code);
    } finally {
      if (this.editor !== null && this.editor.operation === operation) {
        this.editor.saving = false;
        this.view?.setSettingsEditorPending(false);
      }
    }
  }

  /**
   * Adopt a committed record, close the dialog, then confirm with one forced dashboard read. Nothing
   * proposed is rendered before that read is accepted; if it fails the reader is told the state could not
   * be confirmed.
   */
  private async adoptSettings(
    record: SettingsRecord,
    notice: FailureSentence | null,
    editor: "settings" | "market" = "settings",
  ): Promise<void> {
    this.editorOperation += 1;
    this.editor = null;
    this.marketEditor = null;
    if (editor === "market") {
      this.view?.closeMarketEditor();
    } else {
      this.view?.closeSettingsEditor();
    }
    this.view?.setSettingsError(notice);
    this.confirmReadFailed = false;
    await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
    if (notice !== null && this.connected && !this.confirmReadFailed) {
      this.view?.setSettingsError(notice);
    }
  }

  /**
   * Switch strategy: one settings write with no dialog or confirmation prompt. The view already refuses
   * unavailable, unwritable or already-selected rows, so this is a defensive re-check.
   *
   * The record is read fresh because a full replacement needs its current revision, which the dashboard's
   * `strategy` block does not carry (see `settings.ts`). A refusal or conflict leaves the confirmed
   * strategy as shown and is reported through the row-level sentence.
   */
  private async selectStrategy(strategyId: string): Promise<void> {
    await this.writeFreshSettings((record) => strategyReplacement(record, strategyId));
  }

  /**
   * Choose the vehicle the charger plans for: the same dialog-free write as the strategy, changing only
   * `target.vehicle_id` of a freshly read record under its revision.
   */
  private async selectVehicle(vehicleId: string): Promise<void> {
    await this.writeFreshSettings((record) => vehicleReplacement(record, vehicleId));
  }

  private async writeFreshSettings(build: (record: SettingsRecord) => ReplacementCheck): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "" || !this.isAdmin) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.strategyOperation;
    const stale = (): boolean =>
      generation !== this.generation || operation !== this.strategyOperation || !this.connected;
    try {
      const rawRecord: unknown = await getSettings(hass, config.charger);
      if (stale()) {
        return;
      }
      const decodedRecord = decodeSettingsAnswer(rawRecord);
      if (!decodedRecord.ok || decodedRecord.value.settings === null) {
        this.view?.setSettingsError({
          sentenceKey:
            decodedRecord.ok && !decodedRecord.value.ok
              ? settingsErrorKey(decodedRecord.value.code)
              : "settings.error.read",
          code: decodedRecord.ok && !decodedRecord.value.ok ? decodedRecord.value.code : null,
        });
        return;
      }
      const record = decodedRecord.value.settings;
      const check = build(record);
      if (!check.ok) {
        this.view?.setSettingsError({ sentenceKey: check.errorKey, code: null });
        return;
      }
      if (!check.changed) {
        return;
      }
      const raw: unknown = await updateSettings(hass, config.charger, record.revision, check.body);
      if (stale()) {
        return;
      }
      const decoded = decodeSettingsAnswer(raw);
      if (!decoded.ok) {
        this.view?.setSettingsError({
          sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          code: null,
        });
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        await this.adoptSettings(answer.settings, null);
        return;
      }
      if (answer.code === SETTINGS_RECONCILE_FAILED && answer.settings !== null) {
        await this.adoptSettings(answer.settings, {
          sentenceKey: "settings.error.reconcileFailed",
          code: answer.code,
        });
        return;
      }
      this.view?.setSettingsError({ sentenceKey: settingsErrorKey(answer.code), code: answer.code });
    } catch (error) {
      if (stale()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.view?.setSettingsError({ sentenceKey: settingsErrorKey(code), code });
    }
  }

  /**
   * The History dialog was opened: read the charge history (any signed-in user may), and answer the open
   * view. A newer open, a reconfiguration or a disconnect makes an older answer inert.
   */
  private async loadHistory(): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null) {
      return;
    }
    const generation = this.generation;
    const operation = ++this.historyOperation;
    const current = (): boolean =>
      this.connected && generation === this.generation && operation === this.historyOperation && this.view === view;
    try {
      const decoded = decodeSessions(await getSessions(hass, config.charger));
      if (!current()) {
        return;
      }
      if (!decoded.ok) {
        view.setHistoryState({
          kind: "failed",
          sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "history.failed",
          code: null,
        });
        return;
      }
      this.history = decoded.value;
      view.setHistoryState({ kind: "ready", answer: decoded.value });
    } catch (error) {
      if (!current()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      view.setHistoryState({ kind: "failed", sentenceKey: "history.failed", code });
    }
  }

  /** Export CSV: one request for the chosen period, then the file is saved; failure is one sentence. */
  private async exportHistory(range: HistoryRange): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    const answer = this.history;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null || answer === null) {
      return;
    }
    const generation = this.generation;
    const operation = this.historyOperation;
    const current = (): boolean =>
      this.connected && generation === this.generation && operation === this.historyOperation && this.view === view;
    view.setHistoryExport(null, true);
    try {
      const file = decodeCsv(await getSessionsCsv(hass, config.charger, exportDates(range, answer)));
      if (!current()) {
        return;
      }
      if (file === null) {
        view.setHistoryExport("history.exportFailed", false);
        return;
      }
      saveTextFile(this.ownerDocument, file.filename, file.csv);
      view.setHistoryExport(null, false);
    } catch {
      if (current()) {
        view.setHistoryExport("history.exportFailed", false);
      }
    }
  }

  private async loadEntityConfig(): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityState({ kind: "adminOnly" });
      return;
    }
    const generation = this.generation;
    const operation = ++this.entityOperation;
    const current = (): boolean =>
      this.connected && generation === this.generation && operation === this.entityOperation && this.view === view;
    view.setEntityState(
      this.entityConfig === null ? { kind: "loading" } : { kind: "ready", config: this.entityConfig },
    );
    try {
      const [raw] = await Promise.all([
        getEntityConfig(hass, config.charger) as Promise<unknown>,
        ensureHaSelector(this.ownerDocument?.defaultView),
      ]);
      if (!current()) {
        return;
      }
      const decoded = decodeEntityAnswer(raw);
      if (!decoded.ok) {
        view.setEntityState({
          kind: "failed",
          failure: {
            sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "entity.error.read",
            code: null,
          },
        });
        return;
      }
      const answer = decoded.value;
      if (answer.ok) {
        this.entityConfig = answer.config;
        view.setEntityState({ kind: "ready", config: answer.config });
        return;
      }
      view.setEntityState(
        answer.code === ENTITY_NOT_ADMIN
          ? { kind: "adminOnly" }
          : { kind: "failed", failure: { sentenceKey: "entity.error.read", code: answer.code } },
      );
    } catch (error) {
      if (!current()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      view.setEntityState({ kind: "failed", failure: { sentenceKey: "entity.error.read", code } });
    }
  }

  private openEntityEditor(scope: EntityScope): void {
    if (!this.isAdmin) {
      return;
    }
    const open = (): void => {
      const config = this.entityConfig;
      if (config === null) {
        return;
      }
      this.entityOperation += 1;
      this.view?.openEntityEditor(scope, config);
    };
    if (this.entityConfig !== null) {
      open();
      return;
    }
    // The strategy dialog links here before Settings has ever been opened, so nothing was read yet.
    void this.loadEntityConfig().then(open);
  }

  /**
   * One Save of one group: only changed fields, `expected` from the configuration last read, one request,
   * nothing shown as saved until the backend answers. Success adopts the returned config, closes the
   * editor, reads the dashboard once and reopens Settings. A conflict adopts the returned config and
   * redraws the editor. A refused field is marked on the field; other refusals are one general message.
   */
  private async saveEntities(scope: EntityScope, draft: EntityDraft): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    const read = this.entityConfig;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null || read === null) {
      return;
    }
    if (this.entitySaving) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityEditorNotice({ sentenceKey: "entity.error.notAdmin", code: null });
      return;
    }
    const change = entityChange(read, scope, draft);
    if (!change.ok) {
      view.markEntityFieldErrors(change.errors);
      view.setEntityEditorNotice({ sentenceKey: entityErrorKey(ENTITY_INVALID_VALUE), code: null });
      return;
    }
    view.markEntityFieldErrors([]);
    view.setEntityEditorNotice(null);
    if (!change.changed) {
      view.closeEntityEditor();
      view.openSettingsOverview();
      return;
    }
    const generation = this.generation;
    this.entityOperation += 1;
    this.entitySaving = true;
    view.setEntityEditorPending(true);
    const open = (): boolean =>
      this.connected && generation === this.generation && this.view === view && view.entityEditorOpen() === scope;
    try {
      const raw: unknown = await updateEntityConfig(hass, config.charger, change.request);
      if (generation !== this.generation || !this.connected) {
        return;
      }
      const decoded = decodeEntityAnswer(raw);
      if (!decoded.ok) {
        if (open()) {
          view.setEntityEditorNotice({
            sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "entity.error.generic",
            code: null,
          });
        }
        return;
      }
      const answer = decoded.value;
      if (answer.config !== null) {
        this.entityConfig = answer.config;
      }
      if (answer.ok) {
        await this.returnToSettingsAfterEntitySave(view, answer.config);
        return;
      }
      if (!open()) {
        return;
      }
      const notice = { sentenceKey: entityErrorKey(answer.code), code: answer.code };
      if (answer.code === "spotnav_conflict" && answer.config !== null) {
        view.openEntityEditor(scope, answer.config, notice);
        return;
      }
      view.markEntityFieldErrors(answer.fieldErrors);
      view.setEntityEditorNotice(notice);
    } catch (error) {
      if (open()) {
        const code = error instanceof SpotnavApiError ? error.code : null;
        view.setEntityEditorNotice({ sentenceKey: "entity.error.generic", code });
      }
    } finally {
      this.entitySaving = false;
      if (this.view === view && view.entityEditorOpen() === scope) {
        view.setEntityEditorPending(false);
      }
    }
  }

  private async returnToSettingsAfterEntitySave(view: CardView, config: EntityConfig | null): Promise<void> {
    this.reopenOverview = true;
    view.closeEntityEditor();
    if (config !== null) {
      view.setEntityState({ kind: "ready", config });
    }
    this.confirmReadFailed = false;
    await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
    if (this.reopenOverview) {
      this.reopenOverview = false;
      if (this.connected && this.view !== null && !this.view.anyDialogOpen()) {
        this.view.openSettingsOverview();
      }
    }
  }

  private openVehicleEditor(vehicleId: string): void {
    const listed = this.vehicleFacts().some((row) => row.id === vehicleId);
    if (!this.isAdmin || !(listed || this.entityConfig?.vehicles.some((entry) => entry.id === vehicleId) === true)) {
      return;
    }
    this.entityOperation += 1;
    this.view?.openVehicleEditor(vehicleId, this.entityConfig);
  }

  /**
   * One Save of one vehicle's dialog: the typed capacity and consumption are judged first (a refused field
   * is marked and nothing is sent), then a changed charge-level sensor is chosen
   * (`spotnav/choose_vehicle_soc`) and changed properties are written in one `spotnav/update_vehicle`
   * under compare-and-set. Never optimistic. Success closes the dialog, reads the dashboard once and
   * returns to Settings; a refusal stays in the dialog with its sentence.
   */
  private async saveVehicle(vehicleId: string, draft: EntityDraft): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null) {
      return;
    }
    if (this.entitySaving) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityEditorNotice({ sentenceKey: "entity.error.notAdmin", code: null });
      return;
    }
    const row = this.vehicleFacts().find((entry) => entry.id === vehicleId);
    view.markEntityFieldErrors([]);
    view.setEntityEditorNotice(null);

    const errors: EntityFieldError[] = [];
    const changes: { capacity_kwh?: number; consumption_kwh_per_10km?: number } = {};
    const expected: { capacity_kwh?: number | null; consumption_kwh_per_10km?: number | null } = {};
    const judge = (
      text: string | undefined,
      check: (text: string) => { ok: true; value: number } | { ok: false },
      field: "capacity_kwh" | "consumption_kwh_per_10km",
      code: string,
      current: number | null,
    ): void => {
      if (text === undefined) {
        return;
      }
      const checked = check(text);
      if (!checked.ok) {
        errors.push({ field, code });
        return;
      }
      const value = Math.round(checked.value * 10) / 10;
      if (value !== current) {
        changes[field] = value;
        expected[field] = current;
      }
    };
    judge(draft["capacity"], checkCapacity, "capacity_kwh", "invalid_capacity", row?.capacity_kwh ?? null);
    judge(
      draft["consumption"],
      checkConsumption,
      "consumption_kwh_per_10km",
      "invalid_consumption",
      row?.consumption_kwh_per_10km ?? null,
    );
    if (errors.length > 0) {
      view.markEntityFieldErrors(errors);
      return;
    }
    const read = this.entityConfig;
    const socRequests =
      read === null || draft["soc"] === undefined
        ? []
        : vehicleSocChanges(read.vehicles, { [vehicleId]: draft["soc"] }).filter((request) => request.vehicleId === vehicleId);
    const propertiesChanged = Object.keys(changes).length > 0;
    if (socRequests.length === 0 && !propertiesChanged) {
      view.closeEntityEditor();
      view.openSettingsOverview();
      return;
    }

    const generation = this.generation;
    this.entityOperation += 1;
    this.entitySaving = true;
    view.setEntityEditorPending(true);
    const open = (): boolean =>
      this.connected && generation === this.generation && this.view === view && view.vehicleEditorOpen() === vehicleId;
    const refuse = (sentenceKey: TranslationKey, code: string | null, fieldErrors: readonly EntityFieldError[] = []): void => {
      if (open()) {
        view.markEntityFieldErrors(fieldErrors);
        view.setEntityEditorNotice({ sentenceKey, code });
      }
    };
    const generic = (failure: "unsupported" | "malformed"): TranslationKey =>
      failure === "unsupported" ? "settings.error.version" : "entity.error.generic";
    try {
      let latest: EntityConfig | null = read;
      for (const request of socRequests) {
        const decoded = decodeEntityAnswer(await setVehicleSoc(hass, config.charger, request));
        if (generation !== this.generation || !this.connected) {
          return;
        }
        if (!decoded.ok) {
          refuse(generic(decoded.failure), null);
          return;
        }
        const answer = decoded.value;
        if (answer.config !== null) {
          this.entityConfig = answer.config;
          latest = answer.config;
        }
        if (!answer.ok) {
          const notice = { sentenceKey: entityErrorKey(answer.code), code: answer.code };
          if (answer.config !== null && answer.fieldErrors.some((error) => error.code === "unknown_vehicle")) {
            if (open()) {
              view.openVehicleEditor(vehicleId, answer.config, notice);
            }
            return;
          }
          refuse(notice.sentenceKey, notice.code, answer.fieldErrors);
          return;
        }
      }
      if (propertiesChanged) {
        const decoded = decodeVehicleAnswer(await updateVehicle(hass, config.charger, { vehicleId, changes, expected }));
        if (generation !== this.generation || !this.connected) {
          return;
        }
        if (!decoded.ok) {
          refuse(generic(decoded.failure), null);
          return;
        }
        const answer = decoded.value;
        if (answer.config !== null) {
          this.entityConfig = answer.config;
          latest = answer.config;
        }
        if (!answer.ok) {
          if (answer.code === ENTITY_CONFLICT && answer.config !== null) {
            if (open()) {
              view.openVehicleEditor(
                vehicleId,
                answer.config,
                { sentenceKey: entityErrorKey(answer.code), code: answer.code },
                answer.vehicle ?? undefined,
              );
              await this.confirmVehicleWrite();
            }
            return;
          }
          const fieldErrors = answer.fieldErrors.map((error) => ({
            field: error.field,
            code: error.code,
          }));
          refuse(
            fieldErrors.length > 0 && answer.fieldErrors.every((error) => error.code !== "unknown_vehicle")
              ? "entity.error.invalid"
              : answer.fieldErrors.some((error) => error.code === "unknown_vehicle")
                ? "entity.error.field.unknownVehicle"
                : entityErrorKey(answer.code),
            answer.code,
            fieldErrors,
          );
          return;
        }
      }
      await this.returnToSettingsAfterEntitySave(view, latest);
    } catch (error) {
      if (open()) {
        const code = error instanceof SpotnavApiError ? error.code : null;
        view.setEntityEditorNotice({ sentenceKey: "entity.error.generic", code });
      }
    } finally {
      this.entitySaving = false;
      if (this.view === view && view.vehicleEditorOpen() === vehicleId) {
        view.setEntityEditorPending(false);
      }
    }
  }

  private async confirmVehicleWrite(): Promise<void> {
    this.confirmReadFailed = false;
    await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
  }

  private openSolarEditor(): void {
    if (!this.isAdmin || this.siteFacts() === null) {
      return;
    }
    this.view?.openSolarEditor();
  }

  /**
   * The Solar dialog's Save (`spotnav/update_site_settings`, admin only) under compare-and-set: only the
   * changed fields travel. Success closes the dialog, reads the dashboard once and returns to Settings. A
   * conflict adopts the current values by the same route and says so on the row outside the popover; other
   * refusals stay in the dialog with their sentence.
   */
  private async saveSolar(draft: EntityDraft): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    const site = this.siteFacts();
    if (!this.connected || hass === null || config === null || config.charger === "" || view === null || site === null) {
      return;
    }
    if (this.entitySaving) {
      return;
    }
    if (!this.isAdmin) {
      view.setEntityEditorNotice({ sentenceKey: "settings.error.readOnly", code: null });
      return;
    }
    const forecast = site.solarForecastChoices
      .filter((choice) => draft[`${SOLAR_FORECAST_PREFIX}${choice.id}`] === "true")
      .map((choice) => choice.id);
    const request = solarSettingsChange(
      {
        priority: site.solarPriority,
        forecast: site.solarForecastChoices.filter((choice) => choice.selected).map((choice) => choice.id),
      },
      { priority: draft[SOLAR_PRIORITY_KEY] ?? site.solarPriority, forecast },
    );
    if (request === null) {
      view.closeEntityEditor();
      view.openSettingsOverview();
      return;
    }
    const generation = this.generation;
    const operation = ++this.siteOperation;
    this.entitySaving = true;
    view.setEntityEditorNotice(null);
    view.setEntityEditorPending(true);
    const open = (): boolean =>
      this.connected && generation === this.generation && this.view === view && view.solarEditorOpen();
    const refuse = (sentenceKey: TranslationKey, code: string | null): void => {
      if (open()) {
        view.setEntityEditorNotice({ sentenceKey, code });
      }
    };
    try {
      const decoded = decodeSiteSettingsAnswer(await updateSiteSettings(hass, config.charger, request));
      if (generation !== this.generation || operation !== this.siteOperation || !this.connected) {
        return;
      }
      if (!decoded.ok) {
        refuse(decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic", null);
        return;
      }
      const answer = decoded.value;
      if (answer.ok || answer.code === SITE_SETTINGS_CONFLICT) {
        await this.returnToSettingsAfterEntitySave(view, null);
        if (!answer.ok && this.connected && !this.confirmReadFailed) {
          this.view?.setSettingsError({ sentenceKey: siteSettingsErrorKey(answer.code), code: answer.code });
        }
        return;
      }
      refuse(siteSettingsErrorKey(answer.code), answer.code);
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      refuse(siteSettingsErrorKey(code), code);
    } finally {
      this.entitySaving = false;
      if (this.view === view && view.solarEditorOpen()) {
        view.setEntityEditorPending(false);
      }
    }
  }

  /**
   * The active load-balancing switch: one admin-only write, never optimistic. The popover adopts the
   * returned site block and says what happened; a failed restore on disable is worded as "the charger may
   * still be limited". The dashboard is re-read afterwards; while the popover is open that render is
   * deferred, so the answer repaints the switch in place.
   */
  private async setActiveControl(confirmed: boolean, chosen: boolean): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const view = this.view;
    if (
      !this.connected ||
      view === null ||
      hass === null ||
      config === null ||
      config.charger === "" ||
      !this.isAdmin ||
      this.activeControlBusy
    ) {
      return;
    }
    this.activeControlBusy = true;
    view.setActiveControlPending(true);
    const generation = this.generation;
    const operation = ++this.siteOperation;
    const language = this.languageOrFallback;
    const stale = (): boolean =>
      generation !== this.generation || operation !== this.siteOperation || !this.connected;
    // // What a reader who closed the popover mid-call is still told, on the row outside it.
    let fallback: FailureSentence | null = null;
    try {
      const raw: unknown = await updateSiteSettings(hass, config.charger, activeControlChange(confirmed, chosen));
      if (stale()) {
        return;
      }
      const decoded = decodeSiteSettingsAnswer(raw);
      if (!decoded.ok) {
        fallback = {
          sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic",
          code: null,
        };
        view.adoptActiveControl(null, { tone: "warning", lines: [translate(language, fallback.sentenceKey)], code: null });
        return;
      }
      const answer = decoded.value;
      const notice = activeControlNotice(language, answer, !chosen);
      if (answer.restore?.outcome === "failed") {
        fallback = { sentenceKey: "site.activeControl.restore.failed.unknown", code: null };
      } else if (!answer.ok) {
        fallback = { sentenceKey: siteSettingsErrorKey(answer.code), code: answer.code };
      }
      view.adoptActiveControl(answer.site === null ? null : siteFactsFor(answer.site, language), notice);
      if (answer.site !== null) {
        this.confirmReadFailed = false;
        await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
        if (this.view !== view && this.connected && !this.confirmReadFailed && fallback !== null) {
          this.view?.setSettingsError(fallback);
        }
      }
    } catch (error) {
      if (stale()) {
        return;
      }
      const code = error instanceof SpotnavApiError ? error.code : null;
      const key = siteSettingsErrorKey(code);
      view.adoptActiveControl(null, { tone: "warning", lines: [translate(language, key)], code });
    } finally {
      this.activeControlBusy = false;
      if (this.view === view) {
        view.setActiveControlPending(false);
      }
    }
  }

  private cancelMarket(): boolean {
    if (this.marketEditor?.saving === true) {
      return false;
    }
    this.editorOperation += 1;
    this.marketEditor = null;
    return this.connected;
  }

  private closeEditor(): void {
    this.editorOperation += 1;
    this.editor = null;
    this.marketEditor = null;
    this.view?.closeSettingsEditor();
    this.view?.closeMarketEditor();
  }

  private async readDashboard(hass: HomeAssistantLike, charger: string): Promise<unknown> {
    return await getDashboard(hass, charger, API_VERSION);
  }

  private async load(
    hass: HomeAssistantLike,
    charger: string,
    generation: number,
    attempt: number,
    purpose: RefreshPurpose,
    confirm: ConfirmNotice = ACTION_CONFIRM_NOTICE,
  ): Promise<void> {
    let next: CardState;
    let code: string | null = null;
    try {
      const answer: unknown = await this.readDashboard(hass, charger);
      const decoded = decodeDashboard(answer);
      next = decoded.ok ? { kind: "ready", dashboard: decoded.value } : { kind: decoded.failure };
    } catch (error) {
      code = error instanceof SpotnavApiError ? error.code : null;
      next = this.stateFromError(error);
    }
    if (!this.accepts(charger, generation, attempt)) {
      return; // A late answer for a charger, a configuration or an attempt that is no longer current.
    }
    if (purpose === "confirm" && next.kind !== "ready") {
      // // The envelope was accepted but the state could not be read back: keep the confirmed dashboard and
      // // graph, make the trigger usable again and show `confirmationFailed`. It is not `reconcileFailed`,
      // // which is reserved for the backend's own `spotnav_action_reconcile_failed` result -- a failed read
      // // says nothing about the plan.
      this.confirmReadFailed = true;
      this.showFailure(confirm, code);
      return;
    }
    this.cardState = next;
    this.render();
  }

  private accepts(charger: string, generation: number, attempt: number): boolean {
    return (
      this.connected &&
      this.config?.charger === charger &&
      generation === this.generation &&
      attempt === this.attempt
    );
  }

  private stateFromError(error: unknown): CardState {
    const code = error instanceof SpotnavApiError ? error.code : null;
    if (code === UNSUPPORTED_API_VERSION) {
      return { kind: "unsupported" };
    }
    if (code !== null && CHARGER_REFUSALS.includes(code)) {
      return { kind: "charger-missing" };
    }
    return { kind: "failed" };
  }

  private releaseView(): void {
    const view = this.view;
    this.view = null;
    view?.destroy();
  }

  private get languageOrFallback(): Language {
    return this.language ?? resolveLanguage(null);
  }

  private renderLocal(): void {
    this.render();
  }

  private renderPending = false;

  /**
   * One render: destroy the previous view, then mount the accepted view or show a quiet state. Everything
   * is built as nodes with `textContent`; no backend, config or exception value is put into markup.
   *
   * A refresh must never close or steal focus from an open dialog or an input being edited: while the
   * current view has a dialog open, the render is deferred and applied from `onDialogsClosed`. `force` is
   * for a config change only, where a new charger identity has no dialog worth preserving.
   */
  private render(options: { force?: boolean } = {}): void {
    if (options.force !== true && this.view !== null && this.view.anyDialogOpen()) {
      this.renderPending = true;
      return;
    }
    this.renderPending = false;
    this.releaseView();
    const doc = this.ownerDocument;
    const language = this.languageOrFallback;
    const style = doc.createElement("style");
    style.textContent = VISUAL_STYLES;
    const host = doc.createElement("div");
    host.className = VISUAL_CLASSES.shell;
    this.root.replaceChildren(style, host);

    if (this.cardState.kind === "ready") {
      this.view = createCardView({
        model: buildModel({
          dashboard: this.cardState.dashboard,
          language,
          nowMs: Date.now(),
        }),
        mount: host,
        idPrefix: this.idPrefix,
        onAction: (action, choice) => {
          void this.performAction(action, choice);
        },
        onOpenSettings: (kind) => {
          void this.openSettings(kind);
        },
        onSaveSettings: (kind, values) => {
          void this.saveSettings(kind, values);
        },
        onReloadSettings: (kind) => {
          void this.reloadSettings(kind);
        },
        onReapplySettings: (kind, values) => {
          void this.saveSettings(kind, values, true);
        },
        onOpenMarket: () => {
          void this.openMarket();
        },
        onSaveMarket: (values) => {
          void this.saveMarket(values);
        },
        onReloadMarket: () => {
          void this.reloadMarket();
        },
        onReapplyMarket: (values) => {
          void this.saveMarket(values, true);
        },
        onMarketAreaChange: (areaId, live) => {
          this.switchMarketArea(areaId, live);
        },
        isAdmin: this.isAdmin,
        onSelectVehicle: (vehicleId) => {
          void this.selectVehicle(vehicleId);
        },
        onSelectStrategy: (strategyId) => {
          void this.selectStrategy(strategyId);
        },
        onOpenSolarEditor: () => {
          this.openSolarEditor();
        },
        onSaveSolar: (draft) => {
          void this.saveSolar(draft);
        },
        onCancelSolar: () => !this.entitySaving,
        onSetActiveControl: (confirmed, chosen) => {
          void this.setActiveControl(confirmed, chosen);
        },
        onCancelMarket: () => this.cancelMarket(),
        hass: () => this.hassObject,
        onOpenHistory: () => {
          void this.loadHistory();
        },
        onExportHistory: (range) => {
          void this.exportHistory(range);
        },
        onSettingsOverviewOpened: () => {
          void this.loadEntityConfig();
        },
        onOpenEntityEditor: (scope) => {
          this.openEntityEditor(scope);
        },
        onSaveEntities: (scope, draft) => {
          void this.saveEntities(scope, draft);
        },
        onCancelEntities: () => !this.entitySaving,
        onOpenVehicleEditor: (vehicleId) => {
          this.openVehicleEditor(vehicleId);
        },
        onSaveVehicle: (vehicleId, draft) => {
          void this.saveVehicle(vehicleId, draft);
        },
        onDialogsClosed: () => {
          if (this.renderPending) {
            this.render();
          }
        },
      });
      if (this.reopenOverview) {
        this.reopenOverview = false;
        this.view.openSettingsOverview();
      }
      return;
    }

    const state = this.cardState.kind;
    if (state === "unconfigured") {
      // A card with no charger yet still looks like SpotNav in the picker's preview.
      const identity = doc.createElement("div");
      identity.className = VISUAL_CLASSES.header;
      identity.append(brandMark(doc, this.idPrefix));
      host.append(identity);
    }
    const sentence = doc.createElement("p");
    sentence.className = VISUAL_CLASSES.muted;
    sentence.textContent = translate(language, STATE_KEYS[state]);
    host.append(sentence);
    if (state === "charger-missing" || state === "failed") {
      const retry = doc.createElement("button");
      retry.type = "button";
      retry.className = VISUAL_CLASSES.iconButton;
      retry.textContent = translate(language, "state.retry");
      retry.setAttribute("aria-label", translate(language, "state.retry"));
      retry.addEventListener("click", () => {
        if (this.cardState.kind === "charger-missing" || this.cardState.kind === "failed") {
          this.cardState = { kind: "loading" };
          this.render();
        }
        void this.refresh({ force: true });
      });
      host.append(retry);
    }
  }
}
