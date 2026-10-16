"""`python -m emberdb` entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from .config import load_config
from .engine import EmberEngine
from .http_api import close_all_websockets, serve_http
from .server import serve_resp

BANNER = r"""
   ______           __             ____  ____
  / ____/___ ___  / /_  ___  _____/ __ \/ __ )
 / __/ / __ `__ \/ __ \/ _ \/ ___/ / / / __  |
/ /___/ / / / / / /_/ /  __/ /  / /_/ / /_/ /
/_____/_/ /_/ /_/_.___/\___/_/  /_____/_____/   an independent, from-scratch
                                                  in-memory database server
"""


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="emberdb", description="EmberDB server")
    p.add_argument("--config", help="path to an emberdb.conf file")
    p.add_argument("--dir", help="base directory for default snapshot/AOF/CDC file paths")
    p.add_argument("--port", type=int, help="RESP TCP port (default 7379)")
    p.add_argument("--http-port", type=int, help="HTTP/dashboard port (default 7380)")
    p.add_argument("--bind", help="address to bind (default 0.0.0.0)")
    p.add_argument("--appendonly", choices=["yes", "no"], help="enable the AOF")
    p.add_argument("--requirepass", help="set a password for the default user")
    p.add_argument("--maxmemory", type=int, help="maxmemory in bytes (0 = unlimited)")
    p.add_argument("--maxmemory-policy", dest="maxmemory_policy",
                    choices=["noeviction", "allkeys-lru", "volatile-lru", "allkeys-lfu",
                             "volatile-lfu", "allkeys-random", "volatile-random", "volatile-ttl"])
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def _resolve_config(args: argparse.Namespace) -> dict:
    overrides: dict = {
        "port": args.port,
        "http_port": args.http_port,
        "bind": args.bind,
        "requirepass": args.requirepass,
        "maxmemory": args.maxmemory,
        "maxmemory_policy": args.maxmemory_policy,
    }
    if args.appendonly is not None:
        overrides["appendonly"] = args.appendonly == "yes"

    cfg = load_config(path=args.config, cli_overrides=overrides)

    if args.dir:
        os.makedirs(args.dir, exist_ok=True)
        # Only rewrite paths the user hasn't already pinned explicitly via
        # --config; a bare --dir is meant as "put everything under here".
        for key, default_name in (("snapshot_path", "emberdb.snapshot.json"),
                                   ("aof_path", "emberdb.aof.jsonl"),
                                   ("cdc_path", "emberdb.cdc.jsonl")):
            if cfg[key] in (None, "") or os.path.basename(cfg[key]) == cfg[key]:
                cfg[key] = os.path.join(args.dir, os.path.basename(cfg[key]) or default_name)
    return cfg


async def _auto_save_loop(engine: EmberEngine, interval: float) -> None:
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            n = engine.save_snapshot_now()
            logging.getLogger("emberdb.main").info("auto-save: %d keys written", n)
        except Exception:
            logging.getLogger("emberdb.main").exception("auto-save failed")


async def _run(cfg: dict) -> None:
    engine = EmberEngine(cfg)
    engine.load_from_disk()

    await engine.start_background_tasks()
    resp_server = await serve_resp(engine, cfg["bind"], cfg["port"])
    http_server = await serve_http(engine, cfg["bind"], cfg["http_port"])
    auto_save_task = asyncio.create_task(
        _auto_save_loop(engine, cfg["save_interval_seconds"]), name="auto-save"
    )

    print(BANNER)
    print(f"  RESP server:      redis-cli -p {cfg['port']}")
    print(f"  Dashboard/API:    http://localhost:{cfg['http_port']}/")
    print(f"  Persistence:      snapshot={cfg['snapshot_path']}  aof={'on' if cfg['appendonly'] else 'off'}")
    print(f"  Data dir files:   {cfg['snapshot_path']}, {cfg['aof_path'] if cfg['appendonly'] else '(aof disabled)'}")
    print()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logging.getLogger("emberdb.main").info("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass  # signal handlers aren't available on every platform (e.g. some Windows setups)

    await stop_event.wait()

    print("\nShutting down…")
    auto_save_task.cancel()
    resp_server.close()
    http_server.close()

    # wait_closed() waits for every connection handler task to finish, not
    # just for the listening socket to stop — an open redis-cli session or
    # dashboard tab would hang shutdown forever without this. Force-close
    # everything we're tracking first, then bound the wait as a safety net
    # for anything that doesn't cooperate promptly.
    for conn in list(engine.clients.values()):
        try:
            conn.writer.close()
        except Exception:
            pass
    close_all_websockets()
    try:
        await asyncio.wait_for(resp_server.wait_closed(), timeout=5.0)
    except asyncio.TimeoutError:
        logging.getLogger("emberdb.main").warning("RESP server close timed out, proceeding anyway")
    try:
        await asyncio.wait_for(http_server.wait_closed(), timeout=5.0)
    except asyncio.TimeoutError:
        logging.getLogger("emberdb.main").warning("HTTP server close timed out, proceeding anyway")

    engine.save_snapshot_now()
    engine.shutdown()
    print("Goodbye.")


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = _resolve_config(args)
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
