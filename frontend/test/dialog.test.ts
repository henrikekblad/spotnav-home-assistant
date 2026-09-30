// The modal primitive: focus ownership, listener ownership, and the draft hazards it corrects.

import { describe, expect, it, vi } from "vitest";

import { createDialog, type DialogHandle } from "../src/dialog";
import { VISUAL_CLASSES } from "../src/visual-styles";
import { mountPoint } from "./visual-fixtures";

function dialogFor(root: ShadowRoot, prefix = "view", close = "Stäng"): DialogHandle {
  return createDialog({ owner: root, idPrefix: prefix, labels: { close } });
}

/** `"inert" in element` narrows the caller's own reference to `never`; this keeps it local. */
function supportsInert(element: HTMLElement): boolean {
  return "inert" in element;
}

function press(target: EventTarget, key: string, shiftKey = false): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true });
  target.dispatchEvent(event);
  return event;
}

describe("structure and labels", () => {
  it("keeps a deterministic, owner-scoped id and a labelled dialog", () => {
    const first = mountPoint("card-a");
    const second = mountPoint("card-b");
    const firstDialog = dialogFor(first.root, "card-a-issues", "Close");
    const secondDialog = dialogFor(second.root, "card-b-issues", "Close");
    firstDialog.show({ title: "Title A", body: document.createElement("p") });
    secondDialog.show({ title: "Title B", body: document.createElement("p") });
    expect(firstDialog.element.querySelector("h3")?.id).toBe("card-a-issues-dialog-title");
    expect(secondDialog.element.querySelector("h3")?.id).toBe("card-b-issues-dialog-title");
    expect(firstDialog.element.querySelector("h3")?.id).not.toBe(secondDialog.element.querySelector("h3")?.id);
    const dialog = firstDialog.element.querySelector<HTMLElement>('[role="dialog"]');
    expect(dialog?.getAttribute("aria-modal")).toBe("true");
    expect(dialog?.getAttribute("aria-labelledby")).toBe("card-a-issues-dialog-title");
    expect(dialog?.getAttribute("aria-describedby")).toBeNull();
    const close = firstDialog.element.querySelector("button");
    expect(close?.getAttribute("aria-label")).toBe("Close");
  });

  it("references the intro only when there is one, and replaces it on the next show", () => {
    const mount = mountPoint();
    const dialog = dialogFor(mount.root);
    dialog.show({ title: "A", body: document.createElement("p"), intro: "Intro text" });
    const element = dialog.element.querySelector<HTMLElement>('[role="dialog"]');
    const intro = dialog.element.querySelector<HTMLParagraphElement>("#view-dialog-intro");
    expect(element?.getAttribute("aria-describedby")).toBe(intro?.id);
    expect(intro?.textContent).toBe("Intro text");
    expect(intro?.hidden).toBe(false);
    dialog.hide();
    dialog.show({ title: "B", body: document.createElement("p") });
    expect(element?.getAttribute("aria-describedby")).toBeNull();
    expect(intro?.hidden).toBe(true);
    expect(intro?.textContent).toBe("");
  });
});

