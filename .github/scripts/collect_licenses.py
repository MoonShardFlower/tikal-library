"""
Collects the licenses of the third-party software that a standalone build bundles, for its release zip:

- the license files of every distribution that <distribution> requires at runtime, directly or indirectly
- Python's own license, which also covers the libraries of Python's Windows build (e.g., libffi, bzip2)
- the subfolders of --extra: license texts that no installed distribution brings along. A subfolder named like a
  distribution completes that distribution's license files (e.g., for an old wheel that has none).

They go into <output>, one folder per component, with an overview in README.txt.
Run it with the Python environment the build used.

Usage: python .github/scripts/collect_licenses.py <distribution> <output> [--extra <folder>]
"""

import argparse
import platform
import shutil
import sys
from importlib.metadata import Distribution, distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# License files next to METADATA in wheels older than metadata 2.4 (which puts them below licenses/)
_LICENSE_PREFIXES = ("LICEN", "COPYING", "NOTICE", "AUTHORS")


def runtime_distributions(name: str) -> list[Distribution]:
    """The distributions that *name* requires at runtime, directly or indirectly."""
    found: dict[str, Distribution] = {}
    pending = list(distribution(name).requires or [])
    while pending:
        requirement = Requirement(pending.pop())
        # Leaves out extras (e.g., dev) and what other platforms need
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        dist = distribution(requirement.name)
        key = canonicalize_name(dist.metadata["Name"])
        if key not in found:
            found[key] = dist
            pending += dist.requires or []
    return list(found.values())


def copy_license_files(dist: Distribution, target: Path) -> int:
    """Copies the license files in the .dist-info folder of *dist* into *target*. Returns how many there were."""
    count = 0
    for file in dist.files or []:
        parts = file.parts
        if not parts[0].endswith(".dist-info"):
            continue
        if len(parts) > 2 and parts[1] == "licenses":
            relative = Path(*parts[2:])
        elif len(parts) == 2 and parts[1].upper().startswith(_LICENSE_PREFIXES):
            relative = Path(parts[1])
        else:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file.locate(), destination)
        count += 1
    return count


def license_name(dist: Distribution) -> str:
    """The license that the metadata of *dist* names."""
    metadata = dist.metadata
    if metadata.get("License-Expression"):
        return metadata["License-Expression"]
    # Some distributions put the whole license text into this field
    text = metadata.get("License") or ""
    if text and "\n" not in text and len(text) < 80:
        return text
    classifiers = metadata.get_all("Classifier") or []
    names = [c.split(" :: ")[-1] for c in classifiers if c.startswith("License ::")]
    return ", ".join(names) or "see its license files"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collects the licenses of the third-party software a build bundles."
    )
    parser.add_argument("distribution", help="the distribution that was built")
    parser.add_argument("output", type=Path, help="folder to write the licenses to")
    parser.add_argument(
        "--extra", type=Path, help="folder whose subfolders are copied as they are"
    )
    args = parser.parse_args()

    extras = sorted(p for p in args.extra.iterdir() if p.is_dir()) if args.extra else []
    extra_names = {folder.name for folder in extras}

    rows = []
    for dist in runtime_distributions(args.distribution):
        name = dist.metadata["Name"]
        copied = copy_license_files(dist, args.output / name)
        if copied == 0 and name not in extra_names:
            print(
                f"WARNING: {name} brings no license file along. Add its license to a folder {name} in --extra.",
                file=sys.stderr,
            )
        rows.append((name, dist.version, license_name(dist)))

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        sys.exit(f"Python's license is missing: {python_license}")
    (args.output / "Python").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, args.output / "Python" / "LICENSE.txt")
    rows.append(("Python", platform.python_version(), "PSF-2.0 and others"))

    names = {name for name, _, _ in rows}
    for folder in extras:
        shutil.copytree(folder, args.output / folder.name, dirs_exist_ok=True)
        if folder.name not in names:
            texts = ", ".join(file.stem for file in sorted(folder.iterdir()))
            rows.append((folder.name, "", texts))

    built = distribution(args.distribution)
    width = max(len(name) for name, _, _ in rows) + 2
    version_width = max(len(version) for _, version, _ in rows) + 2
    lines = [
        f"Third-party software in this build of {built.metadata['Name']} {built.version}",
        "and the licenses it is used under. Each has a folder of the same name with its license texts.",
        "",
    ]
    for name, version, license in sorted(rows, key=lambda row: row[0].lower()):
        lines.append(f"{name:<{width}}{version:<{version_width}}{license}")
    (args.output / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Collected the licenses of {len(rows)} components into {args.output}")


if __name__ == "__main__":
    main()
