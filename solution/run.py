from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from app.config import AppConfig
from app.main import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the durable campaign dispatcher")
    parser.add_argument(
        "--fixtures",
        default="assignment-b-workflow/fixtures/campaigns.json",
        help="Path to the provided campaigns.json fixture",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "postgresql://au:au_local_only@127.0.0.1:5432/au_assignment"),
        help="PostgreSQL connection URL (defaults to the supplied Docker Compose database)",
    )
    parser.add_argument("--provider", default="http://127.0.0.1:8099", help="Mock telephony provider base URL")
    parser.add_argument("--host", default="127.0.0.1", help="Webhook server bind host")
    parser.add_argument("--port", type=int, default=8200, help="Webhook server port")
    parser.add_argument("--output", default="final_state.csv", help="Final CSV output path")
    return parser.parse_args()


def main() -> None:
    # Load one repository-root .env before resolving DATABASE_URL and the PII key.
    load_dotenv(Path.cwd() / ".env", override=False)
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    root = Path.cwd()
    config = AppConfig.load(root / args.fixtures)
    output_path = (root / args.output).resolve()
    callback_url = f"http://{args.host}:{args.port}/webhooks/telephony"
    app = create_app(
        config=config,
        database_url=args.database_url,
        provider_url=args.provider,
        callback_url=callback_url,
        output_path=output_path,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
