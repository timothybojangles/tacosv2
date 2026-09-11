# Development baseline

The legacy import scan finds requests, urllib3, CustomTkinter and Pillow. pytest is needed for tests, and PyInstaller for the existing packaging approach. tkinter belongs to Python's Windows distribution, not a pip package.

The requirements `.in` files are **resolution inputs, not tested version locks**. This environment did not have the packages installed; network approval for package installation was cancelled. Do not claim a reproducible build until a dependency-enabled Windows machine resolves, verifies and commits exact runtime/dev/build dependency versions. Rust is also absent here, so no Tauri build has been verified.

## Windows developer setup

Run each line separately in PowerShell from the repository root. This avoids activation-policy issues. These are developer instructions; end users will receive installers.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.in
.\.venv\Scripts\python.exe -m pytest -q
```

Use Python 3.12 as the initial test target, then explicitly test any other supported interpreter. No hosted CI is required.

The migration regression tests use local SQLite and mocked requests. Their fixture blocks network access and isolates generated files in temporary directories. Existing tests remain in place. Known defects use strict xfail with AssertionError only: unrelated exceptions still fail, and an unexpected pass requires removing the marker and checking the fix. xfail is not proof that the defect is acceptable or that the new app may ship with it.

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_migration_regressions.py
.\.venv\Scripts\python.exe -m pytest -q -m "not legacy_gap"
```

INV-001 covers two numeric location cases; INV-002 covers malformed quantity and malformed cost. SO-001 guards repeat order creation after payment failure. REF-001 guards complete-snapshot preservation. Two positive inventory cases check that valid inputs still work. These tests have been syntax-checked but not executed in the authoring environment.

Before committing locks: validate the full suite in a clean environment, audit licences, resolve build dependencies on Windows and exercise legacy packaging from a clean checkout. Keep Windows-only transitive packages in a Windows-specific lock where appropriate. Do not silently call pip freeze a universal cross-platform lock.

## Next environment requirements

The desktop prototype needs an environment with permitted Python/npm/Cargo package downloads, Rust and Windows build tools, plus a Windows machine for installer verification. Node was present in the authoring environment, but that does not verify frontend dependency availability. Code can be reviewed here; native Windows build results must come from an actual Windows build.

SharePoint distribution requires no app integration or Graph permissions for v1: users download files through their existing company access and run them locally. Configure all working data under local application storage, outside synchronised folders.
