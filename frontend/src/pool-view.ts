// The heat card's Pool tab (SpotNav 3.0, shadow): the dial for the water the pool should keep, the season open or
// closed (closed: SpotNav touches nothing), what the heat pump and the circulation do now and what SpotNav would do,
// Extra filtrering (+2 h or +4 h, placed in the cheapest or sunniest hours of the next 24), the day band of the hours
// it would filter and heat, and today's filtering hours. Every figure is `get_heat`'s `pool` (`pool/runtime.py`); the
// presses go through `spotnav/pool`, for anyone who sees the card. Text is written with `textContent` only.

import type { NotReady } from "./not-ready-sheet";
import { loadControlLine } from "./spa-view";
import { formatFixed, kwhText } from "./format";
import type { HeatAnswer, HeatPool } from "./heat-model";
import { HEAT_CLASSES as X } from "./heat-styles";
import {
  degrees,
  dialBlock,
  hoursText,
  runBand,
  runsText,
  whenLabel,
  type HeatHandlers,
  type HeatUi,
} from "./heat-view";
import { HOUSE_CLASSES as H } from "./house-styles";
import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

/** The water temperatures the pool's dial offers (`pool/config.py`). */
export const POOL_TARGET_MIN = 18;
export const POOL_TARGET_MAX = 40;
export const POOL_TARGET_STEP = 0.5;

const WHY: readonly string[] = ["sun", "cheap", "need", "extra"];

function why(language: Language, reason: string): string {
  return WHY.includes(reason) ? translate(language, `heat.pool.why.${reason}` as TranslationKey) : "";
}

/** The water now as the dial's line says it ("Vattnet 24,6 °C"), or that it is not read yet. */
export function poolWaterText(language: Language, pool: HeatPool): string {
  return pool.water_c === null
    ? translate(language, pool.heat_pump === null ? "heat.pool.waterNone" : "heat.pool.waterWait")
    : translate(language, "heat.pool.water", { value: degrees(language, pool.water_c) });
}

/** What the pool does now, as read: the season closed, the heat pump heating towards the target, the circulation
 * filtering, or standing still. */
export function poolStatusLine(language: Language, pool: HeatPool): string {
  if (!pool.season_open) {
    return translate(language, "heat.pool.closed");
  }
  const target = degrees(language, pool.target_c);
  if (pool.heat_pump?.heating === true) {
    return pool.water_c === null
      ? translate(language, "heat.pool.heatingTo", { target })
      : translate(language, "heat.pool.heating", { value: degrees(language, pool.water_c), target });
  }
  if (pool.circulation?.on === true) {
    return translate(language, "heat.pool.filtering");
  }
  return translate(language, "heat.pool.still");
}

/** What SpotNav would do now ("Skulle filtrera till 14:00: billig timme"); `null` with the season closed. */
export function poolWouldLine(language: Language, answer: HeatAnswer, pool: HeatPool): string | null {
  const would = pool.would;
  if (would === null || !pool.season_open) {
    return null;
  }
  const until = would.until === null ? null : whenLabel(language, answer, Date.parse(would.until));
  if (would.action === "heat" || would.action === "filter") {
    const base = `heat.pool.would.${would.action}${until === null ? "Now" : ""}` as TranslationKey;
    const text = translate(language, base, { until: until ?? "" });
    const reason = why(language, would.reason);
    return reason === "" ? text : translate(language, "heat.pool.would.with", { text, why: reason });
  }
  if (would.reason === "need_met") {
    return translate(language, "heat.pool.would.met");
  }
  if (would.reason === "no_prices") {
    return translate(language, "heat.pool.would.noPrices");
  }
  return until === null ? translate(language, "heat.pool.would.offNow") : translate(language, "heat.pool.would.off", { until });
}

