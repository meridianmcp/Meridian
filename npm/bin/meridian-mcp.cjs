#!/usr/bin/env node
/**
 * meridian-mcp — npx launcher
 * Checks for Python + pixi, then starts Meridian MCP server via stdio.
 * Falls back to hosted SSE endpoint instructions if local setup not found.
 */
const { execSync, spawn } = require('child_process');
const path = require('path');
const os = require('os');

const REPO = 'https://github.com/meridianmcp/Meridian';
const HOSTED = 'https://usemeridian.us/mcp/sse';

function hasPython() {
  try { execSync('python --version', { stdio: 'ignore' }); return true; } catch {}
  try { execSync('python3 --version', { stdio: 'ignore' }); return true; } catch {}
  return false;
}

function hasPixi() {
  try { execSync('pixi --version', { stdio: 'ignore' }); return true; } catch {}
  return false;
}

function hasRepo() {
  // Check if we're inside the Meridian repo already
  try {
    const here = process.cwd();
    require('fs').accessSync(path.join(here, 'pixi.toml'));
    return here;
  } catch {}
  // Check default install paths
  const candidates = [
    path.join(os.homedir(), 'Meridian'),
    path.join(os.homedir(), 'Documents', 'Meridian'),
    path.join(os.homedir(), 'Documents', 'Meridian', 'repository'),
  ];
  for (const c of candidates) {
    try { require('fs').accessSync(path.join(c, 'pixi.toml')); return c; } catch {}
  }
  return null;
}

const repoPath = hasRepo();

if (repoPath && hasPixi()) {
  // Happy path: run stdio MCP server
  const proc = spawn('pixi', ['run', 'python', '-m', 'meridian', '--mcp'], {
    cwd: repoPath,
    stdio: 'inherit',
  });
  proc.on('exit', code => process.exit(code || 0));
} else {
  // Fallback: print setup instructions
  process.stderr.write(`
Meridian MCP — local setup not found.

Quick setup (30 seconds):
  curl -fsSL https://pixi.sh/install.sh | bash
  git clone ${REPO} && cd Meridian && pixi run start

Or use the hosted SSE endpoint (no install):
  Name: meridian
  URL:  ${HOSTED}
  Add to your MCP client config and you're done.

Docs: https://docs.usemeridian.us
`);
  process.exit(1);
}
