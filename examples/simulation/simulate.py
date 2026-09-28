"""Apply a branch-specific integer recurrence to generated states."""

import argparse
import json
from pathlib import Path


def reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON value {value}")


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)


def simulate(source: object, config: object) -> dict[str, object]:
    if not isinstance(source, dict) or set(source) != {
        "seed",
        "sample_count",
        "steps",
        "initial_states",
    }:
        raise ValueError("input requires seed, sample_count, steps, and initial_states")
    if type(source["seed"]) is not int:
        raise ValueError("input seed must be an integer")
    count, steps, states = (
        source["sample_count"],
        source["steps"],
        source["initial_states"],
    )
    if (
        type(count) is not int
        or not 1 <= count <= 100_000
        or type(steps) is not int
        or not 1 <= steps <= 100_000
        or count * steps > 20_000_000
    ):
        raise ValueError("input size is invalid")
    if (
        not isinstance(states, list)
        or len(states) != count
        or any(type(x) is not int or not 0 <= x < 10_000 for x in states)
    ):
        raise ValueError(
            "initial_states must contain sample_count integers from 0 to 9999"
        )
    if not isinstance(config, dict) or set(config) != {
        "branch",
        "multiplier",
        "increment",
        "modulus",
    }:
        raise ValueError(
            "branch config requires branch, multiplier, increment, and modulus"
        )
    branch = config["branch"]
    if branch not in ("a", "b", "c"):
        raise ValueError("branch must be a, b, or c")
    multiplier, increment, modulus = (
        config[key] for key in ("multiplier", "increment", "modulus")
    )
    if (
        any(type(value) is not int for value in (multiplier, increment, modulus))
        or modulus <= 1
        or not 0 <= multiplier < modulus
        or not 0 <= increment < modulus
    ):
        raise ValueError(
            "parameters must be integers with modulus > 1 and 0 <= multiplier, increment < modulus"
        )
    final_states = []
    for initial in states:
        value = initial
        for _ in range(steps):
            value = (multiplier * value + increment) % modulus
        final_states.append(value)
    return {
        "branch": branch,
        "seed": source["seed"],
        "sample_count": count,
        "steps": steps,
        "parameters": {
            "multiplier": multiplier,
            "increment": increment,
            "modulus": modulus,
        },
        "aggregate": sum(final_states),
        "weighted_checksum": sum(
            (index + 1) * value for index, value in enumerate(final_states)
        ),
        "minimum": min(final_states),
        "maximum": max(final_states),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--branch", choices=("a", "b", "c"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = simulate(read_json(args.input), read_json(args.config))
        if result["branch"] != args.branch:
            raise ValueError("branch config does not match --branch")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"simulate: {error}\n")


if __name__ == "__main__":
    main()
