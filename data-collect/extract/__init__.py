"""Extraction funnel for the Daily Brief crawl corpus.

See ``EXTRACTION.md`` at the repository root for the full design.

Public entry points:
    * :func:`run` — run the mechanical funnel (stages 0–3) over the corpus.
    * :mod:`extract.jev` — the stage-4 (candidate gate) contract.
"""

from .funnel import FUNNEL_VERSION, run  # noqa: F401

__all__ = ["run", "FUNNEL_VERSION"]