// The charge bar under the status line: Home Assistant decides the numbers (`progress`), the card only
// words and draws them, as the app does. Also the reads after a Start or Stop: every few seconds until
// the charge follows, so the bar appears or goes without waiting for the 30-second cycle.

import { formatNumber, hasZone, type FormatContext } from "./format";
import { translate } from "./i18n";
import { momentText } from "./status";
import type { ChargeBarBasis, ChargeBarBlock, Dashboard } from "./validate";

/** What the view draws: the share done (0..1, `null` for the open bar), whether it moves, and its words. */
export interface ChargeBarFacts {
  basis: ChargeBarBasis;
  percent: number | null;
  moving: boolean;
  /** "62 % klart · klart ca 14:35", "93 % av målet", "Laddar · 11 kW". */
  text: string;
  /** The bar's accessible name. */
  label: string;
}

/** The power in kW with one decimal in the reader's language, a whole number without ",0". */
function kwText(format: FormatContext, kw: number): string {
  return formatNumber(format.language, Math.round(kw * 10) / 10, 1);
}

export function chargeBarFor(progress: ChargeBarBlock | null, format: FormatContext, nowMs: number): ChargeBarFacts | null {
  if (progress === null) {
    return null;
  }
  const language = format.language;
  const percent = progress.percent;
  let head: string;
  if (percent === null) {
    head =
      progress.power_kw !== null && progress.power_kw > 0
        ? translate(language, "chargeBar.chargingPower", { kw: kwText(format, progress.power_kw) })
        : translate(language, "chargeBar.charging");
  } else {
    const key = progress.basis === "target" ? "chargeBar.ofTarget" : "chargeBar.done";
    head = translate(language, key, { percent: String(percent) });
  }
  const end =
    progress.ends_at_ms !== null && hasZone(format)
      ? translate(language, "chargeBar.ends", { time: momentText(format, progress.ends_at_ms, nowMs) })
      : null;
  return {
    basis: progress.basis,
    percent,
    moving: progress.moving,
    text: end === null ? head : `${head} · ${end}`,
    label: translate(language, "chargeBar.description"),
  };
}

/** How often the card reads while it waits for a Start or Stop to show, and for how long at most. */
export const OUTCOME_POLL_MS = 5_000;
export const OUTCOME_WINDOW_MS = 120_000;

/** The outcome a Start or Stop waits for: `live.charging` as it should read, and until when. */
export interface OutcomeWatch {
  charging: boolean;
  untilMs: number;
}

export function outcomeWatchFor(action: string, nowMs: number): OutcomeWatch | null {
  if (action !== "start" && action !== "stop") {
    return null;
  }
  return { charging: action === "start", untilMs: nowMs + OUTCOME_WINDOW_MS };
}

/** Whether the wait is over: the charge reads as the action asked, or the two minutes are up. */
export function outcomeSettled(watch: OutcomeWatch, dashboard: Dashboard | null, nowMs: number): boolean {
  return nowMs >= watch.untilMs || (dashboard !== null && dashboard.live.charging === watch.charging);
}
