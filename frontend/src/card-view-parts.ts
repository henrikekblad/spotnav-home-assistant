// The pure helpers of the card view: the readout and banner rules, the period blocks, the icons, the car button, the
// status fit and the issue and capability lists. Everything here is a function of a model (and a document); the
// view itself (`card-view.ts`) puts them together and re-exports the ones other files and tests use.

import { type ChartMark } from "./chart";
import { sunAt } from "./chart-sun";
import { methodWords } from "./identification";
import { clock, formatNumber, hasZone, pricePerKwh, priceUnitName, wallTimeRepeats, weekdayDate } from "./format";
import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { type CardModel, type Issue, type PlanPeriod, type PlanRelationKind } from "./model";
import type { ConnectionShownName } from "./types";
import { issueText, type IssueFix } from "./status";
import { type VehicleLineFacts } from "./vehicle-line";
import type { ChargeBarFacts } from "./charge-bar";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

/**
 * The furthest ahead an interval-boundary appointment is worth keeping (ms). Anything past a day
 * would be replaced by the card's own refresh first, and a platform timer would clamp it; every
 * refresh arms a fresh one.
 */
export const BOUNDARY_HORIZON_MS = 24 * 3_600_000;

/** Whether Home Assistant has loaded an integration, from the `hass` object the card was given. */
export function hasComponent(hass: unknown, domain: string): boolean {
  const components = (hass as { config?: { components?: unknown } } | null | undefined)?.config?.components;
  return Array.isArray(components) && components.includes(domain);
}

/** Open one of Home Assistant's own pages, the way its frontend moves between them. */
export function navigateTo(doc: Document, path: string): void {
  const view = doc.defaultView;
  if (view === null) {
    return;
  }
  view.history.pushState(null, "", path);
  view.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
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
  return `${translate(language, key, { day, time, offset, price }).trim()}${sunReadout(model, mark)}`;
}

/** The sun's figure for the hour of a mark, appended to the readout (empty where the chart draws no sun there). */
export function sunReadout(model: CardModel, mark: ChartMark): string {
  const point = sunAt(model.sun, mark.startMs);
  if (point === null) {
    return "";
  }
  const kw = formatNumber(model.language, point.kw, 1);
  return ` · ${translate(model.language, point.measured ? "graph.readout.sun" : "graph.readout.sunForecast", { kw })}`;
}

/**
 * The visible readout line: the full sentence for a selection, or empty for none, so the aria-live
 * region stays silent instead of showing "Nothing selected." (`graphDescription` still says it, as
 * the whole-picture description).
 */
export function readoutDisplayText(model: CardModel, mark: ChartMark | null): string {
  return mark === null ? "" : readoutTextFor(model, mark);
}

/**
 * The figure for the summary's Now label at one interval, or `null` when there is no containing
 * interval or no market zone (the line is then omitted, never shown as "unknown").
 */
export function currentPriceText(model: CardModel, mark: ChartMark | null): string | null {
  return mark === null || !hasZone(model.format) ? null : pricePerKwh(model.format, mark.price);
}

/**
 * The summary's "Now" line: label and formatted figure, or `null` to hide the whole line. The label
 * is translated here so a redraw never drops it.
 */
export function nowSummaryText(model: CardModel, figure: string | null): string | null {
  return figure === null ? null : `${translate(model.language, "graph.summary.current")} ${figure}`;
}

export function bannerSeverity(model: CardModel): "blocking" | "notice" | null {
  return model.severity;
}

/**
 * Notices about the charger's own settings working against SpotNav: something to act on, so the attention line
 * names them even when the status line says them too, and "Get started" never takes their place.
 */
export const ACT_ON_CODES: ReadonlySet<string> = new Set(["charger_own_mode", "charger_disabled"]);

/**
 * A neutral banner whose issues are all lines of the status headline adds nothing: the headline says it.
 * Judged by the lines' codes, not by their words: an issue is worded from the same line, but a headline joins
 * its lines with " · " (and some sentences have one inside), so the words cannot be matched back reliably.
 * A blocking banner is always kept, and so is one with a charger setting to act on, or an issue that is no
 * line of the headline (the list of missing settings).
 */
