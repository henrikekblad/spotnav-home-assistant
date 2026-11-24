// One notification section for the whole site, per phone (SpotNav 3.0, `plans/v3_notifications_per_phone.md`): the
// phones, each with its own on/off, its events (grouped Charging / Pool / Spa / Sauna as the site has them) and, with
// more than one charger, the chargers it follows. A phone is one row whether it reaches Home Assistant through the
// Companion app, the SpotNav app or both; "Same phone as" joins the two once. Shown in the charger card's Settings
// and in the shared settings popover of the house and heat cards (administrators only).
//
// Reads `spotnav/notification_phones`, writes `spotnav/update_notification_phone` (with the card's page, where a tap on
// a notification opens). Each answer is the whole listing again, read strictly: anything else is the generic failure.

import { getNotificationPhones, SpotnavApiError, updateNotificationPhone } from "./api";
import { element } from "./dom";
import { translate, type Language, type TranslationKey } from "./i18n";
import { NOTIFICATION_PHONES_API_VERSION, type HomeAssistantLike } from "./types";
import { sectionHeading, settingRow, shell, type EditorHandlers } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** Every event a phone can hear, in the backend's order (`notifications/phones.PHONE_EVENTS`). */
export const PHONE_EVENTS = [
  "plan_stopped",
  "plan_at_risk",
  "charge_complete",
  "charge_started",
  "plugged_in",
  "unplugged",
  "plan_installed",
  "vehicle_identify",
  "pool_season",
] as const;
export type PhoneEvent = (typeof PHONE_EVENTS)[number];

export type PhoneRoute = "app" | "companion" | "none";

export interface PhoneRow {
  id: string;
  name: string;
  on: boolean;
  events: PhoneEvent[];
  /** `null`: every charger, also one added later. */
  chargers: string[] | null;
  companion: { service: string; name: string; found: boolean } | null;
  app: { instant: boolean } | null;
  route: PhoneRoute;
}

export interface PhoneLoads {
  chargers: number;
  pool: boolean;
  spa: boolean;
  sauna: boolean;
}

export interface PhonesAnswer {
  phones: PhoneRow[];
  chargers: Array<{ id: string; name: string }>;
  loads: PhoneLoads;
}

export type PhonesDecoded = { ok: true; value: PhonesAnswer } | { ok: false; code: string | null };

const EVENT_KEYS: Record<PhoneEvent, TranslationKey> = {
  plan_stopped: "notifications.event.planStopped",
  plan_at_risk: "notifications.event.planAtRisk",
  charge_complete: "notifications.event.chargeComplete",
  charge_started: "notifications.event.chargeStarted",
  plugged_in: "notifications.event.pluggedIn",
  unplugged: "notifications.event.unplugged",
  plan_installed: "notifications.event.planInstalled",
  vehicle_identify: "notifications.event.vehicleIdentify",
  pool_season: "notifications.event.poolSeason",
};

const ROUTE_KEYS: Record<PhoneRoute, TranslationKey> = {
  app: "notifications.via.app",
  companion: "notifications.via.companion",
  none: "notifications.via.none",
};

/** The untouched Companion phone's id prefix: listed, never stored until someone changes it. */
const UNTOUCHED = "notify:";

function object(raw: unknown): Record<string, unknown> | null {
  return typeof raw === "object" && raw !== null && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null;
}

function text(raw: unknown): string | null {
  return typeof raw === "string" && raw !== "" ? raw : null;
}

function strings(raw: unknown): string[] | null {
  return Array.isArray(raw) && raw.every((item) => typeof item === "string") ? (raw as string[]) : null;
}

