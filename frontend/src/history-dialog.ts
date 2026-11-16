// The history dialog of the house and heat cards: opened from the clock in a card's header, it reads one period at a
// time (`spotnav/get_house_history` or `spotnav/get_heat_history`) and shows it with the card's body function
// (`house-history.ts`, `heat-history.ts`). A newer read wins over an older one; a period that cannot be read keeps the
// one shown before, dimmed no more, with the sentence under the tabs. The Detailed switch is remembered per browser.

import { getPeriodHistory, SpotnavApiError, UNSUPPORTED_API_VERSION } from "./api";
import { createDialog, type DialogHandle } from "./dialog";
import { translate, type Language, type TranslationKey } from "./i18n";
import { readDetailed, writeDetailed, type HistoryPeriod, type PeriodHandlers, type PeriodUi } from "./period-history";
import type { HomeAssistantLike } from "./types";

export type HistoryState<A> =
  | { kind: "loading" }
  | { kind: "failed"; sentenceKey: TranslationKey; code: string | null; answer?: A | null }
  | { kind: "ready"; answer: A };

export interface HistoryBodyInput<A> {
  doc: Document;
  language: Language;
  state: HistoryState<A>;
  ui: PeriodUi;
  handlers: PeriodHandlers;
  width: number;
  idPrefix: string;
  today: string;
}

export interface HistoryDialogOptions<A> {
  kind: "house" | "heat";
  doc: Document;
  owner: HTMLElement;
  background: () => HTMLElement | null;
  idPrefix: string;
  hass: () => HomeAssistantLike | null;
  target: () => { site: string } | { charger: string } | null;
  language: () => Language;
  width: () => number;
  decode: (raw: unknown) => { ok: true; value: A } | { ok: false; failure: "unsupported" | "malformed" };
  body: (input: HistoryBodyInput<A>) => HTMLElement;
}

/** The browser's local date, for the pickers until the first answer gives the site's. */
function browserToday(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export class HistoryDialog<A extends { period: HistoryPeriod; date: string }> {
  private readonly options: HistoryDialogOptions<A>;
  private dialog: DialogHandle | null = null;
  private state: HistoryState<A> = { kind: "loading" };
  private ui: PeriodUi;
  private read = 0;

  constructor(options: HistoryDialogOptions<A>) {
    this.options = options;
    this.ui = { period: "month", date: null, detailed: readDetailed(options.kind), pending: false, selected: null };
  }

  private title(): string {
    return translate(this.options.language(), this.options.kind === "house" ? "hist.title.house" : "hist.title.heat");
  }

  private ensure(): DialogHandle {
    if (this.dialog === null) {
      this.dialog = createDialog({
        owner: this.options.owner,
        idPrefix: `${this.options.idPrefix}-history`,
        labels: { close: translate(this.options.language(), "dialog.close") },
        background: this.options.background,
        onClose: () => {
          this.read += 1;
        },
      });
    }
    return this.dialog;
  }

  isOpen(): boolean {
    return this.dialog?.isOpen() ?? false;
  }

  /** Open at the running month (the period last chosen stays chosen while the card lives). */
  open(opener: HTMLElement | null): void {
    this.ui = { ...this.ui, date: null, pending: false, selected: null };
    this.state = { kind: "loading" };
    this.ensure().show({ title: this.title(), body: this.body(), opener });
    void this.load();
  }

  /** Paint again (a new language or width); nothing is read. */
  repaint(): void {
    if (this.isOpen()) {
      this.paint();
    }
  }

  destroy(): void {
    this.read += 1;
    this.dialog?.destroy();
    this.dialog = null;
  }

  private handlers(): PeriodHandlers {
    return {
      onPeriod: (period, date) => {
        this.ui = { ...this.ui, period, date, pending: true, selected: null };
        this.paint();
        void this.load();
      },
      onDetailed: (detailed) => {
        this.ui = { ...this.ui, detailed };
        writeDetailed(this.options.kind, detailed);
        this.paint();
      },
    };
  }

  private body(): HTMLElement {
    return this.options.body({
      doc: this.options.doc,
      language: this.options.language(),
      state: this.state,
      ui: this.ui,
      handlers: this.handlers(),
      width: this.options.width(),
      idPrefix: this.options.idPrefix,
      today: browserToday(),
    });
  }

  private paint(): void {
    const dialog = this.dialog;
    if (dialog === null || !dialog.isOpen()) {
      return;
    }
    const root = dialog.element.getRootNode() as Document | ShadowRoot;
    const focused = (root as Document).activeElement as HTMLElement | null;
    const refocus =
      focused !== null && dialog.element.contains(focused)
        ? focused.dataset["period"] !== undefined
          ? `button[data-period="${focused.dataset["period"]}"]`
          : focused.dataset["step"] !== undefined
            ? `button[data-step="${focused.dataset["step"]}"]`
            : focused.dataset["pick"] !== undefined
              ? "[data-pick]"
              : focused.dataset["detailed"] !== undefined
                ? "[data-detailed]"
                : null
        : null;
    dialog.show({ title: this.title(), body: this.body() });
    if (refocus !== null) {
      const again = dialog.element.querySelector<HTMLElement>(refocus);
      (again !== null && !(again as HTMLButtonElement).disabled ? again : dialog.element.querySelector<HTMLElement>("[data-pick]"))?.focus();
    }
  }

  private async load(): Promise<void> {
    const hass = this.options.hass();
    const target = this.options.target();
    if (hass === null || target === null) {
      return;
    }
    const read = ++this.read;
    const { period, date } = this.ui;
    let next: HistoryState<A>;
    const shown = this.state.kind === "ready" ? this.state.answer : this.state.kind === "failed" ? (this.state.answer ?? null) : null;
    try {
      const decoded = this.options.decode(await getPeriodHistory(hass, this.options.kind, target, period, date));
      next = decoded.ok
        ? { kind: "ready", answer: decoded.value }
        : { kind: "failed", sentenceKey: decoded.failure === "unsupported" ? "hist.unsupported" : "hist.failed", code: null, answer: shown };
    } catch (error) {
      const code = error instanceof SpotnavApiError ? error.code : null;
      const unsupported = code === UNSUPPORTED_API_VERSION || code === "unknown_command";
      next = { kind: "failed", sentenceKey: unsupported ? "hist.unsupported" : "hist.failed", code, answer: shown };
    }
    if (read !== this.read) {
      return;
    }
    this.state = next;
    this.ui = { ...this.ui, pending: false };
    this.paint();
  }
}
