// The Notifications part of Settings: which phones (the Home Assistant app's `notify.mobile_app_*`
// services) hear about which events, as two summary rows and a dialog with Save and Cancel. The choice is
// the settings record's `notifications` field; the card saves it as one full replacement
// (`notificationsReplacement` in `settings.ts`). Nothing here talks to the backend.

import { translate, type Language, type TranslationKey } from "./i18n";
import { NOTIFICATION_EVENTS, type NotificationsChoice } from "./settings";
import type { NotificationsRecord } from "./types";
import { VISUAL_CLASSES as C } from "./visual-styles";

export interface NotificationsEditorHandlers {
  onSave: (choice: NotificationsChoice) => void;
  onCancel: () => void;
}

export interface NotificationsEditorBody {
  body: HTMLElement;
  choice: () => NotificationsChoice;
}

const EVENT_KEYS: Record<(typeof NOTIFICATION_EVENTS)[number], TranslationKey> = {
  plan_stopped: "notifications.event.planStopped",
  plan_at_risk: "notifications.event.planAtRisk",
  charge_complete: "notifications.event.chargeComplete",
  charge_started: "notifications.event.chargeStarted",
  plugged_in: "notifications.event.pluggedIn",
  unplugged: "notifications.event.unplugged",
  plan_installed: "notifications.event.planInstalled",
  vehicle_identify: "notifications.event.vehicleIdentify",
};

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

/** A chosen service's name: its phone's, or the service itself when no phone answers to it now. */
/** The phones a person can choose (the available ones, then any chosen one that is gone), as editor options. */
export function phoneOptions(language: Language, record: NotificationsRecord): Array<{ value: string; label: string }> {
  const services = [
    ...record.available.map((entry) => entry.service),
    ...record.targets.filter((service) => !record.available.some((entry) => entry.service === service)),
  ];
  return services.map((service) => ({ value: service, label: targetName(language, record, service) }));
}

/** Every event, in the backend's order, as editor options. */
export function eventOptions(language: Language): Array<{ value: string; label: string }> {
  return NOTIFICATION_EVENTS.map((event) => ({ value: event, label: translate(language, EVENT_KEYS[event]) }));
}

function targetName(language: Language, record: NotificationsRecord, service: string): string {
  const found = record.available.find((entry) => entry.service === service);
  return found !== undefined ? found.name : translate(language, "notifications.missing", { name: service });
}

/**
 * The hint for a charger whose events are on but whose phones are none: it sends nothing. `null` otherwise.
 * Display only; it never copies another charger's choice.
 */
export function noRecipientsHint(language: Language, record: NotificationsRecord): string | null {
  return record.events.length > 0 && record.targets.length === 0
    ? translate(language, "notifications.noRecipients")
    : null;
}

/** The hint as a paragraph, or `null` when there is nothing to say. */
export function noRecipientsNote(doc: Document, language: Language, record: NotificationsRecord): HTMLElement | null {
  const hint = noRecipientsHint(language, record);
  if (hint === null) {
    return null;
  }
  const note = element(doc, "p", C.settingsNote, hint);
  note.dataset["notice"] = "notifications_no_targets";
  return note;
}

/** The two summary rows: the chosen phones, and how many events are on. */
export function notificationsSummary(
  language: Language,
  record: NotificationsRecord,
): Array<{ key: string; label: string; value: string }> {
  const phones = record.targets.map((service) => targetName(language, record, service));
  return [
    {
      key: "notification_targets",
      label: translate(language, "notifications.phones"),
      value: phones.length === 0 ? translate(language, "settings.value.none") : phones.join(", "),
    },
    {
      key: "notification_events",
      label: translate(language, "notifications.events"),
      value: translate(language, "notifications.eventsOn", {
        count: String(record.events.length),
        total: String(NOTIFICATION_EVENTS.length),
      }),
    },
  ];
}
