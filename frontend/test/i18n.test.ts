// Localization foundations: key completeness, placeholders, language resolution and plurals.

import { describe, expect, it } from "vitest";

import { LANGUAGES, TRANSLATIONS, placeholders, pluralForm, periodHeadingKey, resolveLanguage, translate } from "../src/i18n";
import en from "../src/i18n/en.json";

const keys = Object.keys(en) as Array<keyof typeof en>;

describe("the five languages", () => {
  it("carry exactly the English key set, none empty", () => {
    for (const language of LANGUAGES) {
      const translation = TRANSLATIONS[language];
      expect(Object.keys(translation).sort(), language).toEqual([...keys].sort());
      for (const key of keys) {
        expect(translation[key].trim(), `${language}/${key}`).not.toBe("");
      }
    }
  });

  it("use the same placeholders as English for every key", () => {
    for (const language of LANGUAGES) {
      for (const key of keys) {
        expect(new Set(placeholders(TRANSLATIONS[language][key])), `${language}/${key}`).toEqual(
          new Set(placeholders(en[key])),
        );
      }
    }
  });

  it("never interpolates a backend reason code into a primary sentence", () => {
    for (const language of LANGUAGES) {
      expect(placeholders(TRANSLATIONS[language]["issue.planningUnavailable"])).toEqual([]);
      expect(TRANSLATIONS[language]["issue.planningUnavailable"]).not.toContain("{reason}");
      // And the actionable one is a sentence with no backend code in it either.
      expect(placeholders(TRANSLATIONS[language]["issue.priceHorizon"])).toEqual([]);
      expect(TRANSLATIONS[language]["issue.priceHorizon"]).not.toContain("insufficient_price_horizon");
    }
  });
});

describe("language resolution", () => {
  it.each([
    ["sv-SE", "sv"],
    ["sv", "sv"],
    ["da-DK", "da"],
    ["fi-FI", "fi"],
    ["nb-NO", "nb"],
    ["no", "nb"],
    ["nn", "nb"],
    ["en-GB", "en"],
    ["de-DE", "de"],
    ["nl-BE", "nl"],
    ["it", "en"],
    ["", "en"],
    [null, "en"],
    [undefined, "en"],
  ])("maps %p to %p", (value, expected) => {
    expect(resolveLanguage(value as string | null)).toBe(expected);
  });
});

describe("plural headings", () => {
  it("uses Intl.PluralRules, so Swedish and Finnish singular is not arithmetic", () => {
    expect(pluralForm("sv", 1)).toBe("one");
    expect(pluralForm("sv", 2)).toBe("other");
    expect(pluralForm("sv", 0)).toBe("other");
    expect(pluralForm("fi", 1)).toBe("one");
    expect(pluralForm("en", 1)).toBe("one");
    expect(periodHeadingKey("plan.proposal", "sv", 1)).toBe("plan.proposal.one");
    expect(periodHeadingKey("plan.proposal", "sv", 2)).toBe("plan.proposal.other");
    expect(translate("sv", "plan.proposal.one", { count: "1" })).toBe("Billigaste laddperioden (1)");
    expect(translate("sv", "plan.proposal.other", { count: "2" })).toBe("Billigaste laddperioderna (2)");
  });
});

describe("translate", () => {
  it("fills placeholders and leaves unknown ones visible rather than blanking them", () => {
    expect(translate("en", "status.externalInstalled", { time: "21:00" })).toContain("21:00");
    expect(translate("en", "plan.period", { day: "Mon", from: "21:00" })).toContain("{to}");
  });
});
