"""Local Zotero connection setup for the workstation tray companion.

The local Zotero HTTP API is read-only for this setup screen: it is used to
test the desktop connection and enumerate collection names. Optional web API
credentials stay in the native OS vault and are never sent to Meridian.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import webbrowser
from typing import Any

from .tunnel_config import (
    ZoteroCredentialStoreUnavailable,
    delete_zotero_api_key,
    get_zotero_api_key,
    get_zotero_collection_keys,
    get_zotero_library_id,
    set_zotero_api_key,
    set_zotero_preferences,
)

_DEFAULT_LOCAL_API_BASE = "http://127.0.0.1:23119/api"
_LOCAL_API_TIMEOUT_SECONDS = 3.0


class ZoteroSetupError(RuntimeError):
    """A user-actionable local Zotero setup or connection error."""


def list_local_zotero_collections(
    *, base_url: str | None = None, timeout: float = _LOCAL_API_TIMEOUT_SECONDS
) -> list[dict[str, str]]:
    """Read collection keys/names from the running local Zotero API.

    No account key is sent: Zotero's local read API serves the signed-in local
    user's library and is intentionally kept on loopback.
    """
    base = (base_url or os.environ.get("MERIDIAN_ZOTERO_API_URL") or _DEFAULT_LOCAL_API_BASE)
    url = f"{base.strip().rstrip('/')}/users/0/collections?format=json"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Meridian-Zotero-Setup/1",
            "Zotero-API-Version": "3",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise ZoteroSetupError(f"Zotero local API returned HTTP {response.status}.")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            raise ZoteroSetupError(
                "Zotero denied local API access. In Zotero Settings > Advanced, "
                "enable ‘Allow other applications on this computer to communicate with Zotero’."
            ) from exc
        raise ZoteroSetupError(f"Zotero local API returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ZoteroSetupError(
            "Could not reach Zotero on this computer. Start Zotero and enable its local API, then refresh."
        ) from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ZoteroSetupError("Zotero returned an unreadable collection list.") from exc

    if not isinstance(payload, list):
        raise ZoteroSetupError("Zotero returned an unexpected collection list.")
    collections: list[dict[str, str]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        data: Any = row.get("data")
        data = data if isinstance(data, dict) else {}
        key = row.get("key")
        name = data.get("name")
        if isinstance(key, str) and key.strip() and isinstance(name, str) and name.strip():
            collections.append({"key": key.strip().upper(), "name": name.strip()})
    return sorted(collections, key=lambda row: row["name"].casefold())


def run_zotero_setup_dialog(parent=None) -> bool:
    """Show an optional local-only connection editor; return True if saved."""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:  # noqa: BLE001 — tray and installer report clearly
        raise ZoteroSetupError("This installation does not include the Tk GUI runtime.") from exc

    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    root.title("Meridian — Zotero connection")
    root.geometry("640x620")
    root.minsize(560, 520)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(5, weight=1)
    saved = {"value": False}
    collections_loaded = {"value": False}
    saved_collections = get_zotero_collection_keys()

    heading = ttk.Label(root, text="Zotero connection", font=("Segoe UI", 15, "bold"))
    heading.grid(row=0, column=0, sticky="w", padx=18, pady=(18, 4))
    ttk.Label(
        root,
        text=(
            "This setup talks to Zotero on this computer. Citation lookup and PDF tools use "
            "the local Zotero connection; PDFs remain in Zotero’s local storage. "
            "Meridian does not receive your Zotero API key."
        ),
        wraplength=590,
        justify="left",
    ).grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 14))

    form = ttk.Frame(root)
    form.grid(row=2, column=0, sticky="ew", padx=18)
    form.columnconfigure(1, weight=1)
    ttk.Label(form, text="Zotero user ID (optional)").grid(row=0, column=0, sticky="w", pady=4)
    library_id = tk.StringVar(value=get_zotero_library_id() or "")
    ttk.Entry(form, textvariable=library_id, width=32).grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=4)
    ttk.Label(
        form,
        text="Only needed with a web API key; find it on Zotero’s API Keys page.",
        wraplength=430,
    ).grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(0, 8))
    ttk.Label(form, text="Web API key (optional)").grid(row=2, column=0, sticky="w", pady=4)
    api_key = tk.StringVar()
    key_entry = ttk.Entry(form, textvariable=api_key, show="•")
    key_entry.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=4)
    key_already_saved = get_zotero_api_key() is not None
    key_status = ttk.Label(
        form,
        text=("A key is stored in the OS credential vault; leave this blank to keep it." if key_already_saved
              else "Local Zotero reads do not require a web API key."),
        wraplength=430,
    )
    key_status.grid(row=3, column=1, sticky="w", padx=(12, 0), pady=(0, 8))

    ttk.Button(
        form,
        text="Open Zotero API Keys",
        command=lambda: webbrowser.open("https://www.zotero.org/settings/keys"),
    ).grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(0, 8))

    ttk.Label(root, text="Collections for Meridian citation lookup (Ctrl/Shift-click to select)").grid(
        row=3, column=0, sticky="w", padx=18, pady=(0, 4)
    )
    list_frame = ttk.Frame(root)
    list_frame.grid(row=4, column=0, sticky="nsew", padx=18)
    root.rowconfigure(4, weight=1)
    collection_list = tk.Listbox(list_frame, selectmode="extended", exportselection=False)
    collection_list.pack(side="left", fill="both", expand=True)
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=collection_list.yview)
    scrollbar.pack(side="right", fill="y")
    collection_list.configure(yscrollcommand=scrollbar.set)

    status = tk.StringVar(value="Refresh to check the local Zotero connection.")
    ttk.Label(root, textvariable=status, wraplength=590).grid(
        row=5, column=0, sticky="ew", padx=18, pady=(8, 0)
    )

    collection_rows: list[dict[str, str]] = []

    def refresh_collections() -> None:
        nonlocal collection_rows
        try:
            collection_rows = list_local_zotero_collections()
        except ZoteroSetupError as exc:
            collections_loaded["value"] = False
            status.set(str(exc))
            return
        collections_loaded["value"] = True
        collection_list.delete(0, tk.END)
        for index, row in enumerate(collection_rows):
            collection_list.insert(tk.END, f"{row['name']}  ({row['key']})")
            if row["key"] in saved_collections:
                collection_list.selection_set(index)
        if collection_rows:
            status.set(
                f"Connected to local Zotero. {len(collection_rows)} collections found. "
                "No selection means whole-library citation lookup."
            )
        else:
            status.set("Connected to local Zotero. No collections were found; whole-library scope is available.")

    def remove_key() -> None:
        try:
            removed = delete_zotero_api_key()
        except ZoteroCredentialStoreUnavailable as exc:
            messagebox.showerror("Zotero credential", str(exc), parent=root)
            return
        key_already_saved_local = get_zotero_api_key() is not None
        key_status.configure(text="No web API key is stored." if removed or not key_already_saved_local
                             else "The stored key was not changed.")
        status.set("Stored web API key removed from the local OS credential vault." if removed
                   else "No stored web API key was present.")

    def save() -> None:
        key = api_key.get().strip()
        user_id = library_id.get().strip()
        if key and not user_id.isdigit():
            messagebox.showerror(
                "Zotero user ID required",
                "Enter the numeric Zotero user ID when saving a web API key.",
                parent=root,
            )
            return
        if collections_loaded["value"]:
            selected = [collection_rows[index]["key"] for index in collection_list.curselection()]
        else:
            selected = saved_collections
        try:
            set_zotero_preferences(library_id=user_id or None, collection_keys=selected)
            if key:
                set_zotero_api_key(key)
        except (ValueError, ZoteroCredentialStoreUnavailable) as exc:
            messagebox.showerror("Could not save Zotero settings", str(exc), parent=root)
            return
        saved["value"] = True
        status.set("Saved locally. Restart the local tunnel to apply credential changes to a running Zotero slot.")
        messagebox.showinfo("Zotero settings saved", status.get(), parent=root)
        root.destroy()

    actions = ttk.Frame(root)
    actions.grid(row=6, column=0, sticky="ew", padx=18, pady=16)
    ttk.Button(actions, text="Refresh collections", command=refresh_collections).pack(side="left")
    ttk.Button(actions, text="Remove saved key", command=remove_key).pack(side="left", padx=(8, 0))
    ttk.Button(actions, text="Save", command=save).pack(side="right")
    ttk.Button(actions, text="Cancel", command=root.destroy).pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    if parent is None:
        root.mainloop()
    else:
        root.transient(parent)
        root.grab_set()
        parent.wait_window(root)
    return saved["value"]
