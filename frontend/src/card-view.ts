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

import { chartNowAt, nextIntervalBoundary, type ChartMark, type ChartNow } from "./chart";
import {
  createChartInteraction,
  FALLBACK_SIZE,
  type ChartInteraction,
  type ChartObserverLike,
  type ChartSize,
} from "./chart-interaction";
import { applyFocus, chartHeightForWidth, renderChart, type ChartLabels } from "./chart-render";
import { stripBarPlacement, stripBars, stripNowPosition, stripTicks } from "./chart-strip";
import { createDialog, type DialogHandle } from "./dialog";
import {
  changeCarBody,
  identificationBanner,
  identificationSummary,
  methodWords,
  sourceChoice,
} from "./identification";
import { eventOptions, noRecipientsNote, notificationsSummary, phoneOptions } from "./notifications";
import { clock, formatFixed, formatNumber, hasZone, percentAmount, pricePerKwh, wallTimeRepeats, weekdayDate } from "./format";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import {
  actionErrorKey,
  type CardModel,
  type Issue,
  type PlanPeriod,
  type PlanRelationKind,
  type SiteFacts,
} from "./model";
import type { ActiveControlNotice } from "./site-settings";
import {
  conflictText,
  entityEditorBody,
  entityNameIn,
  modeLabel,
  siteWarningRows,
  type EntityEditorBody,
} from "./entity-editor";
import {
  fieldsOf,
  isMissingEntity,
  storedMode,
  vehicleChoice,
  type EntityConfig,
  type EntityDraft,
  type EntityFieldError,
  type EntityScope,
} from "./entity-config";
import { marketAreaLabel, type MarketFormValues } from "./market";
import { type RegionLookup } from "./market-editor";
import {
  marketEditorBody,
  type MarketEditorForm,
} from "./market-editor";
import { settingsEditorBody, settingsTrigger, type SettingsEditorForm } from "./settings-editor";
import type { Vehicle } from "./validate";
import { vehicleSummary, type VehicleEdits } from "./vehicle-settings";
import {
  multiEditor,
  numberEditor,
  onOffEditor,
  sectionHeading,
  settingRow,
  singleEditor,
  type ChoiceOption,
  type EditorHandlers,
  type NumberEditorInput,
} from "./value-editors";
import type { FiscalComponentName, ValueWrite } from "./value-writes";
import type { IdentifyMode } from "./types";
import { issueText } from "./status";
import { historyBody, type HistoryState, type HistoryUi } from "./history";
import { connectionLabel, vehicleLineFor } from "./vehicle-line";
import type { ChargeBarFacts } from "./charge-bar";
import {
  CAPACITY_MAX_KWH,
  CAPACITY_MIN_KWH,
  CONSUMPTION_MAX_KWH_PER_10KM,
  CONSUMPTION_MIN_KWH_PER_10KM,
  fiscalRows,
  identificationReplacement,
  notificationsReplacement,
  planSummaryParts,
  type SettingsEditorKind,
  type SettingsFormValues,
} from "./settings";
import { VISUAL_CLASSES as C } from "./visual-styles";

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
  setTimer?: (callback: () => void, delayMs: number) => number;
  clearTimer?: (handle: number) => void;
  /** Start with the chart collapsed to its 24-hour strip (the card's remembered or configured choice). */
  chartCollapsed?: boolean;
  /** The price row was pressed and the chart collapsed (`true`) or expanded; the card remembers it. */
  onChartCollapsedChange?: (collapsed: boolean) => void;
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
  onOpenEntityEditor?: (scope: EntityScope) => void;
  /** The admin pressed "Download debug info" in the Settings popover. */
  onDownloadDebug?: () => void;
  onSaveEntities?: (scope: EntityScope, draft: EntityDraft) => void;
  onCancelEntities?: () => boolean | void;
  /** The one-tap answer to "set its onboard charger to 1-phase?": `1` accepts, `3` keeps it as it was. */
  onAnswerOnboardPhases?: (vehicleId: string, phases: 1 | 3) => void;
  /** The one-tap answer to "which car is plugged in?". */
  onAnswerIdentification?: (vehicleId: string) => void;
  /** One value of the Settings page: the card writes it and answers `null`, or the sentence to show. */
  onWriteValue?: (write: ValueWrite) => Promise<string | null>;
  /** A fee's row: the card reads the area's suggestion and opens its editor (`openFiscalEditor`). */
  onEditFiscal?: (component: FiscalComponentName) => void;
}

export type EntityViewState =
  | { kind: "loading" }
  | { kind: "adminOnly" }
  | { kind: "failed"; failure: FailureSentence }
  | { kind: "ready"; config: EntityConfig };

export type ActionId = "start" | "stop" | "resume";

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
  /** The debug download is being prepared: the button says so and cannot be pressed again. */
  setDebugPending(pending: boolean): void;
  /** Whether the card running here differs from the one the integration serves: Support then says to reload. */
  setCardOutdated(outdated: boolean): void;
  /** Close the Settings popover without returning focus (the card is about to redraw it). */
  closeSettingsOverview(): void;
  /** Whether a value editor is open. */
  valueEditorOpen(): boolean;
  /** Close the value editor after its write took (the card then reads the dashboard and shows Settings). */
  closeValueEditor(): void;
  /** A fee's own number editor, with the area's suggestion the card read for it. */
  openFiscalEditor(component: FiscalComponentName, facts: { current: number | null; suggestion: number | null }): void;
}

/**
 * The furthest ahead an interval-boundary appointment is worth keeping (ms). Anything past a day
 * would be replaced by the card's own refresh first, and a platform timer would clamp it; every
 * refresh arms a fresh one.
 */
export const BOUNDARY_HORIZON_MS = 24 * 3_600_000;

function element<K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  className?: string,
  text?: string,
): HTMLElementTagNameMap[K] {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (text !== undefined) {
    created.textContent = text;
  }
  return created;
}

export function readoutDay(model: CardModel, mark: ChartMark): string {
  return hasZone(model.format) ? weekdayDate(model.format, mark.startMs) : "";
}

/**
 * One selection readout: day, time, price. The UTC offset appears only when the wall time is
 * ambiguous ("02:15 (GMT+2)"); a price with no market zone reads as the model's "unknown".
 */
export function readoutTextFor(model: CardModel, mark: ChartMark | null): string {
  const language = model.language;
  if (mark === null) {
    return translate(language, "graph.descriptionNoSelection");
  }
  const day = readoutDay(model, mark);
  const time = mark.wallClock;
  const zone = hasZone(model.format);
  const ambiguous = zone && wallTimeRepeats(model.format, mark.startMs);
  const offset = ambiguous ? mark.offset : "";
  const price = zone ? pricePerKwh(model.format, mark.price) : "";
  if (price === "") {
    const key: TranslationKey = ambiguous ? "graph.readoutMissingWithOffset" : "graph.readoutMissing";
    return translate(language, key, { day, time, offset }).trim();
  }
  const key: TranslationKey = ambiguous ? "graph.readoutWithOffset" : "graph.readout";
  return translate(language, key, { day, time, offset, price }).trim();
}

/**
 * The visible readout line: the full sentence for a selection, or empty for none, so the aria-live
 * region stays silent instead of showing "Nothing selected." (`graphDescription` still says it, as
 * the whole-picture description).
 */
function readoutDisplayText(model: CardModel, mark: ChartMark | null): string {
  return mark === null ? "" : readoutTextFor(model, mark);
}

/**
 * The figure for the summary's Now label at one interval, or `null` when there is no containing
 * interval or no market zone (the line is then omitted, never shown as "unknown").
 */
function currentPriceText(model: CardModel, mark: ChartMark | null): string | null {
  return mark === null || !hasZone(model.format) ? null : pricePerKwh(model.format, mark.price);
}

/**
 * The summary's "Now" line: label and formatted figure, or `null` to hide the whole line. The label
 * is translated here so a redraw never drops it.
 */
function nowSummaryText(model: CardModel, figure: string | null): string | null {
  return figure === null ? null : `${translate(model.language, "graph.summary.current")} ${figure}`;
}

/** `hass.config.country`, when Home Assistant states one. */
function homeAssistantCountry(hass: unknown): string | null {
  if (typeof hass !== "object" || hass === null) {
    return null;
  }
  const config = (hass as { config?: unknown }).config;
  const country = typeof config === "object" && config !== null ? (config as { country?: unknown }).country : undefined;
  return typeof country === "string" && country.trim() !== "" ? country : null;
}

export function bannerSeverity(model: CardModel): "blocking" | "notice" | null {
  return model.severity;
}

/**
 * A neutral banner whose issues are all already worded in the status headline adds nothing: the
 * headline says it. A blocking banner is always kept.
 */
export function bannerRepeatsStatus(model: CardModel, severity: "blocking" | "notice"): boolean {
  if (severity !== "notice" || model.status === null || model.issues.length === 0) {
    return false;
  }
  const strip = (text: string): string => text.trim().replace(/[.。]$/u, "");
  const shown = model.status.split(" \u00b7 ").map(strip);
  return model.issues.every((issue) => shown.includes(strip(issueText(model.language, issue))));
}

/** The strategy rows whose availability hangs on the site's solar setup. */
const SOLAR_SETUP_ROWS: ReadonlySet<string> = new Set(["solar", "hybrid"]);
/** The reason code of a solar strategy held back because a direct site lacks the meter's total grid power. */
const STRATEGY_NEEDS_TOTAL_POWER = "needs_total_grid_power";

