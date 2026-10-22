/* global Zotero, Services, IOUtils, PathUtils, Components */
//
// Meridian Zotero Bridge: bootstrap glue for the Zotero 7+ bootstrap structure.
//
// Design line: this file has no HTTP endpoint, no server, no watcher, no timer
// that picks up outside files, and no code path another program can trigger.
// Every write starts from a menu command the user chose inside Zotero.
// The decisions live in lib/ and are unit-tested with node --test. This file
// only connects Zotero APIs to that logic.
//
// Status: source only. Not run inside Zotero yet. See README.md.

var MERIDIAN_ADDON_ID = 'meridian-zotero@usemeridian.us';
var MERIDIAN_PREF_PREFIX = 'extensions.zotero.meridianZoteroBridge.';
var MERIDIAN_BIBTEX_TRANSLATOR_ID = '9cb70025-a888-4a29-a210-93ec52da40d4';
var MERIDIAN_MENU_COLLECTION = 'meridian-zotero-bridge-collection-menu';
var MERIDIAN_MENU_ITEM = 'meridian-zotero-bridge-item-menu';
var MERIDIAN_REPORT_LINE_LIMIT = 25;

var meridianRootURI = null;
var meridianLib = null;
var meridianPaneID = null;

function install() {}

function uninstall() {}

async function startup(data, reason) {
  var rootURI = data && data.rootURI;
  await Zotero.initializationPromise;
  meridianRootURI = rootURI;
  meridianLib = meridianLoadLib(rootURI);
  meridianSetDefaultPrefs(rootURI);
  meridianRegisterMenus();
  meridianPaneID = await Zotero.PreferencePanes.register({
    pluginID: MERIDIAN_ADDON_ID,
    src: rootURI + 'content/preferences.xhtml',
    label: 'Meridian Zotero Bridge',
  });
  Zotero.debug('Meridian Zotero Bridge ' + (data && data.version) + ' started');
}

function shutdown(data, reason) {
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
  ['normalize', 'bibkey', 'pdf', 'collections', 'duplicates'].forEach(function (key) {
    var sandbox = { module: { exports: {} }, require: require };
    Services.scriptloader.loadSubScript(rootURI + 'lib/' + key + '.js', sandbox);
    loaded[key] = sandbox.module.exports;
  });
  return loaded;
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
  var before = await meridianLibraryEntries(collection.libraryID);
  var parsed = await meridianParseBibtex(text);

  // Guard: parsing must not write to the library. If the translator saved
  // anything, stop before classifying and report it. See README, verification item 1.
  var after = await meridianLibraryEntries(collection.libraryID);
  if (after.length !== before.length) {
    throw new Error(
      'The BibTeX translator wrote items to the library before the duplicate check. ' +
        'Nothing else was imported. Review the newly added items by hand.'
    );
  }

  var entries = parsed.map(function (json) {
    return { title: json.title, doi: json.DOI, json: json };
  });
  var plan = meridianLib.duplicates.planImport(entries, before);

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
  meridianAlert(meridianImportSummary(collection.name, report));
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
  var items = await Zotero.Items.getAll(true, libraryID);
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
  item.libraryID = collection.libraryID;
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

  item.setCollections([collection.id]);
  await item.saveTx();
}

function meridianCitationKeyFromJson(json) {
  var candidate = null;
  if (typeof json.citationKey === 'string') {
    candidate = json.citationKey;
  } else if (typeof json.extra === 'string') {
    var match = json.extra.match(/^[ \t]*Citation Key:[ \t]*(\S+)[ \t\r]*$/im);
    candidate = match ? match[1] : null;
  }
  return candidate && meridianLib.bibkey.isSafeBibKey(candidate) ? candidate : null;
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
