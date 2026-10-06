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
  cameraCommand,
  chooseVehicleIdentification,
  getDashboard,
  getCardInfo,
  getDebugBundle,
  getEntityConfig,
  getSessions,
  getSessionsCsv,
  findRegion,
  getMarketOptions,
  getSettings,
  identifyVehicle,
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
  type CameraPictureRequest,
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
  fieldErrorKey,
  type EntityConfig,
  type EntityDraft,
  type EntityScope,
} from "./entity-config";
import { decodeDebugAnswer, saveDebugBundle } from "./debug-download";
import { cardOutdated, clientBlock, decodeCardInfo, ownCardBundleHash, servedHashFromBundle } from "./card-identity";
import { saveTextFile } from "./download";
import { ensureHaSelector } from "./entity-editor";
import type { FiscalComponentName, ValueWrite } from "./value-writes";
import { cameraErrorKey, decodePicture, type CameraPicture } from "./camera-editor";
import { decodeCsv, decodeSessions, type SessionsAnswer } from "./history";
import {
  SETTINGS_EDITOR_KINDS,
  decodeSettingsAnswer,
  departureDays,
  formFromRecord,
  manualEnergyReadOnly,
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
} from "./site-settings";
import { resolveLanguage, translate, type Language, type TranslationKey } from "./i18n";
import {
  decodeMarketOptions,
  fiscalValues,
  marketEditable,
  marketFormFor,
  marketReplacement,
  marketSuggestion,
  type MarketReplacementCheck,
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
  actionPending,
  type ControlFacts,
  type SentAction,
  type SiteFacts,
} from "./model";
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
import { OUTCOME_POLL_MS, outcomeSettled, outcomeWatchFor, type OutcomeWatch } from "./charge-bar";
import { parseCardConfig, type CardConfig } from "./view";
import { browserStore, initialChartCollapsed, readChartCollapsed, writeChartCollapsed } from "./chart-preference";
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
  /** After a Start or Stop: the outcome awaited, and the quicker reads until it shows (`charge-bar.ts`). */
  private outcomeWatch: OutcomeWatch | null = null;
  /**
   * The Start or Stop this card last sent, kept until a read shows no `action_pending`, so the
   * Charging cell names what is under way; `null` after a pause or resume.
   */
  private sentAction: SentAction | null = null;
  /** The automatic action last shown, so a pending action keeps the Schedule cell's caption. */
  private shownAutomatic: string | null = null;
  private outcomeTimer: number | null = null;
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
  /** Reference picture thumbnails fetched or being fetched, by car, kind and when the picture was taken. */
  private readonly thumbnails = new Map<string, Promise<{ picture: CameraPicture } | { code: string | null }>>();
  private entityOperation = 0;
  private historyOperation = 0;
  private history: SessionsAnswer | null = null;
  private entitySaving = false;
  private reopenOverview = false;
  /** A warning a value's save leaves for the Settings page it returns to (the plan was not updated). */
  private overviewWarning: { sentenceKey: TranslationKey; code: string | null } | null = null;
  private confirmReadFailed = false;
  private deferredRefresh = false;
  private readonly idPrefix: string;
  /**
   * The chart choice made in this card since its config was set: it holds across the view rebuilt on
   * every refresh even where the browser cannot store it. `null` defers to storage, then the YAML.
   */
  private chartCollapsed: boolean | null = null;

  constructor() {
    super();
    this.root = this.attachShadow({ mode: "open" });
    cardInstanceCounter += 1;
    this.idPrefix = `spotnav-card-${cardInstanceCounter}`;
  }

  setConfig(config: unknown): void {
    this.stopOutcomeWatch();
    this.config = parseCardConfig(config);
    this.chartCollapsed = null;
    this.generation += 1;
    this.attempt += 1;
    this.inFlight = null;
    this.actionInFlight = null;
    this.sentAction = null;
    this.shownAutomatic = null;
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
    this.sentAction = null;
    this.shownAutomatic = null;
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
    this.stopOutcomeWatch();
  }

  /**
   * While a Start or Stop has not shown yet, read every `OUTCOME_POLL_MS`; once `live.charging` reads as
   * asked, or after `OUTCOME_WINDOW_MS`, only the ordinary cycle remains.
   */
  private followOutcome(): void {
    const watch = this.outcomeWatch;
    if (watch === null) {
      return;
    }
    const dashboard = this.cardState.kind === "ready" ? this.cardState.dashboard : null;
    if (!this.connected || outcomeSettled(watch, dashboard, Date.now())) {
      this.stopOutcomeWatch();
      return;
    }
    if (this.outcomeTimer === null) {
      this.outcomeTimer = window.setInterval(() => {
        const current = this.outcomeWatch;
        if (current === null || outcomeSettled(current, null, Date.now())) {
          this.stopOutcomeWatch();
          return;
        }
        void this.refresh();
      }, OUTCOME_POLL_MS);
    }
  }

  private stopOutcomeWatch(): void {
    this.outcomeWatch = null;
    if (this.outcomeTimer !== null) {
      window.clearInterval(this.outcomeTimer);
      this.outcomeTimer = null;
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
    const previousSent = this.sentAction;
    this.sentAction = (action === "start" || action === "stop") && choice === null ? action : null;
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
        this.stopOutcomeWatch();
        this.outcomeWatch = outcomeWatchFor(action, Date.now());
        await this.refresh({ purpose: "confirm" });
        this.followOutcome();
        return;
      }
      this.sentAction = previousSent;
      this.view?.setActionError({ sentenceKey: actionErrorKey(result.error), code: result.error });
    } catch (error) {
      if (!this.actionAnswerIsCurrent(generation, attempt)) {
        return;
      }
      this.sentAction = previousSent;
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.view?.setActionError({ sentenceKey: actionErrorKey(code), code });
    } finally {
      if (this.actionInFlight !== null && this.actionInFlight.generation === generation) {
        this.actionInFlight = null;
        // The confirming read may already show the outcome: nothing pending, nothing to name.
        if (this.cardState.kind === "ready" && !actionPending(this.cardState.dashboard.control)) {
          this.sentAction = null;
        }
        const deferred = this.deferredRefresh;
        this.deferredRefresh = false;
        this.view?.setActionPending(false);
        if (deferred && !confirmed) {
          void this.refresh({ purpose: "ordinary" });
        }
      }
    }
  }

  /**
   * After a render: a read with nothing pending forgets the command sent, and the Schedule cell's
   * caption is kept only while it is offered or held for a pending action.
   */
  private rememberControls(control: ControlFacts): void {
    // A render while the command is still in flight says nothing of its outcome yet.
    if (control.pendingAction === null && this.actionInFlight === null) {
      this.sentAction = null;
    }
    const automatic = control.automatic.action;
    if (automatic === "pause" || automatic === "resume") {
      this.shownAutomatic = automatic;
    } else if (control.heldAutomatic === null) {
      this.shownAutomatic = null;
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
        fillSupported: record.fill_to_limit !== undefined,
        vehicleTargets: this.vehicleTargets(),
        phases: record.phases,
        limitedBy: this.phasesLimitedBy(),
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

  /** Whether the planned vehicle's onboard charger, not the charger's wiring, sets the phases (the dashboard's say). */
  private phasesLimitedBy(): "vehicle" | null {
    return this.cardState.kind === "ready" ? (this.cardState.dashboard.charging_phases?.limited_by ?? null) : null;
  }

  /** Each car's own target (the same at every charger), from the dashboard's vehicle rows. */
  private vehicleTargets(): Record<string, number> {
    const targets: Record<string, number> = {};
    for (const row of this.vehicleFacts()) {
      if (typeof row.target_percent === "number") {
        targets[row.id] = row.target_percent;
      }
    }
    return targets;
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
        fillSupported: record.fill_to_limit !== undefined,
        vehicleTargets: this.vehicleTargets(),
        phases: record.phases,
        limitedBy: this.phasesLimitedBy(),
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
  /**
   * The banner's answer to "which car is plugged in?" (`spotnav/identify_vehicle`): the backend switches the car
   * when it differs, retires the question on every phone, and the dashboard is read again. A refusal (the
   * question was answered elsewhere meanwhile) is shown by that read, not here.
   */
  private async answerIdentification(vehicleId: string): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    // Any signed-in user may answer: the question goes to the household's phones as well.
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return;
    }
    const generation = this.generation;
    const dashboard = this.cardState.kind === "ready" ? this.cardState.dashboard : null;
    if (dashboard?.connection?.state === "disconnected") {
      // No car plugged in: the choice is the plan's car for the next plug-in.
      await this.selectVehicle(vehicleId);
      return;
    }
    let notPluggedIn = false;
    try {
      const answer = (await identifyVehicle(hass, config.charger, vehicleId)) as { ok?: unknown; error?: unknown } | null;
      notPluggedIn = answer !== null && answer.ok === false && answer.error === "spotnav_not_identifying";
    } catch {
      // Not answered: the question stays until the next read says otherwise.
    }
    if (notPluggedIn && generation === this.generation && this.connected && this.isAdmin) {
      // Unplugged meanwhile: the choice becomes the plan's car, which only an administrator may set. For
      // anyone else the confirming read below shows the line as it now is.
      await this.selectVehicle(vehicleId);
      return;
    }
    if (generation === this.generation && this.connected) {
      await this.refresh({ purpose: "confirm" });
    }
  }

  /** The plan's car, written into the settings (an administrator's write): what Byt bil does with no car plugged in. */
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

  /** Fetch the redacted bundle and save it as a file; any failure is a sentence in the Settings popover. */
  private async downloadDebug(): Promise<void> {
    const hass = this.hassObject;
    const view = this.view;
    const doc = this.ownerDocument;
    if (!this.connected || hass === null || view === null || !this.isAdmin) {
      return;
    }
    const fail = (sentenceKey: TranslationKey, code: string | null): void => {
      if (this.view === view) {
        view.setDebugPending(false);
        view.setOverviewNotice({ sentenceKey, code });
      }
    };
    view.setOverviewNotice(null);
    view.setDebugPending(true);
    let raw: unknown;
    try {
      raw = await getDebugBundle(hass);
    } catch (error) {
      fail("debug.error.failed", error instanceof SpotnavApiError ? error.code : null);
      return;
    }
    const answer = decodeDebugAnswer(raw);
    if (answer === null) {
      fail("settings.error.version", null);
      return;
    }
    if (!answer.ok) {
      fail(answer.code === "spotnav_not_admin" ? "debug.error.notAdmin" : "debug.error.failed", answer.code);
      return;
    }
    // The card running here, next to the backend's own versions: a stale card in a browser or the
    // Companion app is a common cause of "it shows the old thing".
    const served = servedHashFromBundle(answer.bundle);
    const client = clientBlock(doc.defaultView?.navigator?.userAgent ?? null, served);
    if (client.card_outdated !== null && this.view === view) {
      view.setCardOutdated(client.card_outdated);
    }
    if (!saveDebugBundle(doc, { ...answer.bundle, client }, new Date())) {
      fail("debug.error.failed", null);
      return;
    }
    if (this.view === view) {
      view.setDebugPending(false);
    }
  }

  /**
   * The Settings popover was opened: ask which card the integration serves, and say in Support when the
   * card running here is another (an old module kept by the browser or the Companion app). Any failure,
   * an integration without the command included, says nothing.
   */
  private async loadCardInfo(): Promise<void> {
    const hass = this.hassObject;
    const view = this.view;
    if (!this.connected || hass === null || view === null || ownCardBundleHash() === null) {
      return;
    }
    let raw: unknown;
    try {
      raw = await getCardInfo(hass);
    } catch {
      return;
    }
    const decoded = decodeCardInfo(raw);
    const outdated = decoded === null ? null : cardOutdated(ownCardBundleHash(), decoded.cardBundleHash);
    if (outdated !== null && this.view === view) {
      view.setCardOutdated(outdated);
    }
  }

  /**
   * The History dialog was opened: read the charge history (any signed-in user may), and answer the open
   * view. A newer open, a reconfiguration or a disconnect makes an older answer inert.
   */
  private async loadHistory(month: string | null = null): Promise<void> {
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
      const decoded = decodeSessions(await getSessions(hass, config.charger, 20, month));
      if (!current()) {
        return;
      }
      if (!decoded.ok) {
        view.setHistoryState({
          kind: "failed",
          sentenceKey: decoded.failure === "unsupported" ? "settings.error.version" : "history.failed",
          code: null,
          answer: this.history,
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
      view.setHistoryState({ kind: "failed", sentenceKey: "history.failed", code, answer: this.history });
    }
  }

  /** Export CSV: one request for the shown month, then the file is saved; failure is one sentence. */
  private async exportHistory(): Promise<void> {
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
      const file = decodeCsv(await getSessionsCsv(hass, config.charger, answer.month));
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

  /**
   * The one-tap answer to the card's "set its onboard charger to 1-phase?": `update_vehicle` under
   * compare-and-set. Keeping three phases is stored as an answer too, which ends the question. Whatever
   * the answer, the dashboard is read again, so a conflict or a refusal shows the real state.
   */
  private async answerOnboardPhases(vehicleId: string, phases: 1 | 3): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    const row = this.vehicleFacts().find((entry) => entry.id === vehicleId);
    if (!this.connected || hass === null || config === null || config.charger === "" || !this.isAdmin || row === undefined) {
      return;
    }
    const generation = this.generation;
    try {
      await updateVehicle(hass, config.charger, {
        vehicleId,
        changes: { onboard_phases: phases },
        expected: { onboard_phases: row.onboard_phases },
      });
    } catch {
      // Not answered: the question stays until the next read says otherwise.
    }
    if (generation === this.generation && this.connected) {
      await this.refresh({ purpose: "confirm" });
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

  /**
   * One value of the Settings page, written through the same request as the dialog it replaces (see
   * `ValueWrite`). Answers `null` when it took: the editor is closed, the dashboard read once and Settings
   * shown again; otherwise the sentence the editor keeps showing.
   */
  private async writeValue(write: ValueWrite): Promise<string | null> {
    const hass = this.hassObject;
    const config = this.config;
    const language = this.languageOrFallback;
    const say = (key: TranslationKey): string => translate(language, key);
    if (!this.connected || hass === null || config === null || config.charger === "") {
      return say("settings.error.generic");
    }
    if (!this.isAdmin) {
      return say("settings.error.readOnly");
    }
    const generation = this.generation;
    const current = (): boolean => this.connected && generation === this.generation;
    this.overviewWarning = null;
    try {
      const failure = await this.performValueWrite(hass, config.charger, write);
      if (!current()) {
        return null;
      }
      if (failure !== null) {
        return say(failure);
      }
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      return say(settingsErrorKey(code));
    }
    this.view?.closeValueEditor();
    this.reopenOverview = true;
    this.confirmReadFailed = false;
    await this.refresh({ purpose: "confirm", confirm: SETTINGS_CONFIRM_NOTICE });
    if (this.reopenOverview) {
      this.reopenOverview = false;
      if (this.connected && this.view !== null && !this.view.anyDialogOpen()) {
        this.view.openSettingsOverview();
      }
    }
    return null;
  }

  /**
   * A picture for the Settings page: the camera's picture now (for the frame editor), or a car's reference
   * thumbnail (fetched once per picture).
   */
  private async cameraPicture(request: CameraPictureRequest): Promise<{ picture: CameraPicture } | { code: string | null }> {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "" || !this.isAdmin) {
      return { code: null };
    }
    const key = request.kind === "reference" ? `${request.vehicleId}/${request.pictureKind}/${request.takenAt}` : null;
    const cached = key === null ? undefined : this.thumbnails.get(key);
    if (cached !== undefined) {
      return await cached;
    }
    const fetched = (async (): Promise<{ picture: CameraPicture } | { code: string | null }> => {
      try {
        return decodePicture(
          request.kind === "snapshot"
            ? await cameraCommand(hass, config.charger, "camera_snapshot")
            : await cameraCommand(hass, config.charger, "reference_picture", {
                vehicle_id: request.vehicleId,
                kind: request.pictureKind,
              }),
        );
      } catch (error) {
        return { code: error instanceof SpotnavApiError ? error.code : null };
      }
    })();
    if (key !== null) {
      this.thumbnails.set(key, fetched);
      // A failed fetch is not kept: the next paint asks again.
      void fetched.then((answer) => {
        if (!("picture" in answer)) {
          this.thumbnails.delete(key);
        }
      });
    }
    return await fetched;
  }

  /** The request itself: `null` when it took, else the sentence key that says why not. */
  private async performValueWrite(
    hass: HomeAssistantLike,
    charger: string,
    write: ValueWrite,
  ): Promise<TranslationKey | null> {
    const adoptConfig = (config: EntityConfig | null): void => {
      if (config !== null) {
        this.entityConfig = config;
        this.view?.setEntityState({ kind: "ready", config });
      }
    };
    switch (write.kind) {
      case "vehicle": {
        const decoded = decodeVehicleAnswer(
          await updateVehicle(hass, charger, { vehicleId: write.vehicleId, changes: write.changes, expected: write.expected }),
        );
        if (!decoded.ok) {
          return decoded.failure === "unsupported" ? "settings.error.version" : "entity.error.generic";
        }
        adoptConfig(decoded.value.config);
        if (decoded.value.ok) {
          return null;
        }
        if (decoded.value.code === ENTITY_CONFLICT && decoded.value.vehicle !== null) {
          write.onConflict?.(decoded.value.vehicle);
        }
        const errors = decoded.value.fieldErrors;
        return errors.length > 0 && errors[0] !== undefined ? fieldErrorKey(errors[0].code) : entityErrorKey(decoded.value.code);
      }
      case "vehicleSoc": {
        const decoded = decodeEntityAnswer(await setVehicleSoc(hass, charger, { vehicleId: write.vehicleId, entityId: write.entityId }));
        if (!decoded.ok) {
          return decoded.failure === "unsupported" ? "settings.error.version" : "entity.error.generic";
        }
        adoptConfig(decoded.value.config);
        if (decoded.value.ok) {
          return null;
        }
        const refused = decoded.value.fieldErrors[0];
        return refused === undefined ? entityErrorKey(decoded.value.code) : fieldErrorKey(refused.code);
      }
      case "cameraFrame":
      case "reference": {
        const answer = (await (write.kind === "cameraFrame"
          ? cameraCommand(hass, charger, "save_camera_frame", { frame: write.frame })
          : write.action === "delete"
            ? cameraCommand(hass, charger, "delete_reference_picture", { vehicle_id: write.vehicleId, kind: null })
            : cameraCommand(hass, charger, "take_reference_picture", { vehicle_id: write.vehicleId, kind: write.action }))) as {
          ok?: unknown;
          error?: unknown;
        } | null;
        return answer !== null && answer.ok === true
          ? null
          : cameraErrorKey(typeof answer?.error === "string" ? answer.error : null);
      }
      case "vehicleSource": {
        const answer = (await chooseVehicleIdentification(hass, charger, {
          vehicleId: write.vehicleId,
          source: write.source,
          entityId: write.entityId,
        })) as { ok?: unknown; error?: unknown } | null;
        return answer !== null && answer.ok === true
          ? null
          : entityErrorKey(typeof answer?.error === "string" ? answer.error : null);
      }
      case "entity": {
        const read = this.entityConfig;
        if (read === null) {
          return "entity.error.generic";
        }
        const change = entityChange(read, write.scope, write.draft);
        if (!change.ok) {
          const first = change.errors[0];
          return first === undefined ? entityErrorKey(ENTITY_INVALID_VALUE) : fieldErrorKey(first.code);
        }
        if (!change.changed) {
          return null;
        }
        const decoded = decodeEntityAnswer(await updateEntityConfig(hass, charger, change.request));
        if (!decoded.ok) {
          return decoded.failure === "unsupported" ? "settings.error.version" : "entity.error.generic";
        }
        adoptConfig(decoded.value.config);
        if (decoded.value.ok) {
          return null;
        }
        const first = decoded.value.fieldErrors[0];
        return first === undefined ? entityErrorKey(decoded.value.code) : fieldErrorKey(first.code);
      }
      case "solar": {
        const site = this.siteFacts();
        if (site === null) {
          return "settings.error.generic";
        }
        const confirmed = {
          priority: site.solarPriority,
          forecast: site.solarForecastChoices.filter((choice) => choice.selected).map((choice) => choice.id),
        };
        const request = solarSettingsChange(confirmed, {
          priority: write.priority ?? confirmed.priority,
          forecast: write.forecast ?? confirmed.forecast,
        });
        if (request === null) {
          return null;
        }
        const decoded = decodeSiteSettingsAnswer(await updateSiteSettings(hass, charger, request));
        if (!decoded.ok) {
          return decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic";
        }
        return decoded.value.ok ? null : siteSettingsErrorKey(decoded.value.code);
      }
      case "settings":
      case "fiscal": {
        const [settingsRaw, optionsRaw] = await Promise.all([
          getSettings(hass, charger),
          write.kind === "fiscal" ? getMarketOptions(hass, charger) : Promise.resolve(null),
        ]);
        const read = decodeSettingsAnswer(settingsRaw);
        if (!read.ok || read.value.settings === null) {
          return read.ok && !read.value.ok ? settingsErrorKey(read.value.code) : "settings.error.read";
        }
        const record = read.value.settings;
        let check: ReplacementCheck | MarketReplacementCheck;
        if (write.kind === "settings") {
          check = write.build(record);
        } else {
          const options = decodeMarketOptions(optionsRaw);
          if (!options.ok) {
            return options.failure === "unsupported" ? "market.error.version" : "market.error.read";
          }
          check = marketReplacement(record, fiscalValues(record, write.component, write.value), options.value);
        }
        if (!check.ok) {
          return check.errorKey;
        }
        if (!check.changed) {
          return null;
        }
        const decoded = decodeSettingsAnswer(await updateSettings(hass, charger, record.revision, check.body));
        if (!decoded.ok) {
          return decoded.failure === "unsupported" ? "settings.error.version" : "settings.error.generic";
        }
        const answer = decoded.value;
        if (answer.ok) {
          return null;
        }
        if (answer.code === SETTINGS_RECONCILE_FAILED) {
          // Saved, but the plan was not updated: a warning the Settings page says once it is back.
          this.overviewWarning = { sentenceKey: "settings.error.reconcileFailed", code: answer.code };
          return null;
        }
        return settingsErrorKey(answer.code);
      }
    }
  }

  /** A fee's own editor needs the area's suggestion: read the market options, then open it. */
  private async editFiscal(component: FiscalComponentName): Promise<void> {
    const hass = this.hassObject;
    const config = this.config;
    if (!this.connected || hass === null || config === null || config.charger === "" || !this.isAdmin) {
      return;
    }
    const generation = this.generation;
    try {
      const [settingsRaw, optionsRaw] = await Promise.all([getSettings(hass, config.charger), getMarketOptions(hass, config.charger)]);
      if (generation !== this.generation || !this.connected) {
        return;
      }
      const read = decodeSettingsAnswer(settingsRaw);
      const options = decodeMarketOptions(optionsRaw);
      if (!read.ok || read.value.settings === null || !options.ok) {
        this.view?.setSettingsError({ sentenceKey: "settings.error.read", code: null });
        return;
      }
      const record = read.value.settings;
      const row = record.overrides.find((item) => item.area_id === record.area_id) ?? null;
      const stored = row === null ? null : row[component];
      this.view?.openFiscalEditor(component, {
        current: stored !== null && stored.enabled ? (stored.value ?? marketSuggestion(options.value, record.area_id, component)) : null,
        suggestion: marketSuggestion(options.value, record.area_id, component),
      });
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      this.view?.setSettingsError({ sentenceKey: settingsErrorKey(code), code });
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
    this.followOutcome();
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
  /** Collapsed or full: this card's own choice, else this browser's for the charger, else the YAML option. */
  private chartCollapsedNow(): boolean {
    if (this.chartCollapsed !== null) {
      return this.chartCollapsed;
    }
    const config = this.config;
    if (config === null || config.charger === "") {
      return false;
    }
    return initialChartCollapsed(readChartCollapsed(browserStore, config.charger), config.chart);
  }

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
      const model = buildModel({
        dashboard: this.cardState.dashboard,
        language,
        nowMs: Date.now(),
        sentAction: this.sentAction,
        shownAutomatic: this.shownAutomatic,
      });
      this.rememberControls(model.control);
      this.view = createCardView({
        model,
        mount: host,
        idPrefix: this.idPrefix,
        chartCollapsed: this.chartCollapsedNow(),
        onChartCollapsedChange: (collapsed) => {
          this.chartCollapsed = collapsed;
          if (this.config !== null && this.config.charger !== "") {
            writeChartCollapsed(browserStore, this.config.charger, collapsed);
          }
        },
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
        onFindRegion: async (postcode) => {
          const hass = this.hassObject;
          if (hass === null) {
            return { region: null, reason: "unavailable" };
          }
          return await findRegion(hass, postcode);
        },
        isAdmin: this.isAdmin,
        onSelectStrategy: (strategyId) => {
          void this.selectStrategy(strategyId);
        },
        onSetActiveControl: (confirmed, chosen) => {
          void this.setActiveControl(confirmed, chosen);
        },
        onCancelMarket: () => this.cancelMarket(),
        hass: () => this.hassObject,
        onOpenHistory: () => {
          void this.loadHistory();
        },
        onHistoryMonth: (month) => {
          void this.loadHistory(month);
        },
        onExportHistory: () => {
          void this.exportHistory();
        },
        onSettingsOverviewOpened: () => {
          const warning = this.overviewWarning;
          if (warning !== null) {
            this.overviewWarning = null;
            this.view?.setOverviewNotice(warning);
          }
          void this.loadEntityConfig();
          void this.loadCardInfo();
        },
        onOpenEntityEditor: (scope) => {
          this.openEntityEditor(scope);
        },
        onDownloadDebug: () => {
          void this.downloadDebug();
        },
        onSaveEntities: (scope, draft) => {
          void this.saveEntities(scope, draft);
        },
        onCancelEntities: () => !this.entitySaving,
        onAnswerOnboardPhases: (vehicleId, phases) => {
          void this.answerOnboardPhases(vehicleId, phases);
        },
        onAnswerIdentification: (vehicleId) => {
          void this.answerIdentification(vehicleId);
        },
        onWriteValue: (write) => this.writeValue(write),
        onCameraPicture: (request) => this.cameraPicture(request),
        onEditFiscal: (component) => {
          void this.editFiscal(component);
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
