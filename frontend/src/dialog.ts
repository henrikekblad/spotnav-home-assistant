// A small, standards-based, read-only modal.
//
//   * ids come from the caller's deterministic prefix, unique per view;
//   * one close listener, registered at creation and removed by `destroy()`;
//   * the document keydown listener is added in `show()` and removed in `hide()` and `destroy()`;
//   * focus is read from the caller's `activeElement` first (a ShadowRoot tracks its own), then the document's;
//   * a removed opener is guarded by `isConnected`; a second `show()` updates in place;
//   * only the backdrop itself closes; the background gets `inert`, else `aria-hidden`, restored on close.
//
// No Home Assistant frontend internals are imported and no label is written in English.

import { VISUAL_CLASSES } from "./visual-styles";

const FOCUSABLE =
  "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])";

export interface DialogLabels {
  close: string;
}

export interface DialogShowInput {
  title: string;
  body: Node;
  opener?: HTMLElement | null;
  intro?: string | null;
}

export interface DialogHandle {
  element: HTMLElement;
  show(input: DialogShowInput): void;
  hide(options?: { restoreFocus?: boolean }): void;
  isOpen(): boolean;
  destroy(): void;
}

export interface DialogOptions {
  owner: ShadowRoot | HTMLElement;
  idPrefix: string;
  labels: DialogLabels;
  background?: () => HTMLElement | null;
  /**
   * Fired when `hide()` takes the dialog from open to closed, including when a sibling dialog is
   * about to replace it, so a caller that cares about "none open" must re-check after the current
   * synchronous work (see `card-view.ts`'s `notifyDialogsChanged`).
   */
  onClose?: () => void;
  /**
   * Fired just before the dialog closes because the reader dismissed it (Escape, backdrop, close
   * button), never for a caller's own `hide()`; lets a sub-dialog return to the page it came from.
   */
  onDismiss?: () => void;
}

function ownerDocumentOf(owner: ShadowRoot | HTMLElement): Document {
  return owner.ownerDocument;
}

/** Whether this element supports `inert`. A function so the `in` narrowing stays local. */
function hasInert(element: HTMLElement): boolean {
  return "inert" in element;
}

/**
 * What the background element looked like before the dialog hid it: platform support, the
 * element's own previous `inert` and its previous `aria-hidden`, so close restores exactly those.
 */
interface BackgroundState {
  element: HTMLElement;
  supported: boolean;
  inert: boolean;
  ariaHidden: string | null;
}

