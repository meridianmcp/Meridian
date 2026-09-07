"""f6627d83 — Meridian's provider-neutral research job scheduling package.

See :mod:`meridian.research.scheduler` for the orchestration layer and
:mod:`meridian.research.providers.base` for the JobSpec/JobHandle/JobStatus/
ProviderCapabilities contract every provider (``local``, ``runpod``, and
future adapters) implements.
"""
from __future__ import annotations
