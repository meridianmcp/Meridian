// Ambient globals for the dashboard modules (423f5929 / cb7d55ae).
//
// The dashboard's modules expose their top-level symbols on `window` and call
// each other through those runtime globals rather than ES imports (the inline
// onclick / cross-file architecture predates the bundler). After the .js → .ts
// migration, TypeScript needs those cross-module names declared so bare
// references type-check under strict mode.
//
// Two central levers keep the legacy modules strict-clean without thousands of
// per-site casts:
//   1. Index signatures on Window/HTMLElement/Element so `window.foo` and
//      `el.value` / `el.checked` style DOM access type as `any` (the codebase
//      reads/writes element-subtype properties off the base HTMLElement that
//      getElementById/querySelector return).
//   2. `declare const NAME: any` for every cross-module global referenced bare.
// Both are deliberately `any` — precise typing is a long-tail follow-up; the
// goal here is to remove @ts-nocheck so real null/implicit-any bugs surface.

export {};

declare global {
  interface Window { [key: string]: any; }
  interface HTMLElement { [key: string]: any; }
  interface Element { [key: string]: any; }
  interface EventTarget { [key: string]: any; }

  // External UMD libs loaded via <script> tags.
  const Chart: any;
  const echarts: any;
  const marked: any;

  // Cross-module globals (defined in one module, referenced bare in others).
  // NOTE: names concretely defined by an un-@ts-nocheck'd *script* module
  // (e.g. api/projectApi/_staleProjectsHandled in dashboard-core.ts) are NOT
  // declared here — the module's own top-level declaration is the global, and
  // an ambient re-declare would be a TS2451 conflict.
  const state: any;
  const STORAGE_KEY: any;
  const DEFAULT_CONTEXT_THRESHOLD: any;
  const DEFAULT_MAX_TURNS: any;
  const DEFAULT_MAX_PINNED_DECISIONS: any;
  const QUEUE_DONE_PAGE_SIZE: any;
  const SESSION_LIVE_WINDOW_MS: any;
  const _PLAN_LABELS: any;
  const _HUMAN_COLORS: any;
  const _codeGraphData: any;
  const _demoTourDone: any;
  const _demoTourSavedStep: any;

  const escapeHtml: any;
  const formatRelativeTime: any;
  const sessionAgeMs: any;
  const isLiveSession: any;
  const sessionRecencyKey: any;
  const sortSessionsMostRecentFirst: any;
  const getPanelState: any;
  const toast: any;
  const _colorForHuman: any;
  const githubIconSvg: any;
  const displayNotifyTarget: any;
  const suggestNtfyTopic: any;
  const suggestedFsRoots: (execCfg: any, currentRoots: any) => string[];
  const osExecutorHintBanner: any;

  const isDemoMode: any;
  const isHostedMode: any;
  const isHostedAdmin: any;
  const getActiveWorkspaceRole: any;
  const hideDemoAdminControls: any;
  const showDemoOnboardingOverlay: any;
  const showDemoReadonlyToast: any;
  const startDemoTour: any;
  const _checkAccountSwitch: any;

  const closeTab: any;
  const setVtabCountBadge: any;
  const ensureFeedbackButton: any;
  const ensureTourButton: any;
  const ensureSignOutLink: any;
  const ensureWorkspaceSwitcher: any;
  const showFeedbackModal: any;
  const renderConstitutionWarning: any;
  const renderProjectLoadError: any;
  const recordProjectLoadError: any;
  const clearProjectLoadError: any;
  const wireProjectLoadRetry: any;
  const renderSearchResults: any;

  const loadProjectSettings: any;
  const saveProjectSettings: any;
  const loadSettingsTab: any;
  const loadFilesTab: any;
  const loadNotesTab: any;
  const loadRewindTab: any;
  const loadDocumentReview: any;
  const renderDocumentReview: any;
  const wireDocumentReviewButtons: any;
  const loadTimeline: any;
  const loadSprintBoard: any;
  const saveFile: any;
  const mountCodeIntelPanel: any;

  const addSprintItemFromInput: any;
  const wireSprintAddEnter: any;
  const renderSprintProgress: any;
  const renderQueue: any;
  const renderWaveProgress: any;
  const computeWaveProgress: any;
  const buildWaveProgressHtml: any;
  const _renderPlanBadge: any;
  const _sprintHistoryBadges: any;
  const _queueAction: any;

  const renderTimeline: any;
  const _renderToolEntry: any;
  const _groupToolsByCategory: any;
  const _renderToolSections: any;
  const _renderTimelineGantt: any;
  const _renderTimelineHeatmap: any;
  const _renderTimelineLog: any;
  const _heatmapMaxFor: any;
  const _heatmapPieces: any;

  const initRewindTab: any;
  const initRewindCharts: any;
  const renderRewindActivity: any;
  const renderRewindCharts: any;
  const renderRewindGoals: any;
  const renderRewindSprint: any;
  const renderRewindSubtabs: any;
  const renderRewindVersions: any;
  const _rewindSec: any;
  const _rewriteRepoImages: any;

  const _activateSettingsTab: any;
  const _classifySettingsSection: any;
  const _organizeSettingsIntoTabs: any;
  const _showConnSetupIfNeeded: any;

  const _buildCodebaseForceGraph: any;
  const _renderCodebaseGraph: any;
  const _generateCodebaseMap: any;
  const _normalizeGraphEdges: any;
  const _renderAutoAnsweredHitls: any;
}
