import { randomBytes } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

if (process.argv.length !== 2) {
  process.stderr.write("Usage: node deploy/init-env.mjs\n");
  process.exitCode = 2;
} else {
  for (const name of [".env", "server.env"]) {
    try {
      const template = readFileSync(new URL(`${name}.example`, import.meta.url), "utf8");
      const content = template.replace("=GENERATE_ONCE", `=${randomBytes(32).toString("hex")}`);
      // Exclusive creation preserves an existing deployment's encryption key.
      writeFileSync(fileURLToPath(new URL(name, import.meta.url)), content, {
        encoding: "utf8", flag: "wx", mode: 0o600,
      });
      process.stdout.write(`Created deploy/${name} (0600).\n`);
    } catch (error) {
      if (error?.code === "EEXIST") {
        process.stdout.write(`Kept existing deploy/${name}; no values were changed.\n`);
      } else {
        process.stderr.write(`Could not create deploy/${name}.\n`);
        process.exitCode = 1;
      }
    }
  }
  if (!process.exitCode) {
    process.stdout.write("For a new deployment, edit the two files locally. For an upgrade, retain the original master key. No secrets were printed.\n");
  }
}
