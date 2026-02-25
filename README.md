# fractalyze PyPI

Private [PEP 503](https://peps.python.org/pep-0503/) package index hosted
on GitHub Pages.

**Index URL:** https://fractalyze.github.io/pypi/simple/

## Usage

```bash
pip install \
    --index-url https://fractalyze.github.io/pypi/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    <package>
```

## How it works

1. Source repos (e.g. `fractalyze/jax`) create GitHub releases with `.whl`
   assets.
2. The `generate-index` workflow mirrors those wheels to this repo's releases
   and generates a static PEP 503 index.
3. The index is deployed to GitHub Pages.

Source repos trigger the index rebuild via `repository_dispatch` after
uploading wheels to their releases.

## Adding a new package

1. Add the package to `config.json`:
   ```json
   {"name": "my-package", "repo": "fractalyze/my-package"}
   ```
2. Ensure the source repo's release workflow uploads `.whl` files and
   triggers `rebuild-index`:
   ```yaml
   - name: Trigger PyPI index rebuild
     env:
       GH_TOKEN: ${{ secrets.PYPI_REPO_TOKEN }}
     run: |
       gh api repos/fractalyze/pypi/dispatches \
         -f event_type=rebuild-index
   ```

## Packages

Configured in [`config.json`](config.json). Browse the live index at
https://fractalyze.github.io/pypi/simple/.
