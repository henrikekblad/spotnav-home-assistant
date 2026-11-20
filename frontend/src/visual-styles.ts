// The card's stylesheet and the class names it defines, shared with the renderers as data so a
// component cannot use a class the stylesheet lacks. Every value is a Home Assistant theme variable
// with a plain fallback or a relative/clamped size, so a 320 px column wraps instead of scrolling.
// Nothing external (no `@import`, `url(...)`, web font or image); dialogs are `position: fixed`
// overlays, so opening one never changes the card's height.

export * from "./visual-classes";
import { SETTINGS_FORM_STYLES } from "./visual-styles-settings";
import { CARD_STYLES } from "./visual-styles-card";
import { CHART_STYLES } from "./visual-styles-chart";
import { POPOVER_STYLES } from "./visual-styles-popover";
import { DIALOG_STYLES } from "./visual-styles-dialog";
import { PERIOD_CHART_STYLES } from "./visual-styles-period";
import { NOTICE_STYLES } from "./visual-styles-notices";

/** The whole stylesheet: the areas in cascade order. */
export const VISUAL_STYLES =
  SETTINGS_FORM_STYLES +
  CARD_STYLES +
  CHART_STYLES +
  POPOVER_STYLES +
  DIALOG_STYLES +
  PERIOD_CHART_STYLES +
  NOTICE_STYLES;
