// Resolves a project-relative file path (e.g. "chapters/intro.tex") to the
// real-time doc id that OverleafProjectSession.joinDoc()/write.js's
// applyFieldEdit() need -- the "known real gap, not yet solved" cli.js's own
// header comment used to flag: there was no way to go from a human-typed
// path to a docId without already knowing the raw Mongo ObjectId.
//
// Closes that gap using the joinProjectResponse doc-tree shape, now
// CONFIRMED against Overleaf's real, current open-source frontend
// (github.com/overleaf/overleaf, AGPL-3.0), read for PROTOCOL/SHAPE FACTS
// ONLY -- this file's code is 100% original, not copied or adapted from
// Overleaf's or any third party's implementation. Two independent sources
// agree:
//
//   1. SERVER (already the basis for the pinned Meridian decision
//      "Overleaf real-time OT protocol: full reverse-engineered spec"):
//      services/real-time/app/js/Router.js emits
//        client.emit('joinProjectResponse', { publicId: client.publicId, project, permissionsLevel, protocolVersion })
//      i.e. the WHOLE project document (as WebApiManager.js fetched it) is
//      the `project` field of the response.
//
//   2. FRONTEND (this file's own independent corroboration, read directly,
//      not from memory):
//      - services/web/frontend/js/features/ide-react/context/ide-react-context.tsx's
//        `handleJoinProjectResponse` destructures `project` straight off the
//        joinProjectResponse payload and stores it verbatim (minus a couple
//        renamed fields unrelated to the file tree) as the app's
//        ProjectContext `project` value.
//      - services/web/frontend/js/shared/context/file-tree-data-context.tsx:
//          const [rootFolder, setRootFolder] = useState(project?.rootFolder)
//          const initialState = (rootFolder?: Folder[]) => {
//            const fileTreeData = rootFolder?.[0]
//            ...
//        CONFIRMS `project.rootFolder` is an ARRAY (`Folder[]`, per
//        services/web/frontend/js/shared/context/types/project-metadata.tsx's
//        own `rootFolder?: Folder[]` field), and that the frontend always
//        takes element [0] as the actual tree root -- never the array
//        itself. `unwrapRootFolder()` below does exactly that.
//      - services/web/types/folder.ts (exact, fetched verbatim):
//          export type Folder = {
//            _id: string
//            name: string
//            docs: Doc[]
//            folders: Folder[]
//            fileRefs: FileRef[]
//          }
//      - services/web/types/doc.ts (exact, fetched verbatim):
//          export type Doc = { _id: string; name: string }
//      - The docId-identity question: services/web/frontend/js/features/ide-react/context/editor-manager-context.tsx's
//        `doOpenNewDocument(doc: Doc)` calls `openDocs.getDocument(doc._id)`;
//        open-documents.ts's `createDoc(docId)` does
//        `new DocumentContainer(docId, ...)`; document-container.ts's
//        DocumentContainer constructor stores that as `readonly doc_id`, and
//        `joinDoc()` does `this.socket.emit('joinDoc', this.doc_id, ...)`.
//        That is a complete, traced chain from a file-tree Doc's `_id` to
//        the literal `joinDoc` wire argument -- CONFIRMS (does not merely
//        suggest) that the tree's `_id` field IS the doc's real-time id.
//
// Not covered (flagged, not guessed): FileRef entries (binary/non-.tex
// assets) have no real-time OT doc -- resolveDocIdByPath() reports these
// with a distinct, explicit reason rather than silently treating them as
// "not found", per this codebase's "never silently misreport" convention
// (see write.js/range-locate.js's own {ok:false, reason} style, reused here
// unchanged).

/** A doc leaf in the Overleaf file tree (`services/web/types/doc.ts`,
 * fetched verbatim -- see the header comment above). */
export interface FileTreeDoc {
  _id: string;
  name: string;
}

/** A binary/non-.tex asset leaf -- has no real-time OT doc (see header
 * comment's "Not covered" note). Only `name` is actually read by this file;
 * `_id` is included since every real fileRef entry carries one. */
export interface FileTreeFileRef {
  _id: string;
  name: string;
}

/** A folder node (`services/web/types/folder.ts`, fetched verbatim -- see
 * the header comment above). */
export interface FileTreeFolder {
  _id: string;
  name: string;
  docs: FileTreeDoc[];
  folders: FileTreeFolder[];
  fileRefs: FileTreeFileRef[];
}

/** The slice of a real `joinProjectResponse`'s `project` field this module
 * reads -- just `rootFolder`, per the header comment's frontend citations. */
