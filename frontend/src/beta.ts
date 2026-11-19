// The beta tester box and the hardware test in Settings → Support (administrators only): `spotnav/beta`.
//
// The "Betatestare" box (off by default) with its "Läs mer" text, the optional e-mail address, the one line once the
// 14-day period has ended by itself ("nothing is sent any more"), and "Kör hårdvarutest": started by the
// administrator, its steps shown live (the box reads the command again while a test runs), its result, and "Skicka"
// when beta is off (beta sends a finished test by itself). The backend decides everything; the card only shows the
// answer; a missing or mistyped known key is no answer, an unknown key is ignored (see `contract.ts`).

import { translate, type Language, type TranslationKey } from "./i18n";
import { hasAllKeys, hasUnknownKeys } from "./contract";
import { PRIVACY_URL, MAX_EMAIL_CHARS, sendFailureText } from "./send-debug";
import { BETA_API_VERSION } from "./types";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";

export { BETA_API_VERSION };
/** How often a running test is read again (ms). */
export const TEST_POLL_MS = 3000;

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export type BetaState = { on: boolean; startedAt: string | null; endsAt: string | null; ended: boolean; email: string | null };
export type TestStep = { part: string; step: string; status: string; reason: string | null };
export type TestState = {
  state: "idle" | "running" | "done";
  blocked: string | null;
  nextAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  result: "passed" | "partial" | "skipped" | "failed" | null;
  steps: TestStep[];
  sentRef: string | null;
};
export type BetaAnswer = {
  ok: boolean;
  error: string | null;
  beta: BetaState | null;
  test: TestState | null;
  ref: string | null;
  retryAfterS: number | null;
};

/** One request: `action` and, for `set`, the changes. The card adds the site or charger. */
export type BetaRequest = { action: "get" | "set" | "start_test" | "stop_test" | "send_test"; changes?: Record<string, unknown> };
export type BetaCall = (request: BetaRequest) => Promise<unknown>;

const ANSWER_KEYS = ["api_version", "ok", "error", "beta", "test", "ref", "retry_after_s"];
const BETA_KEYS = ["on", "started_at", "ends_at", "ended", "email"];
const TEST_KEYS = ["state", "blocked", "next_at", "started_at", "finished_at", "result", "steps", "sent_ref"];
const STEP_KEYS = ["part", "step", "status", "reason"];
const TEST_STATES = ["idle", "running", "done"];
const RESULTS = ["passed", "partial", "skipped", "failed"];

function record(value: unknown, keys: readonly string[]): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const object = value as Record<string, unknown>;
  if (!hasAllKeys(object, keys) || hasUnknownKeys(object, keys)) {
    return null;
  }
  return object;
}

const text = (value: unknown): value is string => typeof value === "string";
const textOrNull = (value: unknown): value is string | null => value === null || typeof value === "string";

function decodeBeta(value: unknown): BetaState | null | undefined {
  if (value === null) {
    return null;
  }
  const raw = record(value, BETA_KEYS);
  if (
    raw === null || typeof raw.on !== "boolean" || typeof raw.ended !== "boolean" || !textOrNull(raw.started_at)
    || !textOrNull(raw.ends_at) || !textOrNull(raw.email)
  ) {
    return undefined;
  }
  return { on: raw.on, startedAt: raw.started_at, endsAt: raw.ends_at, ended: raw.ended, email: raw.email };
}

function decodeStep(value: unknown): TestStep | null {
  const raw = record(value, STEP_KEYS);
  if (raw === null || !text(raw.part) || !text(raw.step) || !text(raw.status) || !textOrNull(raw.reason)) {
    return null;
  }
  return { part: raw.part, step: raw.step, status: raw.status, reason: raw.reason };
}

function decodeTest(value: unknown): TestState | null | undefined {
  if (value === null) {
    return null;
  }
  const raw = record(value, TEST_KEYS);
  if (
    raw === null || !text(raw.state) || !TEST_STATES.includes(raw.state) || !textOrNull(raw.blocked)
    || !textOrNull(raw.next_at) || !textOrNull(raw.started_at) || !textOrNull(raw.finished_at)
    || !(raw.result === null || (text(raw.result) && RESULTS.includes(raw.result))) || !Array.isArray(raw.steps)
    || !textOrNull(raw.sent_ref)
  ) {
    return undefined;
  }
  const steps = raw.steps.map(decodeStep);
  if (steps.some((step) => step === null)) {
    return undefined;
  }
  return {
    state: raw.state as TestState["state"],
    blocked: raw.blocked,
    nextAt: raw.next_at,
    startedAt: raw.started_at,
    finishedAt: raw.finished_at,
    result: raw.result as TestState["result"],
    steps: steps as TestStep[],
    sentRef: raw.sent_ref,
  };
}

