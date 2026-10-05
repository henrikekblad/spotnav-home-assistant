// Bundle entry point: defines the two elements and publishes one picker entry. Evaluating
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
import { CARD_EDITOR_ELEMENT, CARD_ELEMENT, CARD_TYPE } from "./types";

export { SpotnavCard, SpotnavCardEditor };

export const CARD_PICKER_ENTRY = {
  type: CARD_TYPE,
  name: "SpotNav",
  description: "Charging plan and electricity prices from SpotNav.",
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
  const cards = window.customCards ?? [];
  if (!cards.some((entry) => entry.type === CARD_TYPE)) {
    cards.push({ ...CARD_PICKER_ENTRY });
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
