// Bundle entry point: defines the two elements and publishes one picker entry. Evaluating
// the asset twice must be harmless (`customElements.define` throws on a duplicate and
// `customCards.push` would list the card twice), so both are guarded; `defineCard()` is
// exported for tests.

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

defineCard();