/** `spotnav/beta`'s answer, or `null` when it is malformed, of another version or carries a key the card does not know. */
export function decodeBetaAnswer(value: unknown): BetaAnswer | null {
  const raw = record(value, ANSWER_KEYS);
  if (raw === null || raw.api_version !== BETA_API_VERSION || typeof raw.ok !== "boolean" || !textOrNull(raw.error)) {
    return null;
  }
  const beta = decodeBeta(raw.beta);
  const test = decodeTest(raw.test);
  const wait = raw.retry_after_s;
  if (beta === undefined || test === undefined || !textOrNull(raw.ref)) {
    return null;
  }
  if (!(wait === null || (typeof wait === "number" && Number.isFinite(wait) && wait >= 0))) {
    return null;
  }
  return { ok: raw.ok, error: raw.error, beta, test, ref: raw.ref, retryAfterS: wait };
}

function dateText(language: Language, iso: string, withTime: boolean): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat(language, withTime ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" }).format(at);
}

const STEP_LABELS: Record<string, TranslationKey> = {
  "battery:hold": "test.step.battery.hold",
  "battery:charge": "test.step.battery.charge",
  "battery:discharge": "test.step.battery.discharge",
  "battery:hand_back": "test.step.battery.handBack",
  "battery:battery": "test.step.battery",
  "heat:engage": "test.step.heat.engage",
  "heat:release": "test.step.heat.release",
  "heat:heat": "test.step.heat",
};

const RESULT_LABELS: Record<NonNullable<TestState["result"]>, TranslationKey> = {
  passed: "test.result.passed",
  partial: "test.result.partial",
  skipped: "test.result.skipped",
  failed: "test.result.failed",
};

const STATUS_LABELS: Record<string, TranslationKey> = {
  pending: "test.status.pending",
  running: "test.status.running",
  passed: "test.status.passed",
  failed: "test.status.failed",
  not_judgeable: "test.status.notJudgeable",
  skipped: "test.status.skipped",
  aborted: "test.status.aborted",
};

/** One step's line, in the reader's words (a step or status the card does not know is shown by its code). */
export function stepText(language: Language, step: TestStep): string {
  const label = STEP_LABELS[`${step.part}:${step.step}`];
  const status = STATUS_LABELS[step.status];
  return `${label === undefined ? step.step : translate(language, label)}: ${status === undefined ? step.status : translate(language, status)}`;
}

/** Why a test cannot start now, in one sentence; `null` when it can. */
export function blockedText(language: Language, test: TestState): string | null {
  switch (test.blocked) {
    case null:
      return null;
    case "running":
      return null;
    case "once_a_day":
      return test.nextAt === null
        ? translate(language, "test.blocked.later")
        : translate(language, "test.blocked.onceADay", { time: dateText(language, test.nextAt, true) });
    case "nothing_to_test":
      return translate(language, "test.blocked.nothing");
    default:
      return translate(language, "test.blocked.later");
  }
}

export interface BetaBoxInput {
  doc: Document;
  language: Language;
  idPrefix: string;
  call: BetaCall;
  /** Every answer read, so the Support row's value can follow it. */
  onAnswer?: (answer: BetaAnswer) => void;
}

/** The Support row's value: off, on until a date, or the one line once the period ended by itself. */
export function betaSummary(language: Language, answer: BetaAnswer | null): string {
  const beta = answer?.beta ?? null;
  if (beta === null) {
    return "…";
  }
  if (beta.on && beta.endsAt !== null) {
    return translate(language, "beta.until", { date: dateText(language, beta.endsAt, false) });
  }
  return beta.ended ? translate(language, "beta.ended") : translate(language, "settings.value.off");
}

/** Read the state once for the Support row; `null` when it cannot be read. */
export async function readBeta(call: BetaCall): Promise<BetaAnswer | null> {
  try {
    return decodeBetaAnswer(await call({ action: "get" }));
  } catch {
    return null;
  }
}

/**
 * The box as a dialog's body: a form (never submitted) with the box and one Close, as the value editors are drawn,
 * so the settings overview itself holds no input.
 */
