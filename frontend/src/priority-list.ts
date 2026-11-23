// The site's one priority list ("Prioritet", backend `site/priority.py`): one row per load the site has, top first.
// The top gets energy first (the sun's surplus, the fuse) and gives way last (the import target's ceiling). Shown in
// the site settings; an administrator reorders it with up/down buttons and saves it through
// `spotnav/update_site_settings` (`priority`, compare-and-set on the order last seen). Pure DOM and decoding: the
// popover owns the dialog and the call.

import { translate, type Language, type TranslationKey } from "./i18n";
import { VISUAL_CLASSES as C } from "./visual-classes";
import { element } from "./dom";
import { shell, type EditorHandlers } from "./value-editors";
import type { SitePriority, SitePriorityRow } from "./site-model";
import { SITE_SETTINGS_API_VERSION } from "./types";

const CHARGER_PREFIX = "charger:";

/** The fixed rows the card names (a charger row is named by Home Assistant). */
const ROW_KEYS: Record<string, TranslationKey> = {
  heat: "site.priority.row.heat",
  spa: "site.priority.row.spa",
  sauna: "site.priority.row.sauna",
  cooling: "site.priority.row.cooling",
  water_heater: "site.priority.row.waterHeater",
  battery: "site.priority.row.battery",
  spa_idle: "site.priority.row.spaIdle",
  pool_circulation: "site.priority.row.poolCirculation",
  pool_heat: "site.priority.row.poolHeat",
};

/** A row's name: a charger's own title, else the card's word for the load (an id it does not know as itself). */
export function priorityRowLabel(language: Language, row: SitePriorityRow): string {
  if (row.id.startsWith(CHARGER_PREFIX)) {
    return row.name ?? translate(language, "site.priority.row.charger");
  }
  const key = ROW_KEYS[row.id];
  return key === undefined ? row.id : translate(language, key);
}

/** The row's value on the settings page: the top row "first" (one row alone: just its name). */
export function prioritySummary(language: Language, block: SitePriority): string {
  const first = block.order[0];
  if (first === undefined) {
    return translate(language, "settings.value.none");
  }
  const name = priorityRowLabel(language, first);
  return block.order.length === 1 ? name : translate(language, "site.priority.summary", { first: name });
}

/** The order moved: `index` one step up (`-1`) or down (`1`); the same order where it cannot move. */
export function moved(order: readonly string[], index: number, step: -1 | 1): string[] {
  const out = [...order];
  const other = index + step;
  if (index < 0 || index >= out.length || other < 0 || other >= out.length) {
    return out;
  }
  [out[index], out[other]] = [out[other] as string, out[index] as string];
  return out;
}

/** The write's body: the order last seen as `expected`, the new one as `changes`; `null` when nothing moved. */
export function priorityChange(
  confirmed: readonly string[],
  chosen: readonly string[],
): { expected: { priority: string[] }; changes: { priority: string[] } } | null {
  if (confirmed.length === chosen.length && confirmed.every((id, index) => chosen[index] === id)) {
    return null;
  }
  return { expected: { priority: [...confirmed] }, changes: { priority: [...chosen] } };
}

/** The answer to a priority write: `ok`, or the refusal's code (`null` for an answer that does not fit). */
export function decodePriorityAnswer(raw: unknown): { ok: true } | { ok: false; code: string | null } {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, code: null };
  }
  const answer = raw as Record<string, unknown>;
  if (answer["api_version"] !== SITE_SETTINGS_API_VERSION || typeof answer["ok"] !== "boolean") {
    return { ok: false, code: null };
  }
  if (answer["ok"] === true) {
    return { ok: true };
  }
  return { ok: false, code: typeof answer["error"] === "string" ? answer["error"] : null };
}

/**
 * The editor: what the order means in one line, then the rows with an up and a down button each (the top row has
 * no up, the bottom no down). Save hands the new order to `save`, which answers `null` when it took.
 */
export function priorityEditor(
  doc: Document,
  language: Language,
  block: SitePriority,
  idPrefix: string,
  save: (order: string[]) => Promise<string | null>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "priority", handlers);
  editor.body.append(element(doc, "p", C.settingRowHelp, translate(language, "site.priority.intro")));
  const list = element(doc, "ol", C.settingRowHelp);
  list.dataset["priorityList"] = "true";
  list.style.listStyle = "none";
  list.style.padding = "0";
  editor.body.append(list);
  const rows = new Map(block.order.map((row) => [row.id, row]));
  let order = block.order.map((row) => row.id);

  const button = (text: string, label: string, disabled: boolean, onTap: () => void): HTMLButtonElement => {
    const node = element(doc, "button", C.iconButton, text) as HTMLButtonElement;
    node.type = "button";
    node.disabled = disabled;
    node.setAttribute("aria-label", label);
    node.addEventListener("click", onTap);
    return node;
  };

  const paint = (): void => {
    list.replaceChildren(
      ...order.map((id, index) => {
        const name = priorityRowLabel(language, rows.get(id) ?? { id, name: null });
        const item = element(doc, "li", C.settingRow);
        item.dataset["priorityRow"] = id;
        item.id = `${idPrefix}-priority-${index}`;
        const label = element(doc, "span", C.settingRowLabel, `${index + 1}. ${name}`);
        const up = button("▲", translate(language, "site.priority.up", { name }), index === 0, () => {
          order = moved(order, index, -1);
          paint();
        });
        up.dataset["move"] = "up";
        const down = button("▼", translate(language, "site.priority.down", { name }), index === order.length - 1, () => {
          order = moved(order, index, 1);
          paint();
        });
        down.dataset["move"] = "down";
        const buttons = element(doc, "span");
        buttons.append(up, down);
        item.append(label, buttons);
        return item;
      }),
    );
  };
  paint();
  editor.submit(async () => await save(order));
  return editor.form;
}
