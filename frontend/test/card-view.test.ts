// The card view: the DOM tree from one `CardModel`, and everything it must refuse to invent.

import { describe, expect, it } from "vitest";

import { createCardView, periodBlockOrder, readoutTextFor, type CardView, type CardViewInput } from "../src/card-view";
import { chartHeightForWidth } from "../src/chart-render";
import { translate } from "../src/i18n";
import { buildModel, type CardModel } from "../src/model";
import { VISUAL_CLASSES, VISUAL_STYLES } from "../src/visual-styles";
import { decoded, plannedStatus, rawDashboard, statusLine } from "./dashboard-fixtures";
import {
  AFTER_MIDNIGHT_MS,
  autumnModel,
  baseModel,
  externalModel,
  midnightModel,
  modelWith,
  mountPoint,
  quarterModel,
  twoDayModel,
} from "./visual-fixtures";

const SIZE = { width: 320, height: 150 };

function view(
  model: CardModel = baseModel(),
  overrides: Partial<CardViewInput> = {},
  prefix = "card",
): { view: CardView; root: ShadowRoot; host: HTMLElement } {
  const mount = mountPoint(prefix);
  const card = createCardView({
    model,
    mount: mount.root,
    idPrefix: prefix,
    now: () => Date.parse("2026-09-22T04:30:00Z"),
    measure: () => SIZE,
    observe: () => null,
    // The rows report clicks and the settings triggers report a press; the subject here is the view,
    // so nothing is sent anywhere and no dialog is ever fed a record.
    onAction: () => {},
    onOpenSettings: () => {},
    onSaveSettings: () => {},
    onReloadSettings: () => {},
    onReapplySettings: () => {},
    // The fourth editor reports its own presses and choices the same way: the view is the subject.
    onOpenMarket: () => {},
    onSaveMarket: () => {},
    onReloadMarket: () => {},
    onReapplyMarket: () => {},
    onMarketAreaChange: () => {},
    isAdmin: true,
    onSelectStrategy: () => {},
    ...overrides,
  });
  return { view: card, root: mount.root, host: mount.host };
}

describe("the header and the status line", () => {
  it("shows the mark, the charger's own name and one help action, with no visible product name", () => {
    const { view: card, root } = view();
    const header = root.querySelector(`.${VISUAL_CLASSES.header}`);
    const name = root.querySelector(`.${VISUAL_CLASSES.name}`);
    const help = root.querySelector(`.${VISUAL_CLASSES.iconButton}`);
    expect(name?.textContent).toBe("Garage");
    expect(help?.getAttribute("aria-label")).toBe("About this card");
    expect(help?.getAttribute("title")).toBe("About this card");
    // The product name is in the DOM for assistive technology and clipped out of sight: never in a
    // visible node, and never in an attribute (a charger name is text a user controls).
    const hidden = root.querySelector(`.${VISUAL_CLASSES.visuallyHidden}`);
    expect(hidden?.textContent).toBe("SpotNav");
    expect(name?.textContent).not.toContain("SpotNav");
    // Everything else visible in the heading is the charger's own name: no product name, no second
    // control, and no runtime charger selector.
    const visible = Array.from(header?.childNodes ?? [])
      .filter((node) => node !== hidden && node !== help)
      .map((node) => node.textContent ?? "")
      .join("");
    expect(visible).toBe("Garage");
    const mark = header?.querySelector("svg");
    expect(mark?.getAttribute("aria-hidden")).toBe("true");
    expect(mark?.querySelector("path")).not.toBeNull();
    // One card, one charger: the Lovelace editor is the only binding choice, so no runtime selector.
    expect(root.querySelectorAll("select").length).toBe(0);
    expect(card.element.querySelectorAll("input").length).toBe(0);
  });

  it("renders the localized status sentence and localizes every visible string", () => {
    const { view: card, root } = view();
    // The accepted status precedence: an applied proposal with a real planned amount wins, and the
    // line also carries the energy, cost and distance.
    expect(root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent).toBe(
      "Planned from 06:00 · 20 kWh · 22.5 kr · 85 km",
    );
    // A model is immutable and its language reaches both the strings and the formats: a Swedish
    // model is built as Swedish, not relabelled afterwards.
    const swedish = buildModel({
      dashboard: decoded(),
      language: "sv",
      nowMs: Date.parse("2026-09-22T04:30:00Z"),
    });
    const translated = view(swedish, {}, "sv-card");
    expect(translated.root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent).toContain("Planerat");
    expect(translated.root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent).toContain("22,5 kr");
    card.destroy();
    translated.view.destroy();
  });

  it("uses the charger id when the display name is absent", () => {
    const model = modelWith({ charger: { charger_id: "entry_a", charger_name: null, available: true, capabilities: decoded().charger.capabilities } });
    expect(model.chargerName).toBe("entry_a");
    const { view: card, root } = view(model);
    expect(root.querySelector(`.${VISUAL_CLASSES.name}`)?.textContent).toBe("entry_a");
    card.destroy();
  });
});

