// The heat card's Spa and Sauna tabs (SpotNav 3.0, shadow): the dial for the bathing (or the sauna's) temperature, the
// bath times or sessions like a car's departures (weekdays and one date), the spa's idle temperature between baths,
// "Bada nu" / "Värm nu", the status ("Klar 18:40, värmer 2 °C/h"), what SpotNav would do, a bath that will not be ready
// with its choices (take what fits, a later time, allow the peak), and the day band of the heat-up and the hold. The
// sauna is never heated but for a booked session or a press; a lock that reads, or a relay without its declared safety
// measure, is said plainly. Every figure is `get_heat`'s `spa` or `sauna` (`spa/runtime.py`); the presses go through
// `spotnav/spa` and `spotnav/sauna`, for anyone who sees the card. Text is written with `textContent` only.

import { formatNumber } from "./format";
import type { HeatAnswer, HeatNotReady, HeatSauna, HeatSpa } from "./heat-model";
import { HEAT_CLASSES as X } from "./heat-styles";
import { context, degrees, dialBlock, heatClock, instantToLocal, runBand, runsText, whenLabel } from "./heat-view";
import { notReadySheet } from "./not-ready-sheet";
import { HOUSE_CLASSES as H } from "./house-styles";
import { translate, type Language, type TranslationKey } from "./i18n";
import type { LoadPanelInput } from "./pool-view";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

/** The dials' ranges (`spa/config.py`, `sauna/config.py`). */
export const SPA_BATH_MIN = 20;
export const SPA_BATH_MAX = 40;
export const SAUNA_TARGET_MIN = 40;
export const SAUNA_TARGET_MAX = 110;

export type DeadlineKind = "spa" | "sauna";
type Deadline = HeatSpa | HeatSauna;

/** A weekday's short name, Monday (1) to Sunday (7), as a car's departures, in the card's language. */
export function weekdayShort(language: Language, day: number): string {
  try {
    // 2024-01-01 was a Monday.
    return new Intl.DateTimeFormat(language, { weekday: "short", timeZone: "UTC" }).format(new Date(Date.UTC(2024, 0, day)));
  } catch {
    return String(day);
  }
}

/** Weekdays in words, runs of three or more joined ("mån–fre", "lör, sön"); every day is "varje dag". */
export function weekdaysText(language: Language, days: readonly number[]): string {
  const sorted = [...new Set(days)].filter((day) => day >= 1 && day <= 7).sort((a, b) => a - b);
  if (sorted.length === 7) {
    return translate(language, "heat.spa.everyDay");
  }
  const parts: string[] = [];
  let index = 0;
  while (index < sorted.length) {
    let end = index;
    while (end + 1 < sorted.length && sorted[end + 1] === sorted[end]! + 1) {
      end += 1;
    }
    if (end - index >= 2) {
      parts.push(`${weekdayShort(language, sorted[index]!)}–${weekdayShort(language, sorted[end]!)}`);
    } else {
      for (let day = index; day <= end; day += 1) {
        parts.push(weekdayShort(language, sorted[day]!));
      }
    }
    index = end + 1;
  }
  return parts.join(", ");
}

/** The weekly times and the one-off in words ("fre, lör 19:00 · en gång tis 24 nov. 18:00"); none: "Inga badtider". */
export function sessionsText(
  language: Language,
  answer: HeatAnswer,
  kind: DeadlineKind,
  weekly: readonly { weekdays: number[]; time: string }[],
  once: string | null,
): string {
  const parts = weekly.map((item) => `${weekdaysText(language, item.weekdays)} ${item.time}`);
  if (once !== null && Number.isFinite(Date.parse(once))) {
    parts.push(translate(language, "heat.spa.once", { when: whenLabel(language, answer, Date.parse(once)) }));
  }
  return parts.length === 0 ? translate(language, kind === "spa" ? "heat.spa.noBaths" : "heat.sauna.noSessions") : parts.join(" · ");
}

function temperatureOf(load: Deadline): number | null {
  return "water_c" in load ? load.water_c : load.temp_c;
}

