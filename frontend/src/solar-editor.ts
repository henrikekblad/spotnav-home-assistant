// The Solar dialog: priority between the car and the battery, and the forecast sources, with Save and
// Cancel. The body reports a draft as text (`priority`, and `forecast:<id>` = "true"/"false" per source);
// the card turns it into one `spotnav/update_site_settings` request under compare-and-set.

import type { EntityDraft, EntityFieldError } from "./entity-config";
import type { EntityEditorBody, EntityEditorHandlers } from "./entity-editor";
import { translate, type Language } from "./i18n";
import type { SiteFacts } from "./model";
import { VISUAL_CLASSES as C } from "./visual-styles";

export const SOLAR_PRIORITY_KEY = "priority";
export const SOLAR_FORECAST_PREFIX = "forecast:";

export function solarDraftFrom(site: SiteFacts): EntityDraft {
  const draft: EntityDraft = { [SOLAR_PRIORITY_KEY]: site.solarPriority };
  for (const choice of site.solarForecastChoices) {
    draft[`${SOLAR_FORECAST_PREFIX}${choice.id}`] = String(choice.selected);
  }
  return draft;
}

function element(doc: Document, tag: string, className?: string, text?: string): HTMLElement {
  const node = doc.createElement(tag);
  if (className !== undefined) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

export function solarEditorBody(
  doc: Document,
  language: Language,
  site: SiteFacts,
  handlers: EntityEditorHandlers,
  idPrefix: string,
): EntityEditorBody {
  const values = solarDraftFrom(site);
  const controls: Array<HTMLElement & { disabled: boolean }> = [];
  let pending = false;

  const body = element(doc, "form") as HTMLFormElement;
  body.noValidate = true;
  body.dataset["entityEditor"] = "solar";
  const notice = element(doc, "p", C.settingsNotice);
  notice.setAttribute("role", "status");
  notice.hidden = true;
  body.append(notice);
  body.append(element(doc, "p", C.siteApplies, site.appliesToText));

  const priority = element(doc, "fieldset", C.siteFieldset);
  priority.dataset["part"] = "priority";
  priority.append(element(doc, "legend", C.siteLegend, translate(language, "site.solarPriority.title")));
  for (const value of ["car_first", "battery_first"] as const) {
    const label = element(doc, "label", C.siteChoice);
    const radio = doc.createElement("input");
    radio.type = "radio";
    radio.name = `${idPrefix}-solar-priority`;
    radio.value = value;
    radio.checked = site.solarPriority === value;
    radio.addEventListener("change", () => {
      if (radio.checked) {
        values[SOLAR_PRIORITY_KEY] = value;
      }
    });
    controls.push(radio);
    label.append(
      radio,
      doc.createTextNode(translate(language, value === "car_first" ? "site.solarPriority.carFirst" : "site.solarPriority.batteryFirst")),
    );
    priority.append(label);
  }
  body.append(priority);

  const forecast = element(doc, "fieldset", C.siteFieldset);
  forecast.dataset["part"] = "forecast";
  forecast.append(element(doc, "legend", C.siteLegend, translate(language, "site.solarForecast.title")));
  const none = element(doc, "p", C.settingsNote, translate(language, "site.solarForecast.none"));
  const paintNone = (): void => {
    none.hidden = !site.solarForecastChoices.every((choice) => values[`${SOLAR_FORECAST_PREFIX}${choice.id}`] !== "true");
  };
  for (const choice of site.solarForecastChoices) {
    const label = element(doc, "label", C.siteChoice);
    const box = doc.createElement("input");
    box.type = "checkbox";
    box.checked = choice.selected;
    box.dataset["forecast"] = choice.id;
    box.addEventListener("change", () => {
      values[`${SOLAR_FORECAST_PREFIX}${choice.id}`] = String(box.checked);
      paintNone();
    });
    controls.push(box);
    label.append(box, doc.createTextNode(choice.title));
    forecast.append(label);
  }
  forecast.append(none);
  paintNone();
  body.append(forecast);

  const actions = element(doc, "div", C.settingsActions);
  const save = element(doc, "button", `${C.button} ${C.settingsSave}`, translate(language, "settings.save")) as HTMLButtonElement;
  save.type = "submit";
  const cancel = element(doc, "button", C.button, translate(language, "settings.cancel")) as HTMLButtonElement;
  cancel.type = "button";
  actions.append(save, cancel);
  body.append(actions);
  controls.push(save, cancel);
  body.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!pending) {
      handlers.onSave({ ...values });
    }
  });
  cancel.addEventListener("click", () => {
    handlers.onCancel();
  });

  return {
    body,
    draft: () => ({ ...values }),
    markErrors(_errors: readonly EntityFieldError[]) {},
    setNotice(text, code) {
      notice.hidden = text === null;
      notice.textContent = text ?? "";
      if (code === null) {
        notice.removeAttribute("data-code");
      } else {
        notice.dataset["code"] = code;
      }
    },
    setPending(next) {
      pending = next;
      for (const control of controls) {
        control.disabled = pending;
      }
    },
    setHass() {},
  };
}
