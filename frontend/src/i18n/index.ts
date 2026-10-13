// The languages, the fallback rule, the loading of a language, plural selection and placeholder
// substitution.
//
// The words are in `<lang>.json`, one file per language. English is the source of truth for the key
// set and the only language compiled into the bundle; every other language is fetched once, from the
// file the build put beside the bundle (`custom_components/spotnav/www/i18n/<lang>.<hash>.json`,
// served at `/spotnav/i18n/`), when a reader first needs it. Until it is in, `translate()` gives
// English; the card shows its loading state meanwhile (`card.ts`). The other languages are typed
// against English in `all.ts`, which the tests and tools import, so a missing key is a compile error,
// and the i18n tests reject an extra one (a JSON file is not an object literal, so the type cannot).
//
// `hass.language` is read elsewhere, as a state-snapshot field, and only its *value* reaches this
// module. Numbers, weekdays and plural categories come from `Intl`; nothing is hand-formatted, and
// nothing is selected by `count === 1` alone, because a plural category is a language's own rule and
// not arithmetic.

import { LANGUAGE_FILES } from "../generated/languages";
import en from "./en.json";

export type TranslationKey = keyof typeof en;
export type Translation = Record<TranslationKey, string>;

export const LANGUAGES = ["en", "sv", "nb", "da", "fi", "de", "nl", "fr", "es"] as const;
export type Language = (typeof LANGUAGES)[number];

/** Where Home Assistant serves the language files (`card_asset.py`). */
export const LANGUAGE_URL_PATH = "/spotnav/i18n/";

/** The languages this page has: English always, the others once loaded or registered. */
const loaded = new Map<Language, Partial<Translation>>([["en", en]]);
/** Requests under way, so many cards on a page ask once. */
const pending = new Map<Language, Promise<boolean>>();
/** Languages whose file could not be had on this page load: English until the next load. */
const failed = new Set<Language>();

/** The words of one language, for this page (the tests and tools register every language at once). */
export function registerTranslation(language: Language, words: Partial<Translation>): void {
  loaded.set(language, words);
  failed.delete(language);
}

/** Back to English only, as a page starts. For tests. */
export function forgetLanguages(): void {
  loaded.clear();
  loaded.set("en", en);
  pending.clear();
  failed.clear();
}

/** True when `translate()` already gives this language's own words. */
export function isLanguageLoaded(language: Language): boolean {
  return loaded.has(language);
}

/**
 * True while the card should wait for a language: it is neither in nor given up on. A language with
 * no file (none was built for it) is never waited for.
 */
export function isLanguagePending(language: Language): boolean {
  return !loaded.has(language) && !failed.has(language) && LANGUAGE_FILES[language] !== undefined;
}

function isWords(value: unknown): value is Partial<Translation> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((word) => typeof word === "string")
  );
}

/**
 * Load one language's file, once per page. Resolves `true` when its words are in, `false` when the
 * card stays in English: no file for it, or the request failed. A failure is not retried on this
 * page (a `hass` update arrives many times a minute); the next page load tries again.
 */
export function loadLanguage(
  language: Language,
  fetcher: typeof fetch = (input, init) => globalThis.fetch(input, init),
): Promise<boolean> {
  if (loaded.has(language)) {
    return Promise.resolve(true);
  }
  const file = LANGUAGE_FILES[language];
  if (failed.has(language) || file === undefined) {
    return Promise.resolve(false);
  }
  const existing = pending.get(language);
  if (existing !== undefined) {
    return existing;
  }
  const request = (async (): Promise<boolean> => {
    try {
      const response = await fetcher(`${LANGUAGE_URL_PATH}${file}`, { credentials: "same-origin" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const words: unknown = await response.json();
      if (!isWords(words)) {
        throw new Error("not a language file");
      }
      loaded.set(language, words);
      return true;
    } catch {
      failed.add(language);
      return false;
    } finally {
      pending.delete(language);
    }
  })();
  pending.set(language, request);
  return request;
}

/** Plural categories the headings use. Every supported language distinguishes one from other. */
export type PluralCategory = "one" | "other";

const pluralRules = new Map<Language, Intl.PluralRules>();

function rulesFor(language: Language): Intl.PluralRules {
  const existing = pluralRules.get(language);
  if (existing !== undefined) {
    return existing;
  }
  const created = new Intl.PluralRules(language, { type: "cardinal" });
  pluralRules.set(language, created);
  return created;
}

/** Which heading form a count takes, by `Intl.PluralRules` rather than by `count === 1`. */
export function pluralForm(language: Language, count: number): PluralCategory {
  return rulesFor(language).select(count) === "one" ? "one" : "other";
}

/** The heading key for a period block, in the reader's own plural rule. */
export function periodHeadingKey(
  base: "plan.proposal" | "plan.installed",
  language: Language,
  count: number,
): TranslationKey {
  return `${base}.${pluralForm(language, count)}` as TranslationKey;
}

/**
 * The language to use for a `hass.language` value.
 *
 * Region forms resolve to their base language (`sv-SE`, `da-DK`, `fi-FI`, `nb-NO`), Home Assistant's
 * `no` (and `nn`) resolve to Norwegian Bokmål rather than English, and anything unsupported
 * falls back to English.
 */
export function resolveLanguage(language: string | null | undefined): Language {
  if (typeof language !== "string" || language === "") {
    return "en";
  }
  const base = (language.toLowerCase().split(/[-_]/)[0] ?? "").trim();
  if (base === "no" || base === "nn" || base === "nb") {
    return "nb";
  }
  return (LANGUAGES as readonly string[]).includes(base) ? (base as Language) : "en";
}

/** The `{name}` placeholders a template declares, in order of first appearance. */
export function placeholders(template: string): string[] {
  const found: string[] = [];
  for (const match of template.matchAll(/\{(\w+)\}/g)) {
    const name = match[1] as string;
    if (!found.includes(name)) {
      found.push(name);
    }
  }
  return found;
}

/** One sentence, with `{name}` placeholders filled from `params`. */
export function translate(
  language: Language,
  key: TranslationKey,
  params: Record<string, string> = {},
): string {
  const template = loaded.get(language)?.[key] ?? en[key];
  return template.replace(/\{(\w+)\}/g, (match, name: string) => params[name] ?? match);
}
