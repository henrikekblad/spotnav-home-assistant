// Formatting: minor units, stored energy amounts, market timezone only, and endpoint-aware DST
// ambiguity.

import { describe, expect, it } from "vitest";

import { clock, dateLabel, departureDayLabel, energyAmount, formatNumber, hasZone, money, offsetLabel, periodIsAmbiguous, periodLabel, pricePerKwh, wallTimeRepeats, weekdayDate, weekdayPlural } from "../src/format";
import { LANGUAGES, type Language } from "../src/i18n";

const stockholm = {
  language: "en" as const,
  timeZone: "Europe/Stockholm",
  unit: "öre",
  currency: "SEK",
  majorUnit: "kr",
};

const noZone = { ...stockholm, timeZone: "" };

describe("units and money", () => {
  it("quotes prices in the market's minor unit, never from the ISO currency", () => {
    expect(pricePerKwh(stockholm, 112.75)).toBe("112.8 öre/kWh");
    expect(pricePerKwh(stockholm, null)).toBe("");
  });

  it("keeps zero as a real cost and uses the major unit", () => {
    expect(money(stockholm, 0)).toBe("0 kr");
    expect(money(stockholm, 22.5)).toBe("22.5 kr");
    expect(money({ ...stockholm, majorUnit: null }, 12)).toBe("12");
    expect(money(stockholm, null)).toBe("");
  });

  it("formats numbers through Intl", () => {
    expect(formatNumber("sv", 1234.56)).toBe("1\u00a0234,6");
    expect(formatNumber("en", 1234.56)).toBe("1,234.6");
  });
});

describe("one stored energy amount", () => {
  it("states the amount in the reader's own conventions, with the unit", () => {
    expect(energyAmount("en", 41.6)).toBe("41.6 kWh");
    expect(energyAmount("en", 42.0)).toBe("42 kWh");
    expect(energyAmount("sv", 1234.5)).toBe("1\u00a0234,5 kWh");
  });

  it("keeps three amounts three amounts", () => {
    expect(new Set([41.6, 42.0, 42.1].map((value) => energyAmount("en", value))).size).toBe(3);
  });

  it("states the amount in every language's own conventions", () => {
    // Both marks come from `Intl`, not from a table in this module: `sv`, `nb` and `fi` group with a
    // no-break space, `fr` with a narrow one, `da`, `de` and `nl` with a period, and `es` not at all
    // below five digits; every language but `en` writes a comma as the decimal mark. Asserting them all
    // is what keeps a hand-cut formatter from creeping back in.
    const grouped: Record<Language, string> = {
      da: "1.000 kWh",
      en: "1,000 kWh",
      fi: "1\u00a0000 kWh",
      nb: "1\u00a0000 kWh",
      sv: "1\u00a0000 kWh",
      de: "1.000 kWh",
      nl: "1.000 kWh",
      fr: "1\u202f000 kWh",
      es: "1000 kWh",
    };
    for (const language of LANGUAGES) {
      expect(energyAmount(language, 1000), language).toBe(grouped[language]);
      expect(energyAmount(language, 41.6), language).toBe(language === "en" ? "41.6 kWh" : "41,6 kWh");
    }
  });

  it("does not round away an amount the exact field can store", () => {
    // `Intl` at a tenth states this one as `20.3`, an amount the
    // record does not hold and the dialog would contradict.
    expect(energyAmount("en", 20.25)).toBe("20.25 kWh");
    expect(energyAmount("en", 0.1)).toBe("0.1 kWh");
    expect(energyAmount("en", 1000)).toBe("1,000 kWh");
  });

  it("bounds a float artefact rather than printing it", () => {
    expect(energyAmount("en", 41.60000000000001)).toBe("41.6 kWh");
    expect(energyAmount("en", 0.1 + 0.2)).toBe("0.3 kWh");
    expect(energyAmount("en", 1 / 3)).toBe("0.333 kWh");
    for (const value of [41.60000000000001, 0.1 + 0.2, 1 / 3, 2 / 7]) {
      // No amount is ever spelled with a run of digits only a float can produce.
      expect(energyAmount("en", value), String(value)).not.toMatch(/\d{4,}/);
    }
  });

  it("states nothing for an amount it has not got", () => {
    expect(energyAmount("en", null)).toBe("");
    expect(energyAmount("en", Number.NaN)).toBe("");
    expect(energyAmount("en", Number.POSITIVE_INFINITY)).toBe("");
  });
});

describe("no browser-timezone fallback", () => {
  it("reports that times are unavailable rather than guessing", () => {
    expect(hasZone(stockholm)).toBe(true);
    expect(hasZone(noZone)).toBe(false);
    expect(clock(noZone, Date.parse("2026-09-22T04:00:00Z"))).toBe("");
    expect(weekdayDate(noZone, Date.parse("2026-09-22T04:00:00Z"))).toBe("");
    expect(periodLabel(noZone, Date.parse("2026-09-22T04:00:00Z"), Date.parse("2026-09-22T05:00:00Z"))).toBe("");
  });
});

