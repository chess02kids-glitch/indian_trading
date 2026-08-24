"""Static migration gate used by CI before operator-run database deployment."""

from __future__ import annotations

from pathlib import Path


def verify_migrations(directory: Path = Path("migrations")) -> list[str]:
    """Return migration validation errors without connecting to a database."""
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    errors: list[str] = []
    expected = [f"{number:03d}" for number in range(1, len(files) + 1)]
    actual = [file.name[:3] for file in files]
    if actual != expected:
        errors.append(f"migration numbering must be contiguous: found {actual}")
    for file in files:
        content = file.read_text(encoding="utf-8").strip()
        if not content:
            errors.append(f"empty migration: {file}")
        if "DROP TABLE" in content.upper():
            errors.append(f"destructive DROP TABLE requires review: {file}")
    return errors


def main() -> int:
    """Print migration gate failures and return a CI-compatible exit code."""
    errors = verify_migrations()
    if errors:
        print(
            "Migration verification failed:\n"
            + "\n".join(f"- {error}" for error in errors)
        )
        return 1
    print("Migration verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
