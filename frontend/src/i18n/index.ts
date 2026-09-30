// The five languages, the fallback rule, plural selection and placeholder substitution.
//
// Everything here is pure: `hass.language` is read elsewhere, as a state-snapshot field, and only
// its *value* reaches this module. Numbers, weekdays and plural categories come from `Intl`; nothing
// is hand-formatted, and nothing is selected by `count === 1` alone, because a plural category is a
// language's own rule and not arithmetic.

import { da } from "./da";
import { en } from "./en";
import { fi } from "./fi";
import { nb } from "./nb";
import { sv } from "./sv";

export type TranslationKey = keyof typeof en;
export type Translation = Record<TranslationKey, string>;

export const LANGUAGES = ["en", "sv", "nb", "da", "fi"] as const;
export type Language = (typeof LANGUAGES)[number];

export const TRANSLATIONS: Record<Language, Translation> = { en, sv, nb, da, fi };

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
  const template = TRANSLATIONS[language][key] ?? TRANSLATIONS.en[key];
  return template.replace(/\{(\w+)\}/g, (match, name: string) => params[name] ?? match);
}
