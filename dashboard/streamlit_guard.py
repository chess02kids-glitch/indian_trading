"""A single, actionable error for the optional Streamlit dependency.

AUDIT-033
=========

``dashboard/main_dashboard.py``, ``research_dashboard.py``,
``paper_dashboard.py`` and ``broker_dashboard.py`` all need ``streamlit``,
but it was declared in **neither** ``dependencies`` nor any extra, so on a
clean install ``make dashboard`` died with a bare::

    ModuleNotFoundError: No module named 'streamlit'

which gives the operator no idea what to install. It is now an optional
extra (``pip install -e ".[dashboards]"``) because it is a heavy dependency
that the HTTP dashboard — the one the Docker image runs — does not need.

Importing one of these modules must not explode at *import* time; some of
them expose plain data helpers that are exercised without Streamlit
installed. They therefore bind :data:`MISSING_STREAMLIT` and only fail when
a rendering function actually touches it, with the install command in the
message.
"""

from __future__ import annotations

from typing import Any

INSTALL_HINT = (
    'pip install -e ".[dashboards]"   # or: pip install "quant-india[dashboards]"'
)


class MissingStreamlit:
    """Stand-in that raises an actionable error the moment it is used."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(
            f"streamlit is not installed, so this dashboard cannot render "
            f"(accessed streamlit.{name}). Install the dashboard extra:\n"
            f"    {INSTALL_HINT}\n"
            "The HTTP dashboard at `python -m dashboard.server` does not "
            "need it."
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "streamlit is not installed, so this dashboard cannot render. "
            f"Install the dashboard extra:\n    {INSTALL_HINT}"
        )


MISSING_STREAMLIT = MissingStreamlit()


def require_streamlit() -> Any:
    """Return the ``streamlit`` module or raise an actionable ``RuntimeError``."""
    try:
        import streamlit  # noqa: PLC0415 - optional dependency, imported lazily
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "streamlit is not installed, so this dashboard cannot render. "
            f"Install the dashboard extra:\n    {INSTALL_HINT}\n"
            "The HTTP dashboard at `python -m dashboard.server` does not "
            "need it."
        ) from exc
    return streamlit
