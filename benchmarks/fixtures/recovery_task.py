"""Deterministic chain task with a first-attempt-only external barrier."""

import os
import sys
import time
from pathlib import Path


def main() -> None:
    name, output, control, *inputs = sys.argv[1:]
    control_dir = Path(control)
    attempt = Path.cwd().parent.name
    if name == "b" and attempt == "1" and (control_dir / "hold").exists():
        temporary = control_dir / "ready.tmp"
        temporary.write_text(str(os.getpid()), encoding="ascii")
        os.replace(temporary, control_dir / "ready")
        while not (control_dir / "release").exists():
            time.sleep(0.02)
    value = (
        name + ":" + ",".join(Path(item).read_text(encoding="utf-8") for item in inputs)
    )
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    if name == "b" and attempt == "1":
        (control_dir / "b_exited").write_text("complete", encoding="ascii")


if __name__ == "__main__":
    main()
