'use strict';

// Design-line checks. These read the plugin's own source files and fail the
// build if the code gains a network path, a server, a watcher, a timer, or an
// embedded personal email or secret.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PLUGIN_ROOT = path.resolve(__dirname, '..');
const PLUGIN_ID = 'meridian-zotero@usemeridian.us';

function walk(dir, skipDirs) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (skipDirs.includes(entry.name)) continue;
      out.push(...walk(path.join(dir, entry.name), skipDirs));
    } else {
      out.push(path.join(dir, entry.name));
    }
  }
  return out;
}

// Code that runs inside Zotero: bootstrap glue, the lib modules, the pane.
function runtimeFiles() {
  assert.ok(fs.existsSync(path.join(PLUGIN_ROOT, 'bootstrap.js')), 'bootstrap.js must exist');
  const libs = walk(path.join(PLUGIN_ROOT, 'lib'), []).filter((f) => f.endsWith('.js'));
  const pane = walk(path.join(PLUGIN_ROOT, 'content'), []).filter((f) => f.endsWith('.xhtml'));
  return [path.join(PLUGIN_ROOT, 'bootstrap.js'), path.join(PLUGIN_ROOT, 'prefs.js'), ...libs, ...pane];
}

const FORBIDDEN_IN_RUNTIME = [
  [/\bfetch\s*\(/, 'fetch()'],
  [/XMLHttpRequest/, 'XMLHttpRequest'],
  [/\bZotero\.HTTP\b/, 'Zotero.HTTP'],
  [/\bZotero\.API\b/, 'Zotero.API'],
  [/\bZotero\.Connector\b/, 'Zotero.Connector'],
  [/\bsetInterval\s*\(/, 'setInterval()'],
  [/\bsetTimeout\s*\(/, 'setTimeout()'],
  [/\bserverSocket\b|ServerSocket|createServerSocket|HttpServer/, 'server socket'],
  [/require\(\s*['"](?:http|https|net|dgram|tls|ws|chokidar)['"]\s*\)/, 'network or watcher module'],
  [/\bfs\.watch\b|\bwatchFile\b|\bFileWatcher\b/, 'file watcher'],
  [/\bregisterObserver\s*\(|\baddObserver\s*\(|Services\.obs\b/, 'observer or notifier hook'],
  [/localhost|127\.0\.0\.1|\[::1\]/, 'local host address'],
];

// XML namespace declarations in the pane XHTML are identifiers, not requests.
const XML_NAMESPACE_OK = /https?:\/\/(?:www\.w3\.org\/1999\/xhtml|www\.mozilla\.org\/keymaster\/gatekeeper\/there\.is\.only\.xul)/g;

test('runtime code has no network, server, watcher, timer or observer path', () => {
  for (const file of runtimeFiles()) {
    const text = fs.readFileSync(file, 'utf8');
    const relative = path.relative(PLUGIN_ROOT, file);
    for (const [pattern, label] of FORBIDDEN_IN_RUNTIME) {
      if (label === 'URL literal') continue;
      assert.equal(pattern.test(text), false, `${relative} contains ${label}`);
    }
    if (file.endsWith('.js')) {
      assert.equal(/https?:\/\//.test(text), false, `${relative} contains a URL literal`);
    } else {
      const withoutNamespaces = text.replace(XML_NAMESPACE_OK, '');
      assert.equal(/https?:\/\//.test(withoutNamespaces), false, `${relative} contains a URL outside an XML namespace`);
    }
  }
});

test('no email address other than the plugin id appears anywhere in the plugin folder', () => {
  const files = walk(PLUGIN_ROOT, ['node_modules']);
  const emailPattern = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g;
  for (const file of files) {
    const text = fs.readFileSync(file, 'utf8');
    for (const match of text.match(emailPattern) || []) {
      assert.equal(match, PLUGIN_ID, `${path.relative(PLUGIN_ROOT, file)} contains email ${match}`);
    }
  }
});

test('no secret-shaped strings or machine-local absolute paths in the plugin folder', () => {
  const files = walk(PLUGIN_ROOT, ['node_modules']);
  const secret = /sk_meridian_|BEARER_TOKEN|api[_-]?key\s*[:=]|-----BEGIN [A-Z ]*PRIVATE KEY/i;
  const localPath = /[A-Za-z]:\\\\|[A-Za-z]:\/Users\/|\/Users\/[A-Za-z0-9_]+\/|\/home\/[A-Za-z0-9_]+\//;
  for (const file of files) {
    // This file holds the patterns as literals, so it cannot scan itself.
    if (path.basename(file) === 'static-checks.test.js') continue;
    const text = fs.readFileSync(file, 'utf8');
    assert.equal(secret.test(text), false, `${path.relative(PLUGIN_ROOT, file)} contains a secret-shaped string`);
    assert.equal(localPath.test(text), false, `${path.relative(PLUGIN_ROOT, file)} contains a machine-local path`);
  }
});
