#!/usr/bin/env node
import { outlineFile } from "./outline.js";

const [, , command, ...rest] = process.argv;

if (command === "outline") {
  const path = rest[0];
  if (!path) {
    console.error("usage: meridian-latex outline <path-to.tex>");
    process.exit(1);
  }
  const nodes = outlineFile(path);
  console.log(JSON.stringify(nodes, null, 2));
} else {
  console.error(`unknown command: ${command}\navailable commands: outline`);
  process.exit(1);
}
