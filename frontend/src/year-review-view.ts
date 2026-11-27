// The year review's story ("Året med SpotNav"): one fact per panel in a full-screen dialog (`dialog.ts`, its focus
// trap, Escape and the background made inert), with the progress bars on top, Back, Close and Next under it, ← and →
// and a swipe between pages, the panel's words read out as they change (`aria-live`). Each panel has its tint from
// the energy palette, its illustration floating above the figure (`year-review-art.ts`), the page's words in one
// block centred in the panel (scrolled from its top where it is taller), the figure counted up and
// fireworks behind the cover and the thanks page (`year-review-fx.ts`). Under reduced motion nothing moves; every
// frame loop stops when the page changes, the story closes or the tab is hidden.
//
// The thanks page carries the one support ask of the whole story (GitHub Sponsors, once, light) and the button that
// saves a picture to share (`year-review-share.ts`).

import { createDialog, type DialogHandle } from "./dialog";
import { element } from "./dom";
import { translate, type Language } from "./i18n";
import { SPONSOR_URL } from "./support-section";
import { ART_PULSE, ART_SPIN, artElement } from "./year-review-art";
import { countUp, Fireworks, reducedMotion, windowClock, type FrameClock } from "./year-review-fx";
import type { YearReview } from "./year-review-model";
import { figureText, storyPages, type StoryPage } from "./year-review-pages";
import { shareReview, type ShareOutcome } from "./year-review-share";

/** A swipe this far across (CSS px), more across than down, turns the page. */
const SWIPE_PX = 40;

export const YR = {
  overlay: "spotnav-yr-overlay",
  dialog: "spotnav-yr-dialog",
  story: "spotnav-yr-story",
  fx: "spotnav-yr-fx",
  art: "spotnav-yr-art",
  bars: "spotnav-yr-bars",
  bar: "spotnav-yr-bar",
  panel: "spotnav-yr-panel",
  content: "spotnav-yr-content",
  eyebrow: "spotnav-yr-eyebrow",
  big: "spotnav-yr-big",
  unit: "spotnav-yr-unit",
  say: "spotnav-yr-say",
  meter: "spotnav-yr-meter",
  chips: "spotnav-yr-chips",
  foot: "spotnav-yr-foot",
  badge: "spotnav-yr-badge",
  nav: "spotnav-yr-nav",
  cta: "spotnav-yr-cta",
  primary: "spotnav-yr-primary",
  ghost: "spotnav-yr-ghost",
  preview: "spotnav-yr-preview",
  banner: "spotnav-yr-banner",
  bannerOpen: "spotnav-yr-banner-open",
  bannerText: "spotnav-yr-banner-text",
  bannerClose: "spotnav-yr-banner-close",
} as const;

const TINTS = ["house", "export", "car", "sun", "battery", "import", "expensive"] as const;