function targetOf(load: Deadline): number {
  return "bath_c" in load ? load.bath_c : load.target_c;
}

/** The next session's time in words, or `null` without one. */
function nextText(language: Language, answer: HeatAnswer, load: Deadline): string | null {
  const next = "next_bath" in load ? load.next_bath : load.next_session;
  return next === null ? null : whenLabel(language, answer, Date.parse(next.at));
}

/** The status as read and planned: "Klar 18:40, värmer 2 °C/h", ready, idle at its temperature, or the next time. */
export function deadlineStatusLine(language: Language, answer: HeatAnswer, kind: DeadlineKind, load: Deadline): string {
  const now = temperatureOf(load);
  const nowText = now === null ? null : degrees(language, now);
  const ready = load.ready_at === null ? null : heatClock(language, answer, Date.parse(load.ready_at));
  if (load.state === "heating" && ready !== null) {
    // The learnt rate is said once SpotNav has learnt it (the spa's: the sauna's heat-up is said by its time).
    if (kind === "spa" && load.rate_learned) {
      return translate(language, "heat.spa.readyRate", { time: ready, rate: formatNumber(language, load.rate_c_h, 1) });
    }
    return translate(language, "heat.spa.readyAt", { time: ready });
  }
  if (load.state === "ready") {
    return nowText === null ? translate(language, `heat.${kind}.ready` as TranslationKey) : translate(language, `heat.${kind}.readyNow` as TranslationKey, { value: nowText });
  }
  const next = nextText(language, answer, load);
  if (kind === "spa" && "idle_c" in load) {
    const idle = degrees(language, load.idle_c);
    return next === null ? translate(language, "heat.spa.idle", { idle }) : translate(language, "heat.spa.idleNext", { idle, when: next });
  }
  return next === null ? translate(language, "heat.sauna.off") : translate(language, "heat.sauna.offNext", { when: next });
}

const WOULD_WHY: readonly string[] = ["sun", "cheap", "bath", "now", "floor"];

/** What SpotNav would do now ("Skulle värma till 18:45: billig timme"); for the sauna also that a lock holds it or
 * that it must be started by hand. */
export function deadlineWouldLine(language: Language, answer: HeatAnswer, kind: DeadlineKind, load: Deadline): string | null {
  const would = load.would;
  if (would === null) {
    return null;
  }
  const until = would.until === null ? null : whenLabel(language, answer, Date.parse(would.until));
  const target = degrees(language, targetOf(load));
  switch (would.action) {
    case "heat": {
      const text = until === null ? translate(language, "heat.spa.would.heatNow") : translate(language, "heat.spa.would.heat", { until });
      return WOULD_WHY.includes(would.reason) ? translate(language, "heat.spa.would.with", { text, why: translate(language, `heat.spa.why.${would.reason}` as TranslationKey) }) : text;
    }
    case "hold":
      return until === null ? translate(language, "heat.spa.would.holdNow", { target }) : translate(language, "heat.spa.would.hold", { target, until });
    case "idle":
      return "idle_c" in load ? translate(language, "heat.spa.would.idle", { idle: degrees(language, load.idle_c) }) : null;
    case "floor":
      return "floor_c" in load ? translate(language, "heat.spa.would.floor", { floor: degrees(language, load.floor_c) }) : null;
    case "blocked":
      return translate(language, "heat.sauna.would.blocked");
    case "start_by_hand": {
      const next = nextText(language, answer, load);
      return next === null ? translate(language, "heat.sauna.would.byHandNow") : translate(language, "heat.sauna.would.byHand", { when: next });
    }
    default:
      return translate(language, `heat.${kind}.would.off` as TranslationKey);
  }
}

