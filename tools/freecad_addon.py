#!/usr/bin/env python3
"""Build the FreeCAD addon: ``freecad/`` and a copy of the reader.

    python tools/freecad_addon.py            # writes _freecad/OpenSW/
    python tools/freecad_addon.py -o folder

The folder is what FreeCAD loads: copy it into FreeCAD's ``Mod`` directory,
or publish it as the addon's repository.

- ``Init.py``, ``importOpenSW.py``, ``package.xml`` and ``opensw_fc/`` come
  from ``freecad/``, unchanged;
- ``opensw_fc/sldprt/`` is ``src/sldprt``, unchanged: it only imports itself
  relatively, so it runs under another package name;
- ``LICENSE`` and ``NOTICE`` are the repository's;
- ``version.txt``: the commit it all comes from.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "freecad"
READER = ROOT / "src" / "sldprt"
SKIPPED = shutil.ignore_patterns("__pycache__", "*.pyc")


def commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def build(out: Path) -> list[Path]:
    """Write the addon into *out*, replacing what the build wrote there
    before; anything else in *out* (a ``.git``) is left alone."""
    out.mkdir(parents=True, exist_ok=True)
    for old in ("opensw_fc", "Init.py", "importOpenSW.py", "package.xml", "README.md",
                "LICENSE", "NOTICE", "version.txt"):
        target = out / old
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    shutil.copytree(SOURCE, out, ignore=SKIPPED, dirs_exist_ok=True)
    shutil.copytree(READER, out / "opensw_fc" / "sldprt", ignore=SKIPPED)
    for name in ("LICENSE", "NOTICE"):
        shutil.copyfile(ROOT / name, out / name)
    (out / "version.txt").write_text(commit() + "\n", encoding="utf-8", newline="\n")
    return sorted(p for p in out.rglob("*") if p.is_file() and ".git" not in p.parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--out", type=Path, default=ROOT / "_freecad" / "OpenSW")
    args = parser.parse_args(argv)
    files = build(args.out)
    print(f"{len(files)} files, {sum(f.stat().st_size for f in files) / 1e3:.0f} kB "
          f"in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