export function createDialog(options: DialogOptions): DialogHandle {
  const { owner, idPrefix, labels, onClose, onDismiss } = options;
  const doc = ownerDocumentOf(owner);

  const overlay = doc.createElement("div");
  overlay.className = VISUAL_CLASSES.overlay;
  overlay.hidden = true;

  const dialog = doc.createElement("div");
  dialog.className = VISUAL_CLASSES.dialog;
  dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true");
  dialog.tabIndex = -1;

  const titleId = `${idPrefix}-dialog-title`;
  const introId = `${idPrefix}-dialog-intro`;
  const title = doc.createElement("h3");
  title.className = VISUAL_CLASSES.dialogTitle;
  title.id = titleId;
  dialog.setAttribute("aria-labelledby", titleId);

  const intro = doc.createElement("p");
  intro.className = `${VISUAL_CLASSES.muted} ${VISUAL_CLASSES.dialogIntro}`;
  intro.id = introId;
  intro.hidden = true;

  const close = doc.createElement("button");
  close.type = "button";
  close.className = VISUAL_CLASSES.dialogClose;
  close.textContent = "\u00d7";
  close.setAttribute("aria-label", labels.close);

  const body = doc.createElement("div");
  body.className = VISUAL_CLASSES.dialogBody;

  const header = doc.createElement("div");
  header.className = VISUAL_CLASSES.dialogHeader;
  header.append(title, close);

  dialog.append(header, intro, body);
  overlay.append(dialog);
  owner.append(overlay);

  let open = false;
  let destroyed = false;
  let opener: HTMLElement | null = null;
  let hiddenBackground: BackgroundState | null = null;

  function activeElement(): Element | null {
    if (owner instanceof ShadowRoot) {
      return owner.activeElement ?? doc.activeElement;
    }
    return doc.activeElement;
  }

  function focusables(): HTMLElement[] {
    return Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (element) => !element.hasAttribute("disabled") && element.tabIndex >= 0,
    );
  }

  function setBackgroundHidden(hidden: boolean): void {
    if (!hidden) {
      const previous = hiddenBackground;
      hiddenBackground = null;
      if (previous === null) {
        return;
      }
      if (previous.supported) {
        (previous.element as HTMLElement & { inert: boolean }).inert = previous.inert;
      }
      if (previous.ariaHidden === null) {
        previous.element.removeAttribute("aria-hidden");
      } else {
        previous.element.setAttribute("aria-hidden", previous.ariaHidden);
      }
      return;
    }
    const element = options.background?.() ?? null;
    if (element === null || hiddenBackground !== null) {
      return;
    }
    const supported = hasInert(element);
    const previous: BackgroundState = {
      element,
      supported,
      inert: supported ? (element as HTMLElement & { inert: boolean }).inert : false,
      ariaHidden: element.getAttribute("aria-hidden"),
    };
    if (supported) {
      (element as HTMLElement & { inert: boolean }).inert = true;
    } else {
      element.setAttribute("aria-hidden", "true");
    }
    hiddenBackground = previous;
  }

  function onBackdropClick(event: MouseEvent): void {
    if (event.target === overlay) {
      dismiss();
    }
  }

  function onCloseClick(): void {
    dismiss();
  }

  function dismiss(): void {
    if (open && !destroyed) {
      onDismiss?.();
    }
    hide();
  }

  function onKeydown(event: KeyboardEvent): void {
    if (!open) {
      return;
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      dismiss();
      return;
    }
    if (event.key !== "Tab") {
      return;
    }
    const items = focusables();
    if (items.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = items[0] as HTMLElement;
    const last = items[items.length - 1] as HTMLElement;
    const active = activeElement();
    const inside = active !== null && dialog.contains(active) ? (active as HTMLElement) : null;
    if (event.shiftKey) {
      if (inside === null || inside === first) {
        event.preventDefault();
        last.focus();
      }
      return;
    }
    if (inside === null || inside === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /** Close the dialog. `restoreFocus: false` is for switching straight to another modal. */
  function hide(options: { restoreFocus?: boolean } = {}): void {
    if (!open || destroyed) {
      return;
    }
    open = false;
    overlay.hidden = true;
    doc.removeEventListener("keydown", onKeydown, true);
    setBackgroundHidden(false);
    const previous = opener;
    opener = null;
    if (previous !== null && previous.isConnected && options.restoreFocus !== false) {
      previous.focus();
    }
    onClose?.();
  }

  function show(input: DialogShowInput): void {
    if (destroyed) {
      return;
    }
    const wasOpen = open;
    title.textContent = input.title;
    const introText = input.intro ?? null;
    intro.hidden = introText === null;
    intro.textContent = introText ?? "";
    if (introText === null) {
      dialog.removeAttribute("aria-describedby");
    } else {
      dialog.setAttribute("aria-describedby", introId);
    }
    body.replaceChildren(input.body);
    if (!wasOpen) {
      opener = input.opener ?? null;
      overlay.hidden = false;
      open = true;
      setBackgroundHidden(true);
      doc.addEventListener("keydown", onKeydown, true);
      const items = focusables();
      if (items.length > 0) {
        (items[0] as HTMLElement).focus();
      } else {
        dialog.focus();
      }
      return;
    }
    if (input.opener !== undefined && input.opener !== null) {
      opener = input.opener;
    }
  }

  close.addEventListener("click", onCloseClick);
  overlay.addEventListener("click", onBackdropClick);

  return {
    element: overlay,
    show,
    hide,
    isOpen: () => open,
    destroy(): void {
      if (destroyed) {
        return;
      }
      hide();
      destroyed = true;
      doc.removeEventListener("keydown", onKeydown, true);
      close.removeEventListener("click", onCloseClick);
      overlay.removeEventListener("click", onBackdropClick);
      overlay.remove();
    },
  };
}