/** A session that will not be ready, in words ("Badet 19:00 blir inte klart i tid: når 36 °C, säkringen räcker inte"). */
export function notReadyLine(language: Language, answer: HeatAnswer, kind: DeadlineKind, notReady: HeatNotReady): string {
  const causes = ["rate", "import_target", "power_level", "fuse"];
  const cause = causes.includes(notReady.cause) ? translate(language, `heat.spa.cause.${notReady.cause}` as TranslationKey) : "";
  const text = translate(language, `heat.${kind}.notReady` as TranslationKey, {
    when: whenLabel(language, answer, Date.parse(notReady.at)),
    reach: degrees(language, notReady.reach_c),
  });
  return cause === "" ? text : `${text} ${cause}`;
}

/** The choices a session that will not be ready offers: their action and words. */
export function notReadyChoices(language: Language, answer: HeatAnswer, kind: DeadlineKind, notReady: HeatNotReady): { action: string; label: string }[] {
  const out = [{ action: "take_what_fits", label: translate(language, "heat.spa.choice.fits", { reach: degrees(language, notReady.reach_c) }) }];
  if (notReady.later !== null) {
    out.push({
      action: kind === "spa" ? "later_bath" : "later_session",
      label: translate(language, `heat.${kind}.choice.later` as TranslationKey, { time: whenLabel(language, answer, Date.parse(notReady.later)) }),
    });
  }
  if (notReady.cause === "import_target" || notReady.cause === "power_level") {
    out.push({ action: "accept_peak", label: translate(language, "heat.spa.choice.peak") });
  }
  return out;
}

/** The states a load's control is in once its opt-in is on (`loads/hold_part.py`). */
export const CONTROL_STATES: readonly string[] = ["controlled", "paused", "handed_back"];
const CONTROL_WHY: readonly string[] = [
  "no_read_back", "write_cap", "starts_cap", "fault", "automation", "unavailable", "foreign_mode", "own_scheduler",
  "unsupported", "interlock", "run_on", "floor", "lock", "max_on", "lock_unreadable", "pump_not_ours", "restore_waits",
  "stale_plan",
];

/**
 * A controlled load's line ("Styrs av SpotNav.", "Pausad till i morgon 00:00: dagens ändringar är använda.",
 * "Återlämnad till dig till …"), with the writes that did not read back; `null` for a load SpotNav only watches.
 */
export function loadControlLine(
  language: Language,
  answer: HeatAnswer,
  control: { state: string; reason: string | null; paused_until: string | null; failures: number },
): string | null {
  if (!CONTROL_STATES.includes(control.state)) {
    return null;
  }
  const reason = control.reason ?? "";
  const why = CONTROL_WHY.includes(reason) ? translate(language, `heat.loadControl.why.${reason}` as TranslationKey) : null;
  const until = control.paused_until === null ? null : whenLabel(language, answer, Date.parse(control.paused_until));
  let text: string;
  if (control.state === "handed_back") {
    text = translate(language, "heat.loadControl.handedBack", { until: until ?? "" });
  } else if (control.state === "paused") {
    const words = why ?? translate(language, "heat.loadControl.why.unavailable");
    text = until === null ? translate(language, "heat.loadControl.pausedNow", { why: words }) : translate(language, "heat.loadControl.paused", { until, why: words });
  } else if (reason === "cloud_slow") {
    text = translate(language, "heat.loadControl.cloud");
  } else {
    text = why === null ? translate(language, "heat.loadControl.controlled") : translate(language, "heat.loadControl.controlledWhy", { why });
  }
  if (control.failures > 0) {
    text = `${text} ${translate(language, "heat.loadControl.failures", { count: String(control.failures) })}`;
  }
  return text;
}

/** The control's note: SpotNav's control (`loadControlLine`), else watched only (and why) or slow through a cloud. */
export function deadlineControlLine(language: Language, kind: DeadlineKind, load: Deadline, answer?: HeatAnswer): string | null {
  if (answer !== undefined) {
    const controlled = loadControlLine(language, answer, load.control);
    if (controlled !== null) {
      return controlled;
    }
  }
  const reason = load.control.reason;
  if (reason === "no_safety") {
    return translate(language, "heat.sauna.noSafety");
  }
  if (reason === "cloud_slow") {
    return translate(language, "heat.spa.cloud");
  }
  if (load.control.state === "watch_only") {
    return translate(language, `heat.${kind}.watchOnly` as TranslationKey);
  }
  return translate(language, `heat.${kind}.watched` as TranslationKey);
}

