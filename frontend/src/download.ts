// Saving a file from the card: a Blob and a temporary link, the only way a card can hand a
// person a file without a server route. Kept apart so a test can replace it.

export function saveTextFile(doc: Document, filename: string, text: string, type = "text/csv;charset=utf-8"): void {
  const view = doc.defaultView;
  if (view === null) {
    throw new Error("no window to save from");
  }
  saveBlobFile(doc, filename, new view.Blob([text], { type }));
}

/** Save a Blob as `filename` through a temporary link (a picture, a text). */
export function saveBlobFile(doc: Document, filename: string, blob: Blob): void {
  const view = doc.defaultView;
  if (view === null) {
    throw new Error("no window to save from");
  }
  const url = view.URL.createObjectURL(blob);
  const link = doc.createElement("a");
  link.href = url;
  link.download = filename;
  link.hidden = true;
  doc.body.append(link);
  link.click();
  link.remove();
  // Revoked after the click has been handled.
  view.setTimeout(() => view.URL.revokeObjectURL(url), 0);
}
