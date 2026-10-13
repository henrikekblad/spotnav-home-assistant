// Every test starts with every language in, so a card for a Swedish reader renders Swedish at once,
// as it does in a browser once its language file has loaded. The loader's own test forgets them.

import { beforeEach } from "vitest";

import { registerAllLanguages } from "../src/i18n/all";

registerAllLanguages();

beforeEach(() => {
  registerAllLanguages();
});