export function bannerRepeatsStatus(model: CardModel, severity: "blocking" | "notice"): boolean {
  if (severity !== "notice" || model.status === null || model.issues.length === 0) {
    return false;
  }
  if (model.issues.some((issue) => ACT_ON_CODES.has(issue.code) || issue.fix?.action !== undefined)) {
    // A press to act on (a charger setting, or the import target set aside): the banner opens it.
    return false;
  }
  return model.issues.every((issue) => model.statusCodes.includes(issue.code));
}

/** The strategy rows whose availability hangs on the site's solar setup. */
export const SOLAR_SETUP_ROWS: ReadonlySet<string> = new Set(["solar", "hybrid"]);

/**
 * A solar strategy held back because the site has no solar is hidden from a reader who cannot set solar up: only an
 * administrator acts on it (the "set up solar" link). The total-grid-power case stays for everyone, with its hint.
 */
export function strategyRowHidden(
  row: { id: string; available: boolean; reasonCode: string | null },
  isAdmin: boolean,
): boolean {
  return !isAdmin && !row.available && SOLAR_SETUP_ROWS.has(row.id) && row.reasonCode !== STRATEGY_NEEDS_TOTAL_POWER;
}

/**
 * Whether the site can follow solar: no solar strategy is held back for the site's measurement (the dashboard offers
 * Sol and Hybrid then).
 */
export function siteFollowsSolar(rows: ReadonlyArray<{ id: string; available: boolean }>): boolean {
  return !rows.some((row) => SOLAR_SETUP_ROWS.has(row.id) && !row.available);
}

/**
 * The import target row is shown unless the site cannot measure it (no total grid power) and none is set: a set
 * target stays, so it can be cleared. Without a grid tariff block (the charger prices with its own settings) the
 * card does not know, and shows it.
 */
export function importTargetShown(
  demand: { available: boolean; reason: string | null } | null,
  value: number | null,
): boolean {
  return value !== null || demand === null || demand.available || demand.reason !== STRATEGY_NEEDS_TOTAL_POWER;
}

/** Each strategy's one line under its name in the strategy dialog. */
export const STRATEGY_DESCRIBE_KEYS: Readonly<Record<string, TranslationKey>> = {
  cheapest: "strategy.describe.cheapest",
  solar: "strategy.describe.solar",
  hybrid: "strategy.describe.hybrid",
};
/** The reason code of a solar strategy held back because a direct site lacks the meter's total grid power. */
export const STRATEGY_NEEDS_TOTAL_POWER = "needs_total_grid_power";

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

export function periodHeading(model: CardModel, base: "plan.proposal" | "plan.installed", count: number): string {
  const form = pluralForm(model.language, count);
  return translate(model.language, `${base}.${form}` as TranslationKey, { count: String(count) });
}

export function periodLine(doc: Document, model: CardModel, period: PlanPeriod): HTMLElement {
  const line = element(doc, "div", C.period);
  // The model's own offset-disambiguated label; when the market had no zone the model says so.
  line.textContent = period.label === "" ? translate(model.language, "plan.missing") : period.label;
  return line;
}

