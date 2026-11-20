// What the sections of the card view share: the input and model, the document, the dialogs the view owns and the
// few nodes and functions one section needs of another. `createCardView` builds one of these and hands it to each
// section builder (`card-view-*.ts`), so a section is a function of this object instead of a stretch of one closure.

import type { CardViewInput } from "./card-view";
import type { DialogHandle } from "./dialog";
import type { CardModel } from "./model";

/** Every dialog the card view owns, by the name its section uses. */
export interface ViewDialogs {
  issues: DialogHandle;
  release: DialogHandle;
  capability: DialogHandle;
  pause: DialogHandle;
  strategy: DialogHandle;
  vehicle: DialogHandle;
  settings: DialogHandle;
  market: DialogHandle;
  entity: DialogHandle;
  settingsOverview: DialogHandle;
  history: DialogHandle;
  value: DialogHandle;
}

export interface ViewContext {
  input: CardViewInput;
  model: CardModel;
  doc: Document;
  idPrefix: string;
  now: () => number;
  /** Whether `destroy()` has run. */
  isDestroyed: () => boolean;
  dialogs: ViewDialogs;
  /** The action bar: the settings triggers sit in it, and a closed editor returns focus to its trigger. */
  bar: HTMLElement;
  /** The line above the status that tells a Settings write failed. */
  settingsError: HTMLElement;
  /** The header's Settings button: what the editors opened from the Settings page return focus to. */
  settingsGeneral: HTMLElement;
  historyButton: HTMLElement;
  /** Fits the card to a box of fixed height again. */
  fit: () => void;
  /** The one way out of a dialog opened from the Settings page. */
  leaveSettingsChild: (dialog: DialogHandle, cancel?: () => boolean | void) => void;
  /** The entity editor opened from a row of the Settings page, which part it focuses. */
  entityFocus: () => { scope: string; part: string; title: string } | null;
  openSiteSettings: () => void;
}
