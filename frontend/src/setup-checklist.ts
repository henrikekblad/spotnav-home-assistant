// "Get started": what is still left to set up for this charger (the dashboard's `setup` block), shown to an
// administrator in the card's attention line and the "What needs attention" dialog. Each row says what is
// missing and has one button to the place where it is done; an optional row can also be answered "Not needed",
// which the integration keeps for the whole installation. The backend decides which rows are left; the words
// and the places are the card's. A grid sign row (the site's grid reading looks to have its sign the wrong way round)
// names the one site option its button writes and the value; nothing is written unless a person presses it.

import { pluralForm, translate, type Language, type TranslationKey } from "./i18n";
import { addIntegrationPath } from "./price-provider";
import type { SetupBlock } from "./validate";
import { VISUAL_CLASSES as C } from "./visual-styles";

/** The rows this card knows, in the order it lists them. */
export const SETUP_ROWS = [
  "vehicle",
  "car_level",
  "site",
  "grid_power_sign",
  "grid_current_sign",
  "solar_forecast",
  "demand_grid_power",
  "phase_switching",
  "phone",
  "app",
] as const;
export type SetupRowId = (typeof SETUP_ROWS)[number];

export interface SetupRow {
  id: SetupRowId;
  dismissible: boolean;
  /** The site option a grid sign row's button writes, and the value; absent on every other row. */
  write?: { field: string; value: boolean } | null;
}

/** The rows whose button writes a site option rather than opening a place. */
const WRITING_ROWS: ReadonlySet<SetupRowId> = new Set(["grid_power_sign", "grid_current_sign"]);

/** The SpotNav app on Google Play. */
export const APP_STORE_URL = "https://play.google.com/store/apps/details?id=se.sensnology.spotnav";
/** Home Assistant's integrations page, where a car's integration is added. */
export const INTEGRATIONS_PATH = "/config/integrations/dashboard";
/** The solar forecast integration offered when none is there yet. */
export const FORECAST_DOMAIN = "forecast_solar";

const WORDS: Record<SetupRowId, { text: TranslationKey; action: TranslationKey }> = {
  vehicle: { text: "setup.vehicle.text", action: "setup.vehicle.action" },
  car_level: { text: "setup.carLevel.text", action: "setup.carLevel.action" },
  site: { text: "setup.site.text", action: "setup.site.action" },
  grid_power_sign: { text: "setup.gridPowerSign.text", action: "setup.gridPowerSign.turnOff" },
  grid_current_sign: { text: "setup.gridCurrentSign.text", action: "setup.gridCurrentSign.turnOff" },
  solar_forecast: { text: "setup.solarForecast.text", action: "setup.solarForecast.action" },
  demand_grid_power: { text: "setup.demandGridPower.text", action: "setup.demandGridPower.action" },
  phase_switching: { text: "setup.phaseSwitching.text", action: "setup.phaseSwitching.action" },
  phone: { text: "setup.phone.text", action: "setup.phone.action" },
  app: { text: "setup.app.text", action: "setup.app.action" },
};

/** The rows of the `setup` block this card can word, in its own order; a row it does not know is left out. */
export function setupRowsOf(block: SetupBlock | null): SetupRow[] {
  if (block === null) {
    return [];
  }
  return SETUP_ROWS.flatMap((id) => {
    const found = block.items.find((item) => item.id === id);
    const write = found?.write ?? null;
    if (found === undefined || WRITING_ROWS.has(id) !== (write !== null)) {
      return [];
    }
    return [write === null ? { id, dismissible: found.dismissible } : { id, dismissible: found.dismissible, write }];
  });
}

/** "Get started: 3 things left", in the language's own plural. */
export function setupLineText(language: Language, count: number): string {
  const key: TranslationKey = pluralForm(language, count) === "one" ? "setup.line.one" : "setup.line.other";
  return translate(language, key, { count: String(count) });
}