export function periodBlock(
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

/** One small path-only SVG icon, decorative and hidden from assistive technology. */
export function icon(doc: Document, build: (svg: SVGElement, ns: string) => void): SVGElement {
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

export function strokePath(ns: string, doc: Document, d: string): SVGPathElement {
  const path = doc.createElementNS(ns, "path") as SVGPathElement;
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  return path;
}

export function fillPath(ns: string, doc: Document, d: string): SVGPathElement {
  const path = doc.createElementNS(ns, "path") as SVGPathElement;
  path.setAttribute("d", d);
  path.setAttribute("fill", "currentColor");
  return path;
}

/** MDI `power-plug`: a plugged-in car for the status. */
export const PLUG_PATH = "M16,7V3H14V7H10V3H8V7H8C7,7 6,8 6,9V14.5L9.5,18V21H14.5V18L18,14.5V9C18,8 17,7 16,7Z";

/**
 * The charger's status as a symbol, for a header too narrow for the word: MDI path data (`@mdi/js`, Apache License
 * 2.0) for power-plug-off, power-plug, lightning-bolt, pause, clock-outline, check-circle-outline and
 * alert-circle-outline.
 */
export const CONNECTION_ICON_PATHS: Readonly<Record<Exclude<ConnectionShownName, "unknown">, string>> = {
  disconnected:
    "M13,3V7H15V3H17V7C18,7 19,8 19,9V14.5L17.88,15.63L8.25,6H11V3H13M3.28,4L2,5.27L5.5,8.77C5.18,9.04 5,9.5 5,10V15.5L8.5,19V22H13.5V19L14.5,18L18.73,22.27L20,21L3.28,4Z",
  connected: PLUG_PATH,
  charging: "M11 15H6L13 1V9H18L11 23V15Z",
  paused: "M14,19H18V5H14M6,19H10V5H6V19Z",
  waiting:
    "M12,20A8,8 0 0,0 20,12A8,8 0 0,0 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20M12,2A10,10 0 0,1 22,12A10,10 0 0,1 12,22C6.47,22 2,17.5 2,12A10,10 0 0,1 12,2M12.5,7V12.25L17,14.92L16.25,16.15L11,13V7H12.5Z",
  finished:
    "M12 2C6.5 2 2 6.5 2 12S6.5 22 12 22 22 17.5 22 12 17.5 2 12 2M12 20C7.59 20 4 16.41 4 12S7.59 4 12 4 20 7.59 20 12 16.41 20 12 20M16.59 7.58L10 14.17L7.41 11.59L6 13L10 17L18 9L16.59 7.58Z",
  error:
    "M11,15H13V17H11V15M11,7H13V13H11V7M12,2C6.47,2 2,6.5 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2M12,20A8,8 0 0,1 4,12A8,8 0 0,1 12,4A8,8 0 0,1 20,12A8,8 0 0,1 12,20Z",
};

/** The status symbol: an image named by the status word, shown only while the word does not fit. */
export function connectionIcon(doc: Document, shown: Exclude<ConnectionShownName, "unknown">, words: string): HTMLElement {
  const holder = element(doc, "span", C.connectionIcon);
  holder.setAttribute("role", "img");
  holder.setAttribute("aria-label", words);
  holder.title = words;
  holder.append(
    icon(doc, (svg, ns) => {
      svg.append(fillPath(ns, doc, CONNECTION_ICON_PATHS[shown]));
    }),
  );
  return holder;
}

/** What `fitStatus` reads from the header, in CSS pixels; injectable for tests, since a test DOM has no layout. */
export interface StatusFitReading {
  /** The width the name block has beside the header's buttons. */
  available: number;
  /** The charger name's width as laid out (at most `available`). */
  name: number;
  /** The status word's own width, separator included, on one line. */
  word: number;
}

/** The gap between the name and its status (`column-gap` of the name row). */
export const STATUS_GAP_PX = 4;

/**
 * Whether the name and " · <status word>" fit on one row. The reading never depends on which form is shown (the
 * word keeps its width while hidden, and a name that would push the word off the row takes the row either way),
 * so measuring again gives the same answer: no resize loop.
 */
export function statusFits(reading: StatusFitReading): boolean {
  if (reading.available <= 0) {
    return true;
  }
  const name = reading.name > 0 ? reading.name + STATUS_GAP_PX : 0;
  return name + reading.word <= reading.available + 0.5;
}

export function readStatusFit(row: HTMLElement): StatusFitReading {
  const block = row.parentElement ?? row;
  const name = row.querySelector<HTMLElement>(`.${C.name}`);
  const word = row.querySelector<HTMLElement>(`.${C.connectionWord}`);
  return {
    available: block.clientWidth,
    name: name === null ? 0 : name.getBoundingClientRect().width,
    word: word === null ? 0 : word.getBoundingClientRect().width,
  };
}

/** MDI `car` path data (`@mdi/js`, Apache License 2.0), the app's car glyph. */
export const CAR_PATH =
  "M18.92,6.01C18.72,5.42 18.16,5 17.5,5H6.5C5.84,5 5.29,5.42 5.08,6.01L3,12V20A1,1 0 0,0 4,21H5A1,1 0 0,0 6,20V19H18V20A1,1 0 0,0 19,21H20A1,1 0 0,0 21,20V12L18.92,6.01M6.5,16A1.5,1.5 0 0,1 5,14.5A1.5,1.5 0 0,1 6.5,13A1.5,1.5 0 0,1 8,14.5A1.5,1.5 0 0,1 6.5,16M17.5,16A1.5,1.5 0 0,1 16,14.5A1.5,1.5 0 0,1 17.5,13A1.5,1.5 0 0,1 19,14.5A1.5,1.5 0 0,1 17.5,16M5,11L6.5,6.5H17.5L19,11H5Z";

export interface CarButtonInput {
  line: VehicleLineFacts;
  /** How the car was decided at this plug-in, in words (`methodWords`), or `null`. */
  decidedBy: string | null;
  /** The question "which car is plugged in?" is open: the caption asks, and the button is marked. */
  asking: boolean;
  /** The car can be changed here: the button opens Change car (else the car's reading). */
  changeable: boolean;
}

/**
 * The car over the action bar, as in the app: a small caption ("Car · identified by the camera", or "Choose car"
 * while the question is open) over the car's glyph, its name in bold and its charge. The reading's age is not
 * here: the dialog the button opens says it.
 */
export function carButtonElement(doc: Document, language: Language, input: CarButtonInput): HTMLButtonElement {
  const { line } = input;
  const car = translate(language, "carButton.caption");
  const caption = input.asking
    ? translate(language, "carButton.choose")
    : input.decidedBy === null
      ? car
      : `${car} \u00b7 ${input.decidedBy}`;
  const button = element(doc, "button", `${C.button} ${C.barCell} ${C.carButton}`) as HTMLButtonElement;
  button.type = "button";
  button.dataset["carButton"] = line.vehicleId;
  button.dataset["asking"] = String(input.asking);
  button.setAttribute("aria-haspopup", "dialog");
  const captionNode = element(doc, "span", C.barCaption, caption);
  captionNode.setAttribute("aria-hidden", "true");
  const value = element(doc, "span", C.barValue);
  value.setAttribute("aria-hidden", "true");
  const words = element(doc, "span", C.settingsValue);
  const charge = `${line.estimatePrefix}${line.charge}`;
  if (line.name !== null) {
    words.append(element(doc, "strong", C.carButtonName, line.name));
    if (charge !== "") {
      words.append(doc.createTextNode(` \u00b7 ${charge}`));
    }
  } else {
    words.append(doc.createTextNode(charge));
  }
  value.append(
    icon(doc, (svg, ns) => {
      svg.append(fillPath(ns, doc, CAR_PATH));
    }),
    words,
  );
  button.append(captionNode, value);
  const main = line.name === null ? charge : charge === "" ? line.name : `${line.name}, ${charge}`;
  const spoken = [caption, main, input.changeable ? translate(language, "identify.changeCar") : null];
  button.setAttribute("aria-label", spoken.filter((part) => part !== null && part !== "").join(". "));
  return button;
}

export function pauseIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(fillPath(ns, doc, "M7 5h3v14H7zM14 5h3v14h-3z"));
  });
}

