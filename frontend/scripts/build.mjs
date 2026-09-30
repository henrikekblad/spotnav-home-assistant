// Build the committed release asset: custom_components/spotnav/www/spotnav-card.js

import { statSync } from "node:fs";

import { ASSET, bundle, cardVersion } from "./bundle.mjs";

await bundle(ASSET);
const { size } = statSync(ASSET);
console.log(`built ${ASSET} (${size} bytes, integration version ${cardVersion()})`);
