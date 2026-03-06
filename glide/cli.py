import argparse
import json
import logging
import sys

import httpx
import uvicorn

from .config import settings
from .proxy import app


def cmd_start(args):
    if not settings.anthropic_api_key:
        print(
            "Note: ANTHROPIC_API_KEY not set — auth will be passed through "
            "from your client (Max plan / OAuth users are supported).",
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


def cmd_status(args):
    url = f"http://{settings.proxy_host}:{settings.proxy_port}/_glide/status"
    try:
        resp = httpx.get(url, timeout=3.0)
        data = resp.json()
    except httpx.ConnectError:
        print(f"glide is not running on {url}")
        sys.exit(1)

    print(f"\nglide  http://{settings.proxy_host}:{settings.proxy_port}")
    auth = data.get("auth", {})
    print(f"auth   {auth.get('note', '?')}\n")

    cascade = data.get("cascade", [])
    print(f"{'#':<3} {'provider/model':<38} {'TTFT budget':>12} {'TTT budget':>10}  {'p95 TTFT':>9}  {'p95 TTT':>8}")
    print("-" * 90)
    for i, m in enumerate(cascade, 1):
        ttft_b = f"{m['ttft_budget']}s" if m['ttft_budget'] else "no limit"
        ttt_b  = f"{m.get('ttt_budget')}s" if m.get('ttt_budget') else "—"
        lat    = m.get("latency", {})
        ttft_s = lat.get("ttft", {})
        ttt_s  = lat.get("ttt", {})
        p95_ttft = f"{ttft_s['p95']:.2f}s" if ttft_s.get("p95") else "no data"
        p95_ttt  = f"{ttt_s['p95']:.2f}s"  if ttt_s.get("p95")  else "no data"
        label = f"{m['provider']}/{m['model']}"
        print(f"{i:<3} {label:<38} {ttft_b:>12} {ttt_b:>10}  {p95_ttft:>9}  {p95_ttt:>8}")
    print()


def main():
    parser = argparse.ArgumentParser(
        prog="glide",
        description="Latency-aware model cascade proxy for agentic LLM workflows.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("start",  help="Start the glide proxy")
    sub.add_parser("status", help="Show cascade config and live latency stats")

    args = parser.parse_args()

    if args.command == "start" or args.command is None:
        # `glide` with no subcommand also starts (backward compat)
        cmd_start(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
