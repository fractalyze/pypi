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

### Wheel hosting

The index emits a [PEP 658](https://peps.python.org/pep-0658/) `.metadata`
sidecar (`data-core-metadata`), so `pip-compile` / `pip` resolve dependency
metadata without downloading rejected-candidate wheels.

By default wheel `href`s point at this repo's github release assets. Setting
`PYPI_WHEELS_BUCKET` + `PYPI_WHEELS_BASE_URL` (see the workflow env) makes
`generate_index.py` also upload the wheel + sidecar to that S3 bucket and emit
the bucket URL as the `href`, keeping consumers off the github release-asset
CDN (which intermittently `504`s on large wheels). The bucket is provisioned by
`pypi/01-wheels-bucket.sh` in `fractalyze/rbe-infra`. The github release mirror
still runs as the enumeration source and a durable backup.

Sidecars + S3 copies are bounded to the newest `PYPI_WHEELS_BACKFILL_LAST`
source releases per repo (default 10; `0` = all). The github mirror holds every
wheel ever built (~100 GB of mostly stale daily-dev builds); backfilling all of
it to S3 would haul the whole ~100 GB on the first run. Older wheels keep their
github href and resolve as before — an S3 href is emitted only where a sidecar
exists, so the index never points at a wheel the bucket doesn't have.

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
