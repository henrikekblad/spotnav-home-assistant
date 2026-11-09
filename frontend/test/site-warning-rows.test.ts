// The site's notes in Settings sit under one "To check" heading, the label the entity dialogs use.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { decodeEntityAnswer, type EntitySite } from "../src/entity-config";
import { siteWarningRows } from "../src/entity-editor";
import { translate } from "../src/i18n";

const DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");

function site(codes: string[]): EntitySite {
  const decoded = decodeEntityAnswer(JSON.parse(readFileSync(join(DIR, "get_derived.json"), "utf8")) as unknown);
  if (!decoded.ok || !decoded.value.ok || decoded.value.config.site === null) {
    throw new Error("fixture does not decode");
  }
  const base = decoded.value.config.site;
  return {
    ...base,
    measurement: { ...base.measurement, currentEstimated: false },
    warnings: codes.map((code) => ({ code, deviceName: "Easee Equalizer", limitsA: null }) as never),
  };
}

describe("the site's notes in Settings", () => {
  it("are one group with the To check heading, every note under it", () => {
    const rows = siteWarningRows(document, "sv", site(["own_load_balancing", "external_current_balancer"]));
    expect(rows).toHaveLength(1);
    const group = rows[0]!;
    expect(group.firstElementChild?.textContent).toBe(translate("sv", "entity.checks.title"));
    expect(Array.from(group.querySelectorAll("[data-warning]")).map((row) => (row as HTMLElement).dataset["warning"])).toEqual([
      "own_load_balancing",
      "external_current_balancer",
    ]);
  });

  it("keep the heading for a single note", () => {
    const [group] = siteWarningRows(document, "en", site(["own_load_balancing"]));
    expect(group?.firstElementChild?.textContent).toBe("To check");
    expect(group?.querySelectorAll("[data-warning]")).toHaveLength(1);
  });

  it("are nothing, heading included, when there are none", () => {
    expect(siteWarningRows(document, "en", site([]))).toEqual([]);
  });
});
