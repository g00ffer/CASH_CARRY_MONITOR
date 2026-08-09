from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "__version__",
    "build_app",
    "MonitorApp",
    "main",
]


__version__ = "1.0.0"


if TYPE_CHECKING:
    from .app import MonitorApp
    from .bootstrap import build_app
    from .main import main


def __getattr__(name: str) -> Any:
    """
    Lazy imports to avoid loading the whole application immediately
    when the package is imported.
    """

    if name == "build_app":
        from .bootstrap import build_app

        return build_app

    if name == "MonitorApp":
        from .app import MonitorApp

        return MonitorApp

    if name == "main":
        from .main import main

        return main

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