function decodePhone(raw: unknown): PhoneRow | null {
  const phone = object(raw);
  if (phone === null) {
    return null;
  }
  const id = text(phone["id"]);
  const name = typeof phone["name"] === "string" ? phone["name"] : null;
  const events = strings(phone["events"]);
  const chargers = phone["chargers"] === null ? null : strings(phone["chargers"]);
  const route = phone["route"];
  if (
    id === null ||
    name === null ||
    typeof phone["on"] !== "boolean" ||
    events === null ||
    (phone["chargers"] !== null && chargers === null) ||
    (route !== "app" && route !== "companion" && route !== "none")
  ) {
    return null;
  }
  let companion: PhoneRow["companion"] = null;
  if (phone["companion"] !== null) {
    const block = object(phone["companion"]);
    const service = block === null ? null : text(block["service"]);
    if (block === null || service === null || typeof block["name"] !== "string" || typeof block["found"] !== "boolean") {
      return null;
    }
    companion = { service, name: block["name"], found: block["found"] };
  }
  let app: PhoneRow["app"] = null;
  if (phone["app"] !== null) {
    const block = object(phone["app"]);
    if (block === null || typeof block["instant"] !== "boolean") {
      return null;
    }
    app = { instant: block["instant"] };
  }
  return {
    id,
    name,
    on: phone["on"],
    // An event a later backend adds is read past: this card cannot offer it.
    events: events.filter((event): event is PhoneEvent => (PHONE_EVENTS as readonly string[]).includes(event)),
    chargers,
    companion,
    app,
    route,
  };
}

/** `{api_version: 1, ok, error, phones, chargers, loads, events}`, or `null` for anything else. */
export function decodePhonesAnswer(raw: unknown): PhonesDecoded | null {
  const answer = object(raw);
  if (answer === null || answer["api_version"] !== NOTIFICATION_PHONES_API_VERSION || typeof answer["ok"] !== "boolean") {
    return null;
  }
  if (!answer["ok"]) {
    return { ok: false, code: typeof answer["error"] === "string" ? answer["error"] : null };
  }
  const phones = Array.isArray(answer["phones"]) ? answer["phones"].map(decodePhone) : null;
  const chargers = Array.isArray(answer["chargers"])
    ? answer["chargers"].map((raw) => {
        const charger = object(raw);
        const id = charger === null ? null : text(charger["id"]);
        return charger === null || id === null || typeof charger["name"] !== "string" ? null : { id, name: charger["name"] };
      })
    : null;
  const loads = object(answer["loads"]);
  if (
    phones === null ||
    phones.some((phone) => phone === null) ||
    chargers === null ||
    chargers.some((charger) => charger === null) ||
    loads === null ||
    typeof loads["chargers"] !== "number" ||
    typeof loads["pool"] !== "boolean" ||
    typeof loads["spa"] !== "boolean" ||
    typeof loads["sauna"] !== "boolean"
  ) {
    return null;
  }
  return {
    ok: true,
    value: {
      phones: phones as PhoneRow[],
      chargers: chargers as Array<{ id: string; name: string }>,
      loads: { chargers: loads["chargers"], pool: loads["pool"], spa: loads["spa"], sauna: loads["sauna"] },
    },
  };
}

export interface EventGroup {
  key: "charging" | "pool" | "spa" | "sauna";
  events: PhoneEvent[];
}

/**
 * The event groups the site has: Charging with chargers, Pool with a pool, and *Not ready in time* once, in the first
 * of Charging, Spa and Sauna the site has (one event covers the car, the bath and the session). Värme has no event of
 * its own yet, so it is never shown.
 */
export function eventGroups(loads: PhoneLoads): EventGroup[] {
  const groups: EventGroup[] = [];
  if (loads.chargers > 0) {
    groups.push({ key: "charging", events: PHONE_EVENTS.filter((event) => event !== "pool_season") });
  }
  if (loads.pool) {
    groups.push({ key: "pool", events: ["pool_season"] });
  }
  if (loads.chargers === 0 && loads.spa) {
    groups.push({ key: "spa", events: ["plan_at_risk"] });
  } else if (loads.chargers === 0 && loads.sauna) {
    groups.push({ key: "sauna", events: ["plan_at_risk"] });
  }
  return groups;
}

/** The events a phone may tick here, in order. */
export function shownEvents(loads: PhoneLoads): PhoneEvent[] {
  return eventGroups(loads).flatMap((group) => group.events);
}

/** An event's name, as the phone's editor says it. */
export function eventLabel(language: Language, event: PhoneEvent): string {
  return translate(language, EVENT_KEYS[event]);
}

