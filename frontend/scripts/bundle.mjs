// The one bundling rule, shared by `npm run build` and `npm run check-dist` so the two cannot
// disagree about what the released asset is.
//
// Determinism, deliberately: no timestamps, no absolute paths, no source maps, no legal-comment
// extraction from dependencies, and one fixed banner. Two clean installs of the same source must
// produce the same bytes, which is what lets the compiled asset be committed and checked for
// staleness.

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { build } from "esbuild";

const here = dirname(fileURLToPath(import.meta.url));
export const ENTRY = resolve(here, "../src/index.ts");
export const ASSET = resolve(here, "../../custom_components/spotnav/www/spotnav-card.js");

export function cardVersion() {
  const generated = readFileSync(resolve(here, "../src/generated/version.ts"), "utf8");
  const match = /CARD_VERSION = "([^"]+)"/.exec(generated);
  if (match === null) {
    throw new Error("src/generated/version.ts has no version: run `npm run version:write`");
  }
  return match[1];
}

export async function bundle(outfile) {
  await build({
    entryPoints: [ENTRY],
    outfile,
    bundle: true,
    // A Lovelace JavaScript module resource is loaded as an ES module, so the bundle is one.
    format: "esm",
    target: "es2021",
    platform: "browser",
    // Minified: the dashboard waits only two seconds for the element to be defined, so load time
    // matters. The readable source is in frontend/src; the banner below keeps the version.
    minify: true,
    sourcemap: false,
    legalComments: "none",
    charset: "utf8",
    logLevel: "warning",
    banner: {
      js: [
        "// SpotNav card, compiled from frontend/ in the same integration.",
        `// Integration version ${cardVersion()}. The integration registers this file as a`,
        "// Lovelace module resource itself; it is not added by hand.",
      ].join("\n"),
    },
  });
}
