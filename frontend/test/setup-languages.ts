// Every test starts with every language in, so a card for a Swedish reader renders Swedish at once,
// as it does in a browser once its language file has loaded. The loader's own test forgets them.

import { beforeEach } from "vitest";

import { contract } from "../src/contract";
import { registerAllLanguages } from "../src/i18n/all";

registerAllLanguages();
// Contract drift fails here: a fixture with a key the decoders do not know is rejected (production ignores it).
contract.strict = true;

beforeEach(() => {
  registerAllLanguages();
});