/** Today's filtering ("Filtrering i dag: 3 h klart, 4 h kvar av 8,3 h") and the heat pump's planned hours. */
export function poolTodayLines(language: Language, pool: HeatPool): { key: string; text: string }[] {
  if (!pool.season_open) {
    return [];
  }
  const today = pool.today;
  const out: { key: string; text: string }[] = [];
  if (pool.circulation !== null) {
    const words = { done: hoursText(language, today.done_h), planned: hoursText(language, today.planned_h) };
    out.push({
      key: "pool-today",
      text: today.need_h === null
        ? translate(language, "heat.pool.today", words)
        : translate(language, today.need_source === "override" ? "heat.pool.todayNeedSet" : "heat.pool.todayNeed", { ...words, need: hoursText(language, today.need_h) }),
    });
  }
  if (pool.heat_pump !== null && today.heat_h !== null) {
    const heat = translate(language, "heat.pool.todayHeat", { hours: hoursText(language, today.heat_h) });
    out.push({ key: "pool-heat", text: today.kwh === null ? heat : `${heat} · ${kwhText(language, today.kwh)}` });
  }
  return out;
}

/** The planned hours in words, by what is done ("Filtrerar 22:00–02:00 · värmer 12:00–14:00"). */
export function poolPlanLine(language: Language, answer: HeatAnswer, pool: HeatPool): string | null {
  if (!pool.season_open) {
    return null;
  }
  const parts: string[] = [];
  for (const what of ["filter", "heat", "extra"] as const) {
    const runs = pool.planned.filter((run) => run.what === what);
    if (runs.length > 0) {
      parts.push(translate(language, `heat.pool.plan.${what}` as TranslationKey, { runs: runsText(language, answer, runs, 2) }));
    }
  }
  return parts.length === 0 ? translate(language, "heat.pool.plan.none") : parts.join(" · ");
}

/** The extra filtering asked for, and when it would run ("Extra filtrering 4 h: 13:00–15:00, 02:00–04:00"). */
export function poolExtraLine(language: Language, answer: HeatAnswer, pool: HeatPool): string | null {
  const extra = pool.extra;
  if (extra === null) {
    return null;
  }
  const runs = pool.planned.filter((run) => run.what === "extra");
  const hours = formatFixed(language, extra.hours, 0);
  return runs.length === 0
    ? translate(language, "heat.pool.extra.waiting", { hours, until: whenLabel(language, answer, Date.parse(extra.until)) })
    : translate(language, "heat.pool.extra.when", { hours, runs: runsText(language, answer, runs, 3) });
}

function pill(doc: Document, text: string, action: string, disabled: boolean, onClick: () => void): HTMLButtonElement {
  const button = element(doc, "button", X.boostButton, text) as HTMLButtonElement;
  button.type = "button";
  button.dataset["action"] = action;
  button.disabled = disabled;
  button.addEventListener("click", onClick);
  return button;
}

function line(doc: Document, className: string, text: string, key: string): HTMLElement {
  const paragraph = element(doc, "p", className, text);
  paragraph.dataset["line"] = key;
  return paragraph;
}

export interface LoadPanelInput {
  doc: Document;
  language: Language;
  answer: HeatAnswer;
  ui: HeatUi;
  handlers: HeatHandlers;
  /** In a box of fixed height: the dial's height; explaining lines are left out. */
  fill: number | null;
  /** What will not be ready in time across the site (`get_plan`'s, read where a bath or a session is not ready). */
  notReady?: NotReady | null;
}

