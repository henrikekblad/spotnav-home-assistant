// The year's review ("Året med SpotNav", `spotnav/get_year_review` API v1): the decoder and its types. The backend
// works every page out (`year_review/compute.py`) and leaves out a page with nothing to stand on; the card only words
// what it is given. Money in the area's minor unit (öre, cent), prices per kWh in the minor unit, dates ISO.
//
// Exact keys (`contract.ts`): a key the decoder does not know fails in the tests, and a page the card does not know is
// skipped in production (a newer backend may add one) but fails in the tests.

import { contract } from "./contract";
import {
  bad,
  exactKeys,
  flag,
  isRecord,
  list,
  Malformed,
  number,
  numberOrNull,
  record,
  text,
  textOrNull,
} from "./decode-primitives";

export const YEAR_REVIEW_API_VERSION = 1;

export type YearReviewState = "ready" | "computing" | "none";

export interface YearReviewMoney {
  currency: string | null;
  major_unit: string | null;
  minor_unit: string | null;
}

export interface CoverPage {
  id: "cover";
}

export interface SavedPage {
  id: "saved";
  total: number;
  battery: number | null;
  car: number | null;
  heat: number | null;
  estimated: boolean;
  starts: { battery: string | null; car: string | null; heat: string | null };
}

export interface CarPage {
  id: "car";
  cheap_share_pct: number | null;
  charges: number;
  kwh: number;
  cheapest: { price: number; date: string } | null;
  estimated: boolean;
  since: string | null;
}

export interface SunPage {
  id: "sun";
  kwh: number;
  home_pct: number | null;
  /** Whether what went out earned anything: the card says "sold" only when it did. */
  export_paid: boolean | null;
  since: string | null;
}

export interface BatteryPage {
  id: "battery";
  earned: number | null;
  earned_since: string | null;
  /** What the battery saved at each hour's price, whoever controlled it (≈). */
  saved: number | null;
  saved_since: string | null;
  cycles: number | null;
  buy_price: number | null;
  use_price: number | null;
  kwh_in: number | null;
  kwh_out: number | null;
}

export interface FusePage {
  id: "fuse";
  count: number;
  since: string | null;
}

export interface ExpensivePage {
  id: "expensive";
  price: number;
  /** The hour's local start with its offset. */
  start: string;
  bought_kwh: number | null;
  since: string | null;
}

export interface ThanksPage {
  id: "thanks";
}

export type YearReviewPage = CoverPage | SavedPage | CarPage | SunPage | BatteryPage | FusePage | ExpensivePage | ThanksPage;
export type YearReviewPageId = YearReviewPage["id"];

export interface YearReview {
  year: number;
  through: string;
  since: string | null;
  partial: boolean;
  money: YearReviewMoney | null;
  pages: YearReviewPage[];
}

export interface YearReviewAnswer {
  api_version: number;
  site_id: string;
  year: number;
  state: YearReviewState;
  banner: boolean;
  row: boolean;
  review: YearReview | null;
}

export type YearReviewDecodeResult = { ok: true; value: YearReviewAnswer } | { ok: false; failure: "unsupported" | "malformed" };

const ANSWER_KEYS = ["api_version", "site_id", "year", "state", "banner", "row", "review"] as const;
const REVIEW_KEYS = ["year", "through", "since", "partial", "money", "pages"] as const;
const MONEY_KEYS = ["currency", "major_unit", "minor_unit"] as const;
export const PAGE_KEYS: Record<YearReviewPageId, readonly string[]> = {
  cover: ["id"],
  saved: ["id", "total", "battery", "car", "heat", "estimated", "starts"],
  car: ["id", "cheap_share_pct", "charges", "kwh", "cheapest", "estimated", "since"],
  sun: ["id", "kwh", "home_pct", "export_paid", "since"],
  battery: ["id", "earned", "earned_since", "saved", "saved_since", "cycles", "buy_price", "use_price", "kwh_in", "kwh_out"],
  fuse: ["id", "count", "since"],
  expensive: ["id", "price", "start", "bought_kwh", "since"],
  thanks: ["id"],
};
const PART_KEYS = ["battery", "car", "heat"] as const;
const STATES: readonly YearReviewState[] = ["ready", "computing", "none"];

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;

function date(value: unknown): string {
  const raw = text(value);
  return DATE.test(raw) ? raw : bad();
}

function dateOrNull(value: unknown): string | null {
  return value === null ? null : date(value);
}

function count(value: unknown): number {
  const raw = number(value);
  return Number.isInteger(raw) && raw >= 0 ? raw : bad();
}

function flagOrNull(value: unknown): boolean | null {
  return value === null ? null : flag(value);
}

