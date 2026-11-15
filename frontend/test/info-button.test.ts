// The reusable info button: collapsed by default, toggles its paragraph, and keeps its aria state in step.

import { describe, expect, it } from "vitest";

import { infoButton } from "../src/info-button";
import { translate, type Language } from "../src/i18n";

describe("infoButton", () => {
  it("starts collapsed and toggles the help with aria-expanded, aria-controls and a label that says what a press does", () => {
    const help = document.createElement("p");
    const { button, collapse } = infoButton(document, "sv", help, "x-help");
    expect(help.id).toBe("x-help");
    expect(help.hidden).toBe(true);
    expect(button.getAttribute("aria-controls")).toBe("x-help");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.getAttribute("aria-label")).toBe("Visa hjälp");
    button.click();
    expect(help.hidden).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(button.getAttribute("aria-label")).toBe("Dölj hjälp");
    button.click();
    expect(help.hidden).toBe(true);
    button.click();
    collapse();
    expect(help.hidden).toBe(true);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("has both labels in every language", () => {
    for (const language of ["en", "sv", "da", "nb", "fi", "de", "fr", "es", "nl"] as Language[]) {
      expect(translate(language, "settings.info.show")).not.toBe("settings.info.show");
      expect(translate(language, "settings.info.hide")).not.toBe("settings.info.hide");
    }
  });
});
