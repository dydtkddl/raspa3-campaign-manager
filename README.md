# RASPA3 Campaign Manager

Fail-closed, resumable orchestration for large RASPA3 adsorption campaigns across local/WSL and GA/OpenPBS environments.

> Current stable release: **0.2.1**

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

## Validated 0.2.1 execution scope

The stable release preserves the dev8 runtime implementation and is promoted after qualification of:

- real WSL RASPA3 execution
- real GA/OpenPBS execution
- final production-average loading parsing
- `PENDING → CLAIMED → RUNNING → COMPLETE`
- evidence/audit/export/archive workflows
- public GitHub FULL-bundle distribution
- Python 3.11 / 3.12 / 3.13 CI and clean installation
- two-task / two-chunk concurrent PBS execution on GA
- shared-NFS claim ownership without duplicate task execution
- four-task / two-chunk / two-worker manager execution
- four overlapping native RASPA3 process intervals
- direct CPU sampling showing four independent RASPA3 processes consuming approximately four CPU cores concurrently

The concurrency qualification is an execution/orchestration result. It is not a claim of adsorption stationarity, universal scientific parity, or performance superiority.

## Install

Download these two assets from the `v0.2.1` GitHub Release:

```text
RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip
RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip.sha256
```

Then:

```bash
cd /mnt/c/Users/user/Downloads

sha256sum -c RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip.sha256

unzip -q RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip
cd RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL

bash INSTALL_RASPA3_CAMPAIGN_MANAGER_WSL_v0.2.1.sh
bash RUN_RCM_0_2_1_INSTALL_SMOKE_TEST.sh
```

To make 0.2.1 the global `current` release after validation:

```bash
bash INSTALL_RASPA3_CAMPAIGN_MANAGER_WSL_v0.2.1.sh --force-reinstall --set-current
```

Verify:

```bash
~/.local/opt/raspa-campaign-manager/releases/0.2.1/venv/bin/raspa-campaign --version
```

Expected:

```text
raspa-campaign 0.2.1
```

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

`pbs.allowed_hosts` is a fail-closed runtime allowlist. When scheduler placement must be constrained to a specific GA node, set `pbs.host` explicitly.

## Documentation

- [Installation](docs/INSTALLATION.md)
- [Quick Start](docs/QUICK_START.md)
- [GA / OpenPBS](docs/GA_OPENPBS.md)
- [Recovery & Retry](docs/RECOVERY_RETRY.md)
- [CLI Reference](docs/CLI_REFERENCE.md)
- [0.2.1 release notes](docs/RELEASE_NOTES_v0.2.1.md)
- [Documentation map](docs/DOCS_SITE_MAP.md)

## Release policy

`0.2.1` is the stable promotion of the qualified `0.2.1.dev8` runtime.

The promotion changes release/version metadata, documentation, and release CI packaging logic. Runtime implementation files are frozen relative to dev8 except for `src/raspa_campaign/__init__.py`, where only the release identity/version string changes.

Release assets are immutable after publication; corrections require a new version.
