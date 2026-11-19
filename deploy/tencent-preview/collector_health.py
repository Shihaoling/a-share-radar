"""Report SXData traffic and collection outcomes from local state without contacting the provider."""

import argparse
from collections import Counter
import json
import os
import subprocess

from pipeline.data_service import request_usage
from relay_common import STATE


def journal_counts(since):
    units = ("ashare-preview-relay.service", "ashare-preview-news@collect.service")
    command = ["journalctl", "--since", since, "--no-pager", "--output=json"]
    for unit in units:
        command.extend(["-u", unit])
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    counts = Counter()
    for line in result.stdout.splitlines():
        envelope = json.loads(line)
        try:
            message = json.loads(envelope.get("MESSAGE", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(message, dict):
            continue
        if message.get("event") == "request_deferred":
            counts[(message.get("api", "unknown"), "local_deferred_" + message["code"])] += 1
        elif message.get("event") == "source_failure":
            if message.get("provider_code") == 429:
                status = "provider_429"
            else:
                status = message.get("code", "unspecified")
            counts[(message.get("api", "unknown"), status)] += 1
        elif "collect" in message and envelope.get("_SYSTEMD_UNIT") == units[1]:
            counts[("news_collection_round", message["collect"])] += 1
    return [{"scope": scope, "status": status, "count": n}
            for (scope, status), n in sorted(counts.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="today", help="Beginning of the journal interval (server time)")
    args = parser.parse_args()
    os.environ.setdefault("RADAR_SERVICE_GATE_FILE", str(STATE / "relay/rate-gate.json"))
    print(json.dumps({"journal_since": args.since, "worker_journal": journal_counts(args.since),
                      "shared_gate": request_usage()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
