// The Electricity price section's source line and notice (`prices.provider`): where the area's prices come from,
// and, when the area has no source of its own, the one thing an administrator can do about it. The words are the
// card's; the backend sends a stable code and plain parameters only.

import { translate, type Language, type TranslationKey } from "./i18n";
import type { PriceProvider } from "./validate";
import { shell, type EditorHandlers, type SaveValue } from "./value-editors";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** Home Assistant's own pages (frontend 20260826, Home Assistant 2026.9): add an integration, open one. */
export function addIntegrationPath(domain: string): string {
  return `/config/integrations/dashboard/add?domain=${encodeURIComponent(domain)}`;
}

export function integrationPath(domain: string): string {
  return `/config/integrations/integration/${encodeURIComponent(domain)}`;
}

/** The Octopus Energy integration's repository: opened in HACS when HACS is there, else on GitHub. */
export const OCTOPUS_HACS_PATH =
  "/_my_redirect/hacs_repository?owner=BottlecapDave&repository=HomeAssistant-OctopusEnergy&category=integration";
/** Where an ENTSO-E key is registered for. */
export const ENTSOE_URL = "https://transparency.entsoe.eu/";
export const OCTOPUS_REPOSITORY_URL = "https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy";

/** The providers the card has a source line for. */
const SOURCE_LINES: Record<string, TranslationKey> = {
  nordpool: "price.provider.nordpool",
  entsoe: "price.provider.entsoe",
  pvpc: "price.provider.pvpc",
  octopus: "price.provider.octopus",
  relay: "price.provider.relay",
};

/** What a notice's button does. */
type NoticeAction =
  | { kind: "page"; path: string }
  | { kind: "octopus" }
  | { kind: "area" };

interface NoticeSpec {
  text: TranslationKey;
  action?: { label: TranslationKey; does: NoticeAction };
  /** The ENTSO-E key: offered as the alternative, or as the action itself. */
  key?: "offered" | "shown";
}

const NOTICES: Record<string, NoticeSpec> = {
  add_nordpool: {
    text: "price.notice.addNordpool",
    action: { label: "price.notice.addNordpool.action", does: { kind: "page", path: addIntegrationPath("nordpool") } },
    key: "offered",
  },
  nordpool_not_loaded: {
    text: "price.notice.nordpoolNotLoaded",
    action: { label: "price.notice.nordpoolNotLoaded.action", does: { kind: "page", path: integrationPath("nordpool") } },
    key: "offered",
  },
  add_entsoe_key: { text: "price.notice.addEntsoeKey", key: "shown" },
  entsoe_key_rejected: { text: "price.notice.entsoeKeyRejected", key: "shown" },
  add_pvpc: {
    text: "price.notice.addPvpc",
    action: {
      label: "price.notice.addPvpc.action",
      does: { kind: "page", path: addIntegrationPath("pvpc_hourly_pricing") },
    },
  },
  pvpc_not_loaded: {
    text: "price.notice.pvpcNotLoaded",
    action: {
      label: "price.notice.pvpcNotLoaded.action",
      does: { kind: "page", path: integrationPath("pvpc_hourly_pricing") },
    },
  },
  add_octopus: {
    text: "price.notice.addOctopus",
    action: { label: "price.notice.addOctopus.action", does: { kind: "octopus" } },
  },
  octopus_not_loaded: {
    text: "price.notice.octopusNotLoaded",
    action: {
      label: "price.notice.openOctopus",
      does: { kind: "page", path: integrationPath("octopus_energy") },
    },
  },
  octopus_no_agile: {
    text: "price.notice.octopusNoAgile",
    action: {
      label: "price.notice.openOctopus",
      does: { kind: "page", path: integrationPath("octopus_energy") },
    },
  },
  octopus_region_mismatch: {
    text: "price.notice.octopusRegionMismatch",
    action: { label: "price.notice.chooseArea", does: { kind: "area" } },
  },
  source_missing: {
    text: "price.notice.sourceMissing",
    action: { label: "price.notice.chooseArea", does: { kind: "area" } },
  },
};

export interface PriceProviderInput {
  language: Language;
  provider: PriceProvider | null;
  /** The area as the section names it, for the notices' words. */
  areaLabel: string;
  /** The area's id, to check a new key against. */
  areaId: string | null;
  isAdmin: boolean;
  idPrefix: string;
  /** Whether Home Assistant has loaded an integration (HACS, for the Octopus Energy button). */
  hasComponent: (domain: string) => boolean;
  /** Open one of Home Assistant's own pages. */
  navigate: (path: string) => void;
  /** Open an address outside Home Assistant, in a new tab. */
  openExternal: (url: string) => void;
  /** Open the area's own dialog. */
  openArea: () => void;
  /** Open the editor of the installation's ENTSO-E key ([keyEditor]). */
  openKeyEditor: () => void;
}

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const created = doc.createElement(tag);
  if (className !== undefined) {
    created.className = className;
  }
  if (text !== undefined) {
    created.textContent = text;
  }
  return created;
}