export function betaEditor(input: BetaBoxInput, onClose: () => void): HTMLFormElement {
  const form = input.doc.createElement("form");
  form.dataset["beta"] = "form";
  form.noValidate = true;
  form.addEventListener("submit", (event) => event.preventDefault());
  const actions = element(input.doc, "div", C.settingsActions);
  const close = element(input.doc, "button", `${C.button} ${C.settingsSave}`, translate(input.language, "dialog.close")) as HTMLButtonElement;
  close.type = "button";
  close.dataset["beta"] = "close";
  close.addEventListener("click", () => onClose());
  actions.append(close);
  form.append(betaBox(input), actions);
  return form;
}

/**
 * The box: built empty, filled by the first read, read again after each action and every `TEST_POLL_MS` while a test
 * runs (until the box leaves the page).
 */
export function betaBox(input: BetaBoxInput): HTMLElement {
  const { doc, language, idPrefix } = input;
  const say = (key: TranslationKey, values?: Record<string, string>): string => translate(language, key, values);
  const root = element(doc, "div");
  root.dataset["beta"] = "box";

  const check = doc.createElement("input");
  check.type = "checkbox";
  check.id = `${idPrefix}-beta-on`;
  check.dataset["betaField"] = "on";
  check.disabled = true;
  const label = element(doc, "label", C.settingsLabel, say("beta.label"));
  label.setAttribute("for", check.id);
  const moreButton = element(doc, "button", C.button, say("beta.more")) as HTMLButtonElement;
  moreButton.type = "button";
  moreButton.dataset["beta"] = "more";
  moreButton.setAttribute("aria-expanded", "false");
  const checkRow = element(doc, "div", C.settingsCheckRow);
  checkRow.append(check, label, moreButton);

  const more = element(doc, "div", C.muted);
  more.dataset["betaMore"] = "";
  more.hidden = true;
  const privacy = doc.createElement("a");
  privacy.href = PRIVACY_URL;
  privacy.target = "_blank";
  privacy.rel = "noopener noreferrer";
  privacy.textContent = say("sendDebug.privacy");
  const keys: TranslationKey[] = ["beta.more.gets", "beta.more.sent", "beta.more.email", "beta.more.never", "beta.more.test"];
  for (const key of keys) {
    more.append(element(doc, "p", undefined, say(key)));
  }
  more.append(privacy);
  moreButton.addEventListener("click", () => {
    more.hidden = !more.hidden;
    moreButton.setAttribute("aria-expanded", String(!more.hidden));
    moreButton.textContent = say(more.hidden ? "beta.more" : "beta.less");
  });

  const status = element(doc, "p", C.muted);
  status.dataset["beta"] = "status";
  status.setAttribute("role", "status");

  const email = doc.createElement("input");
  email.type = "email";
  email.id = `${idPrefix}-beta-email`;
  email.className = C.settingsInput;
  email.maxLength = MAX_EMAIL_CHARS;
  email.autocomplete = "email";
  email.dataset["betaField"] = "email";
  const emailLabel = element(doc, "label", C.settingsLabel, say("beta.email"));
  emailLabel.setAttribute("for", email.id);
  const saveEmail = element(doc, "button", C.button, say("beta.save")) as HTMLButtonElement;
  saveEmail.type = "button";
  saveEmail.dataset["beta"] = "saveEmail";
  const emailField = element(doc, "div", C.settingsField);
  emailField.append(emailLabel, email, saveEmail);

  const testHeading = element(doc, "p", C.settingsLabel, say("test.heading"));
  const testIntro = element(doc, "p", C.muted, say("test.intro"));
  const run = element(doc, "button", C.button, say("test.run")) as HTMLButtonElement;
  run.type = "button";
  run.dataset["beta"] = "runTest";
  const stop = element(doc, "button", C.button, say("test.stop")) as HTMLButtonElement;
  stop.type = "button";
  stop.dataset["beta"] = "stopTest";
  const send = element(doc, "button", C.button, say("test.send")) as HTMLButtonElement;
  send.type = "button";
  send.dataset["beta"] = "sendTest";
  const testActions = element(doc, "div", C.settingsSupportActions);
  testActions.append(run, stop, send);
  const blocked = element(doc, "p", C.muted);
  blocked.dataset["beta"] = "blocked";
  const steps = element(doc, "ol");
  steps.dataset["beta"] = "steps";
  const result = element(doc, "p");
  result.dataset["beta"] = "result";
  result.setAttribute("role", "status");
  const testBox = element(doc, "div");
  testBox.dataset["beta"] = "test";
  testBox.append(testHeading, testIntro, testActions, blocked, steps, result);

  const error = element(doc, "p", C.muted);
  error.dataset["beta"] = "error";
  error.setAttribute("role", "alert");

  root.append(checkRow, more, status, emailField, testBox, error);

  let answer: BetaAnswer | null = null;
  let busy = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let emailTouched = false;
  email.addEventListener("input", () => {
    emailTouched = true;
  });

  function paint(): void {
    const beta = answer?.beta ?? null;
    const test = answer?.test ?? null;
    check.disabled = busy || beta === null;
    check.checked = beta?.on === true;
    status.textContent = beta === null
      ? ""
      : beta.on && beta.endsAt !== null
        ? say("beta.until", { date: dateText(language, beta.endsAt, false) })
        : beta.ended
          ? say("beta.ended")
          : "";
    status.hidden = status.textContent === "";
    if (!emailTouched) {
      email.value = beta?.email ?? "";
    }
    email.disabled = busy || beta === null;
    saveEmail.disabled = busy || beta === null;
    // A site with nothing to test and no earlier test shows no box: there is nothing to press and nothing to read.
    testBox.hidden = test === null || (test.blocked === "nothing_to_test" && test.state === "idle");
    if (testBox.hidden || test === null) {
      return;
    }
    const running = test.state === "running";
    run.hidden = running;
    run.disabled = busy || test.blocked !== null;
    stop.hidden = !running;
    stop.disabled = busy;
    send.hidden = test.state !== "done" || beta?.on === true || test.sentRef !== null;
    send.disabled = busy;
    const why = blockedText(language, test);
    blocked.textContent = why ?? "";
    blocked.hidden = why === null;
    steps.replaceChildren(
      ...test.steps.map((step) => {
        const item = element(doc, "li", undefined, stepText(language, step));
        item.dataset["status"] = step.status;
        if (step.reason !== null) {
          item.dataset["reason"] = step.reason;
        }
        return item;
      }),
    );
    steps.hidden = test.steps.length === 0;
    const lines: string[] = [];
    if (running) {
      lines.push(say("test.running"));
    } else if (test.result !== null) {
      lines.push(say(RESULT_LABELS[test.result]));
    }
    if (test.sentRef !== null) {
      lines.push(say("test.sent", { ref: test.sentRef }));
    }
    result.textContent = lines.join(" ");
    result.hidden = lines.length === 0;
  }

  function schedule(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    if (answer?.test?.state === "running") {
      timer = setTimeout(() => {
        timer = null;
        if (root.isConnected) {
          void request({ action: "get" });
        }
      }, TEST_POLL_MS);
    }
  }

  async function request(body: BetaRequest, failure: TranslationKey = "beta.error.failed"): Promise<BetaAnswer | null> {
    busy = body.action !== "get";
    paint();
    let decoded: BetaAnswer | null = null;
    try {
      decoded = decodeBetaAnswer(await input.call(body));
    } catch {
      decoded = null;
    }
    busy = false;
    if (decoded !== null && decoded.beta !== null) {
      answer = decoded;
      input.onAnswer?.(decoded);
    }
    if (decoded === null) {
      error.textContent = say(failure);
    } else if (!decoded.ok && body.action !== "get") {
      error.textContent = errorText(body, decoded);
    } else {
      error.textContent = "";
    }
    error.hidden = error.textContent === "";
    paint();
    schedule();
    return decoded;
  }

  function errorText(body: BetaRequest, decoded: BetaAnswer): string {
    if (body.action === "send_test") {
      return sendFailureText(language, decoded.error, decoded.retryAfterS);
    }
    if (decoded.error === "spotnav_report_invalid_email") {
      return say("sendDebug.error.email");
    }
    if (body.action === "start_test") {
      return say("test.error");
    }
    return say("beta.error.failed");
  }

  check.addEventListener("change", () => {
    void request({ action: "set", changes: { on: check.checked } });
  });
  saveEmail.addEventListener("click", () => {
    const address = email.value.trim();
    if (address !== "" && (address.length > MAX_EMAIL_CHARS || !EMAIL.test(address))) {
      error.textContent = say("sendDebug.error.email");
      error.hidden = false;
      return;
    }
    void request({ action: "set", changes: { email: address === "" ? null : address } }).then((decoded) => {
      if (decoded?.ok === true) {
        emailTouched = false;
        paint();
      }
    });
  });
  run.addEventListener("click", () => {
    void request({ action: "start_test" });
  });
  stop.addEventListener("click", () => {
    void request({ action: "stop_test" });
  });
  send.addEventListener("click", () => {
    void request({ action: "send_test" });
  });

  paint();
  error.hidden = true;
  void request({ action: "get" });
  return root;
}
