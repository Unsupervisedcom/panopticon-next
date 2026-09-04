# Contributing

Panopticon is in active development. Bug reports, focused pull requests, and concrete accounts of
how the fleet behaves in real use are welcome.

## Before opening a pull request

For a substantial change, open an issue first so we can align on the user problem and the product
boundary. Small bug fixes can go directly to a pull request when the expected behavior is clear.

Keep pull requests narrow. Include tests that fail before the fix and pass after it, and update the
relevant requirement under `specs/` when behavior changes. Run the same baseline checks as CI:

```sh
make check
npx --yes rfc2119@0.7.0 check
```

Do not commit credentials, task transcripts, operator configuration, private planning documents,
or artifacts from a live fleet. Use synthetic fixtures in tests.

## What makes a useful report

- The Panopticon version or commit
- Host operating system, Docker runtime, Python version, and agent harness
- The exact action you took and the result you expected
- The error message and a minimal reproduction, with secrets removed

Security-sensitive reports should not include live tokens or repository credentials in a public
issue. Describe the affected boundary and reproduction without the secret material.
