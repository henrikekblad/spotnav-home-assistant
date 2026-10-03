// The "Download debug info" button's two plain steps: read the backend's answer strictly, and hand the
// bundle to the browser as a JSON file named `spotnav-debug-<date>.json`. The card never edits or
// inspects the bundle; it is what the backend redacted, saved as it came.

import { DEBUG_API_VERSION } from "./types";

export type DebugAnswer =
  | { ok: true; bundle: Record<string, unknown> }
  | { ok: false; code: string | null };

/** `{api_version, ok, error, bundle}` or nothing: a malformed or newer answer is `null`. */
export function decodeDebugAnswer(raw: unknown): DebugAnswer | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const record = raw as Record<string, unknown>;
  if (record.api_version !== DEBUG_API_VERSION || typeof record.ok !== "boolean") {
    return null;
  }
  if (record.ok) {
    const bundle = record.bundle;
    if (typeof bundle !== "object" || bundle === null || Array.isArray(bundle)) {
      return null;
    }
    return { ok: true, bundle: bundle as Record<string, unknown> };
  }
  return { ok: false, code: typeof record.error === "string" ? record.error : null };
}

export function debugFileName(now: Date): string {
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `spotnav-debug-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}.json`;
}

/** Save `bundle` as a download. Returns false when the browser cannot make a file URL. */
export function saveDebugBundle(doc: Document, bundle: Record<string, unknown>, now: Date): boolean {
  const view = doc.defaultView;
  if (view === null || typeof view.URL?.createObjectURL !== "function") {
    return false;
  }
  const blob = new view.Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" });
  const url = view.URL.createObjectURL(blob);
  const link = doc.createElement("a");
  link.href = url;
  link.download = debugFileName(now);
  link.hidden = true;
  doc.body.append(link);
  link.click();
  link.remove();
  view.setTimeout(() => view.URL.revokeObjectURL(url), 0);
  return true;
}
