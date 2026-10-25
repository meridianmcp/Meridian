'use strict';

// Design-line checks. These read the plugin's own source files and fail the build when:
//   - runtime code gains a network client, a watcher, an observer, or a URL literal,
//   - anything other than api/listener.js opens a socket, starts a timer, or names the loopback address,
//   - the listener stops binding the loopback interface only,
//   - any code path can permanently delete data,
//   - an email address, a secret-shaped string, or a machine-local path is written into the plugin.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const PLUGIN_ROOT = path.resolve(__dirname, '..');
const PLUGIN_ID = 'meridian-zotero@usemeridian.us';
const LISTENER = path.join(PLUGIN_ROOT, 'api', 'listener.js');

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

// Code that runs inside Zotero: bootstrap glue, the lib modules, the listener, the pane.
function runtimeFiles() {
  assert.ok(fs.existsSync(path.join(PLUGIN_ROOT, 'bootstrap.js')), 'bootstrap.js must exist');
  assert.ok(fs.existsSync(LISTENER), 'api/listener.js must exist');
  const libs = walk(path.join(PLUGIN_ROOT, 'lib'), []).filter((f) => f.endsWith('.js'));
  const pane = walk(path.join(PLUGIN_ROOT, 'content'), []).filter((f) => f.endsWith('.xhtml'));
  return [path.join(PLUGIN_ROOT, 'bootstrap.js'), path.join(PLUGIN_ROOT, 'prefs.js'), LISTENER, ...libs, ...pane];
}

// Forbidden in every runtime file, the listener included.
const EVERYWHERE = [
  [/\bfetch\s*\(/, 'fetch()'],
  [/XMLHttpRequest/, 'XMLHttpRequest'],
  [/\bZotero\.HTTP\b/, 'Zotero.HTTP'],
  [/\bZotero\.API\b/, 'Zotero.API'],
  [/\bZotero\.Connector\b/, 'Zotero.Connector'],
  [/require\(\s*['"](?:http|https|net|dgram|tls|ws|chokidar)['"]\s*\)/, 'network or watcher module'],
  [/\bfs\.watch\b|\bwatchFile\b|\bFileWatcher\b/, 'file watcher'],
  [/\bregisterObserver\s*\(|\baddObserver\s*\(|Services\.obs\b/, 'observer or notifier hook'],
  [/localhost/, 'localhost name'],
];

// Allowed only in api/listener.js.
const LISTENER_ONLY = [
  [/nsIServerSocket|server-socket;1|createServerSocket|\bServerSocket\b/, 'server socket'],
  [/nsITransport|nsIAsyncInputStream|nsIBinaryInputStream|nsIBinaryOutputStream|openInputStream|openOutputStream/, 'socket stream'],
  [/setTimeout\s*\(|setInterval\s*\(|nsITimer|timer;1/, 'timer'],
  [/127\.0\.0\.1|\[::1\]/, 'loopback address'],
];

// Permanent deletion. Items and collections go to the trash and come back with restore.
const PERMANENT_DELETE = [
  [/\.erase(Tx)?\s*\(/, 'erase()'],
  [/\bemptyTrash\b/, 'emptyTrash'],
  [/\bDELETE FROM\b/i, 'SQL DELETE'],
];

// Every http or https run (up to whitespace, a quote, or an angle bracket) must be exactly one of the
// allowed XML namespaces. A prefix match is not enough: a longer host, path, query, or port that
// starts with a namespace (xhtml.evil.example, xhtml:evil, xhtml?x) is a URL and is reported.
const URL_RUN = /https?:\/\/[^\s"'<>]+/g;
const XML_NAMESPACE_OK = new Set([
  'http://www.w3.org/1999/xhtml',
  'https://www.w3.org/1999/xhtml',
  'http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul',
  'https://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul',
]);

function disallowedUrls(text) {
  return (text.match(URL_RUN) || []).filter((run) => !XML_NAMESPACE_OK.has(run));
}

test('runtime code has no network client, watcher, observer, localhost name, or URL literal', () => {
  // A namespace with more host or path after it is still a URL, so each of these must be reported.
  for (const hostile of [
    'https://www.w3.org/1999/xhtml.evil.example/x.js',
    'https://www.w3.org/1999/xhtml:evil',
    'https://www.w3.org/1999/xhtml?x=1',
    'https://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul/x',
  ]) {
    assert.deepEqual(disallowedUrls(hostile), [hostile], `${hostile} must not pass as a namespace`);
  }
  // The real namespaces pass, in either scheme.
  assert.deepEqual(
    disallowedUrls('xmlns="http://www.w3.org/1999/xhtml" xmlns:x="https://www.w3.org/1999/xhtml"'),
    [],
  );
  for (const file of runtimeFiles()) {
    const text = fs.readFileSync(file, 'utf8');
    const relative = path.relative(PLUGIN_ROOT, file);
    for (const [pattern, label] of EVERYWHERE) {
      assert.equal(pattern.test(text), false, `${relative} contains ${label}`);
    }
    if (file.endsWith('.js')) {
      assert.equal(/https?:\/\//.test(text), false, `${relative} contains a URL literal`);
    } else {
      const bad = disallowedUrls(text);
      assert.equal(bad.length, 0, `${relative} contains a URL outside an XML namespace: ${bad.join(', ')}`);
    }
  }
});

test('only api/listener.js holds the socket, the stream, the timer, and the loopback address', () => {
  const listenerText = fs.readFileSync(LISTENER, 'utf8');
  for (const [pattern, label] of LISTENER_ONLY) {
    assert.equal(pattern.test(listenerText), true, `the listener should contain ${label}; this check would be vacuous otherwise`);
  }
  for (const file of runtimeFiles()) {
    if (file === LISTENER) continue;
    const text = fs.readFileSync(file, 'utf8');
    const relative = path.relative(PLUGIN_ROOT, file);
    for (const [pattern, label] of LISTENER_ONLY) {
      assert.equal(pattern.test(text), false, `${relative} contains ${label}; only api/listener.js may`);
    }
  }
});

test('the listener binds the loopback interface only', () => {
  const text = fs.readFileSync(LISTENER, 'utf8');
  assert.match(text, /server\.init\(\s*options\.port\s*,\s*true\s*,/, 'server.init must pass loopbackOnly = true');
  assert.equal(/server\.init\(\s*options\.port\s*,\s*false/.test(text), false);
});

test('no code path can permanently delete a library item or collection', () => {
  for (const file of runtimeFiles()) {
    const text = fs.readFileSync(file, 'utf8');
    const relative = path.relative(PLUGIN_ROOT, file);
    for (const [pattern, label] of PERMANENT_DELETE) {
      assert.equal(pattern.test(text), false, `${relative} contains ${label}`);
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
  // A drive letter must start a token, so a letter before the colon (as in the regex doi:\s) does not count.
  const localPath = /(^|[^A-Za-z])[A-Za-z]:\\|[A-Za-z]:\/Users\/|\/Users\/[A-Za-z0-9_]+\/|\/home\/[A-Za-z0-9_]+\//;
  for (const file of files) {
    // This file holds the patterns as literals, so it cannot scan itself.
    if (path.basename(file) === 'static-checks.test.js') continue;
    const text = fs.readFileSync(file, 'utf8');
    assert.equal(secret.test(text), false, `${path.relative(PLUGIN_ROOT, file)} contains a secret-shaped string`);
    assert.equal(localPath.test(text), false, `${path.relative(PLUGIN_ROOT, file)} contains a machine-local path`);
  }
});
