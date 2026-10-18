'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('../lib/api/http.js');
const auth = require('../lib/api/auth.js');
const v = require('../lib/api/validate.js');
const { createApp } = require('../lib/api/app.js');
const { createItemWrites } = require('../lib/api/items.js');
const { createCollectionWrites } = require('../lib/api/collections.js');
const { createFakeZotero, createFakeIO } = require('./support/fake-zotero.js');

const HOST = '127.0.0.1:23125';
const TOKEN = 'T'.repeat(40);

// A delay that never fires, so the file check always finishes first in tests that do not set one.
const neverDelay = () => new Promise(() => {});

function build(overrides) {
  const zotero = createFakeZotero();
  const io = createFakeIO();
  const errors = [];
  const importCalls = [];
  const tracker = { inBatch: false, touched: [], created: [] };
  const deps = {
    zotero,
    version: '0.1.4',
    host: HOST,
    token: TOKEN,
    validate: v,
    http,
    auth,
    tracker,
    items: createItemWrites({
      zotero,
      validate: v,
      io,
      newId: () => 'ID1',
      tempDir: () => '/tmp/zotero-test',
      tracker,
      delay: neverDelay,
    }),
    collections: createCollectionWrites({ zotero, validate: v, tracker }),
    importBibtex: async (request) => {
      importCalls.push(request);
      return { dry_run: request.dryRun, would_import: [] };
    },
    log: (error) => errors.push(error),
    ...overrides,
  };
  const app = createApp(deps);
  return { app, zotero, errors, importCalls, io, tracker };
}

function request(method, target, options = {}) {
  const headers = {
    host: HOST,
    authorization: 'Bearer ' + TOKEN,
    ...options.headers,
  };
  if (options.noAuth) delete headers.authorization;
  const body = options.json !== undefined ? http.utf8Encode(JSON.stringify(options.json)) : options.raw || new Uint8Array(0);
  if (options.json !== undefined && !('content-type' in headers)) {
    headers['content-type'] = 'application/json';
  }
  return { method, target, headers, body };
}

// Reads the status, headers and JSON body back out of the response bytes.
function parse(response) {
  const text = new TextDecoder().decode(response.bytes);
  const split = text.indexOf('\r\n\r\n');
  const head = text.slice(0, split);
  const body = text.slice(split + 4);
  return {
    status: response.status,
    head: head,
    body: body.length ? JSON.parse(body) : null,
  };
}

async function call(app, method, target, options) {
  return parse(await app.handle(request(method, target, options)));
}

test('GET /v1/status answers with the versions, for an admitted caller', async () => {
  const { app } = build();
  const out = await call(app, 'GET', '/v1/status');
  assert.equal(out.status, 200);
  assert.deepEqual(out.body, { plugin_version: '0.1.4', api_version: 1, zotero_version: '10.0.6', library_id: 1 });
});

test('no token is a 401 that says how to send one', async () => {
  const { app } = build();
  const out = await call(app, 'GET', '/v1/status', { noAuth: true });
  assert.equal(out.status, 401);
  assert.match(out.head, /WWW-Authenticate: Bearer/);
  assert.equal(out.body.error.code, 'unauthorized');
});

test('a wrong Host is a 403 even with the right token, which blocks DNS rebinding', async () => {
  const { app } = build();
  const out = await call(app, 'GET', '/v1/status', { headers: { host: 'attacker.example:23125' } });
  assert.equal(out.status, 403);
  assert.equal(out.body.error.code, 'host_not_allowed');
});

test('an Origin header is a 403, so a browser page cannot drive the API', async () => {
  const { app } = build();
  const out = await call(app, 'GET', '/v1/status', { headers: { origin: 'https://example.com' } });
  assert.equal(out.status, 403);
  assert.equal(out.body.error.code, 'origin_not_allowed');
  assert.equal(/access-control/i.test(out.head), false);
});

test('admitHead lets a good request through with null, and answers a refused one the way handle does', () => {
  const { app } = build();
  assert.equal(app.admitHead({ host: HOST, authorization: 'Bearer ' + TOKEN }), null);
  const wrongHost = parse(app.admitHead({ host: 'attacker.example:23125', authorization: 'Bearer ' + TOKEN }));
  assert.equal(wrongHost.status, 403);
  assert.equal(wrongHost.body.error.code, 'host_not_allowed');
  assert.equal(/WWW-Authenticate/.test(wrongHost.head), false, 'only a 401 carries the Bearer challenge');
  const noToken = parse(app.admitHead({ host: HOST }));
  assert.equal(noToken.status, 401);
  assert.equal(noToken.body.error.code, 'unauthorized');
  assert.match(noToken.head, /WWW-Authenticate: Bearer realm="meridian-zotero"/);
});

