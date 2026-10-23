'use strict';

// Decides whether a file may be linked, and which path to store. This module is pure logic. The operating
// system work is injected as deps.resolve, so the node tests can drive every branch with a fake. bootstrap.js
// supplies the Windows resolver, which asks Win32 for the real path of a file.
//
// A file is linked only when all of these hold:
//   - the bridge runs on Windows, the only platform the resolver supports;
//   - the user has allowed at least one folder (extensions.zotero.meridianZoteroBridge.linkFolders);
//   - the path passes the text rules, which need no disk access;
//   - the real path is a local file inside one of the allowed folders, compared with their real paths. A folder
//     that resolves to a drive root does not count, because it would allow the whole drive;
//   - the file is not a folder, not a link or cloud placeholder (on the path or at the end of it), has one name only,
//     and passes the text rules on its real path.
// The containment test comes before the other file checks, so a path outside every allowed folder gets the same
// refusal whether the file exists, is a folder, or has other names. That keeps the refusals from describing the disk.
//
// The stored location is the real path at the time of linking. Nothing re-checks it afterwards. Someone who can write
// to an allowed folder can move it and put a junction in its place, and the stored path would then reach another file.
//
// There is no time limit here. The resolver makes synchronous Win32 calls, so a timer could not stop them anyway.
// The resolver checks each folder on the path for a link before it enters it, and it asks about a network drive
// from the drive mapping before any call touches the path.

function createLinkPolicy(deps) {
  var v = deps.validate;

  function refused(message) {
    return v.apiError(422, 'linking_refused', message);
  }

  var NOT_INSIDE = 'This file is not inside a folder you allowed for linked files.';

  // Asks the resolver. A resolver that throws, or answers with nothing, counts as an error, which refuses the link.
  async function resolveSafely(path) {
    try {
      return (await deps.resolve(path)) || { status: 'error' };
    } catch (error) {
      return { status: 'error' };
    }
  }

  // A local drive path, such as the form the resolver returns after it removes the extended-length prefix.
  function isLocalDrivePath(path) {
    return /^[A-Za-z]:\\/.test(path);
  }

  // A drive root: a drive letter, a colon, and at most one separator.
  function isDriveRoot(path) {
    return /^[A-Za-z]:\\?$/.test(path);
  }

  // True when the file lies strictly inside the folder. Both are real paths from the same disk, so their letter case
  // already matches the disk's own. The folder's trailing separator is what stops a sibling folder whose name starts
  // the same way (papers2 beside papers) from counting as inside it.
  function isInside(file, folder) {
    var base = folder.replace(/[\\\/]+$/, '') + '\\';
    return file.indexOf(base) === 0 && file.length > base.length;
  }

  // Asks whether a real path lies inside one of the allowed folders. Each folder is resolved too, so a folder that
  // is a junction, a network share or a drive root cannot allow more than it should.
  async function insideAnyAllowed(real, folders) {
    for (var i = 0; i < folders.length; i++) {
      var base = await resolveSafely(folders[i]);
      // A folder reached through a link, or a network share, or a drive root, is not an allowed folder.
      if (base.status !== 'ok' || base.remote || base.leafReparse || !isLocalDrivePath(base.path) || isDriveRoot(base.path)) continue;
      if (isInside(real, base.path)) return true;
    }
    return false;
  }

  // Only used to choose between "no such file" and "not inside" for a missing path. It does not decide anything
  // about security, so a case-insensitive guess is enough.
  function textInsideAny(text, folders) {
    var lower = text.toLowerCase();
    return folders.some(function (folder) {
      var base = folder.replace(/[\\\/]+$/, '').toLowerCase() + '\\';
      return lower.indexOf(base) === 0 && lower.length > base.length;
    });
  }

  async function checkLinkable(path) {
    if (!deps.isWindows()) {
      throw v.apiError(422, 'linking_unavailable', 'Linking files works only on Windows in this version. Import the file instead.');
    }
    var folders = v.linkFolderList(deps.linkFolders());
    if (folders.length === 0) {
      throw refused(
        'No folder is allowed for linked files yet. Add a folder to extensions.zotero.meridianZoteroBridge.linkFolders, or import the file.'
      );
    }
    // Forward slashes are the same separator on Windows, so the rest of the policy sees backslashes only.
    var requested = String(path).replace(/\//g, '\\');
    // The text rules run first, so a refused path never reaches the disk.
    var problem = v.linkableFileProblem(requested);
    if (problem) throw refused(problem);

    var found = await resolveSafely(requested);
    if (found.status === 'missing') {
      // The message depends only on the text, so a file that is missing and a file that is outside the folders look the same.
      if (textInsideAny(requested, folders)) {
        throw v.apiError(422, 'file_not_found', 'No file exists at that path.');
      }
      throw refused(NOT_INSIDE);
    }
    if (found.status !== 'ok') {
      throw v.apiError(422, 'file_check_failed', 'The file could not be checked. Nothing was linked.');
    }
    if (found.remote) {
      throw refused('The file is on a network drive or share. Copy it to a folder on this computer first.');
    }
    var real = found.path;
    if (!isLocalDrivePath(real)) {
      throw refused('The file is not on a local drive with a letter, so it cannot be linked.');
    }
    // Containment comes before every other file check, so an outside path reveals nothing more than NOT_INSIDE.
    if (!(await insideAnyAllowed(real, folders))) {
      throw refused(NOT_INSIDE);
    }
    if (found.isDirectory) {
      throw refused('That path names a folder, not a file.');
    }
    if (found.leafReparse) {
      throw refused(
        'The file, or a folder on its path, is a link, junction or cloud placeholder such as an online-only OneDrive file. Make the real file available on this computer, or import it.'
      );
    }
    if (found.hardLinks > 1) {
      throw refused('The file has more than one name (a hard link), so it cannot be linked. Import it instead.');
    }
    var realProblem = v.linkableFileProblem(real);
    if (realProblem) throw refused(realProblem);
    return { path: real };
  }

  return { checkLinkable: checkLinkable };
}

module.exports = { createLinkPolicy: createLinkPolicy };