/** The planned heat-up and hold in words ("Värms 17:00–18:45 · hålls varmt till 21:00"). */
export function deadlinePlanLine(language: Language, answer: HeatAnswer, kind: DeadlineKind, load: Deadline): string | null {
  const heat = load.planned.filter((run) => run.what === "heat");
  const hold = load.planned.filter((run) => run.what === "hold");
  const parts: string[] = [];
  if (heat.length > 0) {
    parts.push(translate(language, `heat.${kind}.plan.heat` as TranslationKey, { runs: runsText(language, answer, heat, 2) }));
  }
  if (hold.length > 0) {
    parts.push(translate(language, `heat.${kind}.plan.hold` as TranslationKey, { until: heatClock(language, answer, Date.parse(hold[hold.length - 1]!.end)) }));
  }
  return parts.length === 0 ? null : parts.join(" · ");
}

function line(doc: Document, className: string, text: string, key: string): HTMLElement {
  const paragraph = element(doc, "p", className, text);
  paragraph.dataset["line"] = key;
  return paragraph;
}

function pill(doc: Document, text: string, action: string, disabled: boolean, onClick: () => void): HTMLButtonElement {
  const button = element(doc, "button", X.boostButton, text) as HTMLButtonElement;
  button.type = "button";
  button.dataset["action"] = action;
  button.disabled = disabled;
  button.addEventListener("click", onClick);
  return button;
}

/** A row with a value and its "Ändra" (the times, the idle temperature). */
function editRow(doc: Document, language: Language, label: string, value: string, key: string, onEdit: (() => void) | null): HTMLElement {
  const row = element(doc, "p", `${X.awayLine} ${H.line}`);
  row.dataset["line"] = key;
  row.append(doc.createTextNode(`${label}: ${value}`));
  if (onEdit !== null) {
    const change = element(doc, "button", undefined, translate(language, "heat.mode.change")) as HTMLButtonElement;
    change.type = "button";
    change.dataset["action"] = `${key}-change`;
    change.setAttribute("aria-label", `${label}: ${translate(language, "heat.mode.change")}`);
    change.addEventListener("click", onEdit);
    row.append(change);
  }
  return row;
}

