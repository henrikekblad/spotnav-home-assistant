// Saving a text file from the card: a Blob, a temporary link with the file name, a click, and cleanup.

import { afterEach, describe, expect, it, vi } from "vitest";

import { saveTextFile } from "../src/download";

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("saveTextFile", () => {
  it("clicks a hidden link to a Blob with the file name, then removes the link and releases the URL", async () => {
    const create = vi.fn(() => "blob:test");
    const revoke = vi.fn();
    Object.assign(window.URL, { createObjectURL: create, revokeObjectURL: revoke });
    const clicks: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this);
    });

    saveTextFile(document, "spotnav-sessions.csv", "a,b\n1,2\n");

    expect(clicks).toHaveLength(1);
    expect(clicks[0]?.download).toBe("spotnav-sessions.csv");
    expect(clicks[0]?.getAttribute("href")).toBe("blob:test");
    expect(document.querySelector("a")).toBeNull();
    const blob = (create.mock.calls[0] as unknown as [Blob])[0];
    expect(blob.type).toBe("text/csv;charset=utf-8");
    expect(await blob.text()).toBe("a,b\n1,2\n");
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(revoke).toHaveBeenCalledWith("blob:test");
  });
});
