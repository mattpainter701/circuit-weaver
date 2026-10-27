# Publishing circuit-weaver to PyPI

Releases use PyPI trusted publishing (OIDC). No PyPI token is stored in
GitHub. The tag-triggered workflow builds the distributions once, tests that
exact wheel on every supported Python/OS gate, validates generated schematics
with an official KiCad installation, and publishes the same artifact only
after every gate passes.

## One-time PyPI and GitHub setup

Create a pending trusted publisher at
<https://pypi.org/manage/account/publishing/> with:

- PyPI project: `circuit-weaver`
- GitHub owner: `mattpainter701`
- Repository: `circuit-weaver`
- Workflow: `release.yml`
- Environment: `pypi`

Create a GitHub environment named `pypi` and add any desired deployment
reviewers. The publish job is the only job granted `id-token: write` and
`contents: write`.

## Version source

`src/circuit_weaver/__init__.py` is the single authoritative version source.
Setuptools reads `circuit_weaver.__version__` dynamically; do not add a second
version value to `pyproject.toml`.

For this release:

```python
__version__ = "0.35.0"
```

The release workflow rejects a tag whose value does not exactly match the
package version, so a stale tag cannot accidentally republish an older wheel.

## Release process

1. Update `src/circuit_weaver/__init__.py` and `CHANGELOG.md` in the release PR.
2. Confirm CI is green, including Linux Python 3.10-3.14, Windows Python
   3.12/3.13, the wheel contract, bundled-skill synchronization, and real
   KiCad 8/9/10 final-artifact validation.
3. Merge the release commit to `main`.
4. Create an annotated tag that exactly matches the package version:

   ```bash
   git switch main
   git pull --ff-only
   git tag -a v0.35.0 -m "circuit-weaver 0.35.0"
   git push origin v0.35.0
   ```

5. Watch the `Release to PyPI` workflow. It will:

   - reject a tag/version mismatch;
   - build one wheel and one source archive;
   - run `twine check` and inspect wheel metadata;
   - install and test that exact wheel on all supported Python/OS gates;
   - start the MCP server over stdio in a real client handshake test;
   - generate representative simple and complex designs and parse them with
     KiCad 8, KiCad 9, and KiCad 10 from KiCad's official stable Ubuntu PPAs;
   - publish the tested files through OIDC and attach them to a GitHub release.

Never delete and recreate a failed tag after artifacts were published. Bump to
a new patch version instead because PyPI filenames are immutable.

## Local preflight

Run these commands from a clean checkout before tagging:

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
python -m ruff check src tests
python -c "import os, pytest; os.environ.pop('CI', None); os.environ['CIRCUIT_WEAVER_TEST_PACKAGE']='source'; raise SystemExit(pytest.main(['tests', '-q']))"
python -m build
python -m twine check dist/*
```

Then verify the built wheel in an isolated environment rather than relying on
the editable checkout:

```bash
python -m venv .wheel-test
.wheel-test/bin/python -m pip install "dist/circuit_weaver-0.35.0-py3-none-any.whl[test]"
CIRCUIT_WEAVER_TEST_PACKAGE=wheel .wheel-test/bin/python -m pytest tests -q
```

On Windows, replace `.wheel-test/bin/python` with
`.wheel-test/Scripts/python.exe` and set
`$env:CIRCUIT_WEAVER_TEST_PACKAGE = "wheel"` before invoking pytest.

## Post-publish verification

Use a new virtual environment so a local editable checkout cannot mask the
PyPI package:

```bash
python -m venv .pypi-smoke
.pypi-smoke/bin/python -m pip install --no-cache-dir "circuit-weaver[mcp]==0.35.0"
.pypi-smoke/bin/circuit-weaver --version
.pypi-smoke/bin/python -c "from importlib.metadata import version; print(version('circuit-weaver'))"
```

Verify the release at <https://pypi.org/project/circuit-weaver/> and confirm
that the GitHub release contains the same wheel and source archive.

## Troubleshooting

| Failure | Resolution |
|---|---|
| `Tag/version mismatch` | Update the tag or `circuit_weaver.__version__`; they must be identical. |
| PyPI OIDC 403 | Verify owner, repository, workflow filename, and `pypi` environment in the trusted-publisher record. |
| `twine check` failure | Fix package metadata/readme rendering and create a new build before tagging. |
| Wheel test imports the checkout | Run outside the repository or confirm the import path contains `site-packages`. |
| PyPI says the file already exists | Bump the patch version; PyPI distributions cannot be overwritten. |
| KiCad gate fails to install | Check the official `kicad-8.0-releases` and `kicad-10.0-releases` PPA status before retrying the workflow. |
