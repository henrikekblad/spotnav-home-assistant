// Write src/generated/languages.ts: the name of each language file the card fetches.
//
// Generated, never committed (like version.ts); the build, the type check and the tests call it.
// The files themselves are written by `npm run build` only.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { languageModule } from "./languages.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const target = resolve(here, "../src/generated/languages.ts");
const contents = languageModule();

mkdirSync(dirname(target), { recursive: true });
try {
  if (readFileSync(target, "utf8") === contents) {
    process.exit(0);
  }
} catch {
  // Not written yet.
}
writeFileSync(target, contents);
console.log("language file names -> src/generated/languages.ts");
