# Image-storage rights policy

This document describes the default policy for storing image bytes in Meridian,
and the human-approval override. The code lives in `meridian/rights_policy.py`
(pure decision logic) and `meridian/rights_override.py` (the override, through
`meridian/gate_override.py`).

> This is not legal advice. The CC BY-NC questions and the AGPL questions need
> review by qualified counsel before anyone relies on this policy for a
> publication or a licensing decision.

## What is gated

Links and metadata are always kept: paths, captions, source ids and license
labels. Only image **bytes** are gated. A figure or table index record that
stores a file path is a link, so it is allowed under every tier.

## Tiers

| Tier | Licenses (canonical id) | Image bytes |
|------|-------------------------|-------------|
| `STORE_IMAGE` | `cc0`, `public-domain`, `cc-by`, `cc-by-sa` | May be stored. A derived figure keeps the license. A `cc-by-sa` derivative must carry the share-alike flag. |
| `STORE_UNMODIFIED` | `cc-by-nd` | Only an unmodified copy. A redrawn or cropped derivative is refused. |
| `LINK_ONLY` | `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nc-nd`, `arxiv-nonexclusive`, `all-rights-reserved`, `unknown` | Refused. Metadata and links only. |

`unknown` covers any license string that does not normalize to a known id. It
is treated as link-only, never as storable. A tier that is not in this table
also fails closed to `LINK_ONLY`.

## Normalization

`normalize_license` accepts creativecommons.org license and public-domain URLs
(for example `https://creativecommons.org/licenses/by-nc-nd/4.0/`), and strings
such as `CC BY-NC-ND 4.0`, `cc-by`, `CC0`, `public domain`,
`arXiv non-exclusive` and `All Rights Reserved`. Matching ignores case and
extra spaces. A CC string needs a `cc` marker or the word "attribution", and a
BY clause. A clause the policy does not recognize, such as a jurisdiction port
like `IGO`, makes the result `unknown`.

## Derivatives

- `derived` and `redrawn` both mark a derivative of the source image.
- A `cc-by-nd` derivative is refused under the `STORE_UNMODIFIED` tier.
- A `LINK_ONLY` source is refused for derivatives as well.
- A `cc-by-sa` derivative sets `share_alike_required` in the decision.

## Override: human approval only

The only way past a refused tier is an explicit human approval, filed through
the existing gate-override mechanism:

1. `request_rights_override(...)` files a `gate_override` HITL with
   `require_human=True`, gate `rights_storage_override`, and the figure, table
   or source id as the subject. Meridian's auto-answer never answers it.
2. A human answers the request "Yes" in the dashboard.
3. `authorize_image_storage(..., hitl_id=..., reason=...)` verifies the answer:
   it must be "yes", bound to this gate and subject, not auto-answered, and not
   already used. The approval is then spent. It is a single use.
4. `approved_by` is taken from the answered HITL's own `answered_by` field. A
   caller cannot supply it. Any approver whose name starts with `agent`,
   `session` or `claude`, or equals `auto`, is refused.
5. A non-empty `reason` is required. It is checked before the approval is
   spent, so a missing reason does not burn an approval.
6. The override is audited in `action_audit_log` with event type
   `rights_storage_override`, recording the subject, the approver, the reason,
   the HITL id, the tier and the license. If the audit write fails, the call
   raises and the override does not proceed.

An agent or session can file the request and show its `hitl_id`. It cannot
approve the request.

Known limit (inherited from `gate_override`): the approval is human-bound
against auto-answer. It is not bound against a holder of the same bearer token,
who can still answer the request through the answer endpoint. Closing that gap
needs a separate authentication channel for the dashboard. It is outside this
policy.

## Where the policy applies today

| Path | Stores | Gated |
|------|--------|-------|
| `index_figure`, `index_table`, `add_figure`, `add_table` (figure and table index) | File path string, caption, semantic label, `file_exists` flag | No. Links only, no bytes. |
| outputs index (`meridian/outputs_indexer.py`) | Path, basename, sha256, size. Images are never read into the index. | No. |
| experiment payload spill (`meridian/tigris_adapter.py`) | Oversized JSON experiment payloads | No. Not images. |
| meridian-docs `insert_image`, `insert_figure_block`, `insert_docx_media_part` (`extensions/meridian-docs/meridian_docs/docs_intel.py`) | Image bytes embedded in a local `.docx` | **Not yet.** See the open decision below. |

Any future code path that writes image bytes must call
`authorize_image_storage` before the write.

## Open decision

The meridian-docs image-embed path writes image bytes into a `.docx`. It runs in
a separate extension process with no access to the Meridian database that holds
the approvals. Wiring it through the policy means adding a license argument.
Without a license it would be refused by default, so existing callers would
break until they declare one. That is a product decision, so it is not made
here.
