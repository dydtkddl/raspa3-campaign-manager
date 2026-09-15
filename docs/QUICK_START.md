# Quick Start

Assume dev8 is installed side-by-side:

```bash
RCM="$HOME/.local/opt/raspa-campaign-manager/releases/0.2.1.dev8/venv/bin/raspa-campaign"
```

Create a small campaign:

```bash
CAMPAIGN="$HOME/RASPA_CAMPAIGNS/H2_PILOT"
"$RCM" init "$CAMPAIGN" --preset single_h2_pilot
```

Review `campaign.toml`, inventory and native-input templates before execution.

Then:

```bash
"$RCM" inventory --root "$CAMPAIGN"
"$RCM" preview-inputs --root "$CAMPAIGN"

"$RCM" doctor --root "$CAMPAIGN"
"$RCM" validate --root "$CAMPAIGN"

"$RCM" build --root "$CAMPAIGN"

"$RCM" plan --root "$CAMPAIGN" --preview
"$RCM" plan --root "$CAMPAIGN"

"$RCM" run --root "$CAMPAIGN" --dry-run
```

For a real engine run, the campaign must explicitly authorize production and the command must include `--confirm-production`:

```bash
"$RCM" run \
  --root "$CAMPAIGN" \
  --workers 1 \
  --limit 1 \
  --confirm-production
```

Then:

```bash
"$RCM" status --root "$CAMPAIGN" --show-tasks
"$RCM" export --root "$CAMPAIGN"
"$RCM" audit --root "$CAMPAIGN"
```

Scale only after a real qualification task has been reviewed.