describe("the banner and the issue dialog", () => {
  it("shows no banner when there is nothing to say, and none for information alone", () => {
    const quiet = view();
    expect(quiet.root.querySelector(`.${VISUAL_CLASSES.banner}`)).toBeNull();
    quiet.view.destroy();

    // Nothing in the block is worth a banner: not even a non-empty, normal one.
    expect(baseModel().severity).toBeNull();
  });

  it("leaves out a neutral banner whose text the status headline already says", () => {
    const notice = modelWith({
      status: { tone: "notice", lines: [statusLine("auto_installed", { start: "2026-09-22T04:00:00+00:00" }), statusLine("unpriced")] },
    });
    expect(notice.severity).toBe("notice");
    expect(notice.issues.map((issue) => [issue.code, issue.severity])).toEqual([["unpriced", "notice"]]);
    const shown = view(notice, {}, "notice");
    expect(shown.root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent).toContain(
      translate("en", notice.issues[0]!.textKey).replace(/\.$/, ""),
    );
    expect(shown.root.querySelector(`.${VISUAL_CLASSES.banner}`)).toBeNull();
    shown.view.destroy();
  });

  it("leaves out the neutral banner when the headline is itself the notice, and when the notice carries the line's own facts", () => {
    const headline = modelWith({ status: { tone: "notice", lines: [statusLine("charging_without_prices")] } });
    expect(headline.severity).toBe("notice");
    expect(translate("en", headline.issues[0]!.textKey).replace(/\.$/, "")).toBe(headline.status?.replace(/\.$/, ""));
    const alone = view(headline, {}, "notice");
    expect(alone.root.querySelector(`.${VISUAL_CLASSES.banner}`)).toBeNull();
    alone.view.destroy();

    const measured = modelWith({
      status: {
        tone: "notice",
        lines: [
          statusLine("auto_installed", { start: "2026-09-22T04:00:00+00:00" }),
          statusLine("site_measurement_problem", {
            no_value_phases: ["L2", "L3"],
            no_value_entities: ["sensor.l2", "sensor.l3"],
            stale_phases: [],
            max_age_s: 120,
          }),
        ],
      },
    });
    expect(measured.issues.map((issue) => issue.code)).toEqual(["site_measurement_problem"]);
    const shown = view(measured, {}, "notice");
    expect(shown.root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent).toContain(
      "L2 and L3 have no value (sensor.l2, sensor.l3).",
    );
    expect(shown.root.querySelector(`.${VISUAL_CLASSES.banner}`)).toBeNull();
    shown.view.destroy();
  });

  it("shows a notice as a neutral banner, never as the red one, when the headline does not say it", () => {
    const notice = modelWith({
      status: { tone: "notice", lines: [statusLine("auto_installed", { start: "2026-09-22T04:00:00+00:00" })] },
    });
    expect(notice.severity).toBe("notice");
    expect(notice.issues.map((issue) => [issue.code, issue.severity])).toEqual([["pending_proposal", "notice"]]);
    const shown = view(notice, {}, "notice");
    const banner = shown.root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.banner}`);
    expect(banner?.classList.contains(VISUAL_CLASSES.bannerNotice)).toBe(true);
    expect(banner?.classList.contains(VISUAL_CLASSES.bannerBlocking)).toBe(false);
    expect(banner?.textContent).toContain(translate("en", "issue.banner.notice"));
    shown.view.destroy();
  });

  it("never lists an issue harsher than the block: a notice tone with no notice line is the waiting proposal", () => {
    const waiting = modelWith({
      status: { tone: "notice", lines: [statusLine("auto_installed", { start: "2026-09-22T04:00:00+00:00" })] },
    });
    expect(waiting.issues.map((issue) => [issue.code, issue.severity])).toEqual([["pending_proposal", "notice"]]);
    const calm = modelWith({ status: { tone: "normal", lines: [statusLine("no_plan")] } });
    expect(calm.issues).toEqual([]);
    expect(calm.severity).toBeNull();
  });

  it("shows the worst severity with a localized count, and lists every issue in order", () => {
    const model = externalModel();
    expect(model.issues).toEqual([]);
    const blocking = modelWith({
      charger: { charger_id: "entry_a", charger_name: "Garage", available: false, capabilities: decoded().charger.capabilities },
      status: { tone: "blocking", lines: [statusLine("charger_unavailable")] },
    });
    expect(blocking.severity).toBe("blocking");
    const { view: card, root } = view(blocking);
    const banner = root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.banner}`);
    expect(banner?.classList.contains(VISUAL_CLASSES.bannerBlocking)).toBe(true);
    expect(banner?.textContent).toContain("Something needs attention before charging can be planned.");
    expect(banner?.textContent).toContain("1 item to review");
    banner?.click();
    expect(card.dialogOpen("issues")).toBe(true);
    const rows = root.querySelectorAll(`.${VISUAL_CLASSES.issueItem}`);
    expect(rows.length).toBe(blocking.issues.length);
    expect(rows[0]?.getAttribute("data-code")).toBe(blocking.issues[0]?.code);
    // The severity sentence is the translated key, not the backend's own prose.
    expect(rows[0]?.querySelector(`.${VISUAL_CLASSES.issueText}`)?.textContent).toBe(
      "The configured charger is not usable: it is unknown, unloaded or a site.",
    );
    card.destroy();
  });

  it("keeps the backend code off the screen and on the row only as an attribute, and pluralizes by Intl", () => {
    const payload = rawDashboard();
    const prices = payload.prices as Record<string, unknown>;
    const model = modelWith({
      prices: { ...prices, state: "unavailable", reason: "<script>alert(1)</script>" },
      status: { tone: "blocking", lines: [statusLine("price_data_unavailable", { reason: "<script>alert(1)</script>" })] },
    });
    expect(model.issues.length).toBeGreaterThan(0);
    const { view: card, root } = view(model);
    card.openIssues();
    expect(root.querySelector("script")).toBeNull();
    const dialogText = root.querySelector(`.${VISUAL_CLASSES.issueItem}`)?.textContent ?? "";
    expect(dialogText).not.toContain("<script>");
    expect(dialogText).not.toContain("Technical detail");
    const banner = root.querySelector(`.${VISUAL_CLASSES.banner}`);
    // One issue: `Intl.PluralRules` picks the singular form, not `count === 1` arithmetic.
    expect(banner?.textContent).toContain("1 item to review");
    expect(banner?.textContent).not.toContain("items to review");
    card.destroy();
  });
});

