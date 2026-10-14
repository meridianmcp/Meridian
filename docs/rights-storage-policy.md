# Image-storage rights policy

This document describes the default policy for storing image bytes in Meridian,
the use modes, and the human-approval override. The code lives in
`meridian/rights_policy.py` (pure decision logic) and
`meridian/rights_override.py` (the override, through `meridian/gate_override.py`).

> This is not legal advice. Legal review is deferred until Meridian has real
> revenue. The policy does not wait on it. The open questions (CC BY-NC use in
> commercialized projects, and IGO and jurisdiction ports) are recorded below
> for review by counsel at that point.

## What is gated

Links and metadata are always kept: paths, captions, source ids and license
labels. Only image **bytes** are gated, and only in commercialized use. A figure
or table index record that stores a file path is a link, so it is allowed under
every use mode.

## Use modes

Each project has a use mode. The policy takes it as the `use_mode` argument of
`decide_image_storage`.

| Use mode | Default | Restricted items (LINK_ONLY, unknown, and derivatives of cc-by-nd) | Gate |
|----------|---------|-----------------------------------------------------------------------|------|
| `standard_publication` | Yes. Academic or standard publication. | Stored, with `requires_credit` and `needs_review` (or `no_derivatives_review`). | Never gates. |
| `commercialized` | No. Opt-in per project. | Refused unless a human approves an override. | Gates restricted items. |

- `None`, or an empty or blank value, means `standard_publication`.
- Any other value raises `RightsPolicyError` with code `USE_MODE_INVALID`. The
  policy fails closed on a bad value.

## Tiers

The tier table is unchanged. What the tier allows depends on the use mode.

| Tier | Licenses (canonical id) | `standard_publication` | `commercialized` |
|------|-------------------------|------------------------|------------------|
| `STORE_IMAGE` | `cc0`, `public-domain`, `cc-by`, `cc-by-sa` | Stored. A derived figure keeps the license. A `cc-by-sa` derivative sets `share_alike`. | Same. No gate. |
| `STORE_UNMODIFIED` | `cc-by-nd` | An unmodified copy is stored. A redrawn or cropped derivative is stored with `no_derivatives_review`. | An unmodified copy is stored. A redrawn or cropped derivative is refused unless a human approves an override. |
| `LINK_ONLY` | `cc-by-nc`, `cc-by-nc-sa`, `cc-by-nc-nd`, `arxiv-nonexclusive`, `all-rights-reserved`, `unknown` | Stored, with `requires_credit` and `needs_review`. | Refused unless a human approves an override. |

`unknown` covers any license string that does not normalize to a known id. In
standard publication it is stored with credit and a review flag. In
commercialized use it is refused. A tier that is not in the table fails closed
to `LINK_ONLY`.

## Flags

Every decision carries a `flags` list. Flags describe the item. They do not
change `allowed`.

| Flag | Set when |
|------|----------|
| `requires_credit` | The license is not `cc0` or `public-domain`. |
| `needs_review` | The tier is `LINK_ONLY`. |
| `share_alike` | A `cc-by-sa` derivative (`derived` or `redrawn`). |
| `no_derivatives_review` | A `cc-by-nd` derivative (`derived` or `redrawn`). |
| `jurisdiction` | The license carries a jurisdiction or IGO port. The port is also in `decision.jurisdiction`. |

## Jurisdiction and IGO ports

A jurisdiction or IGO port of a CC license maps to its base license:

- `CC BY 3.0 IGO` is `cc-by`
- `CC BY 4.0 US` is `cc-by`
- `CC BY-NC 3.0 IGO` is `cc-by-nc`
- URLs such as `https://creativecommons.org/licenses/by/3.0/igo/` map the same way

The port is recorded as a note: `decision.jurisdiction` and the `jurisdiction`
flag. It never changes the tier. Recognised ports are the IGO port, the country
and region codes used in creativecommons.org license URLs, and `england`,
`scotland` and `wales`. A port that is not listed stays unrecognised, so the
license is `unknown`. It is then stored with review in standard publication and
refused in commercialized use. A port is added to the list only once it is
confirmed.

## Normalization

