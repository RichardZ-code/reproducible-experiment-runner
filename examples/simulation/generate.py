"""Generate reproducible initial states for the integer simulations."""

import argparse
import json
import random
from pathlib import Path


def read_config(path: Path, seed: int) -> tuple[int, int]:
    config = json.loads(
        path.read_text(encoding="utf-8"), parse_constant=reject_constant
    )
    if not isinstance(config, dict) or set(config) != {"seed", "sample_count", "steps"}:
        raise ValueError("base config requires seed, sample_count, and steps")
    if type(config["seed"]) is not int or config["seed"] != seed:
        raise ValueError("base seed must match --seed")
    count, steps = config["sample_count"], config["steps"]
    if type(count) is not int or not 1 <= count <= 100_000:
        raise ValueError("sample_count must be an integer from 1 to 100000")
    if type(steps) is not int or not 1 <= steps <= 100_000:
        raise ValueError("steps must be an integer from 1 to 100000")
    if count * steps > 20_000_000:
        raise ValueError("sample_count * steps exceeds 20000000")
    return count, steps


def reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON value {value}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        count, steps = read_config(args.config, args.seed)
        generator = random.Random(args.seed)
        states = [generator.randrange(10_000) for _ in range(count)]
        result = {
            "seed": args.seed,
            "sample_count": count,
            "steps": steps,
            "initial_states": states,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"generate: {error}\n")


if __name__ == "__main__":
    main()
