// The year review in a card: one read of `spotnav/get_year_review` per card mount (answered from memory), the band at
// the top of the house card and the charger card while the answer says `banner`, and the Support row while it says
// `row`. A tap on the band opens the story and says it was viewed; its × says it was dismissed; either way the band is
// gone for good for this person (`spotnav/year_review_seen`, kept per Home Assistant user). The row opens the story
// any time in the window. Nothing here asks for support: the story's thanks page does, once.

import { getYearReview, markYearReviewSeen, type SiteTarget } from "./api";
import { element } from "./dom";
import { translate, type Language } from "./i18n";
import type { HomeAssistantLike } from "./types";
import { decodeYearReview, type YearReviewAnswer } from "./year-review-model";
import { YEAR_REVIEW_STYLES, YearReviewStory, YR, type YearReviewStoryInput } from "./year-review-view";

export { YEAR_REVIEW_STYLES };

/** The Support row the settings pages draw: its words and what a tap does. */
export interface YearReviewRow {
  label: string;
  open: (opener: HTMLElement | null) => void;
}

export interface YearReviewHostInput {
  doc: Document;
  /** Where the story's dialog lives (the card's shadow root, apart from what a render replaces). */
  owner: ShadowRoot | HTMLElement;
  background?: () => HTMLElement | null;
  idPrefix: string;
  hass: () => HomeAssistantLike | null;
  target: () => SiteTarget | null;
  language: () => Language;
  /** Whether this reader can reach Settings → Support (the thanks page says the story stays there). */
  supportReachable: () => boolean;
  /** The band or the row changed: the card draws again. */
  onChange: () => void;
  /** The story closed. */
  onClose?: () => void;
  /** For the tests: the story's own options. */
  story?: Partial<Pick<YearReviewStoryInput, "clock" | "share">>;
}

export class YearReviewHost {
  private readonly input: YearReviewHostInput;
  private answer: YearReviewAnswer | null = null;
  private readFor: string | null = null;
  private seq = 0;
  private story: YearReviewStory | null = null;

  constructor(input: YearReviewHostInput) {
    this.input = input;
  }

  /** Read the answer once for the card's target (again only when the target changes). */
  async load(): Promise<void> {
    const hass = this.input.hass();
    const target = this.input.target();
    if (hass === null || target === null) {
      return;
    }
    const key = JSON.stringify(target);
    if (this.readFor === key) {
      return;
    }
    this.readFor = key;
    this.answer = null;
    const seq = ++this.seq;
    let next: YearReviewAnswer | null = null;
    try {
      const decoded = decodeYearReview(await getYearReview(hass, target));
      next = decoded.ok ? decoded.value : null;
    } catch {
      // An older backend (no such command), a site not loaded: no band and no row.
      next = null;
    }
    if (seq !== this.seq) {
      return;
    }
    this.answer = next;
    if (next !== null && (next.banner || next.row)) {
      this.input.onChange();
    }
  }

  /** Forget the answer (a new config): the next `load` reads again. */
  reset(): void {
    this.seq += 1;
    this.readFor = null;
    const had = this.answer !== null;
    this.answer = null;
    this.story?.close();
    if (had) {
      this.input.onChange();
    }
  }

  isOpen(): boolean {
    return this.story?.isOpen() === true;
  }

  destroy(): void {
    this.seq += 1;
    // A load cut short is read again when the card is back.
    if (this.answer === null) {
      this.readFor = null;
    }
    this.story?.destroy();
    this.story = null;
  }

  /** Whether the band shows. */
  get bannerShown(): boolean {
    return this.answer?.banner === true && this.answer.review !== null;
  }

