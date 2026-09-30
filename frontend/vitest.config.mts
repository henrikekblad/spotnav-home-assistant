import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // A DOM is needed for the two custom elements and for the narrow-width smoke render; the
    // view model, the API wrappers and the bundle audit do not care either way.
    environment: "jsdom",
    include: ["test/**/*.test.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
