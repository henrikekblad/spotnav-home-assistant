// The area and fiscal dialog of the card view: the card owns the record, the catalogue context and the drafts.
// Picking another area rebuilds nothing here; the choice is reported with the values on screen and the card hands
// back the form for that area.

import { takeActions } from "./dialog";
import { element } from "./dom";
import { translate } from "./i18n";
import { marketEditorBody, type MarketEditorForm } from "./market-editor";
import type { MarketFormValues } from "./market";
import { VISUAL_CLASSES as C } from "./visual-styles";
import type { FailureSentence } from "./card-view";
import type { ViewContext } from "./card-view-context";

/** `hass.config.country`, when Home Assistant states one. */
function homeAssistantCountry(hass: unknown): string | null {
  if (typeof hass !== "object" || hass === null) {
    return null;
  }
  const config = (hass as { config?: unknown }).config;
  const country = typeof config === "object" && config !== null ? (config as { country?: unknown }).country : undefined;
  return typeof country === "string" && country.trim() !== "" ? country : null;
}

export function createMarketEditorSection(ctx: ViewContext) {
  const { input, model, doc, idPrefix, isDestroyed, dialogs, settingsGeneral, leaveSettingsChild } = ctx;
  const issuesDialog = dialogs.issues;
  const capabilityDialog = dialogs.capability;
  const pauseDialog = dialogs.pause;
  const strategyDialog = dialogs.strategy;
  const vehicleDialog = dialogs.vehicle;
  const settingsDialog = dialogs.settings;
  const marketDialog = dialogs.market;
  const settingsOverviewDialog = dialogs.settingsOverview;

  // The area/fiscal dialog: the card owns the record, catalogue context and drafts. Picking another area
  // rebuilds nothing here; the choice is reported with the values on screen and the card hands back the
  // form for that area.
  let marketForm: MarketEditorForm | null = null;
  let marketBody: HTMLElement | null = null;
  let marketNotice: FailureSentence | null = null;
  let marketNoticeNode: HTMLParagraphElement | null = null;
  /** The fixed row under the form (the notice, Save and Cancel); `null` while there is no form. */
  let marketFooter: HTMLElement | null = null;
  let marketSaveButton: HTMLButtonElement | null = null;
  let marketReapplyButton: HTMLButtonElement | null = null;
  let marketPending = false;
  let marketBodyReader: (() => MarketFormValues) | null = null;

  function marketTriggerFor(): HTMLElement | null {
    // The area/fiscal editor is reached from the Settings popover: closing it returns focus to the
    // button that opened that surface, the same rule the consumption editor follows.
    return settingsGeneral;
  }

  function applyMarketPending(): void {
    if (marketSaveButton !== null) {
      marketSaveButton.disabled = marketPending;
    }
    if (marketReapplyButton !== null) {
      marketReapplyButton.disabled = marketPending;
    }
  }

  function paintMarketNotice(): void {
    marketNoticeNode?.remove();
    marketNoticeNode = null;
    if (marketBody !== null && marketNotice !== null) {
      if (marketForm === null) {
        marketBody.replaceChildren();
      }
      const paragraph = element(doc, "p", C.settingsNotice, translate(model.language, marketNotice.sentenceKey));
      if (marketNotice.code !== null) {
        paragraph.dataset["code"] = marketNotice.code;
      }
      if (marketForm !== null && marketFooter !== null) {
        marketFooter.prepend(paragraph);
      } else {
        marketBody.append(paragraph);
      }
      marketNoticeNode = paragraph;
    }
    applyMarketPending();
  }

  function renderMarketBody(): void {
    const form = marketForm;
    if (form === null || isDestroyed()) {
      return;
    }
    const built = marketEditorBody(
      doc,
      model.language,
      form,
      {
        onSave: (values) => input.onSaveMarket(values),
        onReload: () => input.onReloadMarket(),
        onReapply: (values) => input.onReapplyMarket(values),
        onCancel: () => leaveSettingsChild(marketDialog, input.onCancelMarket),
        onAreaChange: (areaId, live) => input.onMarketAreaChange(areaId, live),
        ...(input.onFindRegion === undefined ? {} : { onFindRegion: input.onFindRegion }),
      },
      idPrefix,
      homeAssistantCountry(input.hass?.()),
    );
    marketBody = built.body;
    marketBodyReader = built.values;
    marketSaveButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsSave}`);
    marketReapplyButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsReapply}`);
    marketFooter = takeActions(built.body, `${idPrefix}-market`);
    marketDialog.show({
      title: translate(model.language, "market.title"),
      body: built.body,
      opener: marketTriggerFor(),
      actions: marketFooter,
    });
    paintMarketNotice();
  }

  function openMarketEditor(): void {
    if (isDestroyed()) {
      return;
    }
    marketForm = null;
    marketBody = null;
    marketFooter = null;
    marketBodyReader = null;
    marketNotice = null;
    marketNoticeNode = null;
    marketSaveButton = null;
    marketReapplyButton = null;
    marketPending = false;
    // One overlay at a time, and the others do not restore focus on the way out.
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const loading = element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "market.loading"));
    // This paragraph is the dialog's body until a form replaces it, so a failure that arrives while the
    // two reads are still out has a node to be shown in rather than a sentence with nowhere to go.
    marketBody = loading;
    marketFooter = null;
    marketDialog.show({
      title: translate(model.language, "market.title"),
      body: loading,
      opener: marketTriggerFor(),
    });
  }

  function showMarketEditorForm(form: MarketEditorForm): void {
    if (isDestroyed() || !marketDialog.isOpen()) {
      return;
    }
    marketForm = form;
    marketNotice = null;
    marketPending = false;
    renderMarketBody();
  }

  function showMarketEditorConflict(revision: number): void {
    const form = marketForm;
    if (isDestroyed() || form === null) {
      return;
    }
    const values = marketBodyReader === null ? form.values : marketBodyReader();
    // The reader's own area and figures stay exactly as they are; only the base they would be applied
    // to changes, and that base is the server's record the caller kept.
    marketForm = { ...form, values, conflict: revision };
    marketNotice = null;
    marketPending = false;
    renderMarketBody();
  }

  function setMarketEditorNotice(failure: FailureSentence | null): void {
    marketNotice = failure;
    paintMarketNotice();
  }

  function setMarketEditorPending(pending: boolean): void {
    marketPending = pending;
    applyMarketPending();
  }

  function marketEditorOpen(): boolean {
    return marketDialog.isOpen();
  }

  function closeMarketEditor(options: { restoreFocus?: boolean } = {}): void {
    marketDialog.hide(options);
    marketForm = null;
    marketBody = null;
    marketFooter = null;
    marketBodyReader = null;
    marketNotice = null;
    marketNoticeNode = null;
    marketSaveButton = null;
    marketReapplyButton = null;
    marketPending = false;
  }

  return {
    openMarketEditor,
    showMarketEditorForm,
    setMarketEditorNotice,
    showMarketEditorConflict,
    setMarketEditorPending,
    marketEditorOpen,
    closeMarketEditor,
  };
}
