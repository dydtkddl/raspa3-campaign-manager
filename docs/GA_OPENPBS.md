# GA / OpenPBS

## Preconditions

Create tasks and a plan first:

```bash
raspa-campaign build --root <campaign>
raspa-campaign plan --root <campaign>
```

The campaign must explicitly select:

```toml
[pbs]
profile = "ga-openpbs"
```

The validated GA model separates:

- `ga00`: control/submission host
- `ga01`–`ga04`: execution hosts

The renderer rejects `ga00` as an execution target.

## Preview

```bash
raspa-campaign pbs render --root <campaign> --preview
```

Review resources, hosts, Python/manager paths and chunk count.

## Render

```bash
raspa-campaign pbs render --root <campaign>
```

Key immutable files are created under the render directory:

```text
run.pbs
job_spec.json
job_runner.py
render_receipt.json
```

## Submit

Submission requires:

- `[pbs] enabled = true`
- `[campaign] production_authorized = true`
- explicit `--yes`

```bash
raspa-campaign pbs submit --root <campaign> --yes
```

The manager executes `qsub` on the rendered `run.pbs` and records the returned PBS job ID.

An interrupted or ambiguous `qsub` is never automatically retried. Inspect the submit receipt before submitting again.

## Monitor

```bash
qstat -u "$USER"
raspa-campaign status --root <campaign> --show-tasks
```