/** The source line, then the notice (an administrator's: anyone else reads the sentence without its buttons). */
export function priceProviderNodes(doc: Document, input: PriceProviderInput): HTMLElement[] {
  const provider = input.provider;
  if (provider === null) {
    return [];
  }
  const nodes: HTMLElement[] = [];
  const line = provider.key === null ? undefined : SOURCE_LINES[provider.key];
  if (line !== undefined) {
    const source = element(doc, "p", C.siteApplies, translate(input.language, line));
    source.dataset["priceProvider"] = provider.key ?? "";
    nodes.push(source);
  }
  const notice = provider.notice;
  const spec = notice === null ? undefined : NOTICES[notice.code];
  if (notice !== null && spec !== undefined) {
    nodes.push(noticeNode(doc, input, notice.code, notice.params, spec));
  }
  return nodes;
}

function noticeNode(
  doc: Document,
  input: PriceProviderInput,
  code: string,
  params: Record<string, string>,
  spec: NoticeSpec,
): HTMLElement {
  const say = (key: TranslationKey, values: Record<string, string> = {}): string =>
    translate(input.language, key, values);
  const box = element(doc, "div", C.suggestion);
  box.dataset["banner"] = "price";
  box.dataset["priceNotice"] = code;
  box.append(element(doc, "p", C.suggestionText, say(spec.text, { area: input.areaLabel, region: params["region"] ?? "" })));
  if (!input.isAdmin) {
    return box;
  }
  const answers = element(doc, "div", C.suggestionAnswers);
  const button = (label: TranslationKey, action: string, run: () => void): HTMLButtonElement => {
    const created = element(doc, "button", C.choiceButton) as HTMLButtonElement;
    created.type = "button";
    created.dataset["priceAction"] = action;
    created.append(element(doc, "span", C.choiceLabel, say(label)));
    created.addEventListener("click", run);
    answers.append(created);
    return created;
  };
  if (spec.action !== undefined) {
    const does = spec.action.does;
    button(spec.action.label, does.kind === "page" ? does.path : does.kind, () => {
      if (does.kind === "page") {
        input.navigate(does.path);
      } else if (does.kind === "octopus") {
        if (input.hasComponent("hacs")) {
          input.navigate(OCTOPUS_HACS_PATH);
        } else {
          input.openExternal(OCTOPUS_REPOSITORY_URL);
        }
      } else {
        input.openArea();
      }
    });
  }
  if (spec.key !== undefined) {
    const label: TranslationKey =
      spec.key === "offered"
        ? "price.notice.useEntsoeKey"
        : code === "entsoe_key_rejected"
          ? "price.notice.entsoeKeyRejected.action"
          : "price.notice.addEntsoeKey.action";
    button(label, "entsoe-key", () => input.openKeyEditor());
  }
  if (answers.childElementCount > 0) {
    box.append(answers);
  }
  return box;
}

/**
 * The installation's ENTSO-E key, in the value editors' own dialog: what it is for and how to get one, a password
 * field, and Save. The key is handed to `save` and forgotten; the field is never filled from anything stored.
 */
export function keyEditor(
  doc: Document,
  language: Language,
  idPrefix: string,
  save: SaveValue<string>,
  handlers: EditorHandlers,
): HTMLFormElement {
  const editor = shell(doc, language, "entsoeKey", handlers, translate(language, "price.key.save"));
  const field = doc.createElement("input");
  field.type = "password";
  field.id = `${idPrefix}-entsoe-key`;
  field.className = C.settingsInput;
  field.autocomplete = "off";
  field.spellcheck = false;
  field.dataset["valueField"] = "entsoeKey";
  const label = element(doc, "label", C.settingsLabel, translate(language, "price.key.label"));
  label.setAttribute("for", field.id);
  const howto = element(doc, "p", C.entityHelp);
  // The platform's host is a link (a new tab); the sentence around it is the reader's language.
  const marker = "\u0000";
  const [before, after = ""] = translate(language, "price.key.howto", { site: marker }).split(marker);
  const site = doc.createElement("a");
  site.href = ENTSOE_URL;
  site.target = "_blank";
  site.rel = "noopener noreferrer";
  site.textContent = "transparency.entsoe.eu";
  howto.append(before ?? "", site, after);
  howto.dataset["entsoeHowto"] = "";
  howto.id = `${field.id}-howto`;
  field.setAttribute("aria-describedby", howto.id);
  const line = element(doc, "div", C.settingsField);
  line.append(label, field);
  editor.body.append(howto, line);
  editor.submit(async () => {
    const key = field.value.trim();
    if (key === "") {
      return translate(language, "price.key.empty");
    }
    return await save(key);
  });
  return editor.form;
}