/** The Spa or the Sauna tab's content (see the module). */
export function deadlinePanel(input: LoadPanelInput, kind: DeadlineKind, load: Deadline, panel: HTMLElement): void {
  const { doc, language, answer, ui, handlers } = input;
  const boxed = input.fill !== null;
  const busy = ui.loadBusy === kind;
  const spa = kind === "spa" ? (load as HeatSpa) : null;
  const sauna = kind === "sauna" ? (load as HeatSauna) : null;
  const target = ui.pendingLoad?.[kind] ?? targetOf(load);
  const now = temperatureOf(load);
  panel.append(
    dialBlock(doc, language, {
      target,
      min: spa !== null ? SPA_BATH_MIN : SAUNA_TARGET_MIN,
      max: spa !== null ? SPA_BATH_MAX : SAUNA_TARGET_MAX,
      step: spa !== null ? 0.5 : 1,
      decimals: spa !== null ? 1 : 0,
      measured: now,
      band: null,
      label: translate(language, `heat.${kind}.dial` as TranslationKey),
      sub: now === null ? translate(language, `heat.${kind}.tempNone` as TranslationKey) : translate(language, `heat.${kind}.temp` as TranslationKey, { value: degrees(language, now) }),
      lowerLabel: translate(language, `heat.${kind}.lower` as TranslationKey),
      raiseLabel: translate(language, `heat.${kind}.raise` as TranslationKey),
      onStep: (delta) => handlers.onLoadStep?.(kind, delta),
      readOnly: handlers.onLoadStep === undefined,
      error: ui.loadError?.[kind] ?? null,
      height: input.fill,
    }),
  );
  const lines = element(doc, "div", X.statusBlock);
  lines.append(line(doc, X.status, deadlineStatusLine(language, answer, kind, load), `${kind}-status`));
  if (sauna !== null && sauna.locked === true) {
    lines.append(line(doc, X.status, translate(language, "heat.sauna.locked"), "sauna-locked"));
  }
  const would = deadlineWouldLine(language, answer, kind, load);
  if (would !== null) {
    lines.append(line(doc, X.status, would, `${kind}-would`));
  }
  panel.append(lines);
  // A session that will not be ready: its words and the choices that answer it. Where more than this one deadline
  // will not be ready in time across the site, the sheet of them all in its place (the phones' combined notice opens it).
  const sheet = input.notReady ?? null;
  if (load.not_ready !== null && sheet !== null && sheet.items.length > 1 && sheet.items.some((item) => item.kind === kind)) {
    panel.append(notReadySheet(doc, context(language, answer), sheet, answer.today?.money ?? null, handlers.onNotReady === undefined ? undefined : (press) => handlers.onNotReady?.(press)));
  } else if (load.not_ready !== null) {
    const block = element(doc, "div", X.boost);
    block.dataset["block"] = `${kind}-not-ready`;
    block.append(line(doc, X.boostLine, notReadyLine(language, answer, kind, load.not_ready), `${kind}-not-ready`));
    for (const choice of notReadyChoices(language, answer, kind, load.not_ready)) {
      block.append(pill(doc, choice.label, `${kind}-${choice.action}`, busy, () => handlers.onDeadline?.(kind, choice.action)));
    }
    panel.append(block);
  }
  // Bada nu / Värm nu, or the press that runs with its end.
  const nowRow = element(doc, "div", X.boost);
  nowRow.dataset["block"] = `${kind}-now`;
  const pressed = spa !== null ? spa.bath_now : sauna!.session_now;
  const nowAction = spa !== null ? "bath_now" : "session_now";
  if (pressed === null) {
    nowRow.append(pill(doc, translate(language, `heat.${kind}.now` as TranslationKey), `${kind}-now`, busy, () => handlers.onDeadline?.(kind, nowAction)));
  } else {
    nowRow.append(line(doc, X.boostLine, translate(language, `heat.${kind}.nowUntil` as TranslationKey, { until: whenLabel(language, answer, Date.parse(pressed.until)) }), `${kind}-now`));
    nowRow.append(pill(doc, translate(language, "heat.spa.cancelNow"), `${kind}-cancel-now`, busy, () => handlers.onDeadline?.(kind, "cancel_now")));
  }
  panel.append(nowRow);
  const onEdit = handlers.onLoadEdit === undefined ? null : (what: "times" | "idle") => () => handlers.onLoadEdit?.(kind, what);
  const weekly = spa !== null ? spa.baths : sauna!.sessions;
  const once = spa !== null ? spa.bath_once : sauna!.session_once;
  panel.append(
    editRow(doc, language, translate(language, `heat.${kind}.times` as TranslationKey), sessionsText(language, answer, kind, weekly, once), `${kind}-times`, onEdit === null ? null : onEdit("times")),
  );
  if (spa !== null) {
    panel.append(editRow(doc, language, translate(language, "heat.spa.idleLabel"), degrees(language, spa.idle_c), "spa-idle", onEdit === null ? null : onEdit("idle")));
  }
  panel.append(runBand(doc, language, answer, load.planned, deadlinePlanLine(language, answer, kind, load), `${kind}-band`));
  if (boxed) {
    return;
  }
  const note = deadlineControlLine(language, kind, load, answer);
  if (note !== null) {
    panel.append(line(doc, `${H.note} ${C.muted}`, note, `${kind}-control`));
  }
}

/** The day a one-off session is on and its time, as the editor's fields take them ("2026-11-24", "18:00"). */
export function onceFields(answer: HeatAnswer, once: string | null): { date: string; time: string } | null {
  return once === null || !Number.isFinite(Date.parse(once)) ? null : instantToLocal(Date.parse(once), answer.time_zone);
}
