// Bundle entry point: defines the four elements (the charging card, its editor, the house card and the heat card)
// and publishes a picker entry for each card. Evaluating
// the asset twice must be harmless (`customElements.define` throws on a duplicate and
// `customCards.push` would list the card twice), so both are guarded; `defineCard()` is
// exported for tests.
//
// Home Assistant's core script replaces `window.customElements` with a scoped-registry polyfill.
// The card is loaded as an extra module beside that script and, from the cache, can run first; a
// definition made then lands in the native registry, which the polyfill does not read, and the
// dashboard shows "Configuration error". So the elements are defined once that registry is in place.

import { SpotnavCard } from "./card";
import { SpotnavCardEditor } from "./editor";
import { HEAT_CARD_ELEMENT, HEAT_CARD_TYPE, SpotnavHeatCard } from "./heat-card";
import { HOUSE_CARD_ELEMENT, HOUSE_CARD_TYPE, SpotnavHouseCard } from "./house-card";
import { CARD_EDITOR_ELEMENT, CARD_ELEMENT, CARD_TYPE } from "./types";

export { SpotnavCard, SpotnavCardEditor, SpotnavHeatCard, SpotnavHouseCard };

export const CARD_PICKER_ENTRY = {
  type: CARD_TYPE,
  name: "SpotNav",
  description: "Charging plan and electricity prices from SpotNav.",
  preview: true,
  documentationURL: "https://github.com/henrikekblad/spotnav-home-assistant",
} as const;

/** The house card's entry (SpotNav 3.0 preview): the house's load, the sun and the battery plan, in shadow. */
export const HOUSE_PICKER_ENTRY = {
  type: HOUSE_CARD_TYPE,
  name: "SpotNav house",
  description: "The house's load forecast, the sun and the battery plan from SpotNav, in shadow.",
  preview: true,
  documentationURL: "https://github.com/henrikekblad/spotnav-home-assistant",
} as const;

/** The heat card's entry (SpotNav 3.0 preview): the wanted temperature, home or away, and the day's heat, in shadow. */
export const HEAT_PICKER_ENTRY = {
  type: HEAT_CARD_TYPE,
  name: "SpotNav heating",
  description: "The wanted house temperature, home or away, and what SpotNav would do with the heat, in shadow.",
  preview: true,
  documentationURL: "https://github.com/henrikekblad/spotnav-home-assistant",
} as const;

export function defineCard(): void {
  if (customElements.get(CARD_ELEMENT) === undefined) {
    customElements.define(CARD_ELEMENT, SpotnavCard);
  }
  if (customElements.get(CARD_EDITOR_ELEMENT) === undefined) {
    customElements.define(CARD_EDITOR_ELEMENT, SpotnavCardEditor);
  }
  if (customElements.get(HOUSE_CARD_ELEMENT) === undefined) {
    customElements.define(HOUSE_CARD_ELEMENT, SpotnavHouseCard);
  }
  if (customElements.get(HEAT_CARD_ELEMENT) === undefined) {
    customElements.define(HEAT_CARD_ELEMENT, SpotnavHeatCard);
  }
  const cards = window.customCards ?? [];
  for (const entry of [CARD_PICKER_ENTRY, HOUSE_PICKER_ENTRY, HEAT_PICKER_ENTRY]) {
    if (!cards.some((listed) => listed.type === entry.type)) {
      cards.push({ ...entry });
    }
  }
  window.customCards = cards;
}

/** True once the registry Home Assistant's elements are defined in is the one in `window`. */
export function registryReady(): boolean {
  return (
    window.CustomElementRegistryPolyfill !== undefined ||
    customElements.get("home-assistant") !== undefined
  );
}

export const REGISTRY_POLL_MS = 50;
/** A frontend without the polyfill that never defines `home-assistant` still gets the card. */
export const REGISTRY_WAIT_MS = 10_000;

export function defineCardWhenReady(): void {
  const started = Date.now();
  const attempt = (): void => {
    if (registryReady() || Date.now() - started >= REGISTRY_WAIT_MS) {
      defineCard();
    } else {
      setTimeout(attempt, REGISTRY_POLL_MS);
    }
  };
  attempt();
}

defineCardWhenReady();
