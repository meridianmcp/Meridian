# Demo rehearsal: DNABERT paper results to ROC/PR figure

**Rehearsed:** 2026-10-05 (America/Chicago). **Scope:** a read-only inspection of one existing Paper 1 artifact chain. This is a narrow research-project slice, not a live run of the full Meridian workflow.

The Catalyst draft and its criteria review were read as context and were not changed. The slice below supports only the draft’s computational-biology results-to-writing example; it does not demonstrate a complete literature-to-experiment-to-manuscript workflow.

## Starting state

- Research checkout: `C:/Users/13144/Documents/dnabert-error-correction`, branch `master`, HEAD `6678fcef42e43314220b99847b9cb38aa4a5b9e6`. At inspection it had 53 modified or untracked paths. I did not edit it.
- Selected output: the seven-model empirical ROC/precision-recall panel, produced from archived per-base predictions and metric summaries. The existing `paper/data/primary_results.csv` has seven rows; each reports 31,078,378 bases and 212,328 reads.
- Current graph-indexed generator map: `paper/scripts/make_figures.py:131-139` (`PRIMARY`) names these input directories under `reference/`: `results_multipos_v2`, `results_caduceus_v2`, `results_gpn_v2`, `baseline_dilated_cnn_v2`, `baseline_feature_lr_v2`, `results_hyenadna_v2`, and `baseline_majority_v2`. Read-only path checks found both `test_predictions.npz` and `metrics_summary.json` in all seven directories.
- Existing outputs: `paper/data/primary_results.csv`, `paper/figures/roc_curves.pdf`, and `paper/figures/roc_curves.png`. Their SHA-256 hashes at rehearsal are listed below. The PNG was visually inspected and shows ROC and precision-recall panels with seven model curves and reference lines.
- Manuscript pointer: `paper/venues/plos_compbiol/main_plos.tex:29` sets `\graphicspath{{../../figures/}}`; line 245 includes `roc_curves.png`, resolving to the existing `paper/figures/roc_curves.png`.

## Repeatable rehearsal

1. Read `paper/P1_EXECUTION_POINTERS.md`, `paper/MANUSCRIPT_PROVENANCE.md`, `paper/literature/READING_GUIDE.md`, and `paper/README.md` to establish the project’s scientific and manuscript boundaries.
2. Use Codebase Knowledge Graph search for `PRIMARY`, `make_empirical_roc`, and `binned_curves_from_npz` in project `C-Users-13144-Documents-dnabert-error-correction`, then retrieve their snippets. The current implementation maps seven `*_v2` result directories; `make_empirical_roc` reads each pair of prediction/metric files and writes the ROC/PR figure and CSV. `binned_curves_from_npz` streams predictions in chunks of 2,000,000, uses 2,048 display bins, and inverts P(correct)/label coding so errors are the positive class. The code says all archived predictions contribute to the display bins and the curve is not synthesized from AUROC.
3. Confirm the seven input pairs exist. Read `paper/data/primary_results.csv` and verify the seven relative source pointers, row count, and read/base counts. Inspect `paper/figures/roc_curves.png`; record hashes for the CSV, PDF, and PNG.
4. Confirm the PLOS manuscript’s `\\graphicspath` and figure include resolve to the existing figure. Stop before rebuilding or editing if the active manuscript branch or its inputs are unclear.

A regeneration recipe is documented in `paper/README.md`: from a **disposable clean copy** of the repository, run from its `paper` directory:

```powershell
python scripts/make_figures.py --source-root 'C:/Users/13144/Documents/dnabert-error-correction' --caduceus-root 'E:/dnabert_caduceus_results'
```

This command was not run in the rehearsal. The script writes figures/data beneath its own checkout, so the disposable copy keeps the existing dirty research tree intact. The current code loads all seven `*_v2` inputs from `source-root`; `caduceus-root` is used for portable path labeling, not to select the Caduceus input directory.

## Observed result and provenance

- Seven rows in `primary_results.csv` point to `experiment/reference/..._v2/{test_predictions.npz,metrics_summary.json}`; all rows report 31,078,378 bases and 212,328 reads. The `experiment/` prefix is the intentional portable label produced by `portable_source_path` for files relative to `source_root`; `PRIMARY` stores `reference/...` inputs. It is not an additional on-disk directory.
- Output file hashes: `primary_results.csv` — `692485B0E4559D00091DB446941AC76D5F4BC8493469B98D4141A806F0D83433`; `roc_curves.pdf` — `397E8EC251A023BEE8DAEEC6463E0BCA06A2DD3BE12873BE7E00136428FE991A`; `roc_curves.png` — `2FCAE4A6D752C3DA8B7ACEDF69AD5B30ED887314E21D0F5CFC2BD116FCFE30A1`.
- The generator emits a machine-readable source path per model in the CSV. This rehearsal did not hash the input arrays or compare their contents to the summaries, so the paths and counts establish pointers, not cryptographic input provenance or scientific correctness.
- Timing: at 2026-10-05 08:50:49 CDT, checking seven input pairs, CSV row/count fields, and hashing these three outputs took 0.307 seconds. This is only the read-only metadata check; full visual review, graph lookups, and figure regeneration time were not measured.

## Human gate, failure behavior, and limits

- `paper/MANUSCRIPT_PROVENANCE.md` describes the manuscript as an internal, substantially AI-assisted scaffold that is not author-approved. Authors must verify metrics against predictions, confirm split/source provenance, approve claims/captions/figures, and resolve the documented missing logistic-regression interval and other publication blockers before submission.
- The generator’s inspected code raises `FileNotFoundError` when any required prediction or metrics file is absent and `ValueError` when prediction and label lengths differ. These branches were inspected, not triggered in this read-only rehearsal. No automatic fallback or escalation was observed; a human operator must stop and resolve the mismatch.
- The September 4 `paper/P1_EXECUTION_POINTERS.md` is stale for this figure: it lists older result-directory names and an external Caduceus location, while the current generator and CSV point to the seven local `*_v2` directories. The v2 files were present at rehearsal, but the older map should be refreshed before anyone treats it as the canonical pointer list.
- Manuscript guidance also needs a human choice: `paper/README.md` describes an OUP `main.tex` scaffold, while `paper/literature/READING_GUIDE.md` calls the PLOS `main_plos.tex` the only live branch. The figure include was verified in the PLOS file; venue selection and the canonical manuscript branch remain unconfirmed here.
- The codebase graph was available and used. Serena was not available in this session. A Meridian project lookup for `dnabert` returned no match, so this rehearsal is not tied to a retrieved Meridian project/session record. No artifact-graph or registered-output provenance tool was called; the evidence chain here is code pointers plus local files.
- No experiment was rerun, no paper was rebuilt, and no application was edited or submitted. The full runtime, source-array hashes, original split/reference manifest, and identity/equivalence of the local Caduceus v2 files versus `E:/dnabert_caduceus_results` are unknown.
