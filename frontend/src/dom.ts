// Safe-markup helpers: `escapeHtml` for string-rendered views (escapes `&` first so later
// escapes are not re-escaped), and DOM builders that use `textContent` / option value
// properties, so no value can become markup. `test/injection.test.ts` drives both with hostile input.

export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function textParagraph(text: string, className?: string): HTMLParagraphElement {
  const element = document.createElement("p");
  if (className !== undefined) {
    element.className = className;
  }
  element.textContent = text;
  return element;
}

export function labelledButton(label: string, accessibleName: string): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("aria-label", accessibleName);
  return button;
}

/** One `<option>`: label as text, config-entry id as a value property; neither is parsed as markup. */
export function chargerOption(label: string, value: string): HTMLOptionElement {
  return new Option(label, value);
}