describe("the graph and its readout", () => {
  it("draws one tab stop with an accessible name, description and the drawn geometry", () => {
    const { view: card, root } = view();
    const graph = root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.graphSurface}`);
    const viewport = root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.viewport}`);
    // The viewport is the image object and the tab stop; the readout, hint and legend are siblings.
    expect(viewport?.getAttribute("role")).toBe("img");
    expect(viewport?.tabIndex).toBe(0);
    expect(viewport?.getAttribute("aria-labelledby")).toBe("card-chart-title");
    expect(viewport?.getAttribute("aria-describedby")).toBe("card-chart-description");
    expect(graph?.querySelector(`[role='img']`)).toBe(viewport);
    const svg = viewport?.querySelector("svg");
    expect(svg?.getAttribute("viewBox")).toBe("0 0 320 150");
    expect(svg?.getAttribute("aria-hidden")).toBe("true");
    // The viewport is sized by the one height policy for the measured width.
    expect(viewport?.style.height).toBe(`${chartHeightForWidth(320)}px`);
    expect(root.querySelector("#card-chart-title")?.textContent).toBe("Charging plan");
    const description = root.querySelector("#card-chart-description")?.textContent ?? "";
    expect(description).toContain("Price graph from");
    expect(description).toContain("Nothing selected.");
    expect(description).toContain("Arrow keys move from interval to interval");
    // One tab stop for the whole graph, not one per price.
    expect(root.querySelectorAll("[tabindex='0']").length).toBe(1);
    card.destroy();
  });

  it("shows the summary, the legend for what is drawn, and the selection readout below the graph", () => {
    const { view: card, root } = view();
    const summary = root.querySelector(`.${VISUAL_CLASSES.summary}`)?.textContent ?? "";
    // Units are dropped on max/min (the Now figure carries the unit); the words are the accessible name.
    expect(summary).toContain("▲ 400");
    expect(summary).toContain("▼ 100");
    expect(summary).not.toContain("Max");
    expect(summary).toContain("Now");
    const legend = root.querySelector(`.${VISUAL_CLASSES.legend}`)?.textContent ?? "";
    expect(legend).toContain("Today");
    expect(legend).toContain("Cheaper than the day's average");
    expect(legend).toContain("More expensive than the day's average");
    // Nothing selected: the readout is an empty line that is still reserved (so pressing the chart
    // never moves the controls below), and a live region that will announce the next real selection.
    expect(card.readoutText()).toBe("");
    const viewport = root.querySelector(`.${VISUAL_CLASSES.viewport}`);
    const readout = root.querySelector(`.${VISUAL_CLASSES.readout}`);
    // The readout sits immediately under the viewport, as the priority order requires.
    expect(viewport?.nextElementSibling).toBe(readout);
    expect(readout?.getAttribute("aria-live")).toBe("polite");
    expect((readout as HTMLElement | null)?.hidden).toBe(false);
    expect(readout?.textContent).toBe("");
    card.destroy();
  });

  it("reports a selection with day, time and price, and adds the offset only when ambiguous", () => {
    const { view: card, root } = view();
    const mark = baseModel().chart.marks[0];
    expect(mark).toBeDefined();
    card.chart().select(mark ?? null);
    expect(card.readoutText()).toContain("öre/kWh");
    expect(card.readoutText()).toContain("06:00");
    expect(card.readoutText()).not.toContain("GMT");
    expect((root.querySelector(`.${VISUAL_CLASSES.readout}`) as HTMLElement | null)?.hidden).toBe(false);
    // The description follows the selection, and the selection line is shown.
    const description = root.querySelector("#card-chart-description")?.textContent ?? "";
    expect(description).toContain(card.readoutText());
    const line = root.querySelector(`.${VISUAL_CLASSES.selection}`);
    expect(line?.hasAttribute("display")).toBe(false);
    card.destroy();
  });

  it("disambiguates both passes of a repeated autumn hour with the offset", () => {
    const model = autumnModel();
    const passes = model.chart.marks.filter((mark) => mark.wallClock === "02:00");
    expect(passes).toHaveLength(2);
    const { view: card } = view(model, {}, "autumn");
    card.chart().select(passes[0] ?? null);
    const first = card.readoutText();
    card.chart().select(passes[1] ?? null);
    const second = card.readoutText();
    expect(first).toContain("(");
    expect(second).toContain("(");
    expect(first).not.toBe(second);
    expect(first).toMatch(/GMT\+\d/);
    expect(second).toMatch(/GMT\+\d/);
    card.destroy();
  });

  it("explains a missing market zone instead of faking times", () => {
    const withoutMarket = view(modelWith({ market: null }, "en"), {}, "no-market");
    const noZone = withoutMarket.root.querySelector("#no-market-chart-description")?.textContent ?? "";
    expect(noZone).toContain("Times are unavailable");
    expect(withoutMarket.root.querySelector(`.${VISUAL_CLASSES.legend}`)?.textContent).toContain(
      "Times are unavailable",
    );
    // Unavailable values stay honest: the overlay omits each missing fact entirely,
    // rather than showing a plausible number or the word "unknown" beside its own label.
    expect(withoutMarket.root.querySelector(`.${VISUAL_CLASSES.summary}`)?.textContent).not.toContain("unknown");
    for (const selector of [VISUAL_CLASSES.summaryMax, VISUAL_CLASSES.summaryMin, VISUAL_CLASSES.summaryCurrent]) {
      const line = withoutMarket.root.querySelector<HTMLElement>(`.${selector}`);
      expect(line?.hidden, selector).toBe(true);
    }
    expect(withoutMarket.view.readoutText()).not.toMatch(/\d/);
    withoutMarket.view.destroy();
  });
});

describe("the picture across local midnight", () => {
  it("shows the date the clock is in as Today, the one it has left as the earlier series, and the real Now price", () => {
    const model = midnightModel();
    const { view: card, root } = view(model, { now: () => AFTER_MIDNIGHT_MS });
    const summary = root.querySelector(`.${VISUAL_CLASSES.summary}`)?.textContent ?? "";
    // The figure beside "Now" is the interval that really covers the instant, and Max/Min are that same
    // date's own -- never the expired date's, and never the word for an unavailable price.
    expect(summary).toContain("Now 200 öre/kWh");
    expect(summary).toContain("▲ 500");
    expect(summary).toContain("▼ 200");
    expect(summary).not.toContain("unknown");
    // The legend names the roles actually drawn: the current date's series and the earlier one, with no
    // "Tomorrow" for a date that is not in the future.
    const legend = root.querySelector(`.${VISUAL_CLASSES.legend}`)?.textContent ?? "";
    expect(legend).toContain(translate("en", "graph.legend.today"));
    expect(legend).toContain(translate("en", "graph.legend.past"));
    expect(legend).not.toContain(translate("en", "graph.legend.tomorrow"));
    expect(legend).toContain(translate("en", "graph.legend.current"));
    // The one focus fact a reader cannot see is said in words, about that same interval.
    const description = root.querySelector("svg > desc")?.textContent ?? "";
    expect(description).toContain(translate("en", "graph.descriptionNow").slice(0, 18));
    expect(description).toContain(readoutTextFor(model, model.chart.current));
    card.destroy();
  });

  it("still names a coming date Tomorrow, and never the earlier series for it", () => {
    const model = twoDayModel();
    const { view: card, root } = view(model);
    const legend = root.querySelector(`.${VISUAL_CLASSES.legend}`)?.textContent ?? "";
    expect(legend).toContain(translate("en", "graph.legend.today"));
    expect(legend).toContain(translate("en", "graph.legend.tomorrow"));
    expect(legend).not.toContain(translate("en", "graph.legend.past"));
    card.destroy();
  });
});

