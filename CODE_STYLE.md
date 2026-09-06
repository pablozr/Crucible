# Code Style

## Logical Spacing

Keep cohesive operations together. Separate distinct phases of a function with
one blank line when the separation makes the control flow easier to scan.

Typical phases include input resolution, validation, persistence, and response
construction. Do not add blank lines between consecutive statements in one
small operation, and do not extract a helper only to create visual spacing.

Ruff enforces Python formatting and linting. Logical spacing is a review rule:
Ruff preserves it but does not infer where it belongs.

## Python

- Use 79-character lines.
- Use Ruff formatting and the configured `E`, `W`, `F`, and `I` rules.
- Put data contracts in `schemas` as Pydantic `BaseModel` classes.
- Keep routes, services, and infrastructure concerns in their respective
  layers.

## TypeScript

- Keep imports ordered by module family: Node built-ins, packages, then local
  modules.
- Use the same logical-spacing rule as Python.
- Preserve the existing TypeScript compiler checks and tests.
