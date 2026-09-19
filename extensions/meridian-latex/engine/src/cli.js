#!/usr/bin/env node
// Unified CLI entry point -- the `bin` npm publishes as `meridian-latex`.
// Each subcommand delegates to an already-existing, already-tested module
// rather than duplicating logic here; this file is just the single,
// user-facing front door (`npx meridian-latex <command>`) instead of
// requiring someone who installed the npm package to know that `serve`
// lives in server.js and `login` lives in overleaf-login.js.
import { outlineFile } from "./outline.js";
import { login, status, logout } from "./overleaf-login.js";

const [, , command, ...rest] = process.argv;

const USAGE = `usage: meridian-latex <command> [args]

commands:
  outline <path.tex>   parse a .tex file and print its structural outline as JSON
  serve                start the local engine server (http://127.0.0.1:8471) --
                        this is what the Chrome extension's popup talks to
  login                open a dedicated browser window to capture your Overleaf
                        session (human-only -- never run this from an agent session)
  status               show whether a saved Overleaf session exists
  logout               remove the saved Overleaf session`;

async function main() {
  if (command === "outline") {
    const path = rest[0];
    if (!path) {
      console.error("usage: meridian-latex outline <path-to.tex>");
      process.exit(1);
    }
    console.log(JSON.stringify(outlineFile(path), null, 2));
  } else if (command === "serve") {
    // server.js starts listening as a side effect of being imported (see
    // its own header comment / bottom-of-file `server.listen(...)` call) --
    // dynamic import so that only happens when `serve` is the chosen
    // subcommand, not on every `meridian-latex` invocation.
    await import("./server.js");
  } else if (command === "login") {
    await login();
  } else if (command === "status") {
    console.log(JSON.stringify(status(), null, 2));
  } else if (command === "logout") {
    logout();
    console.log("Logged out -- saved cookie removed.");
  } else {
    console.error(command ? `unknown command: ${command}\n\n${USAGE}` : USAGE);
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
