// Build the committed release assets: custom_components/spotnav/www/spotnav-card.js and the
// language files beside it in www/i18n/ (stale ones removed).

import { mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { ASSET, bundle, cardVersion } from "./bundle.mjs";
import { LANGUAGE_DIR, languageFiles } from "./languages.mjs";

const files = languageFiles();
mkdirSync(LANGUAGE_DIR, { recursive: true });
const wanted = new Set(files.map(({ name }) => name));
for (const name of readdirSync(LANGUAGE_DIR)) {
  if (!wanted.has(name)) {
    rmSync(join(LANGUAGE_DIR, name));
  }
}
for (const { name, bytes } of files) {
  writeFileSync(join(LANGUAGE_DIR, name), bytes);
}

await bundle(ASSET);
const { size } = statSync(ASSET);
console.log(`built ${ASSET} (${size} bytes, integration version ${cardVersion()})`);
console.log(`wrote ${files.length} language files to ${LANGUAGE_DIR}`);
