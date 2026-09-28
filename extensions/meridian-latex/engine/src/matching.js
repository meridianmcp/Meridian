// Outline re-matching: given two outline arrays produced by two
// `outlineText()` calls against the same logical document at two points in
// time, figure out which nodes are "the same logical node" across the two
// parses.
//
// Why this needs more than a plain id join: outline.js (v1) already assigns
// each node a content-fingerprinted id (see `fingerprint` there), so most
// unchanged nodes already share the exact same id string across both
// arrays -- that's the easy majority case, a straight Map lookup. This
// module exists for the harder remainder: a node whose id genuinely
// *changed* between the two parses, because either (a) its own content
// changed enough to change the hash (e.g. a heading was retitled), or (b)
// it fell back to the position-derived id (a table/figure/equation with no
// caption and no label -- see outline.js) and drifted purely because
// something shifted upstream, with no content change at all.
//
// For that remainder we do a kind-aware LCS-style sequence alignment: align
// same-kind old/new nodes in document order, tolerating insertions and
// deletions, so a node that merely shifted position (or had a minor content
// edit) is still recognized as "the same node, new content" instead of
// being reported as an unrelated delete+add pair. This is the standard
// "diff" trick (classic LCS alignment, same family as `diff`/`git diff`)
// applied per structural kind instead of per line.

/**
 * Whether an old/new node pair from the *unmatched remainder* of the same
 * `kind` should be considered "the same slot" for alignment purposes. Since
 * everything reaching this function already failed the exact-id join (so a
 * literal equality check would never fire), this uses a coarser, more
 * stable signal than the full content fingerprint:
 *
 *  - heading: same `level` (e.g. both `section`) -- title is allowed to
 *    differ, that's exactly the retitle case this function exists for.
 *  - table/figure: same `env` when both sides have one (so a `table` float
 *    never aligns with a `tabular` layout, even though both share the
 *    `table` `kind` -- see REF_KIND_FOR_ENV in outline.js).
 *  - citation/equation: no finer discriminator is available in this node
 *    shape (a citation's only distinguishing field, `key`, is already what
 *    the id hash is built from -- if it changed, we only have position/
 *    order left to go on). These always align, deferring entirely to the
 *    LCS ordering below to place them sensibly.
 */
function isAlignable(kind, oldNode, newNode) {
  if (kind === "heading") return oldNode.level === newNode.level;
  if (kind === "table" || kind === "figure") {
    if (oldNode.env && newNode.env) return oldNode.env === newNode.env;
    return true;
  }
  return true;
}

/**
 * Classic LCS alignment (same DP as a line-level diff) generalized to an
 * arbitrary `isMatch` predicate instead of strict equality. Returns pairs
 * that align in order, plus what's left over on each side.
 *
 * O(m*n) time and space, per kind group -- fine for a real paper's ~100
 * structural nodes (see README), not something you'd want on a
 * multi-thousand-node document without switching to an O(ND) diff
 * algorithm (Myers). Documented as a known scaling limit, not hidden.
 */
function alignSequences(oldSeq, newSeq, isMatch) {
  const m = oldSeq.length;
  const n = newSeq.length;

  if (m === 0 || n === 0) {
    return { matched: [], removed: oldSeq.slice(), added: newSeq.slice() };
  }

  // dp[i][j] = length of the best alignment between oldSeq[i:] and newSeq[j:]
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      if (isMatch(oldSeq[i], newSeq[j])) {
        dp[i][j] = dp[i + 1][j + 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }

  const matched = [];
  const removedSet = new Set(oldSeq.map((_, i) => i));
  const addedSet = new Set(newSeq.map((_, j) => j));

  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (isMatch(oldSeq[i], newSeq[j]) && dp[i][j] === dp[i + 1][j + 1] + 1) {
      matched.push([oldSeq[i], newSeq[j]]);
      removedSet.delete(i);
      addedSet.delete(j);
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      i++;
    } else {
      j++;
    }
  }

  const removed = [...removedSet].sort((a, b) => a - b).map((idx) => oldSeq[idx]);
  const added = [...addedSet].sort((a, b) => a - b).map((idx) => newSeq[idx]);
  return { matched, removed, added };
}

/** Group nodes by `kind`, preserving document order within each group. */
function groupByKind(nodes) {
  const groups = new Map();
  for (const node of nodes) {
    if (!groups.has(node.kind)) groups.set(node.kind, []);
    groups.get(node.kind).push(node);
  }
  return groups;
}

/**
 * Match two outline arrays (from two `outlineText()` calls on the same
 * logical document at two points in time).
 *
 * Returns `{ matched, added, removed }`:
 *  - `matched`: `{ oldId, newId, node }` for every old node recognized as
 *    the same logical node in the new outline. `node` is the NEW node (the
 *    current state to hand back to a caller). `oldId !== newId` is exactly
 *    the "content changed enough to change the hash, but we still know
 *    it's the same node" case (e.g. a retitled heading).
 *  - `added`: new nodes with no old counterpart (genuinely new content).
 *  - `removed`: old nodes with no new counterpart (genuinely deleted).
 *
 * Two passes:
 *  1. Id join (the easy majority case -- see module docstring above).
 *  2. Kind-aware LCS alignment over whatever's left (the hard case).
 */
export function matchOutlines(oldNodes, newNodes) {
  const newById = new Map(newNodes.map((n) => [n.id, n]));
  const oldById = new Map(oldNodes.map((n) => [n.id, n]));

  const matched = [];
  const oldRemainder = [];
  const newRemainder = [];

  for (const oldNode of oldNodes) {
    const newNode = newById.get(oldNode.id);
    if (newNode) {
      matched.push({ oldId: oldNode.id, newId: newNode.id, node: newNode });
    } else {
      oldRemainder.push(oldNode);
    }
  }
  for (const newNode of newNodes) {
    if (!oldById.has(newNode.id)) {
      newRemainder.push(newNode);
    }
  }

  const added = [];
  const removed = [];

  const oldByKind = groupByKind(oldRemainder);
  const newByKind = groupByKind(newRemainder);
  const kinds = new Set([...oldByKind.keys(), ...newByKind.keys()]);

  for (const kind of kinds) {
    const oldSeq = oldByKind.get(kind) || [];
    const newSeq = newByKind.get(kind) || [];
    const { matched: pairs, removed: rem, added: add } = alignSequences(
      oldSeq,
      newSeq,
      (a, b) => isAlignable(kind, a, b)
    );
    for (const [oldNode, newNode] of pairs) {
      matched.push({ oldId: oldNode.id, newId: newNode.id, node: newNode });
    }
    removed.push(...rem);
    added.push(...add);
  }

  return { matched, added, removed };
}
