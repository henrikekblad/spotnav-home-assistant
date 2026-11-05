// The card notices when SpotNav has been updated under it.
//
// A browser or the Companion app may keep the old card module after an update. That card decodes strictly,
// so the new integration's answers read as `malformed`, and its language file (named by the old hash) is
// gone, so it would speak English. When either happens the card asks which bundle is served
// (`card-identity.ts`):
//
// - Another bundle: a line says SpotNav has been updated, with a Reload button. The page reloads only when
//   the reader presses it; the card never reloads the page by itself.
// - The same bundle: the answer is taken as transient (Home Assistant restarting) and the dashboard read
//   is retried a couple of times before `state.malformed` shows. That retry reads the dashboard again; it
//   does not reload the page.
//
// The line and the button carry their words in every language here, in the bundle, not in the language
// files: they must read right exactly when the reader's file cannot be had.

import type { Language } from "./i18n";
import { VISUAL_CLASSES } from "./visual-styles";

/** Waits before the dashboard read is retried after an unreadable answer from the same card version. */
export const MALFORMED_RETRY_DELAYS_MS: readonly number[] = [3_000, 10_000];

/** How long the card waits for `spotnav/get_card_info` before it gives up on knowing. */
export const CARD_INFO_TIMEOUT_MS = 5_000;

interface UpdateWords {
  updated: string;
  reload: string;
}

export const UPDATE_WORDS: Record<Language, UpdateWords> = {
  en: { updated: "SpotNav has been updated.", reload: "Reload" },
  sv: { updated: "SpotNav har uppdaterats.", reload: "Ladda om" },
  nb: { updated: "SpotNav er oppdatert.", reload: "Last inn på nytt" },
  da: { updated: "SpotNav er blevet opdateret.", reload: "Genindlæs" },
  fi: { updated: "SpotNav on päivitetty.", reload: "Lataa uudelleen" },
  de: { updated: "SpotNav wurde aktualisiert.", reload: "Neu laden" },
  nl: { updated: "SpotNav is bijgewerkt.", reload: "Opnieuw laden" },
  fr: { updated: "SpotNav a été mis à jour.", reload: "Recharger" },
  es: { updated: "SpotNav se ha actualizado.", reload: "Recargar" },
};

/** True for a decoder's verdict that the answer could not be read (not merely another API version). */
export function isMalformed(result: unknown): boolean {
  return (
    typeof result === "object" &&
    result !== null &&
    (result as { ok?: unknown }).ok === false &&
    (result as { failure?: unknown }).failure === "malformed"
  );
}

let reloader: () => void = () => {
  globalThis.location?.reload();
};

/** Reload the page, to load the card the integration serves now. */
export function reloadPage(): void {
  reloader();
}

/** Tests only: what a reload does instead (`null` puts the real one back). */
export function setReloadForTest(reload: (() => void) | null): void {
  reloader =
    reload ??
    (() => {
      globalThis.location?.reload();
    });
}

/** The line, with the Reload button that reloads the page when the reader presses it, in the reader's language. */
export function updateNoticeElement(
  doc: Document,
  language: Language,
): HTMLElement {
  const words = UPDATE_WORDS[language];
  const box = doc.createElement("div");
  box.className = VISUAL_CLASSES.suggestion;
  box.dataset["cardUpdated"] = "button";
  box.setAttribute("role", "status");
  const line = doc.createElement("p");
  line.className = VISUAL_CLASSES.suggestionText;
  line.textContent = words.updated;
  box.append(line);
  const answers = doc.createElement("div");
  answers.className = VISUAL_CLASSES.suggestionAnswers;
  const button = doc.createElement("button");
  button.type = "button";
  button.className = VISUAL_CLASSES.button;
  button.textContent = words.reload;
  button.addEventListener("click", () => {
    reloadPage();
  });
  answers.append(button);
  box.append(answers);
  return box;
}
