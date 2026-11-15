"""The Meridian tool groups the owner can switch off, and the tools in each. Pure Python: no Qt import.

Every tool is on by default. A switched-off tool is stored in the settings file as "group:tool" (see
settings_store.Settings.disabled_tools). Switching off a whole group stores every one of its tools. The local cache
(local_cache.py) skips the tools that are off, and the Settings tab (ui/tools_section.py) lists them.

The group names are the five the local cache writes: code, outputs, docs, latex and debug. chat-files (the filesystem
server) is not listed, because it is needed only by Claude chat, and the local cache never writes it.

The tool names are a snapshot of the local servers, read from their tool lists in this repository's environment. A
server that changes its tools needs this list updated, and a test checks the list's shape, not its content.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

KEY_SEPARATOR = ":"


@dataclass(frozen=True)
class ToolServer:
    """One local MCP server in a group. ``id`` is the stable id the local cache uses for it."""

    id: str
    title: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class ToolGroup:
    """One group on the Settings tab. ``id`` is the stable key stored in settings."""

    id: str
    title: str
    servers: tuple[ToolServer, ...]

    @property
    def tool_keys(self) -> tuple[str, ...]:
        return tuple(tool_key(self.id, tool) for server in self.servers for tool in server.tools)


def tool_key(group: str, tool: str) -> str:
    """The stored key for one tool. The group comes first, so a tool name that two groups share stays two keys."""
    return f"{group}{KEY_SEPARATOR}{tool}"


_CODEBASE_TOOLS = (
    "delete_project", "detect_changes", "get_architecture", "get_code_snippet", "get_graph_schema",
    "index_repository", "index_status", "ingest_traces", "list_projects", "manage_adr", "query_graph",
    "search_code", "search_graph", "trace_path",
)

_SERENA_TOOLS = (
    "delete_memory", "edit_memory", "find_declaration", "find_implementations", "find_referencing_symbols",
    "find_symbol", "get_diagnostics_for_file", "get_symbols_overview", "initial_instructions",
    "insert_after_symbol", "insert_before_symbol", "list_memories", "onboarding", "read_memory",
    "rename_memory", "rename_symbol", "replace_content", "replace_symbol_body", "safe_delete_symbol",
    "write_memory",
)

_OUTPUTS_TOOLS = (
    "annotate_outputs", "bind_artifact_provenance", "bind_artifact_source_edge", "check_staleness",
    "classify_outputs", "evict_derived_cache", "file_fingerprint", "finalize_run_manifest",
    "find_outputs_by_source", "find_stale_by_script", "get_artifact_sources", "get_cached_derived_variant",
    "get_convergence_state", "get_derived_cache_convergence_state", "get_derived_cache_stats",
    "get_evidence_status_and_trusted_pointers", "get_provenance", "get_provenance_status",
    "get_provenance_status_envelope", "get_run_manifest", "get_run_manifest_envelope", "get_source_artifacts",
    "inspect_local_file", "invalidate_derived_variant", "invalidate_stale_derived_variants", "list_provenance",
    "list_registered_artifacts", "list_run_manifests", "npy_metadata", "parse_provenance_envelope",
    "put_cached_derived_variant", "reconcile_legacy_artifact_outputs", "record_provenance", "register_artifact",
    "register_output_paths", "resolve_artifact", "resolve_figure_output", "script_content_hash", "search_logs",
    "search_outputs", "serialize_provenance_envelope", "start_run_manifest", "tag_output",
    "verify_artifact_hash",
)

_DOCS_TOOLS = (
    "apply_and_merge_batch_transform", "apply_batch_transform", "apply_prose_edit_packets",
    "apply_reviewable_edit_transaction", "append_text_run_after_math", "audit_caption_style",
    "audit_cross_document_consistency", "audit_document", "audit_equation_contract", "audit_equation_integrity",
    "audit_equation_style", "audit_heading_style", "audit_manuscript_structure", "audit_reference_consistency",
    "audit_table_style", "build_prose_edit_packet", "check_release_render_gate", "check_render_capability",
    "compare_equation_structures", "copy_section", "delete_user_journal_style_preset", "document_outline",
    "edit_caption", "edit_citation", "edit_equation", "extract_equations", "extract_paragraph_images",
    "find_caption_paragraph", "find_image_paragraph", "find_orphaned_docx_staged_files", "find_references_to",
    "flag_for_review", "format_reference", "get_document_review", "get_equations",
    "get_journal_style_preset", "get_journal_style_preset_provenance", "get_paragraph", "get_section_content",
    "get_structure", "get_structure_elements", "highlight_document", "index_document",
    "index_document_structure", "index_equations", "ingest_local_document", "ingest_local_document_structure",
    "insert_bibliography_entry", "insert_caption", "insert_citation", "insert_column", "insert_cross_reference",
    "insert_equation", "insert_figure_block", "insert_highlighted_note", "insert_image", "insert_media_part",
    "insert_table", "list_internal_notes", "list_journal_style_presets", "list_render_receipts", "locate_anchor",
    "locate_anchors", "merge_docx_draft", "move_section", "parse_document", "plan_batch_transform",
    "read_document_snapshot", "relocate_figure", "relocate_table", "remove_bibliography_entry", "remove_caption",
    "remove_citation", "remove_cross_reference", "remove_equation", "remove_package_part", "remove_table",
    "render_with_receipt", "renumber_sequences", "repair_equation_batch", "retrofit_plaintext_captions",
    "save_user_journal_style_preset", "scan_citation_keys", "scan_stale_notes", "search_document",
    "search_paragraphs", "split_cell", "sync_bibliography", "transpose_table", "update_bibliography_entry",
    "write_section",
)

_LATEX_TOOLS = (
    "check_section_style", "claim_node", "expand_section_aliases", "get_bibliography", "get_live_claims",
    "get_style_guide", "lease_document", "lint_tex", "lint_tex_file", "list_citation_keys",
    "list_local_snapshots", "list_project_docs", "list_provenance", "lookup_citation_key",
    "lookup_published_framing", "mark_provenance_synced", "outline_tex", "outline_tex_file",
    "overleaf_login_status", "pull_doc_expanded", "record_provenance", "release_claim", "snapshot_document",
)

_DEBUG_TOOLS = (
    "attach_to_process", "clear_breakpoints", "close_debug_session", "continue_execution",
    "create_debug_session", "detach_from_process", "evaluate_expression", "expose_session",
    "get_local_variables", "get_output", "get_scopes", "get_source_context", "get_stack_trace", "get_variables",
    "hot_reload", "list_breakpoints", "list_debug_sessions", "list_supported_languages", "list_threads",
    "pause_execution", "remove_breakpoint", "restart_debugging", "set_breakpoint", "start_debugging",
    "step_into", "step_out", "step_over", "unexpose_session", "wait_for_stop",
)

GROUPS: tuple[ToolGroup, ...] = (
    ToolGroup(
        id="code",
        title="Code",
        servers=(
            ToolServer("codebase", "codebase-memory", _CODEBASE_TOOLS),
            ToolServer("serena", "Serena", _SERENA_TOOLS),
        ),
    ),
    ToolGroup(id="outputs", title="Outputs", servers=(ToolServer("outputs", "meridian-outputs", _OUTPUTS_TOOLS),)),
    ToolGroup(id="docs", title="Docs", servers=(ToolServer("docs", "meridian-docs", _DOCS_TOOLS),)),
    ToolGroup(id="latex", title="LaTeX", servers=(ToolServer("latex", "meridian-latex", _LATEX_TOOLS),)),
    ToolGroup(id="debug", title="Debug", servers=(ToolServer("debug", "mcp-debugger", _DEBUG_TOOLS),)),
)

_GROUP_BY_ID = {group.id: group for group in GROUPS}
_SERVER_BY_ID = {server.id: server for group in GROUPS for server in group.servers}
_GROUP_OF_SERVER = {server.id: group.id for group in GROUPS for server in group.servers}


def group_ids() -> tuple[str, ...]:
    return tuple(group.id for group in GROUPS)


def group_of_server(server_id: str) -> str:
    """The group a server belongs to. Raises KeyError for an unknown server id."""
    return _GROUP_OF_SERVER[server_id]


def server_tools(server_id: str) -> tuple[str, ...]:
    """The tool names of one server, as the server itself names them."""
    return _SERVER_BY_ID[server_id].tools


def all_tool_keys() -> tuple[str, ...]:
    return tuple(key for group in GROUPS for key in group.tool_keys)


def group_keys(group_id: str) -> tuple[str, ...]:
    return _GROUP_BY_ID[group_id].tool_keys


def server_disabled_tools(server_id: str, disabled: Iterable[str]) -> tuple[str, ...]:
    """The tool names of one server that are switched off, in the server's own order.

    ``disabled`` is the stored set of "group:tool" keys. An unknown key does nothing.
    """
    off = set(disabled)
    group_id = _GROUP_OF_SERVER[server_id]
    return tuple(tool for tool in _SERVER_BY_ID[server_id].tools if tool_key(group_id, tool) in off)


def normalise(disabled: Iterable[str]) -> tuple[str, ...]:
    """The stored form of a set of keys: unique, stripped, sorted. Keys that name no tool are kept as given."""
    return tuple(sorted({item.strip() for item in disabled if item.strip()}))


__all__ = [
    "GROUPS",
    "KEY_SEPARATOR",
    "ToolGroup",
    "ToolServer",
    "all_tool_keys",
    "group_ids",
    "group_keys",
    "group_of_server",
    "normalise",
    "server_disabled_tools",
    "server_tools",
    "tool_key",
]