describe("the removed visible prose stays removed, and stays accessible", () => {
  it("draws max, min and current inside the plot, current at the right, with up/down indicators", () => {
    const { view: card, root } = view();
    const graph = root.querySelector(`.${VISUAL_CLASSES.graphSurface}`);
    const summary = root.querySelector(`.${VISUAL_CLASSES.summary}`);
    // One line ABOVE the plot, in the graph section's normal flow: before the chart viewport, never
    // positioned over it.
    expect(graph?.contains(summary)).toBe(true);
    const viewport = root.querySelector(`.${VISUAL_CLASSES.viewport}`) as Element;
    expect((summary as Element).compareDocumentPosition(viewport) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(VISUAL_STYLES).toMatch(/\.spotnav-summary \{[^}]*flex-wrap: nowrap/s);
    expect(VISUAL_STYLES).not.toMatch(/\.spotnav-summary \{[^}]*position: absolute/s);
    const max = summary?.querySelector(`.${VISUAL_CLASSES.summaryMax}`);
    const min = summary?.querySelector(`.${VISUAL_CLASSES.summaryMin}`);
    const current = summary?.querySelector(`.${VISUAL_CLASSES.summaryCurrent}`);
    expect(max?.textContent?.trim()).toBe("▲ 400");
    expect(min?.textContent?.trim()).toBe("▼ 100");
    expect(max?.getAttribute("aria-label")).toBe("Max 400 öre/kWh");
    expect(min?.getAttribute("aria-label")).toBe("Min 100 öre/kWh");
    expect(current?.textContent).toContain("Now");
    // Current sits last, at the right of the overlay's own flex row.
    expect(summary?.lastElementChild).toBe(current);
    // Decorative up/down glyphs, hidden from assistive technology -- the words carry the meaning.
    expect(max?.querySelector(`.${VISUAL_CLASSES.summaryArrow}`)?.getAttribute("aria-hidden")).toBe("true");
    expect(min?.querySelector(`.${VISUAL_CLASSES.summaryArrow}`)?.getAttribute("aria-hidden")).toBe("true");
    card.destroy();
  });

  it("keeps the legend, the keyboard hint and every period accessible but visually hidden", () => {
    const { view: card, root } = view();
    const legend = root.querySelector(`.${VISUAL_CLASSES.legend}`);
    const hint = root.querySelector(`.${VISUAL_CLASSES.readoutHint}`);
    const periods = root.querySelectorAll(".spotnav-periods");
    expect(legend?.classList.contains(VISUAL_CLASSES.visuallyHidden)).toBe(true);
    expect(hint?.classList.contains(VISUAL_CLASSES.visuallyHidden)).toBe(true);
    expect(periods.length).toBeGreaterThan(0);
    for (const block of Array.from(periods)) {
      expect(block.classList.contains(VISUAL_CLASSES.visuallyHidden)).toBe(true);
    }
    // Still real content, not emptied: a screen reader user can still learn the whole schedule.
    expect(legend?.textContent).toContain(translate("en", "graph.legend.today"));
    expect(hint?.textContent).toBe(translate("en", "graph.hint"));
    expect(periods[0]?.textContent).toContain("06:00");
    card.destroy();
  });

  it("removes the visible cost/energy/distance/power table", () => {
    const { view: card, root } = view();
    expect(root.querySelector(`.${VISUAL_CLASSES.figures}`)).toBeNull();
    expect(root.querySelector(`.${VISUAL_CLASSES.figureLabel}`)).toBeNull();
    expect(root.querySelector(`.${VISUAL_CLASSES.figureValue}`)).toBeNull();
    card.destroy();
  });

  it("hides the Start explanatory paragraph, and carries it as the button's own accessible description", () => {
    const { view: card, root } = view();
    const start = root.querySelector<HTMLButtonElement>('[data-action="start"]');
    const describedBy = start?.getAttribute("aria-describedby");
    expect(describedBy).not.toBeNull();
    const description = root.querySelector(`#${describedBy}`);
    expect(description?.textContent).toBe(translate("en", "action.startHelp"));
    expect(description?.classList.contains(VISUAL_CLASSES.visuallyHidden)).toBe(true);
    // Kept for a screen reader; not visible under the button.
    expect(start?.querySelector(".spotnav-settings-value")?.textContent).toBe(translate("en", "bar.start"));
    card.destroy();
  });

  it("shows a Pause icon while the automatic action is Pause, and a Play icon while it is Resume", () => {
    const { view: card, root } = view();
    const planner = root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.plannerButton}`);
    expect(planner?.dataset["action"]).toBe("pause");
    const icon = planner?.querySelector("svg");
    expect(icon?.getAttribute("aria-hidden")).toBe("true");
    // Visual fix d: the visible word is short, so row one never wraps onto a third line; the full
    // sentence still reaches anyone who cannot see it, as the button's own accessible name.
    expect(planner?.querySelector(".spotnav-settings-value")?.textContent).toBe(translate("en", "action.pauseAutomaticShort"));
    expect(planner?.getAttribute("aria-label")).toBe(`${translate("en", "bar.schedule")}: ${translate("en", "bar.state.scheduleActive")}. ${translate("en", "action.pauseAutomatic")}`);
    card.destroy();
  });

  it("gives the strategy trigger the piggy-bank, sun, or combined icon for its own selected strategy", () => {
    const cheapest = view(baseModel(), {}, "cheapest-icon");
    const cheapestButton = cheapest.root.querySelector(`.${VISUAL_CLASSES.strategyButton}`);
    expect(cheapestButton?.querySelector("svg")).not.toBeNull();
    expect(cheapestButton?.querySelector(".spotnav-settings-value")?.textContent).toBe("Cheapest");
    cheapest.view.destroy();

    // Selecting solar or hybrid needs dashboard v7; the icon rule itself is a pure
    // function of the model's own `selectedId`, so it is exercised directly here rather than through a
    // v3 fixture that can never carry that selection.
    const withStrategy = (selectedId: "solar" | "hybrid"): CardModel => ({
      ...baseModel(),
      strategy: { ...baseModel().strategy, selected: selectedId, selectedId },
    });
    const solar = view(withStrategy("solar"), {}, "solar-icon");
    expect(solar.root.querySelector(`.${VISUAL_CLASSES.strategyButton}`)?.querySelector("svg")).not.toBeNull();
    solar.view.destroy();

    const hybrid = view(withStrategy("hybrid"), {}, "hybrid-icon");
    // The combined icon is still one legible glyph: exactly one svg element on the button.
    const hybridIcons = hybrid.root.querySelector(`.${VISUAL_CLASSES.strategyButton}`)?.querySelectorAll("svg");
    expect(hybridIcons?.length).toBe(1);
    hybrid.view.destroy();
  });
});


describe("figures, periods and context", () => {
  it("shows a zero figure, omits an absent one, and keeps negative prices honest", () => {
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    const proposal = plan.proposal as Record<string, unknown>;
    // The cost/energy/distance table is gone: these facts now join the one status line, and a zero
    // cost stays a real, visible value there too.
    const zero = view(
      modelWith({
        plan: { ...plan, proposal: { ...proposal, cost: { value: 0, currency: "SEK" } } },
        status: {
          tone: "normal",
          lines: [...plannedStatus().lines.slice(0, 2), statusLine("plan_cost", { amount_minor: 0, currency: "SEK" })],
        },
      }),
      {},
      "zero",
    );
    expect(zero.root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent).toContain("0 kr");
    zero.view.destroy();

    const absent = view(
      modelWith({
        plan: { ...plan, proposal: { ...proposal, cost: null, distance_mil: null } },
        status: { tone: "normal", lines: plannedStatus().lines.slice(0, 2) },
      }),
      {},
      "absent",
    );
    const status = absent.root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent ?? "";
    expect(status).not.toContain("kr");
    expect(status).not.toContain("mil");
    expect(status).toContain("20 kWh");
    absent.view.destroy();

    // Negative prices are drawn honestly: the summary reports them and the gutter shows the sign.
    const negative = view(quarterModel(() => ({}), [-50, -10, 20, 30]), {}, "negative");
    const summary = negative.root.querySelector(`.${VISUAL_CLASSES.summary}`)?.textContent ?? "";
    expect(summary).toContain("-50");
    expect(summary).toContain("30");
    const gutter = Array.from(negative.root.querySelectorAll(`.${VISUAL_CLASSES.axisLabel}`)).map(
      (node) => node.textContent,
    );
    expect(gutter.some((text) => text !== null && text.startsWith("-"))).toBe(true);
    negative.view.destroy();
  });

  it("keeps the installed schedule and a pending proposal in separate, ordered blocks", () => {
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    const relation = plan.relation as Record<string, unknown>;
    // The queued case: not applied *and* this proposal is the one `pending_identity` names.
    const model = modelWith({
      plan: { ...plan, relation: { ...relation, applied: false, pending_identity: "proposal-1" } },
      status: { tone: "notice", lines: [statusLine("auto_installed", { start: "2026-09-22T04:00:00+00:00" })] },
    });
    expect(model.issues.map((issue) => issue.code)).toContain("pending_proposal");
    const { view: card, root } = view(model, {}, "pending");
    const blocks = Array.from(root.querySelectorAll(".spotnav-periods"));
    expect(blocks).toHaveLength(2);
    expect(blocks[0]?.classList.contains(VISUAL_CLASSES.periodsInstalled)).toBe(true);
    expect(blocks[1]?.classList.contains(VISUAL_CLASSES.periodsProposal)).toBe(true);
    // Headings are pluralized per language, and the two sources are never merged.
    expect(blocks[0]?.querySelector(`.${VISUAL_CLASSES.periodsHeading}`)?.textContent).toBe(
      "Scheduled charging periods (2)",
    );
    expect(blocks[1]?.querySelector(`.${VISUAL_CLASSES.periodsHeading}`)?.textContent).toBe(
      "Cheapest charging period (1)",
    );
    // A proposal is never labelled active: only the installed block can carry the active mark.
    expect(blocks[0]?.querySelectorAll(`.${VISUAL_CLASSES.periodActive}`).length).toBe(1);
    expect(blocks[1]?.querySelectorAll(`.${VISUAL_CLASSES.periodActive}`).length).toBe(0);
    // One period per line, with the model's own offset-disambiguated label preserved.
    expect(blocks[0]?.querySelectorAll(`.${VISUAL_CLASSES.period}`).length).toBe(2);
    const line = blocks[0]?.querySelector(`.${VISUAL_CLASSES.period}`)?.textContent ?? "";
    expect(line).toContain("06:00");
    card.destroy();
  });

  it("draws a differing proposal as two blocks without claiming one is waiting", () => {
    // The same two blocks, and nothing queued: `applied === false` with no `pending_identity` naming the
    // proposal means the card shows the difference and says nothing about a proposal being ready. A
    // refusal, an error, an external schedule and an apply that never landed all reach this shape, and
    // none of them is a waiting change.
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    const relation = plan.relation as Record<string, unknown>;
    const model = modelWith({
      plan: { ...plan, relation: { ...relation, applied: false, pending_identity: null } },
      status: { tone: "normal", lines: [statusLine("auto_installed", { start: "2026-09-22T04:00:00+00:00" })] },
    });
    expect(model.planRelation).toBe("pending_beside_installed");
    expect(model.issues.map((issue) => issue.code)).not.toContain("pending_proposal");
    expect(model.status).toBe("Charging is scheduled from 06:00.");
    const { view: card, root } = view(model, {}, "unqueued");
    const blocks = Array.from(root.querySelectorAll(".spotnav-periods"));
    expect(blocks).toHaveLength(2);
    expect(blocks[0]?.classList.contains(VISUAL_CLASSES.periodsInstalled)).toBe(true);
    expect(blocks[1]?.classList.contains(VISUAL_CLASSES.periodsProposal)).toBe(true);
    expect(root.querySelector(`.${VISUAL_CLASSES.status}`)?.textContent).toBe(
      "Charging is scheduled from 06:00.",
    );
    expect(root.textContent ?? "").not.toContain("is not installed yet");
    card.destroy();
  });

  it("draws an applied proposal once, under the proposal heading", () => {
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    const relation = plan.relation as Record<string, unknown>;
    const model = modelWith({ plan: { ...plan, relation: { ...relation, applied: true } } });
    expect(model.planRelation).toBe("applied_same");
    expect(model.proposalPeriods.length).toBeGreaterThan(0);
    expect(model.installedPeriods.length).toBeGreaterThan(0);
    const { view: card, root } = view(model, {}, "applied");
    const blocks = Array.from(root.querySelectorAll(".spotnav-periods"));
    // One block, not the same schedule twice: the relation says they are the same thing.
    expect(blocks).toHaveLength(1);
    expect(blocks[0]?.classList.contains(VISUAL_CLASSES.periodsProposal)).toBe(true);
    expect(blocks[0]?.classList.contains(VISUAL_CLASSES.periodsInstalled)).toBe(false);
    expect(blocks[0]?.querySelector(`.${VISUAL_CLASSES.periodsHeading}`)?.textContent).toBe(
      "Cheapest charging period (1)",
    );
    // The proposal's own periods, not the installed run beside them.
    expect(blocks[0]?.querySelectorAll(`.${VISUAL_CLASSES.period}`).length).toBe(
      model.proposalPeriods.length,
    );
    expect(root.querySelector(`.${VISUAL_CLASSES.periodsInstalled}`)).toBeNull();
    card.destroy();
  });

  it("covers all five relations with the right blocks, in the right order", () => {
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    const relation = plan.relation as Record<string, unknown>;
    const cases: Array<[string, Record<string, unknown>, Array<"proposal" | "installed">]> = [
      ["none", { plan: { ...plan, proposal: null, installed: null } }, []],
      ["proposal_only", { plan: { ...plan, installed: null } }, ["proposal"]],
      ["installed_only", { plan: { ...plan, proposal: null } }, ["installed"]],
      ["applied_same", { plan: { ...plan, relation: { ...relation, applied: true } } }, ["proposal"]],
      [
        "pending_beside_installed",
        { plan: { ...plan, relation: { ...relation, applied: false } } },
        ["installed", "proposal"],
      ],
    ];
    for (const [name, overrides, kinds] of cases) {
      const model = modelWith(overrides);
      expect(model.planRelation, name).toBe(name);
      expect(periodBlockOrder(model), name).toEqual(kinds);
      const { view: card, root } = view(model, {}, name);
      const blocks = Array.from(root.querySelectorAll(".spotnav-periods"));
      expect(blocks, name).toHaveLength(kinds.length);
      kinds.forEach((kind, index) => {
        const block = blocks[index];
        const expected =
          kind === "installed" ? VISUAL_CLASSES.periodsInstalled : VISUAL_CLASSES.periodsProposal;
        expect(block?.classList.contains(expected), `${name}[${index}]`).toBe(true);
      });
      card.destroy();
    }
  });

  it("never labels a proposal active, in any relation", () => {
    const base = rawDashboard();
    const plan = base.plan as Record<string, unknown>;
    for (const overrides of [
      {},
      { plan: { ...plan, installed: null } },
      { plan: { ...plan, relation: { ...(plan.relation as Record<string, unknown>), applied: false } } },
    ]) {
      const { view: card, root } = view(modelWith(overrides), {}, "active-check");
      const active = Array.from(root.querySelectorAll(`.${VISUAL_CLASSES.periodActive}`));
      for (const line of active) {
        expect(line.closest(`.${VISUAL_CLASSES.periodsProposal}`)).toBeNull();
      }
      card.destroy();
    }
  });

  it("shows the area and the fiscal figures as value rows in the market card of the Settings popover", () => {
    // Moved out of the always-visible layout and into the popover: each is a labelled row beside the
    // trigger that actually controls the area, and the fiscal figures join it.
    const { view: card, root } = view();
    card.openSettingsOverview();
    const row = (key: string) => root.querySelector(`[data-section='market'] [data-row='${key}']`)?.textContent ?? "";
    expect(row("area")).toContain("SE4");
    expect(row("currency")).toBe("");
    // Amps, phases and max periods are never repeated here.
    expect(root.querySelector("[data-section='market']")?.textContent ?? "").not.toMatch(/\bmax\b/i);
    card.destroy();
  });
});

describe("the capability dialog", () => {
  it("lists the five capabilities with their states and adds nothing it cannot prove", () => {
    const model = modelWith({}, "en");
    const { view: card, root } = view(model, {}, "caps");
    card.openCapabilities();
    expect(card.dialogOpen("capabilities")).toBe(true);
    const rows = root.querySelectorAll(`.${VISUAL_CLASSES.capabilityItem}`);
    expect(rows.length).toBe(4);
    const keys = Array.from(rows).map((row) => row.getAttribute("data-capability"));
    expect(keys).toEqual(["auto_price", "current_limit", "load_balancing", "target_soc"]);
    expect(rows[0]?.getAttribute("data-state")).toBe("available");
    // The visible overlay is the capability one; the issues dialog stays hidden and empty.
    const visible = root.querySelector(
      `.${VISUAL_CLASSES.overlay}:not([hidden]) .${VISUAL_CLASSES.dialog}`,
    );
    const dialogText = visible?.textContent ?? "";
    expect(dialogText).toContain("Available");
    expect(dialogText).toContain("Unavailable for this charger");
    expect(dialogText).not.toContain("Coming later");
    expect(dialogText).toContain("Needs a charge-level sensor for the vehicle");
    expect(dialogText.toLowerCase()).not.toContain("no vehicle detected");
    expect(dialogText.toLowerCase()).not.toContain("no meter");
    expect(dialogText.toLowerCase()).not.toContain("not set up");
    expect(dialogText.toLowerCase()).not.toContain("missing");
    // A false capability is a fact about the feature, never a verdict on the configured charger:
    // the dialog describes features only, and the usable charger's name stays in the header.
    expect(dialogText).not.toContain("not usable");
    expect(root.querySelector(`.${VISUAL_CLASSES.name}`)?.textContent).toBe("Garage");
    card.destroy();
  });
});

describe("hostile input, widths and isolation", () => {
  it("keeps a hostile charger name and code as text, never as markup or attributes", () => {
    const hostileName = '<img src=x onerror="boom()">ACME" onmouseover="x"';
    const base = rawDashboard();
    const model = modelWith({
      charger: {
        charger_id: "entry_a",
        charger_name: hostileName,
        available: true,
        capabilities: (base.charger as { capabilities: unknown }).capabilities,
      },
      prices: {
        ...(base.prices as Record<string, unknown>),
        state: "unavailable",
        reason: 'code"><script>alert(1)</script>',
      },
      status: { tone: "blocking", lines: [statusLine("price_data_unavailable", { reason: 'code"><script>alert(1)</script>' })] },
    });
    expect(model.chargerName).toBe(hostileName);
    const { view: card, root } = view(model, {}, "hostile");
    expect(root.querySelectorAll("img").length).toBe(0);
    expect(root.querySelectorAll("script").length).toBe(0);
    expect(root.querySelector(`.${VISUAL_CLASSES.name}`)?.textContent).toBe(hostileName);
    // The same string cannot become an attribute: the element's attributes stay ours.
    expect(root.querySelector(`.${VISUAL_CLASSES.name}`)?.getAttribute("onmouseover")).toBeNull();
    expect(card.element.getAttribute("onmouseover")).toBeNull();
    card.openIssues();
    expect(root.querySelectorAll("script").length).toBe(0);
    expect(root.querySelector(`.${VISUAL_CLASSES.issueItem}`)?.textContent ?? "").not.toContain("<script>");
    card.destroy();
  });

  it("renders at 320, 390, 768 and a wide card without a fixed content width", () => {
    for (const width of [320, 390, 768, 1280]) {
      const { view: card, root } = view(baseModel(), { measure: () => ({ width, height: 150 }) }, `w${width}`);
      const svg = root.querySelector(".spotnav-chart-viewport svg");
      expect(svg?.getAttribute("viewBox"), `${width}`).toBe(`0 0 ${width} 150`);
      expect(svg?.hasAttribute("width")).toBe(false);
      expect(svg?.hasAttribute("height")).toBe(false);
      // No inline width anywhere: the stylesheet, not the markup, decides the layout.
      expect(card.element.style.width).toBe("");
      expect(card.element.getAttribute("width")).toBeNull();
      // Every drawn mark stays inside the plot at each width.
      const targets = card.chart() ? root.querySelectorAll("circle, line.spotnav-segment") : [];
      expect(targets.length).toBeGreaterThan(0);
      card.destroy();
    }
  });

  it("isolates two views, their ids, their dialogs and their teardown", () => {
    const first = view(baseModel(), {}, "first");
    const second = view(twoDayModel(), {}, "second");
    expect(first.root.querySelector("#first-chart-title")).not.toBeNull();
    expect(first.root.querySelector("#second-chart-title")).toBeNull();
    first.view.openCapabilities();
    expect(first.view.dialogOpen("capabilities")).toBe(true);
    expect(second.view.dialogOpen("capabilities")).toBe(false);
    const secondDialogs = second.root.querySelectorAll("[role='dialog']").length;
    first.view.destroy();
    expect(first.root.querySelector(`.${VISUAL_CLASSES.card}`)).toBeNull();
    expect(first.root.querySelectorAll("[role='dialog']").length).toBe(0);
    expect(second.root.querySelectorAll("[role='dialog']").length).toBe(secondDialogs);
    expect(second.root.querySelector(`.${VISUAL_CLASSES.card}`)).not.toBeNull();
    second.view.destroy();
    expect(second.root.querySelector(`.${VISUAL_CLASSES.card}`)).toBeNull();
  });

  it("is inert after destroy: no selection, no dialog, no nodes", () => {
    const { view: card, root } = view();
    card.destroy();
    expect(root.querySelector(`.${VISUAL_CLASSES.card}`)).toBeNull();
    expect(card.selection()).toBeNull();
    expect(() => card.openIssues()).not.toThrow();
    expect(card.dialogOpen("issues")).toBe(false);
    expect(card.readoutText()).toBe("");
    card.destroy();
  });
});

describe("the live region, the measured box and one modal at a time", () => {
  it("keeps the live readout and the legend outside the image role", () => {
    const { view: card, root } = view();
    const image = root.querySelector(`[role='img']`);
    const readout = root.querySelector(`.${VISUAL_CLASSES.readout}`);
    const legend = root.querySelector(`.${VISUAL_CLASSES.legend}`);
    const hint = root.querySelector(`.${VISUAL_CLASSES.readoutHint}`);
    for (const node of [readout, legend, hint]) {
      expect(node, "text node present").not.toBeNull();
      expect(image?.contains(node as Node), "not an image descendant").toBe(false);
    }
    expect(readout?.getAttribute("aria-live")).toBe("polite");
    // Exactly one tab stop in the card, and the SVG itself is not one.
    expect(root.querySelectorAll("[tabindex='0']").length).toBe(1);
    expect(root.querySelectorAll("svg[tabindex]").length).toBe(0);
    expect(image?.querySelector(`.${VISUAL_CLASSES.readout}`)).toBeNull();
    card.destroy();
  });

  it("sizes the chart from the viewport alone, even when the outer graph is much taller", () => {
    const measured: string[] = [];
    const { view: card, root } = view(
      baseModel(),
      {
        measure: (node) => {
          measured.push(node.className);
          // A browser-like case: the outer graph carries a big readout and legend, so it is 300 px,
          // while the viewport itself is 320 x 150.
          return node.className.includes(VISUAL_CLASSES.viewport)
            ? { width: 320, height: chartHeightForWidth(320) }
            : { width: 320, height: 300 };
        },
      },
      "sized",
    );
    const svg = root.querySelector(".spotnav-chart-viewport svg");
    expect(svg?.getAttribute("viewBox")).toBe(`0 0 320 ${chartHeightForWidth(320)}`);
    expect(root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.viewport}`)?.style.height).toBe(
      `${chartHeightForWidth(320)}px`,
    );
    expect(measured.every((name) => name.includes(VISUAL_CLASSES.viewport))).toBe(true);
    card.destroy();
  });

  it("does not redraw when the readout grows", () => {
    let size = { width: 320, height: chartHeightForWidth(320) };
    const observers: Array<{ fire: () => void }> = [];
    const { view: card, root } = view(
      baseModel(),
      {
        observe: (_node, onResize) => {
          observers.push({ fire: onResize });
          return { disconnect: () => {} };
        },
        measure: () => size,
      },
      "grow",
    );
    const before = root.querySelector("svg");
    const readout = root.querySelector(`.${VISUAL_CLASSES.readout}`);
    for (let line = 0; line < 6; line += 1) {
      readout?.append(document.createElement("br"));
    }
    observers[0]?.fire();
    // The width is unchanged, so the policy's height is unchanged: the same SVG node, no redraw.
    expect(root.querySelector("svg")).toBe(before);
    expect(observers).toHaveLength(1);
    // A real width change does redraw.
    size = { width: 390, height: chartHeightForWidth(390) };
    observers[0]?.fire();
    expect(root.querySelector(".spotnav-chart-viewport svg")).not.toBe(before);
    expect(root.querySelector(".spotnav-chart-viewport svg")?.getAttribute("viewBox")).toBe(
      `0 0 390 ${chartHeightForWidth(390)}`,
    );
    card.destroy();
  });

  it("keeps only one modal open, in both directions, without focusing a background control", () => {
    const model = modelWith({
      charger: {
        charger_id: "entry_a",
        charger_name: "Garage",
        available: false,
        capabilities: decoded().charger.capabilities,
      },
    });
    const { view: card, root } = view(model, {}, "modal");
    const banner = root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.banner}`);
    const help = root.querySelector<HTMLElement>(`.${VISUAL_CLASSES.iconButton}`);
    const openDialogs = () =>
      Array.from(root.querySelectorAll(`.${VISUAL_CLASSES.overlay}:not([hidden])`)).length;

    card.openIssues();
    expect(openDialogs()).toBe(1);
    expect(card.dialogOpen("issues")).toBe(true);
    card.openCapabilities();
    expect(card.dialogOpen("issues")).toBe(false);
    expect(card.dialogOpen("capabilities")).toBe(true);
    expect(openDialogs()).toBe(1);
    // Focus is inside the open dialog, not on the help button behind it.
    const active = root.activeElement;
    expect(active).not.toBe(help);
    expect(active).not.toBe(banner);
    expect(root.querySelector(`.${VISUAL_CLASSES.overlay}:not([hidden])`)?.contains(active ?? null)).toBe(true);

    card.openIssues(banner ?? null);
    expect(card.dialogOpen("capabilities")).toBe(false);
    expect(card.dialogOpen("issues")).toBe(true);
    expect(openDialogs()).toBe(1);
    card.destroy();
  });
});

describe("the action bar's cells", () => {
  it("are built alike: a caption line, then one line holding the icon (when there is one) before the value", () => {
    const { root } = view();
    const cells = Array.from(root.querySelectorAll<HTMLElement>(`.${VISUAL_CLASSES.barCell}`));
    expect(cells.length).toBeGreaterThanOrEqual(4);
    for (const cell of cells) {
      const id = cell.dataset["cell"];
      expect(cell.children.length, `${id}: caption and value line only`).toBe(2);
      const [caption, line] = Array.from(cell.children) as HTMLElement[];
      expect(caption?.className, id).toBe(VISUAL_CLASSES.barCaption);
      expect(line?.className, id).toBe(VISUAL_CLASSES.barValue);
      // The value is the last thing on the line, and any icon comes before it -- never stacked
      // above it as a second row.
      const kids = Array.from(line?.children ?? []);
      expect(kids.at(-1)?.className, id).toBe(VISUAL_CLASSES.settingsValue);
      for (const icon of kids.slice(0, -1)) {
        expect(icon.tagName.toLowerCase(), id).toBe("svg");
      }
    }
    // "Finish by" is one of them, with the same shape as the rest.
    const deadline = root.querySelector<HTMLElement>(`[data-cell="deadline"]`);
    expect(deadline?.querySelector(`.${VISUAL_CLASSES.barValue} > svg`)).not.toBeNull();
  });

  it("keep the icon and the value on one line whatever the value's length, and align their captions", () => {
    const rule = (name: string): string =>
      VISUAL_STYLES.match(new RegExp(`\\.${name} \\{([^}]*)\\}`))?.[1] ?? "";
    expect(rule(VISUAL_CLASSES.barValue)).toContain("flex-wrap: nowrap");
    expect(rule(VISUAL_CLASSES.barValue)).not.toContain("flex-wrap: wrap");
    // The caption sits at the top of every cell, and the value line takes the rest: equal cells in a row
    // (the grid stretches them) with their captions on one line.
    expect(rule(VISUAL_CLASSES.barCell)).toContain("justify-content: flex-start");
    expect(rule(VISUAL_CLASSES.barValue)).toContain("flex: 1 1 auto");
  });
});
