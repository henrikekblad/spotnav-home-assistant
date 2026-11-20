// The dialog overlay and panel: header, close button, intro and body.

import { VISUAL_CLASSES } from "./visual-classes";

export const DIALOG_STYLES = `\
  .${VISUAL_CLASSES.overlay} {
    position: fixed;
    inset: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    background: var(--spotnav-overlay, rgba(0, 0, 0, 0.45));
  }
  .${VISUAL_CLASSES.overlay}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.dialog} {
    display: flex;
    flex-direction: column;
    gap: 0;
    width: 100%;
    max-width: 420px;
    max-height: 80vh;
    max-height: 80dvh;
    overflow: hidden;
    padding: 0;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    color: var(--primary-text-color, #212121);
    border-radius: 12px;
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.28);
  }
  /*
   * One header row: the title takes the width it needs and the close button keeps its 44x44 tap
   * target at the end of the same row; a long title wraps around the button, never under it.
   * The header is the panel's fixed top row and only the content below it scrolls, so the title and
   * the close stay in view however long the dialog is (no fixed or sticky positioning involved).
   * It has the panel's own background, and a divider appears under it once the content is scrolled.
   */
  .${VISUAL_CLASSES.dialogHeader} {
    position: relative;
    z-index: 1;
    flex: none;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    min-width: 0;
    padding: 14px 14px 6px;
    background: inherit;
    border-bottom: 1px solid transparent;
  }
  .${VISUAL_CLASSES.dialogScrolled} > .${VISUAL_CLASSES.dialogHeader} {
    border-bottom-color: var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  /* The scrolling part: the intro and the body, under the header. */
  .${VISUAL_CLASSES.dialogScroll} {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-height: 0;
    min-width: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    -webkit-overflow-scrolling: touch;
    padding: 0 14px 14px;
  }
  /*
   * The fixed bottom row (Save, Cancel): outside the scrolling part, so it is always in view. It has the panel's
   * own background and a thin divider on top, and keeps clear of a phone's home indicator.
   */
  .${VISUAL_CLASSES.dialogFooter} {
    flex: none;
    min-width: 0;
    padding: 8px 14px calc(12px + env(safe-area-inset-bottom, 0px));
    background: inherit;
    border-top: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  .${VISUAL_CLASSES.dialogFooter}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.dialogFooter} > .spotnav-settings-actions {
    margin-top: 4px;
  }
  .${VISUAL_CLASSES.dialogTitle} {
    flex: 1 1 auto;
    min-width: 0;
    margin: 0;
    font-size: 1rem;
    font-weight: 600;
    overflow-wrap: break-word;
  }
  /*
   * The close is a discreet cross: a 20 px icon in the muted text colour, no box and no border, in the header's
   * top-right corner beside the title. Its 44x44 tap target stays, pulled into the dialog's padding so the cross
   * lines up with the title rather than pushing it down.
   */
  .${VISUAL_CLASSES.dialogClose} {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 44px;
    min-height: 44px;
    margin: -12px -12px -12px 0;
    padding: 0;
    font: inherit;
    line-height: 0;
    color: var(--secondary-text-color, #727272);
    background: none;
    border: 0;
    border-radius: 50%;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.dialogClose}:hover {
    color: var(--primary-text-color, #212121);
  }
  .${VISUAL_CLASSES.dialogIntro} {
    margin: 0;
  }
  .${VISUAL_CLASSES.dialogBody} {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }
`;
