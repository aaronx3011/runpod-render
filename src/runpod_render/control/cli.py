"""Control plane CLI.

    python -m runpod_render.control migrate
    python -m runpod_render.control create-project NAME [--max-active 1]   # prints the API key once
    python -m runpod_render.control api [--host 127.0.0.1] [--port 8080]
    python -m runpod_render.control scheduler
    python -m runpod_render.control ensure-bucket                          # local S3 only
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .settings import Settings


class _RedactWebhookSecret(logging.Filter):
    """Keep the webhook path secret out of access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            path = record.args[2]
            if path.startswith("/runpod/webhook/"):
                rest = path.removeprefix("/runpod/webhook/")
                tail = rest[rest.find("?"):] if "?" in rest else ""
                record.args = (*record.args[:2], f"/runpod/webhook/<redacted>{tail}", *record.args[3:])
        return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="runpod_render.control")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations (alembic upgrade head)")
    cp = sub.add_parser("create-project", help="create a project and print its API key once")
    cp.add_argument("name")
    cp.add_argument("--max-active", type=int, default=1)
    ap = sub.add_parser("api", help="run the HTTP API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    sub.add_parser("scheduler", help="run the scheduler + reconciler (single active instance)")
    sub.add_parser("ensure-bucket", help="create the S3 bucket if missing (local stacks only)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()

    if args.cmd == "migrate":
        from .migrate import upgrade
        upgrade(settings.database_url)
        print("migrated to head")
    elif args.cmd == "create-project":
        from .db import make_engine, make_sessionmaker
        from .service import create_project
        engine = make_engine(settings.database_url)
        with make_sessionmaker(engine).begin() as session:
            project, key = create_project(session, args.name, max_active=args.max_active)
        print(f"project_id={project.id}")
        print(f"api_key={key}")
        print("Store the key now; only its hash is kept.", file=sys.stderr)
    elif args.cmd == "api":
        import uvicorn

        from .api import create_app
        from .context import Context
        logging.getLogger("uvicorn.access").addFilter(_RedactWebhookSecret())
        uvicorn.run(create_app(Context.build(settings)), host=args.host, port=args.port, log_config=None)
    elif args.cmd == "scheduler":
        from .context import Context
        from .scheduler import Scheduler
        sched = Scheduler(Context.build(settings))
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: sched.stop.set())
        sched.run_forever()
    elif args.cmd == "ensure-bucket":
        from .storage import Storage
        Storage(settings).ensure_bucket()
        print(f"bucket {settings.s3_bucket} ready")
    return 0