describe("market-local times", () => {
  it("renders a clock time in the market zone", () => {
    expect(clock(stockholm, Date.parse("2026-09-22T04:00:00Z"))).toBe("06:00");
  });

  it("shows the end day when a period crosses local midnight", () => {
    const label = periodLabel(stockholm, Date.parse("2026-09-22T21:00:00Z"), Date.parse("2026-09-22T23:10:00Z"));
    expect(label).toContain("23:00");
    expect(label).toContain("01:10");
    expect(label).toContain("Wed"); // the end lands on the next local day
  });

  it("adds an offset only to an endpoint whose wall time occurs twice", () => {
    // Autumn: 00:00Z is 02:00 local on the first pass, 01:00Z is 02:00 local on the second.
    expect(wallTimeRepeats(stockholm, Date.parse("2026-10-25T01:00:00Z"))).toBe(true);
    expect(wallTimeRepeats(stockholm, Date.parse("2026-10-25T05:00:00Z"))).toBe(false);
    const ambiguous = periodLabel(stockholm, Date.parse("2026-10-25T01:00:00Z"), Date.parse("2026-10-25T01:30:00Z"));
    expect(ambiguous).toContain("GMT");
    // A period later on the same day -- the day whose offset changed elsewhere -- stays plain.
    const plain = periodLabel(stockholm, Date.parse("2026-10-25T12:00:00Z"), Date.parse("2026-10-25T13:00:00Z"));
    expect(plain).not.toContain("GMT");
    expect(periodIsAmbiguous(stockholm, Date.parse("2026-10-25T12:00:00Z"), Date.parse("2026-10-25T13:00:00Z"))).toBe(false);
    expect(periodIsAmbiguous(stockholm, Date.parse("2026-10-25T01:00:00Z"), Date.parse("2026-10-25T01:30:00Z"))).toBe(true);
  });

  it("does not fabricate the spring hour that does not exist", () => {
    expect(wallTimeRepeats(stockholm, Date.parse("2026-03-29T01:00:00Z"))).toBe(false);
    expect(clock(stockholm, Date.parse("2026-03-29T01:00:00Z"))).toBe("03:00");
  });

  it("labels offsets from the zone, not from arithmetic", () => {
    expect(offsetLabel(stockholm, Date.parse("2026-01-15T12:00:00Z"))).toContain("+1");
    expect(offsetLabel(stockholm, Date.parse("2026-07-15T12:00:00Z"))).toContain("+2");
  });
});


describe("a departure date for people", () => {
  it("is written as a short weekday, day and month in the card's language", () => {
    expect(dateLabel("en", "2026-10-04")).toBe("Sun 4 Oct");
    expect(dateLabel("sv", "2026-10-04")).toBe("sön 4 okt.");
    expect(dateLabel("nb", "2026-10-04")).toContain("4. okt");
    expect(dateLabel("da", "2026-10-04")).toContain("4. okt");
    expect(dateLabel("fi", "2026-10-04")).toContain("4.10.");
    expect(dateLabel("en", "not a date")).toBe("");
  });

  it("never moves a day by the reader's zone: the date is the date", () => {
    expect(dateLabel("en", "2026-01-01")).toBe("Thu 1 Jan");
    expect(dateLabel("en", "2026-12-31")).toBe("Thu 31 Dec");
  });

  const words = { today: "today", tomorrow: "tomorrow" };

  it("says today and tomorrow in words, a later day by its date, and nothing for a day gone by", () => {
    expect(departureDayLabel("en", "2026-10-02", "2026-10-02", words)).toBe("today");
    expect(departureDayLabel("en", "2026-10-03", "2026-10-02", words)).toBe("tomorrow");
    expect(departureDayLabel("en", "2026-10-04", "2026-10-02", words)).toBe("Sun 4 Oct");
    expect(departureDayLabel("en", "2026-10-01", "2026-10-02", words)).toBeNull();
    expect(departureDayLabel("en", "2026-10-02", null, words)).toBe("Fri 2 Oct");
    expect(departureDayLabel("en", "garbage", "2026-10-02", words)).toBeNull();
  });

  it("knows tomorrow across a month and a year end", () => {
    expect(departureDayLabel("en", "2026-11-01", "2026-10-31", words)).toBe("tomorrow");
    expect(departureDayLabel("en", "2027-01-01", "2026-12-31", words)).toBe("tomorrow");
  });
});

describe("a weekday in the plural", () => {
  it.each([
    ["en", 7, "Sundays"],
    ["en", 1, "Mondays"],
    ["sv", 7, "söndagar"],
    ["sv", 3, "onsdagar"],
    ["nb", 7, "søndager"],
    ["da", 7, "søndage"],
    ["fi", 7, "sunnuntaisin"],
    ["fi", 3, "keskiviikkoisin"],
    ["fi", 1, "maanantaisin"],
    ["de", 7, "Sonntage"],
    ["de", 3, "Mittwoche"],
    ["nl", 7, "zondagen"],
    ["es", 7, "domingos"],
    ["es", 1, "lunes"],
    ["fr", 7, "dimanches"],
  ] as const)("%s weekday %s is %s", (language, weekday, expected) => {
    expect(weekdayPlural(language, weekday)).toBe(expected);
  });

  it("is null for a number that is no ISO weekday", () => {
    for (const bad of [0, 8, 1.5, Number.NaN, 1234]) {
      expect(weekdayPlural("en", bad)).toBeNull();
    }
  });
});
