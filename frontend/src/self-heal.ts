// The card heals itself after SpotNav is updated.
//
// A browser or the Companion app may keep the old card module after an update. That card decodes strictly,
// so the new integration's answers read as `malformed`, and its language file (named by the old hash) is
// gone, so it would speak English. When either happens the card asks which bundle is served
// (`card-identity.ts`):
//
// - Another bundle: the page reloads once to load it, after a short line says so. The reload is claimed
//   per served hash in `sessionStorage`, so it can never loop; where storage fails, while a dialog holds
//   unsaved input, or when the reload already happened for that hash, the line offers a Reload button
//   instead.
// - The same bundle: the answer is taken as transient (Home Assistant restarting) and the dashboard read
//   is retried a couple of times before `state.malformed` shows.
//
// The line and the button carry their words in every language here, in the bundle, not in the language
// files: they must read right exactly when the reader's file cannot be had.

import type { Language } from "./i18n";
import { VISUAL_CLASSES } from "./visual-styles";

/** Waits before the dashboard read is retried after an unreadable answer from the same card version. */
export const MALFORMED_RETRY_DELAYS_MS: readonly number[] = [3_000, 10_000];

/** How long the "reloading" line shows before the page reloads. */
export const RELOAD_DELAY_MS = 800;

/** How long the card waits for `spotnav/get_card_info` before it gives up on knowing. */
export const CARD_INFO_TIMEOUT_MS = 5_000;

/** The `sessionStorage` key that records the one reload made for a served bundle hash. */
export const RELOAD_KEY_PREFIX = "spotnav-card-reloaded:";

/** How the line shows: about to reload, or with a button for the reader. */
export type UpdateNotice = "reloading" | "button";

interface UpdateWords {
  reloading: string;
  updated: string;
  reload: string;
}

export const UPDATE_WORDS: Record<Language, UpdateWords> = {
  en: { reloading: "SpotNav has been updated – reloading…", updated: "SpotNav has been updated.", reload: "Reload" },
  sv: { reloading: "SpotNav har uppdaterats – laddar om…", updated: "SpotNav har uppdaterats.", reload: "Ladda om" },
  nb: {
    reloading: "SpotNav er oppdatert – laster inn på nytt…",
    updated: "SpotNav er oppdatert.",
    reload: "Last inn på nytt",
  },
  da: {
    reloading: "SpotNav er blevet opdateret – genindlæser…",
    updated: "SpotNav er blevet opdateret.",
    reload: "Genindlæs",
  },
  fi: {
    reloading: "SpotNav on päivitetty – ladataan uudelleen…",
    updated: "SpotNav on päivitetty.",
    reload: "Lataa uudelleen",
  },
  de: {
    reloading: "SpotNav wurde aktualisiert – wird neu geladen…",
    updated: "SpotNav wurde aktualisiert.",
    reload: "Neu laden",
  },
  nl: { reloading: "SpotNav is bijgewerkt – opnieuw laden…", updated: "SpotNav is bijgewerkt.", reload: "Opnieuw laden" },
  fr: { reloading: "SpotNav a été mis à jour – rechargement…", updated: "SpotNav a été mis à jour.", reload: "Recharger" },
  es: { reloading: "SpotNav se ha actualizado – recargando…", updated: "SpotNav se ha actualizado.", reload: "Recargar" },
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

export interface ReloadStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** This tab's `sessionStorage`, or `null` where the browser will not give it. */
export function sessionStore(): ReloadStore | null {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * Claim the one reload this tab may make for the served bundle `served`: `true` when it is claimed now,
 * `false` when it was made before or storage cannot be trusted to remember it (then nothing reloads by
 * itself, so a page that still runs the old card after its reload cannot loop).
 */
export function claimReload(store: ReloadStore | null, served: string): boolean {
  if (store === null) {
    return false;
  }
  const key = `${RELOAD_KEY_PREFIX}${served}`;
  try {
    if (store.getItem(key) !== null) {
      return false;
    }
    store.setItem(key, "1");
    return store.getItem(key) === "1";
  } catch {
    return false;
  }
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

/** The line, and in `button` form the Reload button, in the reader's language. */
export function updateNoticeElement(doc: Document, language: Language, notice: UpdateNotice): HTMLElement {
  const words = UPDATE_WORDS[language];
  const box = doc.createElement("div");
  box.className = VISUAL_CLASSES.suggestion;
  box.dataset["cardUpdated"] = notice;
  box.setAttribute("role", "status");
  const line = doc.createElement("p");
  line.className = VISUAL_CLASSES.suggestionText;
  line.textContent = notice === "reloading" ? words.reloading : words.updated;
  box.append(line);
  if (notice === "button") {
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
  } else {
    box.setAttribute("aria-busy", "true");
  }
  return box;
}
