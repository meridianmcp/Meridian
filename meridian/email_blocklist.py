"""Disposable / throwaway email-domain blocklist (925909aa).

A small, static, dependency-free blocklist of the most common disposable-email
providers. Used to gate magic-link signups so throwaway addresses can't be used
to spam the provisioning path. Deliberately a curated frozenset rather than a
live API call: no network dependency, no key, deterministic in tests. It is a
speed-bump, not a guarantee — new disposable domains appear constantly; pair it
with the persistent per-IP signup limit for defence in depth.
"""

from __future__ import annotations

# Curated set of high-volume disposable / temporary mail domains. Lowercase,
# registrable domain form. Keep alphabetical for easy diffing.
DISPOSABLE_EMAIL_DOMAINS: frozenset[str] = frozenset({
    "10minutemail.com",
    "20minutemail.com",
    "33mail.com",
    "temp-mail.io",
    "dispostable.com",
    "emailondeck.com",
    "fakeinbox.com",
    "getairmail.com",
    "getnada.com",
    "guerrillamail.com",
    "guerrillamail.info",
    "guerrillamail.net",
    "guerrillamail.org",
    "harakirimail.com",
    "inboxbear.com",
    "mailcatch.com",
    "maildrop.cc",
    "mailinator.com",
    "mailnesia.com",
    "mintemail.com",
    "moakt.com",
    "mohmal.com",
    "mytemp.email",
    "sharklasers.com",
    "spam4.me",
    "spamgourmet.com",
    "tempmail.com",
    "tempmail.net",
    "tempmailo.com",
    "temp-mail.org",
    "throwawaymail.com",
    "trashmail.com",
    "trashmail.de",
    "yopmail.com",
    "yopmail.fr",
    "yopmail.net",
})


def email_domain(email: str) -> str:
    """Return the lowercased domain part of an email, or "" if malformed.

    Handles surrounding whitespace and multiple ``@`` defensively (takes the
    part after the last ``@``, matching how mailers resolve the domain)."""
    if not isinstance(email, str):
        return ""
    at = email.strip().lower().rpartition("@")
    return at[2] if at[1] else ""


def is_disposable_domain(domain: str) -> bool:
    """True if ``domain`` (or any parent registrable domain of it) is a known
    disposable provider. Matches subdomains too — ``x.mailinator.com`` counts."""
    d = (domain or "").strip().lower().rstrip(".")
    if not d:
        return False
    if d in DISPOSABLE_EMAIL_DOMAINS:
        return True
    # Subdomain of a blocked domain (e.g. inbox.mailinator.com).
    parts = d.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in DISPOSABLE_EMAIL_DOMAINS:
            return True
    return False


def is_disposable_email(email: str) -> bool:
    """True if ``email``'s domain is a known disposable/throwaway provider."""
    return is_disposable_domain(email_domain(email))
