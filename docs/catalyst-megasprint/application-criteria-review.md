# Catalyst 2026 application review

Reviewed 2026-10-05 against the [official call](https://www.digital-science.com/about-us/investment/catalyst-grant/) and the current application draft at `C:\Users\13144\Documents\dnabert-error-correction\CATALYST_GRANT_APPLICATION_DRAFT_2026-10-04.md`.

This is the initial criteria review of the source draft. The integrated review copy and the applicant’s remaining checks are summarized under “Follow-up status” below.

## Status

- The deadline is Monday 5 October 2026, 17:00 BST (16:00 UTC). It is today.
- The draft has all nine required headings.
- The source draft had 1,388 regex-counted words including headings and Markdown/link tokens. The final integrated review copy is counted in “Follow-up status” below.
- The budget adds correctly to £25,000: £12,000 engineering/integration, £6,000 pilot and measurement, £4,000 independent review, £3,000 infrastructure.
- No draft edit or submission was made in this review.

## Fit and central argument

The strongest fit is the researcher's need to carry one project through changing questions, sources, methods, code/compute, outputs, and writing while retaining decisions, ownership, and evidence. This is a multi-step workflow with clear human review points, and the thesis DOCX workflow is a real product-use proof point. The draft is appropriately candid that the integrated graph, Zotero/PDF path, evidence tables, Overleaf connection, remote backup, external validation, and broad compute provisioning are incomplete or unverified.

Position Meridian as a **project continuity and operations layer that coordinates specialist research tools**, with computational research as the lead demonstration. Do not claim that iterative research, persistent projects, or literature synthesis alone are unique.

## Competitor and boundary review

- **Elicit:** its current Research Agent supports iterative paper finding, extraction, study investigation, and evidence synthesis; Routines run recurring research work on higher-tier plans. Avoid saying Meridian is the only multi-step or recurring research system. Elicit covers literature research and recurring evidence updates; Meridian's proposed distinction is coordination across research, code/compute, outputs, local tools, and manuscript environments. Cross-tool integration remains only partially joined and must be demonstrated narrowly. Sources: [Elicit workflow guide](https://support.elicit.com/en/articles/14757543-getting-started-with-elicit-which-workflow-should-i-use), [Elicit Routines](https://support.elicit.com/en/articles/17220392-routines-in-elicit).
- **Sakana AI Scientist v2:** its public repository describes an agentic tree-search system that generates hypotheses, runs experiments, analyzes results, and writes manuscripts, focused on ML research. It is not accurately described as one-shot or “open source”: its repository has a custom license with use restrictions. Meridian should be distinguished by broader project/tool continuity and human-governed work, not by claiming that it alone iterates. Source: [AI-Scientist-v2 repository and license](https://github.com/SakanaAI/AI-Scientist-v2).
- **Overleaf:** this is the LaTeX manuscript environment and has its own AI assistant, citation reviewer, and AI table/equation tools. Meridian should complement the authoring environment by carrying project context and evidence to it; do not claim to replace its writing features or say Meridian's live Overleaf path works until tested. Sources: [Overleaf AI Assistant](https://docs.overleaf.com/integrations-and-add-ons/ai-features/ai-assistant), [Overleaf AI features](https://docs.overleaf.com/integrations-and-add-ons/ai-features).
- **Zotero:** it remains the user's reference-manager system of record. Meridian has citation-related tooling, but the private-library metadata/PDF path and reliable writing integration need end-to-end validation. Do not imply Meridian replaces Zotero.
- **Doing nothing/current stack:** researchers can manually move context among chat histories, papers, Zotero, repositories, compute services, output folders, and Word/Overleaf. The draft's reconstruction/repeated-work problem is plausible, but does not yet quantify its frequency or cost.

## Initial recommendations before integration

1. **Answer frequency and cost.** The problem section describes repeated reconstruction, stale assumptions, and risk, but gives no frequency or estimated time/money cost. State this as personal experience if accurate, and clearly label it unmeasured; do not invent a market-wide statistic. The grant pilot can measure baseline versus Meridian resume time, duplicated actions, and missed/stale evidence.
2. **Make the workflow concrete.** Name one trigger, one researcher, the exact working environment/tool(s), the steps the agent can actually perform today, the resulting artifact/decision, and where the researcher reviews, approves, overrides, or stops it. Separate the verified DOCX thesis slice from the broader intended research-to-code-to-output-to-manuscript path.
3. **Specify failure behavior and accountability.** Identify who is accountable (the researcher), what is recorded, and what the current system demonstrably does when a source is missing, a pointer is stale, a task is blocked, or evidence conflicts. The interruption/refusal test is currently proposed, not a completed result; label it accordingly.
4. **Answer market and pricing.** The call scores market and asks who would pay/pricing thinking if commercial. If still the intended direction, describe the user's current ~$20/month standard-plan concept as an early pricing hypothesis, say the free local/self-hosted path is distinct, and avoid promising hosted compute/storage/search costs that have not been modeled. Add the initial buyer/user (independent computational researchers or small research teams) without inventing market size.
5. **Strengthen progress proof.** Link the repository and one reproducible demo if available. The DOCX thesis use is meaningful dogfooding, not external customer or institutional validation. Do not claim new-member onboarding is reliable until the fresh hosted project-create/readback path is verified. Keep the Overleaf path marked unverified.
6. **Make the budget buy an evaluable pilot.** The arithmetic is correct; connect each line to a deliverable and identify what the £4,000 independent review will review and what sample/pilot work the £6,000 supports. Keep the grant ask at or below £25,000.

## Suggested safe language

For the problem: “In my own long-running research projects, restarting after a context or tool change repeatedly means reconstructing what was decided, what ran, and which evidence supports the next step. We have not yet measured the time cost across researchers; the funded pilot will compare resume time and repeated actions against a documented baseline.” Use only if the personal frequency claim is accurate.

For differentiation: “Elicit and AI Scientist v2 already demonstrate multi-step research in their respective domains. Meridian's hypothesis is that researchers also need a durable project record that connects those specialist workflows to code, compute, outputs, documents, and human decisions. We will test that hypothesis in one bounded pilot; several integrations remain incomplete.”

For price: “Our current individual hosted-plan concept is around $20/month, alongside a local/self-hosted route; the pilot will test which hosted coordination and research services users value enough to pay for and what their cost-to-serve is.” Confirm the amount and product-plan distinction before including it.

## Initial decision

The source draft was on-theme and appropriately honest, but needed a targeted pass on personal problem framing, workflow specificity, uncertainty/accountability behavior, pricing/payer, and demo evidence. Confirm every personal fact and final form field manually. The human applicant must review and submit; this review does not authorize submission.

## Follow-up status for the integrated review copy

- The nine proposal headings are present in official order. The current review copy has 1,330 words by a conservative regex count, including headings and Markdown/link tokens; its whitespace count is 1,233. Confirm the live form’s counter after pasting.
- The copy labels frequency/time cost as unquantified, names Codex Desktop as the current dogfooding host, frames cross-tool workflow/configuration and approval logging as pilot work, and keeps the missing/stale-evidence safeguard explicitly untested. Adam must confirm the personal facts and host description.
- Competitor wording now identifies Elicit’s paid-tier Routines, Sakana’s custom license restrictions, and Overleaf’s optional AI tools. Competitor company size/scale remains unasserted.
- The £25,000 budget arithmetic is correct. The checklist assigns pilot task scope, cohort and protocol, measurement definitions, participant data handling, independent review scope, and infrastructure cost assumptions to be set before recruitment.
- See the [application review copy](application-review-copy.md) and [human checklist](application-human-checklist.md). They are not a submitted application; Adam must make the accuracy decision and submit.
