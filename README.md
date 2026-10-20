# SignalCore knowledge sync

A GitHub Action that keeps a SignalCore agent's knowledge in step with your repository. On every
push it sends the agent the docs that changed, as searchable files, and removes the ones you
deleted, so the agent answers from what your product does today.

## Set it up

1. **Make a secret key** with the `read` and `write` scopes
   ([how](https://signalcore.ai/docs/guias/api#crear-una-clave)) and save it in the repository as
   the secret `SIGNALCORE_KEY` (Settings → Secrets and variables → Actions).
2. **Say what the agent should know** in `.signalcore/knowledge.yml`:

   ```yaml
   agent: agt_...          # the agent's id
   include:                # the files it should know
     - docs/**
     - "*.md"
     - openapi.json
   exclude:                # of those, the ones it shouldn't
     - docs/drafts/**
     - CHANGELOG.md
   prefix: repo/           # where its copies go among the agent's files (repo/ by default)
   ```

3. **Add the workflow** as `.github/workflows/signalcore-knowledge.yml`:

   ```yaml
   name: SignalCore knowledge
   on:
     push:
       branches: [main]
     workflow_dispatch:
   concurrency: signalcore-knowledge
   jobs:
     sync:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: OmnistGroup/signalcore-sync-knowledge@v1
           with:
             key: ${{ secrets.SIGNALCORE_KEY }}
   ```

Push, and the first run sends every file the config takes. After that, each run sends only what
changed.

## What it does

- **Changes since the last sync.** The commit synced last is kept on the agent as
  `<prefix>.synced`. Each run compares it with the pushed commit: files added or changed are put
  (`PUT /v1/agents/{id}/files/{prefix}{path}?searchable=true`, which indexes them), removed ones
  are deleted, a rename is both. A run that failed or was skipped is caught up by the next one.
- **A full sync** when nothing was synced yet, when the config changed, when that commit is no
  longer in the repository (a force push), on a manual run (`workflow_dispatch`, or `full: true`):
  every file the config takes is put, and every copy under the prefix without its file is deleted.
- **Your files stay apart.** Copies keep their path under the prefix (`docs/refunds.md` is
  `repo/docs/refunds.md`); the agent's own files, outside it, are never touched.
- **What can't be indexed is skipped** with a warning, and an older copy of it removed: binaries
  (PDF, Word, Excel and PowerPoint are indexed), files over 50 MB, names with characters a file's
  path can't have (only letters, digits, spaces and `_ . - ( )` in each part), links.
- **Rate limits** are waited out (`Retry-After`); server errors are tried again. A file that still
  fails fails the run, and the next run tries it again.

## Globs

As in `.gitignore`: `*` and `?` match within a folder, `**` across folders. A glob without a `/`
matches a name in any folder (`*.md` is every Markdown file); one with a `/` starts at the
repository's root (`docs/**` is everything in `docs`).

## What to index

What answers your users' questions, written for people: docs, help articles, guides, the
OpenAPI spec. Raw code rarely does; index it only when it is what users ask about (an SDK's
examples), and keep tests, fixtures, lock files and generated files out.

## Inputs

| Input | Default | |
|---|---|---|
| `key` | | A secret key (`sk_…`) with `read` and `write`. Required. |
| `config` | `.signalcore/knowledge.yml` | The config's path. |
| `full` | `false` | `true` syncs every file, not only what changed. |
| `org` | | The organization to act on, when the key's organization manages it (an agency's client). |
| `api` | `https://signalcore.ai/v1` | The API's address. |

The action brings its own Python (`actions/setup-python`, off the job's `PATH`) and needs nothing
else on the runner: Linux and macOS runners.

Changing `prefix` leaves the copies under the old one: delete them, or keep the prefix.
