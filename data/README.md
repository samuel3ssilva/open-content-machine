# data/

This directory holds local data for Open Content Machine. Only one
subdirectory exists: `data/private/`.

## `data/private/`

`data/private/` is where your real, legitimately-exported connections CSV
(or any other real personal data source) goes. It is **git-ignored** — see
`.gitignore` — except for a `.gitkeep` marker that keeps the empty folder
present in the repository. Nothing you place here is ever committed, and
nothing here is ever sent anywhere: only `content_machine/providers/` can
perform network I/O, and it never has access to this directory's raw
contents (see [`docs/architecture.md`](../docs/architecture.md) and
[`docs/privacy.md`](../docs/privacy.md) for the trust boundaries this
enforces).

### Placing an export

1. Export your connections from the source platform yourself, through its
   official data-export feature. Scraping is not supported and will not be
   built (see [`docs/privacy.md`](../docs/privacy.md)).
2. Copy the resulting CSV into `data/private/`, for example:
   `data/private/connections.csv`.
3. Point the CLI at it directly, e.g.:
   ```bash
   content-machine audience validate data/private/connections.csv
   content-machine audience anonymize data/private/connections.csv -o data/private/anonymized.json
   ```
4. Only the anonymized output is safe to inspect and share outside your own
   machine, and even then, treat it as sensitive by default (see
   [`docs/privacy.md`](../docs/privacy.md)).

### Deleting everything

All state under `data/private/` is plain local files; there is no hidden
copy anywhere else. To remove everything, delete the contents of the folder:

```bash
rm -rf data/private/*
```

(`data/private/.gitkeep` will need to stay, or be recreated, if you want the
folder to keep showing up in a fresh checkout — it is otherwise not
required for the tool to work.)
