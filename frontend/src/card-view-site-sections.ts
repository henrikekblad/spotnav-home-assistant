// The site and solar sections of the Settings page, "Make a home of this" and the load-balancing
// switch. The switch's state lives outside the model: an answer repaints its block in place.

import { translate } from "./i18n";
import { type SiteFacts } from "./model";
import type { ActiveControlNotice } from "./site-settings";
import { onOffEditor, sectionHeading } from "./value-editors";
import { solarSectionBody as sharedSolarSection, type SectionHost } from "./price-section";
import { makeHomeEditor } from "./phones";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";
import type { PageHelpers, SettingsPageState, ViewContext } from "./card-view-context";

export interface SiteSectionsDeps extends PageHelpers {
  sectionHost: () => SectionHost;
}

export function createSiteSections(ctx: ViewContext, settingsPage: SettingsPageState, deps: SiteSectionsDeps) {
  const { input, model, doc, idPrefix } = ctx;
  const { overviewRow, rowHelp, openValueEditor, writeValue, sectionHost } = deps;

  let homeOffered = false;
  let makeHomeSlot: HTMLElement | null = null;

  /** "Make a home of this": one button under "This charger has no site", for an administrator, once offered. */
  function paintMakeHome(): void {
    if (makeHomeSlot === null) {
      return;
    }
    makeHomeSlot.replaceChildren();
    const make = input.onMakeHome;
    if (!homeOffered || !input.isAdmin || make === undefined || model.site !== null) {
      return;
    }
    const button = element(doc, "button", `${C.button} ${C.settingsSectionConfigure}`, translate(model.language, "makeHome.open"));
    (button as HTMLButtonElement).type = "button";
    button.dataset["makeHome"] = "open";
    button.addEventListener("click", () =>
      openValueEditor(translate(model.language, "makeHome.open"), (handlers) =>
        makeHomeEditor(doc, model.language, idPrefix, make, handlers),
      ),
    );
    makeHomeSlot.append(button);
  }

  /**
   * The site card: headed by the site's name alone ("Site" only when it has none), marked as applying to
   * every charger on it, with the configured entities in words and the load-balancing switch.
   */
  function siteSectionBody(): HTMLElement {
    const site = model.site;
    const siteSection = element(doc, "section", C.settingsSection);
    siteSection.dataset["section"] = "site";
    settingsPage.siteEntitySlot = null;
    if (site === null) {
      siteSection.append(sectionHeading(doc, "site", translate(model.language, "settings.heading.site")));
      siteSection.append(element(doc, "p", C.muted, translate(model.language, "site.none")));
      makeHomeSlot = element(doc, "div");
      makeHomeSlot.dataset["slot"] = "make-home";
      siteSection.append(makeHomeSlot);
      paintMakeHome();
      return siteSection;
    }
    makeHomeSlot = null;
    siteSection.append(sectionHeading(doc, "site", translate(model.language, "settings.heading.site"), site.name));
    // With one charger the site is that charger's: nothing to say about which chargers it applies to.
    if (site.chargerCount > 1) {
      siteSection.append(element(doc, "p", C.siteApplies, site.appliesToText));
    }
    settingsPage.siteEntitySlot = element(doc, "div");
    settingsPage.siteEntitySlot.dataset["slot"] = "site-entities";
    siteSection.append(settingsPage.siteEntitySlot);
    // Active load balancing: a switch for administrators, read-only state for others. `available` and
    // `enabled` stay separate: enabled-but-unavailable keeps the switch on and says why. Never optimistic:
    // a press leaves the control on the confirmed value, pending until the answer.
    activeSlot = element(doc, "div");
    activeSlot.dataset["slot"] = "active-control";
    siteSection.append(activeSlot);
    paintActiveControl();
    return siteSection;
  }

  /** Solar: the forecast sources (any of them), in their own editor (`price-section.ts`). */
  function solarSectionBody(): HTMLElement | null {
    const site = model.site;
    if (site === null) {
      return null;
    }
    return sharedSolarSection({
      ...sectionHost(),
      facts: {
        chargerCount: site.chargerCount,
        appliesToText: site.appliesToText,
        writable: site.writable,
        choices: site.solarForecastChoices,
      },
      writeForecast: (forecast) => writeValue({ kind: "solar", forecast }),
    });
  }

  // The load-balancing switch's state lives outside `model`: an answer repaints this block in place,
  // since the popover stays open and a full render is deferred while a dialog is open.
  let activeSlot: HTMLElement | null = null;
  let activeOverride: { available: boolean; enabled: boolean; reason: { text: string; code: string | null } | null } | null =
    null;
  let activePending = false;
  let activeNotice: ActiveControlNotice | null = null;

  function paintActiveControl(): void {
    const site = model.site;
    if (activeSlot === null || site === null) {
      return;
    }
    const state = activeOverride ?? {
      available: site.activeControlAvailable,
      enabled: site.activeControlEnabled,
      reason: site.activeControlReason,
    };
    const label = translate(model.language, "site.activeControl.title");
    const stateText = activePending
      ? translate(model.language, "site.activeControl.pending")
      : translate(model.language, state.enabled ? "site.activeControl.on" : "site.activeControl.off");
    // Off may always be chosen; on only when the backend says it is available. Never optimistic: the row shows
    // the confirmed value until an answer says otherwise.
    const changeable = site.writable && site.activeControlWritable && !activePending && (state.available || state.enabled);
    const nodes: HTMLElement[] = [];
    const row = overviewRow(
      "active-control",
      label,
      stateText,
      changeable
        ? () =>
            openValueEditor(label, (handlers) =>
              onOffEditor(
                doc,
                model.language,
                { intro: translate(model.language, "site.activeControl.note"), on: state.enabled, idPrefix },
                async (chosen) => {
                  // The answer arrives through `adoptActiveControl`, painted in this row.
                  input.onSetActiveControl?.(state.enabled, chosen);
                  return null;
                },
                handlers,
              ),
            )
        : undefined,
    );
    row.querySelector<HTMLElement>(`.${C.settingRowValue}`)?.setAttribute("data-role", "active-control-state");
    if (activePending) {
      row.setAttribute("aria-busy", "true");
    }
    nodes.push(row);
    if (state.reason !== null) {
      nodes.push(rowHelp("active-control-reason", state.reason.text));
    } else if (!site.writable) {
      nodes.push(rowHelp("active-control-available", translate(model.language, "site.activeControl.available")));
    }
    nodes.push(rowHelp("active-control", translate(model.language, "site.activeControl.note")));
    if (activeNotice !== null) {
      const notice = element(
        doc,
        "p",
        activeNotice.tone === "warning" ? `${C.activeNotice} ${C.activeNoticeWarning}` : C.activeNotice,
      );
      notice.setAttribute("role", "status");
      for (const line of activeNotice.lines) {
        notice.append(element(doc, "span", "", line));
      }
      if (activeNotice.code !== null) {
        notice.dataset["code"] = activeNotice.code;
      }
      nodes.push(notice);
    }
    activeSlot.replaceChildren(...nodes);
  }

  return {
    siteSectionBody,
    solarSectionBody,
    setActiveControlPending(pending: boolean): void {
      activePending = pending;
      paintActiveControl();
    },
    adoptActiveControl(site: SiteFacts | null, notice: ActiveControlNotice | null): void {
      if (site !== null) {
        activeOverride = {
          available: site.activeControlAvailable,
          enabled: site.activeControlEnabled,
          reason: site.activeControlReason,
        };
      }
      activeNotice = notice;
      activePending = false;
      paintActiveControl();
    },
    setHomeOffered(offered: boolean): void {
      homeOffered = offered;
      paintMakeHome();
    },
  };
}
