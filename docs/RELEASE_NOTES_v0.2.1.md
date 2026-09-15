# RASPA3 Campaign Manager 0.2.1

`0.2.1` is the stable promotion of the qualified `0.2.1.dev8` runtime.

## Promotion rule

No runtime behavior is intentionally changed from dev8.

The stable promotion is limited to:

- package/release version identity
- stable installation/documentation text
- release notes
- generic release clean-install workflow so stable asset names are validated
- repository SHA manifest refresh

All `src/raspa_campaign/*.py` runtime files other than `__init__.py` must remain byte-identical to the dev8 source commit. `__init__.py` changes only its release description and `__version__`.

## Qualification basis

The dev8 candidate passed:

- WSL native RASPA3 execution
- GA/OpenPBS single-task execution
- GA two-task / two-chunk concurrent PBS execution
- one-attempt-per-task and no duplicate task execution
- GA four-task / two-chunk / two-worker manager concurrency
- four-way native RASPA3 process overlap
- direct one-second CPU sampling on ga01

Final direct CPU probe:

```text
max RASPA3 process count          4
max summed CPU                    400.50889960118894 %
max equivalent CPU cores          4.00508899601189
samples with >=4 RASPA3           20
samples with all four >=50% CPU   20
joblib used                       false
parallelism                       asyncio workers + native RASPA3 subprocesses
```

Each of the four observed RASPA3 processes reached approximately 100% single-core CPU utilization.

## Scope boundary

The qualification supports the execution/orchestration layer. It does not, by itself, establish:

- adsorption stationarity
- universal RASPA2/RASPA3 scientific parity
- universal force-field validity
- formal performance superiority

Scientific settings remain explicit campaign responsibilities.

## Distribution

Stable release assets:

```text
RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip
RASPA3_CAMPAIGN_MANAGER_v0.2.1_FULL.zip.sha256
RASPA3_CAMPAIGN_MANAGER_v0.2.1_SOURCE.zip
RASPA3_CAMPAIGN_MANAGER_v0.2.1_SOURCE.zip.sha256
raspa_campaign_manager-0.2.1-py3-none-any.whl
raspa_campaign_manager-0.2.1-py3-none-any.whl.sha256
raspa_campaign_manager_v0.2.1.pyz
raspa_campaign_manager_v0.2.1.pyz.sha256
```

The FULL bundle also contains the frozen qualification JSON evidence used for promotion.
