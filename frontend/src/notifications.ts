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
function targetName(language: Language, record: NotificationsRecord, service: string): string {
  const found = record.available.find((entry) => entry.service === service);
  return found !== undefined ? found.name : translate(language, "notifications.missing", { name: service });
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

/**
 * The dialog body: a checkbox per phone (the available ones, then any chosen one that is gone) and per
 * event. `url` is where a tap opens, the page the card is on.
 */
export function notificationsEditorBody(
  doc: Document,
  language: Language,
  record: NotificationsRecord,
  url: string | null,
  handlers: NotificationsEditorHandlers,
): NotificationsEditorBody {
  const targets = new Set(record.targets);
  const events = new Set(record.events);
  const choice = (): NotificationsChoice => ({
    targets: [...targets],
    events: NOTIFICATION_EVENTS.filter((event) => events.has(event)),
    url,
  });

  const body = element(doc, "form") as HTMLFormElement;
  body.noValidate = true;
  body.dataset["notificationsEditor"] = "true";
  body.append(element(doc, "p", C.siteApplies, translate(language, "notifications.intro")));

  const phones = element(doc, "fieldset", C.siteFieldset);
  phones.dataset["part"] = "targets";
  phones.append(element(doc, "legend", C.siteLegend, translate(language, "notifications.phones")));
  const services = [
    ...record.available.map((entry) => entry.service),
    ...record.targets.filter((service) => !record.available.some((entry) => entry.service === service)),
  ];
  for (const service of services) {
    const label = element(doc, "label", C.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.checked = targets.has(service);
    box.dataset["target"] = service;
    box.addEventListener("change", () => {
      if (box.checked) {
        targets.add(service);
      } else {
        targets.delete(service);
      }
    });
    label.append(box, doc.createTextNode(targetName(language, record, service)));
    phones.append(label);
  }
  if (services.length === 0) {
    phones.append(element(doc, "p", C.settingsNote, translate(language, "notifications.noPhones")));
  }
  body.append(phones);

  const kinds = element(doc, "fieldset", C.siteFieldset);
  kinds.dataset["part"] = "events";
  kinds.append(element(doc, "legend", C.siteLegend, translate(language, "notifications.events")));
  for (const event of NOTIFICATION_EVENTS) {
    const label = element(doc, "label", C.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.checked = events.has(event);
    box.dataset["event"] = event;
    box.addEventListener("change", () => {
      if (box.checked) {
        events.add(event);
      } else {
        events.delete(event);
      }
    });
    label.append(box, doc.createTextNode(translate(language, EVENT_KEYS[event])));
    kinds.append(label);
  }
  body.append(kinds);

  const actions = element(doc, "div", C.settingsActions);
  const save = element(doc, "button", `${C.button} ${C.settingsSave}`, translate(language, "settings.save")) as HTMLButtonElement;
  save.type = "submit";
  const cancel = element(doc, "button", C.button, translate(language, "settings.cancel")) as HTMLButtonElement;
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    handlers.onSave(choice());
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });
  return { body, choice };
}
