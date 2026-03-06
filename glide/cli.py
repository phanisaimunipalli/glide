import logging
import sys

import uvicorn

from .config import settings
from .proxy import app


def main():
    if not settings.anthropic_api_key:
        print(
            "Note: ANTHROPIC_API_KEY not set — auth will be passed through "
            "from your client. Max plan / OAuth users are supported.",
            file=sys.stderr,
        )

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    uvicorn.run(
        app,
        host=settings.proxy_host,
        port=settings.proxy_port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
