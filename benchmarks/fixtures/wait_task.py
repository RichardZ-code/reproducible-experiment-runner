"""One deterministic output plus log-only scheduling timestamps."""

import json
import sys
import time
from pathlib import Path


def main() -> None:
    task_id, wait_text, output = sys.argv[1:]
    wait = float(wait_text)
    print(
        json.dumps({"event": "start", "task": task_id, "ns": time.monotonic_ns()}),
        flush=True,
    )
    time.sleep(wait)
    print(
        json.dumps({"event": "end", "task": task_id, "ns": time.monotonic_ns()}),
        flush=True,
    )
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{task_id}:{wait_text}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
