"""Entry point. Phase 1 exposes configuration checking only; no trading logic runs."""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from app import __version__
from app.config import Settings
from app.monitoring.logging_setup import configure_logging, get_logger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cryptoquant-ai")
    parser.add_argument("--check-config", action="store_true", help="validate settings and print a redacted summary")
    args = parser.parse_args(argv)

    try:
        settings = Settings()
    except ValidationError as exc:
        print("Invalid configuration:", file=sys.stderr)
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "settings"
            print(f"  - {loc}: {err['msg']}", file=sys.stderr)  # never echo input values
        return 2
    configure_logging(settings.log_level, settings.log_json)
    log = get_logger("app.main")

    if args.check_config:
        log.info(
            "configuration valid",
            extra={
                "version": __version__,
                "mode": settings.mode.value,
                "symbols": settings.symbols,
                "timeframes": settings.timeframes,
                "real_orders_possible": settings.allows_real_orders,
                "auth_configured": settings.exchange.has_credentials,
                "kill_switch": settings.risk.kill_switch,
            },
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