export const YEAR_REVIEW_STYLES = `
  .${YR.overlay} { padding: 16px; }
  .${YR.dialog} {
    max-width: 480px;
    height: min(720px, calc(100vh - 32px));
    height: min(720px, calc(100dvh - 32px));
    max-height: none;
    border-radius: 20px;
    background: #2a2f7a;
    color: #fff;
  }
  .${YR.dialog} > .spotnav-dialog-header { display: none; }
  .${YR.dialog} .spotnav-dialog-scroll { padding: 0; flex: 1 1 auto; overflow: hidden; }
  .${YR.dialog} .spotnav-dialog-body { flex: 1 1 auto; min-height: 0; gap: 0; }
  @media (max-width: 520px) {
    .${YR.overlay} { padding: 0; }
    .${YR.dialog} { max-width: none; height: 100vh; height: 100dvh; border-radius: 0; }
  }
  .${YR.story} {
    --spotnav-yr-tint: var(--spotnav-energy-house);
    position: relative;
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
    color: #fff;
    background-color: var(--spotnav-yr-tint);
    background-image: linear-gradient(150deg, rgba(0, 0, 0, 0) 0%, rgba(0, 0, 0, 0.5) 100%);
    transition: background-color 0.4s ease;
    touch-action: pan-y;
    font-family: inherit;
  }
  ${TINTS.map((tint) => `.${YR.story}[data-tint="${tint}"] { --spotnav-yr-tint: var(--spotnav-energy-${tint}); }`).join("\n  ")}
  .${YR.story}[data-page="cover"] {
    background-image: linear-gradient(150deg, rgba(0, 0, 0, 0) 0%, rgba(16, 18, 70, 0.75) 100%);
  }
  .${YR.fx} { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 0; }
  .${YR.art} {
    flex: none; width: clamp(96px, min(40vw, 24vh), 168px); height: clamp(96px, min(40vw, 24vh), 168px);
    margin: 4px 0 8px; pointer-events: none;
    filter: drop-shadow(0 8px 14px rgba(0, 0, 0, 0.35));
    animation: spotnav-yr-float 5s ease-in-out infinite;
  }
  .${YR.story}[data-page="thanks"] .${YR.art} { width: clamp(72px, min(26vw, 14vh), 104px); height: clamp(72px, min(26vw, 14vh), 104px); margin-bottom: 0; }
  .${YR.art} svg { width: 100%; height: 100%; display: block; }
  @keyframes spotnav-yr-float { 0%, 100% { transform: translateY(0) rotate(0); } 50% { transform: translateY(-7px) rotate(-2deg); } }
  .${ART_SPIN} { transform-origin: 50% 50%; animation: spotnav-yr-spin 14s linear infinite; }
  @keyframes spotnav-yr-spin { to { transform: rotate(360deg); } }
  .${ART_PULSE} { animation: spotnav-yr-pulse 2.4s ease-in-out infinite; }
  @keyframes spotnav-yr-pulse { 0%, 100% { opacity: 0.55; } 50% { opacity: 1; } }
  .${YR.bars} { position: relative; z-index: 1; display: flex; gap: 4px; padding: calc(12px + env(safe-area-inset-top, 0px)) 14px 0; }
  .${YR.bar} { flex: 1; height: 3px; border-radius: 2px; background: rgba(255, 255, 255, 0.3); }
  .${YR.bar}[data-on] { background: rgba(255, 255, 255, 0.95); }
  .${YR.panel} {
    position: relative; z-index: 1; flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden;
    display: flex; flex-direction: column; padding: 14px 24px 16px;
  }
  .${YR.content} { margin: auto 0; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .${YR.eyebrow} { font-size: 0.85rem; letter-spacing: 0.09em; text-transform: uppercase; opacity: 0.85; font-weight: 600; margin: 0; }
  .${YR.big} {
    margin: 0; font-size: clamp(2.9rem, 13vw, 4.4rem); font-weight: 700; line-height: 1.02;
    font-variant-numeric: tabular-nums; text-wrap: balance; overflow-wrap: break-word; text-shadow: 0 3px 12px rgba(0, 0, 0, 0.28);
  }
  .${YR.big}[data-words] { font-size: clamp(2.2rem, 9.5vw, 3.2rem); overflow-wrap: break-word; hyphens: auto; }
  .${YR.unit} { font-size: 0.42em; font-weight: 500; opacity: 0.9; }
  .${YR.say} { margin: 0; font-size: 1.22rem; line-height: 1.42; max-width: 30em; text-wrap: pretty; overflow-wrap: break-word; hyphens: auto; }
  .${YR.story}[data-page="thanks"] .${YR.say} { font-size: 1.06rem; line-height: 1.45; }
  .${YR.foot} { margin: 0; font-size: 0.85rem; opacity: 0.85; line-height: 1.4; }
  .${YR.meter} { height: 14px; border-radius: 999px; background: rgba(255, 255, 255, 0.25); overflow: hidden; }
  .${YR.meter} > span { display: block; height: 100%; background: rgba(255, 255, 255, 0.95); border-radius: 999px; }
  .${YR.chips} { display: flex; gap: 10px; flex-wrap: wrap; margin: 0; padding: 0; list-style: none; }
  .${YR.chips} > li { background: rgba(255, 255, 255, 0.16); border-radius: 10px; padding: 7px 11px; font-size: 0.98rem; line-height: 1.3; font-variant-numeric: tabular-nums; }
  .${YR.badge} { align-self: flex-start; background: rgba(0, 0, 0, 0.22); border-radius: 999px; padding: 4px 10px; font-size: 0.78rem; }
  .${YR.nav} {
    position: relative; z-index: 1; display: flex; justify-content: space-between; align-items: center; gap: 8px;
    padding: 0 14px calc(14px + env(safe-area-inset-bottom, 0px));
  }
  .${YR.nav} button, .${YR.cta} a, .${YR.cta} button {
    font: inherit; border: 0; cursor: pointer; color: #fff; min-height: 40px;
  }
  .${YR.nav} button {
    display: inline-flex; align-items: center; justify-content: center; box-sizing: border-box; height: 40px;
    margin: 0; padding: 0 16px; font-size: 0.95rem; line-height: 1; border-radius: 999px; background: rgba(255, 255, 255, 0.2);
    vertical-align: middle;
  }
  .${YR.nav} button > span { display: block; line-height: 1; text-box: trim-both cap alphabetic; }
  .${YR.nav} button:disabled { opacity: 0.35; cursor: default; }
  .${YR.nav} button:focus-visible, .${YR.cta} a:focus-visible, .${YR.cta} button:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .${YR.cta} { display: flex; flex-direction: column; gap: 8px; }
  .${YR.cta} a, .${YR.cta} button { display: block; text-align: center; border-radius: 12px; padding: 10px 14px; font-weight: 600; text-decoration: none; box-sizing: border-box; }
  .${YR.primary} { background: #fff; color: #1b2430 !important; }
  .${YR.ghost} { background: rgba(255, 255, 255, 0.18); }
  .${YR.ghost}:disabled { opacity: 0.6; cursor: default; }
  .${YR.preview} { display: block; width: 100%; max-width: 220px; height: auto; align-self: center; border-radius: 12px; box-shadow: 0 6px 22px rgba(0, 0, 0, 0.25); }
  .${YR.banner} {
    display: flex; align-items: center; gap: 12px; margin: 0 0 12px; padding: 12px 14px; border-radius: 14px; color: #fff;
    background: linear-gradient(120deg, var(--spotnav-energy-house), var(--spotnav-energy-car));
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.2);
    box-sizing: border-box;
  }
  .${YR.bannerOpen} {
    display: flex; align-items: center; gap: 12px; flex: 1; min-width: 0; min-height: 40px; padding: 0;
    background: none; border: 0; color: inherit; font: inherit; text-align: left; cursor: pointer;
  }
  .${YR.bannerText} { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
  .${YR.bannerText} > b { font-size: 1rem; }
  .${YR.bannerText} > small { opacity: 0.92; font-size: 0.82rem; }
  .${YR.banner} [data-chevron] { flex: none; font-size: 1.4rem; line-height: 1; }
  .${YR.bannerClose} {
    flex: none; display: inline-flex; align-items: center; justify-content: center; min-width: 32px; min-height: 32px; padding: 0;
    background: rgba(255, 255, 255, 0.2); border: 0; color: #fff; border-radius: 999px; font: inherit; font-size: 1rem; cursor: pointer;
  }
  .${YR.bannerOpen}:focus-visible, .${YR.bannerClose}:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) {
    .${YR.story} { transition: none; }
    .${YR.art}, .${ART_SPIN}, .${ART_PULSE} { animation: none; }
  }
`;