  /** The band for the top of the card, or `null` when it does not show. */
  banner(): HTMLElement | null {
    const answer = this.answer;
    if (answer === null || !answer.banner || answer.review === null) {
      return null;
    }
    const doc = this.input.doc;
    const L = this.input.language();
    const year = String(answer.review.year);
    const band = element(doc, "div", YR.banner);
    band.dataset["yearBanner"] = "";
    const title = translate(L, "yearReview.banner.title");
    const sub = translate(L, "yearReview.banner.sub", { year });
    // The band's words are one button (it opens the story), the cross another: no button inside a button.
    const opener = element(doc, "button", YR.bannerOpen);
    opener.type = "button";
    opener.dataset["yearBannerOpen"] = "";
    const words = element(doc, "span", YR.bannerText);
    words.append(element(doc, "b", undefined, title), element(doc, "small", undefined, sub));
    const chevron = element(doc, "span", undefined, "›");
    chevron.dataset["chevron"] = "";
    chevron.setAttribute("aria-hidden", "true");
    opener.append(rocket(doc), words, chevron);
    opener.addEventListener("click", () => this.open(opener));
    const close = element(doc, "button", YR.bannerClose, "×");
    close.type = "button";
    close.dataset["yearBannerClose"] = "";
    close.setAttribute("aria-label", translate(L, "yearReview.banner.close"));
    close.title = translate(L, "yearReview.banner.close");
    close.addEventListener("click", () => this.dismiss());
    band.append(opener, close);
    return band;
  }

  /** The Support row, or `null` outside the window. */
  row(): YearReviewRow | null {
    const answer = this.answer;
    if (answer === null || !answer.row || answer.review === null) {
      return null;
    }
    return {
      label: translate(this.input.language(), "yearReview.row", { year: String(answer.review.year) }),
      open: (opener) => this.open(opener),
    };
  }

  /** Open the story; the first opening while the band shows says it was viewed (the band goes for good). */
  open(opener: HTMLElement | null = null): void {
    const answer = this.answer;
    if (answer === null || answer.review === null) {
      return;
    }
    if (answer.banner) {
      this.answer = { ...answer, banner: false };
      this.tell("viewed", answer);
      this.input.onChange();
    }
    this.ensureStory().open(answer.review, opener?.isConnected === true ? opener : null);
  }

  private dismiss(): void {
    const answer = this.answer;
    if (answer === null || !answer.banner) {
      return;
    }
    this.answer = { ...answer, banner: false };
    this.tell("dismissed", answer);
    this.input.onChange();
  }

  private tell(action: "viewed" | "dismissed", answer: YearReviewAnswer): void {
    const hass = this.input.hass();
    if (hass === null || answer.review === null) {
      return;
    }
    // Not kept on a failure: the band stays away on this page, and the backend's answer decides the next load.
    void markYearReviewSeen(hass, answer.site_id, answer.review.year, action).catch(() => undefined);
  }

  private ensureStory(): YearReviewStory {
    if (this.story === null) {
      this.story = new YearReviewStory({
        doc: this.input.doc,
        owner: this.input.owner,
        ...(this.input.background === undefined ? {} : { background: this.input.background }),
        idPrefix: this.input.idPrefix,
        language: this.input.language,
        supportReachable: this.input.supportReachable(),
        ...(this.input.onClose === undefined ? {} : { onClose: this.input.onClose }),
        ...(this.input.story ?? {}),
      });
    }
    return this.story;
  }
}

/** The band's rocket (the cover's, small). */
function rocket(doc: Document): SVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const svg = doc.createElementNS(ns, "svg") as SVGElement;
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "22");
  svg.setAttribute("height", "22");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const body = doc.createElementNS(ns, "path");
  body.setAttribute("d", "M12 2 C17 6 17 13 15 18 H9 C7 13 7 6 12 2Z");
  body.setAttribute("fill", "#fff");
  const window = doc.createElementNS(ns, "circle");
  window.setAttribute("cx", "12");
  window.setAttribute("cy", "9");
  window.setAttribute("r", "2.2");
  window.setAttribute("fill", "var(--spotnav-energy-house)");
  const flame = doc.createElementNS(ns, "path");
  flame.setAttribute("d", "M10 18 Q12 23 14 18Z");
  flame.setAttribute("fill", "#ffd23f");
  svg.append(body, window, flame);
  return svg;
}
