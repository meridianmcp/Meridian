"""Drafting helpers: writes from Meridian into external documents.

Kept import-light on purpose. Submodules pull in ``meridian.db`` lazily so that
importing this package never creates an import cycle with the server.
"""
