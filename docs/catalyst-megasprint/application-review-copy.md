# Digital Science 2026 Catalyst Grant — review copy

For review only. The original draft remains unchanged. Paste only the nine proposal sections into the form after clearing the human checklist.

## 1. THE PROBLEM

In my computational-biology work, returning after interruption has meant rebuilding what was decided, what ran and which evidence supports the next step. Context sits across chats, reference managers, repositories, compute, outputs and documents, making stale assumptions easy to miss. I have not quantified this frequency or time cost for myself or other researchers; the pilot will measure both rather than invent an estimate.

Meridian is a project-centered continuity layer for people and AI tools working across specialist research systems. Its hypothesis is that a durable, inspectable record of decisions and evidence can make the next research action easier to resume and review. It does not autonomously decide what experiment a scientist should run.

## 2. YOUR WORKFLOW

The proposed pilot starts when a researcher returns to an interrupted project, sets the question and approves a bounded plan. I dogfood Meridian through Codex Desktop’s MCP connection and local tools; use by other researchers is untested. The pilot will configure the assistant to retrieve project goals, decisions, tasks and handoffs, then test for stale or unresolved context. It will search selected scholarly sources; the researcher checks and saves a cited finding. Subject to the connected host and local setup, code intelligence will be tested against a method in the researcher’s repository. The researcher selects a method, runs the analysis in their compute environment and inspects outputs. Local Outputs records run and artifact identities, hashes and manifests. Since project-ID joins are incomplete, the pilot must configure and instrument a plan-to-artifact comparison, retain relevant IDs and paths with the project record, and measure manual linking. The researcher reviews each missing/conflicting-evidence flag and decides whether to revise a method or manuscript claim.

Today, these are separate working components, not a validated automatic end-to-end path. The Catalyst pilot will test their use together in one bounded workflow. Human gates remain at method approval, analysis execution, evidence conflict resolution and manuscript approval. Meridian does not provision the compute or replace the researcher’s scripts, Zotero library or writing environment. Zotero and DOCX integrations are partial; the live Overleaf path is unverified.

## 3. TRUST, AUDIT AND GOVERNANCE

Users can inspect recorded goals, decisions, task status, sessions, handoffs and saved source pointers. New pilot instrumentation—not an existing generic executor-return record—will capture the selected plan, step outcomes and human approvals for evaluator review. Local Outputs retains file identities, hashes, run manifests and source relationships, but joins to project IDs are inconsistent and coverage varies by tool. Meridian should point to specialist systems rather than copy all research material into one service.

The pilot will test an interruption with a missing, stale or conflicting source. The proposed behavior is to mark the gap, show its evidence pointer and request researcher review before treating a conclusion as settled. This safeguard is not yet demonstrated; the researcher remains accountable for method, interpretation and publication.

## 4. TEAM

Meridian is built by Adam Camerer, a researcher-engineer developing the product alongside computational biology and other research workflows. I chose this problem from my own research and software work, where decisions and outputs cross tools and must be reconstructed after interruptions. I am currently the sole founder; I have no formal co-founder or advisory board to report. I use Meridian in my own research and project work, including developing my thesis with its DOCX tools. The grant would support external researcher testing and focused research-software and scholarly-workflow review.

## 5. WHERE YOU ARE TODAY

Meridian is a working product under active development. Existing components include project/session continuity, academic search and explicit finding capture, code intelligence for connected repositories, local output indexing/provenance, document tools and LaTeX tooling. I dogfood it in my own research and software projects; the thesis DOCX workflow is a product-use example, not external validation. My current integration point is Codex Desktop’s MCP connection and local tools; external day-to-day fit is untested. There are no paid customers or external institutional validation yet. Cross-tool evidence joins, dependable Zotero-to-writing flows, live Overleaf use, Tigris-backed remote artifact storage/production backup and multi-project workstation operations remain incomplete or unavailable; current storage falls back locally. These are pilot and development needs, not completed capabilities. Product repository: https://github.com/meridianmcp/Meridian.

One rehearsed demo slice is a read-only trace of existing DNABERT Paper 1 files: an inspected generator maps seven archived v2 prediction/metrics pairs to an existing seven-row results table and figure pointer in a PLOS manuscript scaffold. This verifies file pointers and counts in a separate research repository; it does not demonstrate a Meridian-linked workflow or validate the scientific result. No experiment or figure was rerun, the input arrays were not hashed or compared with metric summaries, and the manuscript still needs author approval. The venue and canonical manuscript branch also need confirmation.

## 6. ALTERNATIVES AND COMPETITORS

Researchers can move context manually among chat histories, papers, Zotero, repositories, compute services, output folders and document editors. Elicit is a partial alternative for literature research and recurring evidence updates: it offers free and paid plans, and Routines are available on Pro, Scale and Enterprise ([plans](https://elicit.com/pricing), [workflow guide](https://support.elicit.com/en/articles/14757543-getting-started-with-elicit-which-workflow-should-i-use), [Routines](https://support.elicit.com/en/articles/17220392-routines-in-elicit)). Sakana AI Scientist v2 is a publicly available agentic machine-learning research system whose repository uses a custom license with use restrictions ([repository and license](https://github.com/SakanaAI/AI-Scientist-v2)). Zotero remains a reference manager; Overleaf is a manuscript environment with optional AI tools, limited free daily use and a paid AI Assist add-on ([Overleaf](https://docs.overleaf.com/integrations-and-add-ons/ai-features/ai-assistant)). Meridian is not claiming that iteration or persistent research context is unique, or that it replaces these products. Its testable distinction is continuity across research decisions, connected code, run/output evidence and writing tools, with incomplete joins made visible.

## 7. WHERE THIS GOES

The long-term goal is a user-controlled project layer that helps researchers carry decisions and evidence across sustained work while leaving specialist tools and raw materials in their chosen systems. The initial buyer hypothesis is independent computational researchers and small research teams, with an individual researcher or team lead paying for hosted use; the pilot should test the segment, payer, hosted value and cost to serve. Pricing and willingness to pay are not validated, so no price is presented as settled. Local/self-hosted use is a distinct route from hosted services.

## 8. FIT WITH DIGITAL SCIENCE

The project sits in research discovery, methods, analysis and writing for independent computational researchers and small research teams. The Catalyst fit is the multi-step coordination problem: a connected assistant carries a bounded task from sourced finding to code discovery and recorded output, while a researcher reviews consequential steps and provenance gaps. Digital Science’s experience across research software and scholarly workflows would help evaluate where this continuity layer fits existing researcher tools, what evidence users need, and which integrations merit investment. The pilot will measure rather than assume that the approach reduces repeated work.

## 9. BUDGET

Working request: £25,000. £12,000 would integrate one bounded evidence-to-output workflow and a compact project-resume path. £6,000 would onboard a pilot cohort and compare each participant’s usual response to an interruption with a matched Meridian-assisted task, recording resume time, repeated actions, traceable evidence entries, planned-versus-observed discrepancies, and seeded missing/stale-source flags followed by human review. £4,000 would fund an independent written review of provenance, privacy and the observed failure test. £3,000 would provide controlled cloud, storage and test infrastructure for the pilot. Before recruitment, the protocol will define cohort, duration, baseline, measurement numerators, denominators and capture methods, and reviewer scope. The grant would not be used to claim institution-wide readiness or production research compute.
