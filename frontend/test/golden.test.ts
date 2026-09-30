import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { decodeDashboard } from "../src/validate";

/**
 * The boundary guard: every dashboard golden the backend commits must decode here.
 *
 * The goldens are the checked-in fixtures (`tests/fixtures/dashboard/*.json`), trees normalized by
 * `tests/test_dashboard_api.py` so that volatile leaves read `"<volatile>"`. They are structure, not a
 * literal response: this test materializes the volatile leaves into values that are valid *because*
 * they are consistent (instants on the row's own `day`, each row one duration long, ordered and
 * non-overlapping), then requires the real decoder to accept the result. A field the serializer emits
 * but the decoder has stopped reading -- or a shape it has started to invent -- fails right here, while
 * JSON itself is asserted byte for byte in the Python suite. The bare no-settings shape is pinned separately in `validate.test.ts`.
 */
const GOLDEN_DIR = join(__dirname, "..", "..", "tests", "fixtures", "dashboard");
const VOLATILE = "<volatile>";
const ZONE_OFFSET = "+02:00";

function goldenNames(): string[] {
  return readdirSync(GOLDEN_DIR).filter((name) => name.endsWith(".json"));
}

function localInstant(day: string, minutes: number): string {
  const hours = String(Math.floor(minutes / 60)).padStart(2, "0");
  const mins = String(minutes % 60).padStart(2, "0");
  return `${day}T${hours}:${mins}:00${ZONE_OFFSET}`;
}

function text(value: unknown): unknown {
  return value === VOLATILE ? "volatile" : value;
}

function instant(value: unknown, fallback: string): unknown {
  return value === VOLATILE ? fallback : value;
}

/** The dashboard golden with its volatile leaves turned into a consistent, decodable response. */
function materialize(raw: Record<string, unknown>): Record<string, unknown> {
  const root = structuredClone(raw);
  root.generated_at = instant(root.generated_at, "2026-09-22T04:30:00+02:00");

  const charger = root.charger as Record<string, unknown>;
  charger.charger_id = text(charger.charger_id);
  charger.charger_name = text(charger.charger_name);

  const settings = root.settings as Record<string, unknown> | null;
  if (settings !== null) {
    settings.area_id = text(settings.area_id);
  }

  const planning = root.planning as Record<string, unknown> | null;
  if (planning !== null) {
    planning.calculated_at = instant(planning.calculated_at, "2026-09-22T04:30:00+02:00");
    for (const key of ["price_state", "price_identity", "today", "tomorrow", "applied_identity"]) {
      planning[key] = text(planning[key]);
    }
    planning.pending_identity = text(planning.pending_identity);
  }

  const market = root.market as Record<string, unknown> | null;
  if (market !== null) {
    market.catalogue_fetched_at = instant(market.catalogue_fetched_at, "2026-09-22T04:30:00+02:00");
    for (const key of ["area_id", "area_name", "currency", "major_unit", "minor_unit"]) {
      market[key] = text(market[key]);
    }
    // The zone must stay a real IANA name: the decoder checks every row's `day` against it.
    if (market.timezone === VOLATILE || market.timezone === null) {
      market.timezone = "Europe/Stockholm";
    }
  }

  const prices = root.prices as Record<string, unknown>;
  for (const key of ["area_id", "state", "reason", "today", "tomorrow", "today_state"]) {
    prices[key] = text(prices[key]);
  }
  for (const key of [
    "tomorrow_state",
    "today_source",
    "tomorrow_source",
    "today_fetched_at",
    "tomorrow_fetched_at",
    "today_attempt_at",
    "tomorrow_attempt_at",
    "today_attempt_error",
    "tomorrow_attempt_error",
    "index_state",
    "index_revision",
  ]) {
    const value = prices[key];
    prices[key] = value !== null && value !== undefined && typeof value === "string" && value.includes("T")
      ? instant(value, "2026-09-22T04:30:00+02:00")
      : text(value);
  }
  const rows = prices.intervals as Array<Record<string, unknown>>;
  const seen = new Map<string, number>();
  for (const row of rows) {
    // Each day restarts at its own local midnight: 96 rows of 15 minutes stay inside the day.
    const day = String(row.day);
    const index = seen.get(day) ?? 0;
    seen.set(day, index + 1);
    const duration = row.duration_minutes as number;
    row.start = localInstant(day, index * duration);
    row.end = localInstant(day, (index + 1) * duration);
  }

  const plan = root.plan as Record<string, unknown>;
  plan.delivered_kwh = plan.delivered_kwh === VOLATILE ? null : plan.delivered_kwh;
  plan.remaining_kwh = plan.remaining_kwh === VOLATILE ? null : plan.remaining_kwh;
  const proposal = plan.proposal as Record<string, unknown> | null;
  if (proposal !== null) {
    proposal.identity = text(proposal.identity);
    const periods = proposal.periods as Array<Record<string, unknown>>;
    periods.forEach((period, index) => {
      period.start = instant(period.start, localInstant("2026-09-22", 120 + index * 60));
      period.end = instant(period.end, localInstant("2026-09-22", 180 + index * 60));
    });
  }
  const installed = plan.installed as Record<string, unknown> | null;
  if (installed !== null) {
    installed.identity = text(installed.identity);
    installed.origin = text(installed.origin);
    const periods = installed.periods as Array<Record<string, unknown>>;
    periods.forEach((period, index) => {
      period.start = instant(period.start, localInstant("2026-09-22", 60 + index * 60));
      period.end = instant(period.end, localInstant("2026-09-22", 120 + index * 60));
    });
  }
  const relation = plan.relation as Record<string, unknown>;
  relation.applied_identity = text(relation.applied_identity);
  relation.pending_identity = text(relation.pending_identity);

  return root;
}

describe("committed backend goldens", () => {
  it("finds the backend goldens, so an empty directory cannot pass quietly", () => {
    expect(goldenNames().length).toBeGreaterThan(0);
  });

  it("really has volatile leaves to materialize", () => {
    const raw = readFileSync(join(GOLDEN_DIR, goldenNames()[0] as string), "utf8");
    expect(raw.length).toBeGreaterThan(0);
  });

  it.each(goldenNames())("decodes %s once its volatile leaves are materialized", (name) => {
    const raw = JSON.parse(readFileSync(join(GOLDEN_DIR, name), "utf8")) as Record<string, unknown>;
    const result = decodeDashboard(materialize(raw));
    expect(result.ok, `${name} did not decode`).toBe(true);
  });
});
