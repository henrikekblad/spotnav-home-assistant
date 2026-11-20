// The settings editor dialogs of the card view (Plan, Consumption and the other editable settings). The row labels
// itself from the dashboard's settings section (no request); a press is reported and the card hands back the form.
// One overlay serves all of them, and opening one closes the others first.

import { takeActions } from "./dialog";
import { element } from "./dom";
import { translate, type TranslationKey } from "./i18n";
import { renderPlanPreview, type PreviewState } from "./plan-preview";
import { settingsEditorBody, type SettingsEditorForm } from "./settings-editor";
import type { SettingsEditorKind, SettingsFormValues } from "./settings";
import { VISUAL_CLASSES as C } from "./visual-styles";
import type { FailureSentence } from "./card-view";
import type { ViewContext } from "./card-view-context";

export function createSettingsEditorSection(ctx: ViewContext) {
  const { input, model, doc, idPrefix, now, isDestroyed, dialogs, bar, settingsError, fit } = ctx;
  const issuesDialog = dialogs.issues;
  const capabilityDialog = dialogs.capability;
  const pauseDialog = dialogs.pause;
  const strategyDialog = dialogs.strategy;
  const vehicleDialog = dialogs.vehicle;
  const settingsDialog = dialogs.settings;
  const marketDialog = dialogs.market;
  const settingsOverviewDialog = dialogs.settingsOverview;

  // The settings row and its dialog. The row labels itself from the dashboard's settings section (no
  // request); a press is reported and the card hands back the form. One overlay serves all three
  // editors, and opening one closes the others first.
  let settingsKind: SettingsEditorKind | null = null;
  let settingsForm: SettingsEditorForm | null = null;
  let settingsBody: HTMLElement | null = null;
  let settingsNotice: FailureSentence | null = null;
  let settingsNoticeNode: HTMLParagraphElement | null = null;
  /** The fixed row under the form (the notice, Save and Cancel); `null` while there is no form. */
  let settingsFooter: HTMLElement | null = null;
  let settingsSaveButton: HTMLButtonElement | null = null;
  let settingsReapplyButton: HTMLButtonElement | null = null;
  let settingsPending = false;
  let settingsBodyReader: (() => SettingsFormValues) | null = null;
  /** Where the Plan dialog's live preview is painted, while an editable Plan dialog is shown. */
  let settingsPreviewSlot: HTMLElement | null = null;
  /** The car the Plan popover is about, as its body last said ("Charging plan for EV6"); `null` for none. */
  let settingsVehicleName: string | null = null;

  function settingsTitleKey(kind: SettingsEditorKind): TranslationKey {
    return `settings.${kind}.title` as TranslationKey;
  }

  function settingsTitle(kind: SettingsEditorKind): string {
    return kind === "plan" && settingsVehicleName !== null
      ? translate(model.language, "settings.plan.titleFor", { name: settingsVehicleName })
      : translate(model.language, settingsTitleKey(kind));
  }

  function settingsTriggerFor(kind: SettingsEditorKind): HTMLElement | null {
    return bar.querySelector<HTMLElement>(`[data-setting="${kind}"]`);
  }

  function applySettingsPending(): void {
    if (settingsSaveButton !== null) {
      settingsSaveButton.disabled = settingsPending;
    }
    if (settingsReapplyButton !== null) {
      settingsReapplyButton.disabled = settingsPending;
    }
    const cancel = settingsFooter?.querySelector<HTMLButtonElement>("[data-action='cancel']") ?? null;
    if (cancel !== null) {
      cancel.disabled = settingsPending;
    }
  }

  /**
   * Notices inside the dialog: one sentence, re-inserted whenever the body is rebuilt. Validation
   * errors and refusals share one node; the stable code stays as subdued detail.
   */
  function paintSettingsNotice(): void {
    settingsNoticeNode?.remove();
    settingsNoticeNode = null;
    if (settingsBody !== null && settingsNotice !== null) {
      if (settingsForm === null) {
        settingsBody.replaceChildren();
      }
      const paragraph = element(doc, "p", C.settingsNotice, translate(model.language, settingsNotice.sentenceKey));
      if (settingsNotice.code !== null) {
        paragraph.dataset["code"] = settingsNotice.code;
      }
      // Beside Save, in the fixed row, so a refusal is seen however far down the form is scrolled.
      if (settingsForm !== null && settingsFooter !== null) {
        settingsFooter.prepend(paragraph);
      } else {
        settingsBody.append(paragraph);
      }
      settingsNoticeNode = paragraph;
    }
    applySettingsPending();
  }

  function renderSettingsBody(): void {
    const form = settingsForm;
    if (form === null || isDestroyed()) {
      return;
    }
    settingsVehicleName = null;
    let building = true;
    const built = settingsEditorBody(
      doc,
      model.language,
      form,
      {
        onSave: (values) => input.onSaveSettings(form.kind, values),
        onCancel: () => closeSettingsEditor(),
        onReload: () => input.onReloadSettings(form.kind),
        onReapply: (values) => input.onReapplySettings(form.kind, values),
        ...(input.onPreviewSettings === undefined
          ? {}
          : { onDraft: (values: SettingsFormValues) => input.onPreviewSettings?.(form.kind, values) }),
        // The title follows the Vehicle select and the mode at once, before anything is saved.
        onVehicleName: (name) => {
          settingsVehicleName = name;
          if (!building && settingsForm === form && settingsDialog.isOpen()) {
            settingsDialog.setTitle(settingsTitle(form.kind));
          }
        },
      },
      `${idPrefix}-settings-${form.kind}`,
    );
    building = false;
    settingsBody = built.body;
    settingsBodyReader = built.values;
    settingsPreviewSlot = built.preview;
    settingsSaveButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsSave}`);
    settingsReapplyButton = built.body.querySelector<HTMLButtonElement>(`.${C.settingsReapply}`);
    settingsFooter = takeActions(built.body, `${idPrefix}-settings`);
    settingsDialog.show({
      title: settingsTitle(form.kind),
      body: built.body,
      opener: settingsTriggerFor(form.kind),
      actions: settingsFooter,
    });
    paintSettingsNotice();
  }

  function openSettingsEditor(kind: SettingsEditorKind): void {
    if (isDestroyed()) {
      return;
    }
    settingsKind = kind;
    settingsForm = null;
    settingsBody = null;
    settingsFooter = null;
    settingsBodyReader = null;
    settingsPreviewSlot = null;
    settingsNotice = null;
    settingsNoticeNode = null;
    settingsSaveButton = null;
    settingsReapplyButton = null;
    settingsPending = false;
    // One overlay at a time, and the others do not restore focus on the way out.
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const loading = element(
      doc,
      "p",
      `${C.muted} ${C.dialogIntro}`,
      translate(model.language, "settings.loading"),
    );
    // This paragraph is the dialog's body until a form replaces it, so a failure that arrives while the
    // record is still being read has a node to be shown in rather than a sentence with nowhere to go.
    settingsBody = loading;
    settingsFooter = null;
    settingsDialog.show({
      title: translate(model.language, settingsTitleKey(kind)),
      body: loading,
      opener: settingsTriggerFor(kind),
    });
  }

  function showSettingsEditorForm(form: SettingsEditorForm): void {
    if (isDestroyed() || settingsKind !== form.kind) {
      return;
    }
    settingsForm = form;
    settingsNotice = null;
    settingsPending = false;
    renderSettingsBody();
  }

  function showSettingsEditorConflict(revision: number, phases: number | null): void {
    const form = settingsForm;
    if (isDestroyed() || form === null) {
      return;
    }
    const values = settingsBodyReader === null ? form.values : settingsBodyReader();
    // The server's own phase count comes with the conflict: a reapply is built on that record, so the
    // nominal power the form names must be the one that record would actually draw.
    settingsForm = { ...form, values, conflict: revision, phases };
    settingsNotice = null;
    settingsPending = false;
    renderSettingsBody();
  }

  function setSettingsEditorNotice(failure: FailureSentence | null): void {
    settingsNotice = failure;
    paintSettingsNotice();
  }

  function setSettingsEditorPending(pending: boolean): void {
    settingsPending = pending;
    applySettingsPending();
  }

  function setSettingsPreview(state: PreviewState): void {
    const slot = settingsPreviewSlot;
    if (isDestroyed() || slot === null || !slot.isConnected || settingsKind !== "plan") {
      return;
    }
    renderPlanPreview(doc, slot, state, model.format, now());
  }

  function closeSettingsEditor(): void {
    settingsDialog.hide();
    settingsKind = null;
    settingsForm = null;
    settingsBody = null;
    settingsFooter = null;
    settingsBodyReader = null;
    settingsPreviewSlot = null;
    settingsNotice = null;
    settingsNoticeNode = null;
    settingsSaveButton = null;
    settingsReapplyButton = null;
    settingsPending = false;
  }

  function settingsEditorOpen(): SettingsEditorKind | null {
    return settingsDialog.isOpen() ? settingsKind : null;
  }

  function setSettingsError(failure: FailureSentence | null): void {
    if (failure === null) {
      settingsError.hidden = true;
      settingsError.textContent = "";
      settingsError.removeAttribute("data-code");
      fit();
      return;
    }
    settingsError.hidden = false;
    settingsError.textContent = translate(model.language, failure.sentenceKey);
    if (failure.code === null) {
      settingsError.removeAttribute("data-code");
    } else {
      settingsError.dataset["code"] = failure.code;
    }
    // A sentence appearing or going changes the rows above the chart: in a box of fixed height, fit again.
    fit();
  }

  return {
    openSettingsEditor,
    showSettingsEditorForm,
    setSettingsEditorNotice,
    showSettingsEditorConflict,
    setSettingsEditorPending,
    setSettingsPreview,
    settingsEditorOpen,
    closeSettingsEditor,
    setSettingsError,
  };
}