export interface SetupSectionInput {
  language: Language;
  rows: readonly SetupRow[];
  /** Open the Plan popover (where the car's charge-level sensor is chosen). */
  openPlan: () => void;
  /** Open Settings at one of its sections and, once it is there, the editor of one of its rows (`data-edit`). */
  openSetting: (section: string, edit: string) => void;
  /** Whether a solar forecast source can be chosen already (one is added to Home Assistant). */
  forecastChoosable: boolean;
  /** Move to one of Home Assistant's own pages. */
  navigate: (path: string) => void;
  openExternal: (url: string) => void;
  /** Whether the section has its own "Get started" heading: when it shares the dialog with reported issues. */
  heading: boolean;
  /** "Not needed": the card asks the integration to keep the row away. */
  dismiss: (id: SetupRowId) => void;
  /** A grid sign row's button: the card writes that one site option, then reads the dashboard again. */
  writeSiteOption: (field: string, value: boolean) => void;
}

/**
 * The dialog's "Get started" section: one box per thing left, its sentence and below it its buttons, headed
 * "Get started" when the dialog also lists issues (alone, the dialog's title says it).
 */
export function setupSection(doc: Document, input: SetupSectionInput): HTMLElement {
  const say = (key: TranslationKey): string => translate(input.language, key);
  const section = doc.createElement("section");
  section.dataset["section"] = "setup";
  if (input.heading) {
    const heading = doc.createElement("h3");
    heading.className = C.setupHeading;
    heading.textContent = say("setup.heading");
    section.append(heading);
  }
  for (const row of input.rows) {
    const box = doc.createElement("div");
    box.className = C.suggestion;
    box.dataset["setup"] = row.id;
    box.dataset["banner"] = "setup-row";
    const text = doc.createElement("p");
    text.className = C.suggestionText;
    text.textContent = say(WORDS[row.id].text);
    box.append(text);
    const answers = doc.createElement("div");
    answers.className = C.suggestionAnswers;
    const button = (label: TranslationKey, action: string, run: () => void): void => {
      const created = doc.createElement("button");
      created.type = "button";
      created.className = C.choiceButton;
      created.dataset["setupAction"] = action;
      const span = doc.createElement("span");
      span.className = C.choiceLabel;
      span.textContent = say(label);
      created.append(span);
      created.addEventListener("click", run);
      answers.append(created);
    };
    const write = row.write ?? null;
    if (write !== null) {
      const label: TranslationKey =
        row.id === "grid_power_sign" && write.value ? "setup.gridPowerSign.turnOn" : WORDS[row.id].action;
      button(label, "write", () => input.writeSiteOption(write.field, write.value));
    } else {
      const action =
        row.id === "solar_forecast" && !input.forecastChoosable ? "setup.solarForecast.add" : WORDS[row.id].action;
      button(action, "open", () => open(input, row.id));
    }
    if (row.dismissible) {
      button("setup.dismiss", "dismiss", () => input.dismiss(row.id));
    }
    box.append(answers);
    section.append(box);
  }
  return section;
}

function open(input: SetupSectionInput, id: SetupRowId): void {
  switch (id) {
    case "vehicle":
      input.navigate(INTEGRATIONS_PATH);
      return;
    case "car_level":
      input.openPlan();
      return;
    case "site":
      input.navigate(addIntegrationPath("spotnav"));
      return;
    case "solar_forecast":
      // With a forecast integration already added the site's choice of source is the place; without one, adding
      // Forecast.Solar comes first (the row then stays, and its button then opens the choice).
      if (input.forecastChoosable) {
        input.openSetting("solar", "solar_forecast");
      } else {
        input.navigate(addIntegrationPath(FORECAST_DOMAIN));
      }
      return;
    case "grid_power_sign":
    case "grid_current_sign":
      // Their button writes the option itself (`writeSiteOption`); the site's settings are where it is otherwise.
      input.openSetting("site", "measurement_mode");
      return;
    case "demand_grid_power":
      // The site's measurement row opens its entity dialog, where the total grid power is chosen.
      input.openSetting("site", "measurement_mode");
      return;
    case "phase_switching":
      input.openSetting("entities", "phase_switching");
      return;
    case "phone":
      input.openSetting("notifications", "notification_targets");
      return;
    case "app":
      input.openExternal(APP_STORE_URL);
      return;
  }
}