export function issueCountText(language: Language, count: number): string {
  const key: TranslationKey = pluralForm(language, count) === "one" ? "issue.count.one" : "issue.count.other";
  return translate(language, key, { count: String(count) });
}

/**
 * Which period blocks to draw, from the model's relation, never from issue presence. `applied_same`
 * is one block (proposal equals what runs); a lone section is drawn alone; a pending proposal
 * follows the running schedule.
 */
export function periodBlockOrder(model: CardModel): Array<"proposal" | "installed"> {
  const table: Record<PlanRelationKind, Array<"proposal" | "installed">> = {
    none: [],
    proposal_only: ["proposal"],
    installed_only: ["installed"],
    applied_same: ["proposal"],
    pending_beside_installed: ["installed", "proposal"],
  };
  return table[model.planRelation].filter((kind) =>
    kind === "installed" ? model.installedPeriods.length > 0 : model.proposalPeriods.length > 0,
  );
}

function periodHeading(model: CardModel, base: "plan.proposal" | "plan.installed", count: number): string {
  const form = pluralForm(model.language, count);
  return translate(model.language, `${base}.${form}` as TranslationKey, { count: String(count) });
}

function periodLine(doc: Document, model: CardModel, period: PlanPeriod): HTMLElement {
  const line = element(doc, "div", C.period);
  // The model's own offset-disambiguated label; when the market had no zone the model says so.
  line.textContent = period.label === "" ? translate(model.language, "plan.missing") : period.label;
  return line;
}

function periodBlock(
  doc: Document,
  model: CardModel,
  kind: "proposal" | "installed",
  periods: readonly PlanPeriod[],
): HTMLElement {
  const block = element(
    doc,
    "section",
    `${C.periods} ${kind === "installed" ? C.periodsInstalled : C.periodsProposal}`,
  );
  const heading = element(
    doc,
    "h4",
    C.periodsHeading,
    periodHeading(model, kind === "installed" ? "plan.installed" : "plan.proposal", periods.length),
  );
  block.append(heading);
  periods.forEach((period, index) => {
    const line = periodLine(doc, model, period);
    // `activeNow` is only ever set on an installed period by the model, and is asserted here too.
    const active = kind === "installed" && period.activeNow;
    if (active) {
      line.classList.add(C.periodActive);
      line.dataset["activeIndex"] = String(index);
    }
    block.append(line);
  });
  return block;
}

/**
 * The product mark: a dark rounded square with the red-yellow-green bolt, readable on light and
 * dark themes. Hidden from assistive technology and built with `createElementNS`; gradient and
 * filter ids carry the instance prefix so several cards never share a `url(#id)`.
 */
const MARK_PATH =
  "m214 42-6 62-18-10-23 49 47 3-86.00154 76.23385L142 232l-50 32 6-62 13.99846 9.52923L139 163l-47-3 " +
  "81.29385-75.527692-16.59077-9.06z";

export function brandMark(doc: Document, idPrefix: string): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const make = (tag: string, attributes: Record<string, string>): SVGElement => {
    const node = doc.createElementNS(ns, tag) as SVGElement;
    for (const [name, value] of Object.entries(attributes)) {
      node.setAttribute(name, value);
    }
    return node;
  };
  const stop = (offset: string, color: string): SVGElement => make("stop", { offset, "stop-color": color });
  const backgroundId = `${idPrefix}-mark-background`;
  const boltId = `${idPrefix}-mark-bolt`;
  const haloId = `${idPrefix}-mark-halo`;

  const svg = make("svg", {
    viewBox: "0 0 306 306",
    width: "22",
    height: "22",
    "aria-hidden": "true",
    focusable: "false",
  });
  svg.classList.add(C.identity);

  const background = make("radialGradient", {
    id: backgroundId,
    cx: "106.158",
    cy: "79.888",
    r: "261",
    gradientUnits: "userSpaceOnUse",
  });
  background.append(stop("0", "#454d55"), stop(".42", "#1b1e22"), stop(".76", "#101216"), stop("1", "#080a0d"));
  const bolt = make("linearGradient", {
    id: boltId,
    x1: "155.35",
    y1: "4.69",
    x2: "129.22",
    y2: "303.87",
    gradientUnits: "userSpaceOnUse",
  });
  bolt.append(
    stop("0", "#ff0000"),
    stop(".102", "#ff0000"),
    stop(".43", "#ffd22e"),
    stop(".57", "#ffe56a"),
    stop(".904", "#00ff06"),
    stop("1", "#00ff00"),
  );
  const halo = make("filter", { id: haloId, x: "-30%", y: "-30%", width: "160%", height: "160%" });
  halo.append(make("feGaussianBlur", { stdDeviation: "3.2" }));
  const defs = make("defs", {});
  defs.append(background, bolt, halo);

  svg.append(
    defs,
    make("rect", { x: "8", y: "8", width: "290", height: "290", rx: "70", fill: `url(#${backgroundId})` }),
    make("path", {
      d: MARK_PATH,
      fill: "none",
      stroke: "#ffe36b",
      "stroke-width": "8",
      "stroke-opacity": ".36",
      "stroke-linejoin": "round",
      filter: `url(#${haloId})`,
    }),
    make("path", {
      d: MARK_PATH,
      fill: `url(#${boltId})`,
      stroke: "#ffe36b",
      "stroke-width": "1.2",
      "stroke-opacity": ".52",
      "stroke-linejoin": "round",
    }),
  );
  return svg;
}

/** One small path-only SVG icon, decorative and hidden from assistive technology. */
function icon(doc: Document, build: (svg: SVGElement, ns: string) => void): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add(C.actionIcon);
  build(svg, ns);
  return svg;
}

function strokePath(ns: string, doc: Document, d: string): SVGPathElement {
  const path = doc.createElementNS(ns, "path") as SVGPathElement;
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  return path;
}

function fillPath(ns: string, doc: Document, d: string): SVGPathElement {
  const path = doc.createElementNS(ns, "path") as SVGPathElement;
  path.setAttribute("d", d);
  path.setAttribute("fill", "currentColor");
  return path;
}

/**
 * The settings cog: MDI `mdiCog` path data (`@mdi/js`, Apache License 2.0), one filled path unlike
 * this file's usual stroked primitives.
 */
function historyIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(
      strokePath(ns, doc, "M3.5 12a8.5 8.5 0 1 0 2.6-6.1"),
      strokePath(ns, doc, "M3.5 4.5v4.5H8"),
      strokePath(ns, doc, "M12 7.5V12l3 2"),
    );
  });
}

function settingsGearIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(
      fillPath(
        ns,
        doc,
        "M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43," +
          "12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11L21.54,9.37C21.73,9.22 21.78,8.95 " +
          "21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5," +
          "2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95," +
          "5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.22,8.95 2.27,9.22 2.46,9.37L4.57,11C4.53,11.34 4.5,11.67 " +
          "4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.22,15.05 2.34,15.27L4.34,18.73C4.46," +
          "18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 " +
          "10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.68 16.04,18.34 16.56,17.94L19.05,18.95C" +
          "19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z",
      ),
    );
  });
}

/** A small battery glyph for the vehicle line. */
/** Byt bil's ⇄ at the end of the car line. */
function swapIcon(doc: Document): SVGElement {
  const svg = icon(doc, (svg, ns) => {
    svg.append(strokePath(ns, doc, "M5 8h13M15 5l3 3-3 3M19 16H6M9 13l-3 3 3 3"));
  });
  svg.dataset["icon"] = "swap";
  return svg;
}

function batteryIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    const body = doc.createElementNS(ns, "path");
    body.setAttribute("d", "M4 8h13a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1zM19 11h1.5v2H19z");
    body.setAttribute("fill", "none");
    body.setAttribute("stroke", "currentColor");
    body.setAttribute("stroke-width", "1.6");
    body.setAttribute("stroke-linejoin", "round");
    const level = doc.createElementNS(ns, "path");
    level.setAttribute("d", "M5.5 10h7v4h-7z");
    level.setAttribute("fill", "currentColor");
    svg.append(body, level);
  });
}

function pauseIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(fillPath(ns, doc, "M7 5h3v14H7zM14 5h3v14h-3z"));
  });
}

function playIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(fillPath(ns, doc, "M8 5v14l11-7Z"));
  });
}

function stopIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(fillPath(ns, doc, "M7 7h10v10H7z"));
  });
}

function filledEllipse(ns: string, doc: Document, cx: number, cy: number, rx: number, ry: number): SVGEllipseElement {
  const ellipse = doc.createElementNS(ns, "ellipse") as SVGEllipseElement;
  ellipse.setAttribute("cx", String(cx));
  ellipse.setAttribute("cy", String(cy));
  ellipse.setAttribute("rx", String(rx));
  ellipse.setAttribute("ry", String(ry));
  ellipse.setAttribute("fill", "currentColor");
  return ellipse;
}

function filledRect(ns: string, doc: Document, x: number, y: number, width: number, height: number): SVGRectElement {
  const rect = doc.createElementNS(ns, "rect") as SVGRectElement;
  rect.setAttribute("x", String(x));
  rect.setAttribute("y", String(y));
  rect.setAttribute("width", String(width));
  rect.setAttribute("height", String(height));
  rect.setAttribute("rx", "0.6");
  rect.setAttribute("fill", "currentColor");
  return rect;
}