/** A phone's name as its row says it: marked when its Companion phone is gone and it has no app. */
export function phoneName(language: Language, phone: PhoneRow): string {
  return phone.companion !== null && !phone.companion.found && phone.app === null
    ? translate(language, "notifications.missing", { name: phone.name })
    : phone.name;
}

/** The row's value: Off, or how many of the shown events are on. */
export function phoneSummary(language: Language, phone: PhoneRow, loads: PhoneLoads): string {
  if (!phone.on) {
    return translate(language, "settings.value.off");
  }
  const shown = shownEvents(loads);
  return translate(language, "notifications.eventsOn", {
    count: String(shown.filter((event) => phone.events.includes(event)).length),
    total: String(shown.length),
  });
}

/** The entries "Same phone as" offers: the other kind (an app for a Companion phone, and back), not joined yet. */
export function linkCandidates(phone: PhoneRow, phones: readonly PhoneRow[]): PhoneRow[] {
  if (phone.companion !== null && phone.app === null) {
    return phones.filter((other) => other.app !== null && other.companion === null);
  }
  if (phone.app !== null && phone.companion === null) {
    return phones.filter((other) => other.companion !== null && other.app === null);
  }
  return [];
}

/** What one Save changes, against the phone as read: `null` when nothing does. Events not shown here are kept. */
export function phoneChanges(
  phone: PhoneRow,
  loads: PhoneLoads,
  chargerIds: readonly string[],
  choice: { on: boolean; events: readonly string[]; chargers: readonly string[] },
): Record<string, unknown> | null {
  const changes: Record<string, unknown> = {};
  if (choice.on !== phone.on) {
    changes["on"] = choice.on;
  }
  const shown = shownEvents(loads);
  const events = PHONE_EVENTS.filter((event) =>
    shown.includes(event) ? choice.events.includes(event) : phone.events.includes(event),
  );
  if (events.join(",") !== phone.events.join(",")) {
    changes["events"] = events;
  }
  if (chargerIds.length > 1) {
    const followed = chargerIds.filter((id) => choice.chargers.includes(id));
    const before = phone.chargers === null ? [...chargerIds] : chargerIds.filter((id) => phone.chargers?.includes(id));
    if (followed.join(",") !== before.join(",")) {
      changes["chargers"] = followed.length === chargerIds.length ? null : followed;
    }
  }
  return Object.keys(changes).length === 0 ? null : changes;
}

export interface NotificationPhonesInput {
  doc: Document;
  idPrefix: string;
  hass: () => HomeAssistantLike | null;
  language: () => Language;
  /** One editor in the card's own dialog; Save or Cancel goes back to the page. */
  openEditor: (title: string, build: (handlers: EditorHandlers) => HTMLFormElement) => void;
}

type State = { kind: "loading" } | { kind: "failed" } | { kind: "ready"; answer: PhonesAnswer };

/** The section and its phone editor; it reads the phones itself and paints its own slot when the answer comes. */
export class NotificationPhones {
  private readonly input: NotificationPhonesInput;
  private state: State = { kind: "loading" };
  private slot: HTMLElement | null = null;
  private reading: Promise<void> | null = null;

  constructor(input: NotificationPhonesInput) {
    this.input = input;
  }

  private get language(): Language {
    return this.input.language();
  }

  /** The section, drawn from what is known now and read again in the background. */
  node(): HTMLElement {
    const doc = this.input.doc;
    const section = element(doc, "section", C.settingsSection);
    section.dataset["section"] = "notifications";
    section.append(sectionHeading(doc, "notifications", translate(this.language, "settings.section.notifications")));
    const slot = element(doc, "div");
    slot.dataset["slot"] = "notification-phones";
    section.append(slot);
    this.slot = slot;
    this.paint();
    void this.load();
    return section;
  }

