// The card shell, its header, banners, status line and the charge bar.

import { VISUAL_CLASSES, HEADER_NARROW_PX } from "./visual-classes";

export const CARD_STYLES = `\
  /*
   * height: 100% on the host and the shell is auto in an ordinary column, and the box's own height when
   * the container gives the card one (a layout card on a wall screen, a sections view with a number of
   * rows). The view compares that box with the card's natural height and fits the card to it.
   */
  :host {
    display: block;
    box-sizing: border-box;
    max-width: 100%;
    height: 100%;
  }
  *, *::before, *::after {
    box-sizing: border-box;
  }
  .${VISUAL_CLASSES.card} {
    box-sizing: border-box;
    max-width: 100%;
    min-width: 0;
    padding: 12px 14px;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    color: var(--primary-text-color, #212121);
    border-radius: var(--ha-card-border-radius, 12px);
    border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color, transparent));
    box-shadow: var(--ha-card-box-shadow, none);
    font-size: 0.95rem;
    line-height: 1.35;
  }
  .${VISUAL_CLASSES.shell} {
    display: block;
    min-width: 0;
    height: 100%;
  }
  /*
   * The card in a box of fixed height (card-fill.ts): it takes the box's height, the chart is drawn at the
   * height left over, the status sentence keeps the lines that fit (an ellipsis ends the last, the whole
   * sentence is its title), and any height still left goes above the action bar, so the bar sits at the
   * box's bottom edge.
   */
  .${VISUAL_CLASSES.card}[data-fill] {
    height: 100%;
  }
  .${VISUAL_CLASSES.card}[data-fill] .${VISUAL_CLASSES.status}[data-lines] {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: var(--spotnav-status-lines, 1);
    line-clamp: var(--spotnav-status-lines, 1);
    overflow: hidden;
  }
  /* The readout line of a tapped hour is drawn over the chart's empty bottom edge (below the axis labels), not
     reserved below the chart, so the height it would take goes to the chart. Empty, it shows nothing. */
  .${VISUAL_CLASSES.card}[data-fill] .${VISUAL_CLASSES.readout} {
    position: relative;
    margin: -1.4em 0 0;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
  }
  .${VISUAL_CLASSES.card}[data-fill] .${VISUAL_CLASSES.readout}:empty {
    background: transparent;
  }
  .${VISUAL_CLASSES.card}[data-fill] .${VISUAL_CLASSES.actionBarWrap} {
    margin-top: calc(8px + var(--spotnav-fill-spare, 0px));
  }
  /* The header is a size container: what it shows follows the card's width, not the viewport. */
  .${VISUAL_CLASSES.header} {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    min-width: 0;
    container-type: inline-size;
  }
  .${VISUAL_CLASSES.identity} {
    flex: 0 0 auto;
    font-weight: 600;
    letter-spacing: 0.01em;
    color: var(--primary-color, #03a9f4);
  }
  /* A narrow card (a phone, the card picker's preview) gives the mark's room to the charger's name; a header with
     no name (a card with no charger yet) keeps its mark. */
  @container (max-width: ${HEADER_NARROW_PX - 1}px) {
    .${VISUAL_CLASSES.identity}:has(~ .${VISUAL_CLASSES.name}) {
      display: none;
    }
  }
  /* A modest basis in rem (not em of the large title font): a phone-wide card keeps name, vehicle line and
     both buttons on one row, and only a very narrow card moves the button group to a row of its own. */
  .${VISUAL_CLASSES.name} {
    flex: 1 1 8rem;
    min-width: 0;
    /* No heading margins, also on a name without its status (the charge bar showing): every card's header row is
       as tall as its buttons, whatever it shows after the name. */
    margin: 0;
    line-height: 1.25;
    font-weight: 500;
    overflow-wrap: break-word;
  }
  /* The header's buttons wrap only as a unit, never one by one, aligned to the end. */
  .${VISUAL_CLASSES.headerActions} {
    display: flex;
    flex: none;
    gap: 4px;
    margin-inline-start: auto;
    margin-inline-end: -4px;
  }
  /*
   * The header's history and cog buttons: a flex container centres the icon on both axes, and
   * line-height 0 stops the empty line box pushing it off centre. The retry button shares the class.
   */
  /* An author display beats the browser's [hidden]; a reader who may change nothing gets no cog. */
  .${VISUAL_CLASSES.iconButton}[hidden] {
    display: none;
  }
  .${VISUAL_CLASSES.iconButton} {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    min-width: 44px;
    min-height: 44px;
    margin: 0;
    padding: 0;
    line-height: 0;
    box-sizing: border-box;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.iconButton} > svg {
    display: block;
    margin: 0;
    vertical-align: baseline;
    width: 1.25em;
    height: 1.25em;
    flex: none;
  }
  .${VISUAL_CLASSES.iconButton}:focus-visible,
  .${VISUAL_CLASSES.banner}:focus-visible,
  .${VISUAL_CLASSES.dialogClose}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  /*
   * In the header the two buttons are plain icons, as Home Assistant's own ha-icon-button: no frame and no fill,
   * a 32 px round hover and focus mark around a 22 px glyph, and a 40 px tap target (the mark's 4 px of padding
   * around it). The group sits 4 px into the card's padding so the glyph lines up with the content's edge,
   * and the narrow pair leaves the charger's name its row.
   */
  .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton} {
    position: relative;
    min-width: 40px;
    min-height: 40px;
    color: var(--secondary-text-color, #727272);
    background: none;
    border: 0;
    border-radius: 50%;
  }
  .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton}::before {
    content: "";
    position: absolute;
    inset: 4px;
    border-radius: 50%;
    pointer-events: none;
  }
  /* Hover only where there is one: on a touch screen a tapped button would keep its mark. */
  @media (hover: hover) {
    .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton}:hover {
      color: var(--primary-text-color, #212121);
    }
    .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton}:hover::before {
      background: color-mix(in srgb, currentColor 12%, transparent);
    }
  }
  .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton}:focus-visible::before {
    background: color-mix(in srgb, currentColor 12%, transparent);
  }
  .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton}:focus-visible {
    outline: none;
  }
  .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton}:focus-visible::before {
    outline: 2px solid var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.headerActions} > .${VISUAL_CLASSES.iconButton} > svg {
    width: 1.375rem;
    height: 1.375rem;
  }
  .${VISUAL_CLASSES.infoButton} {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    min-width: 40px;
    min-height: 40px;
    margin: -8px 0;
    padding: 0;
    line-height: 0;
    box-sizing: border-box;
    color: var(--secondary-text-color, #727272);
    background: none;
    border: 0;
    border-radius: 50%;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.infoButton}[aria-expanded="true"] {
    color: var(--primary-color, #03a9f4);
  }
  .${VISUAL_CLASSES.infoButton} > svg {
    display: block;
    width: 1.5rem;
    height: 1.5rem;
  }
  .${VISUAL_CLASSES.infoButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
  .${VISUAL_CLASSES.banner} {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    width: 100%;
    margin: 10px 0 0;
    padding: 8px 10px;
    font: inherit;
    text-align: start;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.bannerBlocking} {
    border-color: var(--error-color, #db4437);
    color: var(--error-color, #db4437);
  }
  .${VISUAL_CLASSES.bannerNotice} {
    color: var(--secondary-text-color, #727272);
  }
  .${VISUAL_CLASSES.bannerCount} {
    flex: 0 0 auto;
    color: var(--secondary-text-color, #727272);
    font-size: 0.8rem;
  }
  .${VISUAL_CLASSES.nameBlock} {
    flex: 1 1 8rem;
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
  }
  .${VISUAL_CLASSES.nameBlock} > .${VISUAL_CLASSES.name} {
    /* A two-line title: no heading margins, so the vehicle line reads as the name's subtitle. */
    margin: 0;
    line-height: 1.25;
    flex: 0 0 auto;
    max-width: 100%;
  }
  /* The charger's name and its status after it; the status wraps under the name as a whole when the name is long. */
  .${VISUAL_CLASSES.nameRow} {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    column-gap: 4px;
    max-width: 100%;
    min-width: 0;
  }
  .${VISUAL_CLASSES.nameRow} > .${VISUAL_CLASSES.name} {
    margin: 0;
    line-height: 1.25;
    flex: 0 1 auto;
    max-width: 100%;
    min-width: 0;
  }
  /* The status as its word, or (too narrow for the word) as its symbol right after the name. The hidden word
     keeps its width, so the card measures the same whichever form shows. */
  .${VISUAL_CLASSES.nameRow} {
    position: relative;
  }
  .${VISUAL_CLASSES.nameRow}[data-status-fit="icon"] {
    flex-wrap: nowrap;
    align-items: center;
  }
  .${VISUAL_CLASSES.connectionIcon} {
    display: none;
  }
  .${VISUAL_CLASSES.nameRow}[data-status-fit="icon"] .${VISUAL_CLASSES.connectionIcon} {
    display: inline-flex;
  }
  .${VISUAL_CLASSES.connectionIcon} > svg {
    width: 1.1em;
    height: 1.1em;
  }
  .${VISUAL_CLASSES.connectionWord} {
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.nameRow}[data-status-fit="icon"] .${VISUAL_CLASSES.connectionWord} {
    position: absolute;
    inset-inline-start: 0;
    top: 0;
    visibility: hidden;
  }
  .${VISUAL_CLASSES.connectionLine} {
    flex: none;
    white-space: nowrap;
  }
  /* The charger's status, after its name; an error in the warning colour. */
  .${VISUAL_CLASSES.connectionLine} {
    margin: 0;
    font-size: 0.85rem;
    color: var(--secondary-text-color, #727272);
    white-space: nowrap;
  }
  .${VISUAL_CLASSES.connectionError} {
    color: var(--warning-color, #b26a00);
  }
  /* A status that explains itself when tapped (the house and heat cards' "· Test"): the muted word itself, dotted
     underneath, with a finger-sized target around it. */
  .${VISUAL_CLASSES.statusButton} {
    position: relative;
    margin: 0;
    padding: 0;
    border: 0;
    background: none;
    font: inherit;
    color: inherit;
    text-decoration: underline dotted;
    text-underline-offset: 3px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.statusButton}::after {
    content: "";
    position: absolute;
    inset: -12px -8px;
  }
  .${VISUAL_CLASSES.statusButton}:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
    border-radius: 2px;
  }
  .${VISUAL_CLASSES.vehicleChoices} {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .${VISUAL_CLASSES.vehicleChoice} {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 44px;
    padding: 4px 8px;
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 8px;
    cursor: pointer;
  }
  .${VISUAL_CLASSES.vehicleChoice} > input {
    flex: none;
    margin: 0;
  }
  .${VISUAL_CLASSES.vehicleChoiceName} {
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.vehicleChoiceCharge} {
    flex: none;
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
  }
  .${VISUAL_CLASSES.status} {
    margin: 8px 0 0;
    overflow-wrap: break-word;
  }
  /* The running charge: a slim track in the muted colour, the share done in the accent, faint diagonal
     stripes on it that drift slowly while current flows and stand still otherwise. The open bar (no
     share known) is a short stretch gliding along the track, or the whole track softly when still. */
  .${VISUAL_CLASSES.chargeBar} {
    margin: 6px 0 0;
    min-width: 0;
  }
  .${VISUAL_CLASSES.chargeBarTrack} {
    position: relative;
    height: 0.375rem; /* 6 px at the default size */
    border-radius: 0.1875rem;
    overflow: hidden;
    background: var(--spotnav-charge-bar-track, var(--divider-color, rgba(127, 127, 127, 0.3)));
  }
  .${VISUAL_CLASSES.chargeBarFill} {
    position: absolute;
    inset: 0 auto 0 0;
    max-width: 100%;
    border-radius: 0.1875rem;
    background-color: var(--spotnav-charge-bar-fill, var(--primary-color, #03a9f4));
    background-image: linear-gradient(
      135deg,
      rgba(255, 255, 255, 0.25) 0 20%,
      transparent 20% 50%,
      rgba(255, 255, 255, 0.25) 50% 70%,
      transparent 70% 100%
    );
    background-size: 10px 10px;
  }
  .${VISUAL_CLASSES.chargeBar}[data-moving="true"] .${VISUAL_CLASSES.chargeBarFill} {
    animation: spotnav-charge-bar-drift 1.4s linear infinite;
  }
  .${VISUAL_CLASSES.chargeBar}[data-basis="open"] .${VISUAL_CLASSES.chargeBarFill} {
    width: 100%;
    opacity: 0.5;
  }
  .${VISUAL_CLASSES.chargeBar}[data-basis="open"][data-moving="true"] .${VISUAL_CLASSES.chargeBarFill} {
    width: 33.333%;
    opacity: 1;
    animation: spotnav-charge-bar-glide 2.4s linear infinite;
  }
  .${VISUAL_CLASSES.chargeBarLine} {
    margin: 4px 0 0;
    font-size: 0.85rem;
    color: var(--secondary-text-color, #727272);
    overflow-wrap: break-word;
  }
  @keyframes spotnav-charge-bar-drift {
    from {
      background-position: 0 0;
    }
    to {
      background-position: 10px 0;
    }
  }
  @keyframes spotnav-charge-bar-glide {
    from {
      transform: translateX(-100%);
    }
    to {
      transform: translateX(300%);
    }
  }
  .${VISUAL_CLASSES.muted} {
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
    overflow-wrap: break-word;
  }
  .${VISUAL_CLASSES.unavailable} {
    color: var(--secondary-text-color, #727272);
    font-style: italic;
  }
`;
