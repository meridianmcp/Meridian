/* global Zotero, Services, IOUtils, PathUtils, Components */
//
// Meridian Zotero Bridge: bootstrap glue for the Zotero 7+ bootstrap structure.
//
// Design line: writes come from two places only. A menu command the user chose
// inside Zotero, or the local write API: a loopback listener (api/listener.js)
// that accepts a request only with the bearer token from the Meridian token file.
// Reads stay on Zotero's own local API. The decisions live in lib/ and are
// unit-tested with node --test. This file connects Zotero APIs to that logic.
//
// The listener starts with the add-on and stops at shutdown. It starts only when
// the profile names a token file (set by the Meridian install) and that file holds
// a usable token. Without one, the menus still work and the API stays off.

var MERIDIAN_ADDON_ID = 'meridian-zotero@usemeridian.us';
var MERIDIAN_PREF_PREFIX = 'extensions.zotero.meridianZoteroBridge.';
var MERIDIAN_API_PORT_PREF = MERIDIAN_PREF_PREFIX + 'apiPort';
var MERIDIAN_API_TOKEN_PATH_PREF = MERIDIAN_PREF_PREFIX + 'apiTokenPath';
var MERIDIAN_BIBTEX_TRANSLATOR_ID = '9cb70025-a888-4a29-a210-93ec52da40d4';
var MERIDIAN_MENU_COLLECTION = 'meridian-zotero-bridge-collection-menu';
var MERIDIAN_MENU_ITEM = 'meridian-zotero-bridge-item-menu';
var MERIDIAN_REPORT_LINE_LIMIT = 25;
var MERIDIAN_API_DEFAULT_PORT = 23125;

var meridianRootURI = null;
var meridianLib = null;
var meridianPaneID = null;
var meridianApi = null;
var meridianVersion = null;

var MERIDIAN_API_MODULES = [
  'api/http',
  'api/auth',
  'api/validate',
  'api/items',
  'api/collections',
  'api/app',
];

function install() {}

function uninstall() {}

async function startup(data, reason) {
  var rootURI = data && data.rootURI;
  await Zotero.initializationPromise;
  meridianRootURI = rootURI;
  meridianVersion = data && data.version;
  meridianLib = meridianLoadLib(rootURI);
  meridianSetDefaultPrefs(rootURI);
  meridianRegisterMenus();
  meridianPaneID = await Zotero.PreferencePanes.register({
    pluginID: MERIDIAN_ADDON_ID,
    src: rootURI + 'content/preferences.xhtml',
    label: 'Meridian Zotero Bridge',
  });
  try {
    await meridianStartApi();
  } catch (error) {
    // The menus keep working without the API. The reason goes to the error console.
    Zotero.logError(error);
  }
  Zotero.debug('Meridian Zotero Bridge ' + meridianVersion + ' started');
}

function shutdown(data, reason) {
  if (meridianApi) {
    try {
      meridianApi.stop();
    } catch (error) {
      Zotero.logError(error);
    }
    meridianApi = null;
  }
  [MERIDIAN_MENU_COLLECTION, MERIDIAN_MENU_ITEM].forEach(function (menuID) {
    try {
      if (Zotero.MenuManager && typeof Zotero.MenuManager.unregisterMenu === 'function') {
        Zotero.MenuManager.unregisterMenu(menuID);
      }
    } catch (error) {
      Zotero.logError(error);
    }
  });
  try {
    if (meridianPaneID && typeof Zotero.PreferencePanes.unregister === 'function') {
      Zotero.PreferencePanes.unregister(meridianPaneID);
    }
  } catch (error) {
    Zotero.logError(error);
  }
  meridianPaneID = null;
  meridianLib = null;
  meridianRootURI = null;
}

