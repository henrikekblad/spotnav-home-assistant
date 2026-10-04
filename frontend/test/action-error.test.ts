// A refused action's stable code, as the sentence the card shows for it.

import { describe, expect, it } from "vitest";

import { translate } from "../src/i18n";
import { actionErrorKey } from "../src/model";

describe("actionErrorKey", () => {
  it("names a Start refused because no car is plugged in, in every language", () => {
    const key = actionErrorKey("vehicle_not_connected");
    expect(key).toBe("action.error.notConnected");
    expect(translate("en", key)).toContain("No car is plugged in");
    for (const language of ["sv", "da", "nb", "fi"] as const) {
      expect(translate(language, key)).not.toBe(translate("en", key));
    }
  });

  it("keeps one generic sentence for a code it does not know", () => {
    expect(actionErrorKey("something_new")).toBe("action.error.generic");
  });
});
