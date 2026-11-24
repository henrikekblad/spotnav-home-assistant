// The card's DOM, built from one immutable `CardModel` and nothing else.
//
// Knows no `hass`, `callWS`, timer or raw payload: everything visible is a model field or a
// translation key. Owns every node it creates (including the chart interaction and the dialogs);
// `destroy()` releases them. No `innerHTML`: names, codes and labels go through `textContent`, so a
// hostile charger name or backend code stays text.
//
// Top to bottom: banner (only when something is wrong), status sentence, the graph with its
// max/min/current overlay (facts the model lacks are omitted, not shown as "unknown"), the action
// bar, and the header's History and Settings actions.

import { NotificationPhones } from "./notification-phones";
import { chartNowAt, nextIntervalBoundary, type ChartAxisMode, type ChartMark, type ChartNow } from "./chart";
import {
  createChartInteraction,
  measureChart,
  FALLBACK_SIZE,
  type ChartInteraction,
  type ChartObserverLike,
  type ChartSize,
} from "./chart-interaction";
import { FILL_FLOOR_CHART_PX, FILL_MIN_CHART_PX, planFill, type FillReading } from "./card-fill";
import { applyFocus, chartHeightForWidth, renderChart, type ChartLabels } from "./chart-render";
import { stripBarPlacement, stripBars, stripNowPosition, stripTicks } from "./chart-strip";
import { createDialog, takeActions, type DialogHandle } from "./dialog";
import { changeCarBody, identificationBanner, methodWords } from "./identification";
import { clock, dayCue, hourLabel, formatNumber, priceAxisLabel, type PriceUnit } from "./format";
import { translate, type TranslationKey } from "./i18n";
import { type CardModel, type DoesNotFitLine, type SiteFacts } from "./model";
import type { ActiveControlNotice } from "./site-settings";
import { type EntityConfig, type EntityDraft, type EntityFieldError, type EntityScope } from "./entity-config";
import { type MarketFormValues } from "./market";
import { type RegionLookup } from "./market-editor";
import type { MarketEditorForm } from "./market-editor";
import { settingsTrigger, type SettingsEditorForm } from "./settings-editor";
import type { PreviewState } from "./plan-preview";
import { GUEST_VEHICLE_ID, type Vehicle } from "./validate";
import {
  multiEditor,
  numberEditor,
  settingRow,
  singleEditor,
  type ChoiceOption,
  type EditorHandlers,
  type NumberEditorInput,
} from "./value-editors";
import type { FiscalComponentName, ValueWrite } from "./value-writes";
import { type CameraPicture, type PictureKind, type ReferenceAnswer } from "./camera-editor";
import { integrationPath } from "./price-provider";
import { priceSectionBody as sharedPriceSection, type SectionHost } from "./price-section";
import { setupLineText, setupSettingsSection, type SetupRowId } from "./setup-checklist";
import { type BlueprintUse } from "./automations";
import { type SendDebug } from "./send-debug";
import { type BetaCall } from "./beta";
import { cardHeader, headerButton, headerStatus, headerTitle } from "./card-header";
import type { HistoryState } from "./history";
import type { PowerState } from "./power-view";
import { connectionLabel, guestLineFor, vehicleLineFor, type VehicleLineFacts } from "./vehicle-line";
import { planSummaryParts, type SettingsEditorKind, type SettingsFormValues } from "./settings";
import { CHART_DENSITY, VISUAL_CLASSES as C } from "./visual-styles";
import type { SiteTarget } from "./api";
import { SiteSections } from "./settings-popover";
import type { HomeAssistantLike } from "./types";
import { element } from "./dom";
import type { SettingsPageState, ViewContext } from "./card-view-context";
import { createDialogOpeners } from "./card-view-dialogs";
import { createSiteSections } from "./card-view-site-sections";
import { createSupportSection } from "./card-view-support";
import { createEntityRowsSection } from "./card-view-entity-rows";
import { createEntityEditorSection } from "./card-view-entity-editor";
import { createHistorySection } from "./card-view-history";
import { createMarketEditorSection } from "./card-view-market-editor";
import { createSettingsEditorSection } from "./card-view-settings-editor";
import {
  BOUNDARY_HORIZON_MS,
  hasComponent,
  navigateTo,
  readoutDisplayText,
  currentPriceText,
  nowSummaryText,
  bannerSeverity,
  bannerRepeatsStatus,
  siteFollowsSolar,
  importTargetShown,
  issueCountText,
  periodBlockOrder,
  periodBlock,
  icon,
  connectionIcon,
  statusFits,
  readStatusFit,
  carButtonElement,
  pauseIcon,
  playIcon,
  stopIcon,
  strategyIcon,
  graphDescription,
  chargeBarElement,
  type StatusFitReading,
} from "./card-view-parts";

export {
  BOUNDARY_HORIZON_MS,
  readoutDay,
  readoutTextFor,
  bannerSeverity,
  bannerRepeatsStatus,
  strategyRowHidden,
  siteFollowsSolar,
  importTargetShown,
  issueCountText,
  periodBlockOrder,
  STATUS_GAP_PX,
  statusFits,
  carButtonElement,
  issueListBody,
  capabilityBody,
  graphDescription,
  chargeBarElement,
} from "./card-view-parts";
export type { StatusFitReading, CarButtonInput } from "./card-view-parts";

/** What the camera's editors ask the card for. */
export type CameraPictureRequest =
  | { kind: "snapshot" }
  | { kind: "reference"; vehicleId: string; pictureKind: "day" | "night"; takenAt: string };

/**
 * Where the reader was on the Settings page when a value's editor (or another dialog) opened over it: the page's
 * scroll position and the row that opened it. Kept by the card across a re-render, so the page comes back there.
 */
export interface OverviewPlace {
  scrollTop: number;
  edit: string | null;
}

export interface OverviewPlaceHolder {
  place: OverviewPlace | null;
  /** The car whose tab is open on the Settings page (with two or more cars). */
  vehicle?: string | null;
}

/** One section of the Settings page: whether anyone may change something in it, and how it is added to the page. */
interface OverviewSection {
  key: string;
  /** A preference every user may change (of this device); otherwise only an administrator changes its values. */
  anyone: boolean;
  append: (body: HTMLElement) => void;
}


export interface CardViewInput {
  model: CardModel;

  mount: ShadowRoot | HTMLElement;
  idPrefix: string;
  /** The clock the keyboard's "current interval" fallback uses; injectable for tests. */
  now?: () => number;
  size?: () => ChartSize | null;
  measure?: (host: HTMLElement) => ChartSize;
  /** The resize observer factory; `undefined` means the platform's own, `null` means none. */
  observe?: (host: HTMLElement, onResize: () => void) => ChartObserverLike | null;
  /**
   * The observer of the card's box, which fits the card to a box of fixed height (see `card-fill.ts`);
   * `undefined` means the platform's own, `null` means none.
   */
  observeBox?: (box: HTMLElement, onResize: () => void) => ChartObserverLike | null;
  /**
   * The observer of the header's name block, which decides whether the charger's status shows as its word or its
   * symbol (`statusFits`); `undefined` means the platform's own, `null` means none.
   */
  observeHeader?: (block: HTMLElement, onResize: () => void) => ChartObserverLike | null;
  /** Reads the header for `statusFits`; injectable for tests, since a test DOM has no layout. */
  readStatusFit?: (row: HTMLElement) => StatusFitReading;
  /** Reads the card's natural layout for the fit; injectable for tests, since a test DOM has no layout. */
  readFill?: (card: HTMLElement) => FillReading;
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (handle: number) => void;
  /** Start with the chart collapsed to its 24-hour strip (the card's remembered or configured choice). */
  chartCollapsed?: boolean;
  /** The `chart_detail` card option: draw the chart at `CHART_DENSITY.detail`, for a card shown enlarged. */
  chartDetail?: boolean;
  /** The price row was pressed and the chart collapsed (`true`) or expanded; the card remembers it. */
  onChartCollapsedChange?: (collapsed: boolean) => void;
  /**
   * The chart's time axis was chosen in Settings (Display): a preference of this browser, which the card
   * stores and applies to every SpotNav card in it. Anyone may choose it, not only an administrator.
   */
  onChartAxisChange?: (mode: ChartAxisMode) => void;
  /** The price unit chosen in Display: stored for this browser, every SpotNav card in it redraws. */
  onPriceUnitChange?: (unit: PriceUnit) => void;
  /** The viewer turned the sun in the price chart on or off (stored for this browser by the card). */
  onChartSunChange?: (on: boolean) => void;
  /**
   * What the row does when a person acts: the card owns the request, the guard and the refresh; the
   * view only reports the click with the backend-supplied choice.
   */
  onAction: (action: ActionId, choice: string | null) => void;
  /**
   * A settings trigger was pressed: the card reads the canonical record and answers with the
   * dialog's state. The view never fetches.
   */
  onOpenSettings: (kind: SettingsEditorKind) => void;
  onSaveSettings: (kind: SettingsEditorKind, values: SettingsFormValues) => void;
  onReloadSettings: (kind: SettingsEditorKind) => void;
  onReapplySettings: (kind: SettingsEditorKind, values: SettingsFormValues) => void;
  /** The editable Plan dialog's draft whenever it may have changed: the card asks what a Save would plan. */
  onPreviewSettings?: (kind: SettingsEditorKind, values: SettingsFormValues) => void;
  /**
   * The market editor trigger: the card reads the record and market context and answers with the
   * form.
   */
  onOpenMarket: () => void;
  onSaveMarket: (values: MarketFormValues) => void;
  onReloadMarket: () => void;
  onReapplyMarket: (values: MarketFormValues) => void;
  /**
   * The reader picked another area in the open form. The card decides which draft that area has and
   * hands back a new form.
   */
  onMarketAreaChange: (areaId: string | null, live: MarketFormValues) => void;
  /** A postcode's Great Britain region, for the market editor's optional "Find my region" field. */
  onFindRegion?: (postcode: string) => Promise<RegionLookup>;
  /**
   * Whether this connection is an administrator. A courtesy only: the backend's admin check on the
   * write is the security boundary; this decides whether a row looks pressable.
   */
  isAdmin: boolean;
  /**
   * An available, non-selected strategy row was pressed. The view has already closed the dialog;
   * the card owns the request and every outcome.
   */
  onSelectStrategy: (strategyId: string) => void;
  /**
   * Active load-balancing switch (admin only): `confirmed` is the last shown opt-in, the other the
   * one just asked for. The control is already back on `confirmed`; the answer arrives through
   * `adoptActiveControl`.
   */
  onSetActiveControl?: (confirmed: boolean, chosen: boolean) => void;
  /**
   * The reader is leaving an area/fiscal editor for the Settings page it was opened from (Cancel,
   * Escape, backdrop or close). Answering `false` means do not open Settings: a save is in flight
   * and its answer returns the reader itself.
   */
  onCancelMarket?: () => boolean | void;
  /**
   * Every dialog this view owns is now closed. Fired at most once per batch of closes (see
   * `notifyDialogsChanged`); the card uses it to apply a refresh it deferred while a dialog was
   * open.
   */
  onDialogsClosed?: () => void;
  /** The Home Assistant object the entity pickers need, read when an editor opens. */
  hass?: () => unknown;
  /**
   * The site the shared battery and heat sections read and write (`settings-popover.ts`), named by this card's
   * charger; without it, or without a site, Settings has no such sections.
   */
  siteTarget?: () => SiteTarget | null;
  onSettingsOverviewOpened?: () => void;
  /**
   * The History button was pressed: the card reads the charge history and answers through
   * `setHistoryState`. The view only shows the dialog (loading) and reports the press.
   */
  onOpenHistory?: () => void;
  /** A month was picked in the History dialog (`YYYY-MM`): the card reads it and answers through `setHistoryState`. */
  onHistoryMonth?: (month: string) => void;
  /** Export CSV was pressed: the card fetches the shown month's file and saves it. */
  onExportHistory?: () => void;
  /**
   * The History dialog's Power view wants a month (`YYYY-MM`, `null` for the current one): the card reads the site's
   * power and answers through `setPowerState`.
   */
  onPowerMonth?: (month: string | null) => void;
  onOpenEntityEditor?: (scope: EntityScope) => void;
  /** The admin pressed "Download debug info" in the Settings popover. */
  onDownloadDebug?: () => void;
  /** The admin sent the debug info from its dialog: the reference, or the sentence to show. */
  onSendDebug?: SendDebug;
  /** The beta box and the hardware test in Support (`beta.ts`), administrators only. */
  onBeta?: BetaCall;
  /** "Disconnect all phones" in Support, confirmed in its dialog (administrators only): `null` when done, else the sentence. */
  onDisconnectPhones?: () => Promise<string | null>;
  /**
   * "Make a home of this" in the site section of a charger whose installation has no home (administrators only),
   * confirmed in its dialog with the main fuse: `null` when made, else the sentence. Offered once the card has said
   * the installation has no home (`setHomeOffered`).
   */
  onMakeHome?: (mainFuseA: number) => Promise<string | null>;
  /** How many automations use each of SpotNav's blueprints (`automations.ts`), `null` when it could not be read. */
  onLoadBlueprintUse?: () => Promise<BlueprintUse | null>;
  onSaveEntities?: (scope: EntityScope, draft: EntityDraft) => void;
  onCancelEntities?: () => boolean | void;
  /** The one-tap answer to "set its onboard charger to 1-phase?": `1` accepts, `3` keeps it as it was. */
  onAnswerOnboardPhases?: (vehicleId: string, phases: 1 | 3) => void;
  /** The one-tap answer to "which car is plugged in?". */
  onAnswerIdentification?: (vehicleId: string) => void;
  /** One value of the Settings page: the card writes it and answers `null`, or the sentence to show. */
  onWriteValue?: (write: ValueWrite) => Promise<string | null>;
  /** A "Get started" row answered "Not needed": the card asks the integration to keep it away, then reads again. */
  onDismissSetup?: (id: SetupRowId) => void;
  /** A grid sign row's button (administrators only): the card writes that one site option, then reads again. */
  onWriteSiteOption?: (field: string, value: boolean) => void;
  /** An issue's "Set to …" or "Turn on" (administrators only): the card writes that fix, then reads again. */
  onFixOwnMode?: (entity: string, value: string) => void;
  /** An issue's "Release the import target for this charge" (any user): the card sets the import target aside for this charge, then reads again. */
  onReleaseImportTarget?: () => void;
  /** The "does not fit" line's "Take what fits", for this departure only (`spotnav/take_what_fits`). */
  onTakeWhatFits?: () => void;
  /** The "does not fit" line's "Later departure (14:00)", for this departure only (`spotnav/later_departure`). */
  onLaterDeparture?: () => void;
  /** The import target's editor opened (administrators): the suggestion from Home Assistant's statistics, `null` for none. */
  onSuggestImportTarget?: () => Promise<unknown>;
  /** Where Settings was left for a child dialog: the page returns there (owned by the card, kept across renders). */
  overviewPlace?: OverviewPlaceHolder;
  /** A picture for the camera's editors: the camera's picture now, or a car's reference thumbnail. */
  onCameraPicture?: (request: CameraPictureRequest) => Promise<{ picture: CameraPicture } | { code: string | null }>;
  /** A slot of a car's reference editor: take or delete that kind's picture; the editor stays open. */
  onReferencePicture?: (vehicleId: string, kind: PictureKind, action: "take" | "delete") => Promise<ReferenceAnswer>;
  /** The reference editor closed after a picture was taken or deleted: the card reads the dashboard again. */
  onReferencesChanged?: () => void;
  /** A fee's row: the card reads the area's suggestion and opens its editor (`openFiscalEditor`). */
  onEditFiscal?: (component: FiscalComponentName) => void;
}