export interface YearReviewStoryInput {
  doc: Document;
  owner: ShadowRoot | HTMLElement;
  background?: () => HTMLElement | null;
  idPrefix: string;
  language: () => Language;
  /** Whether this reader can open the story again from Settings → Support (the thanks page says so). */
  supportReachable: boolean;
  /** The frame clock (the tests' own); the window's otherwise. */
  clock?: FrameClock | null;
  /** The story closed (by the reader or the card). */
  onClose?: () => void;
  /** How the picture is shared or saved (the tests replace it). */
  share?: (review: YearReview, language: Language, doc: Document, fontFamily: string) => Promise<ShareOutcome>;
}

/** The story in its dialog: `open(review)`, `close()`, `destroy()`. */
export class YearReviewStory {
  private readonly input: YearReviewStoryInput;
  private dialog: DialogHandle | null = null;
  private review: YearReview | null = null;
  private pages: StoryPage[] = [];
  private index = 0;
  private story: HTMLElement | null = null;
  private fireworks: Fireworks | null = null;
  private stopCount: () => void = () => {};
  private swipeFrom: { x: number; y: number } | null = null;
  private preview: HTMLImageElement | null = null;
  private shareState: "idle" | "busy" | "failed" = "idle";

  constructor(input: YearReviewStoryInput) {
    this.input = input;
  }

