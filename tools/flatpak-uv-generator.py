#!/usr/bin/env python3
"""Generate flatpak-builder modules for NegPy's Python dependencies from uv.lock.

NegPy pins every runtime dependency (and their transitive dependencies) in
uv.lock, so instead of re-resolving with pip we read the lock file directly.
This keeps the Flatpak on exactly the versions upstream tests with.

Usage:
    flatpak-uv-generator.py NegPy/uv.lock -o generated/python3-modules.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

from packaging.markers import Marker
from packaging.tags import parse_tag

PYTHON_VERSION = "3.13"
ARCHES = ("x86_64", "aarch64")
# glibc of the freedesktop 25.08 base used by org.kde.Platform//6.11
GLIBC = (2, 42)

# Upstream packages that are not installed from uv.lock.
SKIP = {
    "negpy",  # the app itself
    "negpy-core",  # built from the NegPy checkout with maturin
    "pyqt6",  # provided by com.riverbankcomputing.PyQt.BaseApp
    "pyqt6-qt6",
    "pyqt6-sip",
    "pyqt6-charts",  # built from source against the runtime's QtCharts
    "pyqt6-charts-qt6",
}

# Optional dependency groups of NegPy to include.
EXTRAS = ("sane",)

# Packages whose binary wheels are used instead of building the sdist.
PREFER_WHEELS = {"onnxruntime", "rawpy", "wgpu", "protobuf"}

# Build-time Python dependencies not recorded in uv.lock, which only covers
# runtime dependencies. These must be installed before the dependent package
# because pip runs with --no-build-isolation.
BUILD_DEPS = {
    "numpy": ["meson-python"],
    "imagecodecs": ["numpy"],
    "pillow": ["pybind11"],
}

# Extra module keys for packages that need a non-default build environment.
MODULE_OVERRIDES = {
    "imagecodecs": {
        "build-options": {
            "env": {
                "CFLAGS": "-I/app/include -I/app/include/libjxr/common"
                " -I/app/include/libjxr/image -I/app/include/libjxr/glue",
                "LDFLAGS": "-L/app/lib",
            }
        }
    },
}

# Build tools not present in the SDK, with their pinned wheels.
EXTRA_MODULES = [
    {
        "name": "python3-meson-python",
        "requirement": "meson-python",
        "install": ["pyproject-metadata==0.12.1", "meson-python==0.22.1"],
        "sources": [
            {
                "type": "file",
                "url": "https://files.pythonhosted.org/packages/b9/8e/d883718e872abc214ba053f60f7e6edc2abd5ffbd1544beb01a9c92ddd3c/pyproject_metadata-0.12.1-py3-none-any.whl",
                "sha256": "f7162d580a96386a8eb096da06215f981f547d1490f03055ef99e323bc2da427",
            },
            {
                "type": "file",
                "url": "https://files.pythonhosted.org/packages/96/62/0c1cf20fe158b0e623e78acbf5fb6de5f1410a408f65f727e4090497627b/meson_python-0.22.1-py3-none-any.whl",
                "sha256": "258fde948cf45aab5df23fcc9e17a9050ee73d4e1002fe6cf86519f61c414778",
            },
        ],
        # Only needed while building numpy.
        "cleanup": ["*"],
    },
    {
        "name": "python3-pybind11",
        "requirement": "pybind11",
        "install": ["pybind11==3.1.0"],
        "sources": [
            {
                "type": "file",
                "url": "https://files.pythonhosted.org/packages/33/fd/8762f7ee3e4e4be6d1d846cffb4916dd9bb02b2f800d3603718a0efe494c/pybind11-3.1.0-py3-none-any.whl",
                "sha256": "b8488090f8acffbcb6b5d6a85571a6827a0a2981ffb75e5a0b27b87c4a6b7dd0",
            },
        ],
        # Only needed while building pillow.
        "cleanup": ["*"],
    },
]


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def marker_env(arch: str) -> dict[str, str]:
    return {
        "implementation_name": "cpython",
        "implementation_version": PYTHON_VERSION + ".0",
        "os_name": "posix",
        "platform_machine": arch,
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": PYTHON_VERSION + ".0",
        "python_version": PYTHON_VERSION,
        "sys_platform": "linux",
        "extra": "",
    }


def applies(dep: dict) -> bool:
    marker = dep.get("marker")
    if not marker:
        return True
    return any(Marker(marker).evaluate(marker_env(arch)) for arch in ARCHES)


def wheel_tags(filename: str):
    # name-version(-build)?-pytag-abitag-plattag.whl
    parts = filename[: -len(".whl")].split("-")
    return parse_tag("-".join(parts[-3:]))


def wheel_supported(filename: str, arch: str) -> bool:
    py = "cp" + PYTHON_VERSION.replace(".", "")
    for tag in wheel_tags(filename):
        if tag.interpreter not in (py, "py3", "cp3") and not (
            tag.abi == "abi3" and tag.interpreter.startswith("cp3")
        ):
            continue
        if tag.abi not in (py, "abi3", "none"):
            continue
        if tag.platform == "any":
            return True
        m = re.fullmatch(r"manylinux_(\d+)_(\d+)_(\w+)", tag.platform)
        if m and m.group(3) == arch and (int(m.group(1)), int(m.group(2))) <= GLIBC:
            return True
        legacy = {"manylinux2014": (2, 17), "manylinux2010": (2, 12), "manylinux1": (2, 5)}
        m = re.fullmatch(r"(manylinux\w+?)_(\w+)", tag.platform)
        if m and m.group(1) in legacy and m.group(2) == arch:
            return True
    return False


def file_source(entry: dict, only_arch: str | None = None) -> dict:
    algo, digest = entry["hash"].split(":", 1)
    assert algo == "sha256", entry
    src = {"type": "file", "url": entry["url"], "sha256": digest}
    if only_arch:
        src["only-arches"] = [only_arch]
    return src


def package_sources(pkg: dict) -> list[dict]:
    name = pkg["name"]
    wheels = pkg.get("wheels", [])
    filename = lambda w: w["url"].rsplit("/", 1)[-1]

    universal = [w for w in wheels if filename(w).endswith("-none-any.whl")]
    if universal:
        return [file_source(universal[0])]

    if name in PREFER_WHEELS or "sdist" not in pkg:
        sources = []
        for arch in ARCHES:
            candidates = [w for w in wheels if wheel_supported(filename(w), arch)]
            if not candidates:
                sys.exit(f"error: no {arch} wheel for {name} {pkg['version']}")
            sources.append(file_source(candidates[0], arch))
        return sources

    return [file_source(pkg["sdist"])]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lockfile", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()

    lock = tomllib.loads(args.lockfile.read_text())
    packages = {normalize(p["name"]): p for p in lock["package"]}

    root = packages["negpy"]
    roots = [d for d in root.get("dependencies", []) if applies(d)]
    for extra in EXTRAS:
        roots += [d for d in root.get("optional-dependencies", {}).get(extra, []) if applies(d)]

    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        name = normalize(name)
        if name in seen or name in SKIP:
            return
        seen.add(name)
        if name in packages:
            for dep in packages[name].get("dependencies", []):
                if applies(dep):
                    visit(dep["name"])
        for dep in BUILD_DEPS.get(name, []):
            visit(dep)
        order.append(name)

    for dep in roots:
        visit(dep["name"])

    extra = {normalize(m["requirement"]): m for m in EXTRA_MODULES}
    modules = []
    for name in order:
        if name in extra:
            m = extra[name]
            requirements = m["install"]
            sources = m["sources"]
            module = {"name": m["name"]}
            if "cleanup" in m:
                module["cleanup"] = m["cleanup"]
        else:
            pkg = packages[name]
            requirements = [f"{pkg['name']}=={pkg['version']}"]
            sources = package_sources(pkg)
            module = {"name": f"python3-{name}"}
        module["buildsystem"] = "simple"
        module["build-commands"] = [
            "pip3 install --verbose --exists-action=i --no-index --no-deps --ignore-installed"
            ' --find-links="file://${PWD}" --prefix=${FLATPAK_DEST}'
            + "".join(f' "{r}"' for r in requirements)
            + " --no-build-isolation"
        ]
        module["sources"] = sources
        module.update(MODULE_OVERRIDES.get(name, {}))
        modules.append(module)

    manifest = {
        "name": "python3-modules",
        "buildsystem": "simple",
        "build-commands": [],
        "modules": modules,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {len(modules)} modules to {args.output}: {', '.join(order)}")


if __name__ == "__main__":
    main()
