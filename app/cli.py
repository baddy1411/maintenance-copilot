"""`copilot <command>`. One image, one entrypoint per process type (DESIGN §4).

Only `migrate` exists so far; `api`, `worker` and `mcp` are added by their Phase 2 slices.
"""

import argparse

from app.config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply all database migrations (run before any service update)")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        from app.migrate import migrate

        print(f"database at revision {migrate(Settings())}")


if __name__ == "__main__":
    main()
