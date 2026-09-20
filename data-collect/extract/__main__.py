"""CLI: ``python -m extract`` runs the mechanical funnel (stages 0-3)."""

from .funnel import run

if __name__ == "__main__":
    raise SystemExit(run())