  /** Read the phones again (one read at a time), then paint. */
  async load(): Promise<void> {
    if (this.reading !== null) {
      return await this.reading;
    }
    const hass = this.input.hass();
    if (hass === null) {
      return;
    }
    this.reading = (async () => {
      try {
        const decoded = decodePhonesAnswer(await getNotificationPhones(hass));
        if (decoded !== null && decoded.ok) {
          this.state = { kind: "ready", answer: decoded.value };
        } else if (this.state.kind !== "ready") {
          this.state = { kind: "failed" };
        }
      } catch {
        if (this.state.kind !== "ready") {
          this.state = { kind: "failed" };
        }
      } finally {
        this.reading = null;
      }
      this.paint();
    })();
    return await this.reading;
  }

  private paint(): void {
    const slot = this.slot;
    if (slot === null) {
      return;
    }
    const doc = this.input.doc;
    const language = this.language;
    slot.replaceChildren();
    if (this.state.kind === "loading") {
      slot.append(element(doc, "p", C.muted, translate(language, "popover.loading")));
      return;
    }
    if (this.state.kind === "failed") {
      slot.append(element(doc, "p", C.muted, translate(language, "popover.failed")));
      return;
    }
    const { phones, loads } = this.state.answer;
    if (phones.length === 0) {
      slot.append(element(doc, "p", C.settingsNote, translate(language, "notifications.none")));
      return;
    }
    const intro = element(doc, "p", C.settingsNote, translate(language, "notifications.phonesIntro"));
    intro.dataset["notice"] = "notification_phones";
    slot.append(intro);
    for (const phone of phones) {
      slot.append(
        ...settingRow(doc, {
          key: `phone:${phone.id}`,
          label: phoneName(language, phone),
          value: phoneSummary(language, phone, loads),
          ...(phone.on ? { help: translate(language, ROUTE_KEYS[phone.route]) } : {}),
          onTap: () => this.openPhone(phone.id),
        }),
      );
    }
  }

  /** The card's page: where a tap on a notification opens. */
  private page(): string | null {
    const path = this.input.doc.defaultView?.location?.pathname ?? null;
    return path !== null && path.startsWith("/") ? path : null;
  }

  private async write(phoneId: string, changes: Record<string, unknown>): Promise<string | null> {
    const hass = this.input.hass();
    if (hass === null) {
      return translate(this.language, "settings.error.generic");
    }
    let decoded: PhonesDecoded | null;
    try {
      decoded = decodePhonesAnswer(await updateNotificationPhone(hass, phoneId, changes, this.page()));
    } catch (error) {
      decoded = { ok: false, code: error instanceof SpotnavApiError ? error.code : null };
    }
    if (decoded !== null && decoded.ok) {
      this.state = { kind: "ready", answer: decoded.value };
      this.paint();
      return null;
    }
    return translate(
      this.language,
      decoded !== null && decoded.code === "spotnav_not_admin" ? "phones.error.notAdmin" : "settings.error.generic",
    );
  }

  /** One phone's editor: on/off, its events by group, the chargers it follows, and "Same phone as". */
  openPhone(phoneId: string): void {
    if (this.state.kind !== "ready") {
      return;
    }
    const answer = this.state.answer;
    const phone = answer.phones.find((row) => row.id === phoneId);
    if (phone === undefined) {
      return;
    }
    this.input.openEditor(phoneName(this.language, phone), (handlers) => this.phoneEditor(answer, phone, handlers));
  }

