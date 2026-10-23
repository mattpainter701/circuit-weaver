"""Circuit Weaver CLI entrypoint."""

from __future__ import annotations

import sys
import warnings

warnings.filterwarnings(
    "ignore",
    message="Bad certificate in Windows certificate store",
    category=UserWarning,
    module="ssl",
)

from . import __version__  # noqa: E402
from .dispatcher import main as mvp_main  # noqa: E402


def main() -> None:
    if len(sys.argv) == 1:
        from .workflow_cli import welcome

        raise SystemExit(welcome())
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(__version__)
        return
    mvp_main()


if __name__ == "__main__":
    main()