describe("closing and focus", () => {
  it("focuses inside on open, returns focus to the opener, and tolerates a removed opener", () => {
    const mount = mountPoint();
    const opener = document.createElement("button");
    opener.textContent = "open";
    mount.root.append(opener);
    opener.focus();
    const dialog = dialogFor(mount.root);
    dialog.show({ title: "A", body: document.createElement("p"), opener });
    expect(mount.root.activeElement).toBeInstanceOf(HTMLElement);
    const inside = mount.root.activeElement;
    expect(dialog.element.contains(inside)).toBe(true);
    dialog.hide();
    expect(mount.root.activeElement).toBe(opener);

    // An opener that was removed while the dialog was open must not throw, or steal focus.
    dialog.show({ title: "B", body: document.createElement("p"), opener });
    opener.remove();
    const survivor = document.createElement("button");
    mount.root.append(survivor);
    survivor.focus();
    expect(() => dialog.hide()).not.toThrow();
    expect(mount.root.activeElement).toBe(survivor);
  });

  it("closes on Escape, the close button and the backdrop itself, but not on a click inside", () => {
    const mount = mountPoint();
    const dialog = dialogFor(mount.root);
    const body = document.createElement("p");
    body.textContent = "content";
    dialog.show({ title: "A", body });
    body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(dialog.isOpen()).toBe(true);
    dialog.element.querySelector<HTMLElement>('[role="dialog"]')?.dispatchEvent(
      new MouseEvent("click", { bubbles: true }),
    );
    expect(dialog.isOpen()).toBe(true);
    dialog.element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(dialog.isOpen()).toBe(false);

    dialog.show({ title: "A", body });
    dialog.element.querySelector("button")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(dialog.isOpen()).toBe(false);

    dialog.show({ title: "A", body });
    press(document, "Escape");
    expect(dialog.isOpen()).toBe(false);
  });

  it("traps Tab and Shift+Tab inside the dialog", () => {
    const mount = mountPoint();
    const dialog = dialogFor(mount.root);
    const first = document.createElement("button");
    const last = document.createElement("button");
    const body = document.createElement("div");
    body.append(first, last);
    dialog.show({ title: "A", body });
    // The close button is the first focusable, then the two in the body.
    last.focus();
    const forward = press(document, "Tab");
    expect(forward.defaultPrevented).toBe(true);
    expect(mount.root.activeElement?.getAttribute("aria-label") ?? "").toBe("Stäng");
    const close = dialog.element.querySelector("button");
    close?.focus();
    const backward = press(document, "Tab", true);
    expect(backward.defaultPrevented).toBe(true);
    expect(mount.root.activeElement).toBe(last);
  });

  it("does not throw or focus anything when there is nothing focusable", () => {
    const mount = mountPoint();
    const dialog = dialogFor(mount.root);
    dialog.show({ title: "A", body: document.createTextNode("text only") });
    // The close button is always focusable, so the trap's empty case is exercised explicitly.
    const close = dialog.element.querySelector<HTMLButtonElement>("button");
    close?.setAttribute("disabled", "disabled");
    const tab = press(document, "Tab");
    expect(tab.defaultPrevented).toBe(true);
    close?.removeAttribute("disabled");
  });
});

