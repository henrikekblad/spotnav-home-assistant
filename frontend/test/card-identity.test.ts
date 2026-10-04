// Which card this browser runs: the hash in the URL it was loaded from, against the one served.

import { afterEach, describe, expect, it } from "vitest";

import {
  bundleHashFromUrl,
  CARD_OUTDATED_NOTE,
  cardOutdated,
  clientBlock,
  decodeCardInfo,
  servedHashFromBundle,
  setOwnCardBundleHashForTest,
} from "../src/card-identity";
import { CARD_VERSION } from "../src/generated/version";

describe("card identity", () => {
  afterEach(() => {
    setOwnCardBundleHashForTest(null);
  });

  it("reads the hash from the card URL the integration hands browsers", () => {
    expect(bundleHashFromUrl("http://ha.local:8123/spotnav/spotnav-card.js?v=1.0.0-0a1b2c3d")).toBe("0a1b2c3d");
    expect(bundleHashFromUrl("https://x/spotnav/spotnav-card.js?v=1.0.0-beta.1-deadbeef")).toBe("deadbeef");
    expect(bundleHashFromUrl("https://x/spotnav/spotnav-card.js?v=1.0.0")).toBeNull();
    expect(bundleHashFromUrl("https://x/spotnav/spotnav-card.js")).toBeNull();
    expect(bundleHashFromUrl("https://x/spotnav/spotnav-card.js?v=1.0.0-XYZ")).toBeNull();
    expect(bundleHashFromUrl("not a url")).toBeNull();
    expect(bundleHashFromUrl(null)).toBeNull();
  });

  it("calls a card outdated only when both hashes are known and differ", () => {
    expect(cardOutdated("11111111", "22222222")).toBe(true);
    expect(cardOutdated("11111111", "11111111")).toBe(false);
    expect(cardOutdated(null, "11111111")).toBeNull();
    expect(cardOutdated("11111111", null)).toBeNull();
  });

  it("decodes the card info strictly", () => {
    expect(decodeCardInfo({ api_version: 1, ok: true, error: null, card_bundle_hash: "0a1b2c3d" })).toEqual({
      cardBundleHash: "0a1b2c3d",
    });
    expect(decodeCardInfo({ api_version: 1, ok: true, card_bundle_hash: null })).toEqual({ cardBundleHash: null });
    expect(decodeCardInfo({ api_version: 2, ok: true, card_bundle_hash: "0a1b2c3d" })).toBeNull();
    expect(decodeCardInfo("nope")).toBeNull();
  });

  it("prefers the served hash a bundle states over the file's", () => {
    expect(servedHashFromBundle({ versions: { card_bundle_hash: "22222222", card_bundle_hash_served: "33333333" } })).toBe(
      "33333333",
    );
    expect(servedHashFromBundle({ versions: { card_bundle_hash: "22222222", card_bundle_hash_served: null } })).toBe(
      "22222222",
    );
    expect(servedHashFromBundle({})).toBeNull();
  });

  it("builds the client block, with the note only for an outdated card", () => {
    setOwnCardBundleHashForTest("11111111");
    const long = "Mozilla/5.0 ".repeat(40);
    const stale = clientBlock(long, "22222222");
    expect(stale).toEqual({
      card_bundle_hash: "11111111",
      card_version: CARD_VERSION,
      user_agent: long.slice(0, 160),
      card_outdated: true,
      note: CARD_OUTDATED_NOTE,
    });
    expect(clientBlock("UA", "11111111")).toEqual({
      card_bundle_hash: "11111111",
      card_version: CARD_VERSION,
      user_agent: "UA",
      card_outdated: false,
    });
  });
});
