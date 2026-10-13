// Every language, compiled in: for the tests and tools, never imported by the card's runtime code
// (the bundle carries English only and fetches the others, `index.ts`).
//
// Typed against English here, so a key missing from any language is a compile error.

import da from "./da.json";
import de from "./de.json";
import en from "./en.json";
import es from "./es.json";
import fi from "./fi.json";
import fr from "./fr.json";
import { LANGUAGES, registerTranslation, type Language, type Translation } from "./index";
import nb from "./nb.json";
import nl from "./nl.json";
import sv from "./sv.json";

export const TRANSLATIONS: Record<Language, Translation> = { en, sv, nb, da, fi, de, nl, fr, es };

/** Make every language available at once, as if each file had been loaded. */
export function registerAllLanguages(): void {
  for (const language of LANGUAGES) {
    registerTranslation(language, TRANSLATIONS[language]);
  }
}
