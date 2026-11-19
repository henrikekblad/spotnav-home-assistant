// The card's unknown-key policy (`src/contract.ts`): in production a decoder IGNORES keys it does not know, so an
// additive backend key never blanks a card; a missing or mistyped KNOWN key still rejects. The test setup turns
// the strict mode on, so the other tests fail on a fixture with a key the decoders do not know; here it is
// switched off to prove the production behaviour on every strict decoder.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { decodeBetaAnswer } from "../src/beta";
import { contract } from "../src/contract";
import { decodeEntityAnswer } from "../src/entity-config";
import { decodeHeat } from "../src/heat-model";
import { decodeSettingsAnswer } from "../src/settings";
import { decodeSiteSettingsAnswer } from "../src/site-settings";
import { decodeDashboard } from "../src/validate";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

function load(dir: string, v?: string): [string, unknown][] {
  const path = v === undefined ? join(FIXTURES, dir) : join(FIXTURES, dir, v);
  return readdirSync(path)
    .filter((name) => name.endsWith(".json"))
    .map((name) => [`${dir}/${name}`, JSON.parse(readFileSync(join(path, name), "utf8"))]);
}

/**
 * A copy of `value` with an extra key in every object, as a newer backend might send. The free-form `params`
 * maps (translation parameters, text to text) are not part of the key contract and are left alone.
 */
function withFutureKeys(value: unknown, mapped = false): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => withFutureKeys(item));
  }
  if (typeof value === "object" && value !== null) {
    const copy: Record<string, unknown> = {};
    for (const [key, inner] of Object.entries(value)) {
      copy[key] = withFutureKeys(inner, key === "params");
    }
    if (!mapped) {
      copy["future_key"] = { anything: [1, 2] };
    }
    return copy;
  }
  return value;
}

/** Strip the key again from whatever a decoder echoes, so a result can be compared with the clean one. */
function plain(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value, (key, inner) => (key === "future_key" ? undefined : inner)));
}

const DECODERS: [string, [string, unknown][], (raw: unknown) => unknown][] = [
  ["dashboard", load("dashboard"), decodeDashboard],
  ["heat", load("heat_card"), decodeHeat],
  ["beta", load("beta"), decodeBetaAnswer],
  ["settings", load("settings", "v1"), decodeSettingsAnswer],
  ["site settings", load("site_settings", "v1"), decodeSiteSettingsAnswer],
  ["entity config", load("entity_config", "v1"), decodeEntityAnswer],
];

describe("the unknown-key policy", () => {
  let strict = true;
  beforeEach(() => {
    strict = contract.strict;
  });
  afterEach(() => {
    contract.strict = strict;
  });

  it("runs the other tests in strict mode", () => {
    expect(strict).toBe(true);
  });

  for (const [label, fixtures, decode] of DECODERS) {
    it(`${label}: ignores unknown keys in production, rejects them in strict mode`, () => {
      expect(fixtures.length).toBeGreaterThan(0);
      for (const [name, raw] of fixtures) {
        contract.strict = false;
        const clean = decode(raw);
        const noisy = decode(withFutureKeys(raw));
        expect(plain(noisy), name).toEqual(plain(clean));
        contract.strict = true;
        expect(plain(decode(withFutureKeys(raw))), name).not.toEqual(plain(clean));
      }
    });
  }

  it("still rejects a mistyped known key in production", () => {
    contract.strict = false;
    const [, raw] = load("heat_card")[0]!;
    const broken = { ...(raw as Record<string, unknown>), now: 5 };
    expect(decodeHeat(broken)).toMatchObject({ ok: false });
  });
});
