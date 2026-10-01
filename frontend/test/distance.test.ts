import { describe, expect, it } from "vitest";

import { distanceText } from "../src/format";

describe("distanceText", () => {
  it("writes Scandinavian miles for Swedish and Norwegian", () => {
    expect(distanceText("sv", 15.2)).toBe("15,2 mil");
    expect(distanceText("nb", 15.2)).toBe("15,2 mil");
  });

  it("writes kilometres for every other language", () => {
    expect(distanceText("en", 15.2)).toBe("152 km");
    expect(distanceText("da", 15.2)).toBe("152 km");
    expect(distanceText("fi", 15.2)).toBe("152 km");
  });
});