/** Piggy-bank glyph (Cheapest strategy): a bold filled silhouette that stays legible at 16-18 px. */
function piggyBankPath(ns: string, doc: Document): SVGElement[] {
  return [
    filledEllipse(ns, doc, 10.5, 13.5, 7.5, 5.5),
    filledEllipse(ns, doc, 18, 13.5, 2.3, 1.9),
    fillPath(ns, doc, "M6.2 8.4 9.8 6.6 8.7 10.8Z"),
    filledRect(ns, doc, 5.8, 17.6, 1.8, 3.2),
    filledRect(ns, doc, 10.2, 18.2, 1.8, 3.2),
    filledRect(ns, doc, 14.6, 17.6, 1.8, 3.2),
  ];
}

function sunPath(ns: string, doc: Document): (SVGCircleElement | SVGPathElement)[] {
  const circle = doc.createElementNS(ns, "circle") as SVGCircleElement;
  circle.setAttribute("cx", "12");
  circle.setAttribute("cy", "12");
  circle.setAttribute("r", "3.4");
  circle.setAttribute("fill", "none");
  circle.setAttribute("stroke", "currentColor");
  circle.setAttribute("stroke-width", "2");
  const rays = strokePath(
    ns,
    doc,
    "M12 3v2.4M12 18.6V21M21 12h-2.4M5.4 12H3M18.1 5.9l-1.7 1.7M7.6 16.4l-1.7 1.7M18.1 18.1l-1.7-1.7M7.6 7.6 5.9 5.9",
  );
  return [circle, rays];
}

/** The strategy trigger's own icon: piggy bank, sun, or a legible piggy-bank-and-sun for Hybrid. */
function strategyIcon(doc: Document, strategyId: string | null): SVGElement | null {
  if (strategyId === "cheapest") {
    return icon(doc, (svg, ns) => svg.append(...piggyBankPath(ns, doc)));
  }
  if (strategyId === "solar") {
    return icon(doc, (svg, ns) => svg.append(...sunPath(ns, doc)));
  }
  if (strategyId === "hybrid") {
    // The same two marks, scaled and offset so both read clearly at the small size a narrow card
    // allows -- one legible combined icon, rather than two full-size glyphs competing for the space.
    return icon(doc, (svg, ns) => {
      const sun = doc.createElementNS(ns, "g") as SVGGElement;
      sun.setAttribute("transform", "translate(1 -2) scale(0.62)");
      sun.append(...sunPath(ns, doc));
      const piggy = doc.createElementNS(ns, "g") as SVGGElement;
      piggy.setAttribute("transform", "translate(-2 4) scale(0.72)");
      piggy.append(...piggyBankPath(ns, doc));
      svg.append(sun, piggy);
    });
  }
  return null;
}

function capabilityStateText(language: Language, state: "available" | "unavailable"): string {
  return translate(language, state === "available" ? "cap.available" : "cap.unavailable");
}

/** The issue list, in the model's stable order, with codes only as subdued technical detail. */
export function issueListBody(doc: Document, model: CardModel): HTMLElement {
  const body = element(doc, "div");
  body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "dialog.issuesIntro")));
  for (const issue of model.issues) {
    body.append(issueRow(doc, model, issue));
  }
  return body;
}

function issueRow(doc: Document, model: CardModel, issue: Issue): HTMLElement {
  const row = element(doc, "div", C.issueItem);
  row.dataset["code"] = issue.code;
  row.dataset["severity"] = issue.severity;
  row.append(
    element(doc, "span", C.issueText, issueText(model.language, issue)),
  );
  return row;
}

/** The five capabilities, and nothing API v1 cannot prove about the charger. */
export function capabilityBody(doc: Document, model: CardModel): HTMLElement {
  const body = element(doc, "div");
  body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "cap.intro")));
  for (const item of model.capabilities) {
    const row = element(doc, "div", C.capabilityItem);
    row.dataset["capability"] = item.key;
    row.dataset["state"] = item.state;
    row.append(element(doc, "span", C.capabilityLabel, translate(model.language, item.labelKey)));
    row.append(element(doc, "span", C.capabilityState, capabilityStateText(model.language, item.state)));
    body.append(row);
    if (item.key === "target_soc" && item.state === "unavailable") {
      body.append(element(doc, "span", C.capabilityNote, translate(model.language, "cap.targetSocNote")));
    }
  }
  return body;
}

/** The graph's accessible name and description, from the model and the reader's own language. */
export function graphDescription(
  model: CardModel,
  selected: ChartMark | null,
  now: ChartMark | null,
  marks: readonly ChartMark[],
  days: number,
): string {
  const language = model.language;
  const zone = hasZone(model.format);
  const first = marks[0] ?? null;
  const last = marks[marks.length - 1] ?? null;
  const stamp = (instantMs: number | undefined, fallback: string): string =>
    instantMs === undefined || !zone
      ? fallback
      : `${weekdayDate(model.format, instantMs)} ${clock(model.format, instantMs)}`;
  const from = stamp(first?.startMs, translate(language, "plan.missing"));
  const to = stamp(last?.endMs, translate(language, "plan.missing"));
  const unit = model.format.unit === "" ? translate(language, "plan.missing") : model.format.unit;
  const selectedText = selected === null ? translate(language, "graph.descriptionNoSelection") : readoutTextFor(model, selected);
  const parts = [
    translate(language, "graph.description", {
      from,
      to,
      unit,
      count: String(marks.length),
      days: String(days),
      selected: selectedText,
    }),
    // The one focus fact a reader cannot see: which interval is current, said in words rather than
    // left to a decorative line the accessibility tree never receives.
    ...(now === null ? [] : [translate(language, "graph.descriptionNow", { now: readoutTextFor(model, now) })]),
    translate(language, "graph.keyboardInstructions"),
  ];
  if (!zone) {
    parts.push(translate(language, "graph.noZone"));
  }
  return parts.join(" ");
}

/**
 * The slim bar under the status line and its one line of words. The track is a `progressbar` whose
 * value is the share done (none for the open bar); the stripes drift only while `data-moving` is true,
 * and the stylesheet stops every motion when the system asks for less.
 */
export function chargeBarElement(doc: Document, bar: ChargeBarFacts): HTMLElement {
  const block = element(doc, "div", C.chargeBar);
  block.dataset["basis"] = bar.basis;
  block.dataset["moving"] = String(bar.moving);
  const track = element(doc, "div", C.chargeBarTrack);
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-label", bar.label);
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  if (bar.percent !== null) {
    track.setAttribute("aria-valuenow", String(bar.percent));
  }
  track.setAttribute("aria-valuetext", bar.text);
  const fill = element(doc, "div", C.chargeBarFill);
  if (bar.percent !== null) {
    fill.style.width = `${bar.percent}%`;
  }
  track.append(fill);
  const words = element(doc, "p", C.chargeBarLine, bar.text);
  words.setAttribute("aria-hidden", "true");
  block.append(track, words);
  return block;
}