test('an unknown path is a 404, and a known path with the wrong method is a 405 that lists the allowed ones', async () => {
  const { app } = build();
  assert.equal((await call(app, 'GET', '/v1/nothing')).status, 404);
  const wrong = await call(app, 'GET', '/v1/items');
  assert.equal(wrong.status, 405);
  assert.deepEqual(wrong.body.error.details.allow, ['POST']);
  assert.equal((await call(app, 'OPTIONS', '/v1/items')).status, 405);
});

test('a body must be JSON, must be sent as application/json, and must be an object', async () => {
  const { app } = build();
  const wrongType = await call(app, 'POST', '/v1/items', {
    headers: { 'content-type': 'text/plain' },
    raw: http.utf8Encode('{"item_type":"book"}'),
  });
  assert.equal(wrongType.status, 415);
  const broken = await call(app, 'POST', '/v1/items', { headers: { 'content-type': 'application/json' }, raw: http.utf8Encode('{nope') });
  assert.equal(broken.status, 400);
  assert.equal(broken.body.error.code, 'invalid_json');
  const list = await call(app, 'POST', '/v1/items', { json: [1, 2] });
  assert.equal(list.status, 422);
});

test('POST /v1/items creates an item and answers 201 with its key', async () => {
  const { app, zotero } = build();
  const out = await call(app, 'POST', '/v1/items', { json: { item_type: 'book', fields: { title: 'Over HTTP' } } });
  assert.equal(out.status, 201);
  assert.match(out.body.key, /^[A-Z0-9]{8}$/);
  assert.equal(zotero._state.items[0].fields.title, 'Over HTTP');
});

test('a validation failure comes back as a 422 with the reasons, and no item is written', async () => {
  const { app, zotero } = build();
  const out = await call(app, 'POST', '/v1/items', { json: { item_type: 'book', fields: { nope: 'x' } } });
  assert.equal(out.status, 422);
  assert.equal(out.body.error.code, 'invalid_fields');
  assert.equal(zotero._state.items.length, 0);
});

test('a batch runs its operations in order and returns each result', async () => {
  const { app, zotero } = build();
  const out = await call(app, 'POST', '/v1/batch', {
    json: {
      operations: [
        { method: 'POST', path: '/v1/items', body: { item_type: 'book', fields: { title: 'First' } } },
        { method: 'POST', path: '/v1/items', body: { item_type: 'book', fields: { title: 'Second' } } },
      ],
    },
  });
  assert.equal(out.status, 200);
  assert.equal(out.body.results.length, 2);
  assert.equal(zotero._state.items.length, 2);
  assert.equal(zotero._calls.saveTx, 0, 'every save inside the batch used save()');
});

test('one failing operation rolls back the whole batch and names the operation that failed', async () => {
  const { app, zotero } = build();
  const out = await call(app, 'POST', '/v1/batch', {
    json: {
      operations: [
        { method: 'POST', path: '/v1/items', body: { item_type: 'book', fields: { title: 'Should vanish' } } },
        { method: 'PATCH', path: '/v1/items/ZZZZZZZZ', body: { fields: { title: 'Missing' } } },
      ],
    },
  });
  assert.equal(out.status, 404);
  assert.equal(out.body.error.code, 'batch_rolled_back');
  assert.equal(out.body.error.details.failed_index, 1);
  assert.equal(zotero._state.items.length, 0, 'the first create was rolled back');
});

test('a rolled-back batch reloads the cached objects it changed, and drops the ones it created from the cache', async () => {
  const { app, zotero } = build();
  const existing = zotero._addItem('book', { title: 'Before' });
  const reloads = [];
  existing.reload = async function (dataTypes, reloadUnchanged) {
    reloads.push({ key: this.key, dataTypes, reloadUnchanged });
  };
  // The fake database rolls back its own rows, but a real Zotero keeps the cached objects. Only the reload and unload show the revert.
  const unloaded = [];
  zotero.Item.prototype.ObjectsClass = { unload: (id) => unloaded.push(id) };
  const createdID = zotero._state.nextID;
  const out = await call(app, 'POST', '/v1/batch', {
    json: {
      operations: [
        { method: 'PATCH', path: '/v1/items/' + existing.key, body: { fields: { title: 'Changed in batch' } } },
        { method: 'POST', path: '/v1/items', body: { item_type: 'book', fields: { title: 'Created in batch' } } },
        { method: 'PATCH', path: '/v1/items/ZZZZZZZZ', body: { fields: { title: 'Missing' } } },
      ],
    },
  });
  assert.equal(out.status, 404);
  assert.deepEqual(reloads, [{ key: existing.key, dataTypes: null, reloadUnchanged: true }]);
  assert.deepEqual(unloaded, [createdID], 'only the object the batch created leaves the cache');
});

