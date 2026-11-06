# Switch-over and rollback

Known-good version: **v0.1** (single photo, Telegram bot v1). Git branch `stable/v0.1`.

## What v2 adds (all behind switches)

| Railway shared variable | Effect when `true` |
|---|---|
| `FEATURE_SETS` | Albums of up to 6 photos studio-edited as one set |
| `FEATURE_NOVEL_ANGLES` | "Create extra angles" mode (AI visualizations from 1–3 photos) |
| `FEATURE_PLATE_BLUR` | Optional number-plate blur |
| `FEATURE_OVERLAY_CLEANUP` | Removes screenshot buttons / timestamps / stickers |
| `WATERMARK_POLICY` | `ask_original` (default): refuse photos with another site's watermark. `remove`: erase it (currently set for private testing; switch back before any public use) |
| `COMPOSITE_FALLBACK` | When every AI attempt fails QA, deliver the pixel-preserving local composite instead of failing |

## Switch to v2

1. Railway → carstudio-ai → Settings → Shared Variables:
   - `FEATURE_SETS`, `FEATURE_NOVEL_ANGLES`, `FEATURE_PLATE_BLUR`, `FEATURE_OVERLAY_CLEANUP` = `true`
   - `N8N_CALLBACK_URL` = `https://paikhom.app.n8n.cloud/webhook/carstudio-job-events-v2`
2. n8n: **deactivate** "CarStudio · Telegram bot", **activate** "CarStudio · Telegram bot v2".
   (Only one workflow can own the Telegram bot at a time.)

## Roll back to v0.1 (≈2 minutes, no code change)

1. n8n: **deactivate** "CarStudio · Telegram bot v2", **activate** "CarStudio · Telegram bot".
2. Railway shared variables: set the four `FEATURE_*` to `false` and
   `N8N_CALLBACK_URL` = `https://paikhom.app.n8n.cloud/webhook/carstudio-job-events`.

Partial rollback is fine too: e.g. turn off only `FEATURE_NOVEL_ANGLES` and the bot answers
"Generating extra angles isn't available yet" while everything else keeps working.

## Full code rollback (only if the v2 code itself misbehaves)

Railway → api / worker → Deployments → pick the deployment from commit `39c09a3` (branch
`stable/v0.1`) → **Rollback**. Database changes in `0004_sets_and_options.sql` are additive only,
so v0.1 code runs unchanged against the new schema.

## Live IDs

| Thing | v1 | v2 |
|---|---|---|
| n8n bot workflow | `x1kqhJH9BI1VADs8` | `xFEfCUjTCUwp7nO2` |
| n8n delivery workflow | `gpQZzYxDWJhSeR7A` (`/webhook/carstudio-job-events`) | `ADwUuCpuiAZPdsNP` (`/webhook/carstudio-job-events-v2`) |