`normalize_license` accepts creativecommons.org license and public-domain URLs
(for example `https://creativecommons.org/licenses/by-nc-nd/4.0/`), and strings
such as `CC BY-NC-ND 4.0`, `cc-by`, `CC0`, `public domain`,
`arXiv non-exclusive` and `All Rights Reserved`. Matching ignores case and extra
spaces. A CC string needs a `cc` marker or the word "attribution", and a BY
clause. A clause the policy does not recognize makes the result `unknown`.

## Derivatives

- `derived` and `redrawn` both mark a derivative of the source image.
- A `cc-by-nd` derivative is flagged `no_derivatives_review` in standard
  publication. In commercialized use it is refused under `STORE_UNMODIFIED`.
- A `LINK_ONLY` derivative is stored with review in standard publication. In
  commercialized use it is refused.
- A `cc-by-sa` derivative sets `share_alike_required` (the `share_alike` flag)
  in every use mode. It is never gated.

## Override: human approval, commercialized use only

The only way past a refused tier is an explicit human approval, filed through
the existing gate-override mechanism. It is used only in commercialized use.

1. `request_rights_override(...)` files a `gate_override` HITL with
   `require_human=True`, gate `rights_storage_override`, and the figure, table
   or source id as the subject. Meridian's auto-answer never answers it.
2. A human answers the request "Yes" in the dashboard. The approval method is the
   existing bearer token plus the approver's name. The answer records the
   approver's name as `answered_by`. OAuth sign-in for approvers is deferred.
3. `authorize_image_storage(..., hitl_id=..., reason=...)` verifies the answer:
   it must be "yes", bound to this gate and subject, not auto-answered, and not
   already used. The approval is then spent. It is a single use.
4. `approved_by` is taken from the answered HITL's own `answered_by` field. A
   caller cannot supply it. Any approver whose name starts with `agent`,
   `session` or `claude`, or equals `auto`, is refused.
5. A non-empty `reason` is required. It is checked before the approval is spent,
   so a missing reason does not burn an approval.
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

## Wiring status

No byte-writing path calls the policy yet. Nothing is gated today, because no
project is declared commercialized.

| Path | Stores | Gated |
|------|--------|-------|
| `index_figure`, `index_table`, `add_figure`, `add_table` (figure and table index) | File path string, caption, semantic label, `file_exists` flag | No. Links only, no bytes. |
| outputs index (`meridian/outputs_indexer.py`) | Path, basename, sha256, size. Images are never read into the index. | No. |
| experiment payload spill (`meridian/tigris_adapter.py`) | Oversized JSON experiment payloads | No. Not images. |
| meridian-docs `insert_image`, `insert_figure_block`, `insert_docx_media_part` (`extensions/meridian-docs/meridian_docs/docs_intel.py`) | Image bytes embedded in a local `.docx` | No. Not wired. These calls take no license argument and do not call the policy. |

## Future hook (not built)

A commercialized project would route image-byte writes through
`authorize_image_storage` before the write. Two gaps stand in the way today:

1. `authorize_image_storage` has no `use_mode` argument. It always decides as
   `standard_publication`, so it never refuses. Adding a `use_mode` argument,
   passed to both `decide_image_storage` calls, is required. That is a change to
   `meridian/rights_override.py`, which is out of scope for this change.
2. The per-project use mode is not stored yet. The policy takes `use_mode` as an
   argument. A caller must supply the project's mode.

The meridian-docs image-embed path runs in a separate extension process with no
access to the Meridian database that holds the approvals. Wiring it means adding
a license argument to those calls, and also a way to reach the approvals. Adding
a license argument makes a missing license an `unknown` license. In standard
publication that stores with review, and in commercialized use it refuses. That
is a product decision, so it is not made here.

## Lawyer review

Legal review is deferred until Meridian has real revenue. The policy does not
block on it, and no code waits for a legal decision. The open questions for
counsel are: whether commercialized projects may use CC BY-NC material, and
whether the IGO and jurisdiction ports are handled as this policy states.
Software licenses of extraction tools (for example AGPL-3.0 or unresolved model
licenses) are a separate dependency decision and are not covered by this policy.
