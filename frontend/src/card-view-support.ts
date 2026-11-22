// The Display and Support sections of the Settings page: the chart's axis, sun and the price unit (a preference of this
// browser), and the support rows (the beta box, the debug download and sending, the notes about a card out of date).

import { type ChartAxisMode } from "./chart";
import { pricePerKwh, priceUnitChoosable, type PriceUnit } from "./format";
import { translate } from "./i18n";
import { onOffEditor, sectionHeading, settingRow, singleEditor, type ChoiceOption } from "./value-editors";
import { sendDebugEditor } from "./send-debug";
import { disconnectPhonesEditor } from "./phones";
import { BetaRows } from "./beta";
import { cardOutdatedNote, supportAction, supportSection, type SupportAction } from "./support-section";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";
import type { PageHelpers, ViewContext } from "./card-view-context";

export interface SupportDeps extends Pick<PageHelpers, "overviewRow" | "openValueEditor"> {
  openAutomations: () => void;
  openCapabilities: () => void;
}

/** The price the Display choice shows each unit with, in the minor unit ("163,1 öre/kWh", "1,63 kr/kWh"). */
const PRICE_UNIT_EXAMPLE = 163.1;

export function createSupportSection(ctx: ViewContext, deps: SupportDeps) {
  const { input, model, doc, idPrefix, isDestroyed } = ctx;
  const { overviewRow, openValueEditor, openAutomations, openCapabilities } = deps;

  let chartAxisChoice: ChartAxisMode = model.chartAxis;

  /**
   * Display: how this device draws the card, not a charger setting. Stored in this browser for every
   * SpotNav card in it, and marked so (as the site's sections say which chargers they apply to). Anyone may
   * change it, administrator or not.
   */
  function displaySectionBody(): HTMLElement {
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "display";
    section.append(sectionHeading(doc, "display", translate(model.language, "settings.section.display")));
    const applies = element(doc, "p", C.siteApplies, translate(model.language, "display.applies"));
    applies.dataset["deviceOnly"] = "true";
    section.append(applies);
    const label = translate(model.language, "display.chartAxis");
    const value = translate(model.language, chartAxisChoice === "from_now" ? "display.chartAxis.fromNow" : "display.chartAxis.day");
    const options: ChoiceOption[] = [
      {
        value: "day",
        label: translate(model.language, "display.chartAxis.day"),
        help: translate(model.language, "display.chartAxis.dayHelp"),
      },
      {
        value: "from_now",
        label: translate(model.language, "display.chartAxis.fromNow"),
        help: translate(model.language, "display.chartAxis.fromNowHelp"),
      },
    ];
    const [row] = settingRow(doc, {
      key: "chart_axis",
      label,
      value,
      onTap: () =>
        openValueEditor(
          label,
          (handlers) =>
            singleEditor(
              doc,
              model.language,
              { options, selected: chartAxisChoice, idPrefix: `${idPrefix}-chart-axis` },
              async (chosen) => {
                chartAxisChoice = chosen === "from_now" ? "from_now" : "day";
                input.onChartAxisChange?.(chartAxisChoice);
                return null;
              },
              handlers,
            ),
          { anyone: true },
        ),
      changeableText: translate(model.language, "settings.row.changeable", { label, value }),
    });
    section.append(row!);
    if (priceUnitChoosable(model.format)) {
      section.append(priceUnitRow());
    }
    if (model.sunOffered) {
      section.append(chartSunRow());
    }
    return section;
  }

  /** The sun this view shows as chosen: the model's, then whatever was chosen here since. */
  let chartSunChoice: boolean = model.sunOn;

  /** Display's "Sol": whether the price chart draws the sun. Offered only where the site has a sun source. */
  function chartSunRow(): HTMLElement {
    const label = translate(model.language, "display.sun");
    const onOff = (on: boolean): string => translate(model.language, on ? "settings.value.on" : "settings.value.off");
    const value = onOff(chartSunChoice);
    const [row] = settingRow(doc, {
      key: "chart_sun",
      label,
      value,
      onTap: () =>
        openValueEditor(
          label,
          (handlers) =>
            onOffEditor(
              doc,
              model.language,
              { intro: translate(model.language, "display.sun.help"), on: chartSunChoice, idPrefix: `${idPrefix}-chart-sun` },
              async (chosen) => {
                chartSunChoice = chosen;
                input.onChartSunChange?.(chosen);
                return null;
              },
              handlers,
            ),
          { anyone: true },
        ),
      changeableText: translate(model.language, "settings.row.changeable", { label, value }),
    });
    return row!;
  }

  /** The price unit this view shows as chosen: the model's, then whatever was chosen here since. */
  let priceUnitChoice: PriceUnit = model.priceUnit;

  /**
   * Display's price unit: the market's minor unit ("öre/kWh", "cent/kWh") or its major one ("kr/kWh",
   * "€/kWh"), named from the market itself. Offered only when the market names both.
   */
  function priceUnitRow(): HTMLElement {
    const minorName = `${model.format.unit}/kWh`;
    const majorName = `${model.format.majorUnit ?? ""}/kWh`;
    const label = translate(model.language, "display.priceUnit");
    const example = (unit: PriceUnit): string =>
      translate(model.language, "display.priceUnit.example", {
        price: pricePerKwh({ ...model.format, priceUnit: unit }, PRICE_UNIT_EXAMPLE),
      });
    const value = priceUnitChoice === "major" ? majorName : minorName;
    const options: ChoiceOption[] = [
      {
        value: "minor",
        label: minorName,
        help: example("minor"),
      },
      {
        value: "major",
        label: majorName,
        help: example("major"),
      },
    ];
    const [row] = settingRow(doc, {
      key: "price_unit",
      label,
      value,
      onTap: () =>
        openValueEditor(
          label,
          (handlers) =>
            singleEditor(
              doc,
              model.language,
              { options, selected: priceUnitChoice, idPrefix: `${idPrefix}-price-unit` },
              async (chosen) => {
                priceUnitChoice = chosen === "major" ? "major" : "minor";
                input.onPriceUnitChange?.(priceUnitChoice);
                return null;
              },
              handlers,
            ),
          { anyone: true },
        ),
      changeableText: translate(model.language, "settings.row.changeable", { label, value }),
    });
    return row!;
  }

  /**
   * Support (`support-section.ts`, the same as in the popover): "About this card" (the capability list, for everyone)
   * and, for administrators only, the "Automations" row (SpotNav's blueprints), the beta tester and hardware test rows,
   * the button that saves the debug bundle, the one that sends it to SpotNav's developer (only from its dialog, with
   * the reader's consent) and "Disconnect all phones".
   */
  function supportSectionBody(): HTMLElement {
    const note = cardOutdatedNote(doc, model.language, cardIsOutdated);
    outdatedNote = note;
    const rows: HTMLElement[] = [];
    const actions: SupportAction[] = [];
    if (input.isAdmin) {
      rows.push(
        overviewRow(
          "automations",
          translate(model.language, "settings.automations.label"),
          translate(model.language, "settings.automations.value"),
          () => openAutomations(),
        ),
      );
      const call = input.onBeta;
      if (call !== undefined) {
        betaRows ??= new BetaRows(call, () => !isDestroyed());
        rows.push(
          ...betaRows.rows({
            doc,
            language: model.language,
            idPrefix,
            row: (key, label, value, onTap) => overviewRow(key, label, value, onTap),
            open: (title, build) => openValueEditor(title, (handlers) => build(() => handlers.onDone())),
          }),
        );
      }
    }
    const L = model.language;
    const about = supportAction(doc, "about", translate(L, "settings.about.open"), () => openCapabilities());
    about.button.dataset["about"] = "open";
    actions.push(about);
    if (input.isAdmin) {
      const download = supportAction(doc, "download_debug", translate(L, "debug.download"), () => {
        if (!debugPending) {
          input.onDownloadDebug?.();
        }
      }, translate(L, "debug.intro"));
      download.button.dataset["downloadDebug"] = "true";
      debugButton = download.button;
      paintDebugButton();
      const send = supportAction(doc, "send_debug", translate(L, "sendDebug.open"), () => openSendDebug());
      send.button.dataset["sendDebug"] = "open";
      actions.push(download, send);
      const disconnect = input.onDisconnectPhones;
      if (disconnect !== undefined) {
        const label = translate(L, "phones.disconnect.open");
        const phones = supportAction(doc, "disconnect_phones", label, () =>
          openValueEditor(label, (handlers) => disconnectPhonesEditor(doc, L, disconnect, handlers)),
        );
        phones.button.dataset["disconnectPhones"] = "open";
        actions.push(phones);
      }
    }
    return supportSection({ doc, language: L, note, rows, actions });
  }

  /** The "Betatestare" and "Hårdvarutest" rows, read once and then from every answer their dialogs read. */
  let betaRows: BetaRows | null = null;

  /** "Send debug info": its dialog, in the value editors' dialog, back to Settings when closed. Administrators only. */
  function openSendDebug(): void {
    const send = input.onSendDebug;
    if (send === undefined) {
      return;
    }
    openValueEditor(translate(model.language, "sendDebug.title"), (handlers) =>
      sendDebugEditor(doc, model.language, idPrefix, async (message, email) => {
        const outcome = await send(message, email);
        if ("ref" in outcome && email !== null) {
          betaRows?.rememberEmail(email);
        }
        return outcome;
      }, handlers, betaRows?.email ?? null),
    );
  }

  let debugButton: HTMLButtonElement | null = null;
  let debugPending = false;
  let outdatedNote: HTMLElement | null = null;
  let cardIsOutdated = false;

  function paintCardOutdated(): void {
    if (outdatedNote !== null) {
      outdatedNote.hidden = !cardIsOutdated;
    }
  }

  function paintDebugButton(): void {
    if (debugButton === null) {
      return;
    }
    debugButton.disabled = debugPending;
    // The row's label says what it does; while the file is made its value says so.
    debugButton.textContent = debugPending ? translate(model.language, "debug.preparing") : "";
  }

  return {
    displaySectionBody,
    supportSectionBody,
    setDebugPending(pending: boolean): void {
      debugPending = pending;
      paintDebugButton();
    },
    setCardOutdated(outdated: boolean): void {
      cardIsOutdated = outdated;
      paintCardOutdated();
    },
  };
}