/** The Pool tab's content (see the module). */
export function poolPanel(input: LoadPanelInput, pool: HeatPool, panel: HTMLElement): void {
  const { doc, language, answer, ui, handlers } = input;
  const boxed = input.fill !== null;
  const busy = ui.loadBusy === "pool";
  const target = ui.pendingLoad?.pool ?? pool.target_c;
  panel.append(
    dialBlock(doc, language, {
      target,
      min: POOL_TARGET_MIN,
      max: POOL_TARGET_MAX,
      step: POOL_TARGET_STEP,
      decimals: 1,
      measured: pool.water_c,
      band: null,
      label: translate(language, "heat.pool.dial"),
      sub: poolWaterText(language, pool),
      lowerLabel: translate(language, "heat.pool.lower"),
      raiseLabel: translate(language, "heat.pool.raise"),
      onStep: (delta) => handlers.onLoadStep?.("pool", delta),
      readOnly: handlers.onLoadStep === undefined,
      error: ui.loadError?.pool ?? null,
      height: input.fill,
    }),
  );
  // The season: open (SpotNav plans the pool) or closed (it touches nothing).
  const season = element(doc, "div", X.modes);
  season.setAttribute("role", "group");
  season.setAttribute("aria-label", translate(language, "heat.pool.season"));
  for (const [open, key] of [
    [true, "heat.pool.seasonOpen"],
    [false, "heat.pool.seasonClosed"],
  ] as const) {
    const button = element(doc, "button", X.mode, translate(language, key)) as HTMLButtonElement;
    button.type = "button";
    button.dataset["mode"] = open ? "season-open" : "season-closed";
    button.setAttribute("aria-pressed", String(pool.season_open === open));
    button.disabled = busy;
    button.addEventListener("click", () => {
      if (pool.season_open !== open) {
        handlers.onPool?.({ action: "season", open });
      }
    });
    season.append(button);
  }
  panel.append(season);
  const lines = element(doc, "div", X.statusBlock);
  lines.append(line(doc, X.status, poolStatusLine(language, pool), "pool-status"));
  const would = poolWouldLine(language, answer, pool);
  if (would !== null) {
    lines.append(line(doc, X.status, would, "pool-would"));
  }
  panel.append(lines);
  if (pool.season_hint) {
    panel.append(line(doc, `${H.note} ${C.muted}`, translate(language, "heat.pool.seasonHint"), "pool-season-hint"));
  }
  // Extra filtrering: only with a circulation pump and the season open.
  if (pool.season_open && pool.circulation !== null) {
    const row = element(doc, "div", X.boost);
    row.dataset["block"] = "pool-extra";
    const extra = poolExtraLine(language, answer, pool);
    if (extra === null) {
      row.append(line(doc, X.boostLine, translate(language, "heat.pool.extra.label"), "pool-extra-label"));
      for (const hours of [2, 4] as const) {
        row.append(
          pill(doc, translate(language, "heat.pool.extra.add", { hours: String(hours) }), `pool-extra-${hours}`, busy, () =>
            handlers.onPool?.({ action: "extra_filtering", hours }),
          ),
        );
      }
    } else {
      row.append(line(doc, X.boostLine, extra, "pool-extra"));
      row.append(pill(doc, translate(language, "heat.pool.extra.cancel"), "pool-extra-cancel", busy, () => handlers.onPool?.({ action: "cancel_extra" })));
    }
    panel.append(row);
  }
  if (pool.season_open) {
    panel.append(runBand(doc, language, answer, pool.planned, poolPlanLine(language, answer, pool), "pool-band"));
  }
  if (boxed) {
    return;
  }
  for (const today of poolTodayLines(language, pool)) {
    panel.append(line(doc, H.line, today.text, today.key));
  }
  if (pool.season_open) {
    const controlled = poolControlLines(language, answer, pool);
    if (controlled.length === 0) {
      panel.append(line(doc, `${H.note} ${C.muted}`, translate(language, "heat.pool.watched"), "pool-watched"));
    }
    for (const item of controlled) {
      panel.append(line(doc, `${H.note} ${C.muted}`, item.text, item.key));
    }
  }
}

/** Each part SpotNav controls, in words ("Cirkulationen: Styrs av SpotNav."); none while it only watches both. */
export function poolControlLines(language: Language, answer: HeatAnswer, pool: HeatPool): { key: string; text: string }[] {
  const out: { key: string; text: string }[] = [];
  for (const [key, part, name] of [
    ["pool-control-heat-pump", pool.heat_pump, "heat.pool.partHeatPump"],
    ["pool-control-circulation", pool.circulation, "heat.pool.partCirculation"],
  ] as const) {
    if (part === null) {
      continue;
    }
    const text = loadControlLine(language, answer, part);
    if (text !== null) {
      out.push({ key, text: translate(language, "heat.loadControl.part", { name: translate(language, name), text }) });
    }
  }
  return out;
}
