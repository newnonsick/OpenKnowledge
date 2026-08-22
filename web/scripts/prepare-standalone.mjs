import { cpSync } from "node:fs";
import { join } from "node:path";

const buildDirectory = join(process.cwd(), ".next");
cpSync(join(buildDirectory, "static"), join(buildDirectory, "standalone", ".next", "static"), {
  force: true,
  recursive: true,
});
