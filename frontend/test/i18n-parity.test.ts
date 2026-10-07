// The locales, as a set: exact key parity, and the compact row's labels actually localized.
//
// The types already enforce parity at compile time (`Translation` is `Record<TranslationKey, string>`),
// and this is the runtime half: it fails loudly if a shipped locale is edited outside the type system,
// and it proves the compact fiscal row's own labels exist in every language rather than only in English.

import { readdirSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { LANGUAGES, TRANSLATIONS, translate, type TranslationKey } from "../src/i18n";

/** The keys the compact row adds or renames. */
const ROW_KEYS: TranslationKey[] = [
  "market.enabled.aria",
  "market.value.aria",
  "market.suggestion.label",
  "market.suggestion.none",
  "market.resetToSuggestion",
];

describe("the shipped locales", () => {
  it("are every JSON file in src/i18n, so a new language file cannot be left unwired", () => {
    const folder = resolve(__dirname, "../src/i18n");
    const files = readdirSync(folder)
      .filter((name) => name.endsWith(".json"))
      .map((name) => name.slice(0, -".json".length));
    expect([...files].sort()).toEqual([...LANGUAGES].sort());
  });

  it("hold exactly the same keys, in every language", () => {
    const english = Object.keys(TRANSLATIONS.en).sort();
    expect(english.length).toBeGreaterThan(0);
    for (const language of LANGUAGES) {
      expect(Object.keys(TRANSLATIONS[language]).sort()).toEqual(english);
    }
  });

  it("say the fiscal row's labels in words, not in placeholders", () => {
    for (const language of LANGUAGES) {
      for (const key of ROW_KEYS) {
        const text = translate(language, key, { component: "VAT", value: "25", unit: "%" });
        expect(text.length).toBeGreaterThan(0);
        // A placeholder that nothing substitutes would be a bug the types cannot see.
        expect(text).not.toContain("{component}");
        expect(text).not.toContain("{value}");
        expect(text).not.toContain("{unit}");
      }
    }
    // And they are translations rather than the English text copied into every file.
    const reset = LANGUAGES.map((language) => translate(language, "market.resetToSuggestion"));
    expect(new Set(reset).size).toBeGreaterThanOrEqual(4);
  });
});
