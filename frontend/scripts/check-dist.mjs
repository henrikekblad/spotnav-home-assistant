// Fail if the committed asset is not what this source compiles to.
//
// It bundles into a temporary file and compares bytes with the committed one, so the check can
// never pass by rebuilding the thing it is checking, and it never rewrites the source tree.

import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { ASSET, bundle } from "./bundle.mjs";

const directory = mkdtempSync(join(tmpdir(), "spotnav-card-"));
const fresh = join(directory, "spotnav-card.js");

try {
  await bundle(fresh);
  const compiled = readFileSync(fresh);
  let committed;
  try {
    committed = readFileSync(ASSET);
  } catch {
    throw new Error(`No committed asset at ${ASSET}: run \`npm run build\``);
  }
  const digest = (buffer) => createHash("sha256").update(buffer).digest("hex");
  if (digest(compiled) !== digest(committed)) {
    throw new Error(
      `The committed asset is stale (${committed.length} bytes committed, ${compiled.length} ` +
        `bytes compiled): run \`npm run build\` and commit the result.`,
    );
  }
  console.log(
    `dist is current: ${ASSET} (${committed.length} bytes, sha256 ${digest(committed)})`,
  );
} finally {
  rmSync(directory, { recursive: true, force: true });
}
