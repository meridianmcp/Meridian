"""Named API keys for the desktop. Pure Python: no Qt import.

A key is one entry in the OS keychain under the desktop's service name, with one account per key name. RunPod keeps
the account the Meridian core already reads (``runpod-api-key``), so the RunPod entry and the core share one key. Any
other name gets the account ``key-<slug>``, where the slug is the name in lower case with each run of other characters
turned into one dash.

The keychain cannot be listed, so the saved custom names are kept in settings.json as ``key_names``. A name is not a
secret. A value is never written there. The critical marks are kept there too, as ``critical_keys``, by listed name.

A key can also be set as a Windows user variable (set_user_environment). The value is read from the keychain, handed to
meridian_desktop.user_environment, and dropped. It is never stored in settings and never returned to the caller.

Only ``read_value`` returns a value, for the owner's Reveal and Copy on one card. ``status`` tests only whether a value is set,
and a failed read reads as unreadable. Errors name the step and the class, never the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from meridian_desktop.secret_store import MAX_SECRET_LENGTH, RUNPOD_ACCOUNT, RUNPOD_SERVICE, SecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.user_environment import UserEnvironment, variable_name_problem

KEY_SERVICE = RUNPOD_SERVICE
RUNPOD_NAME = "RunPod"
BUILT_IN_NAMES: tuple[str, ...] = (RUNPOD_NAME,)
# The group a key starts in, by its name. The owner can change any group, so these are where a key starts, not a rule.
# A name with no default starts in FALLBACK_GROUP. Labels hold text only, never a value.
DEFAULT_GROUP_NAMES: dict[str, str] = {"OpenAI": "AI", "Anthropic": "AI", "Fly.io": "Hosting", "Neon": "Hosting"}
FALLBACK_GROUP = "Other"
# The subgroup a key starts in, by its name. A subgroup holds keys that only work as a set, such as an access key ID with its
# secret. No multi-value service is registered in the desktop app, so none is seeded. A key with no subgroup has none.
DEFAULT_SUBGROUP_NAMES: dict[str, str] = {}
MAX_GROUP_LENGTH = 40
MAX_NAME_LENGTH = 40
MAX_CUSTOM_NAMES = 50
CUSTOM_ACCOUNT_PREFIX = "key-"
# The Windows user variable RunPod is set as. It is the name the RunPod tools already read.
RUNPOD_ENVIRONMENT_NAME = "RUNPOD_API_KEY"

STATUS_SET = "set"
STATUS_NOT_SET = "not_set"
STATUS_UNREADABLE = "unreadable"

_NAME_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]*")
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


class KeyNameError(ValueError):
    """A key name is refused. The text says why, and it never repeats a value."""


@dataclass(frozen=True)
class KeyEntry:
    """One named key as the Keys section lists it: the name, whether it is built in, its status and its critical mark.

    Never its value.
    """

    name: str
    status: str
    built_in: bool
    # True when the owner marked the key critical. Such a key is listed first and shows a badge.
    critical: bool = False


@dataclass(frozen=True)
class EnvironmentSet:
    """What setting a key as a Windows user variable did: the variable's name, and whether Windows confirmed the change."""

    variable: str
    announced: bool


def clean_name(text: object) -> str:
    """Collapse the spaces in a typed name and check it. Raise KeyNameError with plain words when it is refused."""
    if not isinstance(text, str):
        raise KeyNameError("The key name must be text.")
    collapsed = " ".join(text.split())
    if not collapsed:
        raise KeyNameError("Type a name for the key first, for example OpenAI.")
    if len(collapsed) > MAX_NAME_LENGTH:
        raise KeyNameError(f"The key name is longer than {MAX_NAME_LENGTH} characters.")
    if not _NAME_SHAPE.fullmatch(collapsed):
        raise KeyNameError("The key name may use letters, digits, spaces, dots, dashes and underscores.")
    return collapsed


def slug(name: str) -> str:
    """The name in lower case, with each run of other characters as one dash. Two names with one slug are one key."""
    return _NOT_SLUG.sub("-", name.lower()).strip("-")


def account_for(name: str) -> str:
    """The keychain account for a key name. RunPod keeps the account the Meridian core reads."""
    key = slug(clean_name(name))
    if key == slug(RUNPOD_NAME):
        return RUNPOD_ACCOUNT
    return f"{CUSTOM_ACCOUNT_PREFIX}{key}"


