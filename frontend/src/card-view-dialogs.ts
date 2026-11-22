// The dialogs the card view opens from its header, banner and action bar: the issues (with "Get started"), the
// capabilities, the automations, the pause choices and the strategy list, and the settings a banner row jumps to.

import { translate } from "./i18n";
import { type EntityScope } from "./entity-config";
import { type IssueFix } from "./status";
import { setupSection, type SetupRowId } from "./setup-checklist";
import { automationsBody } from "./automations";
import { VISUAL_CLASSES as C } from "./visual-styles";
import { element } from "./dom";
import {
  SOLAR_SETUP_ROWS,
  STRATEGY_DESCRIBE_KEYS,
  STRATEGY_NEEDS_TOTAL_POWER,
  capabilityBody,
  issueListBody,
  navigateTo,
  strategyRowHidden,
} from "./card-view-parts";
import type { SettingsPageState, ViewContext } from "./card-view-context";

export interface DialogOpenersDeps {
  mayPlan: boolean;
  setTimer: (callback: () => void, delayMs: number) => number;
  /** The action bar's planner and strategy buttons, which a dialog returns focus to (they exist once the bar is built). */
  plannerButton: () => HTMLButtonElement | null;
  strategyButton: () => HTMLButtonElement | null;
  hideForChildDialog: () => void;
  openEntities: (scope: EntityScope, focus?: { part: string; title: string } | null) => void;
  openSettingsOverview: () => void;
  overviewPanel: () => HTMLElement;
  /** Show the shared heat section in full even when nothing is found ("Heat · nothing found" opened). */
  expandHeat: () => void;
  /** The info dialog is opened from Settings (or not): leaving it by the cross, Escape or the backdrop goes back. */
  setCapabilityFromSettings: (fromSettings: boolean) => void;
}