  private checkbox(label: string, checked: boolean, data: Record<string, string>): [HTMLLabelElement, HTMLInputElement] {
    const doc = this.input.doc;
    const wrap = element(doc, "label", C.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.checked = checked;
    for (const [key, value] of Object.entries(data)) {
      box.dataset[key] = value;
    }
    wrap.append(box, doc.createTextNode(label));
    return [wrap, box];
  }

  private phoneEditor(answer: PhonesAnswer, phone: PhoneRow, handlers: EditorHandlers): HTMLFormElement {
    const doc = this.input.doc;
    const language = this.language;
    const t = (key: TranslationKey): string => translate(language, key);
    const editor = shell(doc, language, "notification-phone", handlers);
    const body = editor.body;
    body.append(element(doc, "p", C.entityHelp, t(ROUTE_KEYS[phone.route])));

    const [onLabel, onBox] = this.checkbox(t("notifications.send"), phone.on, { phoneOn: "true" });
    const onGroup = element(doc, "fieldset", C.siteFieldset);
    onGroup.append(onLabel);
    body.append(onGroup);

    const eventBoxes: Array<[PhoneEvent, HTMLInputElement]> = [];
    for (const group of eventGroups(answer.loads)) {
      const fieldset = element(doc, "fieldset", C.siteFieldset);
      const heading = element(doc, "p", C.settingsNote, t(`notifications.group.${group.key}` as TranslationKey));
      heading.dataset["valueGroup"] = group.key;
      fieldset.append(heading);
      for (const event of group.events) {
        const [label, box] = this.checkbox(t(EVENT_KEYS[event]), phone.events.includes(event), { phoneEvent: event });
        fieldset.append(label);
        if (event === "plan_at_risk" && answer.loads.chargers > 0 && (answer.loads.spa || answer.loads.sauna)) {
          fieldset.append(element(doc, "p", C.entityHelp, t("notifications.atRiskHelp")));
        }
        eventBoxes.push([event, box]);
      }
      body.append(fieldset);
    }

    const chargerIds = answer.chargers.map((charger) => charger.id);
    const chargerBoxes: Array<[string, HTMLInputElement]> = [];
    if (answer.chargers.length > 1) {
      const fieldset = element(doc, "fieldset", C.siteFieldset);
      const heading = element(doc, "p", C.settingsNote, t("notifications.chargers"));
      heading.dataset["valueGroup"] = "chargers";
      fieldset.append(heading);
      for (const charger of answer.chargers) {
        const follows = phone.chargers === null || phone.chargers.includes(charger.id);
        const [label, box] = this.checkbox(charger.name, follows, { phoneCharger: charger.id });
        fieldset.append(label);
        chargerBoxes.push([charger.id, box]);
      }
      body.append(fieldset);
    }

    let link: HTMLSelectElement | null = null;
    const candidates = linkCandidates(phone, answer.phones);
    if (candidates.length > 0) {
      const id = `${this.input.idPrefix}-phone-link`;
      const label = element(doc, "label", C.settingsNote, t("notifications.sameAs"));
      label.htmlFor = id;
      link = doc.createElement("select");
      link.id = id;
      link.className = C.settingsInput;
      link.dataset["phoneLink"] = "true";
      const none = doc.createElement("option");
      none.value = "";
      none.textContent = t("notifications.sameAsNone");
      link.append(none);
      for (const other of candidates) {
        const option = doc.createElement("option");
        option.value = other.id;
        option.textContent = phoneName(language, other);
        link.append(option);
      }
      body.append(label, link);
    }

    // Not the editor's actions row (`takeActions` moves that one under the dialog): these act at once.
    const extras = element(doc, "div");
    extras.dataset["phoneActions"] = "true";
    const extra = (key: TranslationKey, action: string, changes: Record<string, unknown>): void => {
      const button = element(doc, "button", C.button, t(key)) as HTMLButtonElement;
      button.type = "button";
      button.dataset["phoneAction"] = action;
      button.addEventListener("click", () => {
        button.disabled = true;
        void this.write(phone.id, changes).then((message) => {
          button.disabled = false;
          if (message === null) {
            handlers.onDone();
          } else {
            editor.setError(message);
          }
        });
      });
      extras.append(button);
    };
    if (phone.companion !== null && phone.app !== null) {
      extra("notifications.unlink", "unlink", { unlink: true });
    }
    if (!phone.id.startsWith(UNTOUCHED)) {
      extra("notifications.forget", "forget", { forget: true });
    }
    if (extras.childElementCount > 0) {
      body.append(extras);
    }

    editor.submit(async () => {
      if (link !== null && link.value !== "") {
        return await this.write(phone.id, { link: link.value });
      }
      const changes = phoneChanges(phone, answer.loads, chargerIds, {
        on: onBox.checked,
        events: eventBoxes.filter(([, box]) => box.checked).map(([event]) => event),
        chargers: chargerBoxes.filter(([, box]) => box.checked).map(([id]) => id),
      });
      return changes === null ? null : await this.write(phone.id, changes);
    });
    return editor.form;
  }
}
