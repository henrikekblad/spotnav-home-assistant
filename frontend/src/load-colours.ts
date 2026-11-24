/**
 * The pool's, the spa's and the sauna's colours, one place for every card that draws them: the Plan tab's layers
 * (`house-plan.ts`, in the house and the heat card), the energy flow's heat parts (`house-flow.ts`, `house-loads.ts`)
 * and the heat card's Use tab (`heat-use.ts`). Water blues for the pool (its heat pump darker than its circulation),
 * olive for the spa and wood brown for the sauna: apart from the house's purple, heat's red, the water heater's pink,
 * the car's orange, the sun's amber and the battery's teal, and at least 3:1 against a light or a dark card for the
 * fills (the circulation's light blue is the one below it on white, drawn beside its darker heat pump). A theme may
 * set its own through the `--spotnav-pool-heat`, `--spotnav-pool-circulation`, `--spotnav-spa` and `--spotnav-sauna`
 * custom properties.
 */
export const LOAD_COLOURS = {
  pool_heat: "var(--spotnav-pool-heat-colour)",
  pool_circulation: "var(--spotnav-pool-circulation-colour)",
  spa: "var(--spotnav-spa-colour)",
  sauna: "var(--spotnav-sauna-colour)",
} as const;
export type OwnLoadKind = keyof typeof LOAD_COLOURS;

/** Each colour with the theme's own first: `var(--spotnav-pool-heat, #2b6cb0)`. */
export const LOAD_COLOUR_VALUE = {
  pool_heat: "var(--spotnav-pool-heat, #2b6cb0)",
  pool_circulation: "var(--spotnav-pool-circulation, #63a4e0)",
  spa: "var(--spotnav-spa, #6f8f2f)",
  sauna: "var(--spotnav-sauna, #a8693f)",
} as const;

/** The custom properties behind `LOAD_COLOURS`, to put on a card's root (or any box that draws them). */
export const LOAD_COLOUR_VARS = `
    --spotnav-pool-heat-colour: ${LOAD_COLOUR_VALUE.pool_heat};
    --spotnav-pool-circulation-colour: ${LOAD_COLOUR_VALUE.pool_circulation};
    --spotnav-spa-colour: ${LOAD_COLOUR_VALUE.spa};
    --spotnav-sauna-colour: ${LOAD_COLOUR_VALUE.sauna};`;