export type EntityViewState =
  | { kind: "loading" }
  | { kind: "adminOnly" }
  | { kind: "failed"; failure: FailureSentence }
  | { kind: "ready"; config: EntityConfig };

export type ActionId = "start" | "stop" | "resume";

/** The connection states that say a car is plugged in. */
const PLUGGED_IN_STATES: ReadonlySet<string> = new Set(["connected", "charging", "paused", "finished"]);

/**
 * Whether "Guest car" can be chosen now: the backend knows the guest, and the charger says a car is plugged in (a
 * guest is an answer about that car, never the plan's car for a next plug-in), or a guest is planned for already.
 */
export function guestOffered(model: CardModel): boolean {
  if (model.guest === null) {
    return false;
  }
  return model.guest.active || (model.connection !== null && PLUGGED_IN_STATES.has(model.connection.state));
}

/**
 * What the action row says when something went wrong: the card's sentence plus the stable code,
 * kept as subdued detail.
 */
export interface FailureSentence {
  sentenceKey: TranslationKey;
  code: string | null;
}

export interface CardView {
  element: HTMLElement;
  destroy(): void;
  chart(): ChartInteraction;
  selection(): ChartMark | null;
  readoutText(): string;
  openIssues(opener?: HTMLElement | null): void;
  openCapabilities(): void;
  openPause(): void;
  /**
   * Strategy dialog: an available, non-selected row writes the strategy when `isAdmin`; every other
   * row stays read-only.
   */
  openStrategy(): void;
  openSettingsOverview(): void;
  /** The charge history dialog, showing what `setHistoryState` last said (loading until it says more). */
  openHistory(): void;
  setHistoryState(state: HistoryState): void;
  /** What the History dialog's Power view shows (loading until it says more). */
  setPowerState(state: PowerState): void;
  /** One sentence about the last export, or `null`; `exporting` disables the button while it runs. */
  setHistoryExport(notice: TranslationKey | null, exporting: boolean): void;
  dialogOpen(
    kind: "issues" | "capabilities" | "pause" | "strategy" | "settingsOverview" | "history",
  ): boolean;
  /** While a request is in flight its trigger is disabled, so a second click cannot send a second. */
  /** Disable both action cells; the one for `action`, when given, shows its busy state. */
  setActionPending(pending: boolean, action?: ActionId, choice?: string | null): void;
  /**
   * A refusal or failed confirmation above the status line: one sentence plus the stable code as
   * subdued detail; `null` clears it.
   */
  setActionError(failure: FailureSentence | null): void;
  openSettingsEditor(kind: SettingsEditorKind): void;
  showSettingsEditorForm(form: SettingsEditorForm): void;
  setSettingsEditorNotice(failure: FailureSentence | null): void;
  showSettingsEditorConflict(revision: number, phases: number | null): void;
  setSettingsEditorPending(pending: boolean): void;
  /** Paint the Plan dialog's live preview (`plan-preview.ts`); nothing while no editable Plan dialog is shown. */
  setSettingsPreview(state: PreviewState): void;
  settingsEditorOpen(): SettingsEditorKind | null;
  closeSettingsEditor(): void;
  openMarketEditor(): void;
  showMarketEditorForm(form: MarketEditorForm): void;
  setMarketEditorNotice(failure: FailureSentence | null): void;
  showMarketEditorConflict(revision: number): void;
  setMarketEditorPending(pending: boolean): void;
  marketEditorOpen(): boolean;
  /** Close it, restoring focus to its trigger unless the caller is switching straight to another dialog. */
  closeMarketEditor(options?: { restoreFocus?: boolean }): void;
  setActiveControlPending(pending: boolean): void;
  /**
   * Adopt the returned site block (`null` keeps what is shown) and say what happened, including
   * what became of any balancing-lowered current on a disable.
   */
  adoptActiveControl(site: SiteFacts | null, notice: ActiveControlNotice | null): void;
  setSettingsError(failure: FailureSentence | null): void;
  anyDialogOpen(): boolean;
  setEntityState(state: EntityViewState): void;
  openEntityEditor(scope: EntityScope, config: EntityConfig, notice?: FailureSentence | null): void;
  entityEditorOpen(): EntityScope | null;
  setEntityEditorNotice(failure: FailureSentence | null): void;
  markEntityFieldErrors(errors: readonly EntityFieldError[]): void;
  setEntityEditorPending(pending: boolean): void;
  setEntityHass(hass: unknown): void;
  closeEntityEditor(): void;
  setOverviewNotice(failure: FailureSentence | null): void;
  /** Whether "Make a home of this" is offered: the charger has no site and the installation has no home. */
  setHomeOffered(offered: boolean): void;
  /** One of same-area electricity prices was chosen: the price section says once that the other can be removed. */
  showPriceRemovableNote(): void;
  /** The debug download is being prepared: the button says so and cannot be pressed again. */
  setDebugPending(pending: boolean): void;
  /** Whether the card running here differs from the one the integration serves: Support then says to reload. */
  setCardOutdated(outdated: boolean): void;
  /** The line saying SpotNav was updated (`self-heal.ts`), at the top of the card; `null` takes it away. */
  setUpdateNotice(notice: HTMLElement | null): void;
  /** Close the Settings popover without returning focus (the card is about to redraw it). */
  closeSettingsOverview(): void;
  /** Close the value editor after its write took (the card then reads the dashboard and shows Settings). */
  closeValueEditor(): void;
  /** A fee's own number editor, with the area's suggestion the card read for it. */
  openFiscalEditor(component: FiscalComponentName, facts: { current: number | null; suggestion: number | null }): void;
}

/** The platform's `ResizeObserver` on the card's box, or none where there is none (a test DOM). */
function defaultBoxObserver(_box: HTMLElement, onResize: () => void): ChartObserverLike | null {
  const Constructor = (globalThis as { ResizeObserver?: new (callback: () => void) => ChartObserverLike })
    .ResizeObserver;
  return typeof Constructor === "function" ? new Constructor(onResize) : null;
}

