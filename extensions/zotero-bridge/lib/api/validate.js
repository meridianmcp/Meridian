'use strict';

// Input checks for the write API. Each rejection carries a stable code, so a
// caller can tell a bad field from a bad token. Nothing here touches Zotero.

var KEY_RE = /^[A-Z0-9]{8}$/;
var PREDICATE_RE = /^[a-z]+:[A-Za-z]+$/;
var BASE64_RE = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
var FILENAME_BAD_RE = /[\\/\x00-\x1f\x7f]/;
// Windows device names. A file named CON.pdf or nul.txt opens the device, so these stems are refused.
// The superscript digits are the COM and LPT variants that Windows also reserves.
var RESERVED_NAME_RE = /^(CON|PRN|AUX|NUL|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3])$/i;
// Two leading separators name a network share (//host/share) or a device path (the \\?\ and \\.\ forms).
// Zotero would stat the share over SMB, so these are refused before a path reaches it.
var UNC_OR_DEVICE_RE = /^[\\/]{2}|^\\[?.]/;
// A drive-letter path with a separator after the colon, or a rooted path whose slash is not doubled.
var ABSOLUTE_RE = /^([A-Za-z]:[\\/]|\/(?![\\/]))/;

var LIMITS = {
  fieldValue: 100000,
  noteHtml: 2000000,
  name: 500,
  tag: 255,
  title: 512,
  contentType: 255,
  path: 4096,
  filename: 255,
  predicate: 100,
  relationObject: 2000,
  bibtex: 8 * 1024 * 1024,
  attachmentBytes: 24 * 1024 * 1024,
  creators: 500,
  tags: 500,
  keys: 500,
  operations: 200,
};

function apiError(httpStatus, code, message, details) {
  var error = new Error(message);
  error.isApiError = true;
  error.httpStatus = httpStatus;
  error.code = code;
  if (details !== undefined) error.details = details;
  return error;
}

function invalid(name, message) {
  return apiError(422, 'invalid_value', name + ' ' + message + '.');
}

function requireObject(value, name) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw invalid(name, 'must be a JSON object');
  }
  return value;
}

function optionalString(value, name, max) {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') throw invalid(name, 'must be a string');
  if (value.length > max) throw invalid(name, 'must be at most ' + max + ' characters');
  return value;
}

function requireString(value, name, max) {
  if (value === undefined || value === null || (typeof value === 'string' && value.trim() === '')) {
    throw invalid(name, 'is required');
  }
  return optionalString(value, name, max);
}

function optionalBoolean(value, name) {
  if (value === undefined || value === null) return false;
  if (typeof value !== 'boolean') throw invalid(name, 'must be true or false');
  return value;
}

function optionalInteger(value, name) {
  if (value === undefined || value === null) return undefined;
  if (!Number.isInteger(value) || value < 0) throw invalid(name, 'must be a whole number, zero or more');
  return value;
}

// Optimistic concurrency. A caller sends back the date_modified it read (expected_modified), or the
// version it read (expected_version). Local libraries keep version at 0 until a sync, so date_modified
// is the token that changes for them. Both are checked when both are sent.
function checkPrecondition(object, body, noun) {
  if (!body || typeof body !== 'object') return;
  if (body.expected_modified !== undefined && body.expected_modified !== null) {
    if (typeof body.expected_modified !== 'string' || body.expected_modified.length > 40) {
      throw invalid('expected_modified', 'must be the date_modified string the API reported');
    }
    if (object.dateModified !== body.expected_modified) {
      throw apiError(
        409,
        'changed_since_read',
        'The ' + noun + ' changed after you read it. Its date_modified is now ' + object.dateModified + '. Read it again before writing.',
        { current_modified: object.dateModified }
      );
    }
  }
  if (body.expected_version !== undefined && body.expected_version !== null) {
    var version = optionalInteger(body.expected_version, 'expected_version');
    if (object.version !== version) {
      throw apiError(
        409,
        'version_conflict',
        'The ' + noun + ' is at version ' + object.version + ', not ' + version + '. Read it again before writing.',
        { current_version: object.version }
      );
    }
  }
}

function keyValue(value, name) {
  if (typeof value !== 'string' || !KEY_RE.test(value)) {
    throw invalid(name, 'must be an 8-character Zotero key of capital letters and digits');
  }
  return value;
}

function optionalKey(value, name) {
  if (value === undefined || value === null) return null;
  return keyValue(value, name);
}

function keyList(value, name) {
  if (!Array.isArray(value)) throw invalid(name, 'must be a list of keys');
  if (value.length > LIMITS.keys) throw invalid(name, 'may hold at most ' + LIMITS.keys + ' keys');
  return value.map(function (key, index) {
    return keyValue(key, name + '[' + index + ']');
  });
}

function optionalList(value, name, max) {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) throw invalid(name, 'must be a list');
  if (value.length > max) throw invalid(name, 'may hold at most ' + max + ' entries');
  return value;
}

