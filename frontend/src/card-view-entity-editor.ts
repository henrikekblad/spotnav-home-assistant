// The entity editors of the card view: one overlay drawn from the config the card read, never optimistic.

import { takeActions } from "./dialog";
import { entityEditorBody, type EntityEditorBody } from "./entity-editor";
import type { EntityConfig, EntityFieldError, EntityScope } from "./entity-config";
import { translate } from "./i18n";
import type { FailureSentence } from "./card-view";
import type { ViewContext } from "./card-view-context";

export function createEntityEditorSection(ctx: ViewContext) {
  const { input, model, doc, idPrefix, isDestroyed, dialogs, settingsGeneral, leaveSettingsChild, entityFocus: currentEntityFocus, openSiteSettings } = ctx;
  const issuesDialog = dialogs.issues;
  const capabilityDialog = dialogs.capability;
  const pauseDialog = dialogs.pause;
  const strategyDialog = dialogs.strategy;
  const vehicleDialog = dialogs.vehicle;
  const settingsDialog = dialogs.settings;
  const marketDialog = dialogs.market;
  const settingsOverviewDialog = dialogs.settingsOverview;
  const entityDialog = dialogs.entity;

  // The entity editors: one overlay drawn from the config the card read, never optimistic.
  let entityEditor: { scope: EntityScope | "vehicle" | "solar"; built: EntityEditorBody } | null = null;

  function openEntityEditor(scope: EntityScope, config: EntityConfig, notice: FailureSentence | null = null): void {
    if (isDestroyed()) {
      return;
    }
    const entityFocus = currentEntityFocus();
    const focus = entityFocus !== null && entityFocus.scope === scope ? entityFocus : null;
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsDialog.hide({ restoreFocus: false });
    marketDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const built = entityEditorBody(
      doc,
      model.language,
      {
        scope,
        config,
        hass: () => input.hass?.(),
        appliesText: scope === "site" ? (model.site?.appliesToText ?? null) : null,
        ...(model.chargerName === null ? {} : { chargerName: model.chargerName }),
        ...(focus === null ? {} : { focus: focus.part }),
      },
      {
        onSave: (draft) => input.onSaveEntities?.(scope, draft),
        onCancel: () => leaveSettingsChild(entityDialog, input.onCancelEntities),
        onOpenSite: openSiteSettings,
      },
      idPrefix,
    );
    entityEditor = { scope, built };
    if (notice !== null) {
      built.setNotice(translate(model.language, notice.sentenceKey), notice.code);
    }
    entityDialog.show({
      actions: takeActions(built.body, `${idPrefix}-entity`),
      title: focus !== null ? focus.title : translate(model.language, scope === "site" ? "entity.editor.site" : "entity.editor.charger"),
      body: built.body,
      opener: settingsGeneral,
    });
  }

  function entityEditorOpen(): EntityScope | null {
    return entityEditor !== null && entityEditor.scope !== "vehicle" && entityEditor.scope !== "solar" && entityDialog.isOpen()
      ? entityEditor.scope
      : null;
  }

  function closeEntityEditor(): void {
    entityDialog.hide({ restoreFocus: false });
    entityEditor = null;
  }

  return {
    openEntityEditor,
    entityEditorOpen,
    closeEntityEditor,
    setEntityEditorNotice(failure: FailureSentence | null): void {
      entityEditor?.built.setNotice(
        failure === null ? null : translate(model.language, failure.sentenceKey),
        failure === null ? null : failure.code,
      );
    },
    markEntityFieldErrors(errors: readonly EntityFieldError[]): void {
      entityEditor?.built.markErrors(errors);
    },
    setEntityEditorPending(pending: boolean): void {
      entityEditor?.built.setPending(pending);
    },
    setEntityHass(hass: unknown): void {
      entityEditor?.built.setHass(hass);
    },
  };
}
