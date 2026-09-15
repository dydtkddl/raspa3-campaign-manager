# Recovery & Retry

Start by observing:

```bash
raspa-campaign status --root <campaign> --show-tasks
raspa-campaign recover --root <campaign>
```

`recover` is read-only.

Inspect claims:

```bash
raspa-campaign claims inspect --root <campaign>
```

Do not release a claim until the previous owner/process is proven dead.

Retries preserve previous attempts. They do not overwrite old evidence.

If the native output is valid and only parser logic changed, use `reparse` rather than rerunning RASPA3:

```bash
raspa-campaign reparse --root <campaign>
```

Use drain to stop launching new tasks while allowing current work to finish:

```bash
raspa-campaign drain --root <campaign>
```

After reviewed recovery actions:

```bash
raspa-campaign audit --root <campaign>
```