describe("listener and background ownership", () => {
  it("registers one close listener and one keyboard trap, however often it is shown", () => {
    const mount = mountPoint();
    const dialog = dialogFor(mount.root);
    const add = vi.spyOn(document, "addEventListener");
    dialog.show({ title: "A", body: document.createElement("p") });
    const keydownAdds = add.mock.calls.filter((call) => call[0] === "keydown").length;
    expect(keydownAdds).toBe(1);

    const close = dialog.element.querySelector<HTMLButtonElement>("button");
    const closeAdd = vi.spyOn(close as HTMLButtonElement, "addEventListener");
    // A second show replaces the body and must not stack another close handler.
    dialog.show({ title: "B", body: document.createElement("p") });
    expect(closeAdd).not.toHaveBeenCalled();
    expect(add.mock.calls.filter((call) => call[0] === "keydown").length).toBe(1);
    expect(dialog.element.querySelector("h3")?.textContent).toBe("B");
    expect(dialog.isOpen()).toBe(true);

    dialog.hide();
    const remove = vi.spyOn(document, "removeEventListener");
    dialog.show({ title: "C", body: document.createElement("p") });
    dialog.hide();
    expect(remove.mock.calls.filter((call) => call[0] === "keydown").length).toBe(1);
    // Escape after a hide reaches nothing: the trap is gone, and the dialog does not reopen.
    press(document, "Escape");
    expect(dialog.isOpen()).toBe(false);
  });

  it("hides the card content from assistive tech, and restores it on close", () => {
    const mount = mountPoint();
    const card = document.createElement("div");
    mount.root.append(card);
    const dialog = createDialog({
      owner: mount.root,
      idPrefix: "view",
      labels: { close: "Close" },
      background: () => card,
    });
    expect(card.getAttribute("aria-hidden")).toBeNull();
    dialog.show({ title: "A", body: document.createElement("p") });
    if (supportsInert(card)) {
      expect((card as HTMLElement & { inert: boolean }).inert).toBe(true);
      expect(card.getAttribute("aria-hidden")).toBeNull();
    } else {
      // The tested fallback on platforms without `inert`.
      expect(card.getAttribute("aria-hidden")).toBe("true");
    }
    dialog.hide();
    expect(card.getAttribute("aria-hidden")).toBeNull();
  });

  it("restores a background that was already inert, and a pre-existing aria-hidden", () => {
    const mount = mountPoint();
    const card = document.createElement("div");
    card.setAttribute("aria-hidden", "true");
    mount.root.append(card);
    Object.defineProperty(card, "inert", { value: true, writable: true, configurable: true });
    const dialog = createDialog({
      owner: mount.root,
      idPrefix: "inert-true",
      labels: { close: "Close" },
      background: () => card,
    });
    dialog.show({ title: "A", body: document.createElement("p") });
    expect((card as HTMLElement & { inert: boolean }).inert).toBe(true);
    dialog.hide();
    // "Was inert" stays inert: closing a dialog must not quietly make the card interactive.
    expect((card as HTMLElement & { inert: boolean }).inert).toBe(true);
    expect(card.getAttribute("aria-hidden")).toBe("true");
  });

  it("restores any non-null aria-hidden value too", () => {
    const mount = mountPoint();
    const card = document.createElement("div");
    card.setAttribute("aria-hidden", "false");
    mount.root.append(card);
    Object.defineProperty(card, "inert", { value: false, writable: true, configurable: true });
    const dialog = createDialog({
      owner: mount.root,
      idPrefix: "aria-value",
      labels: { close: "Close" },
      background: () => card,
    });
    dialog.show({ title: "A", body: document.createElement("p") });
    dialog.hide();
    expect(card.getAttribute("aria-hidden")).toBe("false");
    expect((card as HTMLElement & { inert: boolean }).inert).toBe(false);
  });

  it("restores the background when destroyed while open", () => {
    const mount = mountPoint();
    const card = document.createElement("div");
    mount.root.append(card);
    Object.defineProperty(card, "inert", { value: false, writable: true, configurable: true });
    const dialog = createDialog({
      owner: mount.root,
      idPrefix: "destroyed-open",
      labels: { close: "Close" },
      background: () => card,
    });
    dialog.show({ title: "A", body: document.createElement("p") });
    expect((card as HTMLElement & { inert: boolean }).inert).toBe(true);
    dialog.destroy();
    expect((card as HTMLElement & { inert: boolean }).inert).toBe(false);
    expect(card.getAttribute("aria-hidden")).toBeNull();
  });

  it("can close without restoring focus, for a caller switching to another modal", () => {
    const mount = mountPoint();
    const opener = document.createElement("button");
    mount.root.append(opener);
    opener.focus();
    const dialog = dialogFor(mount.root);
    dialog.show({ title: "A", body: document.createElement("p"), opener });
    dialog.hide({ restoreFocus: false });
    expect(mount.root.activeElement).not.toBe(opener);
    dialog.show({ title: "B", body: document.createElement("p"), opener });
    dialog.hide();
    expect(mount.root.activeElement).toBe(opener);
  });

  it("uses `inert` when the element supports it, leaving aria-hidden alone", () => {
    const mount = mountPoint();
    const card = document.createElement("div");
    mount.root.append(card);
    Object.defineProperty(card, "inert", { value: false, writable: true, configurable: true });
    const dialog = createDialog({
      owner: mount.root,
      idPrefix: "view",
      labels: { close: "Close" },
      background: () => card,
    });
    dialog.show({ title: "A", body: document.createElement("p") });
    expect((card as HTMLElement & { inert: boolean }).inert).toBe(true);
    expect(card.getAttribute("aria-hidden")).toBeNull();
    dialog.hide();
    expect((card as HTMLElement & { inert: boolean }).inert).toBe(false);
    expect(card.getAttribute("aria-hidden")).toBeNull();
  });

  it("leaves nothing behind on destroy", () => {
    const mount = mountPoint();
    const dialog = dialogFor(mount.root);
    dialog.show({ title: "A", body: document.createElement("p") });
    const remove = vi.spyOn(document, "removeEventListener");
    dialog.destroy();
    expect(dialog.isOpen()).toBe(false);
    expect(dialog.element.isConnected).toBe(false);
    expect(remove.mock.calls.filter((call) => call[0] === "keydown").length).toBeGreaterThan(0);
    const again = press(document, "Escape");
    expect(again.defaultPrevented).toBe(false);
    expect(dialog.isOpen()).toBe(false);
    expect(() => dialog.show({ title: "B", body: document.createElement("p") })).not.toThrow();
    expect(dialog.isOpen()).toBe(false);
  });

  it("opens as an overlay that never changes the card's layout", () => {
    const mount = mountPoint();
    const card = document.createElement("div");
    mount.root.append(card);
    const heightBefore = card.getBoundingClientRect().height;
    const dialog = dialogFor(mount.root);
    dialog.show({ title: "A", body: document.createElement("p") });
    expect(card.contains(dialog.element)).toBe(false);
    // Appended to the shadow root itself: `parentElement` is null when the parent is a root.
    expect(dialog.element.parentNode).toBe(mount.root);
    expect(card.getBoundingClientRect().height).toBe(heightBefore);
    expect(mount.root.querySelectorAll("[role='dialog']").length).toBe(1);
  });
});

