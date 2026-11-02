// The import target's status lines as the card words them: held, paused to the next hour, the house alone over the
// target, and the target set aside for a departure.

import { describe, expect, it } from "vitest";

import { lineText } from "../src/status";
import type { StatusLine } from "../src/validate";

const NOW = Date.parse("2026-11-02T13:20:00Z");
const EN = { language: "en" as const, timeZone: "Europe/Stockholm", unit: "öre", currency: "SEK", majorUnit: "kr" };
const SV = { ...EN, language: "sv" as const };

function line(code: string, params: Record<string, unknown>): StatusLine {
  return { code, params } as unknown as StatusLine;
}

describe("the import target's status lines", () => {
  it("says what the car is held to, the target and the hour so far", () => {
    const held = line("import_target_held", { limit_a: 7, target_kw: 6, hour_kw: 5.1 });
    expect(lineText(held, EN, NOW)).toBe("Held to 7 A to keep this hour under 6.0 kW (now 5.1 kW).");
    expect(lineText(held, SV, NOW)).toBe("Hålls på 7 A så att timmen stannar under 6,0 kW (nu 5,1 kW).");
    const unknown = line("import_target_held", { limit_a: null, target_kw: 6, hour_kw: 5.1 });
    expect(lineText(unknown, EN, NOW)).toBe("Held back to keep this hour under 6.0 kW (now 5.1 kW).");
  });

  it("says until when a pause lasts", () => {
    const paused = line("import_target_paused", { until: "2026-11-02T14:00:00+00:00", target_kw: 6 });
    expect(lineText(paused, EN, NOW)).toBe("Paused until 15:00 to keep this hour under 6.0 kW.");
    expect(lineText(paused, SV, NOW)).toBe("Pausad till kl. 15:00 så att timmen stannar under 6,0 kW.");
  });

  it("says when the house alone is over the target, and when the departure sets it aside", () => {
    expect(lineText(line("import_target_house_over", { target_kw: 6 }), EN, NOW)).toBe(
      "The house alone is above the 6.0 kW target this hour.",
    );
    const released = line("import_target_released", { target_kw: 6, departure: "2026-11-02T16:00:00+00:00" });
    expect(lineText(released, EN, NOW)).toBe(
      "The import target is set aside: the car needs the power to be ready by 17:00.",
    );
    expect(lineText(released, SV, NOW)).toBe(
      "Effektmålet släpps: bilen behöver effekten för att bli klar till avresan kl. 17:00.",
    );
  });

  it("words an older line for the import target as the plain limit", () => {
    const older = line("load_balancing_limited", { limit_a: 7, phase: null, cause: "import_target" });
    expect(lineText(older, EN, NOW)).toBe(lineText(line("load_balancing_limited", { limit_a: 7, phase: null, cause: null }), EN, NOW));
  });
});
