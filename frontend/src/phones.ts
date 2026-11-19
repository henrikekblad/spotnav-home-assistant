// The app's connection to Home Assistant, as an administrator manages it from a card (SpotNav 3.0):
//
// * "Disconnect all phones" in Support: one explanatory line and a confirming button; the backend
//   (`spotnav/disconnect_phones`) gives every charger and home a new webhook id, so every paired phone must pair again.
// * "Make a home of this" in the Settings of a charger whose installation has no home yet: what it does in one line,
//   the main fuse (the one thing SpotNav cannot know) and a confirming button; the backend (`spotnav/make_home`) makes
//   the home around the charger. Never without the person's press.
//
// Both answers are read strictly; anything else is the generic failure sentence.

import { disconnectPhones, makeHome, SpotnavApiError } from "./api";
import { formatNumber } from "./format";
import { translate, type Language, type TranslationKey } from "./i18n";
import type { HomeAssistantLike } from "./types";
import { MAKE_HOME_API_VERSION, PHONES_API_VERSION } from "./types";
import { shell, type EditorHandlers } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** A home's main fuse, as the backend accepts it: from 6 A (below any household's fuse: a typo) to 1000 A. */
export const MAIN_FUSE_MIN_A = 6;
export const MAIN_FUSE_MAX_A = 1000;

export type DisconnectAnswer =
  | { ok: true; chargers: number; sites: number; notificationsNotCleared: number }
  | { ok: false; code: string | null };
export type MakeHomeAnswer = { ok: true; siteId: string } | { ok: false; code: string | null };

function record(raw: unknown): Record<string, unknown> | null {
  return typeof raw === "object" && raw !== null && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** `{api_version: 1, ok, error, chargers, sites, notifications_not_cleared?}`, or `null` for anything else. */
export function decodeDisconnectAnswer(raw: unknown): DisconnectAnswer | null {
  const answer = record(raw);
  if (answer === null || answer.api_version !== PHONES_API_VERSION || typeof answer.ok !== "boolean") {
    return null;
  }
  if (!answer.ok) {
    return { ok: false, code: typeof answer.error === "string" ? answer.error : null };
  }
  const chargers = count(answer.chargers);
  const sites = count(answer.sites);
  const left = answer.notifications_not_cleared === undefined ? 0 : count(answer.notifications_not_cleared);
  return chargers === null || sites === null || left === null
    ? null
    : { ok: true, chargers, sites, notificationsNotCleared: left };
}

/** `{api_version: 1, ok, error, site_id}`, or `null` for anything else. */
export function decodeMakeHomeAnswer(raw: unknown): MakeHomeAnswer | null {
  const answer = record(raw);
  if (answer === null || answer.api_version !== MAKE_HOME_API_VERSION || typeof answer.ok !== "boolean") {
    return null;
  }
  if (!answer.ok) {
    return { ok: false, code: typeof answer.error === "string" ? answer.error : null };
  }
  return typeof answer.site_id === "string" && answer.site_id !== "" ? { ok: true, siteId: answer.site_id } : null;
}

/** One press of "Disconnect all phones": `null` when done, else the sentence to show. */
export async function runDisconnectPhones(hass: HomeAssistantLike, language: Language): Promise<string | null> {
  let decoded: DisconnectAnswer | null;
  try {
    decoded = decodeDisconnectAnswer(await disconnectPhones(hass));
  } catch (error) {
    decoded = { ok: false, code: error instanceof SpotnavApiError ? error.code : null };
  }
  if (decoded !== null && decoded.ok) {
    // Every id is replaced; say so truthfully when some notifications are still on (a second press clears them).
    return decoded.notificationsNotCleared === 0
      ? null
      : translate(language, "phones.disconnect.notificationsLeft", { count: String(decoded.notificationsNotCleared) });
  }
  const key: TranslationKey =
    decoded !== null && decoded.code === "spotnav_not_admin" ? "phones.error.notAdmin" : "phones.error.generic";
  return translate(language, key);
}

/** One press of "Make a home": `null` when made, else the sentence to show. */
export async function runMakeHome(
  hass: HomeAssistantLike,
  language: Language,
  chargerId: string,
  mainFuseA: number,
): Promise<string | null> {
  let decoded: MakeHomeAnswer | null;
  try {
    decoded = decodeMakeHomeAnswer(await makeHome(hass, chargerId, mainFuseA));
  } catch (error) {
    decoded = { ok: false, code: error instanceof SpotnavApiError ? error.code : null };
  }
  if (decoded !== null && decoded.ok) {
    return null;
  }
  const code = decoded === null ? null : decoded.code;
  const key: TranslationKey =
    code === "spotnav_home_exists"
      ? "makeHome.error.exists"
      : code === "spotnav_invalid_value"
        ? "makeHome.error.fuse"
        : code === "spotnav_not_admin"
          ? "phones.error.notAdmin"
          : "phones.error.generic";
  return translate(language, key);
}

function line(doc: Document, className: string, text: string): HTMLElement {
  const node = doc.createElement("p");
  node.className = className;
  node.textContent = text;
  return node;
}

/** "Disconnect all phones": the one line that says what it does, and the confirming button beside Cancel. */
export function disconnectPhonesEditor(
  doc: Document,
  language: Language,
  run: () => Promise<string | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "disconnect-phones", handlers, translate(language, "phones.disconnect.confirm"));
  editor.body.append(line(doc, C.entityHelp, translate(language, "phones.disconnect.text")));
  editor.submit(run);
  return editor.form;
}

/**
 * "Make a home of this": what it does, the main fuse in amperes, and the confirming button beside Cancel. The fuse is
 * asked, never guessed; `make(fuse)` answers `null` when the home is made, else the sentence to show.
 */
export function makeHomeEditor(
  doc: Document,
  language: Language,
  idPrefix: string,
  make: (mainFuseA: number) => Promise<string | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "make-home", handlers, translate(language, "makeHome.confirm"));
  editor.body.append(line(doc, C.entityHelp, translate(language, "makeHome.text")));
  const label = doc.createElement("label");
  label.htmlFor = `${idPrefix}-make-home-fuse`;
  label.textContent = translate(language, "makeHome.fuse");
  const field = doc.createElement("input");
  field.type = "text";
  field.inputMode = "numeric";
  field.className = C.settingsInput;
  field.id = `${idPrefix}-make-home-fuse`;
  field.dataset["valueField"] = "main_fuse_a";
  const row = doc.createElement("div");
  row.className = C.settingsRow;
  const unit = doc.createElement("span");
  unit.className = C.settingsUnit;
  unit.textContent = "A";
  row.append(field, unit);
  editor.body.append(label, row);
  editor.submit(async () => {
    const text = field.value.trim().replace(/\s/g, "").replace(",", ".");
    const fuse = text === "" ? Number.NaN : Number(text);
    if (!Number.isFinite(fuse) || fuse < MAIN_FUSE_MIN_A || fuse > MAIN_FUSE_MAX_A) {
      return translate(language, "makeHome.error.range", {
        min: formatNumber(language, MAIN_FUSE_MIN_A, 0),
        max: formatNumber(language, MAIN_FUSE_MAX_A, 0),
      });
    }
    return await make(fuse);
  });
  return editor.form;
}
