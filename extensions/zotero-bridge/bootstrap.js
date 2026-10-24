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
  'api/grants',
  'api/link_policy',
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
  // Linking goes through the same policy as the write API: an allowed folder, the text rules, and the real path.
  var policy = link ? meridianLinkPolicy(meridianLib['api/validate']) : null;
  var attached = [];
  var skipped = [];
  // Two selected items whose keys differ only in case name the same <key>.pdf on Windows, so neither may take it.
  var keyCounts = {};
  items.forEach(function (item) {
    var token = meridianLib.bibkey.readBibKeyToken(meridianField(item, 'extra'));
    if (token) keyCounts[token.toLowerCase()] = (keyCounts[token.toLowerCase()] || 0) + 1;
  });
  for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var label = meridianField(item, 'title') || 'item ' + item.id;
    // One item's failure is reported against that item. The loop goes on, so the summary still shows everything.
    try {
      var token = meridianLib.bibkey.readBibKeyToken(meridianField(item, 'extra'));
      if (!token) {
        skipped.push(label + ': no BibTeX key line in Extra');
        continue;
      }
      if (!meridianLib.bibkey.isSafeFileKey(token)) {
        skipped.push(label + ': the BibTeX key cannot be used as a file name');
        continue;
      }
      var key = token;
      if (keyCounts[key.toLowerCase()] > 1) {
        skipped.push(label + ': another selected item has the same key in a different case, so neither is attached');
        continue;
      }
      if (await meridianHasPdfAttachment(item)) {
        skipped.push(label + ': already has a PDF');
        continue;
      }
      var path = PathUtils.join(folder, key + '.pdf');
      var target = path;
      if (link) {
        // The link policy runs before anything touches the disk, so a refused path is never probed. It also returns the
        // real path, which is the one that gets stored. A file the policy refuses is reported, not copied.
        try {
          target = (await policy.checkLinkable(path)).path;
        } catch (error) {
          if (!(error && error.isApiError)) throw error;
          skipped.push(
            label + ': ' + (error.code === 'file_not_found' ? key + '.pdf not found in folder' : 'not linked. ' + error.message)
          );
          continue;
        }
      }
      if (!(await IOUtils.exists(target))) {
        skipped.push(label + ': ' + key + '.pdf not found in folder');
        continue;
      }
      var head = await IOUtils.read(target, { maxBytes: 5 });
      if (!meridianLib.pdf.isPdfHeader(head)) {
        skipped.push(label + ': ' + key + '.pdf does not start with %PDF-');
        continue;
      }
      if (link) {
        await Zotero.Attachments.linkFromFile({ file: Zotero.File.pathToFile(target), parentItemID: item.id });
      } else {
        await Zotero.Attachments.importFromFile({ file: Zotero.File.pathToFile(target), parentItemID: item.id });
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
  // Write grants: a person approves each app at the desktop. The token alone never writes. See lib/api/grants.js.
  var grants = meridianLib['api/grants'].createGrants({
    validate: validate,
    now: Date.now,
    randomHex: meridianRandomHex,
    ask: meridianAskWriteGrant,
    constantTimeEquals: meridianLib['api/auth'].constantTimeEquals,
  });
  var app = meridianLib['api/app'].createApp({
    zotero: Zotero,
    grants: grants,
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
    linkPolicy: meridianLinkPolicy(validate),
    validate: validate,
    tracker: tracker,
    io: {
      join: PathUtils.join,
      makeDirectory: IOUtils.makeDirectory,
      write: IOUtils.write,
      remove: IOUtils.remove,
    },
    newId: function () {
      return Zotero.Utilities.randomString(16);
    },
    tempDir: function () {
      return Zotero.getTempDirectory().path;
    },
  };
}

// The desktop prompt for a write grant. Deny is the default, so Enter and Escape both refuse. The app name is
// self-reported, and the dialog says so. The request is answered after a short delay, so the HTTP response to the
// request that asked is sent before the dialog opens.
function meridianAskWriteGrant(appName) {
  return Zotero.Promise.delay(50).then(function () {
    var index = Zotero.Prompt.confirm({
      title: 'Meridian Zotero Bridge: write access',
      text:
        'An app asks to write to your Zotero library:\n\n"' + appName + '"\n\n' +
        'The app name is what the app says it is. Allow only if you started this request yourself. ' +
        'A session grant ends when Zotero restarts, or after 8 hours.',
      // Deny is the second button, because Escape comes back as that button's index. Only the two allow buttons
      // grant anything, and every other answer, Escape included, refuses.
      button0: 'Allow for 1 hour',
      button1: 'Deny',
      button2: 'Allow until Zotero restarts',
      defaultButton: 1,
    });
    if (index === 0) return 'hour';
    if (index === 2) return 'session';
    return 'deny';
  });
}

// Random hex from a secure source. Zotero's own keys come from Math.random, which is not enough for a credential,
// so this uses the platform's generator and fails rather than fall back to a weaker one.
function meridianRandomHex(bytes) {
  var buffer = new Uint8Array(bytes);
  if (typeof crypto !== 'undefined' && crypto && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(buffer);
  } else {
    var generator = Components.classes['@mozilla.org/security/random-generator;1'].createInstance(
      Components.interfaces.nsIRandomGenerator
    );
    buffer = generator.generateRandomBytes(bytes);
  }
  return Array.prototype.map
    .call(buffer, function (b) {
      return ('0' + b.toString(16)).slice(-2);
    })
    .join('');
}

// The link policy for the write API and the F2 menu. See lib/api/link_policy.js for the rules it applies.
function meridianLinkPolicy(validate) {
  return meridianLib['api/link_policy'].createLinkPolicy({
    validate: validate,
    isWindows: function () {
      return Zotero.isWin;
    },
    linkFolders: function () {
      return Zotero.Prefs.get(MERIDIAN_PREF_PREFIX + 'linkFolders', true) || '';
    },
    resolve: meridianResolveLinkPath,
  });
}

// Win32 calls for the real path of a file. Firefox's nsIFile does not follow junctions or symbolic links on
// Windows: its isSymlink() is always false there, and its target and normalize() do not resolve the path.
// So the bridge asks the OS directly. Loaded on first use, and only on Windows.
var meridianWin32Api = null;

function meridianWin32() {
  if (meridianWin32Api) return meridianWin32Api;
  var ctypes = ChromeUtils.importESModule('resource://gre/modules/ctypes.sys.mjs').ctypes;
  var kernel32 = ctypes.open('kernel32.dll');
  var abi = ctypes.winapi_abi;
  var FileTime = ctypes.StructType('FILETIME', [
    { dwLowDateTime: ctypes.uint32_t },
    { dwHighDateTime: ctypes.uint32_t },
  ]);
  var ByHandleInfo = ctypes.StructType('BY_HANDLE_FILE_INFORMATION', [
    { dwFileAttributes: ctypes.uint32_t },
    { ftCreationTime: FileTime },
    { ftLastAccessTime: FileTime },
    { ftLastWriteTime: FileTime },
    { dwVolumeSerialNumber: ctypes.uint32_t },
    { nFileSizeHigh: ctypes.uint32_t },
    { nFileSizeLow: ctypes.uint32_t },
    { nNumberOfLinks: ctypes.uint32_t },
    { nFileIndexHigh: ctypes.uint32_t },
    { nFileIndexLow: ctypes.uint32_t },
  ]);
  meridianWin32Api = {
    ctypes: ctypes,
    ByHandleInfo: ByHandleInfo,
    getFileAttributesW: kernel32.declare('GetFileAttributesW', abi, ctypes.uint32_t, ctypes.char16_t.ptr),
    getDriveTypeW: kernel32.declare('GetDriveTypeW', abi, ctypes.uint32_t, ctypes.char16_t.ptr),
    // Access 0 and the share flags are the ones Firefox's own path code uses. BACKUP_SEMANTICS lets a folder open too.
    // OPEN_REPARSE_POINT is left out on purpose, so the call follows links to their targets.
    createFileW: kernel32.declare(
      'CreateFileW',
      abi,
      ctypes.intptr_t,
      ctypes.char16_t.ptr,
      ctypes.uint32_t,
      ctypes.uint32_t,
      ctypes.voidptr_t,
      ctypes.uint32_t,
      ctypes.uint32_t,
      ctypes.intptr_t
    ),
    getFinalPathNameByHandleW: kernel32.declare(
      'GetFinalPathNameByHandleW',
      abi,
      ctypes.uint32_t,
      ctypes.intptr_t,
      ctypes.char16_t.ptr,
      ctypes.uint32_t,
      ctypes.uint32_t
    ),
    getFileInformationByHandle: kernel32.declare('GetFileInformationByHandle', abi, ctypes.int, ctypes.intptr_t, ByHandleInfo.ptr),
    closeHandle: kernel32.declare('CloseHandle', abi, ctypes.int, ctypes.intptr_t),
  };
  return meridianWin32Api;
}

// CreateFileW gives 0 or -1 (INVALID_HANDLE_VALUE) on failure. A ctypes integer may arrive as a number or an object, so both are compared as text.
function meridianBadHandle(handle) {
  var value = Number(String(handle));
  return isNaN(value) || value === 0 || value === -1;
}

// Resolves a path to the real path the OS sees. Status 'missing' means nothing is there, and 'error' means any other
// failure. When the status is 'ok', the result gives the real path without its extended-length prefix, whether the
// requested path is a folder, whether a link or cloud placeholder (a reparse point) sits anywhere on it, how many names
// the file has, and whether it sits on a network share.
//
// A network drive is answered from the drive mapping, before any call touches the path. Then the path is walked one
// component at a time, and each component is checked for a reparse point before the walk goes below it. Opening a path
// follows a junction in any folder on it, and a junction can point at a network share, so the check has to come first.
// A failed hard-link count is an error, not one name, so it cannot slip through. A failed attribute lookup is reported
// as missing: the bridge cannot read the error code reliably through ctypes, and both outcomes refuse the link.
async function meridianResolveLinkPath(path) {
  var w = meridianWin32();
  if (!/^[A-Za-z]:\\/.test(path)) return { status: 'error' };
  // GetDriveType reads the drive mapping and does not open the path. 1 is no such drive, and 4 is a network drive.
  var driveType = w.getDriveTypeW(path.substring(0, 3));
  if (driveType === 1) return { status: 'missing' };
  if (driveType === 4) {
    return { status: 'ok', path: path, leafReparse: false, hardLinks: 1, isDirectory: false, remote: true };
  }
  // Each prefix of the path is asked about before the walk goes below it. GetFileAttributes on a prefix reads that
  // prefix's own attributes and does not enter a junction at its last part, so a junction stops the walk here.
  var parts = path.split('\\');
  var prefix = parts[0] + '\\';
  var attributes = 0;
  for (var i = 1; i < parts.length; i++) {
    if (parts[i] === '') continue;
    prefix = prefix.length === parts[0].length + 1 ? prefix + parts[i] : prefix + '\\' + parts[i];
    attributes = w.getFileAttributesW(prefix);
    if (attributes === 0xffffffff) return { status: 'missing' };
    if (attributes & 0x400) {
      return { status: 'ok', path: path, leafReparse: true, hardLinks: 1, isDirectory: (attributes & 0x10) !== 0, remote: false };
    }
  }
  // The walk above found no reparse point on any part of the path, so opening it follows nothing that was not checked.
  var leafReparse = false;
  var isDirectory = (attributes & 0x10) !== 0;
  var handle = w.createFileW(path, 0, 0x7, null, 3, 0x02000000, 0);
  if (meridianBadHandle(handle)) return { status: 'error' };
  try {
    var size = 32768;
    var buffer = new (w.ctypes.ArrayType(w.ctypes.char16_t, size))();
    var length = w.getFinalPathNameByHandleW(handle, buffer, size, 0);
    if (length === 0 || length >= size) return { status: 'error' };
    var real = buffer.readString().substring(0, length);
    var remote = false;
    if (real.indexOf('\\\\?\\UNC\\') === 0) {
      real = '\\\\' + real.substring(8);
      remote = true;
    } else if (real.indexOf('\\\\?\\') === 0) {
      real = real.substring(4);
    }
    if (real.indexOf('\\\\') === 0) remote = true;
    var info = new w.ByHandleInfo();
    if (!w.getFileInformationByHandle(handle, info.address())) return { status: 'error' };
    return {
      status: 'ok',
      path: real,
      leafReparse: leafReparse,
      hardLinks: info.nNumberOfLinks,
      isDirectory: isDirectory,
      remote: remote,
    };
  } finally {
    w.closeHandle(handle);
  }
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
