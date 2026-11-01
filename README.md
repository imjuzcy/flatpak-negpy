# flatpak-negpy

Flatpak packaging repository for [NegPy](https://github.com/marcinz606/NegPy), a tool for processing film negatives.

## Contents

- `flatpak.yml`: Flatpak manifest and module definitions.
- `extra/`: Desktop file, launcher script, and AppStream metadata.
- `tools/flatpak-uv-generator.py`: Generates the Python dependency modules from NegPy's `uv.lock`.

## Generated sources

The manifest references two files that are generated from the pinned NegPy release and are not committed:

- `generated/python3-modules.json`: Python dependencies, from `uv.lock`.
- `cargo-sources/negpy-core/negpy-core.cargo-sources.json`: Rust crates for `negpy_core`, from `negpy_core/Cargo.lock`.

To generate them locally (requires Python 3.11+ with `packaging`, and Poetry):

```sh
TAG=$(yq -r '.modules.[] | select(.name=="NegPy").sources.[0].tag' flatpak.yml)
git clone --branch "$TAG" https://github.com/marcinz606/NegPy.git /tmp/NegPy
git clone https://github.com/flatpak/flatpak-builder-tools.git /tmp/flatpak-builder-tools
python3 tools/flatpak-uv-generator.py /tmp/NegPy/uv.lock -o generated/python3-modules.json
mkdir -p cargo-sources/negpy-core
(cd /tmp/flatpak-builder-tools/cargo && poetry install && poetry run python3 flatpak-cargo-generator.py \
  /tmp/NegPy/negpy_core/Cargo.lock -o "$OLDPWD/cargo-sources/negpy-core/negpy-core.cargo-sources.json")
flatpak-builder --user --install-deps-from=flathub --force-clean --install build flatpak.yml
```

When updating NegPy, also check that `base-version` still provides the PyQt6 version pinned in NegPy's `pyproject.toml`, and that the `PyQt6-Charts` module matches it.

## Build and release

This repository builds the Flatpak bundle with GitHub Actions using `.github/workflows/build.yml`.

## License

This repository is licensed under **GPL-3.0-only**. See [LICENSE](./LICENSE).
