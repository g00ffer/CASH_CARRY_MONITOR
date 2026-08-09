from __future__ import annotations

import asyncio
import signal
import sys

from monitor.bootstrap import build_app
from monitor.config import ConfigError


async def _main_async() -> None:
    """
    Async entrypoint.
    """

    app = build_app()

    loop = asyncio.get_running_loop()

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                signal_name,
                app.request_shutdown,
            )
        except NotImplementedError:
            # Windows and some platforms do not support add_signal_handler.
            pass

    try:
        await app.run()
    finally:
        await app.close()


def main() -> None:
    """
    CLI entrypoint.
    """

    try:
        asyncio.run(_main_async())
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