export function createDialogOpeners(ctx: ViewContext, settingsPage: SettingsPageState, deps: DialogOpenersDeps) {
  const { input, model, doc, idPrefix, isDestroyed, dialogs, settingsGeneral } = ctx;
  const issuesDialog = dialogs.issues;
  const releaseDialog = dialogs.release;
  const capabilityDialog = dialogs.capability;
  const pauseDialog = dialogs.pause;
  const strategyDialog = dialogs.strategy;
  const vehicleDialog = dialogs.vehicle;
  const settingsOverviewDialog = dialogs.settingsOverview;
  const { mayPlan, setTimer, plannerButton, strategyButton, hideForChildDialog, openEntities, openSettingsOverview, overviewPanel, expandHeat, setCapabilityFromSettings } =
    deps;

  function openIssues(opener: HTMLElement | null): void {
    if (isDestroyed()) {
      return;
    }
    // Two overlays and two focus traps must never coexist: the other dialog closes first, and it does
    // not restore focus on the way out (that would briefly focus a background control).
    capabilityDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const setup = setupNode();
    issuesDialog.show({
      // Only setup rows to show: the dialog is "Get started" itself.
      title: translate(model.language, model.issues.length === 0 && setup !== null ? "setup.heading" : "dialog.issues"),
      body: issueListBody(doc, model, setup, issueFix, input.isAdmin),
      opener,
    });
  }

  /**
   * Settings, at one section, and the editor of one of its rows. A row of the charger's section appears only once
   * its entity configuration is read, so the row is looked for a few times before the section alone is left shown.
   */
  function openSettingAt(section: string, edit: string): void {
    if (section === "heat") {
      expandHeat();
    }
    openSettingsOverview();
    overviewPanel().querySelector<HTMLElement>(`[data-section="${section}"]`)?.scrollIntoView?.({ block: "start" });
    let tries = 0;
    const look = (): void => {
      if (isDestroyed() || !settingsOverviewDialog.isOpen()) {
        return;
      }
      const row = Array.from(overviewPanel().querySelectorAll<HTMLElement>("[data-edit]")).find(
        (node) => node.dataset["edit"] === edit,
      );
      if (row !== undefined) {
        row.click();
        return;
      }
      tries += 1;
      if (tries < 10) {
        setTimer(look, 300);
      }
    };
    look();
  }

  /** An issue's button was pressed: the dialog closes and the card writes the fix, then reads the dashboard again. */
  /**
   * "Release the import target for this charge": what it would mean, and Cancel or Release; only Release sends it (an
   * accidental press must not cost the month's peak).
   */
  function confirmRelease(opener: HTMLElement | null): void {
    const confirm = model.releaseConfirm;
    if (confirm === null || isDestroyed()) {
      return;
    }
    const body = element(doc, "div");
    body.append(element(doc, "p", C.dialogIntro, confirm.text));
    const answers = element(doc, "div", C.suggestionAnswers);
    const cancel = element(doc, "button", C.choiceButton, translate(model.language, "settings.cancel"));
    cancel.setAttribute("type", "button");
    cancel.dataset["action"] = "cancel";
    cancel.addEventListener("click", () => releaseDialog.hide());
    const release = element(doc, "button", C.choiceButton, translate(model.language, "release.confirm.release"));
    release.setAttribute("type", "button");
    release.dataset["action"] = "release";
    // At the power level's last allowed exceedance (or past it) the first press only asks again.
    let asked = !confirm.askTwice;
    release.addEventListener("click", () => {
      if (!asked) {
        asked = true;
        release.textContent = translate(model.language, "release.confirm.levelAgain");
        return;
      }
      releaseDialog.hide({ restoreFocus: false });
      input.onReleaseImportTarget?.();
    });
    answers.append(cancel, release);
    body.append(answers);
    releaseDialog.show({ title: translate(model.language, "release.confirm.title"), body, opener });
  }

  function issueFix(fix: IssueFix): void {
    issuesDialog.hide({ restoreFocus: false });
    if (fix.action === "release_import_target") {
      confirmRelease(null);
      return;
    }
    input.onFixOwnMode?.(fix.entity, fix.value);
  }

  /** The dialog's "Get started" section, for an administrator with something left to set up; else `null`. */
  function setupNode(): HTMLElement | null {
    if (!input.isAdmin || model.setup.length === 0) {
      return null;
    }
    return setupSection(doc, {
      language: model.language,
      rows: model.setup,
      heading: model.issues.length > 0,
      openPlan: () => {
        issuesDialog.hide({ restoreFocus: false });
        input.onOpenSettings("plan");
      },
      openSetting: (section, edit) => openSettingAt(section, edit),
      forecastChoosable: (model.site?.solarForecastChoices.length ?? 0) > 0,
      navigate: (path) => {
        issuesDialog.hide({ restoreFocus: false });
        navigateTo(doc, path);
      },
      openExternal: (url) => {
        doc.defaultView?.open(url, "_blank", "noopener,noreferrer");
      },
      dismiss: (id: SetupRowId) => {
        issuesDialog.hide({ restoreFocus: false });
        input.onDismissSetup?.(id);
      },
      writeSiteOption: (field, value) => {
        issuesDialog.hide({ restoreFocus: false });
        input.onWriteSiteOption?.(field, value);
      },
    });
  }

  function openCapabilities(): void {
    if (isDestroyed()) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    setCapabilityFromSettings(settingsOverviewDialog.isOpen());
    settingsOverviewDialog.hide({ restoreFocus: false });
    capabilityDialog.show({
      title: translate(model.language, "cap.title"),
      body: capabilityBody(doc, model),
      opener: settingsGeneral,
    });
  }

  /** "Automations" from Settings: the blueprints, each with its import button, in the info dialog. Administrators only. */
  function openAutomations(): void {
    if (isDestroyed() || !input.isAdmin) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    setCapabilityFromSettings(settingsOverviewDialog.isOpen());
    settingsOverviewDialog.hide({ restoreFocus: false });
    const list = automationsBody(doc, model.language, (path) => {
      capabilityDialog.hide({ restoreFocus: false });
      navigateTo(doc, path);
    });
    capabilityDialog.show({
      title: translate(model.language, "settings.automations.label"),
      body: list.body,
      opener: settingsGeneral,
    });
    // How many automations use each one, once Home Assistant answers; none shown while it has not.
    void input.onLoadBlueprintUse?.().then((use) => {
      if (use !== null && !isDestroyed()) {
        list.showUse(use);
      }
    });
  }

  function openPause(): void {
    if (isDestroyed() || model.control.choices.length === 0) {
      // No choices is not an empty menu: it is this charger having none to offer, and opening a
      // dialog with nothing in it would be worse than doing nothing.
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    strategyDialog.hide({ restoreFocus: false });
    vehicleDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const body = element(doc, "div");
    body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "pause.intro")));
    const list = element(doc, "div", C.pauseChoices);
    for (const choice of model.control.choices) {
      const button = element(doc, "button", C.choiceButton, translate(model.language, choice.labelKey));
      button.type = "button";
      button.dataset["choice"] = choice.id;
      button.disabled = !model.control.canAct;
      button.addEventListener("click", () => {
        // One click, one request: the dialog closes first, and the choice is the backend's own id.
        pauseDialog.hide({ restoreFocus: false });
        input.onAction("stop", choice.id);
      });
      list.append(button);
    }
    body.append(list);
    pauseDialog.show({
      title: translate(model.language, "pause.sheetTitle"),
      body,
      // The sheet belongs to the automatic control, so focus returns to *that* button.
      opener: plannerButton(),
    });
  }

  function openStrategy(): void {
    if (isDestroyed()) {
      return;
    }
    issuesDialog.hide({ restoreFocus: false });
    capabilityDialog.hide({ restoreFocus: false });
    pauseDialog.hide({ restoreFocus: false });
    settingsOverviewDialog.hide({ restoreFocus: false });
    const body = element(doc, "div");
    body.append(element(doc, "p", `${C.muted} ${C.dialogIntro}`, translate(model.language, "strategy.intro")));
    if (!mayPlan) {
      body.append(element(doc, "p", C.settingsReadOnly, translate(model.language, "settings.readOnly")));
    }
    for (const row of model.strategy.rows) {
      if (strategyRowHidden(row, input.isAdmin)) {
        continue;
      }
      const item = element(doc, "div", C.strategyRow);
      const selected = row.id === model.strategy.selectedId;
      // An available, non-selected row is a real button for a reader who may change the strategy (`mayPlan`). The
      // selected and unavailable rows stay disabled; the backend's own check is the security boundary.
      const writable = row.available && !selected && mayPlan;
      const button = element(doc, "button", C.choiceButton, translate(model.language, row.labelKey));
      button.type = "button";
      button.dataset["strategy"] = row.id;
      button.disabled = !writable;
      button.setAttribute("aria-pressed", selected ? "true" : "false");
      if (writable) {
        button.addEventListener("click", () => {
          // No confirmation dialog: the click closes this one at once, and the card reports a refusal on the row-level sentence.
          strategyDialog.hide({ restoreFocus: false });
          vehicleDialog.hide({ restoreFocus: false });
          input.onSelectStrategy(row.id);
        });
      }
      item.append(button);
      // What the strategy does, in one line under its name.
      const describe = STRATEGY_DESCRIBE_KEYS[row.id];
      if (describe !== undefined) {
        item.append(element(doc, "span", C.strategyReason, translate(model.language, describe)));
      }
      if (row.reason !== null) {
        item.append(element(doc, "span", C.strategyReason, row.reason));
        // A solar strategy held back for lack of solar settings points an administrator at the card that sets them.
        if (
          !row.available &&
          model.site !== null &&
          SOLAR_SETUP_ROWS.has(row.id) &&
          row.reasonCode !== STRATEGY_NEEDS_TOTAL_POWER &&
          input.isAdmin
        ) {
          const link = element(doc, "button", C.strategyLink, translate(model.language, "strategy.setupSolar"));
          link.type = "button";
          link.dataset["action"] = "setup-solar";
          link.addEventListener("click", () => {
            strategyDialog.hide({ restoreFocus: false });
            vehicleDialog.hide({ restoreFocus: false });
            openSettingsOverview();
            settingsPage.overviewBodyNode
              ?.querySelector<HTMLElement>("[data-section='solar']")
              ?.scrollIntoView?.({ block: "nearest" });
            settingsPage.overviewBodyNode?.querySelector<HTMLElement>("[data-edit-solar]")?.focus();
          });
          item.append(link);
        }
        // A direct site held back for lack of the meter's total grid power points at the site's entities,
        // where the field is, for the administrator who can change it.
        if (!row.available && row.reasonCode === STRATEGY_NEEDS_TOTAL_POWER && input.isAdmin) {
          const link = element(doc, "button", C.strategyLink, translate(model.language, "strategy.setupSite"));
          link.type = "button";
          link.dataset["action"] = "setup-site";
          link.addEventListener("click", () => {
            strategyDialog.hide({ restoreFocus: false });
            vehicleDialog.hide({ restoreFocus: false });
            openEntities("site");
          });
          item.append(link);
        }
      }
      body.append(item);
    }
    strategyDialog.show({
      title: translate(model.language, "strategy.dialogTitle"),
      body,
      opener: strategyButton(),
    });
  }

  return { openIssues, openSettingAt, confirmRelease, openCapabilities, openAutomations, openPause, openStrategy };
}