// ------------------------------------------------------------------------------------------------
// The compact header: one row for the title and the close control, on every dialog the shell builds.
// ------------------------------------------------------------------------------------------------

describe("the compact header", () => {
  it("holds the title and the close control in one row, title first", () => {
    const mounted = mountPoint("header");
    const handle = dialogFor(mounted.root, "header-dialog", "Close");
    const panel = handle.element.querySelector<HTMLElement>(`.${VISUAL_CLASSES.dialog}`);
    expect(panel).not.toBeNull();
    const header = panel?.querySelector<HTMLElement>(`.${VISUAL_CLASSES.dialogHeader}`) ?? null;
    expect(header).not.toBeNull();
    // It is the panel's own first child, not something nested in the body.
    expect(header?.parentElement).toBe(panel);
    expect(panel?.firstElementChild).toBe(header);
    // Title first, then the close control: the accessible name leads, the tap target follows it.
    expect(Array.from(header?.children ?? []).map((node) => node.className)).toEqual([
      VISUAL_CLASSES.dialogTitle,
      VISUAL_CLASSES.dialogClose,
    ]);
    const close = header?.querySelector<HTMLButtonElement>(`.${VISUAL_CLASSES.dialogClose}`) ?? null;
    expect(close?.parentElement).toBe(header);
    expect(close?.getAttribute("aria-label")).toBe("Close");
    // Still named by its title, and still exactly one close control in the dialog.
    const title = header?.querySelector<HTMLElement>(`.${VISUAL_CLASSES.dialogTitle}`);
    expect(panel?.getAttribute("aria-labelledby")).toBe(title?.id);
    expect(panel?.querySelectorAll(`.${VISUAL_CLASSES.dialogClose}`)).toHaveLength(1);
    // And nothing else claims a row: the dialog's own children are the header, the optional intro and
    // the body -- never a second row holding only a close control.
    const direct = Array.from(panel?.children ?? []).map((node) => node.className);
    expect(direct[0]).toContain(VISUAL_CLASSES.dialogHeader);
    expect(direct).not.toContain(VISUAL_CLASSES.dialogClose);
    expect(direct.filter((name) => name.includes(VISUAL_CLASSES.dialogClose))).toEqual([]);
    handle.destroy();
  });

  it("keeps the close control first in the tab order and reachable by keyboard", () => {
    const mounted = mountPoint("header-tab");
    const handle = dialogFor(mounted.root, "header-tab-dialog", "Stäng");
    handle.show({ title: "Titel", body: document.createElement("p") });
    const close = handle.element.querySelector<HTMLButtonElement>(`.${VISUAL_CLASSES.dialogClose}`);
    expect(close?.getAttribute("aria-label")).toBe("Stäng");
    // The trap's first stop is the header's own control: nothing sits between the title and it.
    const focusables = Array.from(
      handle.element.querySelectorAll<HTMLElement>("button, [href], input, select, textarea"),
    ).filter((node) => node.tabIndex >= 0);
    expect(focusables[0]).toBe(close);
    handle.destroy();
  });
});