export function createCardView(input: CardViewInput): CardView {
  const { model, idPrefix } = input;
  const doc = input.mount.ownerDocument;
  const now = input.now ?? (() => Date.now());
  let destroyed = false;
  /** Whether this reader may change the strategy and the plan: an administrator, or anyone the charger lets. */
  const mayPlan = input.isAdmin || model.dashboardSettings?.users_change_plan === true;

  const card = element(doc, "div", C.card);
  card.style.boxSizing = "border-box";

  // Header: the product mark and the charger's own name (`card-header.ts`, shared with the house and heat cards). One
  // card is bound to one charger, so there is no selector and no visible product name; the accessible name still
  // names the product.
  // A guest car's line is "Guest car · 10 kWh": it has no level of its own.
  const vehicleLine =
    model.guest?.active === true
      ? guestLineFor(model.language, model.dashboardSettings)
      : vehicleLineFor(model.language, model.soc, model.dashboardSettings);
  // While the charge bar shows, it already says the charge runs: the connection status steps aside.
  const connectionText = model.chargeBar === null ? connectionLabel(model.language, model.connection) : null;
  // The charger's name with its status after it ("HALO Charger · Waiting"): the status is the charger's, not the car's.
  // The status as a word, and as a symbol for when the word does not fit beside the name (`fitStatus`).
  let statusRow: HTMLElement | null = null;
  let title: HTMLElement | null = null;
  if (connectionText !== null) {
    const shown = model.connection?.shown;
    let status: HTMLElement | null = null;
    if (shown !== undefined && shown !== "unknown") {
      status = headerStatus(doc, connectionText, model.chargerName !== null).line;
      if (shown === "error") {
        status.classList.add(C.connectionError);
      }
      status.dataset["connection"] = shown;
      status.append(connectionIcon(doc, shown, connectionText));
    }
    const titled = headerTitle(doc, model.chargerName, status);
    if (status !== null) {
      titled.row.dataset["statusFit"] = "word";
      statusRow = titled.row;
    }
    title = titled.block;
  } else if (model.chargerName !== null) {
    title = element(doc, "h3", C.name, model.chargerName);
  }
  // The car is a button of its own over the action bar (`carButtonElement`), not a line under the name.
  const vehicleButton = vehicleLine === null ? null : carButtonFor(vehicleLine);
  // The charge history: what each charge delivered and cost, beside Settings.
  const historyButton = headerButton(doc, "history", translate(model.language, "header.history"));
  // The Settings entry point: one popover gathering price/fiscal, consumption, capabilities and the site, beside the history.
  const settingsGeneral = headerButton(doc, "settings", translate(model.language, "header.settings"));
  // Shown to a reader who is not an administrator only when Settings holds something they may change.
  settingsGeneral.hidden = !settingsChangeable();
  const { header } = cardHeader(doc, {
    idPrefix,
    productName: translate(model.language, "card.title"),
    title,
    buttons: [historyButton, settingsGeneral],
  });
  card.append(header);

  // Dialogs are created once as overlays so nothing shifts the card's height.
  const labels = { close: translate(model.language, "dialog.close") };
  const issuesDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-issues`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  const releaseDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-release`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  const capabilityDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-capabilities`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
    // Opened from Settings (About this card, Automations): the cross, Escape or the backdrop go back there.
    onDismiss: () => {
      if (capabilityFromSettings) {
        capabilityFromSettings = false;
        leaveSettingsChild(capabilityDialog);
      }
    },
  });
  /** Whether the info dialog was opened from Settings (`setCapabilityFromSettings`), so leaving it goes back. */
  let capabilityFromSettings = false;
  const pauseDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-pause`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  const strategyDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-strategy`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  const vehicleDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-vehicle`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  const settingsDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-settings`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  // The plan dialog's group headings read like the entity dialogs' (one heading style across the card).
  settingsDialog.element.classList.add(C.planDialog);
  const marketDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-market`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
    // Leaving the price/tax editor by any of the reader's own routes lands on the Settings page it
    // was opened from, exactly as a Save does.
    onDismiss: () => leaveSettingsChild(marketDialog, input.onCancelMarket),
  });
  const entityDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-entities`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
    onDismiss: () => leaveSettingsChild(entityDialog, input.onCancelEntities),
  });
  entityDialog.element.classList.add(C.entityDialog);
  const settingsOverviewDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-settings-overview`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  const historyDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-history`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
  /** One value of the Settings page in its own editor (`value-editors.ts`). */
  const valueDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-value`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
    onDismiss: () => (valueLeave ?? (() => leaveSettingsChild(valueDialog)))(),
  });
  /** How the open value editor is left by Escape, the backdrop or the cross, when it is not as Cancel. */
  let valueLeave: (() => void) | null = null;
  for (const wide of [settingsOverviewDialog, valueDialog, marketDialog]) {
    wide.element.classList.add(C.settingsWide);
  }

  /**
   * The one way out of a dialog opened from the Settings page (Cancel, close, Escape, backdrop).
   * The card's callback drops the editor's state and may answer `false` (a save is in flight and
   * returns the reader itself); otherwise this closes the dialog without restoring focus to a
   * background control and reopens Settings built fresh from the model.
   */
  function leaveSettingsChild(dialog: DialogHandle, cancel?: () => boolean | void): void {
    const returns = cancel?.() !== false;
    dialog.hide({ restoreFocus: false });
    if (returns && !destroyed) {
      openSettingsOverview();
    }
  }

  function anyDialogOpenNow(): boolean {
    return (
      issuesDialog.isOpen() ||
      releaseDialog.isOpen() ||
      capabilityDialog.isOpen() ||
      pauseDialog.isOpen() ||
      strategyDialog.isOpen() ||
      vehicleDialog.isOpen() ||
      settingsDialog.isOpen() ||
      marketDialog.isOpen() ||
      entityDialog.isOpen() ||
      settingsOverviewDialog.isOpen() ||
      historyDialog.isOpen() ||
      valueDialog.isOpen()
    );
  }

  /**
   * One dialog just closed. Deferred to a microtask so closing one dialog and opening another in
   * the same synchronous call is never reported as "nothing open".
   */
  function notifyDialogsChanged(): void {
    queueMicrotask(() => {
      if (destroyed || anyDialogOpenNow()) {
        return;
      }
      input.onDialogsClosed?.();
    });
  }

  const severity = bannerSeverity(model);
  const issueBannerShown = severity !== null && !bannerRepeatsStatus(model, severity);
  if (severity !== null && issueBannerShown) {
    const banner = element(doc, "button", `${C.banner} ${severity === "blocking" ? C.bannerBlocking : C.bannerNotice}`);
    banner.type = "button";
    banner.append(
      element(
        doc,
        "span",
        undefined,
        translate(model.language, severity === "blocking" ? "issue.banner.blocking" : "issue.banner.notice"),
      ),
    );
    banner.append(element(doc, "span", C.bannerCount, issueCountText(model.language, model.issues.length)));
    banner.addEventListener("click", () => {
      openIssues(banner);
    });
    card.append(banner);
  } else if (input.isAdmin && model.setup.length > 0) {
    // Nothing more serious to say: the same line says what is left to set up, and opens the same dialog.
    const banner = element(doc, "button", `${C.banner} ${C.bannerNotice}`);
    banner.type = "button";
    banner.dataset["banner"] = "setup";
    banner.append(element(doc, "span", undefined, setupLineText(model.language, model.setup.length)));
    banner.addEventListener("click", () => {
      openIssues(banner);
    });
    card.append(banner);
  }

  // Two different facts above the status sentence: the action error (the outcome of what a person just
  // did) and the backend's own explanation of the control state. Codes appear only as subdued detail.
  const actionError = element(doc, "p", C.actionError);
  actionError.setAttribute("role", "status");
  actionError.hidden = true;
  card.append(actionError);
  // A save that committed but could not be applied or confirmed is a row-level fact: the dialog is
  // closed by then, so the sentence sits beside the action error.
  const settingsError = element(doc, "p", C.settingsError);
  settingsError.setAttribute("role", "status");
  settingsError.hidden = true;
  card.append(settingsError);

  if (model.control.notice !== null) {
    const notice = element(doc, "p", C.controlNotice, model.control.notice);
    card.append(notice);
  }

  // Vehicle-side advisory: the only line about the car rather than the plan or charger. Shown only when
  // the backend observes a started charge not being taken (see charge_progress.py); no button, since
  // there is no recovery command. It states what was observed, not a fix.
  if (model.advisory !== null) {
    const advisory = element(doc, "p", C.advisory, model.advisory.text);
    advisory.setAttribute("role", "status");
    card.append(advisory);
  }

  // What the charges showed about the planned car: it seems to charge on one phase. A question with a
  // one-tap answer for the administrator, never a change made by itself.
  const suggestedVehicle = model.vehicles.find((row) => row.id === model.targetVehicleId);
  if (input.isAdmin && suggestedVehicle !== undefined && suggestedVehicle.suggested_onboard_phases === 1) {
    const suggestion = element(doc, "div", C.suggestion);
    suggestion.dataset["suggestion"] = "onboard-phases";
    suggestion.setAttribute("role", "status");
    suggestion.append(element(doc, "p", C.suggestionText, translate(model.language, "suggestion.onboardOne.text")));
    const answers = element(doc, "div", C.suggestionAnswers);
    for (const [phases, label, action] of [
      [1, "suggestion.onboardOne.accept", "accept"],
      [3, "suggestion.onboardOne.dismiss", "dismiss"],
    ] as const) {
      const answer = element(doc, "button", C.choiceButton, translate(model.language, label));
      answer.type = "button";
      answer.dataset["action"] = action;
      answer.addEventListener("click", () => {
        answer.disabled = true;
        input.onAnswerOnboardPhases?.(suggestedVehicle.id, phases);
      });
      answers.append(answer);
    }
    suggestion.append(answers);
    card.append(suggestion);
  }

  // Which car is plugged in: the open question, answered with one tap (by anyone signed in), else the current
  // car is kept. The phones are asked the same; the first answer wins.
  const identification = model.identification;
  if (identification !== null && identification.state === "asking") {
    const current = identification.candidates.find((item) => item.vehicle_id === identification.vehicle_id);
    card.append(
      identificationBanner(doc, model.language, {
        block: identification,
        currentName: current?.name ?? null,
        canAnswer: true,
        ...(model.guest === null ? {} : { guest: model.guest.first ? ("first" as const) : ("last" as const) }),
        onAnswer: (vehicleId) => input.onAnswerIdentification?.(vehicleId),
      }),
    );
  }

  /**
   * The planned charge does not fit under the ceiling by its departure: the line, and a button per choice still open.
   * "Take what fits" holds this departure only (`spotnav/take_what_fits`, any user); "Later departure (14:00)" moves this departure
   * there (`spotnav/later_departure`, any user), and without a time opens the departure's editor; "Allow
   * the peak" is the import target's set-aside for this charge, confirmed first as everywhere.
   */
  function doesNotFitNode(line: DoesNotFitLine): HTMLElement {
    const box = element(doc, "div", C.suggestion);
    box.dataset["part"] = "does-not-fit";
    box.setAttribute("role", "status");
    box.append(element(doc, "p", C.suggestionText, line.text));
    const answers = element(doc, "div", C.suggestionAnswers);
    for (const choice of line.choices) {
      if (choice.code === "takes_what_fits" && input.onTakeWhatFits === undefined) {
        continue;
      }
      if (choice.moves && input.onLaterDeparture === undefined) {
        continue;
      }
      if (choice.code === "accept_peak" && (input.onReleaseImportTarget === undefined || model.releaseConfirm === null)) {
        continue;
      }
      const button = element(doc, "button", C.choiceButton, choice.label);
      button.setAttribute("type", "button");
      button.dataset["choice"] = choice.code;
      button.addEventListener("click", () => {
        if (choice.code === "takes_what_fits") {
          button.disabled = true;
          input.onTakeWhatFits?.();
        } else if (choice.moves) {
          button.disabled = true;
          input.onLaterDeparture?.();
        } else if (choice.code === "later") {
          input.onOpenSettings("deadline");
        } else {
          confirmRelease(button);
        }
      });
      answers.append(button);
    }
    if (answers.childElementCount > 0) {
      box.append(answers);
    }
    return box;
  }

  // The status sentence, the one row that gives up lines when the card is in a box too short for it.
  const statusNode = model.status === null ? null : element(doc, "p", C.status, model.status);
  if (statusNode !== null) {
    card.append(statusNode);
  }
  // A status line that sends the reader to the site's settings has the way there, for an administrator.
  if (model.status !== null && model.statusNeedsSiteSettings && input.isAdmin) {
    const answers = element(doc, "div", C.suggestionAnswers);
    answers.dataset["part"] = "open-site";
    const open = element(doc, "button", C.choiceButton, translate(model.language, "entity.phases.openSite"));
    open.setAttribute("type", "button");
    open.dataset["action"] = "open-site";
    open.addEventListener("click", openSiteSettings);
    answers.append(open);
    card.append(answers);
  }
  if (model.doesNotFit !== null) {
    card.append(doesNotFitNode(model.doesNotFit));
  }
  if (model.doesNotFit === null && model.releaseConfirm !== null && input.onReleaseImportTarget !== undefined) {
    // The one way past the import target, offered whenever it holds or pauses the charge: confirmed first.
    const answers = element(doc, "div", C.suggestionAnswers);
    answers.dataset["part"] = "release-import-target";
    const release = element(doc, "button", C.choiceButton, translate(model.language, "issue.importTargetRelease"));
    release.setAttribute("type", "button");
    release.addEventListener("click", () => confirmRelease(release));
    answers.append(release);
    card.append(answers);
  }
  if (model.chargeBar !== null) {
    card.append(chargeBarElement(doc, model.chargeBar));
  }
  if (model.statusNote !== null) {
    card.append(element(doc, "p", `${C.status} ${C.muted}`, model.statusNote));
  }

  // ---- the graph: one outer section whose *viewport* is the tab stop and the image object.
  // The readout, hint and legend are siblings of that viewport, not descendants: an `aria-live`
  // region inside `role="img"` is presentational as far as accessibility APIs are concerned, and the
  // viewport is also exactly the box the chart is measured and drawn for.
  const graph = element(doc, "section", C.graphSurface);
  const viewport = element(doc, "div", C.viewport);
  viewport.id = `${idPrefix}-chart`;
  viewport.tabIndex = 0;
  viewport.setAttribute("role", "img");
  viewport.setAttribute("aria-labelledby", `${idPrefix}-chart-title`);
  viewport.setAttribute("aria-describedby", `${idPrefix}-chart-description`);
  const readout = element(doc, "p", C.readout, readoutDisplayText(model, null));
  // Always present and always one line high: selecting a point writes into a line that was already
  // reserved, so the controls below the chart never move when the chart is pressed.
  readout.textContent = "";
  readout.setAttribute("aria-live", "polite");
  // The hint and legend are visually hidden but kept in the accessibility tree: `graphDescription`
  // covers most of it, and these nodes carry the roles it does not (cheap/expensive, installed/proposal,
  // selection line).
  const hint = element(doc, "p", `${C.readoutHint} ${C.visuallyHidden}`, translate(model.language, "graph.hint"));
  const legend = element(doc, "p", `${C.legend} ${C.visuallyHidden}`);

  /**
   * Only the legend entries that describe something drawn. Day entries name roles ("Today",
   * "Tomorrow", an earlier captured day), not date counts; the current interval is named while one
   * is published; the decorative focus lines are never mentioned.
   */
  function legendKeys(current: CardModel, selected: ChartMark | null): TranslationKey[] {
    const keys: TranslationKey[] = [];
    if (current.chart.days.some((day) => day.role === "today")) {
      keys.push("graph.legend.today");
    }
    if (current.chart.days.some((day) => day.role === "future")) {
      keys.push("graph.legend.tomorrow");
    }
    if (current.chart.days.some((day) => day.role === "past")) {
      keys.push("graph.legend.past");
    }
    keys.push("graph.legend.cheap", "graph.legend.expensive");
    if (nowState.mark !== null) {
      keys.push("graph.legend.current");
    }
    if (current.bands.installed.length > 0) {
      keys.push("graph.legend.installed");
    }
    if (current.bands.proposal.length > 0) {
      keys.push("graph.legend.proposal");
    }
    if (current.sun !== null) {
      keys.push(current.sun.points.some((point) => !point.measured) ? "graph.legend.sun" : "graph.legend.sunMeasured");
    }
    if (!current.timesAvailable) {
      keys.push("graph.noZone");
    }
    if (selected !== null) {
      keys.push("graph.legend.selection");
    }
    return keys;
  }

  function paintLegend(selected: ChartMark | null): void {
    legend.replaceChildren(
      ...legendKeys(model, selected).map((key) =>
        element(doc, "span", undefined, translate(model.language, key)),
      ),
    );
  }

  let interaction: ChartInteraction | null = null;
  let drawn: ReturnType<typeof renderChart> | null = null;
  // The initial draw uses the measurement's own policy, so the first viewBox already matches what the reader sees.
  const initial = input.size?.() ?? FALLBACK_SIZE;
  const density = input.chartDetail === true ? CHART_DENSITY.detail : CHART_DENSITY.normal;
  let size: ChartSize = { width: initial.width, height: chartHeightForWidth(initial.width, density) };
  // The chart's height while the card fills a box of fixed height, else `null` and the width rule applies.
  let fillHeight: number | null = null;
  // The now fact of the last draw; nothing else in this view reads the wall clock for geometry.
  let nowState: ChartNow = chartNowAt(model.chart.marks, now());
  // The one pending interval-boundary appointment, and the injected scheduler that owns it.
  const setTimer = input.setTimer ?? ((callback: () => void, delayMs: number) => window.setTimeout(callback, delayMs));
  const clearTimer = input.clearTimer ?? ((handle: number) => window.clearTimeout(handle));
  let boundaryHandle: number | null = null;
  // The summary's Now figure, kept so a redraw moves it with the now line.
  let nowValue: HTMLElement | null = null;

  /** (Re)draw the SVG only: the interaction, the readout and the legend survive a resize. */
  function render(next: ChartSize): void {
    size = next;
    nowState = chartNowAt(model.chart.marks, now());
    // The one now fact in every place it shows: the line, the description, the keyboard fallback and the summary figure.
    if (nowValue !== null) {
      const text = nowSummaryText(model, currentPriceText(model, nowState.mark));
      // A now fact this instant lacks (no containing interval or no zone) omits the whole line rather than say "Now unknown".
      nowValue.hidden = text === null;
      nowValue.textContent = text ?? "";
    }
    const labels: ChartLabels = {
      time: (instantMs) => clock(model.format, instantMs),
      day: (instantMs) => dayCue(model.format, instantMs),
      hour: (instantMs) => hourLabel(model.format, instantMs),
      price: (value, span) => priceAxisLabel(model.format, value, span),
    };
    const result = renderChart({
      series: model.chart,
      bands: model.bands,
      now: nowState,
      width: size.width,
      height: size.height,
      selected: interaction?.selection() ?? null,
      title: translate(model.language, "graph.title"),
      description: graphDescription(
        model,
        interaction?.selection() ?? null,
        nowState.mark,
        model.chart.marks,
        model.chart.days.length,
      ),
      labels,
      density,
      sun: model.sun,
      ids: {
        title: `${idPrefix}-chart-title`,
        description: `${idPrefix}-chart-description`,
        hatch: `${idPrefix}-chart-hatch`,
      },
    });
    drawn?.element.remove();
    drawn = result;
    // The viewport's height is the policy's answer for the measured width (or the fitted height in a box
    // of fixed height), so the box, viewBox and CSS viewport agree 1:1.
    viewport.style.height = `${fillHeight ?? chartHeightForWidth(size.width, density)}px`;
    viewport.append(result.element);
    paintStrip();
    publish();
  }

  /**
   * The collapsed chart's strip, from the same bands, now fact and axis as the chart. Decorative for
   * assistive technology: the period list below carries the whole schedule in words.
   */
  const strip = element(doc, "div", C.strip);
  strip.id = `${idPrefix}-strip`;
  strip.setAttribute("aria-hidden", "true");
  const stripArea = element(doc, "div", C.stripArea);
  const stripTrack = element(doc, "div", C.stripTrack);
  const stripNow = element(doc, "span", C.stripNow);
  const stripLabels = element(doc, "div", C.stripLabels);
  stripArea.append(stripTrack, stripNow);
  strip.append(stripArea, stripLabels);

  function paintStrip(): void {
    const at = now();
    stripTrack.replaceChildren(
      ...stripBars(model.bands, at).map((bar) => {
        const node = element(doc, "span", C.stripBar);
        node.dataset["kind"] = bar.kind;
        node.dataset["past"] = bar.past ? "true" : "false";
        node.dataset["tomorrow"] = bar.tomorrow ? "true" : "false";
        const place = stripBarPlacement(bar);
        if (place.left !== null) {
          node.style.left = place.left;
        }
        if (place.right !== null) {
          node.style.right = place.right;
        }
        node.style.width = place.width;
        node.style.minWidth = place.minWidth;
        return node;
      }),
    );
    const position = stripNowPosition(nowState, at, model.chart.time.spanMs);
    stripNow.hidden = position === null;
    stripNow.style.left = position === null ? "" : `${position * 100}%`;
    const ticks = stripTicks(model.chart, model.format.timeZone);
    // A midnight inside the real-time axis: a thin divider through the track, as the chart draws one.
    stripArea.querySelectorAll(`.${C.stripDivider}`).forEach((node) => node.remove());
    for (const tick of ticks.filter((entry) => entry.dayStart === true)) {
      const divider = element(doc, "span", C.stripDivider);
      divider.style.left = `${tick.position * 100}%`;
      stripArea.insertBefore(divider, stripNow);
    }
    stripLabels.replaceChildren(
      ...ticks.map((tick) => {
        const label = element(doc, "span", C.stripLabel, tick.label);
        label.style.left = `${tick.position * 100}%`;
        if (tick.position <= 0 || tick.position >= 1) {
          label.dataset["edge"] = tick.position <= 0 ? "start" : "end";
        }
        return label;
      }),
    );
  }

  /** Hand the drawn geometry to the controller: the one geometry, never a recomputed second one. */
  function publish(): void {
    if (interaction === null || drawn === null) {
      return;
    }
    interaction.setGeometry({
      scale: drawn.scale,
      targets: drawn.targets,
      marks: model.chart.marks,
      current: nowState.mark,
    });
    applyFocus(drawn, interaction.selection()?.startMs ?? null);
  }

  /**
   * Wait for the one instant the focus can change: the end of the current interval. One appointment
   * at a time (arming replaces); `null` from the model cancels; `destroy()` is terminal and a late
   * callback touches nothing.
   */
  function scheduleBoundary(): void {
    cancelBoundary();
    if (destroyed) {
      return;
    }
    const at = now();
    const boundary = nextIntervalBoundary(model.chart.marks, at);
    if (boundary === null || boundary - at > BOUNDARY_HORIZON_MS) {
      return;
    }
    boundaryHandle = setTimer(onBoundary, Math.max(0, boundary - at));
  }

  function cancelBoundary(): void {
    if (boundaryHandle === null) {
      return;
    }
    clearTimer(boundaryHandle);
    boundaryHandle = null;
  }

  function onBoundary(): void {
    boundaryHandle = null;
    if (destroyed) {
      return; // A cancelled appointment that still fired owns nothing.
    }
    // Redraw only when the current interval really changed: an early or duplicate callback inside the
    // same interval is not a redraw, and the next appointment is always recomputed from the clock.
    if (chartNowAt(model.chart.marks, now()).identity !== nowState.identity) {
      render(size);
      paintLegend(interaction?.selection() ?? null);
    }
    scheduleBoundary();
  }

  // Summary: max, min and current inside the plot, as an absolutely positioned overlay in the graph
  // section so it can never affect chart measurement. Current sits alone on the right; max and min share
  // the left with up/down markers in the plot's expensive/cheap colours.
  const summary = element(doc, "div", C.summary);
  const summaryExtremes = element(doc, "span", C.summaryExtremes);
  // One line above the plot: `▲ 267  ▼ 112.9` on the left with the unit dropped (the "Now" figure at
  // the right carries it), and the labelled, unit-bearing wording kept as each figure's accessible name.
  const maxLine = element(doc, "span", C.summaryMax);
  maxLine.append(element(doc, "span", C.summaryArrow, "▲"), doc.createTextNode(` ${model.summary.maxFigure ?? ""}`));
  maxLine.querySelector(`.${C.summaryArrow}`)?.setAttribute("aria-hidden", "true");
  maxLine.setAttribute("aria-label", `${translate(model.language, "graph.summary.max")} ${model.summary.max ?? ""}`);
  maxLine.hidden = model.summary.max === null;
  const minLine = element(doc, "span", C.summaryMin);
  minLine.append(element(doc, "span", C.summaryArrow, "▼"), doc.createTextNode(` ${model.summary.minFigure ?? ""}`));
  minLine.querySelector(`.${C.summaryArrow}`)?.setAttribute("aria-hidden", "true");
  minLine.setAttribute("aria-label", `${translate(model.language, "graph.summary.min")} ${model.summary.min ?? ""}`);
  minLine.hidden = model.summary.min === null;
  summaryExtremes.append(maxLine, minLine);
  if (model.sun !== null) {
    // The legend's swatch for the sun in the chart, in the summary row beside the price extremes.
    const sunKey = element(doc, "span", C.summarySun);
    sunKey.dataset["part"] = "sun-key";
    // With the site's peak known, the swatch says what the sun is scaled against ("Sol, topp 10 kW"), so a dull day
    // drawn low reads as little sun; the label stays out of the plot, where it would sit on the price marks.
    const sunText =
      model.sun.peakKw !== null
        ? translate(model.language, "graph.sun.peak", { kw: formatNumber(model.language, model.sun.peakKw, 1) })
        : translate(model.language, "graph.sun.short");
    sunKey.append(element(doc, "span", C.sunSwatch), doc.createTextNode(sunText));
    sunKey.querySelector(`.${C.sunSwatch}`)?.setAttribute("aria-hidden", "true");
    summaryExtremes.append(sunKey);
  }
  // The model's build-time figure is the first paint; later redraws read the injected now fact and hide the line the same way when it has no figure.
  const currentText = nowSummaryText(model, model.summary.current);
  const currentValue = element(doc, "span", C.summaryCurrent, currentText ?? "");
  currentValue.hidden = currentText === null;
  nowValue = currentValue;
  // The whole price row is the chart's toggle: a small chevron at its right end, always present so the
  // row's text never moves, and the action in words for assistive technology.
  const toggle = element(doc, "span", C.chartToggle);
  const toggleGlyph = element(doc, "span", C.chartToggleGlyph);
  toggleGlyph.setAttribute("aria-hidden", "true");
  const toggleWords = element(doc, "span", C.visuallyHidden);
  toggle.append(toggleGlyph, toggleWords);
  summary.append(summaryExtremes, currentValue, toggle);
  summary.setAttribute("role", "button");
  summary.tabIndex = 0;
  summary.setAttribute("aria-controls", `${viewport.id} ${strip.id}`);

  let collapsed = input.chartCollapsed === true;
  function applyCollapsed(): void {
    summary.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggleGlyph.textContent = collapsed ? "\u02c5" : "\u02c4";
    toggleWords.textContent = translate(model.language, collapsed ? "graph.toggle.show" : "graph.toggle.hide");
    viewport.hidden = collapsed;
    readout.hidden = collapsed;
    hint.hidden = collapsed;
    legend.hidden = collapsed;
    strip.hidden = !collapsed;
  }
  function toggleChart(): void {
    collapsed = !collapsed;
    // A selection belongs to the chart that showed it; either way the reader starts from the now line.
    interaction?.select(null);
    applyCollapsed();
    fit();
    input.onChartCollapsedChange?.(collapsed);
  }
  summary.addEventListener("click", toggleChart);
  summary.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      toggleChart();
    }
  });
  applyCollapsed();
  paintStrip();

  graph.append(summary, viewport, readout, strip, hint, legend);
  card.append(graph);

  interaction = createChartInteraction({
    // Listeners on the stable outer section; measurement and observation on the viewport alone.
    host: graph,
    viewport,
    surface: () => viewport.querySelector("svg"),
    fallbackSize: size,
    now,
    // In a box of fixed height the fitted height replaces the width rule's; the width is measured as ever.
    measure: (target) => {
      const measured = input.measure !== undefined ? input.measure(target) : measureChart(target, density);
      return fillHeight === null ? measured : { width: measured.width, height: fillHeight };
    },
    density,
    observe: input.observe,
    onResize: (next) => {
      render(next);
    },
    onSelectionChange: (mark) => {
      if (drawn !== null) {
        applyFocus(drawn, mark?.startMs ?? null);
      }
      readout.textContent = mark === null ? "" : readoutDisplayText(model, mark);
      paintLegend(mark);
      if (drawn !== null) {
        const description = graphDescription(
          model,
          mark,
          nowState.mark,
          model.chart.marks,
          model.chart.days.length,
        );
        drawn.element.querySelector("desc")?.replaceChildren(document.createTextNode(description));
      }
    },
    // The viewport and the readout are the region where a press belongs to the chart; anywhere else
    // in the card is "outside" and clears the selection.
    isInsideInteractive: (node) =>
      node instanceof Node && (viewport.contains(node) || readout.contains(node)),
  });
  publish();

  // Periods: the blocks the model's relation asks for, in its order. The rows are visually hidden but
  // kept in the accessibility tree so a screen reader user gets the whole schedule.
  for (const kind of periodBlockOrder(model)) {
    const block =
      kind === "installed"
        ? periodBlock(doc, model, "installed", model.installedPeriods)
        : periodBlock(doc, model, "proposal", model.proposalPeriods);
    block.classList.add(C.visuallyHidden);
    card.append(block);
  }

  // Action bar: six cells (3x2 on a narrow card, 6x1 on a wide one), each a caption naming the axis over
  // an icon and the value or action. Charging (Start/Stop) commands the charger; Schedule (Pause/Resume)
  // is Home Assistant's automatic execution; the rest are planning values. Every cell renders a backend
  // fact and only reports a click; the two control axes are never folded together. The accessible name
  // states axis + value + action, since the caption is not part of a button's name.
  const bar = element(doc, "div", C.actionBar);
  const barWrap = element(doc, "div", C.actionBarWrap);
  // The car, as in the app: its own full-width button right over the cells.
  if (vehicleButton !== null) {
    barWrap.append(vehicleButton);
  }
  barWrap.append(bar);
  const immediateLabelKey = model.control.immediate.labelKey;
  const automaticLabelKey = model.control.automatic.labelKey;
  let actionButton: HTMLButtonElement | null = null;
  let plannerButton: HTMLButtonElement | null = null;
  let strategyButton: HTMLButtonElement | null = null;
  const axisName = (key: TranslationKey): string => translate(model.language, key);
  const changeWord = translate(model.language, "bar.change");

  /**
   * The Charging cell's word and name while the Start or Stop it sent is in flight ([sent]), or its
   * rendered ones again (`null`). The rendered ones are kept on the cell the first time it changes.
   */
  function underWay(button: HTMLButtonElement, sent: "start" | "stop" | null): void {
    const valueNode = button.querySelector(`.${C.settingsValue}`);
    if (valueNode === null) {
      return;
    }
    if (sent === null) {
      const word = button.dataset["renderedValue"];
      const name = button.dataset["renderedLabel"];
      if (word !== undefined && name !== undefined) {
        valueNode.textContent = word;
        button.setAttribute("aria-label", name);
        delete button.dataset["renderedValue"];
        delete button.dataset["renderedLabel"];
      }
      return;
    }
    if (button.dataset["renderedValue"] === undefined) {
      button.dataset["renderedValue"] = valueNode.textContent ?? "";
      button.dataset["renderedLabel"] = button.getAttribute("aria-label") ?? "";
    }
    const stopping = sent === "stop";
    valueNode.textContent = axisName(stopping ? "bar.stopping" : "bar.starting");
    button.setAttribute(
      "aria-label",
      `${axisName("bar.charging")}: ${axisName(stopping ? "bar.state.charging" : "bar.state.notCharging")}. ${axisName("bar.waitingForCharger")}`,
    );
  }

  function cell(
    cellClass: string,
    id: string,
    caption: string,
    glyph: SVGElement | null,
    value: string | readonly string[],
    ariaLabel: string,
    wide = false,
  ): HTMLButtonElement {
    const button = element(doc, "button", `${C.button} ${C.barCell} ${cellClass}`);
    button.type = "button";
    button.dataset["cell"] = id;
    button.setAttribute("aria-label", ariaLabel);
    const captionNode = element(doc, "span", C.barCaption, caption);
    captionNode.setAttribute("aria-hidden", "true");
    const line = element(doc, "span", C.barValue);
    if (wide) {
      // The Plan cell's value is a whole line of its own: it gets the cell's full width.
      button.classList.add(C.barWide);
    }
    if (glyph !== null) {
      line.append(glyph);
    }
    const valueNode = element(doc, "span", C.settingsValue);
    if (typeof value === "string") {
      valueNode.textContent = value;
    } else {
      // One line of parts (`20 kWh · No deadline · 16 A`): each part keeps together, so a narrow cell
      // breaks only at a separator.
      value.forEach((part, index) => {
        if (index > 0) {
          valueNode.append(doc.createTextNode(" \u00b7 "));
        }
        valueNode.append(element(doc, "span", C.barPart, part));
      });
    }
    line.append(valueNode);
    button.append(captionNode, line);
    return button;
  }

  // The explanation a Start button might show is kept as its accessible description, not visible prose.
  const startHelpId = `${idPrefix}-start-help`;
  const startHelp = element(doc, "p", `${C.actionHelp} ${C.visuallyHidden}`, translate(model.language, "action.startHelp"));
  startHelp.id = startHelpId;
  if (immediateLabelKey !== null) {
    const action = model.control.immediate.action;
    // The caption states the state the same fact implies: a Stop on offer means a charge is running.
    const running = action === "stop";
    actionButton = cell(
      `${C.actionButton}`,
      "charging",
      axisName(running ? "bar.chargingNow" : "bar.chargeNow"),
      running ? stopIcon(doc) : playIcon(doc),
      axisName(running ? "bar.stop" : "bar.start"),
      `${axisName("bar.charging")}: ${axisName(running ? "bar.state.charging" : "bar.state.notCharging")}. ${axisName(immediateLabelKey)}`,
    );
    actionButton.dataset["action"] = action;
    // While the integration is starting up the charger's state is not known: no Start or Stop.
    const actionDisabled = !model.control.canAct || model.startingUp;
    actionButton.disabled = actionDisabled;
    actionButton.dataset["renderedDisabled"] = String(actionDisabled);
    actionButton.addEventListener("click", () => {
      // The immediate axis is exactly one command and carries no choice: a bare Stop changes the
      // charger now and leaves the planning record alone. Taking a pause is the *other* cell.
      input.onAction(action as ActionId, null);
    });
    if (action === "start") {
      actionButton.setAttribute("aria-describedby", startHelpId);
    }
    bar.append(actionButton);
  }
  const pendingAction = model.control.pendingAction;
  if (immediateLabelKey === null && pendingAction !== null) {
    // Home Assistant awaits the charger's report of a Start or Stop: the cell stays where it was,
    // greyed and busy, naming what is under way instead of vanishing until the answer arrives.
    const stopping = pendingAction === "stopping";
    actionButton = cell(
      `${C.actionButton}`,
      "charging",
      axisName(stopping ? "bar.chargingNow" : "bar.chargeNow"),
      stopping ? stopIcon(doc) : playIcon(doc),
      axisName(stopping ? "bar.stopping" : "bar.starting"),
      `${axisName("bar.charging")}: ${axisName(stopping ? "bar.state.charging" : "bar.state.notCharging")}. ${axisName("bar.waitingForCharger")}`,
    );
    actionButton.dataset["action"] = stopping ? "stop" : "start";
    actionButton.disabled = true;
    actionButton.dataset["renderedDisabled"] = "true";
    actionButton.dataset["waiting"] = "true";
    actionButton.setAttribute("aria-busy", "true");
    actionButton.classList.add(C.barBusy);
    bar.append(actionButton);
  }
  if (automaticLabelKey !== null) {
    const action = model.control.automatic.action;
    // Pause on offer means the schedule is running, Resume on offer means it is paused.
    const paused = action === "resume";
    plannerButton = cell(
      C.plannerButton,
      "schedule",
      axisName(paused ? "bar.schedulePaused" : "bar.scheduleActive"),
      paused ? playIcon(doc) : pauseIcon(doc),
      axisName(paused ? "action.resumeShort" : "action.pauseAutomaticShort"),
      `${axisName("bar.schedule")}: ${axisName(paused ? "bar.state.schedulePaused" : "bar.state.scheduleActive")}. ${axisName(automaticLabelKey)}`,
    );
    plannerButton.dataset["action"] = action;
    const plannerDisabled = !model.control.canAct || model.startingUp;
    plannerButton.disabled = plannerDisabled;
    plannerButton.dataset["renderedDisabled"] = String(plannerDisabled);
    plannerButton.addEventListener("click", () => {
      if (action === "pause") {
        // The pause always names the choice it is taken with, so the sheet is not optional -- and a
        // charger that offers none of its own gets no menu rather than an empty one (see `openPause`).
        openPause();
        return;
      }
      if (action === "resume") {
        input.onAction("resume", null);
      }
    });
    bar.append(plannerButton);
  }
  const heldAutomatic = model.control.heldAutomatic;
  if (automaticLabelKey === null && heldAutomatic !== null) {
    // While a Start or Stop awaits the charger, the schedule cell keeps the caption it last had,
    // disabled, rather than vanishing and coming back once the charger reports.
    const paused = heldAutomatic === "resume";
    plannerButton = cell(
      C.plannerButton,
      "schedule",
      axisName(paused ? "bar.schedulePaused" : "bar.scheduleActive"),
      paused ? playIcon(doc) : pauseIcon(doc),
      axisName(paused ? "action.resumeShort" : "action.pauseAutomaticShort"),
      `${axisName("bar.schedule")}: ${axisName(paused ? "bar.state.schedulePaused" : "bar.state.scheduleActive")}. ${axisName("bar.waitingForCharger")}`,
    );
    plannerButton.dataset["action"] = heldAutomatic;
    plannerButton.disabled = true;
    plannerButton.dataset["renderedDisabled"] = "true";
    bar.append(plannerButton);
  }
  if (model.strategy.selected !== null) {
    strategyButton = cell(
      C.strategyButton,
      "strategy",
      axisName("strategy.title"),
      strategyIcon(doc, model.strategy.selectedId),
      model.strategy.selected,
      `${axisName("strategy.title")}: ${model.strategy.selected}. ${changeWord}`,
    );
    strategyButton.addEventListener("click", () => {
      openStrategy();
    });
    bar.append(strategyButton);
  }
  // The Plan cell: requested energy, finish by and current in one popover. Area/fiscal and consumption live in Settings.
  const planParts = planSummaryParts(
    model.language,
    model.dashboardSettings,
    model.today,
    model.calendarDeparture,
    model.currentRange.kwPerA,
  );
  const planCaption = axisName("bar.plan");
  const planTrigger = cell(
    C.settingsTrigger,
    "plan",
    planCaption,
    null,
    planParts,
    `${planCaption}: ${planParts.join(" \u00b7 ")}. ${changeWord}`,
    true,
  );
  planTrigger.dataset["setting"] = "plan";
  planTrigger.addEventListener("click", () => {
    input.onOpenSettings("plan");
  });
  bar.append(planTrigger);
  card.append(barWrap);
  if (actionButton !== null && actionButton.dataset["action"] === "start") {
    card.append(startHelp);
  }


  mountCard();
  paintLegend(null);
  render(size);
  scheduleBoundary();

  // ---- a box of fixed height (a wall screen's layout card, a sections view with a number of rows):
  // the card fills it, the action bar sits at its bottom edge and the chart takes the height between.
  // Detected from layout, never from an option: the box is the host's height (`:host` and the shell
  // are `height: 100%`, which is auto unless the container's height is definite), against the card's
  // own natural height. Anywhere else the two are equal and nothing here writes to the DOM.
  const fillBox: HTMLElement | null =
    input.mount instanceof ShadowRoot
      ? input.mount.host instanceof HTMLElement
        ? input.mount.host
        : null
      : input.mount;
  let filled = false;

  function readFillFromLayout(target: HTMLElement): FillReading {
    let lineHeight = 0;
    if (statusNode !== null) {
      const style = getComputedStyle(statusNode);
      lineHeight = Number.parseFloat(style.lineHeight);
      if (!Number.isFinite(lineHeight)) {
        // "normal": the card's own line height (1.35) on the sentence's font size.
        lineHeight = Number.parseFloat(style.fontSize) * 1.35;
      }
    }
    return {
      // Layout pixels, not the bounding rectangle: a scaled container (a CSS transform) must not change the fit.
      box: fillBox?.offsetHeight ?? 0,
      natural: target.offsetHeight,
      statusHeight: statusNode?.offsetHeight ?? 0,
      lineHeight: Number.isFinite(lineHeight) ? lineHeight : 0,
      // The readout line is out of the flow in a box of fixed height (see `[data-fill]` in visual-styles.ts).
      reclaimed: readout.hidden ? 0 : readout.offsetHeight + Number.parseFloat(getComputedStyle(readout).marginTop || "0"),
    };
  }

  /** The card laid out as anywhere else: chart at its width rule, the status sentence in full. */
  function naturalLayout(): void {
    if (!filled) {
      return;
    }
    filled = false;
    delete card.dataset["fill"];
    card.style.removeProperty("--spotnav-fill-spare");
    if (statusNode !== null) {
      delete statusNode.dataset["lines"];
      statusNode.style.removeProperty("--spotnav-status-lines");
      if (statusNode.getAttribute("style") === "") {
        statusNode.removeAttribute("style");
      }
      statusNode.removeAttribute("title");
    }
    viewport.style.height = `${chartHeightForWidth(size.width, density)}px`;
  }

  /**
   * Fit the card to its box. One synchronous pass: back to the natural layout, one reading of it, the
   * plan from that reading alone (`planFill`), applied. Nothing is painted in between, and since the
   * reading never sees the plan's own layout, measuring again gives the same plan: no resize loop.
   */
  function fit(): void {
    if (destroyed) {
      return;
    }
    const before = fillHeight;
    naturalLayout();
    const basis = collapsed ? 0 : chartHeightForWidth(size.width, density);
    const plan = planFill((input.readFill ?? readFillFromLayout)(card), {
      basis,
      min: collapsed ? 0 : FILL_MIN_CHART_PX,
      floor: collapsed ? 0 : FILL_FLOOR_CHART_PX,
    });
    fillHeight = plan === null || collapsed ? null : plan.chartHeight;
    if (plan !== null) {
      filled = true;
      card.dataset["fill"] = "true";
      card.style.setProperty("--spotnav-fill-spare", `${plan.spare}px`);
      if (statusNode !== null && plan.statusLines !== null) {
        statusNode.dataset["lines"] = String(plan.statusLines);
        statusNode.style.setProperty("--spotnav-status-lines", String(plan.statusLines));
        statusNode.title = model.status ?? "";
      }
      viewport.style.height = `${fillHeight ?? basis}px`;
    }
    if (fillHeight !== before) {
      // Redraw at the new height through the interaction, so its size, the viewBox and the hit targets agree.
      interaction?.remeasure();
    }
  }

  /** The status as its word when the word fits beside the name, else as its symbol (`statusFits`). */
  function fitStatus(): void {
    if (destroyed || statusRow === null) {
      return;
    }
    const fits = statusFits((input.readStatusFit ?? readStatusFit)(statusRow));
    statusRow.dataset["statusFit"] = fits ? "word" : "icon";
  }
  // The fit changes what the header shows, so it is written in the next frame, not inside the observer's own
  // callback: a write there can resize the block again and the browser reports "ResizeObserver loop completed".
  let statusFrame: number | null = null;
  function scheduleFitStatus(): void {
    const request = (globalThis as { requestAnimationFrame?: (callback: () => void) => number }).requestAnimationFrame;
    if (typeof request !== "function") {
      fitStatus();
      return;
    }
    if (statusFrame === null) {
      statusFrame = request(() => {
        statusFrame = null;
        fitStatus();
      });
    }
  }
  const headerBlock = (statusRow as HTMLElement | null)?.parentElement ?? null;
  const headerObserver: ChartObserverLike | null =
    headerBlock === null ? null : (input.observeHeader ?? defaultBoxObserver)(headerBlock, scheduleFitStatus);
  headerObserver?.observe?.(headerBlock as HTMLElement);

  const boxObserver: ChartObserverLike | null =
    fillBox === null
      ? null
      : (input.observeBox ?? defaultBoxObserver)(fillBox, () => {
          fit();
        });
  boxObserver?.observe?.(fillBox as HTMLElement);

  function mountCard(): void {
    if (!input.mount.contains(card)) {
      input.mount.append(card);
    }
  }


  /**
   * The car button over the action bar. It opens Change car wherever the car can be changed (more than one car can
   * charge here): for every user while a car is plugged in, since any user may answer which car it is. With no car
   * plugged in the choice is the plan's car, a plan setting (`mayPlan`). A guest car can be plugged in anywhere: with
   * one car too, while a car is plugged in, it can be the answer. Anywhere else it opens the car's reading.
   */
  function carButtonFor(line: VehicleLineFacts): HTMLButtonElement {
    const changeable =
      (model.chargerCars.length >= 2 && (mayPlan || model.connection?.state !== "disconnected")) ||
      guestOffered(model);
    const button = carButtonElement(doc, model.language, {
      line,
      decidedBy: methodWords(model.language, model.identification),
      asking: model.identification?.state === "asking",
      changeable,
    });
    button.addEventListener("click", () => {
      if (changeable) {
        openChangeCar(button, line);
      } else {
        openCarReading(button, line);
      }
    });
    return button;
  }

  /** The car's charge and how old its reading is, where the car cannot be changed: the card says the age only here. */
  function openCarReading(opener: HTMLElement, line: VehicleLineFacts): void {
    if (destroyed) {
      return;
    }
    hideForChildDialog();
    const body = element(doc, "div");
    const reading = element(doc, "p", C.settingsNote, line.reading ?? `${line.estimatePrefix}${line.charge}`);
    reading.dataset["reading"] = line.vehicleId;
    body.append(reading);
    const decidedBy = methodWords(model.language, model.identification);
    if (decidedBy !== null) {
      body.append(element(doc, "p", C.settingsNote, decidedBy));
    }
    vehicleDialog.show({
      title: line.name ?? translate(model.language, "carButton.caption"),
      body,
      opener,
    });
  }

  function openChangeCar(opener: HTMLElement, line: VehicleLineFacts): void {
    if (destroyed) {
      return;
    }
    hideForChildDialog();
    const body = changeCarBody(doc, model.language, {
      reading: line.reading === null ? null : { vehicleId: line.vehicleId, text: line.reading },
      block: model.identification,
      vehicles: model.chargerCars,
      currentId:
        model.guest?.active === true
          ? GUEST_VEHICLE_ID
          : (model.identification?.vehicle_id ?? model.soc?.vehicle_id ?? model.targetVehicleId),
      // The guest is an answer about the car plugged in: never the plan's car for a next plug-in.
      guest: guestOffered(model),
      decidedBy: methodWords(model.language, model.identification),
      chargerName: model.chargerName,
      idPrefix,
      onChoose: (vehicleId) => {
        vehicleDialog.hide({ restoreFocus: false });
        input.onAnswerIdentification?.(vehicleId);
      },
      onCancel: () => vehicleDialog.hide(),
    });
    vehicleDialog.show({
      title: translate(model.language, "identify.changeCar"),
      body,
      opener,
      actions: takeActions(body, `${idPrefix}-change-car`),
    });
  }


  /**
   * The Settings popover's sections, in order. `anyone` marks a section holding something every user may change
   * (a preference of this device, not a charger setting); every other section's values are an administrator's to
   * change. A reader who is not an administrator is shown only the `anyone` sections, and no Settings button at
   * all when there is none (`settingsChangeable`): a wall screen signed in as an ordinary user gets no page of
   * values it cannot change.
   */
  function overviewSections(): OverviewSection[] {
    return [
      // "Get started" first, as in the house and heat cards' popover: the charger's rows and its site's.
      { key: "setup", anyone: false, append: (body) => appendSetup(body) },
      { key: "market", anyone: false, append: (body) => body.append(priceSectionBody()) },
      {
        key: "vehicle",
        anyone: false,
        append: (body) => {
          // One card per vehicle, repainted when the entity configuration arrives (the sensor row needs it).
          settingsPage.vehicleRows = model.vehicles.map((entry) => ({ ...entry }));
          settingsPage.vehicleListSlot = element(doc, "div");
          settingsPage.vehicleListSlot.dataset["slot"] = "vehicles";
          body.append(settingsPage.vehicleListSlot);
        },
      },
      {
        key: "entities",
        anyone: false,
        append: (body) => {
          settingsPage.entitySlot = element(doc, "section", C.settingsSection);
          settingsPage.entitySlot.dataset["section"] = "entities";
          body.append(settingsPage.entitySlot);
        },
      },
      {
        key: "site",
        anyone: false,
        append: (body) => {
          body.append(siteSectionBody());
          const solar = solarSectionBody();
          if (solar !== null) {
            body.append(solar);
          }
        },
      },
      {
        // The home battery and heat: the same sections as the house and heat cards' popover, read for the site.
        key: "site-shared",
        anyone: false,
        append: (body) => {
          if (siteSections === null || model.site === null) {
            batterySlot = null;
            heatSlot = null;
            return;
          }
          batterySlot = element(doc, "div");
          batterySlot.dataset["slot"] = "battery";
          heatSlot = element(doc, "div");
          heatSlot.dataset["slot"] = "heat";
          body.append(batterySlot, heatSlot);
          paintSiteSections();
        },
      },
      {
        key: "notifications",
        anyone: false,
        append: (body) => {
          if (input.hass !== undefined) {
            body.append(notificationPhones.node());
          }
        },
      },
      // How this device draws the card: anyone's, administrator or not.
      { key: "display", anyone: true, append: (body) => body.append(displaySectionBody()) },
      { key: "support", anyone: false, append: (body) => body.append(supportSectionBody()) },
    ];
  }

  /** Settings' "Get started" (`setup-checklist.setupSettingsSection`), for an administrator with rows left. */
  function appendSetup(body: HTMLElement): void {
    if (!input.isAdmin || model.setup.length === 0) {
      return;
    }
    const section = setupSettingsSection(doc, {
      language: model.language,
      rows: model.setup,
      openPlan: () => {
        settingsOverviewDialog.hide({ restoreFocus: false });
        input.onOpenSettings("plan");
      },
      openSetting: (place, edit) => openSettingAt(place, edit),
      forecastChoosable: (model.site?.solarForecastChoices.length ?? 0) > 0,
      navigate: (path) => {
        settingsOverviewDialog.hide({ restoreFocus: false });
        navigateTo(doc, path);
      },
      openExternal: (url) => {
        doc.defaultView?.open(url, "_blank", "noopener,noreferrer");
      },
      // Kept away at once here (the card is drawn again only once Settings closes), the last row with its heading.
      dismiss: (id: SetupRowId) => {
        const rows = Array.from(section.querySelectorAll<HTMLElement>("[data-setup]"));
        rows.find((row) => row.dataset["setup"] === id)?.remove();
        if (rows.length <= 1) {
          section.remove();
        }
        input.onDismissSetup?.(id);
      },
      writeSiteOption: (field, value) => {
        settingsOverviewDialog.hide({ restoreFocus: false });
        input.onWriteSiteOption?.(field, value);
      },
    });
    body.append(section);
  }

  // The shared battery and heat sections (`settings-popover.ts`), read again each time Settings opens.
  let batterySlot: HTMLElement | null = null;
  let heatSlot: HTMLElement | null = null;
  const siteSections: SiteSections | null =
    input.siteTarget === undefined
      ? null
      : new SiteSections({
          doc,
          idPrefix,
          hass: () => (input.hass?.() ?? null) as HomeAssistantLike | null,
          target: () => input.siteTarget?.() ?? null,
          language: () => model.language,
          isAdmin: () => input.isAdmin,
          openEditor: (title, build) => openValueEditor(title, build),
          repaint: () => paintSiteSections(),
        });

  function paintSiteSections(): void {
    if (siteSections === null) {
      return;
    }
    batterySlot?.replaceChildren(...[siteSections.batteryNode()].filter((node): node is HTMLElement => node !== null));
    if (siteSections.heat !== null || siteSections.heatFailed) {
      heatSlot?.replaceChildren(...siteSections.heatNodes());
    }
  }

  function loadSiteSections(): void {
    if (siteSections === null || model.site === null || !input.isAdmin) {
      return;
    }
    void siteSections.loadSite().then(() => {
      if (!destroyed && settingsOverviewDialog.isOpen()) {
        paintSiteSections();
        // The site's priority list came with the same read: the site rows show it.
        paintEntities();
      }
    });
  }

  /** Whether this reader may change anything in Settings: an administrator always, anyone else only an `anyone` section. */
  function settingsChangeable(): boolean {
    return input.isAdmin || overviewSections().some((section) => section.anyone);
  }

  /**
   * The general Settings popover, built fresh from the model on every open (no request): an overview
   * with one card per area (price area and taxes, each vehicle, the charger, the site, solar), two or
   * three summary rows each and one Change button that opens a dialog with Save and Cancel. Nothing here
   * writes by itself except the active-control switch. A charger with no site gets a plain statement.
   */
  function settingsOverviewBody(): HTMLElement {
    const body = element(doc, "div");
    settingsPage.overviewBodyNode = body;
    settingsPage.vehicleListSlot = null;
    settingsPage.entitySlot = null;

    overviewNoticeNode = element(doc, "p", C.settingsNotice);
    overviewNoticeNode.hidden = true;
    overviewNoticeNode.setAttribute("role", "status");
    body.append(overviewNoticeNode);
    paintOverviewNotice();

    for (const section of overviewSections()) {
      if (input.isAdmin || section.anyone) {
        section.append(body);
      }
    }
    paintEntities();

    return body;
  }

  /** The chart axis this view shows as chosen: the model's, then whatever was chosen here since. */

  /**
   * One value row (`value-editors.settingRow`): tappable in the accent colour when `onTap` changes it (only an
   * administrator's), read-only in the normal colour otherwise.
   */
  function overviewRow(key: string, label: string, value: string, onTap?: () => void): HTMLElement {
    const tap = input.isAdmin ? onTap : undefined;
    const [row] = settingRow(doc, {
      key,
      label,
      value,
      ...(tap === undefined
        ? {}
        : { onTap: tap, changeableText: translate(model.language, "settings.row.changeable", { label, value }) }),
    });
    return row!;
  }

  /**
   * The part of the next entity dialog to show alone, titled by its row; `null` opens the whole dialog. Set by
   * every way into the dialog from this view, so a later open never inherits it.
   */
  let entityFocus: { scope: EntityScope; part: string; title: string } | null = null;

  /** Home Assistant has no address for one entry's options, so the SpotNav page, where its Configure is. */
  function openSiteSettings(): void {
    entityDialog.hide({ restoreFocus: false });
    navigateTo(doc, integrationPath("spotnav"));
  }

  function openEntities(scope: EntityScope, focus: { part: string; title: string } | null = null): void {
    entityFocus = focus === null ? null : { scope, ...focus };
    input.onOpenEntityEditor?.(scope);
  }

  /**
   * A setup row whose value opens a scope's entity dialog (only `part` of it when given): its button also
   * carries `data-edit-entities`.
   */
  function entityRow(key: string, label: string, value: string, scope: EntityScope, part?: string): HTMLElement {
    const row = overviewRow(key, label, value, () =>
      openEntities(scope, part === undefined ? null : { part, title: label }),
    );
    const button = row.querySelector<HTMLButtonElement>("button");
    if (button !== null) {
      button.dataset["editEntities"] = scope;
    }
    return row;
  }

  function rowHelp(key: string, text: string): HTMLElement {
    const help = element(doc, "p", C.settingRowHelp, text);
    help.dataset["help"] = key;
    return help;
  }

  /**
   * One value's own editor in the value dialog: Settings steps aside, and comes back on Cancel or a save.
   * Only an administrator's, except for a display preference of this device (`anyone`).
   */
  function openValueEditor(
    title: string,
    build: (handlers: EditorHandlers) => HTMLFormElement | { form: HTMLFormElement; close: () => void },
    options: { anyone?: boolean } = {},
  ): void {
    if (destroyed || (!input.isAdmin && options.anyone !== true)) {
      return;
    }
    hideForChildDialog();
    const handlers: EditorHandlers = {
      onDone: () => {
        if (valueDialog.isOpen()) {
          valueDialog.hide({ restoreFocus: false });
          openSettingsOverview();
        }
      },
      onCancel: () => leaveSettingsChild(valueDialog),
    };
    const built = build(handlers);
    valueLeave = "close" in built ? built.close : null;
    const valueBody = "close" in built ? built.form : built;
    // The report form swaps its whole content when sent, so its buttons stay in it.
    const valueActions = valueBody.dataset["sendDebug"] === "form" ? null : takeActions(valueBody, `${idPrefix}-value`);
    valueDialog.show({ title, body: valueBody, opener: settingsGeneral, actions: valueActions });
  }

  function writeValue(write: ValueWrite): Promise<string | null> {
    return input.onWriteValue?.(write) ?? Promise.resolve(translate(model.language, "settings.error.generic"));
  }

  function editNumber(
    title: string,
    spec: Omit<NumberEditorInput, "idPrefix" | "rangeMessage">,
    save: (value: number | null) => Promise<string | null>,
  ): void {
    const rangeMessage = translate(model.language, "settings.error.range", {
      min: formatNumber(model.language, spec.min, spec.decimals),
      max: formatNumber(model.language, spec.max, spec.decimals),
    });
    openValueEditor(title, (handlers) =>
      numberEditor(doc, model.language, { ...spec, idPrefix, rangeMessage }, save, handlers),
    );
  }

  function editSingle(
    title: string,
    options: readonly ChoiceOption[],
    selected: string | null,
    save: (value: string) => Promise<string | null>,
    intro?: string,
    status?: string,
  ): void {
    openValueEditor(title, (handlers) =>
      singleEditor(
        doc,
        model.language,
        { ...(intro === undefined ? {} : { intro }), ...(status === undefined ? {} : { status }), options, selected, idPrefix },
        save,
        handlers,
      ),
    );
  }

  function editMulti(
    title: string,
    options: readonly ChoiceOption[],
    checked: readonly string[],
    save: (values: string[]) => Promise<string | null>,
    extra: { intro?: string; atLeastOne?: string } = {},
  ): void {
    openValueEditor(title, (handlers) =>
      multiEditor(doc, model.language, { ...extra, options, checked }, save, handlers),
    );
  }

  /** The host side of the shared sections (`price-section.ts`): this view's rows and editors. */
  function sectionHost(): SectionHost {
    return {
      doc,
      language: model.language,
      idPrefix,
      isAdmin: input.isAdmin,
      row: overviewRow,
      rowHelp,
      openValueEditor: (title, build) => openValueEditor(title, build),
      editNumber,
      editSingle: (title, options, selected, save, intro) => editSingle(title, options, selected, save, intro),
      editMulti: (title, options, checked, save) => editMulti(title, options, checked, save),
    };
  }

  /** The price section (`price-section.ts`), from the dashboard's price blocks; writes through the card. */
  function priceSectionBody(): HTMLElement {
    return sharedPriceSection({
      ...sectionHost(),
      facts: {
        format: model.format,
        priceEntry: model.priceEntry,
        gridTariff: model.gridTariff,
        fiscal: model.dashboardFiscal,
        provider: model.priceProvider,
        areaId: model.contextAreaId,
        areaName: model.contextAreaName,
        onSite: model.site !== null,
      },
      write: (write) => writeValue(write),
      openArea: () => input.onOpenMarket(),
      editFiscal: (component) => input.onEditFiscal?.(component),
      hasComponent: (domain) => hasComponent(input.hass?.(), domain),
      navigate: (path) => {
        settingsOverviewDialog.hide({ restoreFocus: false });
        navigateTo(doc, path);
      },
      openExternal: (url) => {
        doc.defaultView?.open(url, "_blank", "noopener,noreferrer");
      },
    });
  }

  const settingsPage: SettingsPageState = {
    overviewBodyNode: null,
    entityState: input.isAdmin ? { kind: "loading" } : { kind: "adminOnly" },
    entitySlot: null,
    vehicleListSlot: null,
    vehicleRows: [],
    siteEntitySlot: null,
  };
  const viewContext: ViewContext = {
    input,
    model,
    doc,
    idPrefix,
    now,
    isDestroyed: () => destroyed,
    dialogs: {
      issues: issuesDialog,
      release: releaseDialog,
      capability: capabilityDialog,
      pause: pauseDialog,
      strategy: strategyDialog,
      vehicle: vehicleDialog,
      settings: settingsDialog,
      market: marketDialog,
      entity: entityDialog,
      settingsOverview: settingsOverviewDialog,
      history: historyDialog,
      value: valueDialog,
    },
    bar,
    settingsError,
    settingsGeneral,
    historyButton,
    fit,
    leaveSettingsChild,
    entityFocus: () => entityFocus,
    openSiteSettings,
  };
  const dialogOpeners = createDialogOpeners(viewContext, settingsPage, {
    mayPlan,
    setTimer,
    plannerButton: () => plannerButton,
    strategyButton: () => strategyButton,
    hideForChildDialog,
    openEntities,
    openSettingsOverview,
    overviewPanel,
    expandHeat: () => {
      if (siteSections !== null) {
        siteSections.heatExpanded = true;
      }
    },
    setCapabilityFromSettings: (fromSettings) => {
      capabilityFromSettings = fromSettings;
    },
  });
  const { openIssues, openSettingAt, confirmRelease, openCapabilities, openAutomations, openPause, openStrategy } = dialogOpeners;

  const { paintEntities } = createEntityRowsSection(viewContext, settingsPage, {
    writeValue,
    overviewRow,
    rowHelp,
    entityRow,
    openValueEditor,
    editNumber,
    editSingle,
    editMulti,
    siteFollowsSolar,
    importTargetShown,
    sitePriority: () => siteSections?.priority ?? null,
  });
  const supportSection = createSupportSection(viewContext, {
    overviewRow,
    openValueEditor,
    openAutomations,
    openCapabilities,
  });
  const { displaySectionBody, supportSectionBody } = supportSection;

  const siteSectionsBuilt = createSiteSections(viewContext, settingsPage, {
    overviewRow,
    rowHelp,
    openValueEditor,
    editNumber,
    editSingle,
    editMulti,
    writeValue,
    entityRow,
    sectionHost,
  });
  const { siteSectionBody, solarSectionBody } = siteSectionsBuilt;
  // One notification section for the whole site, per phone (`notification-phones.ts`): it reads the phones itself.
  const notificationPhones = new NotificationPhones({
    doc,
    idPrefix,
    hass: () => (input.hass?.() ?? null) as HomeAssistantLike | null,
    language: () => model.language,
    openEditor: (title, build) => openValueEditor(title, build),
  });


  let overviewNotice: FailureSentence | null = null;
  let overviewNoticeNode: HTMLParagraphElement | null = null;

  function paintOverviewNotice(): void {
    if (overviewNoticeNode === null) {
      return;
    }
    if (overviewNotice === null) {
      overviewNoticeNode.hidden = true;
      overviewNoticeNode.textContent = "";
      overviewNoticeNode.removeAttribute("data-code");
      return;
    }
    overviewNoticeNode.hidden = false;
    overviewNoticeNode.textContent = translate(model.language, overviewNotice.sentenceKey);
    if (overviewNotice.code === null) {
      overviewNoticeNode.removeAttribute("data-code");
    } else {
      overviewNoticeNode.dataset["code"] = overviewNotice.code;
    }
  }

  function openSettingsOverview(): void {
    if (destroyed || !settingsChangeable()) {
      return;
    }
    overviewNotice = null;
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    entityDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.show({
      title: translate(model.language, "settings.overview.title"),
      body: settingsOverviewBody(),
      opener: settingsGeneral,
    });
    // Back from a value's editor: the page where it was, scrolled as it was, the row that opened it focused.
    const place = input.overviewPlace?.place ?? null;
    if (input.overviewPlace !== undefined) {
      input.overviewPlace.place = null;
    }
    if (place !== null) {
      const panel = overviewPanel();
      const row =
        place.edit === null
          ? null
          : (Array.from(panel.querySelectorAll<HTMLElement>("[data-edit]")).find((node) => node.dataset["edit"] === place.edit) ?? null);
      row?.focus({ preventScroll: true });
      panel.scrollTop = place.scrollTop;
    }
    loadSiteSections();
    input.onSettingsOverviewOpened?.();
  }

  const historySection = createHistorySection(viewContext);
  const { openHistory } = historySection;

  historyButton.addEventListener("click", () => {
    openHistory();
  });
  settingsGeneral.addEventListener("click", () => {
    // Opened afresh: from the top.
    if (input.overviewPlace !== undefined) {
      input.overviewPlace.place = null;
    }
    openSettingsOverview();
  });

  const settingsEditorSection = createSettingsEditorSection(viewContext);
  const {
    openSettingsEditor,
    showSettingsEditorForm,
    showSettingsEditorConflict,
    setSettingsEditorNotice,
    setSettingsEditorPending,
    setSettingsPreview,
    settingsEditorOpen,
    closeSettingsEditor,
    setSettingsError,
  } = settingsEditorSection;

  const marketEditorSection = createMarketEditorSection(viewContext);
  const {
    openMarketEditor,
    showMarketEditorForm,
    showMarketEditorConflict,
    setMarketEditorNotice,
    setMarketEditorPending,
    marketEditorOpen,
    closeMarketEditor,
  } = marketEditorSection;

  const entityEditorSection = createEntityEditorSection(viewContext);
  const { openEntityEditor, entityEditorOpen, closeEntityEditor } = entityEditorSection;

  /** The scrolling panel of the Settings page. */
  function overviewPanel(): HTMLElement {
    return settingsOverviewDialog.element.querySelector<HTMLElement>(`.${C.dialogScroll}`) ?? settingsOverviewDialog.element;
  }

  function hideForChildDialog(): void {
    if (settingsOverviewDialog.isOpen() && input.overviewPlace !== undefined) {
      const panel = overviewPanel();
      const active = (panel.getRootNode() as Document | ShadowRoot).activeElement as HTMLElement | null;
      input.overviewPlace.place = {
        scrollTop: panel.scrollTop,
        edit: active !== null && panel.contains(active) ? (active.dataset["edit"] ?? null) : null,
      };
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
  }


  return {
    element: card,
    chart: () => interaction as ChartInteraction,
    selection: () => interaction?.selection() ?? null,
    readoutText: () => readout.textContent ?? "",
    // The opener is what focus returns to; the banner passes itself when it is clicked.
    openIssues: (opener: HTMLElement | null = null) => {
      openIssues(opener);
    },
    openCapabilities,
    openPause,
    openStrategy,
    openSettingsOverview,
    openHistory,
    setHistoryState: historySection.setHistoryState,
    setPowerState: historySection.setPowerState,
    setHistoryExport: historySection.setHistoryExport,
    dialogOpen: (kind) => {
      if (kind === "issues") {
        return issuesDialog.isOpen();
      }
      if (kind === "capabilities") {
        return capabilityDialog.isOpen();
      }
      if (kind === "pause") {
        return pauseDialog.isOpen();
      }
      if (kind === "strategy") {
        return strategyDialog.isOpen();
      }
      if (kind === "history") {
        return historyDialog.isOpen();
      }
      return settingsOverviewDialog.isOpen();
    },
    setActionPending(pending: boolean, action?: ActionId, choice?: string | null): void {
      // Both controls, always together: one physical request is in flight, and an axis that stayed
      // clickable while it was would be an invitation to send a second one. `pending` disables what
      // the row rendered; it never enables a cell that was rendered disabled. The cell that was
      // pressed stays in place and is marked busy until the answer arrives.
      const pressed =
        action === undefined ? null : action === "resume" || (choice ?? null) !== null ? plannerButton : actionButton;
      for (const button of [actionButton, plannerButton]) {
        if (button === null) {
          continue;
        }
        if (button === actionButton) {
          // A Start or Stop just sent says at once what is under way, as the cell will while Home
          // Assistant awaits the charger's report; a refused one gets its own word back.
          underWay(button, pending && button === pressed && (action === "start" || action === "stop") ? action : null);
        }
        button.disabled = pending || button.dataset["renderedDisabled"] === "true";
        const busy = (pending && button === pressed) || button.dataset["waiting"] === "true";
        button.classList.toggle(C.barBusy, busy);
        if (busy) {
          button.setAttribute("aria-busy", "true");
        } else {
          button.removeAttribute("aria-busy");
        }
      }
    },
    openSettingsEditor,
    showSettingsEditorForm,
    setSettingsEditorNotice,
    showSettingsEditorConflict,
    setSettingsEditorPending,
    setSettingsPreview,
    settingsEditorOpen,
    closeSettingsEditor,
    setSettingsError,
    openMarketEditor,
    showMarketEditorForm,
    setMarketEditorNotice,
    showMarketEditorConflict,
    setMarketEditorPending,
    marketEditorOpen,
    closeMarketEditor,
    setActiveControlPending: siteSectionsBuilt.setActiveControlPending,
    adoptActiveControl: siteSectionsBuilt.adoptActiveControl,
    anyDialogOpen: anyDialogOpenNow,
    setEntityState(state: EntityViewState): void {
      settingsPage.entityState = state;
      paintEntities();
    },
    openEntityEditor,
    closeValueEditor(): void {
      valueDialog.hide({ restoreFocus: false });
    },
    openFiscalEditor(component: FiscalComponentName, facts: { current: number | null; suggestion: number | null }): void {
      const fiscal = model.dashboardFiscal?.[component];
      const label = translate(
        model.language,
        component === "vat" ? "settings.fiscal.vat" : component === "tax" ? "settings.fiscal.tax" : "settings.fiscal.transfer",
      );
      editNumber(
        label,
        {
          unit: fiscal?.unit ?? "",
          current: facts.current,
          min: 0,
          max: component === "vat" ? 100 : 100000,
          decimals: 2,
          noneLabel: translate(model.language, "settings.value.off"),
          suggestion: facts.suggestion,
        },
        (value) => writeValue({ kind: "fiscal", component, value }),
      );
    },
    entityEditorOpen,
    setEntityEditorNotice: entityEditorSection.setEntityEditorNotice,
    markEntityFieldErrors: entityEditorSection.markEntityFieldErrors,
    setEntityEditorPending: entityEditorSection.setEntityEditorPending,
    setEntityHass: entityEditorSection.setEntityHass,
    closeEntityEditor,
    setDebugPending: supportSection.setDebugPending,
    setCardOutdated: supportSection.setCardOutdated,
    setOverviewNotice(failure: FailureSentence | null): void {
      overviewNotice = failure;
      paintOverviewNotice();
    },
    setHomeOffered: siteSectionsBuilt.setHomeOffered,
    showPriceRemovableNote(): void {
      const section = settingsPage.overviewBodyNode?.querySelector<HTMLElement>("[data-section='market']");
      if (section === null || section === undefined || section.querySelector("[data-price-removable]") !== null) {
        return;
      }
      const note = element(doc, "p", C.siteApplies, translate(model.language, "price.duplicate.removable"));
      note.dataset["priceRemovable"] = "";
      note.setAttribute("role", "status");
      const sourceLine = section.querySelector("[data-price-source]");
      if (sourceLine === null) {
        section.append(note);
      } else {
        sourceLine.after(note);
      }
    },
    closeSettingsOverview(): void {
      settingsOverviewDialog.hide({ restoreFocus: false });
    },
    setUpdateNotice(notice: HTMLElement | null): void {
      card.querySelector(":scope > [data-card-updated]")?.remove();
      if (notice !== null) {
        card.prepend(notice);
      }
      fit();
    },
    setActionError(failure: FailureSentence | null): void {
      if (failure === null) {
        actionError.hidden = true;
        actionError.textContent = "";
        actionError.removeAttribute("data-code");
        fit();
        return;
      }
      actionError.hidden = false;
      actionError.textContent = translate(model.language, failure.sentenceKey);
      if (failure.code === null) {
        actionError.removeAttribute("data-code");
      } else {
        actionError.dataset["code"] = failure.code;
      }
      fit();
    },
    destroy(): void {
      if (destroyed) {
        return;
      }
      destroyed = true;
      // The boundary appointment is released before anything else: a callback that fired after this
      // would belong to a view that no longer exists.
      cancelBoundary();
      boxObserver?.disconnect();
      headerObserver?.disconnect();
      if (statusFrame !== null) {
        (globalThis as { cancelAnimationFrame?: (handle: number) => void }).cancelAnimationFrame?.(statusFrame);
        statusFrame = null;
      }
      interaction?.destroy();
      issuesDialog.destroy();
      releaseDialog.destroy();
      capabilityDialog.destroy();
      pauseDialog.destroy();
      strategyDialog.destroy();
      vehicleDialog.destroy();
      settingsDialog.destroy();
      marketDialog.destroy();
      entityDialog.destroy();
      settingsOverviewDialog.destroy();
      historyDialog.destroy();
      valueDialog.destroy();
      card.remove();
    },
  };
}
