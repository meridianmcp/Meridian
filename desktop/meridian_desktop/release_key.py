"""The public key that verifies Meridian Desktop release signatures. It is public, so it is safe to commit.

RELEASE_PUBLIC_KEY_B64 is the base64 of the 32-byte Ed25519 public key. The updater refuses any release whose
SHA256SUMS.sig does not verify against it, and the release publisher refuses to sign with a seed that does not
match it. Written from the output of packaging/make_release_key.py.

The private seed lives only outside the repository, at %LOCALAPPDATA%\\meridian-release-keys\\release-ed25519.key on
the release machine. It is never committed, printed or logged. While the value is empty, no release can be signed or
verified, so the check fails closed.
"""

RELEASE_PUBLIC_KEY_B64 = "X6VaLCZJ7PInnYW+CB7xm0G2OUNKDiY336Lp3xpGySU="
