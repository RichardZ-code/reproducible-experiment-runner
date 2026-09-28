"""Combine the three branch simulations in an explicit order."""

import argparse
import json
from pathlib import Path


def reject_constant(value: str) -> None:
    raise ValueError(f"nonfinite JSON value {value}")


def summarize(results: list[object]) -> dict[str, object]:
    branches = []
    for expected, result in zip(("a", "b", "c"), results, strict=True):
        if not isinstance(result, dict) or set(result) != {
            "branch",
            "seed",
            "sample_count",
            "steps",
            "parameters",
            "aggregate",
            "weighted_checksum",
            "minimum",
            "maximum",
        }:
            raise ValueError("branch result structure is invalid")
        if result["branch"] != expected:
            raise ValueError(f"expected branch {expected}")
        if any(
            type(result[key]) is not int
            for key in (
                "seed",
                "sample_count",
                "steps",
                "aggregate",
                "weighted_checksum",
                "minimum",
                "maximum",
            )
        ):
            raise ValueError("branch result has invalid numeric fields")
        if (
            result["sample_count"] < 1
            or result["steps"] < 1
            or result["minimum"] > result["maximum"]
            or not isinstance(result["parameters"], dict)
            or set(result["parameters"]) != {"multiplier", "increment", "modulus"}
        ):
            raise ValueError("branch result values are invalid")
        if any(
            type(result["parameters"][key]) is not int
            for key in ("multiplier", "increment", "modulus")
        ):
            raise ValueError("branch parameters are invalid")
        branches.append(result)
    if (
        len({(item["seed"], item["sample_count"], item["steps"]) for item in branches})
        != 1
    ):
        raise ValueError("branch seed, sample_count, and steps must agree")
    return {
        "seed": branches[0]["seed"],
        "sample_count": branches[0]["sample_count"],
        "steps": branches[0]["steps"],
        "branches": branches,
        "combined_total": sum(item["aggregate"] for item in branches),
        "combined_weighted_checksum": sum(
            item["weighted_checksum"] for item in branches
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for branch in ("a", "b", "c"):
        parser.add_argument(f"--{branch}", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        results = [
            json.loads(
                getattr(args, branch).read_text(encoding="utf-8"),
                parse_constant=reject_constant,
            )
            for branch in ("a", "b", "c")
        ]
        result = summarize(results)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"summarize: {error}\n")


if __name__ == "__main__":
    main()
