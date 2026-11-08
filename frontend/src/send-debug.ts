// "Send debug info": the dialog an administrator sends the debug bundle to SpotNav's developer from, and the
// backend's answer read strictly. The dialog says what is sent and how long it is kept, asks what happened (required)
// and an e-mail address (optional), and sends only with its consent box ticked. On success it shows the
// reference (`SN-XXXX`) to quote; on a failure it says why and points to "Download debug info" instead.
//
// The card never builds or reads the bundle here: `spotnav/send_debug_info` builds the same redacted bundle the
// download saves, and sends it itself.

import { translate, type Language, type TranslationKey } from "./i18n";
import { DEBUG_API_VERSION } from "./types";
import { shell, type EditorHandlers } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** The privacy policy, which says what a sent report holds and how long it is kept. */
export const PRIVACY_URL = "https://spotnav.sensnology.se/privacy";
/** The relay's limits, kept here too so the fields stop where the server would refuse. */
export const MAX_MESSAGE_CHARS = 4000;
export const MAX_EMAIL_CHARS = 254;

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export type SendAnswer =
  | { ok: true; ref: string }
  | { ok: false; code: string | null; retryAfterS: number | null };

/** `{api_version, ok, error, ref, retry_after_s}` or nothing: a malformed or newer answer is `null`. */
export function decodeSendAnswer(raw: unknown): SendAnswer | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const record = raw as Record<string, unknown>;
  if (record.api_version !== DEBUG_API_VERSION || typeof record.ok !== "boolean") {
    return null;
  }
  if (record.ok) {
    return typeof record.ref === "string" && record.ref !== "" ? { ok: true, ref: record.ref } : null;
  }
  const wait = record.retry_after_s;
  return {
    ok: false,
    code: typeof record.error === "string" ? record.error : null,
    retryAfterS: typeof wait === "number" && Number.isFinite(wait) && wait >= 0 ? wait : null,
  };
}

/** A wait as the reader says it: whole minutes up to two hours, whole hours beyond, always rounded up. */
export function waitText(language: Language, seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  if (minutes < 120) {
    return translate(language, "sendDebug.wait.minutes", { count: String(minutes) });
  }
  return translate(language, "sendDebug.wait.hours", { count: String(Math.ceil(minutes / 60)) });
}

/** Why a send failed, in one sentence, followed by the suggestion to download the file instead. */
export function sendFailureText(language: Language, code: string | null, retryAfterS: number | null = null): string {
  let reason: string;
  switch (code) {
    case "spotnav_report_rate_limited":
      reason =
        retryAfterS === null
          ? translate(language, "sendDebug.error.rateLimitedLater")
          : translate(language, "sendDebug.error.rateLimited", { wait: waitText(language, retryAfterS) });
      break;
    case "spotnav_report_invalid_message":
      return translate(language, "sendDebug.error.message");
    case "spotnav_report_invalid_email":
      return translate(language, "sendDebug.error.email");
    default:
      reason = translate(language, FAILURE_KEYS[code ?? ""] ?? "sendDebug.error.failed");
  }
  return `${reason} ${translate(language, "sendDebug.error.download")}`;
}

const FAILURE_KEYS: Record<string, TranslationKey> = {
  spotnav_report_too_large: "sendDebug.error.tooLarge",
  spotnav_report_rejected: "sendDebug.error.rejected",
  spotnav_report_network: "sendDebug.error.network",
  spotnav_report_server: "sendDebug.error.server",
  unauthorized: "sendDebug.error.notAdmin",
};

/** What one send came to: the reference, or the sentence to show. */
export type SendOutcome = { ref: string } | { error: string };

export type SendDebug = (message: string, email: string | null) => Promise<SendOutcome>;

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

/**
 * The dialog's form: what is sent (with the privacy policy), the message, the e-mail address, the consent box and
 * Send beside Cancel. After a send it shows the reference and one Close.
 */
export function sendDebugEditor(
  doc: Document,
  language: Language,
  idPrefix: string,
  send: SendDebug,
  handlers: EditorHandlers,
): HTMLFormElement {
  const say = (key: TranslationKey, values?: Record<string, string>): string => translate(language, key, values);
  let sentRef: string | null = null;
  const editor = shell(
    doc,
    language,
    "sendDebug",
    { onDone: () => showSent(), onCancel: handlers.onCancel },
    say("sendDebug.send"),
  );
  const form = editor.form;
  form.dataset["sendDebug"] = "form";

  const intro = element(doc, "p", C.entityHelp);
  intro.dataset["sendDebugIntro"] = "";
  const privacy = doc.createElement("a");
  privacy.href = PRIVACY_URL;
  privacy.target = "_blank";
  privacy.rel = "noopener noreferrer";
  privacy.textContent = say("sendDebug.privacy");
  intro.append(say("sendDebug.intro"), " ", privacy);

  const message = doc.createElement("textarea");
  message.id = `${idPrefix}-send-debug-message`;
  message.className = `${C.settingsInput} ${C.settingsTextarea}`;
  message.rows = 5;
  message.maxLength = MAX_MESSAGE_CHARS;
  message.required = true;
  message.dataset["sendDebugField"] = "message";
  const messageLabel = element(doc, "label", C.settingsLabel, say("sendDebug.message"));
  messageLabel.setAttribute("for", message.id);
  const messageField = element(doc, "div", C.settingsField);
  messageField.append(messageLabel, message);

  const email = doc.createElement("input");
  email.type = "email";
  email.id = `${idPrefix}-send-debug-email`;
  email.className = C.settingsInput;
  email.maxLength = MAX_EMAIL_CHARS;
  email.autocomplete = "email";
  email.dataset["sendDebugField"] = "email";
  const emailLabel = element(doc, "label", C.settingsLabel, say("sendDebug.email"));
  emailLabel.setAttribute("for", email.id);
  const emailField = element(doc, "div", C.settingsField);
  emailField.append(emailLabel, email);

  const consent = doc.createElement("input");
  consent.type = "checkbox";
  consent.id = `${idPrefix}-send-debug-consent`;
  consent.required = true;
  consent.dataset["sendDebugField"] = "consent";
  const consentLabel = element(doc, "label", C.settingsLabel, say("sendDebug.consent"));
  consentLabel.setAttribute("for", consent.id);
  const consentRow = element(doc, "div", C.settingsCheckRow);
  consentRow.append(consent, consentLabel);

  editor.body.append(intro, messageField, emailField, consentRow);

  editor.submit(async () => {
    const text = message.value.trim();
    if (text === "") {
      return say("sendDebug.error.message");
    }
    const address = email.value.trim();
    if (address !== "" && (address.length > MAX_EMAIL_CHARS || !EMAIL.test(address))) {
      return say("sendDebug.error.email");
    }
    if (!consent.checked) {
      return say("sendDebug.error.consent");
    }
    const outcome = await send(text, address === "" ? null : address);
    if ("ref" in outcome) {
      sentRef = outcome.ref;
      return null;
    }
    return outcome.error;
  });
  function showSent(): void {
    const done = element(doc, "p", undefined, say("sendDebug.sent", { ref: sentRef ?? "" }));
    done.dataset["sendDebugSent"] = sentRef ?? "";
    done.setAttribute("role", "status");
    const actions = element(doc, "div", C.settingsActions);
    const close = element(doc, "button", `${C.button} ${C.settingsSave}`, say("dialog.close")) as HTMLButtonElement;
    close.type = "button";
    close.dataset["sendDebugClose"] = "";
    close.addEventListener("click", () => handlers.onDone());
    actions.append(close);
    form.replaceChildren(done, actions);
    close.focus();
  }

  return form;
}