  isOpen(): boolean {
    return this.dialog?.isOpen() === true;
  }

  /** The page shown (its index), for the tests. */
  get page(): number {
    return this.index;
  }

  get pageIds(): string[] {
    return this.pages.map((entry) => entry.id);
  }

  /** Whether a frame loop is running: the fireworks. */
  get animating(): boolean {
    return this.fireworks?.running === true;
  }

  open(review: YearReview, opener: HTMLElement | null = null): void {
    this.review = review;
    this.pages = storyPages(this.input.language(), review, { supportReachable: this.input.supportReachable });
    this.index = 0;
    this.preview = null;
    this.shareState = "idle";
    const dialog = this.ensureDialog();
    const story = this.build();
    dialog.show({ title: translate(this.input.language(), "yearReview.row", { year: String(review.year) }), body: story, opener });
    this.input.doc.addEventListener("visibilitychange", this.onVisibility);
    this.paint(false);
    // Next is what the reader wants first (Back is disabled on the cover).
    story.querySelector<HTMLButtonElement>("[data-nav='next']")?.focus();
  }

  close(): void {
    this.dialog?.hide();
  }

  destroy(): void {
    this.stopMotion();
    this.input.doc.removeEventListener("visibilitychange", this.onVisibility);
    this.dialog?.destroy();
    this.dialog = null;
  }

  /** Turn to another page (clamped); the motion of the one left stops. */
  go(next: number): void {
    const clamped = Math.max(0, Math.min(this.pages.length - 1, next));
    if (clamped === this.index || this.story === null) {
      return;
    }
    this.index = clamped;
    this.paint(true);
  }

  private reduced(): boolean {
    return reducedMotion(this.input.doc.defaultView);
  }

  private clock(): FrameClock | null {
    if (this.input.clock !== undefined) {
      return this.input.clock;
    }
    const view = this.input.doc.defaultView;
    return view !== null && typeof view.requestAnimationFrame === "function" ? windowClock(view) : null;
  }

  private ensureDialog(): DialogHandle {
    if (this.dialog === null) {
      const dialog = createDialog({
        owner: this.input.owner,
        idPrefix: `${this.input.idPrefix}-yr`,
        labels: { close: translate(this.input.language(), "yearReview.nav.close") },
        ...(this.input.background === undefined ? {} : { background: this.input.background }),
        onClose: () => {
          this.stopMotion();
          this.input.doc.removeEventListener("visibilitychange", this.onVisibility);
          this.input.onClose?.();
        },
      });
      dialog.element.classList.add(YR.overlay);
      const panel = dialog.element.firstElementChild as HTMLElement | null;
      panel?.classList.add(YR.dialog);
      // The header's cross is hidden: the story's own Close is in its row, and the hidden one is never focused.
      const cross = dialog.element.querySelector<HTMLButtonElement>(".spotnav-dialog-close");
      if (cross !== null) {
        cross.tabIndex = -1;
      }
      panel?.addEventListener("keydown", (event) => {
        if (event.key === "ArrowRight") {
          event.preventDefault();
          this.go(this.index + 1);
        } else if (event.key === "ArrowLeft") {
          event.preventDefault();
          this.go(this.index - 1);
        }
      });
      this.dialog = dialog;
    }
    return this.dialog;
  }

