// Issue lists, capability rows, the active notice, and the narrow-width and reduced-motion overrides.

import { VISUAL_CLASSES } from "./visual-classes";

export const NOTICE_STYLES = `\
  .${VISUAL_CLASSES.issueItem} {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.issueText} {
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.capabilityItem} {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
  }
  .${VISUAL_CLASSES.capabilityLabel} {
    overflow-wrap: break-word;
  }
  /*
   * A summary row on the Settings page: the label never breaks mid-label (ellipsis only as a last
   * resort); a short value sits right-aligned beside it. A value that does not fit beside the label
   * wraps to its own line (flex-wrap), and a value past LONG_VALUE_LENGTH characters is stacked
   * under the label left-aligned from the start (the Android app's threshold), so the two never
   * read as extra rows.
   */
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} {
    flex-wrap: wrap;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.capabilityLabel} {
    /* Muted, as in the app: a value stacked under its label must not read as one more label. */
    color: var(--secondary-text-color, #727272);
    flex: 0 1 auto;
    max-width: 100%;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.settingsValue} {
    flex: 1 1 auto;
    min-width: 0;
    text-align: right;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.settingsSection} .${VISUAL_CLASSES.capabilityItem} > .${VISUAL_CLASSES.settingsValueLong} {
    flex: 1 0 100%;
    text-align: left;
    margin: 1px 0 6px;
  }
  [data-slot='vehicles'] > .${VISUAL_CLASSES.settingsSection} {
    margin-top: 12px;
  }
  .${VISUAL_CLASSES.capabilityState} {
    flex: 0 0 auto;
    color: var(--secondary-text-color, #727272);
    font-size: 0.82rem;
  }
  .${VISUAL_CLASSES.capabilityNote} {
    display: block;
    color: var(--secondary-text-color, #727272);
    font-size: 0.78rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.activeNotice} {
    margin: 4px 0 0;
    padding: 6px 8px;
    border-inline-start: 3px solid var(--success-color, #43a047);
    font-size: 0.82rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.activeNotice} > span {
    display: block;
  }
  .${VISUAL_CLASSES.activeNoticeWarning} {
    border-inline-start-color: var(--warning-color, #b26a00);
  }
  @media (max-width: 400px) {
    .${VISUAL_CLASSES.card} {
      padding: 10px 11px;
    }
    .${VISUAL_CLASSES.figures} {
      gap: 6px 12px;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    * {
      transition: none !important;
      animation: none !important;
    }
    /* Nothing glides: the open bar stands as the whole track, softly. */
    .${VISUAL_CLASSES.chargeBar}[data-basis="open"] .${VISUAL_CLASSES.chargeBarFill} {
      width: 100%;
      opacity: 0.5;
      animation: none;
    }
  }
`;