export function playIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(fillPath(ns, doc, "M8 5v14l11-7Z"));
  });
}

export function stopIcon(doc: Document): SVGElement {
  return icon(doc, (svg, ns) => {
    svg.append(fillPath(ns, doc, "M7 7h10v10H7z"));
  });
}

export function filledEllipse(ns: string, doc: Document, cx: number, cy: number, rx: number, ry: number): SVGEllipseElement {
  const ellipse = doc.createElementNS(ns, "ellipse") as SVGEllipseElement;
  ellipse.setAttribute("cx", String(cx));
  ellipse.setAttribute("cy", String(cy));
  ellipse.setAttribute("rx", String(rx));
  ellipse.setAttribute("ry", String(ry));
  ellipse.setAttribute("fill", "currentColor");
  return ellipse;
}

export function filledRect(ns: string, doc: Document, x: number, y: number, width: number, height: number): SVGRectElement {
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
export function piggyBankPath(ns: string, doc: Document): SVGElement[] {
  return [
    filledEllipse(ns, doc, 10.5, 13.5, 7.5, 5.5),
    filledEllipse(ns, doc, 18, 13.5, 2.3, 1.9),
    fillPath(ns, doc, "M6.2 8.4 9.8 6.6 8.7 10.8Z"),
    filledRect(ns, doc, 5.8, 17.6, 1.8, 3.2),
    filledRect(ns, doc, 10.2, 18.2, 1.8, 3.2),
    filledRect(ns, doc, 14.6, 17.6, 1.8, 3.2),
  ];
}

export function sunPath(ns: string, doc: Document): (SVGCircleElement | SVGPathElement)[] {
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
export function strategyIcon(doc: Document, strategyId: string | null): SVGElement | null {
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

export function capabilityStateText(language: Language, state: "available" | "unavailable"): string {
  return translate(language, state === "available" ? "cap.available" : "cap.unavailable");
}

/**
 * The issue list, in the model's stable order, with codes only as subdued technical detail; then, for an
 * administrator, the "Get started" section (`setup`) when anything is left to set up. `fix` is given to an
 * administrator only: an issue with a fix then has its button, and every other one its hint.
 */
export function issueListBody(
  doc: Document,
  model: CardModel,
  setup: HTMLElement | null = null,
  fix: ((fix: IssueFix) => void) | null = null,
  admin = true,
): HTMLElement {
  const body = element(doc, "div");
  if (model.issues.length > 0 || setup === null) {
    body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "dialog.issuesIntro")));
  }
  for (const issue of model.issues) {
    body.append(issueRow(doc, model, issue, fix, admin));
  }
  if (setup !== null) {
    body.append(setup);
  }
  return body;
}

