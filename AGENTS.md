# Repository working rules

## Scope and context

- Read [docs/design.md](docs/design.md) and [docs/progress.md](docs/progress.md) before phase work. Consult [design decisions](docs/design-decisions.md) for rationale.
- Follow only the currently authorized phase. Keep changes small and coherent; do not add optional features, unrelated rewrites, or directories merely to match a proposed tree.
- Resolve genuine semantic conflicts before changing the contract. Keep accepted decisions, code, tests, and documentation consistent.

## Implementation

- Target Python 3.12, Linux, and macOS. Use the project environment after P02 creates it; do not silently substitute an older system Python.
- Prefer straightforward Python, standard-library functionality, descriptive names, and useful abstractions. Explain the architectural need for each new dependency.
- Do not introduce frameworks, plugin systems, or speculative subsystems.
- Keep scheduler state under one event-loop owner, use short SQLite transactions, and follow the design's attempt and publication protocols.

## Verification

- Run checks appropriate to the authorized phase. During implementation, diagnose failures, make scoped repairs, and rerun affected checks before reporting completion.
- Never delete tests, weaken assertions, or relax the design merely to obtain a passing result.
- Distinguish a blocked command from an implementation defect. Report executed commands, exit status, failures, skips, and limitations.
- Never fabricate tests, benchmark values, or CI status. Documentation alone does not establish working software.

## Repository safety

- Preserve unrelated work and keep operations workspace-scoped.
- Do not expose secrets, credential stores, private data, or full environment dumps. Do not commit runtime state, caches, virtual environments, personal paths, or proprietary materials.
- Do not stage, commit, push, merge, tag, publish, change visibility, or perform destructive Git operations without explicit user authorization. File-editing permission is not publication approval.

## Writing and progress

- Use plain, specific engineering language. Comments explain non-obvious reasoning rather than narrating code. Do not use em dashes.
- Avoid marketing language, generated banners, repetitive boilerplate, invented users or motivations, and unsupported guarantees.
- Do not claim generated work was entirely manually written, manufacture history, or introduce mistakes to appear human.
- Update [docs/progress.md](docs/progress.md) with the current phase and actual evidence. Separate prepared work, passed checks, user acceptance, and publication. Never mark review or push complete without evidence.
