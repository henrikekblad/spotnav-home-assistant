// Test helpers: a faked `hass` at the public boundary, and small v1 payloads.
//
// The fake records every message and hands back a promise the test resolves or rejects by hand,
// so a race is a stated order of events rather than a sleep.

import type { ChargerList, ChargerSummary, HomeAssistantLike } from "../src/types";
import { SpotnavCard } from "../src/card";
import { SpotnavCardEditor } from "../src/editor";
import { rawDashboard } from "./dashboard-fixtures";

interface Pending {
  message: Record<string, unknown>;
  resolve: (value: unknown) => void;
  reject: (reason: unknown) => void;
}

export class FakeHass implements HomeAssistantLike {
  readonly messages: Record<string, unknown>[] = [];
  /** Which snapshot issued each message, in order. */
  readonly callers: unknown[] = [];
  private readonly pending: Pending[] = [];
  /**
   * The entity-configuration commands are answered on their own line, never by the FIFO the other
   * tests resolve by hand: opening the Settings popover reads them as a side channel, and a test about
   * something else must not have its first `resolveNext` swallowed by it. `entityHandler` answers
   * them; the default never answers, which is "still reading".
   */
  readonly entityMessages: Record<string, unknown>[] = [];
  entityHandler: ((message: Record<string, unknown>) => Promise<unknown>) | null = null;

  callWS<T>(message: Record<string, unknown>): Promise<T> {
    return this.callWSFor(this, message);
  }

  /** The same queue and the same promise, recording which object asked for it. */
  callWSFor<T>(from: unknown, message: Record<string, unknown>): Promise<T> {
    if (message["type"] === "spotnav/get_entity_config" || message["type"] === "spotnav/update_entity_config" || message["type"] === "spotnav/choose_vehicle_soc" || message["type"] === "spotnav/update_vehicle") {
      this.entityMessages.push(message);
      return (this.entityHandler === null ? new Promise<unknown>(() => undefined) : this.entityHandler(message)) as Promise<T>;
    }
    this.messages.push(message);
    this.callers.push(from);
    return new Promise<T>((resolve, reject) => {
      this.pending.push({
        message,
        resolve: resolve as (value: unknown) => void,
        reject,
      });
    });
  }

  /** One HA state snapshot: a *new* object per state update, one shared fake transport. */
  snapshot(label = "snapshot", language?: string): HassSnapshot {
    return new HassSnapshot(this, label, language);
  }

  snapshots(count: number, prefix = "snapshot"): HassSnapshot[] {
    return Array.from({ length: count }, (_unused, index) => new HassSnapshot(this, `${prefix}-${index}`));
  }

  get outstanding(): number {
    return this.pending.length;
  }

  resolveNext(value: unknown): void {
    const next = this.pending.shift();
    if (next === undefined) {
      throw new Error("no request is in flight");
    }
    next.resolve(value);
  }

  /** Resolve the request at this position, to land answers out of order on purpose. */
  resolveAt(index: number, value: unknown): void {
    const target = this.pending[index];
    if (target === undefined) {
      throw new Error(`no request at ${index}`);
    }
    this.pending.splice(index, 1);
    target.resolve(value);
  }

  rejectAt(index: number, reason: unknown): void {
    const target = this.pending[index];
    if (target === undefined) {
      throw new Error(`no request at ${index}`);
    }
    this.pending.splice(index, 1);
    target.reject(reason);
  }

  rejectNext(reason: unknown): void {
    const next = this.pending.shift();
    if (next === undefined) {
      throw new Error("no request is in flight");
    }
    next.reject(reason);
  }
}

/**
 * A distinct `hass` object backed by one `FakeHass`.
 *
 * Home Assistant hands a custom card a *new* `hass` object as state changes; the object reference
 * is a state snapshot, never a connection identity. Tests use these to prove that ordinary state
 * updates during a pending request change nothing -- and to see which snapshot a later request
 * actually went through.
 */
export class HassSnapshot implements HomeAssistantLike {
  constructor(
    private readonly fake: FakeHass,
    readonly label: string,
    /** The Home Assistant language this snapshot carries, when a test cares about it. */
    readonly language?: string,
  ) {}

  /**
   * The Home Assistant user behind this snapshot, when a test asks about administration.
   *
   * `undefined` is the honest default: an older frontend (or a bare stub) carries no user, and the
   * card then offers no Save -- the backend's `require_admin` is the boundary either way.
   */
  user?: { is_admin?: boolean };

  callWS<T>(message: Record<string, unknown>): Promise<T> {
    return this.fake.callWSFor(this, message);
  }
}

/** An error shaped like the one Home Assistant's WebSocket layer rejects with. */
export function apiFailure(code: string, message = "refused"): Error {
  const error = new Error(message);
  (error as Error & { code?: string }).code = code;
  return error;
}

export function charger(id: string, name: string, available = true): ChargerSummary {
  return {
    charger_id: id,
    charger_name: name,
    available,
    capabilities: {
      auto_price: true,
      current_limit: false,
      set_current: false,
      regulated_current: false,
      target_soc: false,
      load_balancing: false,
      refresh_vehicle: false,
      set_charge_limit: false,
      target_stop: false,
    },
  };
}

export function chargerList(chargers: ChargerSummary[]): ChargerList {
  return { api_version: 2, chargers };
}

/**
 * A coherent, *decodable* v1 answer, overridable per test.
 *
 * Built from the shared dashboard fixture, so every production test feeds the card a payload the real
 * decoder accepts: the prototype's hand-written partial objects are now rejected as malformed, which
 * is exactly what the decoder is for. Overrides are shallow, so a test replacing a section spreads the
 * fixture's own section (`dashboard({ settings: { ...settingsOf(base), revision: 4 } })`).
 */
export function dashboard(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { ...rawDashboard(), ...overrides };
}

/** The fixture's own settings section, for tests that patch one field of it. */
export function settingsOf(payload: Record<string, unknown>): Record<string, unknown> {
  return { ...(payload.settings as Record<string, unknown>) };
}

/** The fixture's own plan section, for tests that patch periods or the relation. */
export function planOf(payload: Record<string, unknown>): Record<string, unknown> {
  return { ...(payload.plan as Record<string, unknown>) };
}

/** The fixture's own prices section. */
export function pricesOf(payload: Record<string, unknown>): Record<string, unknown> {
  return { ...(payload.prices as Record<string, unknown>) };
}

/** The fixture's own charger section. */
export function chargerOf(payload: Record<string, unknown>): Record<string, unknown> {
  return { ...(payload.charger as Record<string, unknown>) };
}

export function defineElements(): void {
  if (customElements.get("spotnav-card") === undefined) {
    customElements.define("spotnav-card", SpotnavCard);
  }
  if (customElements.get("spotnav-card-editor") === undefined) {
    customElements.define("spotnav-card-editor", SpotnavCardEditor);
  }
}

export function mountCard(config: unknown, hass: HomeAssistantLike | null = null): SpotnavCard {
  defineElements();
  const element = document.createElement("spotnav-card") as SpotnavCard;
  element.setConfig(config);
  if (hass !== null) {
    element.hass = hass;
  }
  document.body.appendChild(element);
  return element;
}

export function mountEditor(
  config: unknown,
  hass: HomeAssistantLike | null = null,
): SpotnavCardEditor {
  defineElements();
  const element = document.createElement("spotnav-card-editor") as SpotnavCardEditor;
  element.setConfig(config);
  if (hass !== null) {
    element.hass = hass;
  }
  document.body.appendChild(element);
  return element;
}