// Decodes standard base64 with padding. Rejects anything else rather than skipping it.
function decodeBase64(text, name, maxBytes) {
  if (!BASE64_RE.test(text)) throw invalid(name, 'must be standard base64 with padding');
  var alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  var padding = text.endsWith('==') ? 2 : text.endsWith('=') ? 1 : 0;
  var byteLength = (text.length / 4) * 3 - padding;
  if (byteLength > maxBytes) throw invalid(name, 'decodes to more than ' + maxBytes + ' bytes');
  var out = new Uint8Array(byteLength);
  var position = 0;
  for (var i = 0; i < text.length; i += 4) {
    var chunk = 0;
    var count = 0;
    for (var j = 0; j < 4; j++) {
      var ch = text.charAt(i + j);
      var value = ch === '=' ? 0 : alphabet.indexOf(ch);
      chunk = (chunk << 6) | value;
      if (ch !== '=') count++;
    }
    var bytes = [(chunk >> 16) & 0xff, (chunk >> 8) & 0xff, chunk & 0xff];
    for (var k = 0; k < count - 1 && position < byteLength; k++) {
      out[position++] = bytes[k];
    }
  }
  return out;
}

// A file name the caller chooses becomes a path inside Zotero's storage, so it
// must be one plain name with no separators, control characters, or Windows device stem.
function safeFilename(name) {
  if (FILENAME_BAD_RE.test(name) || name === '.' || name === '..' || name.trim() !== name || /\.$/.test(name)) {
    throw invalid('filename', 'must be a plain file name with no folder parts');
  }
  // Windows reads the part before the first dot or colon as the device name, so CON.pdf is the CON device.
  var stem = name.split(/[.:]/)[0].replace(/ +$/, '');
  if (RESERVED_NAME_RE.test(stem)) {
    throw invalid('filename', 'must not be a reserved Windows device name such as CON or NUL');
  }
  return name;
}

// Only a drive path or a single rooted path passes. Shares and device paths are refused first.
function isAbsolutePath(path) {
  if (typeof path !== 'string' || path.indexOf('\u0000') !== -1) return false;
  if (UNC_OR_DEVICE_RE.test(path)) return false;
  return ABSOLUTE_RE.test(path);
}

// Extensions a linked file may have. Zotero attachments are documents, figures and media. Keys, configs, scripts,
// databases and archives are not on the list, so a linked file can never be one of them.
var LINKABLE_EXTENSIONS = [
  'pdf', 'epub', 'djvu', 'doc', 'docx', 'odt', 'rtf', 'txt', 'md', 'html', 'htm', 'csv', 'tsv',
  'xls', 'xlsx', 'ods', 'ppt', 'pptx', 'odp', 'png', 'jpg', 'jpeg', 'gif', 'tif', 'tiff', 'webp',
  'bmp', 'svg', 'mp3', 'm4a', 'wav', 'mp4', 'mov',
];

// Folders that hold credentials, profiles or app state. A path through any of them is refused.
var SENSITIVE_SEGMENTS = [
  'appdata', '.meridian', '.ssh', '.gnupg', '.aws', '.azure', '.kube', '.docker', '.git', '.password-store',
];

// Null when the path may be linked, otherwise the reason it may not. It judges the text of the path only.
function linkableFileProblem(path) {
  var segments = String(path).split(/[\\\/]+/).map(function (s) { return s.toLowerCase(); }).filter(function (s) { return s !== ''; });
  for (var i = 0; i < segments.length; i++) {
    if (SENSITIVE_SEGMENTS.indexOf(segments[i]) !== -1) {
      return 'That folder holds credentials, profiles or app data, so it cannot be linked.';
    }
  }
  var name = segments.length ? segments[segments.length - 1] : '';
  var dot = name.lastIndexOf('.');
  var ext = dot > 0 ? name.slice(dot + 1) : '';
  if (LINKABLE_EXTENSIONS.indexOf(ext) === -1) {
    return 'Only documents, images and media can be linked. Import the file instead.';
  }
  return null;
}
function predicateValue(value, name) {
  if (typeof value !== 'string' || !PREDICATE_RE.test(value)) {
    throw invalid(name, 'must look like dc:relation');
  }
  return value;
}

function relationObjectValue(value, name) {
  if (typeof value !== 'string' || value === '' || value.length > LIMITS.relationObject || /\s/.test(value)) {
    throw invalid(name, 'must be one non-empty string with no spaces');
  }
  return value;
}

module.exports = {
  LIMITS: LIMITS,
  apiError: apiError,
  invalid: invalid,
  requireObject: requireObject,
  optionalString: optionalString,
  requireString: requireString,
  optionalBoolean: optionalBoolean,
  optionalInteger: optionalInteger,
  checkPrecondition: checkPrecondition,
  keyValue: keyValue,
  optionalKey: optionalKey,
  keyList: keyList,
  optionalList: optionalList,
  decodeBase64: decodeBase64,
  safeFilename: safeFilename,
  isAbsolutePath: isAbsolutePath,
  linkableFileProblem: linkableFileProblem,
  predicateValue: predicateValue,
  relationObjectValue: relationObjectValue,
};
