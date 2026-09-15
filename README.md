# RASPA3 Campaign Manager

Fail-closed, resumable orchestration for large RASPA3 adsorption campaigns across local/WSL and GA/OpenPBS environments.

> Current validated candidate: **0.2.1.dev8**

## What it does

RASPA3 Campaign Manager provides one execution layer for:

- campaign initialization and input review
- deterministic task materialization
- runtime estimation and LPT-balanced chunk planning
- bounded local/WSL RASPA3 execution
- GA/OpenPBS rendering and submission
- task states, claims, retries, recovery, and reparsing
- normalized CSV/optional Parquet export
- SHA-256 evidence and campaign audit
- scientific screening QC and qualification lanes

It is **not** a replacement for RASPA3 and does not automatically validate force fields, gas models, charges, cutoffs, cycle counts, or convergence.

## Validated 0.2.1.dev8 scope

The candidate has been exercised in:

- a real WSL RASPA3 calculation
- a real GA/OpenPBS single-task end-to-end calculation
- final production-average loading parsing
- `PENDING → CLAIMED → RUNNING → COMPLETE`
- evidence/audit and export
- versioned FULL bundle installation
- Python >=3.11 auto-discovery
- isolated side-by-side venv installation
- CLI installation smoke test

Large concurrent multi-worker production remains a separate qualification scope.

## Install

Download these two assets from the `v0.2.1.dev8` GitHub Release:

```text
RASPA3_CAMPAIGN_MANAGER_v0.2.1.dev8_FULL.zip
RASPA3_CAMPAIGN_MANAGER_v0.2.1.dev8_FULL.zip.sha256
```

Then:

```bash
cd /mnt/c/Users/user/Downloads

sha256sum RASPA3_CAMPAIGN_MANAGER_v0.2.1.dev8_FULL.zip

unzip -q RASPA3_CAMPAIGN_MANAGER_v0.2.1.dev8_FULL.zip
cd RASPA3_CAMPAIGN_MANAGER_v0.2.1.dev8_FULL

bash INSTALL_RASPA3_CAMPAIGN_MANAGER_WSL_v0.2.1.dev8.sh
bash RUN_RCM_DEV8_INSTALL_SMOKE_TEST.sh
```

Verify:

```bash
~/.local/opt/raspa-campaign-manager/releases/0.2.1.dev8/venv/bin/raspa-campaign --version
```

Expected:

```text
raspa-campaign 0.2.1.dev8
```

See [Installation](docs/INSTALLATION.md) and [Quick Start](docs/QUICK_START.md).

## Basic workflow

```text
init
→ inventory / preview-inputs
→ doctor / validate
→ build
→ plan
→ dry-run
→ run or PBS submit
→ status
→ export
→ audit
```

## Local execution

```bash
raspa-campaign run --root <campaign> --dry-run

raspa-campaign run \
  --root <campaign> \
  --workers <N> \
  --confirm-production
```

Native execution also requires `campaign.production_authorized=true`.

## GA / OpenPBS

```bash
raspa-campaign pbs render --root <campaign> --preview
raspa-campaign pbs render --root <campaign>
raspa-campaign pbs submit --root <campaign> --yes
```

See [GA / OpenPBS Guide](docs/GA_OPENPBS.md).

## Documentation

- [Installation](docs/INSTALLATION.md)
- [Quick Start](docs/QUICK_START.md)
- [GA / OpenPBS](docs/GA_OPENPBS.md)
- [Recovery & Retry](docs/RECOVERY_RETRY.md)
- [CLI Reference](docs/CLI_REFERENCE.md)
- [Documentation map](docs/DOCS_SITE_MAP.md)

## Release policy

`0.2.1.dev8` is published as a **pre-release candidate**, not as an assertion that every scientific model or large-scale concurrency mode has been qualified.

Release assets are versioned and should not be replaced in-place after publication.