def clean_group(text: str) -> str:
    """The group label the owner typed, with its spaces collapsed. Raise KeyNameError for an empty or too long label."""
    cleaned = " ".join(str(text).split())
    if not cleaned:
        raise KeyNameError("Type a group name first.")
    if len(cleaned) > MAX_GROUP_LENGTH:
        raise KeyNameError(f"A group name can be at most {MAX_GROUP_LENGTH} characters.")
    return cleaned


def clean_subgroup(text: str) -> str:
    """The subgroup label the owner typed, with its spaces collapsed. An empty label is "", which clears the subgroup.

    Raise KeyNameError for a label longer than MAX_GROUP_LENGTH characters.
    """
    cleaned = " ".join(str(text).split())
    if len(cleaned) > MAX_GROUP_LENGTH:
        raise KeyNameError(f"A subgroup name can be at most {MAX_GROUP_LENGTH} characters.")
    return cleaned


def environment_name(name: str) -> str:
    """The Windows user variable a key is set as.

    RunPod is RUNPOD_API_KEY. Any other name is its slug in capitals, with each dash as an underscore, so OpenAI becomes
    OPENAI. Raise KeyNameError when Windows cannot take that variable name.
    """
    cleaned = clean_name(name)
    if slug(cleaned) == slug(RUNPOD_NAME):
        return RUNPOD_ENVIRONMENT_NAME
    variable = slug(cleaned).upper().replace("-", "_")
    problem = variable_name_problem(variable)
    if problem is not None:
        raise KeyNameError(problem)
    return variable


