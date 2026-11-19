// The primitives every payload decoder is built from. A decoder throws `Malformed` (through `bad`) on
// the first field that is missing or has the wrong type, and its entry point catches that one class.
// Two shapes: `*At(source, key)` reads a key of a record; the plain names check a value already in hand.
// Whether unknown keys are allowed is each decoder's own policy (see contract.ts), not decided here.

import { hasAllKeys, hasUnknownKeys } from "./contract";

export class Malformed extends Error {
  constructor(message = "malformed") {
    super(message);
  }
}

export function bad(): never {
  throw new Malformed();
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : bad();
}

export function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : bad();
}

export function text(value: unknown): string {
  return typeof value === "string" ? value : bad();
}

export function textOrNull(value: unknown): string | null {
  return value === null ? null : text(value);
}

export function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : bad();
}

export function numberOrNull(value: unknown): number | null {
  return value === null ? null : number(value);
}

export function flag(value: unknown): boolean {
  return typeof value === "boolean" ? value : bad();
}

export function textAt(source: Record<string, unknown>, key: string): string {
  return text(source[key]);
}

export function textOrNullAt(source: Record<string, unknown>, key: string): string | null {
  return textOrNull(source[key]);
}

export function numberAt(source: Record<string, unknown>, key: string): number {
  return number(source[key]);
}

export function numberOrNullAt(source: Record<string, unknown>, key: string): number | null {
  return numberOrNull(source[key]);
}

export function flagAt(source: Record<string, unknown>, key: string): boolean {
  return flag(source[key]);
}

export function listAt(source: Record<string, unknown>, key: string): unknown[] {
  return list(source[key]);
}

/** Exactly these keys: none missing, none unknown. */
export function exactKeys(source: Record<string, unknown>, keys: readonly string[]): void {
  if (!hasAllKeys(source, keys) || hasUnknownKeys(source, keys)) {
    bad();
  }
}

export function textListAt(source: Record<string, unknown>, key: string): string[] {
  return listAt(source, key).map(text);
}