test('a batch leaves the shared tracker idle, whether it succeeds or rolls back', async () => {
  const { app, tracker } = build();
  const ok = await call(app, 'POST', '/v1/batch', {
    json: { operations: [{ method: 'POST', path: '/v1/items', body: { item_type: 'book' } }] },
  });
  assert.equal(ok.status, 200);
  assert.equal(tracker.inBatch, false);
  assert.deepEqual(tracker.touched, []);
  const failed = await call(app, 'POST', '/v1/batch', {
    json: { operations: [{ method: 'PATCH', path: '/v1/items/ZZZZZZZZ', body: { fields: { title: 'x' } } }] },
  });
  assert.equal(failed.status, 404);
  assert.equal(tracker.inBatch, false);
  assert.deepEqual(tracker.touched, []);
});

test('a batch refuses routes that cannot run inside a transaction, before writing anything', async () => {
  const { app, zotero } = build();
  const attachments = await call(app, 'POST', '/v1/batch', {
    json: { operations: [
      { method: 'POST', path: '/v1/items', body: { item_type: 'book' } },
      { method: 'POST', path: '/v1/items/ABCDEFGH/attachments', body: { mode: 'linked_file', path: '/x' } },
    ] },
  });
  assert.equal(attachments.status, 422);
  assert.equal(zotero._state.items.length, 0);
  const nested = await call(app, 'POST', '/v1/batch', { json: { operations: [{ method: 'POST', path: '/v1/batch', body: {} }] } });
  assert.equal(nested.status, 422);
  const rename = await call(app, 'POST', '/v1/batch', { json: { operations: [{ method: 'POST', path: '/v1/tags/rename', body: { from: 'a', to: 'b' } }] } });
  assert.equal(rename.status, 422);
});

test('writes run one at a time, even when several arrive together', async () => {
  const { app, zotero } = build();
  let active = 0;
  let peak = 0;
  const original = zotero.Item.prototype.save;
  zotero.Item.prototype.save = async function () {
    active += 1;
    peak = Math.max(peak, active);
    await new Promise((resolve) => setTimeout(resolve, 5));
    const result = await original.call(this);
    active -= 1;
    return result;
  };
  const outs = await Promise.all([
    call(app, 'POST', '/v1/items', { json: { item_type: 'book', fields: { title: 'A' } } }),
    call(app, 'POST', '/v1/items', { json: { item_type: 'book', fields: { title: 'B' } } }),
    call(app, 'POST', '/v1/items', { json: { item_type: 'book', fields: { title: 'C' } } }),
  ]);
  assert.deepEqual(outs.map((out) => out.status), [201, 201, 201]);
  assert.equal(peak, 1, 'no two writes overlapped');
});

test('an unexpected failure is a 500 with a generic message, and the detail goes to the error console', async () => {
  const { app, errors } = build({
    items: { createItem: async () => { throw new Error('database locked: /secret/path'); } },
  });
  const out = await call(app, 'POST', '/v1/items', { json: { item_type: 'book' } });
  assert.equal(out.status, 500);
  assert.equal(out.body.error.code, 'internal');
  assert.equal(/secret/.test(out.body.error.message), false, 'the detail is not sent to the caller');
  assert.equal(errors.length, 1);
});

test('the import route passes the text, collection and dry-run flag through, and validates them', async () => {
  const { app, importCalls } = build();
  const dry = await call(app, 'POST', '/v1/import/bibtex', { json: { bibtex: '@article{x,title={T}}', dry_run: true } });
  assert.equal(dry.status, 200);
  assert.equal(dry.body.dry_run, true);
  assert.deepEqual(importCalls[0], { text: '@article{x,title={T}}', collectionKey: null, dryRun: true });
  const missing = await call(app, 'POST', '/v1/import/bibtex', { json: { collection_key: 'ABCDEFGH' } });
  assert.equal(missing.status, 422);
  const badKey = await call(app, 'POST', '/v1/import/bibtex', { json: { bibtex: 'x', collection_key: 'not-a-key' } });
  assert.equal(badKey.status, 422);
});

test('a parser refusal from the socket layer answers in the same error shape', () => {
  const { app } = build();
  const parsed = parse(app.rejectParse({ httpStatus: 413, code: 'payload_too_large', message: 'Too big.' }));
  assert.equal(parsed.status, 413);
  assert.equal(parsed.body.error.code, 'payload_too_large');
});

test('the app lists its routes, and marks the ones a batch may contain', () => {
  const { app } = build();
  assert.ok(app.routes.includes('POST /v1/items (batchable)'));
  assert.ok(app.routes.includes('POST /v1/items/:key/attachments'));
  assert.ok(!app.routes.includes('POST /v1/items/:key/attachments (batchable)'));
});

test('createApp refuses to start without the shared tracker', () => {
  const zotero = createFakeZotero();
  assert.throws(
    () => createApp({ zotero, validate: v, http, auth, host: HOST, token: TOKEN, items: {}, collections: {} }),
    /needs deps\.tracker/
  );
});