def _clean_value(value: object) -> str:
    """Strip the value and check it. The messages hold no part of the value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Type the key value first.")
    cleaned = value.strip()
    if len(cleaned) > MAX_SECRET_LENGTH:
        raise ValueError(f"The key value is longer than {MAX_SECRET_LENGTH} characters.")
    return cleaned


class ApiKeys:
    """The named keys: list them, read whether each one is set, save a value, replace it, or remove it."""

    def __init__(
        self,
        settings: SettingsStore,
        secrets: SecretStore,
        user_environment: UserEnvironment | None = None,
    ) -> None:
        self._settings = settings
        self._secrets = secrets
        # The Windows user variable writer. Tests pass a fake. The default writes HKCU only when it is asked to.
        self._user_environment = user_environment if user_environment is not None else UserEnvironment()

    def names(self) -> list[str]:
        """The built-in names, then the saved custom names in the order they were added."""
        return [*BUILT_IN_NAMES, *self._custom_names()]

    def entries(self) -> list[KeyEntry]:
        """Every listed name with its status and critical mark. Critical names come first, the rest keep their order.

        No value is in the result.
        """
        marked = self._critical_slugs()
        found = [
            KeyEntry(name, self.status(name), name in BUILT_IN_NAMES, slug(name) in marked) for name in self.names()
        ]
        return sorted(found, key=lambda entry: not entry.critical)  # sorted is stable, so each group keeps its order

    def set_critical(self, name: str, critical: bool) -> str:
        """Mark a listed key critical, or clear the mark. Return the name as it is listed.

        Only the listed name is saved in settings, never a value. Raise KeyNameError when the name is not listed, and OSError
        when the settings file cannot be written.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        key = slug(listed)
        # A settings file that did not load cleanly is copied aside first, as the other writers do, so its content survives.
        self._settings.keep_unreadable_copy()
        current = self._settings.load().settings
        kept = [raw for raw in current.critical_keys if slug(raw) != key]
        if critical:
            kept.append(listed)
        self._settings.save(replace(current, critical_keys=tuple(kept)))
        return listed

    def group_of(self, name: str) -> str:
        """The group a listed key is shown under: the owner's label when there is one, else its default, else the fallback."""
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        key = slug(listed)
        for saved_name, label in self._settings.load().settings.key_groups:
            if slug(saved_name) == key:
                return label
        for default_name, default_label in DEFAULT_GROUP_NAMES.items():
            if slug(default_name) == key:
                return default_label
        return FALLBACK_GROUP

    def set_group(self, name: str, label: str) -> str:
        """Save the owner's group label for a listed key, and return it. Only the label is stored, never a value.

        Raise KeyNameError when the name is not listed or the label is empty or too long. Raise OSError when the settings
        file cannot be written.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        cleaned = clean_group(label)
        key = slug(listed)
        current = self._settings.load().settings
        kept = tuple(pair for pair in current.key_groups if slug(pair[0]) != key)
        self._settings.save(replace(current, key_groups=(*kept, (listed, cleaned))))
        return cleaned

    def subgroup_of(self, name: str) -> str:
        """The subgroup a listed key is in, or "" when it has none. The owner's label wins over a default.

        Raise KeyNameError when the name is not listed.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        key = slug(listed)
        for saved_name, label in self._settings.load().settings.key_subgroups:
            if slug(saved_name) == key:
                return label
        for default_name, default_label in DEFAULT_SUBGROUP_NAMES.items():
            if slug(default_name) == key:
                return default_label
        return ""

    def set_subgroup(self, name: str, label: str) -> str:
        """Save the owner's subgroup label for a listed key, and return it. Only the label is stored, never a value.

        An empty label clears the owner's label, so the key falls back to its default subgroup, if one is seeded, or to none.
        Raise KeyNameError when the name is not listed or the label is too long. Raise OSError when the settings file cannot
        be written.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        cleaned = clean_subgroup(label)
        key = slug(listed)
        current = self._settings.load().settings
        kept = tuple(pair for pair in current.key_subgroups if slug(pair[0]) != key)
        if cleaned:
            kept = (*kept, (listed, cleaned))
        self._settings.save(replace(current, key_subgroups=kept))
        return cleaned

    def environment_name(self, name: str) -> str:
        """The Windows user variable this listed key is set as. Raise KeyNameError when it is not listed or cannot be one."""
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        return environment_name(listed)

    def set_user_environment(self, name: str) -> EnvironmentSet:
        """Write the saved value of a listed key to the Windows user environment, under environment_name(name).

        The value is read here, handed to the writer and dropped. It is never returned, never stored and never put in an
        error. Raise KeyNameError when the name is not listed, or when no value is saved for it. Raise SecretStoreError when
        the keychain cannot be read, and UserEnvironmentError when the variable cannot be written.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        variable = environment_name(listed)
        value = self._secrets.get_secret(KEY_SERVICE, account_for(listed))
        if not value:
            raise KeyNameError("No value is saved for that key yet. Save a value first.")
        announced = self._user_environment.set(variable, value)
        return EnvironmentSet(variable, announced)

    def status(self, name: str) -> str:
        """Whether a value is set for this name. The value is read only to test it, and it is dropped here."""
        try:
            value = self._secrets.get_secret(KEY_SERVICE, account_for(name))
        except Exception:  # noqa: BLE001 - a failed read is shown as unreadable, and its text is never used
            return STATUS_UNREADABLE
        return STATUS_SET if value else STATUS_NOT_SET

    def read_value(self, name: str) -> str | None:
        """The saved value of a listed key, or None when no value is set. The only call here that returns a value.

        The owner's Reveal and Copy controls use it, one card at a time. Nothing calls it at startup or to fill a list, and
        it never logs the value. Raise KeyNameError when the name is not listed. Raise SecretStoreError when the keychain
        cannot be read, so a failed read is never reported as an unset key.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        return self._secrets.get_secret(KEY_SERVICE, account_for(listed)) or None

    def save(self, name: str, value: str) -> str:
        """Save a value under a name. A new name is added to the list. Return the name as it is listed.

        An existing name (the same slug) keeps its listing and gets the new value. A new name writes the value first,
        then the list. When the list cannot be written, the value is removed again, so no orphan value is left.
        """
        new_name = clean_name(name)
        cleaned_value = _clean_value(value)
        listed = self._listed(new_name)
        if listed is not None:
            self._secrets.set_secret(KEY_SERVICE, account_for(listed), cleaned_value)
            return listed
        custom = self._custom_names()
        if len(custom) >= MAX_CUSTOM_NAMES:
            raise KeyNameError(f"The list holds at most {MAX_CUSTOM_NAMES} custom names. Remove one first.")
        self._secrets.set_secret(KEY_SERVICE, account_for(new_name), cleaned_value)
        try:
            self._write_custom([*custom, new_name])
        except (OSError, ValueError):
            self._drop_value(new_name)
            raise
        return new_name

    def remove(self, name: str) -> bool:
        """Remove the saved value. A custom name also leaves the list. Return True when a value was removed.

        A built-in name (RunPod) stays listed, so its card is always there. Raise KeyNameError when the name is not listed.
        """
        listed = self._listed(clean_name(name))
        if listed is None:
            raise KeyNameError("That key name is not listed.")
        # The Windows variable goes first. If it cannot be deleted, the keychain value stays, so the owner can try again.
        self._remove_user_variable(listed)
        removed = bool(self._secrets.delete_secret(KEY_SERVICE, account_for(listed)))
        if listed not in BUILT_IN_NAMES:
            # A custom name that leaves the list also loses its critical mark, so no stale mark is left behind.
            gone = slug(listed)
            marks = tuple(raw for raw in self._settings.load().settings.critical_keys if slug(raw) != gone)
            self._write_custom([kept for kept in self._custom_names() if slug(kept) != gone], critical_keys=marks)
            # A removed custom name also loses its group and subgroup labels, so no stale label is left behind.
            current = self._settings.load().settings
            groups = tuple(pair for pair in current.key_groups if slug(pair[0]) != gone)
            subgroups = tuple(pair for pair in current.key_subgroups if slug(pair[0]) != gone)
            if groups != current.key_groups or subgroups != current.key_subgroups:
                self._settings.save(replace(current, key_groups=groups, key_subgroups=subgroups))
        return removed

    def _remove_user_variable(self, listed: str) -> None:
        """Delete the Windows user variable this key was set as, but only when it holds this key's saved value.

        A variable of the same name that holds some other text, or that another program set, is left alone. With no
        saved value there is nothing to compare, so nothing is deleted. Errors come from the registry as
        UserEnvironmentError, and the message names the step and the class only, never the value.
        """
        variable = environment_name(listed)
        saved = self._secrets.get_secret(KEY_SERVICE, account_for(listed))
        if not saved:
            return
        if self._user_environment.read(variable) != saved:
            return
        self._user_environment.delete(variable)

    def _listed(self, name: str) -> str | None:
        """The listed name with the same slug as ``name``, or None when there is none."""
        key = slug(name)
        for listed in self.names():
            if slug(listed) == key:
                return listed
        return None

    def _custom_names(self) -> list[str]:
        """The saved custom names, cleaned. A malformed, built-in or repeated name is left out."""
        seen = {slug(name) for name in BUILT_IN_NAMES}
        names: list[str] = []
        for raw in self._settings.load().settings.key_names:
            try:
                cleaned = clean_name(raw)
            except KeyNameError:
                continue
            if slug(cleaned) in seen:
                continue
            seen.add(slug(cleaned))
            names.append(cleaned)
        return names

    def _critical_slugs(self) -> set[str]:
        """The slugs of the keys marked critical. A mark on a name that is no longer listed matches nothing."""
        return {slug(raw) for raw in self._settings.load().settings.critical_keys}

    def _write_custom(self, names: list[str], *, critical_keys: tuple[str, ...] | None = None) -> None:
        """Save the custom names, and the critical marks when given. Every other field in the settings file is kept.

        Raises OSError when it cannot write.
        """
        current = self._settings.load().settings
        changes: dict[str, object] = {"key_names": tuple(names)}
        if critical_keys is not None:
            changes["critical_keys"] = critical_keys
        self._settings.save(replace(current, **changes))

    def _drop_value(self, name: str) -> None:
        """Remove a value after a failed save. A failure here is not reported, because the save error is the one to show."""
        try:
            self._secrets.delete_secret(KEY_SERVICE, account_for(name))
        except Exception:  # noqa: BLE001 - nothing here may carry a value or a second error into the UI
            return


__all__ = [
    "BUILT_IN_NAMES",
    "KEY_SERVICE",
    "MAX_CUSTOM_NAMES",
    "MAX_NAME_LENGTH",
    "RUNPOD_ENVIRONMENT_NAME",
    "RUNPOD_NAME",
    "STATUS_NOT_SET",
    "STATUS_SET",
    "STATUS_UNREADABLE",
    "ApiKeys",
    "EnvironmentSet",
    "KeyEntry",
    "KeyNameError",
    "account_for",
    "clean_name",
    "environment_name",
    "slug",
]
