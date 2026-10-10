import { rmSync } from "node:fs";

// Deleted adapters and tests must not survive the next build as stale JavaScript.
rmSync(new URL("../dist/", import.meta.url), { recursive: true, force: true });
