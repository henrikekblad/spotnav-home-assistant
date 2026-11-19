// The card's one policy for keys a decoder does not know: in production they are IGNORED, so an additive
// backend key never blanks a card (a missing or mistyped KNOWN key still rejects). The tests switch
// `contract.strict` on (`test/setup-languages.ts`), so a fixture that carries a key the decoder does not
// know fails in CI and contract drift is caught there, not on a user's dashboard.

export const contract = { strict: false };

/** Whether `source` carries a key outside `keys` while strict mode is on (never in production). */
export function hasUnknownKeys(source: Record<string, unknown>, keys: readonly string[]): boolean {
  return contract.strict && Object.keys(source).some((key) => !keys.includes(key));
}

/** Whether every key in `keys` is an own key of `source` (the part of the contract kept in production). */
export function hasAllKeys(source: Record<string, unknown>, keys: readonly string[]): boolean {
  return keys.every((key) => Object.prototype.hasOwnProperty.call(source, key));
}