  private build(): HTMLElement {
    const doc = this.input.doc;
    const L = this.input.language();
    const story = element(doc, "section", YR.story);
    story.setAttribute("aria-roledescription", "carousel");
    const canvas = element(doc, "canvas", YR.fx);
    canvas.setAttribute("aria-hidden", "true");
    const bars = element(doc, "div", YR.bars);
    bars.setAttribute("aria-hidden", "true");
    const panel = element(doc, "div", YR.panel);
    panel.setAttribute("aria-live", "polite");
    panel.dataset["panel"] = "";
    const nav = element(doc, "div", YR.nav);
    const back = navButton(doc, "back", `‹ ${translate(L, "yearReview.nav.back")}`, translate(L, "yearReview.nav.back"));
    const close = navButton(doc, "close", translate(L, "yearReview.nav.close"), translate(L, "yearReview.nav.close"));
    const next = navButton(doc, "next", `${translate(L, "yearReview.nav.next")} ›`, translate(L, "yearReview.nav.next"));
    back.addEventListener("click", () => this.go(this.index - 1));
    next.addEventListener("click", () => this.go(this.index + 1));
    close.addEventListener("click", () => this.close());
    nav.append(back, close, next);
    story.append(canvas, bars, panel, nav);
    story.addEventListener("pointerdown", (event) => {
      this.swipeFrom = { x: event.clientX, y: event.clientY };
    });
    story.addEventListener("pointerup", (event) => {
      const from = this.swipeFrom;
      this.swipeFrom = null;
      if (from === null) {
        return;
      }
      const dx = event.clientX - from.x;
      const dy = event.clientY - from.y;
      if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > Math.abs(dy)) {
        this.go(this.index + (dx < 0 ? 1 : -1));
      }
    });
    story.addEventListener("pointercancel", () => {
      this.swipeFrom = null;
    });
    this.story = story;
    this.fireworks = new Fireworks(canvas, this.clock(), this.reduced());
    return story;
  }

  private stopMotion(): void {
    this.fireworks?.stop();
    this.stopCount();
    this.stopCount = () => {};
  }

  private readonly onVisibility = (): void => {
    if (!this.isOpen()) {
      return;
    }
    if (this.input.doc.visibilityState === "hidden") {
      this.stopMotion();
    } else if (this.pages[this.index]?.fireworks === true) {
      this.fireworks?.start();
    }
  };

  /** Draw the shown page; `focusKept` keeps the focus on the nav button that turned it (or the next one there is). */
  private paint(focusKept: boolean): void {
    const story = this.story;
    const entry = this.pages[this.index];
    if (story === null || entry === undefined) {
      return;
    }
    this.stopMotion();
    const doc = this.input.doc;
    const L = this.input.language();
    story.dataset["page"] = entry.id;
    story.dataset["tint"] = entry.tint;
    const bars = story.querySelector<HTMLElement>(`.${YR.bars}`)!;
    bars.replaceChildren(
      ...this.pages.map((_, k) => {
        const bar = element(doc, "i", YR.bar);
        if (k <= this.index) {
          bar.dataset["on"] = "";
        }
        return bar;
      }),
    );
    const panel = story.querySelector<HTMLElement>(`.${YR.panel}`)!;
    panel.setAttribute("aria-label", translate(L, "yearReview.nav.page", { page: String(this.index + 1), pages: String(this.pages.length) }));
    panel.replaceChildren(...this.panelNodes(entry, L));
    panel.scrollTop = 0;
    const back = story.querySelector<HTMLButtonElement>("[data-nav='back']")!;
    const next = story.querySelector<HTMLButtonElement>("[data-nav='next']")!;
    const active = (this.input.owner as ShadowRoot | Document).activeElement ?? doc.activeElement;
    back.disabled = this.index === 0;
    next.disabled = this.index === this.pages.length - 1;
    if (focusKept && (active === back || active === next) && (active as HTMLButtonElement).disabled) {
      (active === back ? next : back).focus();
    }
    if (entry.fireworks) {
      this.fireworks?.start();
    }
  }

  private panelNodes(entry: StoryPage, L: Language): Node[] {
    const doc = this.input.doc;
    const art = element(doc, "div", YR.art);
    art.setAttribute("aria-hidden", "true");
    const svg = artElement(doc, entry.id);
    if (svg !== null) {
      art.append(svg);
    }
    const nodes: Node[] = [art, element(doc, "p", YR.eyebrow, entry.eyebrow)];
    if (typeof entry.big === "string") {
      const big = element(doc, "p", YR.big, entry.big);
      big.dataset["words"] = "";
      nodes.push(big);
    } else if (entry.big !== null) {
      const figure = entry.big;
      const big = element(doc, "p", YR.big);
      big.dataset["figure"] = figureText(L, figure);
      if (figure.approx) {
        big.append(doc.createTextNode("≈ "));
      }
      const number = doc.createTextNode(figureText(L, figure));
      big.append(number);
      if (figure.unit !== "") {
        big.append(doc.createTextNode(" "), element(doc, "span", YR.unit, figure.unit));
      }
      nodes.push(big);
      this.stopCount = countUp(number, figure.value, (value) => figureText(L, figure, value), this.clock(), this.reduced());
    }
    if (entry.say !== null) {
      nodes.push(element(doc, "p", YR.say, entry.say));
    }
    if (entry.meter !== null) {
      const meter = element(doc, "div", YR.meter);
      meter.setAttribute("aria-hidden", "true");
      const fill = element(doc, "span");
      fill.style.width = `${Math.max(0, Math.min(100, entry.meter))}%`;
      meter.append(fill);
      nodes.push(meter);
    }
    if (entry.chips.length > 0) {
      const chips = element(doc, "ul", YR.chips);
      chips.append(...entry.chips.map((chip) => element(doc, "li", undefined, chip)));
      nodes.push(chips);
    }
    if (entry.badge !== null) {
      nodes.push(element(doc, "span", YR.badge, entry.badge));
    }
    if (entry.id === "thanks") {
      nodes.push(this.thanksActions(L));
    }
    for (const line of entry.foot) {
      nodes.push(element(doc, "p", YR.foot, line));
    }
    // One block, centred in the panel by its margins (from the top where it is taller than the panel).
    const content = element(doc, "div", YR.content);
    content.append(...nodes);
    return [content];
  }

  /** The support ask (once, here only) and the picture to share. */
  private thanksActions(L: Language): HTMLElement {
    const doc = this.input.doc;
    const cta = element(doc, "div", YR.cta);
    const sponsor = element(doc, "a", YR.primary, translate(L, "yearReview.thanks.sponsor"));
    sponsor.href = SPONSOR_URL;
    sponsor.target = "_blank";
    sponsor.rel = "noopener noreferrer";
    sponsor.dataset["sponsor"] = "open";
    const share = element(doc, "button", YR.ghost);
    share.type = "button";
    share.dataset["share"] = this.shareState;
    share.disabled = this.shareState === "busy";
    share.textContent = translate(L, this.shareState === "busy" ? "yearReview.thanks.sharing" : "yearReview.thanks.share");
    share.addEventListener("click", () => void this.sharePicture());
    cta.append(sponsor, share);
    if (this.shareState === "failed") {
      const failed = element(doc, "p", YR.foot, translate(L, "yearReview.thanks.shareFailed"));
      failed.setAttribute("role", "status");
      cta.append(failed);
    }
    if (this.preview !== null) {
      cta.append(this.preview, element(doc, "p", YR.foot, translate(L, "yearReview.thanks.shareHint")));
    }
    return cta;
  }

  private async sharePicture(): Promise<void> {
    const review = this.review;
    if (review === null || this.shareState === "busy") {
      return;
    }
    this.shareState = "busy";
    this.repaintThanks();
    const L = this.input.language();
    let outcome: ShareOutcome;
    try {
      // The picture is drawn in the fonts Home Assistant shows the card in.
      const view = this.input.doc.defaultView;
      const family = this.story === null || view === null ? "" : view.getComputedStyle(this.story).fontFamily;
      outcome = await (this.input.share ?? shareReview)(review, L, this.input.doc, family || "Roboto, system-ui, sans-serif");
    } catch {
      outcome = { kind: "failed" };
    }
    if (outcome.kind === "failed") {
      this.shareState = "failed";
    } else {
      this.shareState = "idle";
      const image = element(this.input.doc, "img", YR.preview);
      image.src = outcome.preview;
      image.alt = translate(L, "yearReview.share.alt");
      image.width = 1080;
      image.height = 1350;
      this.preview = image;
    }
    this.repaintThanks();
  }

  /** The thanks page's actions drawn again in place (the figures and fireworks keep running). */
  private repaintThanks(): void {
    const panel = this.story?.querySelector<HTMLElement>(`.${YR.panel}`);
    const old = panel?.querySelector<HTMLElement>(`.${YR.cta}`);
    if (panel === null || panel === undefined || old === null || old === undefined || this.pages[this.index]?.id !== "thanks") {
      return;
    }
    const focused = (this.input.owner as ShadowRoot | Document).activeElement === old.querySelector("[data-share]");
    const fresh = this.thanksActions(this.input.language());
    old.replaceWith(fresh);
    if (focused) {
      fresh.querySelector<HTMLButtonElement>("[data-share]")?.focus();
    }
  }
}

function navButton(doc: Document, key: string, label: string, accessible: string): HTMLButtonElement {
  const button = element(doc, "button");
  button.append(element(doc, "span", undefined, label));
  button.type = "button";
  button.dataset["nav"] = key;
  button.setAttribute("aria-label", accessible);
  return button;
}
