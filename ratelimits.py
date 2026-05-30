from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from common.config import get_settings
from common.usage.provider import ProviderUsageSyncService


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch provider rate limits and save them into Agentis providers table.",
    )
    parser.add_argument(
        "providers",
        nargs="*",
        help="Optional provider codes to sync. Defaults to all providers.",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    providers: list[str] | None = args.providers or None
    result = ProviderUsageSyncService(get_settings()).sync(providers)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
