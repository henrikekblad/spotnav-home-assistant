// The additive-tolerant decoders (house, house history, house loads, house now, heat history, power month, sessions)
// read the keys they know one by one, so strict mode (`src/contract.ts`) cannot reject the keys they ignore. This
// audit does the same job from outside: every key of every contract fixture must be READ by its decoder. A key the
// backend added to a fixture that the decoder never touches is contract drift, caught here in CI while production
// still ignores unknown keys.

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { decodeHeatHistory } from "../src/heat-history";
import { decodeHouseHistory } from "../src/house-history";
import { decodeHouseLoads } from "../src/house-loads";
import { decodeHouse } from "../src/house-model";
import { decodeHouseNow } from "../src/house-now";
import { decodePowerMonth } from "../src/power-view";

const FIXTURES = join(__dirname, "..", "..", "tests", "fixtures");

/** `raw` wrapped so every key a decoder reads is noted under its path; arrays and objects alike. */
function watched(raw: unknown, path: string, read: Set<string>): unknown {
  if (typeof raw !== "object" || raw === null) {
    return raw;
  }
  const children = new Map<string, unknown>();
  return new Proxy(raw as object, {
    get(target, key, receiver) {
      const value = Reflect.get(target, key, receiver);
      if (typeof key !== "string" || (Array.isArray(target) && key === "length")) {
        return value;
      }
      const at = `${path}.${Array.isArray(target) ? "[]" : key}`;
      if (Object.prototype.hasOwnProperty.call(target, key)) {
        read.add(at);
        if (!children.has(key)) {
          children.set(key, watched(value, at, read));
        }
        return children.get(key);
      }
      return value;
    },
  });
}

function paths(value: unknown, path: string, into: Set<string>): void {
  if (typeof value !== "object" || value === null) {
    return;
  }
  for (const [key, inner] of Object.entries(value)) {
    const at = `${path}.${Array.isArray(value) ? "[]" : key}`;
    into.add(at);
    paths(inner, at, into);
  }
}

/**
 * Keys the backend sends that the card knowingly does not read (the app and the diagnostics do). Listing one here
 * is the decision; a key neither read nor listed fails the audit.
 */
const KNOWN_UNUSED: Record<string, readonly string[]> = {
  // `demand_ceiling`: the soft ceiling under the demand charge, in shadow; not drawn yet.
  house: [".state", ".days.[].arbitrage", ".today.battery_export_kwh", ".today.quarters", ".demand_ceiling"],
  house_history: [".generated_at"],
  heat_history: [".generated_at"],
  power_month: [".site_id"],
};

const DECODERS: [string, (raw: unknown) => unknown][] = [
  ["house", decodeHouse],
  ["house_history", decodeHouseHistory],
  ["house_loads", decodeHouseLoads],
  ["house_now", decodeHouseNow],
  ["heat_history", decodeHeatHistory],
  ["power_month", decodePowerMonth],
];

describe("every key of a contract fixture is read by the tolerant decoders", () => {
  for (const [dir, decode] of DECODERS) {
    it(dir, () => {
      const names = readdirSync(join(FIXTURES, dir)).filter((name) => name.endsWith(".json"));
      expect(names.length).toBeGreaterThan(0);
      const unread: string[] = [];
      for (const name of names) {
        const raw = JSON.parse(readFileSync(join(FIXTURES, dir, name), "utf8"));
        const read = new Set<string>();
        decode(watched(raw, "", read));
        const present = new Set<string>();
        paths(raw, "", present);
        for (const path of present) {
          if (!read.has(path) && !(KNOWN_UNUSED[dir] ?? []).includes(path)) {
            unread.push(`${name}: ${path}`);
          }
        }
      }
      expect(unread).toEqual([]);
    });
  }
});