// Load lib/*.js as CommonJS-style modules inside a sandbox. Each module gets
// its own module object, and require() resolves only the lib modules above.
function meridianLoadLib(rootURI) {
  var loaded = {};
  var require = function (name) {
    var key = String(name).replace(/^\.\//, '').replace(/\.js$/, '');
    if (!(key in loaded)) {
      throw new Error('Meridian Zotero Bridge: unknown module ' + name);
    }
    return loaded[key];
  };
  ['normalize', 'bibkey', 'pdf', 'collections', 'duplicates'].concat(MERIDIAN_API_MODULES).forEach(function (key) {
    var sandbox = { module: { exports: {} }, require: require };
    Services.scriptloader.loadSubScript(rootURI + 'lib/' + key + '.js', sandbox);
    loaded[key] = sandbox.module.exports;
  });
  return loaded;
}

// The socket file is loaded on its own, with the Components and Services globals it needs.
// lib/ has no such access, so the static checks can tell the two apart.
function meridianLoadListener(rootURI) {
  var sandbox = { module: { exports: {} }, Components: Components, Services: Services };
  Services.scriptloader.loadSubScript(rootURI + 'api/listener.js', sandbox);
  return sandbox.module.exports;
}

function meridianSetDefaultPrefs(rootURI) {
  var branch = Services.prefs.getDefaultBranch('');
  var pref = function (name, value) {
    if (typeof value === 'boolean') {
      branch.setBoolPref(name, value);
    } else if (typeof value === 'number') {
      branch.setIntPref(name, value);
    } else {
      branch.setStringPref(name, String(value));
    }
  };
  Services.scriptloader.loadSubScript(rootURI + 'prefs.js', { pref: pref });
}

function meridianRegisterMenus() {
  var MenuManager = Zotero.MenuManager;
  if (!MenuManager || typeof MenuManager.registerMenu !== 'function') {
    throw new Error('Zotero.MenuManager.registerMenu is not available in this Zotero build');
  }
  MenuManager.registerMenu({
    menuID: MERIDIAN_MENU_COLLECTION,
    pluginID: MERIDIAN_ADDON_ID,
    target: 'main/library/collection',
    menus: [
      {
        menuType: 'menuitem',
        label: 'Import BibTeX here...',
        onCommand: function (event, context) {
          meridianRun('BibTeX import', function () {
            return meridianImportBibtex(context);
          });
        },
      },
    ],
  });
  MenuManager.registerMenu({
    menuID: MERIDIAN_MENU_ITEM,
    pluginID: MERIDIAN_ADDON_ID,
    target: 'main/library/item',
    menus: [
      {
        menuType: 'menuitem',
        label: 'Attach PDFs from folder...',
        onCommand: function (event, context) {
          meridianRun('PDF attach', function () {
            return meridianAttachPdfs(context);
          });
        },
      },
    ],
  });
}

function meridianRun(label, task) {
  Promise.resolve()
    .then(task)
    .catch(function (error) {
      Zotero.logError(error);
      meridianAlert(label + ' stopped: ' + error.message);
    });
}

function meridianMainWindow() {
  return Zotero.getMainWindow();
}

function meridianAlert(text) {
  Services.prompt.alert(meridianMainWindow(), 'Meridian Zotero Bridge', text);
}

function meridianField(item, name) {
  try {
    return item.getField(name) || '';
  } catch (error) {
    return '';
  }
}

// ---------------------------------------------------------------------------
// F1: Import BibTeX into the collection the menu was opened on.
// ---------------------------------------------------------------------------

async function meridianImportBibtex(context) {
  var collection = await meridianResolveCollection(context);
  var path = await meridianPickPath({
    folder: false,
    title: 'Choose a BibTeX file',
    filterName: 'BibTeX',
    pattern: '*.bib',
  });
  if (!path) return;

  var text = await IOUtils.readUTF8(path);
  var planned = await meridianPlanBibtex(text, collection.libraryID);
  var report = await meridianSaveBibtexPlan(planned.plan, collection);
  meridianAlert(meridianImportSummary(collection.name, report));
}

// Parses the text and classifies every entry against the library. Writes nothing.
// The count guard stops the import if the translator saved anything.
// See README, verification item 1.
async function meridianPlanBibtex(text, libraryID) {
  var before = await meridianLibraryEntries(libraryID);
  var parsed = await meridianParseBibtex(text);
  var after = await meridianLibraryEntries(libraryID);
  if (after.length !== before.length) {
    // The menu alert shows this text after "BibTeX import stopped: ". The change may be the translator's own
    // write or an unrelated one, so the text asks the user to check for items they did not add.
    throw meridianLib['api/validate'].apiError(
      409,
      'library_changed',
      'The library changed while the BibTeX file was read, so nothing was imported. Run the import again. ' +
        'If items you did not add appeared, review them by hand.'
    );
  }

  var entries = parsed.map(function (json) {
    return { title: json.title, doi: json.DOI, json: json };
  });
  return { plan: meridianLib.duplicates.planImport(entries, before), parsedCount: parsed.length };
}

// Saves the new entries from a plan. A null collection imports into the library root.
async function meridianSaveBibtexPlan(plan, collection) {
  var report = { imported: 0, exact: plan.exact.length, conflicts: plan.conflicts, failed: [] };
  for (var i = 0; i < plan.toImport.length; i++) {
    var entry = plan.toImport[i];
    try {
      await meridianSaveNewItem(entry.json, collection);
      report.imported += 1;
    } catch (error) {
      Zotero.logError(error);
      report.failed.push(entry.title || '(untitled)');
    }
  }
  return report;
}

async function meridianResolveCollection(context) {
  var opened = meridianCollectionFromContext(context);
  if (!opened || !opened.libraryID) {
    throw new Error('Open this menu on a collection.');
  }
  var all = await Zotero.Collections.getByLibrary(opened.libraryID, true);
  var result = meridianLib.collections.pickCollectionByName(all, opened.name, opened.id);
  if (result.status === 'ok') return result.collection;
  if (result.status === 'ambiguous') {
    throw new Error(
      'More than one collection is named "' + opened.name + '" in this library. Rename one and try again.'
    );
  }
  throw new Error('No collection named "' + opened.name + '" was found.');
}

function meridianCollectionFromContext(context) {
  var row = context && context.collectionTreeRow;
  if (row && row.ref) return row.ref;
  var pane = Zotero.getActiveZoteroPane && Zotero.getActiveZoteroPane();
  return pane && typeof pane.getSelectedCollection === 'function' ? pane.getSelectedCollection() : null;
}

async function meridianParseBibtex(text) {
  var translation = new Zotero.Translate.Import();
  translation.setString(text);
  var translators = await translation.getTranslators();
  var bibtex =
    translators.find(function (t) {
      return t.translatorID === MERIDIAN_BIBTEX_TRANSLATOR_ID;
    }) ||
    translators.find(function (t) {
      return t.label === 'BibTeX';
    });
  if (!bibtex) {
    throw new Error('The Zotero BibTeX translator is not available.');
  }
  translation.setTranslator(bibtex);

  var items = [];
  translation.setHandler('itemDone', function (_obj, item) {
    items.push(item);
  });
  translation.setHandler('error', function (_obj, error) {
    Zotero.logError(error);
  });
  // Intended as parse-only. Whether this option stops the translator saving items
  // is unverified in Zotero. The count guard in meridianImportBibtex detects it.
  await translation.translate({ libraryID: false });
  return items;
}

async function meridianLibraryEntries(libraryID) {
  // Zotero's signature is getAll(libraryID, onlyTopLevel, includeDeleted). Trashed items count as
  // existing, so a re-import does not bring back something the user already threw away.
  var items = await Zotero.Items.getAll(libraryID, true, true);
  var entries = [];
  items.forEach(function (item) {
    if (!item.isRegularItem()) return;
    entries.push({ title: meridianField(item, 'title'), doi: meridianField(item, 'DOI') });
  });
  return entries;
}

async function meridianSaveNewItem(json, collection) {
  var skip = {
    itemType: true,
    creators: true,
    tags: true,
    attachments: true,
    notes: true,
    collections: true,
    key: true,
    id: true,
    libraryID: true,
    itemID: true,
    citationKey: true,
    extra: true,
  };
  var item = new Zotero.Item(json.itemType || 'document');
  item.libraryID = collection ? collection.libraryID : Zotero.Libraries.userLibraryID;
  Object.keys(json).forEach(function (field) {
    var value = json[field];
    if (skip[field] || (typeof value !== 'string' && typeof value !== 'number') || value === '') return;
    try {
      item.setField(field, value);
    } catch (error) {
      // Field not valid for this item type: skip it, keep the rest.
    }
  });
  if (Array.isArray(json.creators)) item.setCreators(json.creators);
  if (Array.isArray(json.tags)) item.setTags(json.tags);

  // Write "BibTeX key: <key>" so that F2 can find the PDF later.
  var extra = typeof json.extra === 'string' ? json.extra : '';
  var key = meridianCitationKeyFromJson(json);
  if (key && !meridianLib.bibkey.readBibKey(extra)) {
    extra = (extra ? extra + '\n' : '') + 'BibTeX key: ' + key;
  }
  if (extra) item.setField('extra', extra);

  if (collection) item.setCollections([collection.id]);
  await item.saveTx();
}

// The BibTeX import translator keeps the entry's citekey in itemID, so lib/bibkey.js reads that too.
function meridianCitationKeyFromJson(json) {
  return meridianLib.bibkey.citationKeyFromImport(json);
}

function meridianImportSummary(collectionName, report) {
  var lines = [
    'Collection: ' + collectionName,
    'Imported: ' + report.imported,
    'Skipped as exact duplicates: ' + report.exact,
    'Conflicts, not imported: ' + report.conflicts.length,
  ];
  if (report.conflicts.length) {
    lines.push('', 'Same title, different DOI. Review these by hand:');
    report.conflicts.slice(0, MERIDIAN_REPORT_LINE_LIMIT).forEach(function (entry) {
      lines.push('- ' + (entry.title || '(untitled)') + ' [DOI in file: ' + (entry.doi || 'none') + ']');
    });
    if (report.conflicts.length > MERIDIAN_REPORT_LINE_LIMIT) {
      lines.push('...and ' + (report.conflicts.length - MERIDIAN_REPORT_LINE_LIMIT) + ' more');
    }
  }
  if (report.failed.length) {
    lines.push('', 'Failed to save: ' + report.failed.join('; '));
  }
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// F2: Attach <key>.pdf from a folder to the selected items.
// ---------------------------------------------------------------------------

async function meridianAttachPdfs(context) {
  var items = meridianSelectedItems(context).filter(function (item) {
    return item.isRegularItem();
  });
  if (items.length === 0) {
    meridianAlert('Select one or more regular items first.');
    return;
  }
  var folder = await meridianPickPath({ folder: true, title: 'Choose the folder with <key>.pdf files' });
  if (!folder) return;

  var link = Zotero.Prefs.get(MERIDIAN_PREF_PREFIX + 'linkPdfs', true) === true;
  var attached = [];
  var skipped = [];
  for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var label = meridianField(item, 'title') || 'item ' + item.id;
    var key = meridianLib.bibkey.readBibKey(meridianField(item, 'extra'));
    if (!key) {
      skipped.push(label + ': no BibTeX key line in Extra');
      continue;
    }
    if (await meridianHasPdfAttachment(item)) {
      skipped.push(label + ': already has a PDF');
      continue;
    }
    var path = PathUtils.join(folder, key + '.pdf');
    if (!(await IOUtils.exists(path))) {
      skipped.push(label + ': ' + key + '.pdf not found in folder');
      continue;
    }
    var head = await IOUtils.read(path, { maxBytes: 5 });
    if (!meridianLib.pdf.isPdfHeader(head)) {
      skipped.push(label + ': ' + key + '.pdf does not start with %PDF-');
      continue;
    }
    try {
      var file = Zotero.File.pathToFile(path);
      if (link) {
        await Zotero.Attachments.linkFromFile({ file: file, parentItemID: item.id });
      } else {
        await Zotero.Attachments.importFromFile({ file: file, parentItemID: item.id });
      }
      attached.push(label);
    } catch (error) {
      Zotero.logError(error);
      skipped.push(label + ': attach failed');
    }
  }

  var lines = [
    (link ? 'Linked ' : 'Copied into Zotero storage: ') + attached.length + ' PDF(s)',
    'Skipped: ' + skipped.length,
  ];
  skipped.slice(0, MERIDIAN_REPORT_LINE_LIMIT).forEach(function (line) {
    lines.push('- ' + line);
  });
  meridianAlert(lines.join('\n'));
}

function meridianSelectedItems(context) {
  if (context && Array.isArray(context.items) && context.items.length) return context.items;
  var pane = Zotero.getActiveZoteroPane && Zotero.getActiveZoteroPane();
  return pane && typeof pane.getSelectedItems === 'function' ? pane.getSelectedItems() : [];
}

async function meridianHasPdfAttachment(item) {
  var ids = item.getAttachments(false);
  for (var i = 0; i < ids.length; i++) {
    var attachment = await Zotero.Items.getAsync(ids[i]);
    if (attachment && attachment.isAttachment() && attachment.attachmentContentType === 'application/pdf') {
      return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Local write API. Routes and checks live in lib/api/; the socket lives in api/listener.js.
// ---------------------------------------------------------------------------

async function meridianStartApi() {
  var tokenPath = Services.prefs.getStringPref(MERIDIAN_API_TOKEN_PATH_PREF, '');
  if (!tokenPath) {
    Zotero.debug('Meridian Zotero Bridge: write API is off because no token file is set.');
    return;
  }
  var port = Services.prefs.getIntPref(MERIDIAN_API_PORT_PREF, MERIDIAN_API_DEFAULT_PORT);
  var token = (await IOUtils.readUTF8(tokenPath)).trim();
  if (!meridianLib['api/auth'].isUsableToken(token)) {
    throw new Error('The write API token file does not hold a usable token. Run the Meridian install again.');
  }
  var listenerModule = meridianLoadListener(meridianRootURI);
  var http = meridianLib['api/http'];
  var validate = meridianLib['api/validate'];
  // One tracker serves both write factories and the app, so a rolled-back batch sees every object it touched.
  var tracker = { inBatch: false, touched: [], created: [] };
  var app = meridianLib['api/app'].createApp({
    zotero: Zotero,
    version: meridianVersion,
    host: listenerModule.LOOPBACK_HOST + ':' + port,
    token: token,
    validate: validate,
    http: http,
    auth: meridianLib['api/auth'],
    tracker: tracker,
    items: meridianLib['api/items'].createItemWrites(meridianWriteDeps(validate, tracker)),
    collections: meridianLib['api/collections'].createCollectionWrites({ zotero: Zotero, validate: validate, tracker: tracker }),
    importBibtex: meridianApiImportBibtex,
    log: function (error) {
      Zotero.logError(error);
    },
  });
  meridianApi = listenerModule.startListener({
    port: port,
    createParser: function () {
      return http.createRequestParser();
    },
    handle: app.handle,
    admitHead: app.admitHead,
    rejectParse: app.rejectParse,
    internalFailure: function () {
      return app.rejectParse({ httpStatus: 500, code: 'internal', message: 'Zotero could not complete the request.' });
    },
    log: function (error) {
      Zotero.logError(error);
    },
  });
  Zotero.debug('Meridian Zotero Bridge: write API listening on port ' + meridianApi.port);
}

function meridianWriteDeps(validate, tracker) {
  return {
    zotero: Zotero,
    validate: validate,
    tracker: tracker,
    delay: meridianDelay,
    io: {
      join: PathUtils.join,
      makeDirectory: IOUtils.makeDirectory,
      write: IOUtils.write,
      remove: IOUtils.remove,
      stat: IOUtils.stat,
    },
    newId: function () {
      return Zotero.Utilities.randomString(16);
    },
    tempDir: function () {
      return Zotero.getTempDirectory().path;
    },
  };
}

// Resolves after the given milliseconds. The file check in lib/api/items.js uses it to stop waiting on a slow share.
function meridianDelay(ms) {
  return Zotero.Promise.delay(ms);
}

// Plans a BibTeX import for the API. Dry runs return the plan. Real runs save it.
async function meridianApiImportBibtex(request) {
  var validate = meridianLib['api/validate'];
  var libraryID = Zotero.Libraries.userLibraryID;
  var collection = null;
  if (request.collectionKey) {
    collection = Zotero.Collections.getByLibraryAndKey(libraryID, request.collectionKey);
    if (!collection) {
      throw validate.apiError(404, 'not_found', 'No collection with key ' + request.collectionKey + ' exists in the user library.');
    }
    if (collection.deleted) throw validate.invalid('collection_key', 'is in the trash');
  }
  var planned = await meridianPlanBibtex(request.text, libraryID);
  var plan = planned.plan;
  var result = {
    dry_run: request.dryRun,
    parsed: planned.parsedCount,
    would_import: plan.toImport.map(meridianPlanRow),
    exact_duplicates: plan.exact.length,
    conflicts: plan.conflicts.map(meridianPlanRow),
  };
  if (request.dryRun) return result;
  var report = await meridianSaveBibtexPlan(plan, collection);
  result.imported = report.imported;
  result.failed = report.failed;
  return result;
}

function meridianPlanRow(entry) {
  return { title: entry.title || '', doi: entry.doi || '' };
}

// ---------------------------------------------------------------------------
// File pickers. Each runs only after a menu command.
// ---------------------------------------------------------------------------

function meridianPickPath(options) {
  var Ci = Components.interfaces;
  var picker = Components.classes['@mozilla.org/filepicker;1'].createInstance(Ci.nsIFilePicker);
  picker.init(
    meridianMainWindow(),
    options.title,
    options.folder ? Ci.nsIFilePicker.modeGetFolder : Ci.nsIFilePicker.modeOpen
  );
  if (!options.folder) picker.appendFilter(options.filterName, options.pattern);
  return new Promise(function (resolve) {
    picker.open(function (result) {
      resolve(result === Ci.nsIFilePicker.returnOK && picker.file ? picker.file.path : null);
    });
  });
}