export function issueRow(
  doc: Document,
  model: CardModel,
  issue: Issue,
  fix: ((fix: IssueFix) => void) | null,
  admin = true,
): HTMLElement {
  const row = element(doc, "div", C.issueItem);
  row.dataset["code"] = issue.code;
  row.dataset["severity"] = issue.severity;
  // A charger's own setting is set back, and Devices & services opened, by an administrator only; the import target
  // is set aside by anyone.
  const offered =
    fix !== null && issue.fix !== undefined && (admin || issue.fix.action === "release_import_target") ? issue.fix : null;
  const text = issueText(model.language, issue);
  const hint = offered === null && issue.hintKey !== undefined ? ` ${translate(model.language, issue.hintKey)}` : "";
  row.append(element(doc, "span", C.issueText, `${text}${hint}`));
  if (offered !== null && fix !== null) {
    // The charger's own setting, set back by one press: what the backend named, never anything else.
    const answers = element(doc, "div", C.suggestionAnswers);
    const button = doc.createElement("button");
    button.type = "button";
    button.className = C.choiceButton;
    button.dataset["fixEntity"] = offered.entity;
    button.append(element(doc, "span", C.choiceLabel, translate(model.language, offered.labelKey, offered.labelParams)));
    button.addEventListener("click", () => fix(offered));
    answers.append(button);
    row.append(answers);
  }
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
    if (item.noteKey !== undefined) {
      const note = element(doc, "span", C.capabilityNote, translate(model.language, item.noteKey));
      note.dataset["capabilityNote"] = item.key;
      body.append(note);
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
  const shownUnit = priceUnitName(model.format);
  const unit = shownUnit === "" ? translate(language, "plan.missing") : shownUnit;
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
