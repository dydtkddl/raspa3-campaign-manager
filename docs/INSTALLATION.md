# Installation

## Requirements

- Linux or WSL2
- Python >= 3.11 with `venv`
- `sha256sum`
- `unzip`
- RASPA3 installed separately for real calculations

The installer searches:

```text
python3.13 → python3.12 → python3.11 → python3
```

and uses the first interpreter with Python >=3.11.

## Download

From the GitHub Release `v0.2.1`, download:

```text
RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip
RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip.sha256
```

## Verify the FULL ZIP

```bash
cd /mnt/c/Users/user/Downloads

cat RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip.sha256
sha256sum RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip
```

The hashes must match.

## Install side-by-side

```bash
unzip -q RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip
cd RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL

bash INSTALL_RASPA3_CAMPAIGN_MANAGER_WSL_v0.2.1.sh
```

Default installation:

```text
~/.local/opt/raspa-campaign-manager/releases/0.2.1/
```

The default installer does **not** change the existing `current` symlink.

## Verify

```bash
~/.local/opt/raspa-campaign-manager/releases/0.2.1/venv/bin/raspa-campaign --version
```

Expected:

```text
raspa-campaign 0.2.1
```

## Installation smoke test

```bash
bash RUN_RCM_0_2_1_INSTALL_SMOKE_TEST.sh
```

Expected:

```text
INSTALL_SMOKE_TEST=PASS
```

This validates the installed CLI surface; it does not execute RASPA3 and is not scientific qualification.

## Reinstall for validation

If dev8 is already installed:

```bash
bash INSTALL_RASPA3_CAMPAIGN_MANAGER_WSL_v0.2.1.sh \
  --force-reinstall
```

The previous dev8 installation is archived with a UTC timestamp.
