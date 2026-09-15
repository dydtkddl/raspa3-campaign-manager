# CLI Reference

Top-level commands in `0.2.1.dev8`:

```text
init
doctor
validate
build
plan
inventory
preview-inputs
estimate-storage
run
collect
status
task
retry
recover
claims
reparse
export
audit
compress-logs
drain
resume
qualify
qc
pbs
dashboard
migrate
stage-upgrade-data
archive
```

## Common commands

### Create campaign

```bash
raspa-campaign init PATH --preset single_h2_pilot
```

Presets:

```text
single_h2_pilot
ispt_multigas_293K
multigas_qualification_short
```

### Run

```bash
raspa-campaign run --root ROOT --dry-run
```

Real native execution:

```bash
raspa-campaign run \
  --root ROOT \
  --workers N \
  --confirm-production
```

Options include:

```text
--workers
--chunk
--limit
--dry-run
--retry-failed
--allocation
--confirm-production
--json
```

### PBS

```bash
raspa-campaign pbs render --root ROOT --preview
raspa-campaign pbs render --root ROOT
raspa-campaign pbs submit --root ROOT --yes
```

### Export

```bash
raspa-campaign export --root ROOT
```

Supports task/state/gas filters, all-attempt export, metadata CSV join and optional Parquet.

### Audit

```bash
raspa-campaign audit --root ROOT
```

Modes:

```text
safe
fast
```

Scopes:

```text
all-attempts
latest
structure
```
