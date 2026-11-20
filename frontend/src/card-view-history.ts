// The charge history dialog of the card view: what the card last said, what the reader chose in it (the month, the
// day under the readout), and its repaint on any change. The card owns the request.

import { historyBody, type HistoryPower, type HistoryState, type HistoryUi, type HistoryView } from "./history";
import { translate, type TranslationKey } from "./i18n";
import type { PowerState, PowerUi } from "./power-view";
import type { ViewContext } from "./card-view-context";

export function createHistorySection(ctx: ViewContext) {
  const { input, model, doc, isDestroyed, dialogs, historyButton } = ctx;
  const issuesDialog = dialogs.issues;
  const capabilityDialog = dialogs.capability;
  const pauseDialog = dialogs.pause;
  const strategyDialog = dialogs.strategy;
  const vehicleDialog = dialogs.vehicle;
  const settingsOverviewDialog = dialogs.settingsOverview;
  const historyDialog = dialogs.history;

  // ---- the charge history dialog. The card owns the request; this holds what was last said and
  // what the reader chose in it (the month, the day under the readout), and repaints on any change.
  let historyState: HistoryState = { kind: "loading" };
  const historyUi: HistoryUi = { view: "charges", month: null, pending: false, day: null, exporting: false, notice: null };
  // The Power view: offered where the site measures its import; read when first chosen, then with the month.
  let powerState: PowerState | null = null;
  const powerUi: PowerUi = { day: null, pending: false };

  function historyPower(): HistoryPower {
    return {
      offered: model.powerView,
      state: powerState,
      ui: powerUi,
      handlers: {
        onDay: (day) => {
          powerUi.day = day;
          paintHistory();
          // Into a day: its way back takes the focus; back to the month: the day's bar does.
          const target =
            day === null
              ? null
              : historyDialog.element.querySelector<HTMLElement>("[data-action='power-month']");
          target?.focus();
        },
      },
    };
  }

  function chooseView(view: HistoryView): void {
    historyUi.view = view;
    historyUi.notice = null;
    if (view === "power") {
      powerUi.day = null;
      const shown = powerState?.kind === "ready" ? powerState.answer.month : null;
      if (powerState === null || powerState.kind === "failed" || (historyUi.month !== null && shown !== historyUi.month)) {
        if (powerState?.kind === "ready") {
          powerUi.pending = true;
        } else {
          powerState = { kind: "loading" };
        }
        paintHistory();
        input.onPowerMonth?.(historyUi.month);
        return;
      }
    } else if (historyState.kind === "ready" && historyUi.month !== null && historyState.answer.month !== historyUi.month) {
      historyUi.pending = true;
      paintHistory();
      input.onHistoryMonth?.(historyUi.month);
      return;
    }
    paintHistory();
  }

  function paintHistory(): void {
    if (isDestroyed() || !historyDialog.isOpen()) {
      return;
    }
    const focused = (input.mount instanceof ShadowRoot ? input.mount.activeElement : doc.activeElement) as
      | HTMLElement
      | null;
    const refocus =
      focused !== null && historyDialog.element.contains(focused)
        ? focused.dataset["view"] !== undefined
          ? `button[data-view="${focused.dataset["view"]}"]`
          : focused.dataset["month"] !== undefined
          ? `button[data-month="${focused.dataset["month"]}"]`
          : focused.dataset["action"] === "export"
            ? "[data-action='export']"
            : focused.dataset["monthSelect"] !== undefined
              ? "[data-month-select]"
              : null
        : null;
    historyDialog.show({
      title:
        model.chargerName === null
          ? translate(model.language, "history.title")
          : translate(model.language, "history.titleNamed", { name: model.chargerName }),
      body: historyBody(doc, model.language, historyState, historyUi, {
        onMonth: (month) => {
          historyUi.month = month;
          historyUi.day = null;
          historyUi.notice = null;
          if (historyUi.view === "power") {
            powerUi.day = null;
            powerUi.pending = true;
            paintHistory();
            input.onPowerMonth?.(month);
            return;
          }
          historyUi.pending = true;
          paintHistory();
          input.onHistoryMonth?.(month);
        },
        onExport: () => {
          input.onExportHistory?.();
        },
        onView: chooseView,
      }, model.priceUnit, historyPower()),
    });
    if (refocus !== null) {
      historyDialog.element.querySelector<HTMLElement>(refocus)?.focus();
    }
  }

  function openHistory(): void {
    if (isDestroyed()) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    historyUi.notice = null;
    historyUi.exporting = false;
    historyUi.month = null;
    historyUi.pending = false;
    historyUi.day = null;
    historyUi.view = "charges";
    historyState = { kind: "loading" };
    powerState = null;
    powerUi.day = null;
    powerUi.pending = false;
    historyDialog.show({
      title: translate(model.language, "history.title"),
      body: historyBody(doc, model.language, historyState, historyUi, {
        onMonth: () => undefined,
        onExport: () => undefined,
      }, model.priceUnit),
      opener: historyButton,
    });
    paintHistory();
    input.onOpenHistory?.();
  }

  return {
    openHistory,
    setHistoryState(state: HistoryState): void {
      historyState = state;
      historyUi.pending = false;
      if (state.kind === "ready") {
        historyUi.month = state.answer.month;
      }
      paintHistory();
    },
    setPowerState(state: PowerState): void {
      powerUi.pending = false;
      powerState = state;
      if (state.kind === "ready") {
        historyUi.month = state.answer.month;
        if (powerUi.day !== null && !state.answer.days.some((day) => day.date === powerUi.day)) {
          powerUi.day = null;
        }
      }
      paintHistory();
    },
    setHistoryExport(notice: TranslationKey | null, exporting: boolean): void {
      historyUi.notice = notice;
      historyUi.exporting = exporting;
      paintHistory();
    },
  };
}
