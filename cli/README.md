# cli/ — the `pyre` command-line tool

`pyre` is a thin, auditable wrapper around the detection workflow. It's how you get detections *from the external repo into the running engine*, and how you deploy. It shells out to first-party tools (`git`, `func`, `az`) rather than hiding magic, so you can always see what it does.

Run it as `python cli/pyre <command>` (Windows-friendly) or `./cli/pyre <command>` (Mac/Linux/Git Bash).

New to the terms (bundle, DaC, publish, hot-reload)? See the [glossary](../docs/GLOSSARY.md).

## The mental model

Detections live in an **external** Git repo (see [../config/detections.yaml](../config/detections.yaml)). The CLI moves them along this path:

```
external DaC repo ──pull──▶ ./.bundle ──build──▶ index ──publish──▶ Azure Blob ──▶ engine hot-reloads
                            (local copy)                            (pointer)      (within ~45s)
```

## Commands

| Command | What it does | When you use it |
|---|---|---|
| `pull` | Clones the external detections repo (at the pinned version in `config/detections.yaml`) into a local bundle folder `./.bundle`, stamped with the commit id. The only step that needs a Git token. | First step of any detection change; safe to re-run. |
| `validate` | Lints the bundle: every detection has the required fields and its `.py` file exists. | Before build/publish; in CI on every PR. |
| `test [id]` | Runs the Python test suite (`pytest` over `tests/`). Optional `id` filters to one detection. | Locally and in CI to catch regressions. |
| `build` | Reads every `.yml` in the bundle and writes the "LogType → detections" index the engine uses, plus the bundle version. | After `pull`, before `publish`/`deploy`. |
| `publish` | Zips the bundle, uploads it to Azure **Blob**, then flips a small pointer file — so warm engine workers **hot-reload** it within ~45s, no redeploy. Uploads the zip *before* the pointer so a worker never sees a pointer to a missing bundle. | To push detection changes live (the everyday motion). Needs `az login` + `BUNDLE_BLOB_ACCOUNT_URL`. |
| `deploy --env <env>` | Prints the ordered deploy steps (Terraform + `func publish` + `publish`). Kept explicit for auditability rather than doing hidden magic. | As a checklist when standing up an environment. |
| `enable <id> --env <env>` / `disable <id> --env <env>` | *Intended* to flip a detection on/off live via App Configuration. **Currently a stub** (no App Config resource yet) — detections honor their YAML `Enabled` flag instead. | Later, once App Config is wired. |
| `status --env <env>` | Shows which detections are live. Stub for now. | Later. |

## The everyday motion (update a detection)

```bash
python cli/pyre pull      # get the latest detections from the external repo
python cli/pyre validate  # sanity-check them
python cli/pyre build     # rebuild the routing index
python cli/pyre publish   # push to Blob -> engine hot-reloads within ~45s
```

In a wired-up CI, a `git push` to the detections repo (Azure Repos Git) triggers exactly this via [../.azure-pipelines/publish-detections.yml](../.azure-pipelines/publish-detections.yml).

## Configuration it reads

- [../config/detections.yaml](../config/detections.yaml) — which repo/branch/folder the detections come from, and where the bundle is published.
- Environment overrides: `DAC_TOKEN` (Git token for a private repo), `DAC_REPO`/`DAC_REF` (override repo/version), `BUNDLE_BLOB_ACCOUNT_URL` / `BUNDLE_LOCAL_DIR`.

## Design note

The thing you turn on and off is a **detection**, not infrastructure. Standing up or changing the Azure environment is Terraform's job ([../infra/README.md](../infra/README.md)), kept deliberately separate so a detection change never risks the cloud plumbing and vice-versa.