export interface OverleafProjectLike {
  rootFolder?: FileTreeFolder[];
}

/**
 * `project.rootFolder` is confirmed to be a one-element array wrapping the
 * actual root `Folder` node (see this file's header). Returns `null` if the
 * project has no `rootFolder` at all (e.g. a joinProjectResponse captured
 * before OverleafProjectSession's own rootFolder-capture wiring existed, or
 * a malformed/test payload) rather than throwing -- callers already have to
 * handle "not connected yet" as a distinct state.
 */
export function unwrapRootFolder(project?: OverleafProjectLike): FileTreeFolder | null {
  return (project && project.rootFolder && project.rootFolder[0]) || null;
}

export type ResolveDocIdResult = { ok: true; docId: string } | { ok: false; reason: string };

/**
 * Resolves a `/`-separated, project-relative path (e.g. "chapters/intro.tex"
 * or "main.tex") to the docId `joinDoc()`/`applyFieldEdit()` need, by
 * walking the confirmed `{_id, name, folders, docs, fileRefs}` shape.
 * Leading/trailing slashes are ignored (`"/main.tex"` and `"main.tex"` are
 * the same request) -- matches the tolerant path handling every Overleaf
 * frontend call site above already applies (see e.g. file.ts's own
 * `path === ''` root check).
 *
 * Never throws -- returns `{ok:true, docId}` or `{ok:false, reason}`,
 * matching write.js's `computeFieldEditOps` convention so a CLI caller can
 * report a precise reason instead of a generic error.
 *
 * @param rootFolder  already-unwrapped, e.g. from `unwrapRootFolder()`
 */
export function resolveDocIdByPath(rootFolder: FileTreeFolder | null, path: string): ResolveDocIdResult {
  if (!rootFolder) {
    return { ok: false, reason: "No project file tree available -- has the session's joinProjectResponse arrived yet?" };
  }
  const segments = String(path)
    .split("/")
    .filter((s) => s.length > 0);
  if (segments.length === 0) {
    return { ok: false, reason: "Empty path -- the project root itself is a folder, not a doc." };
  }

  let current: FileTreeFolder = rootFolder;
  const walked: string[] = [];
  for (let i = 0; i < segments.length - 1; i++) {
    const name = segments[i];
    walked.push(name);
    const next = (current.folders || []).find((f) => f.name === name);
    if (!next) {
      return { ok: false, reason: `No folder named "${name}" under "${walked.slice(0, -1).join("/") || "/"}".` };
    }
    current = next;
  }

  const leaf = segments[segments.length - 1];
  const doc = (current.docs || []).find((d) => d.name === leaf);
  if (doc) {
    return { ok: true, docId: doc._id };
  }
  const fileRef = (current.fileRefs || []).find((f) => f.name === leaf);
  if (fileRef) {
    return {
      ok: false,
      reason: `"${path}" is a binary file (fileRef), not an editable doc -- there is no real-time OT doc to joinDoc() for it.`,
    };
  }
  const folder = (current.folders || []).find((f) => f.name === leaf);
  if (folder) {
    return { ok: false, reason: `"${path}" is a folder, not a doc.` };
  }
  return { ok: false, reason: `No doc named "${leaf}" found in "${walked.join("/") || "/"}".` };
}

/** One doc's resolved project-relative path plus its real-time docId, as
 * returned by `listDocPaths` below. */
export interface DocPathEntry {
  path: string;
  docId: string;
}

/**
 * Lists every doc's project-relative path in the tree, depth-first --
 * mirrors Overleaf's own equivalent frontend walk (e.g.
 * use-file-tree-command-source.ts's `folder.docs.map(...)` /
 * `folder.folders` recursion, same field names). Useful for a `write`
 * caller that knows roughly what they're looking for but not the exact
 * path, or a future `meridian-latex ls <project-id>` command.
 *
 * @param rootFolder  already-unwrapped, e.g. from `unwrapRootFolder()`
 */
export function listDocPaths(rootFolder: FileTreeFolder | null): DocPathEntry[] {
  if (!rootFolder) return [];
  const results: DocPathEntry[] = [];
  const walk = (folder: FileTreeFolder, prefix: string): void => {
    for (const doc of folder.docs || []) {
      results.push({ path: prefix ? `${prefix}/${doc.name}` : doc.name, docId: doc._id });
    }
    for (const child of folder.folders || []) {
      walk(child, prefix ? `${prefix}/${child.name}` : child.name);
    }
  };
  walk(rootFolder, "");
  return results;
}