function percentOrNull(value: unknown): number | null {
  const raw = numberOrNull(value);
  return raw === null || (raw >= 0 && raw <= 100) ? raw : bad();
}

function isPageId(value: unknown): value is YearReviewPageId {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(PAGE_KEYS, value);
}

/** One page, or `null` for a page this card does not know (outside the tests, where it fails). */
function page(value: unknown): YearReviewPage | null {
  const source = record(value);
  const id = source.id;
  if (!isPageId(id)) {
    if (contract.strict || typeof id !== "string") {
      bad();
    }
    return null;
  }
  exactKeys(source, PAGE_KEYS[id]);
  switch (id) {
    case "cover":
    case "thanks":
      return { id };
    case "saved": {
      const starts = record(source.starts);
      exactKeys(starts, PART_KEYS);
      return {
        id,
        total: number(source.total),
        battery: numberOrNull(source.battery),
        car: numberOrNull(source.car),
        heat: numberOrNull(source.heat),
        estimated: flag(source.estimated),
        starts: { battery: dateOrNull(starts.battery), car: dateOrNull(starts.car), heat: dateOrNull(starts.heat) },
      };
    }
    case "car": {
      let cheapest: CarPage["cheapest"] = null;
      if (source.cheapest !== null) {
        const raw = record(source.cheapest);
        exactKeys(raw, ["price", "date"]);
        cheapest = { price: number(raw.price), date: date(raw.date) };
      }
      return {
        id,
        cheap_share_pct: percentOrNull(source.cheap_share_pct),
        charges: count(source.charges),
        kwh: number(source.kwh),
        cheapest,
        estimated: flag(source.estimated),
        since: dateOrNull(source.since),
      };
    }
    case "sun":
      return {
        id,
        kwh: number(source.kwh),
        home_pct: percentOrNull(source.home_pct),
        export_paid: flagOrNull(source.export_paid),
        since: dateOrNull(source.since),
      };
    case "battery":
      return {
        id,
        earned: numberOrNull(source.earned),
        earned_since: dateOrNull(source.earned_since),
        saved: numberOrNull(source.saved),
        saved_since: dateOrNull(source.saved_since),
        cycles: numberOrNull(source.cycles),
        buy_price: numberOrNull(source.buy_price),
        use_price: numberOrNull(source.use_price),
        kwh_in: numberOrNull(source.kwh_in),
        kwh_out: numberOrNull(source.kwh_out),
      };
    case "fuse":
      return { id, count: count(source.count), since: dateOrNull(source.since) };
    case "expensive": {
      const start = text(source.start);
      if (!INSTANT.test(start)) {
        bad();
      }
      return {
        id,
        price: number(source.price),
        start,
        bought_kwh: numberOrNull(source.bought_kwh),
        since: dateOrNull(source.since),
      };
    }
  }
}

function review(value: unknown): YearReview {
  const source = record(value);
  exactKeys(source, REVIEW_KEYS);
  let money: YearReviewMoney | null = null;
  if (source.money !== null) {
    const raw = record(source.money);
    exactKeys(raw, MONEY_KEYS);
    money = { currency: textOrNull(raw.currency), major_unit: textOrNull(raw.major_unit), minor_unit: textOrNull(raw.minor_unit) };
  }
  const pages = list(source.pages)
    .map(page)
    .filter((entry): entry is YearReviewPage => entry !== null);
  const year = count(source.year);
  return {
    year,
    through: date(source.through),
    since: dateOrNull(source.since),
    partial: flag(source.partial),
    money,
    pages,
  };
}

/** `spotnav/get_year_review`'s answer, strictly typed: the review only when the state is ready. */
export function decodeYearReview(raw: unknown): YearReviewDecodeResult {
  try {
    if (!isRecord(raw)) {
      bad();
    }
    if (raw.api_version !== YEAR_REVIEW_API_VERSION) {
      return { ok: false, failure: typeof raw.api_version === "number" ? "unsupported" : "malformed" };
    }
    exactKeys(raw, ANSWER_KEYS);
    const state = raw.state;
    if (typeof state !== "string" || !STATES.includes(state as YearReviewState)) {
      bad();
    }
    const decoded = raw.review === null ? null : review(raw.review);
    if ((state === "ready") !== (decoded !== null)) {
      bad();
    }
    return {
      ok: true,
      value: {
        api_version: YEAR_REVIEW_API_VERSION,
        site_id: text(raw.site_id),
        year: count(raw.year),
        state: state as YearReviewState,
        banner: flag(raw.banner),
        row: flag(raw.row),
        review: decoded,
      },
    };
  } catch (error) {
    if (error instanceof Malformed) {
      return { ok: false, failure: "malformed" };
    }
    throw error;
  }
}
