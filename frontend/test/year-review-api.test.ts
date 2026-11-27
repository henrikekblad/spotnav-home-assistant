// The year review's two messages on the wire, exactly.

import { describe, expect, it } from "vitest";

import { getYearReview, markYearReviewSeen } from "../src/api";
import { FakeHass } from "./helpers";

describe("the year review's commands", () => {
  it("asks for a site's review by the site's id or by a charger's", async () => {
    const hass = new FakeHass();
    void getYearReview(hass, { site: "site_villa" });
    void getYearReview(hass, { charger: "entry_a" });
    expect(hass.messages).toEqual([
      { type: "spotnav/get_year_review", api_version: 1, site_id: "site_villa" },
      { type: "spotnav/get_year_review", api_version: 1, charger_id: "entry_a" },
    ]);
  });

  it("says the review was opened or its banner closed, for the year and site", async () => {
    const hass = new FakeHass();
    void markYearReviewSeen(hass, "site_villa", 2026, "viewed");
    void markYearReviewSeen(hass, "site_villa", 2026, "dismissed");
    expect(hass.messages).toEqual([
      { type: "spotnav/year_review_seen", api_version: 1, site_id: "site_villa", year: 2026, action: "viewed" },
      { type: "spotnav/year_review_seen", api_version: 1, site_id: "site_villa", year: 2026, action: "dismissed" },
    ]);
  });
});
