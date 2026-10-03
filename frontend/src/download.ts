// Saving a text file from the card: a Blob and a temporary link, the only way a card can hand a
// person a file without a server route. Kept apart so a test can replace it.

export function saveTextFile(doc: Document, filename: string, text: string, type = "text/csv;charset=utf-8"): void {
  const view = doc.defaultView;
  if (view === null) {
    throw new Error("no window to save from");
  }
  const url = view.URL.createObjectURL(new view.Blob([text], { type }));
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
