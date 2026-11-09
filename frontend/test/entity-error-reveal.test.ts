// A refused Save brings the first field with an error into view and puts the focus on it.

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { decodeEntityAnswer } from "../src/entity-config";
import { entityEditorBody } from "../src/entity-editor";

const DIR = join(__dirname, "..", "..", "tests", "fixtures", "entity_config", "v1");

function build() {
  const raw = JSON.parse(readFileSync(join(DIR, "get_derived.json"), "utf8")) as unknown;
  const decoded = decodeEntityAnswer(raw);
  if (!decoded.ok || !decoded.value.ok) {
    throw new Error("fixture does not decode");
  }
  const built = entityEditorBody(
    document,
    "en",
    { scope: "site", config: decoded.value.config, hass: () => undefined, appliesText: null, readOnly: false },
    { onSave: () => undefined, onCancel: () => undefined },
    "t",
  );
  document.body.replaceChildren(built.body);
  return built;
}

const shown = (): HTMLElement[] =>
  Array.from(document.querySelectorAll<HTMLElement>("[data-field-error]")).filter((node) => !node.hidden);

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe("a failed Save", () => {
  it("scrolls the first field with an error into view and focuses it", () => {
    const built = build();
    const scrolled: Element[] = [];
    HTMLElement.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this);
    };
    // Report the later field first: the page order decides, not the order of the answer.
    built.markErrors([
      { field: "max_age_s", code: "required" },
      { field: "main_fuse_a", code: "required" },
    ]);
    const errors = shown();
    expect(errors.length).toBe(2);
    const first = errors[0]!;
    expect(scrolled).toEqual([first.parentElement]);
    const active = document.activeElement as HTMLElement;
    expect(active).not.toBe(document.body);
    expect(first.parentElement?.contains(active)).toBe(true);
    expect(active.getAttribute("aria-invalid")).toBe("true");
  });

  it("moves nothing when no field has an error", () => {
    const built = build();
    const scrolled: Element[] = [];
    HTMLElement.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this);
    };
    built.markErrors([]);
    expect(scrolled).toEqual([]);
    expect(document.activeElement).toBe(document.body);
  });
});