export function createCardView(input: CardViewInput): CardView {
  const { model, idPrefix } = input;
  const doc = input.mount.ownerDocument;
  const now = input.now ?? (() => Date.now());
  let destroyed = false;

  const card = element(doc, "div", C.card);
  card.style.boxSizing = "border-box";

  // Header: the product mark and the charger's own name. One card is bound to one charger, so there is
  // no selector and no visible product name; the accessible name still names the product.
  const header = element(doc, "div", C.header);
  header.append(brandMark(doc, idPrefix));
  const vehicleLine = vehicleLineFor(model.language, model.soc, model.dashboardSettings);
  let vehicleButton: HTMLElement | null = null;
  // While the charge bar shows, it already says the charge runs: the connection line steps aside.
  const connectionText = model.chargeBar === null ? connectionLabel(model.language, model.connection) : null;
  const connectionClass = (): string =>
    model.connection?.state === "error" ? `${C.connectionLine} ${C.connectionError}` : C.connectionLine;
  if (vehicleLine !== null) {
    // The name and the planned vehicle stack in one block, so the line sits under the name.
    const identity = element(doc, "div", C.nameBlock);
    if (model.chargerName !== null) {
      identity.append(element(doc, "h3", C.name, model.chargerName));
    }
    // The car line is Byt bil wherever the car can be changed (more than one car can charge here): for every
    // user while a car is plugged in, since any user may answer which car it is. With no car plugged in the
    // choice is the plan's car, an administrator's setting, so a reader who is not one gets the plain line.
    const changeable =
      model.chargerCars.length >= 2 && (input.isAdmin || model.connection?.state !== "disconnected");
    const lineName = vehicleLine.name ?? translate(model.language, "settings.vehicle.unnamed");
    const changeLabel = translate(model.language, "identify.changeCarAria", { name: lineName });
    vehicleButton = element(doc, changeable ? "button" : "div", C.vehicleLine);
    if (changeable) {
      (vehicleButton as HTMLButtonElement).type = "button";
      vehicleButton.setAttribute("aria-haspopup", "dialog");
    }
    vehicleButton.dataset["vehicleLine"] = vehicleLine.vehicleId;
    vehicleButton.setAttribute("aria-label", changeable ? changeLabel : vehicleLine.ariaLabel);
    if (vehicleLine.estimateTitle !== null) {
      vehicleButton.title = vehicleLine.estimateTitle;
      vehicleButton.dataset["estimated"] = "true";
    }
    vehicleButton.append(batteryIcon(doc));
    // Only the parts that exist, each after the first behind a " · ": never a leading or doubled separator.
    const parts: { cls: string; text: string; connection?: boolean }[] = [];
    if (vehicleLine.name !== null) {
      parts.push({ cls: C.vehicleLineName, text: vehicleLine.name });
    }
    parts.push({ cls: C.vehicleLineCharge, text: `${vehicleLine.estimatePrefix}${vehicleLine.charge}` });
    if (vehicleLine.age !== null) {
      parts.push({ cls: C.vehicleLineAge, text: vehicleLine.age });
    }
    // How the car was decided at this plug-in: "identified by the car's charging cable", "your answer" ...
    const decidedBy = methodWords(model.language, model.identification);
    if (decidedBy !== null) {
      parts.push({ cls: C.vehicleLineAge, text: decidedBy });
    }
    if (connectionText !== null) {
      parts.push({ cls: connectionClass(), text: connectionText, connection: true });
    }
    parts.forEach((part, index) => {
      const span = element(doc, "span", part.cls, index === 0 ? part.text : `\u00b7 ${part.text}`);
      if (part.connection === true) {
        span.dataset["connection"] = model.connection?.state ?? "";
        if (!changeable) {
          vehicleButton?.setAttribute("aria-label", `${vehicleLine.ariaLabel}, ${connectionText}`);
        }
      }
      vehicleButton?.append(span);
    });
    if (changeable) {
      vehicleButton.append(swapIcon(doc));
      const opener = vehicleButton;
      vehicleButton.addEventListener("click", () => {
        openChangeCar(opener);
      });
    }
    identity.append(vehicleButton);
    header.append(identity);
  } else if (connectionText !== null) {
    // No vehicle: the status alone under the name.
    const identity = element(doc, "div", C.nameBlock);
    if (model.chargerName !== null) {
      identity.append(element(doc, "h3", C.name, model.chargerName));
    }
    const status = element(doc, "p", connectionClass(), connectionText);
    status.dataset["connection"] = model.connection?.state ?? "";
    identity.append(status);
    header.append(identity);
  } else if (model.chargerName !== null) {
    header.append(element(doc, "h3", C.name, model.chargerName));
  }
  // The product name: in the DOM for anyone who cannot see the mark, clipped out of sight (text nodes only, never an attribute).
  header.append(
    element(doc, "span", C.visuallyHidden, translate(model.language, "card.title")),
  );
  // The charge history: what each charge delivered and cost, beside Settings.
  const historyButton = element(doc, "button", C.iconButton);
  historyButton.type = "button";
  historyButton.setAttribute("aria-label", translate(model.language, "header.history"));
  historyButton.setAttribute("aria-haspopup", "dialog");
  historyButton.title = translate(model.language, "header.history");
  historyButton.dataset["history"] = "open";
  historyButton.append(historyIcon(doc));
  const headerActions = element(doc, "div", C.headerActions);
  // The Settings entry point: one popover gathering price/fiscal, consumption, capabilities and the site, beside the history.
  const settingsGeneral = element(doc, "button", C.iconButton);
  settingsGeneral.type = "button";
  settingsGeneral.setAttribute("aria-label", translate(model.language, "header.settings"));
  settingsGeneral.title = translate(model.language, "header.settings");
  settingsGeneral.append(settingsGearIcon(doc));
  headerActions.append(historyButton, settingsGeneral);
  header.append(headerActions);
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
  const capabilityDialog: DialogHandle = createDialog({
    owner: input.mount,
    idPrefix: `${idPrefix}-capabilities`,
    labels,
    background: () => card,
    onClose: notifyDialogsChanged,
  });
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
    onDismiss: () => leaveSettingsChild(valueDialog),
  });

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
  if (severity !== null && !bannerRepeatsStatus(model, severity)) {
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
        onAnswer: (vehicleId) => input.onAnswerIdentification?.(vehicleId),
      }),
    );
  }

  if (model.status !== null) {
    card.append(element(doc, "p", C.status, model.status));
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
  let size: ChartSize = { width: initial.width, height: chartHeightForWidth(initial.width) };
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
      ids: { title: `${idPrefix}-chart-title`, description: `${idPrefix}-chart-description` },
    });
    drawn?.element.remove();
    drawn = result;
    // The viewport's height is the policy's answer for the measured width, so the box, viewBox and CSS viewport agree 1:1.
    viewport.style.height = `${chartHeightForWidth(size.width)}px`;
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
    const position = stripNowPosition(nowState, at);
    stripNow.hidden = position === null;
    stripNow.style.left = position === null ? "" : `${position * 100}%`;
    stripLabels.replaceChildren(
      ...stripTicks(model.chart).map((tick) => {
        const label = element(doc, "span", C.stripLabel, tick.label);
        label.style.left = `${tick.position * 100}%`;
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
    measure: input.measure,
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
  const planParts = planSummaryParts(model.language, model.dashboardSettings, model.today);
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

  function mountCard(): void {
    if (!input.mount.contains(card)) {
      input.mount.append(card);
    }
  }

  function openIssues(opener: HTMLElement | null): void {
    if (destroyed) {
      return;
    }
    // Two overlays and two focus traps must never coexist: the other dialog closes first, and it does
    // not restore focus on the way out (that would briefly focus a background control).
    capabilityDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    issuesDialog.show({
      title: translate(model.language, "dialog.issues"),
      body: issueListBody(doc, model),
      opener,
    });
  }

  function openCapabilities(): void {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    capabilityDialog.show({
      title: translate(model.language, "cap.title"),
      body: capabilityBody(doc, model),
      opener: settingsGeneral,
    });
  }

  function openPause(): void {
    if (destroyed || model.control.choices.length === 0) {
      // No choices is not an empty menu: it is this charger having none to offer, and opening a
      // dialog with nothing in it would be worse than doing nothing.
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const body = element(doc, "div");
    body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "pause.intro")));
    const list = element(doc, "div", C.pauseChoices);
    for (const choice of model.control.choices) {
      const button = element(doc, "button", C.choiceButton, translate(model.language, choice.labelKey));
      button.type = "button";
      button.dataset["choice"] = choice.id;
      button.disabled = !model.control.canAct;
      button.addEventListener("click", () => {
        // One click, one request: the dialog closes first, and the choice is the backend's own id.
        pauseDialog.hide({ restoreFocus: false });
        input.onAction("stop", choice.id);
      });
      list.append(button);
    }
    body.append(list);
    pauseDialog.show({
      title: translate(model.language, "pause.sheetTitle"),
      body,
      // The sheet belongs to the automatic control, so focus returns to *that* button.
      opener: plannerButton,
    });
  }

  function openChangeCar(opener: HTMLElement): void {
    if (destroyed) {
      return;
    }
    hideForChildDialog();
    const body = changeCarBody(doc, model.language, {
      block: model.identification,
      vehicles: model.chargerCars,
      currentId: model.identification?.vehicle_id ?? model.soc?.vehicle_id ?? model.targetVehicleId,
      chargerName: model.chargerName,
      idPrefix,
      onChoose: (vehicleId) => {
        vehicleDialog.hide({ restoreFocus: false });
        input.onAnswerIdentification?.(vehicleId);
      },
      onCancel: () => vehicleDialog.hide(),
    });
    vehicleDialog.show({ title: translate(model.language, "identify.changeCar"), body, opener });
  }

  function openStrategy(): void {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const body = element(doc, "div");
    body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "strategy.intro")));
    if (!input.isAdmin) {
      body.append(element(doc, "p", C.settingsReadOnly, translate(model.language, "settings.readOnly")));
    }
    for (const row of model.strategy.rows) {
      const item = element(doc, "div", C.strategyRow);
      const selected = row.id === model.strategy.selectedId;
      // An available, non-selected row is a real button for an administrator. The selected and unavailable
      // rows stay disabled; the backend's `require_admin` is the security boundary, this is a courtesy.
      const writable = row.available && !selected && input.isAdmin;
      const button = element(doc, "button", C.choiceButton, translate(model.language, row.labelKey));
      button.type = "button";
      button.dataset["strategy"] = row.id;
      button.disabled = !writable;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      if (writable) {
        button.addEventListener("click", () => {
          // No confirmation dialog: the click closes this one at once, and the card reports a refusal on the row-level sentence.
          strategyDialog.hide({ restoreFocus: false });
          vehicleDialog.hide({ restoreFocus: false });
          input.onSelectStrategy(row.id);
        });
      }
      item.append(button);
      if (row.reason !== null) {
        item.append(element(doc, "span", C.strategyReason, row.reason));
        // A solar strategy held back for lack of solar settings points at the card that sets them.
        if (
          !row.available &&
          model.site !== null &&
          SOLAR_SETUP_ROWS.has(row.id) &&
          row.reasonCode !== STRATEGY_NEEDS_TOTAL_POWER
        ) {
          const link = element(doc, "button", C.strategyLink, translate(model.language, "strategy.setupSolar"));
          link.type = "button";
          link.dataset["action"] = "setup-solar";
          link.addEventListener("click", () => {
            strategyDialog.hide({ restoreFocus: false });
            vehicleDialog.hide({ restoreFocus: false });
            openSettingsOverview();
            overviewBodyNode
              ?.querySelector<HTMLElement>("[data-section='solar']")
              ?.scrollIntoView?.({ block: "nearest" });
            overviewBodyNode?.querySelector<HTMLElement>("[data-edit-solar]")?.focus();
          });
          item.append(link);
        }
        // A direct site held back for lack of the meter's total grid power points at the site's entities,
        // where the field is, for the administrator who can change it.
        if (!row.available && row.reasonCode === STRATEGY_NEEDS_TOTAL_POWER && input.isAdmin) {
          const link = element(doc, "button", C.strategyLink, translate(model.language, "strategy.setupSite"));
          link.type = "button";
          link.dataset["action"] = "setup-site";
          link.addEventListener("click", () => {
            strategyDialog.hide({ restoreFocus: false });
            vehicleDialog.hide({ restoreFocus: false });
            openEntities("site");
          });
          item.append(link);
        }
      }
      body.append(item);
    }
    strategyDialog.show({
      title: translate(model.language, "strategy.dialogTitle"),
      body,
      opener: strategyButton,
    });
  }

  /**
   * The general Settings popover, built fresh from the model on every open (no request): an overview
   * with one card per area (price area and taxes, each vehicle, the charger, the site, solar), two or
   * three summary rows each and one Change button that opens a dialog with Save and Cancel. Nothing here
   * writes by itself except the active-control switch. A charger with no site gets a plain statement.
   */
  function settingsOverviewBody(): HTMLElement {
    const body = element(doc, "div");
    overviewBodyNode = body;

    overviewNoticeNode = element(doc, "p", C.settingsNotice);
    overviewNoticeNode.hidden = true;
    overviewNoticeNode.setAttribute("role", "status");
    body.append(overviewNoticeNode);
    paintOverviewNotice();

    if (!input.isAdmin) {
      body.append(element(doc, "p", C.settingsReadOnly, translate(model.language, "settings.readOnly")));
    }

    body.append(priceSectionBody());

    // One card per vehicle, repainted when the entity configuration arrives (the sensor row needs it).
    vehicleRows = model.vehicles.map((entry) => ({ ...entry }));
    vehicleListSlot = element(doc, "div");
    vehicleListSlot.dataset["slot"] = "vehicles";
    body.append(vehicleListSlot);

    entitySlot = element(doc, "section", C.settingsSection);
    entitySlot.dataset["section"] = "entities";
    body.append(entitySlot);

    body.append(siteSectionBody());
    const solar = solarSectionBody();
    if (solar !== null) {
      body.append(solar);
    }
    paintEntities();

    const notifications = notificationsSectionBody();
    if (notifications !== null) {
      body.append(notifications);
    }

    body.append(supportSectionBody());

    return body;
  }

  /**
   * Support: "About this card" (the capability list, for everyone) and, for administrators only, the
   * button that saves the debug bundle. Nothing is sent anywhere.
   */
  function supportSectionBody(): HTMLElement {
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "support";
    section.append(
      element(doc, "h4", C.settingsSectionHeading, translate(model.language, "settings.section.support")),
    );
    const outdatedNote = element(doc, "p", C.muted, translate(model.language, "debug.cardOutdated"));
    outdatedNote.dataset["cardOutdated"] = "true";
    outdatedNote.setAttribute("role", "status");
    cardOutdatedNote = outdatedNote;
    paintCardOutdated();
    section.append(outdatedNote);
    if (input.isAdmin) {
      section.append(element(doc, "p", C.muted, translate(model.language, "debug.intro")));
    }
    const actions = element(doc, "div", C.settingsSupportActions);
    const about = element(doc, "button", `${C.button} ${C.settingsSectionConfigure}`);
    about.type = "button";
    about.dataset["about"] = "open";
    about.textContent = translate(model.language, "settings.about.open");
    about.addEventListener("click", () => {
      openCapabilities();
    });
    actions.append(about);
    if (input.isAdmin) {
      const button = element(doc, "button", `${C.button} ${C.settingsSectionConfigure}`);
      button.type = "button";
      button.dataset["downloadDebug"] = "true";
      button.addEventListener("click", () => {
        if (!debugPending) {
          input.onDownloadDebug?.();
        }
      });
      debugButton = button;
      paintDebugButton();
      actions.append(button);
    }
    section.append(actions);
    return section;
  }

  let debugButton: HTMLButtonElement | null = null;
  let debugPending = false;
  let cardOutdatedNote: HTMLElement | null = null;
  let cardIsOutdated = false;

  function paintCardOutdated(): void {
    if (cardOutdatedNote !== null) {
      cardOutdatedNote.hidden = !cardIsOutdated;
    }
  }

  function paintDebugButton(): void {
    if (debugButton === null) {
      return;
    }
    debugButton.disabled = debugPending;
    debugButton.textContent = translate(model.language, debugPending ? "debug.preparing" : "debug.download");
  }

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

  /** One value's own editor in the value dialog: Settings steps aside, and comes back on Cancel or a save. */
  function openValueEditor(title: string, build: (handlers: EditorHandlers) => HTMLFormElement): void {
    if (destroyed || !input.isAdmin) {
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
    valueDialog.show({ title, body: build(handlers), opener: settingsGeneral });
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
  ): void {
    openValueEditor(title, (handlers) =>
      singleEditor(
        doc,
        model.language,
        { ...(intro === undefined ? {} : { intro }), options, selected, idPrefix },
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

  /** The price: the area (its own dialog, with the catalogue), and each fee in its own number editor. */
  function priceSectionBody(): HTMLElement {
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "market";
    section.append(sectionHeading(doc, "price", translate(model.language, "settings.heading.price")));
    const area = marketAreaLabel(model.language, model.contextAreaName, model.contextAreaId);
    // The area opens its dialog for everyone: a reader who is not an administrator inspects it read-only there.
    const areaLabel = translate(model.language, "context.area");
    const areaValue = area || translate(model.language, "settings.value.unset");
    const [areaRow] = settingRow(doc, {
      key: "area",
      label: areaLabel,
      value: areaValue,
      onTap: () => input.onOpenMarket(),
      changeableText: translate(model.language, "settings.row.changeable", { label: areaLabel, value: areaValue }),
    });
    const areaButton = areaRow!.querySelector<HTMLElement>("button")!;
    areaButton.classList.add(C.settingsTrigger);
    if (!input.isAdmin) {
      // Opened to look at only: drawn in the normal colour, like every other value a reader cannot change.
      areaButton.classList.remove(C.settingRowEditable);
    }
    areaButton.dataset["setting"] = "market";
    section.append(areaRow!);
    // The price source is named in the area dialog only, beside the area choice (not in this overview).
    for (const fiscal of fiscalRows(model.language, model.dashboardFiscal)) {
      const component = fiscal.key as FiscalComponentName;
      const included = model.dashboardFiscal?.[component].policy === "included";
      section.append(
        overviewRow(fiscal.key, fiscal.label, fiscal.value, included ? undefined : () => input.onEditFiscal?.(component)),
      );
    }
    return section;
  }

  let overviewBodyNode: HTMLElement | null = null;
  let entityState: EntityViewState = input.isAdmin ? { kind: "loading" } : { kind: "adminOnly" };
  let entitySlot: HTMLElement | null = null;
  let vehicleListSlot: HTMLElement | null = null;
  let vehicleRows: Vehicle[] = [];
  let siteEntitySlot: HTMLElement | null = null;

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
    nodes.push(...priorityRows());
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

  /** The charger's place in its site's order: a choice of three, with what it means above them. */
  function priorityRows(): HTMLElement[] {
    const priority = model.chargerPriority;
    if (priority === null) {
      return [];
    }
    const label = translate(model.language, "entity.field.chargerPriority");
    const options: ChoiceOption[] = [
      { value: "first", label: translate(model.language, "entity.priority.first") },
      { value: "normal", label: translate(model.language, "entity.priority.normal") },
      { value: "last", label: translate(model.language, "entity.priority.last") },
    ];
    const value = options.find((option) => option.value === priority)?.label ?? priority;
    const help = translate(model.language, "entity.help.chargerPriority");
    return [
      overviewRow("charger_priority", label, value, () =>
        editSingle(label, options, priority, (chosen) =>
          writeValue({ kind: "entity", scope: "charger", draft: { charger_priority: chosen } }), help),
      ),
      rowHelp("charger_priority", help),
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
    return nodes;
  }

  /** A car's editors: each property its own number or choice, written as the vehicle dialog wrote it. */
  function vehicleEdits(row: Vehicle, config: EntityConfig | null): VehicleEdits {
    const edits: VehicleEdits = {};
    /**
     * One property's Save, under compare-and-set on what the row showed; after a conflict the next Save expects
     * what the answer says is stored now (the editor says it changed elsewhere and keeps what was typed).
     */
    const propertyWrite = <K extends "capacity_kwh" | "consumption_kwh_per_10km" | "onboard_phases" | "target_percent">(
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
    if (row.target_percent !== undefined) {
      const current = row.target_percent;
      edits.target = () =>
        editNumber(
          translate(model.language, "settings.soc.target"),
          {
            help: translate(model.language, "settings.vehicle.targetHelp"),
            unit: "%",
            current,
            min: 0,
            max: 100,
            decimals: 0,
            noneLabel: translate(model.language, "entity.notSet"),
          },
          propertyWrite("target_percent"),
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
              ...source.candidates.map((candidate) => ({ value: candidate.entity_id, label: candidate.name ?? candidate.entity_id })),
              { value: "none", label: translate(model.language, "identify.source.none") },
            ],
            sourceChoice(source),
            (chosen) =>
              writeValue({ kind: "vehicleSource", vehicleId: row.id, source: kind, entityId: chosen === "" ? null : chosen }),
            translate(model.language, "identify.source.help"),
          );
      }
    }
    return edits;
  }

  /**
   * The site's setup: the main fuse in its own number editor; the measurement and the battery in short words,
   * opening the site's entity dialog.
   */
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
    if (config.site !== null) {
      nodes.push(...siteWarningRows(doc, model.language, config.site));
    }
    return nodes;
  }

  function paintEntities(): void {
    const state = entityState;
    if (vehicleListSlot !== null) {
      const root = vehicleListSlot.getRootNode() as Document | ShadowRoot;
      const active = root.activeElement;
      const focusId =
        active !== null && vehicleListSlot.contains(active) ? (active as HTMLElement).dataset["editVehicle"] : undefined;
      vehicleListSlot.replaceChildren();
      if (vehicleRows.length === 0 && !(state.kind === "ready" && state.config.vehicles.length > 0)) {
        const none = element(doc, "section", C.settingsSection);
        none.dataset["section"] = "vehicle";
        none.append(
          element(doc, "h4", C.settingsSectionHeading, translate(model.language, "settings.section.vehicle")),
          element(doc, "p", C.muted, translate(model.language, "settings.vehicle.none")),
        );
        vehicleListSlot.append(none);
      }
      // A vehicle the configuration lists but the dashboard does not: its sensor can still be chosen.
      const extra: Vehicle[] =
        state.kind === "ready"
          ? state.config.vehicles
              .filter((entry) => !vehicleRows.some((row) => row.id === entry.id))
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
      for (const row of [...vehicleRows, ...extra]) {
        vehicleListSlot.append(
          vehicleSummary(doc, model.language, {
            row,
            properties: !extra.includes(row),
            planned: row.id === model.targetVehicleId,
            charge: chargeFor(row),
            edits: input.isAdmin ? vehicleEdits(row, state.kind === "ready" ? state.config : null) : {},
          }),
        );
      }
      if (focusId !== undefined) {
        vehicleListSlot
          .querySelector<HTMLElement>(`[data-vehicle="${focusId}"] button`)
          ?.focus();
      }
    }
    if (entitySlot !== null) {
      entitySlot.replaceChildren(
        sectionHeading(doc, "charger", translate(model.language, "settings.heading.charger"), model.chargerName),
      );
      const line = unreadableLine(state);
      if (line !== null) {
        entitySlot.append(line);
      }
      if (state.kind === "ready") {
        entitySlot.append(...chargerRows(state.config));
      } else if (model.chargerPriority !== null) {
        entitySlot.append(...priorityRows());
      }
      entitySlot.append(...identificationRows());
    }
    if (siteEntitySlot !== null) {
      siteEntitySlot.replaceChildren();
      const line = unreadableLine(state);
      if (line !== null) {
        siteEntitySlot.append(line);
      }
      if (state.kind === "ready" && state.config.site !== null) {
        siteEntitySlot.append(...siteRows(state.config));
      }
    }

  }

  /**
   * The site card: headed by the site's name alone ("Site" only when it has none), marked as applying to
   * every charger on it, with the configured entities in words and the load-balancing switch.
   */
  function siteSectionBody(): HTMLElement {
    const site = model.site;
    const siteSection = element(doc, "section", C.settingsSection);
    siteSection.dataset["section"] = "site";
    siteEntitySlot = null;
    if (site === null) {
      siteSection.append(sectionHeading(doc, "site", translate(model.language, "settings.heading.site")));
      siteSection.append(element(doc, "p", C.muted, translate(model.language, "site.none")));
      return siteSection;
    }
    siteSection.append(sectionHeading(doc, "site", translate(model.language, "settings.heading.site"), site.name));
    siteSection.append(element(doc, "p", C.siteApplies, site.appliesToText));
    siteEntitySlot = element(doc, "div");
    siteEntitySlot.dataset["slot"] = "site-entities";
    siteSection.append(siteEntitySlot);
    // Active load balancing: a switch for administrators, read-only state for others. `available` and
    // `enabled` stay separate: enabled-but-unavailable keeps the switch on and says why. Never optimistic:
    // a press leaves the control on the confirmed value, pending until the answer.
    activeSlot = element(doc, "div");
    activeSlot.dataset["slot"] = "active-control";
    siteSection.append(activeSlot);
    paintActiveControl();
    return siteSection;
  }

  /** Solar: the priority (one of two) and the forecast sources (any of them), each in its own editor. */
  function solarSectionBody(): HTMLElement | null {
    const site = model.site;
    if (site === null) {
      return null;
    }
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "solar";
    section.append(sectionHeading(doc, "solar", translate(model.language, "settings.section.solar")));
    section.append(element(doc, "p", C.siteApplies, site.appliesToText));
    const writable = site.writable;
    const priorityLabel = translate(model.language, "site.solarPriority.title");
    const priorities: ChoiceOption[] = [
      { value: "car_first", label: translate(model.language, "site.solarPriority.carFirst") },
      { value: "battery_first", label: translate(model.language, "site.solarPriority.batteryFirst") },
    ];
    section.append(
      overviewRow(
        "solar_priority",
        priorityLabel,
        priorities.find((option) => option.value === site.solarPriority)?.label ?? site.solarPriority,
        writable
          ? () => editSingle(priorityLabel, priorities, site.solarPriority, (chosen) => writeValue({ kind: "solar", priority: chosen }))
          : undefined,
      ),
    );
    const forecastLabel = translate(model.language, "site.solarForecast.title");
    const sources = site.solarForecastChoices.filter((choice) => choice.selected);
    section.append(
      overviewRow(
        "solar_forecast",
        forecastLabel,
        sources.length === 0 ? translate(model.language, "settings.value.none") : sources.map((choice) => choice.title).join(", "),
        writable && site.solarForecastChoices.length > 0
          ? () =>
              editMulti(
                forecastLabel,
                site.solarForecastChoices.map((choice) => ({ value: choice.id, label: choice.title })),
                sources.map((choice) => choice.id),
                (values) => writeValue({ kind: "solar", forecast: values }),
              )
          : undefined,
      ),
    );
    return section;
  }

  /**
   * Notifications: the chosen phones and the events, each row its own editor (any phones, any events). Absent on
   * a backend without notifications.
   */
  function notificationsSectionBody(): HTMLElement | null {
    const record = model.dashboardSettings?.notifications;
    if (record === undefined) {
      return null;
    }
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "notifications";
    section.append(sectionHeading(doc, "notifications", translate(model.language, "settings.section.notifications")));
    // A tap on a notification opens the page this card is on.
    const page = doc.defaultView?.location?.pathname ?? null;
    const url = page !== null && page.startsWith("/") ? page : null;
    const [phones, events] = notificationsSummary(model.language, record);
    const phonesLabel = translate(model.language, "notifications.phones");
    const eventsLabel = translate(model.language, "notifications.events");
    section.append(
      overviewRow(phones?.key ?? "notification_targets", phonesLabel, phones?.value ?? "", () =>
        editMulti(phonesLabel, phoneOptions(model.language, record), record.targets, (targets) =>
          writeValue({
            kind: "settings",
            build: (fresh) =>
              notificationsReplacement(fresh, { targets, events: fresh.notifications?.events ?? record.events, url }),
          }),
          {
            intro: translate(model.language, record.available.length === 0 ? "notifications.noPhones" : "notifications.intro"),
          },
        ),
      ),
      overviewRow(events?.key ?? "notification_events", eventsLabel, events?.value ?? "", () =>
        editMulti(eventsLabel, eventOptions(model.language), record.events, (chosen) =>
          writeValue({
            kind: "settings",
            build: (fresh) =>
              notificationsReplacement(fresh, { targets: fresh.notifications?.targets ?? record.targets, events: chosen, url }),
          }),
        ),
      ),
    );
    const missing = noRecipientsNote(doc, model.language, record);
    if (missing !== null) {
      section.append(missing);
    }
    return section;
  }

  // The load-balancing switch's state lives outside `model`: an answer repaints this block in place,
  // since the popover stays open and a full render is deferred while a dialog is open.
  let activeSlot: HTMLElement | null = null;
  let activeOverride: { available: boolean; enabled: boolean; reason: { text: string; code: string | null } | null } | null =
    null;
  let activePending = false;
  let activeNotice: ActiveControlNotice | null = null;

  function paintActiveControl(): void {
    const site = model.site;
    if (activeSlot === null || site === null) {
      return;
    }
    const state = activeOverride ?? {
      available: site.activeControlAvailable,
      enabled: site.activeControlEnabled,
      reason: site.activeControlReason,
    };
    const label = translate(model.language, "site.activeControl.title");
    const stateText = activePending
      ? translate(model.language, "site.activeControl.pending")
      : translate(model.language, state.enabled ? "site.activeControl.on" : "site.activeControl.off");
    // Off may always be chosen; on only when the backend says it is available. Never optimistic: the row shows
    // the confirmed value until an answer says otherwise.
    const changeable = site.writable && site.activeControlWritable && !activePending && (state.available || state.enabled);
    const nodes: HTMLElement[] = [];
    const row = overviewRow(
      "active-control",
      label,
      stateText,
      changeable
        ? () =>
            openValueEditor(label, (handlers) =>
              onOffEditor(
                doc,
                model.language,
                { intro: translate(model.language, "site.activeControl.note"), on: state.enabled, idPrefix },
                async (chosen) => {
                  // The answer arrives through `adoptActiveControl`, painted in this row.
                  input.onSetActiveControl?.(state.enabled, chosen);
                  return null;
                },
                handlers,
              ),
            )
        : undefined,
    );
    row.querySelector<HTMLElement>(`.${C.settingRowValue}`)?.setAttribute("data-role", "active-control-state");
    if (activePending) {
      row.setAttribute("aria-busy", "true");
    }
    nodes.push(row);
    if (state.reason !== null) {
      nodes.push(rowHelp("active-control-reason", state.reason.text));
    } else if (!site.writable) {
      nodes.push(rowHelp("active-control-available", translate(model.language, "site.activeControl.available")));
    }
    nodes.push(rowHelp("active-control", translate(model.language, "site.activeControl.note")));
    if (activeNotice !== null) {
      const notice = element(
        doc,
        "p",
        activeNotice.tone === "warning" ? `${C.activeNotice} ${C.activeNoticeWarning}` : C.activeNotice,
      );
      notice.setAttribute("role", "status");
      for (const line of activeNotice.lines) {
        notice.append(element(doc, "span", "", line));
      }
      if (activeNotice.code !== null) {
        notice.dataset["code"] = activeNotice.code;
      }
      nodes.push(notice);
    }
    activeSlot.replaceChildren(...nodes);
  }

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
    if (destroyed) {
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
      title:
        model.chargerName === null
          ? translate(model.language, "settings.overview.title")
          : translate(model.language, "settings.overview.titleNamed", { name: model.chargerName }),
      body: settingsOverviewBody(),
      opener: settingsGeneral,
    });
    input.onSettingsOverviewOpened?.();
  }

  // ---- the charge history dialog. The card owns the request; this holds what was last said and
  // what the reader chose in it (the month, the day under the readout), and repaints on any change.
  let historyState: HistoryState = { kind: "loading" };
  const historyUi: HistoryUi = { month: null, pending: false, day: null, exporting: false, notice: null };

  function paintHistory(): void {
    if (destroyed || !historyDialog.isOpen()) {
      return;
    }
    const focused = (input.mount instanceof ShadowRoot ? input.mount.activeElement : doc.activeElement) as
      | HTMLElement
      | null;
    const refocus =
      focused !== null && historyDialog.element.contains(focused)
        ? focused.dataset["month"] !== undefined
          ? `button[data-month="${focused.dataset["month"]}"]`
          : focused.dataset["action"] === "export"
            ? "[data-action='export']"
            : focused.dataset["monthSelect"] !== undefined
              ? "[data-month-select]"
              : null
        : null;
    historyDialog.show({
      title:
        model.chargerName === null
          ? translate(model.language, "history.title")
          : translate(model.language, "history.titleNamed", { name: model.chargerName }),
      body: historyBody(doc, model.language, historyState, historyUi, {
        onMonth: (month) => {
          historyUi.month = month;
          historyUi.pending = true;
          historyUi.day = null;
          historyUi.notice = null;
          paintHistory();
          input.onHistoryMonth?.(month);
        },
        onExport: () => {
          input.onExportHistory?.();
        },
      }),
    });
    if (refocus !== null) {
      historyDialog.element.querySelector<HTMLElement>(refocus)?.focus();
    }
  }

  function openHistory(): void {
    if (destroyed) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    historyUi.notice = null;
    historyUi.exporting = false;
    historyUi.month = null;
    historyUi.pending = false;
    historyUi.day = null;
    historyState = { kind: "loading" };
    historyDialog.show({
      title: translate(model.language, "history.title"),
      body: historyBody(doc, model.language, historyState, historyUi, {
        onMonth: () => undefined,
        onExport: () => undefined,
      }),
      opener: historyButton,
    });
    paintHistory();
    input.onOpenHistory?.();
  }

  historyButton.addEventListener("click", () => {
    openHistory();
  });
  settingsGeneral.addEventListener("click", () => {
    openSettingsOverview();
  });

  // The settings row and its dialog. The row labels itself from the dashboard's settings section (no
  // request); a press is reported and the card hands back the form. One overlay serves all three
  // editors, and opening one closes the others first.
  let settingsKind: SettingsEditorKind | null = null;
  let settingsForm: SettingsEditorForm | null = null;
  let settingsBody: HTMLElement | null = null;
  let settingsNotice: FailureSentence | null = null;
  let settingsNoticeNode: HTMLParagraphElement | null = null;
  let settingsSaveButton: HTMLButtonElement | null = null;
  let settingsReapplyButton: HTMLButtonElement | null = null;
  let settingsPending = false;
  let settingsBodyReader: (() => SettingsFormValues) | null = null;

  function settingsTitleKey(kind: SettingsEditorKind): TranslationKey {
    return `settings.${kind}.title` as TranslationKey;
  }

  function settingsTriggerFor(kind: SettingsEditorKind): HTMLElement | null {
    return bar.querySelector<HTMLElement>(`[data-setting="${kind}"]`);
  }

  function applySettingsPending(): void {
    if (settingsSaveButton !== null) {
      settingsSaveButton.disabled = settingsPending;
    }
    if (settingsReapplyButton !== null) {
      settingsReapplyButton.disabled = settingsPending;
    }
    const cancel = settingsBody?.querySelector<HTMLButtonElement>("[data-action='cancel']") ?? null;
    if (cancel !== null) {
      cancel.disabled = settingsPending;
    }
  }

  /**
   * Notices inside the dialog: one sentence, re-inserted whenever the body is rebuilt. Validation
   * errors and refusals share one node; the stable code stays as subdued detail.
   */
  function paintSettingsNotice(): void {
    settingsNoticeNode?.remove();
    settingsNoticeNode = null;
    if (settingsBody !== null && settingsNotice !== null) {
      if (settingsForm === null) {
        settingsBody.replaceChildren();
      }
      const paragraph = element(doc, "p", C.settingsNotice, translate(model.language, settingsNotice.sentenceKey));
      if (settingsNotice.code !== null) {
        paragraph.dataset["code"] = settingsNotice.code;
      }
      settingsBody.append(paragraph);
      settingsNoticeNode = paragraph;
    }
    applySettingsPending();
  }

  function renderSettingsBody(): void {
    const form = settingsForm;
    if (form === null || destroyed) {
      return;
    }
    const built = settingsEditorBody(
      doc,
      model.language,
      form,
      {
        onSave: (values) => input.onSaveSettings(form.kind, values),
        onCancel: () => closeSettingsEditor(),
        onReload: () => input.onReloadSettings(form.kind),
        onReapply: (values) => input.onReapplySettings(form.kind, values),
      },
      `${idPrefix}-settings-${form.kind}`,
    );
    settingsBody = built.body;
    settingsBodyReader = built.values;
    settingsSaveButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsSave}`);
    settingsReapplyButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsReapply}`);
    settingsDialog.show({
      title: translate(model.language, settingsTitleKey(form.kind)),
      body: built.body,
      opener: settingsTriggerFor(form.kind),
    });
    paintSettingsNotice();
  }

  function openSettingsEditor(kind: SettingsEditorKind): void {
    if (destroyed) {
      return;
    }
    settingsKind = kind;
    settingsForm = null;
    settingsBody = null;
    settingsBodyReader = null;
    settingsNotice = null;
    settingsNoticeNode = null;
    settingsSaveButton = null;
    settingsReapplyButton = null;
    settingsPending = false;
    // One overlay at a time, and the others do not restore focus on the way out.
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const loading = element(
      doc,
      "p",
      `${C.muted} ${C.dialogIntro}`,
      translate(model.language, "settings.loading"),
    );
    // This paragraph is the dialog's body until a form replaces it, so a failure that arrives while the
    // record is still being read has a node to be shown in rather than a sentence with nowhere to go.
    settingsBody = loading;
    settingsDialog.show({
      title: translate(model.language, settingsTitleKey(kind)),
      body: loading,
      opener: settingsTriggerFor(kind),
    });
  }

  function showSettingsEditorForm(form: SettingsEditorForm): void {
    if (destroyed || settingsKind !== form.kind) {
      return;
    }
    settingsForm = form;
    settingsNotice = null;
    settingsPending = false;
    renderSettingsBody();
  }

  function showSettingsEditorConflict(revision: number, phases: number | null): void {
    const form = settingsForm;
    if (destroyed || form === null) {
      return;
    }
    const values = settingsBodyReader === null ? form.values : settingsBodyReader();
    // The server's own phase count comes with the conflict: a reapply is built on that record, so the
    // nominal power the form names must be the one that record would actually draw.
    settingsForm = { ...form, values, conflict: revision, phases };
    settingsNotice = null;
    settingsPending = false;
    renderSettingsBody();
  }

  function setSettingsEditorNotice(failure: FailureSentence | null): void {
    settingsNotice = failure;
    paintSettingsNotice();
  }

  function setSettingsEditorPending(pending: boolean): void {
    settingsPending = pending;
    applySettingsPending();
  }

  function closeSettingsEditor(): void {
    settingsDialog.hide();
    settingsKind = null;
    settingsForm = null;
    settingsBody = null;
    settingsBodyReader = null;
    settingsNotice = null;
    settingsNoticeNode = null;
    settingsSaveButton = null;
    settingsReapplyButton = null;
    settingsPending = false;
  }

  function settingsEditorOpen(): SettingsEditorKind | null {
    return settingsDialog.isOpen() ? settingsKind : null;
  }

  function setSettingsError(failure: FailureSentence | null): void {
    if (failure === null) {
      settingsError.hidden = true;
      settingsError.textContent = "";
      settingsError.removeAttribute("data-code");
      return;
    }
    settingsError.hidden = false;
    settingsError.textContent = translate(model.language, failure.sentenceKey);
    if (failure.code === null) {
      settingsError.removeAttribute("data-code");
    } else {
      settingsError.dataset["code"] = failure.code;
    }
  }

  // The area/fiscal dialog: the card owns the record, catalogue context and drafts. Picking another area
  // rebuilds nothing here; the choice is reported with the values on screen and the card hands back the
  // form for that area.
  let marketForm: MarketEditorForm | null = null;
  let marketBody: HTMLElement | null = null;
  let marketNotice: FailureSentence | null = null;
  let marketNoticeNode: HTMLParagraphElement | null = null;
  let marketSaveButton: HTMLButtonElement | null = null;
  let marketReapplyButton: HTMLButtonElement | null = null;
  let marketPending = false;
  let marketBodyReader: (() => MarketFormValues) | null = null;

  function marketTriggerFor(): HTMLElement | null {
    // The area/fiscal editor is reached from the Settings popover: closing it returns focus to the
    // button that opened that surface, the same rule the consumption editor follows.
    return settingsGeneral;
  }

  function applyMarketPending(): void {
    if (marketSaveButton !== null) {
      marketSaveButton.disabled = marketPending;
    }
    if (marketReapplyButton !== null) {
      marketReapplyButton.disabled = marketPending;
    }
  }

  function paintMarketNotice(): void {
    marketNoticeNode?.remove();
    marketNoticeNode = null;
    if (marketBody !== null && marketNotice !== null) {
      if (marketForm === null) {
        marketBody.replaceChildren();
      }
      const paragraph = element(doc, "p", C.settingsNotice, translate(model.language, marketNotice.sentenceKey));
      if (marketNotice.code !== null) {
        paragraph.dataset["code"] = marketNotice.code;
      }
      marketBody.append(paragraph);
      marketNoticeNode = paragraph;
    }
    applyMarketPending();
  }

  function renderMarketBody(): void {
    const form = marketForm;
    if (form === null || destroyed) {
      return;
    }
    const built = marketEditorBody(
      doc,
      model.language,
      form,
      {
        onSave: (values) => input.onSaveMarket(values),
        onReload: () => input.onReloadMarket(),
        onReapply: (values) => input.onReapplyMarket(values),
        onCancel: () => leaveSettingsChild(marketDialog, input.onCancelMarket),
        onAreaChange: (areaId, live) => input.onMarketAreaChange(areaId, live),
        ...(input.onFindRegion === undefined ? {} : { onFindRegion: input.onFindRegion }),
      },
      idPrefix,
      homeAssistantCountry(input.hass?.()),
    );
    marketBody = built.body;
    marketBodyReader = built.values;
    marketSaveButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsSave}`);
    marketReapplyButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsReapply}`);
    marketDialog.show({
      title: translate(model.language, "market.title"),
      body: built.body,
      opener: marketTriggerFor(),
    });
    paintMarketNotice();
  }

  function openMarketEditor(): void {
    if (destroyed) {
      return;
    }
    marketForm = null;
    marketBody = null;
    marketBodyReader = null;
    marketNotice = null;
    marketNoticeNode = null;
    marketSaveButton = null;
    marketReapplyButton = null;
    marketPending = false;
    // One overlay at a time, and the others do not restore focus on the way out.
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const loading = element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "market.loading"));
    // This paragraph is the dialog's body until a form replaces it, so a failure that arrives while the
    // two reads are still out has a node to be shown in rather than a sentence with nowhere to go.
    marketBody = loading;
    marketDialog.show({
      title: translate(model.language, "market.title"),
      body: loading,
      opener: marketTriggerFor(),
    });
  }

  function showMarketEditorForm(form: MarketEditorForm): void {
    if (destroyed || !marketDialog.isOpen()) {
      return;
    }
    marketForm = form;
    marketNotice = null;
    marketPending = false;
    renderMarketBody();
  }

  function showMarketEditorConflict(revision: number): void {
    const form = marketForm;
    if (destroyed || form === null) {
      return;
    }
    const values = marketBodyReader === null ? form.values : marketBodyReader();
    // The reader's own area and figures stay exactly as they are; only the base they would be applied
    // to changes, and that base is the server's record the caller kept.
    marketForm = { ...form, values, conflict: revision };
    marketNotice = null;
    marketPending = false;
    renderMarketBody();
  }

  function setMarketEditorNotice(failure: FailureSentence | null): void {
    marketNotice = failure;
    paintMarketNotice();
  }

  function setMarketEditorPending(pending: boolean): void {
    marketPending = pending;
    applyMarketPending();
  }

  function marketEditorOpen(): boolean {
    return marketDialog.isOpen();
  }

  function closeMarketEditor(options: { restoreFocus?: boolean } = {}): void {
    marketDialog.hide(options);
    marketForm = null;
    marketBody = null;
    marketBodyReader = null;
    marketNotice = null;
    marketNoticeNode = null;
    marketSaveButton = null;
    marketReapplyButton = null;
    marketPending = false;
  }

  // The entity editors: one overlay drawn from the config the card read, never optimistic.
  let entityEditor: { scope: EntityScope | "vehicle" | "solar"; built: EntityEditorBody } | null = null;

  function openEntityEditor(scope: EntityScope, config: EntityConfig, notice: FailureSentence | null = null): void {
    if (destroyed) {
      return;
    }
    const focus = entityFocus !== null && entityFocus.scope === scope ? entityFocus : null;
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const built = entityEditorBody(
      doc,
      model.language,
      {
        scope,
        config,
        hass: () => input.hass?.(),
        appliesText: scope === "site" ? (model.site?.appliesToText ?? null) : null,
        ...(focus === null ? {} : { focus: focus.part }),
      },
      {
        onSave: (draft) => input.onSaveEntities?.(scope, draft),
        onCancel: () => leaveSettingsChild(entityDialog, input.onCancelEntities),
        onOpenSite: () => {
          entityDialog.hide({ restoreFocus: false });
          openEntities("site");
        },
      },
      idPrefix,
    );
    entityEditor = { scope, built };
    if (notice !== null) {
      built.setNotice(translate(model.language, notice.sentenceKey), notice.code);
    }
    entityDialog.show({
      title: focus !== null ? focus.title : translate(model.language, scope === "site" ? "entity.editor.site" : "entity.editor.charger"),
      body: built.body,
      opener: settingsGeneral,
    });
  }

  function hideForChildDialog(): void {
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
  }

  function entityEditorOpen(): EntityScope | null {
    return entityEditor !== null && entityEditor.scope !== "vehicle" && entityEditor.scope !== "solar" && entityDialog.isOpen()
      ? entityEditor.scope
      : null;
  }

  function closeEntityEditor(): void {
    entityDialog.hide({ restoreFocus: false });
    entityEditor = null;
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
    setHistoryState(state: HistoryState): void {
      historyState = state;
      historyUi.pending = false;
      if (state.kind === "ready") {
        historyUi.month = state.answer.month;
      }
      paintHistory();
    },
    setHistoryExport(notice: TranslationKey | null, exporting: boolean): void {
      historyUi.notice = notice;
      historyUi.exporting = exporting;
      paintHistory();
    },
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
    setActiveControlPending(pending: boolean): void {
      activePending = pending;
      paintActiveControl();
    },
    adoptActiveControl(site: SiteFacts | null, notice: ActiveControlNotice | null): void {
      if (site !== null) {
        activeOverride = {
          available: site.activeControlAvailable,
          enabled: site.activeControlEnabled,
          reason: site.activeControlReason,
        };
      }
      activeNotice = notice;
      activePending = false;
      paintActiveControl();
    },
    anyDialogOpen: anyDialogOpenNow,
    setEntityState(state: EntityViewState): void {
      entityState = state;
      paintEntities();
    },
    openEntityEditor,
    valueEditorOpen: () => valueDialog.isOpen(),
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
    setEntityEditorNotice(failure: FailureSentence | null): void {
      entityEditor?.built.setNotice(
        failure === null ? null : translate(model.language, failure.sentenceKey),
        failure === null ? null : failure.code,
      );
    },
    markEntityFieldErrors(errors: readonly EntityFieldError[]): void {
      entityEditor?.built.markErrors(errors);
    },
    setEntityEditorPending(pending: boolean): void {
      entityEditor?.built.setPending(pending);
    },
    setEntityHass(hass: unknown): void {
      entityEditor?.built.setHass(hass);
    },
    closeEntityEditor,
    setDebugPending(pending: boolean): void {
      debugPending = pending;
      paintDebugButton();
    },
    setCardOutdated(outdated: boolean): void {
      cardIsOutdated = outdated;
      paintCardOutdated();
    },
    setOverviewNotice(failure: FailureSentence | null): void {
      overviewNotice = failure;
      paintOverviewNotice();
    },
    closeSettingsOverview(): void {
      settingsOverviewDialog.hide({ restoreFocus: false });
    },
    setActionError(failure: FailureSentence | null): void {
      if (failure === null) {
        actionError.hidden = true;
        actionError.textContent = "";
        actionError.removeAttribute("data-code");
        return;
      }
      actionError.hidden = false;
      actionError.textContent = translate(model.language, failure.sentenceKey);
      if (failure.code === null) {
        actionError.removeAttribute("data-code");
      } else {
        actionError.dataset["code"] = failure.code;
      }
    },
    destroy(): void {
      if (destroyed) {
        return;
      }
      destroyed = true;
      // The boundary appointment is released before anything else: a callback that fired after this
      // would belong to a view that no longer exists.
      cancelBoundary();
      interaction?.destroy();
      issuesDialog.destroy();
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
