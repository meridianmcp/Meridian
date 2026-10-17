'use strict';

// Routes, admission, JSON bodies, the write queue, and /v1/batch for the local write API.
// Every dependency is passed in, so node --test can drive each route with fakes.
//
// Writes go through one queue, so two requests never interleave. Reads run at once.
// /v1/batch runs its operations in one transaction and rolls the whole batch back when
// any operation fails. Only the routes marked batchable may appear inside a batch.
// deps.tracker is shared with items.js and collections.js. A rolled-back batch uses it to
// reload or drop the cached Zotero objects its operations changed.

var API_VERSION = 1;

function noop() {}

function splitPath(target) {
  return target.split('/');
}

function compileRoute(method, pattern, write, batchable, handler, status) {
  return {
    method: method,
    pattern: pattern,
    segments: splitPath(pattern),
    write: write,
    batchable: batchable,
    handler: handler,
    status: status,
  };
}

function matchSegments(segments, parts) {
  if (segments.length !== parts.length) return null;
  var params = {};
  for (var i = 0; i < segments.length; i++) {
    var segment = segments[i];
    if (segment.charAt(0) === ':') {
      params[segment.slice(1)] = parts[i];
    } else if (segment !== parts[i]) {
      return null;
    }
  }
  return params;
}

function createApp(deps) {
  var v = deps.validate;
  var http = deps.http;
  var auth = deps.auth;
  var items = deps.items;
  var collections = deps.collections;
  var zotero = deps.zotero;
  var log = deps.log || noop;
  var importBibtex = deps.importBibtex;
  var tracker = deps.tracker;
  var admission = { host: deps.host, token: deps.token };
  var apiError = v.apiError;
  var invalid = v.invalid;

  if (!tracker) throw new Error('createApp needs deps.tracker');
  if (!Array.isArray(tracker.created)) tracker.created = [];

  var routes = [
    compileRoute('GET', '/v1/status', false, false, function () {
      return {
        plugin_version: deps.version,
        api_version: API_VERSION,
        zotero_version: zotero.version,
        library_id: zotero.Libraries.userLibraryID,
      };
    }),
    compileRoute('POST', '/v1/items', true, true, function (params, body) {
      return items.createItem(body);
    }, 201),
    compileRoute('PATCH', '/v1/items/:key', true, true, function (params, body) {
      return items.updateItem(params.key, body);
    }),
    compileRoute('POST', '/v1/items/:key/trash', true, true, function (params, body) {
      return items.trashItem(params.key, body);
    }),
    compileRoute('POST', '/v1/items/:key/restore', true, true, function (params, body) {
      return items.restoreItem(params.key, body);
    }),
    compileRoute('POST', '/v1/items/:key/notes', true, true, function (params, body) {
      return items.createNote(params.key, body);
    }, 201),
    compileRoute('PATCH', '/v1/notes/:key', true, true, function (params, body) {
      return items.updateNote(params.key, body);
    }),
    compileRoute('POST', '/v1/items/:key/attachments', true, false, function (params, body) {
      return items.createAttachment(params.key, body);
    }, 201),
    compileRoute('POST', '/v1/items/:key/relations', true, true, function (params, body) {
      return items.addRelation(params.key, body);
    }),
    compileRoute('DELETE', '/v1/items/:key/relations', true, true, function (params, body) {
      return items.deleteRelation(params.key, body);
    }),
    compileRoute('POST', '/v1/collections', true, true, function (params, body) {
      return collections.createCollection(body);
    }, 201),
    compileRoute('PATCH', '/v1/collections/:key', true, true, function (params, body) {
      return collections.updateCollection(params.key, body);
    }),
    compileRoute('POST', '/v1/collections/:key/trash', true, true, function (params, body) {
      return collections.trashCollection(params.key, body);
    }),
    compileRoute('POST', '/v1/collections/:key/restore', true, true, function (params, body) {
      return collections.restoreCollection(params.key, body);
    }),
    compileRoute('POST', '/v1/collections/:key/items', true, true, function (params, body) {
      return collections.addItems(params.key, body);
    }),
    compileRoute('DELETE', '/v1/collections/:key/items', true, true, function (params, body) {
      return collections.removeItems(params.key, body);
    }),
    compileRoute('POST', '/v1/tags/rename', true, false, function (params, body) {
      return items.renameTag(body);
    }),
    compileRoute('POST', '/v1/import/bibtex', true, false, function (params, body) {
      v.requireObject(body, 'body');
      return importBibtex({
        text: v.requireString(body.bibtex, 'bibtex', v.LIMITS.bibtex),
        collectionKey: v.optionalKey(body.collection_key, 'collection_key'),
        dryRun: v.optionalBoolean(body.dry_run, 'dry_run'),
      });
    }),
    compileRoute('POST', '/v1/batch', true, false, function (params, body) {
      return runBatch(body);
    }),
  ];

  // Writes run one at a time. A failed task does not block the queue behind it.
  var chain = Promise.resolve();
  function enqueue(task) {
    var run = chain.then(task, task);
    chain = run.then(noop, noop);
    return run;
  }

  function matchRoute(method, target) {
    var parts = splitPath(target);
    var allowed = [];
    for (var i = 0; i < routes.length; i++) {
      var params = matchSegments(routes[i].segments, parts);
      if (!params) continue;
      if (routes[i].method === method) return { route: routes[i], params: params, allowed: null };
      allowed.push(routes[i].method);
    }
    return { route: null, params: null, allowed: allowed.length ? allowed : null };
  }

  function errorBody(code, message, details) {
    var error = { code: code, message: message };
    if (details !== undefined) error.details = details;
    return { error: error };
  }

  function errorResult(error) {
    if (error && error.isApiError) {
      return { status: error.httpStatus, body: errorBody(error.code, error.message, error.details) };
    }
    log(error);
    return {
      status: 500,
      body: errorBody('internal', 'Zotero could not complete the request. See Zotero’s error console.'),
    };
  }

  async function runRoute(route, params, body) {
    try {
      var value = await route.handler(params, body);
      return { status: route.status || 200, body: value };
    } catch (error) {
      return errorResult(error);
    }
  }

  function parseBody(request) {
    var bytes = request.body;
    if (!bytes || bytes.length === 0) return undefined;
    var contentType = String(request.headers['content-type'] || '').trim();
    if (!/^application\/json(\s*;\s*charset=utf-8)?$/i.test(contentType)) {
      throw apiError(415, 'unsupported_media_type', 'Send the body as application/json.');
    }
    var text;
    try {
      text = http.utf8Decode(bytes);
    } catch (error) {
      throw apiError(400, 'invalid_encoding', 'The body is not valid UTF-8.');
    }
    try {
      return JSON.parse(text);
    } catch (error) {
      throw apiError(400, 'invalid_json', 'The body is not valid JSON.');
    }
  }

  function planBatch(body) {
    v.requireObject(body, 'body');
    var operations = v.optionalList(body.operations, 'operations', v.LIMITS.operations);
    if (operations.length === 0) throw invalid('operations', 'must hold at least one operation');
    return operations.map(function (operation, index) {
      var label = 'operations[' + index + ']';
      v.requireObject(operation, label);
      var method = v.requireString(operation.method, label + '.method', 8);
      var path = v.requireString(operation.path, label + '.path', 512);
      var match = matchRoute(method, path);
      if (!match.route) throw invalid(label, 'does not name a write route');
      if (!match.route.batchable) {
        throw invalid(label, 'cannot run inside a batch; send ' + method + ' ' + path + ' on its own');
      }
      return { route: match.route, params: match.params, body: operation.body };
    });
  }

  // Brings the cached objects a rolled-back batch touched back in line with the database.
  // Objects the batch created leave Zotero's cache. Objects it changed reload from the database.
  async function revertCache(touched, created) {
    var createdSet = new Set(created);
    var seen = new Set();
    for (var i = 0; i < touched.length; i++) {
      var object = touched[i];
      if (seen.has(object)) continue;
      seen.add(object);
      try {
        if (createdSet.has(object)) {
          var owner = object.ObjectsClass;
          if (owner && typeof owner.unload === 'function') owner.unload(object.id);
        } else if (typeof object.reload === 'function') {
          await object.reload(null, true);
        }
      } catch (error) {
        // One object that cannot reload must not leave the others stale, so the loop goes on.
        log(error);
      }
    }
  }

  // Runs the operations in one transaction. Every operation is checked before any is applied,
  // so a malformed batch is refused with nothing written.
  async function runBatch(body) {
    var plans = planBatch(body);
    var results = [];
    var failure = null;
    var thrown = null;
    tracker.touched = [];
    tracker.created = [];
    tracker.inBatch = true;
    try {
      await zotero.DB.executeTransaction(async function () {
        for (var i = 0; i < plans.length; i++) {
          var outcome = await runRoute(plans[i].route, plans[i].params, plans[i].body);
          if (outcome.status >= 400) {
            failure = { index: i, outcome: outcome };
            throw new Error('batch operation ' + i + ' failed');
          }
          results.push(outcome.body);
        }
      });
    } catch (error) {
      thrown = error;
    } finally {
      tracker.inBatch = false;
    }
    var touched = tracker.touched;
    var created = tracker.created;
    tracker.touched = [];
    tracker.created = [];
    if (failure || thrown) {
      await revertCache(touched, created);
    }
    if (failure) {
      throw apiError(
        failure.outcome.status,
        'batch_rolled_back',
        'Operation ' + failure.index + ' failed, so nothing in the batch was written.',
        { failed_index: failure.index, error: failure.outcome.body.error }
      );
    }
    if (thrown) throw thrown;
    return { results: results };
  }

  function respond(outcome, extraHeaders) {
    return http.buildResponse(outcome.status, outcome.body, extraHeaders);
  }

  // The refusal for a request the admission check turned away. A 401 carries the Bearer challenge.
  function refusal(denied) {
    var headers = denied.httpStatus === 401 ? ['WWW-Authenticate: Bearer realm="meridian-zotero"'] : [];
    return respond({ status: denied.httpStatus, body: errorBody(denied.code, denied.message) }, headers);
  }

  // Called by the listener once the header block is in, before it reads the body.
  // Returns null when the request is admitted, or the response to send.
  function admitHead(headers) {
    var denied = auth.admit(headers, admission);
    return denied ? refusal(denied) : null;
  }

  // Parser failures from the socket layer answer with the same error shape.
  function rejectParse(error) {
    return respond({ status: error.httpStatus, body: errorBody(error.code, error.message) });
  }

  async function handle(request) {
    var refused = admitHead(request.headers);
    if (refused) return refused;
    var match = matchRoute(request.method, request.target);
    if (!match.route) {
      if (match.allowed) {
        return respond({
          status: 405,
          body: errorBody('method_not_allowed', 'That method is not allowed on this path.', { allow: match.allowed }),
        });
      }
      return respond({ status: 404, body: errorBody('not_found', 'No such route.') });
    }
    var outcome;
    try {
      var body = parseBody(request);
      outcome = match.route.write
        ? await enqueue(function () {
          return runRoute(match.route, match.params, body);
        })
        : await runRoute(match.route, match.params, body);
    } catch (error) {
      outcome = errorResult(error);
    }
    return respond(outcome);
  }

  return {
    handle: handle,
    admitHead: admitHead,
    rejectParse: rejectParse,
    routes: routes.map(function (route) {
      return route.method + ' ' + route.pattern + (route.batchable ? ' (batchable)' : '');
    }),
  };
}

module.exports = { createApp: createApp, API_VERSION: API_VERSION };
