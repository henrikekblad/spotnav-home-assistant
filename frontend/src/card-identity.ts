// Which card this browser runs, against the one the integration serves.
//
// The card cannot carry its own bundle hash (the hash is of the file the hash would be written into),
// but it knows the URL it was loaded from: the integration hands browsers `/spotnav/spotnav-card.js?v=
// <version>-<hash>` (`card_asset.py`), so a browser or a Companion app holding an old module still
// reports the old hash. `spotnav/get_card_info` answers the hash served now; when the two differ the
// Support section says to reload, and the debug download records both under `client`.

import { CARD_VERSION } from "./generated/version";
import { DEBUG_API_VERSION } from "./types";

/** The note the debug file carries when the card in this browser differs from the one served. */
export const CARD_OUTDATED_NOTE =
  "The card in this browser or app is older than SpotNav. Reload the page; in the Home Assistant Companion app, force-stop the app and open it again.";

const HASH = /^[0-9a-f]{8}$/;
/** The longest user agent the debug file keeps. */
const USER_AGENT_LENGTH = 160;

/** The bundle hash in a card URL's `v=<version>-<hash>` query, or `null` when it carries none. */
export function bundleHashFromUrl(url: string | null | undefined): string | null {
  if (typeof url !== "string" || url === "") {
    return null;
  }
  let token: string | null;
  try {
    token = new URL(url).searchParams.get("v");
  } catch {
    return null;
  }
  if (token === null) {
    return null;
  }
  const last = token.slice(token.lastIndexOf("-") + 1);
  return token.includes("-") && HASH.test(last) ? last : null;
}

function loadedFrom(): string | null {
  try {
    return import.meta.url;
  } catch {
    return null;
  }
}

let ownHash: string | null = bundleHashFromUrl(loadedFrom());

/** The bundle hash of the card running here, from the URL it was loaded from (`null` when unknown). */
export function ownCardBundleHash(): string | null {
  return ownHash;
}

/** Tests only: pretend the card was loaded with this hash. */
export function setOwnCardBundleHashForTest(hash: string | null): void {
  ownHash = hash;
}

/** Whether the card here differs from the one served: `null` when either hash is unknown. */
export function cardOutdated(own: string | null, served: string | null): boolean | null {
  if (own === null || served === null) {
    return null;
  }
  return own !== served;
}

/** `{api_version, ok, card_bundle_hash}` from `spotnav/get_card_info`: the hash served, or `null`. */
export function decodeCardInfo(raw: unknown): { cardBundleHash: string | null } | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const record = raw as Record<string, unknown>;
  if (record.api_version !== DEBUG_API_VERSION || record.ok !== true) {
    return null;
  }
  const hash = record.card_bundle_hash;
  return { cardBundleHash: typeof hash === "string" && HASH.test(hash) ? hash : null };
}

/** The hash the backend serves, as a debug bundle states it (`versions.card_bundle_hash_served`, else the
 * file's own `versions.card_bundle_hash`), or `null`. */
export function servedHashFromBundle(bundle: Record<string, unknown>): string | null {
  const versions = bundle.versions;
  if (typeof versions !== "object" || versions === null || Array.isArray(versions)) {
    return null;
  }
  const record = versions as Record<string, unknown>;
  for (const key of ["card_bundle_hash_served", "card_bundle_hash"]) {
    const value = record[key];
    if (typeof value === "string" && HASH.test(value)) {
      return value;
    }
  }
  return null;
}

export interface ClientBlock {
  card_bundle_hash: string | null;
  card_version: string;
  user_agent: string | null;
  card_outdated: boolean | null;
  note?: string;
}

/** The `client` block the debug download adds: the card running here, and whether it is outdated. */
export function clientBlock(userAgent: string | null | undefined, served: string | null): ClientBlock {
  const own = ownCardBundleHash();
  const outdated = cardOutdated(own, served);
  const block: ClientBlock = {
    card_bundle_hash: own,
    card_version: CARD_VERSION,
    user_agent: typeof userAgent === "string" && userAgent !== "" ? userAgent.slice(0, USER_AGENT_LENGTH) : null,
    card_outdated: outdated,
  };
  if (outdated === true) {
    block.note = CARD_OUTDATED_NOTE;
  }
  return block;
}
