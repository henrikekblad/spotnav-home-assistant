// Card stylesheet: Home Assistant theme variables with plain fallbacks, sized in rem/% with
// wrapping text so a 320 px column cannot overflow.

export const CARD_STYLES = `
  :host {
    display: block;
    box-sizing: border-box;
    max-width: 100%;
  }
  .card {
    box-sizing: border-box;
    max-width: 100%;
    padding: 12px 14px;
    background: var(--ha-card-background, var(--card-background-color, #ffffff));
    color: var(--primary-text-color, #212121);
    border-radius: var(--ha-card-border-radius, 12px);
  }
  h2 {
    margin: 0 0 2px;
    font-size: 1.05rem;
    font-weight: 500;
  }
  .muted {
    color: var(--secondary-text-color, #727272);
    font-size: 0.85rem;
    overflow-wrap: anywhere;
  }
  dl {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 2px 10px;
    margin: 10px 0 0;
    font-size: 0.9rem;
  }
  dt {
    color: var(--secondary-text-color, #727272);
  }
  dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  .error {
    color: var(--error-color, #db4437);
  }
  .prototype {
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--divider-color, #e0e0e0);
    color: var(--secondary-text-color, #727272);
    font-size: 0.75rem;
    overflow-wrap: anywhere;
  }
  button {
    margin-top: 10px;
    font: inherit;
    color: var(--primary-text-color, #212121);
    background: var(--secondary-background-color, transparent);
    border: 1px solid var(--divider-color, #e0e0e0);
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
  }
  button:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }
`;
