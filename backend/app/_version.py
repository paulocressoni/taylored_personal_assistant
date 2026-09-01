"""Application version — single source of truth (M12).

Every runtime consumer of the version imports ``__version__`` from here
instead of hardcoding it:
  - FastAPI metadata (``app.main``)
  - the /health endpoint (``app.api.routes``)
  - the frontend footer (fetched from /health at runtime)

Keep this in sync with two places when releasing:
  1. ``backend/pyproject.toml`` -> ``[project].version``
  2. the git tag ``v<version>`` (e.g. v0.2.0)

Why a plain module and not ``importlib.metadata``?
  The Docker image does NOT pip-install the package — it only copies
  ``app/`` and runs uvicorn — so ``importlib.metadata.version("backend")``
  would raise inside the container. A module always works everywhere.
"""

__version__ = "0.2.0"
