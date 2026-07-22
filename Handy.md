# Handy: Using Local MinerU with Knowhere Self-Hosted

## Purpose

Enable Knowhere self-hosted to work with a **local MinerU API** instance instead of the MinerU cloud service. This allows fully offline/private document ingestion without depending on `mineru.net`.

## What We Did

We patched Knowhere's `pdf_service.py` to add a **local mode** that uses MinerU's `/file_parse` synchronous endpoint (the one your local MinerU actually exposes) instead of the cloud-only batch APIs (`/file-urls/batch`, `/extract/task/batch`, `/extract-results/batch`).

### Files Changed

| File | Change |
|------|--------|
| `patches/pdf_service.py` | New file — copy of the container's original `pdf_service.py` with `parse_via_local()` added and `parse_via_full()` guarded by `MINERU_LOCAL_MODE` |
| `.env` | Added `MINERU_LOCAL_MODE=true` |
| `.env.defaults` | Added `MINERU_LOCAL_MODE=false` (default off) |
| `compose.yaml` | Added volume mount: `./patches/pdf_service.py` → container path (read-only) |

### How It Works

1. `parse_via_full()` checks `_MINERU_LOCAL_MODE` (read from `os.environ.get`, no config module change needed)
2. If true, delegates to `parse_via_local()` which:
   - POSTs the PDF as multipart form data to `{MINERU_URL}/file_parse` with `response_format_zip=true`
   - Extracts the ZIP, flattens the nested directory structure (`{stem}/auto/{stem}.md` → `full.md`, `{stem}/auto/images/` → `images/`)
   - Filters files the same way `download_and_extract_zip` does (keeps `.md`, `.jpg`, `.jpeg`, `.png`, `.gif`, `.json`; excludes `content_list`, `middle.json`, `model.json`)
3. Falls back to JSON response parsing if the response isn't a ZIP
4. The rest of the pipeline (heading prediction, markdown parsing, etc.) works unchanged since it just reads `output_dir/full.md`

### Why Volume Mount Instead of Rebuild

The Knowhere Docker image (`ghcr.io/ontos-ai/knowhere:latest`) is pre-built. Rebuilding requires the sibling repos (`knowhere`, `knowhere-dashboard`). A volume mount overrides just the patched file inside the container — no rebuild needed.

## Pitfalls & How We Overcame Them

### 1. DeepSeek text model used for vision tasks

**Symptom:** `LLMServiceException: unknown variant 'image_url', expected 'text'` — 4.2MB payload rejected by DeepSeek's chat API.

**Root cause:** `IMAGE_MODEL` and `IMAGE_MODEL_MAX` were set to `deepseek-v4-flash` (text-only), but Knowhere sends image content blocks to these models for PDF analysis.

**Fix:** Unset `IMAGE_MODEL`/`IMAGE_MODEL_MAX` in `.env` to fall back to defaults (`qwen3.6-flash`), and set `ALI_API_KEYS` for Qwen access. You can also point `ALI_URL` to OpenRouter or any OpenAI-compatible broker.

### 2. MinerU cloud-only batch APIs called against local MinerU

**Symptom:** `404 Not Found` on `/file-urls/batch` and `/extract/task/batch` — these endpoints only exist on MinerU's cloud service.

**Root cause:** Knowhere's `pdf_service.py` only implements the cloud MinerU flow: request upload URL → upload file → poll batch status → download ZIP. `MINERU_UPLOAD_MODE_ENABLED=false` just switches between two cloud modes (URL-based vs upload-based), neither of which works locally.

**Fix:** Added `MINERU_LOCAL_MODE` flag and `parse_via_local()` function that uses `/file_parse`.

### 3. ZIP from local MinerU has nested structure

**Symptom:** `FileNotFoundError: shard_0: full.md not found` — the pipeline expects `output_dir/full.md`, but the local MinerU ZIP extracts to `output_dir/og/auto/og.md`.

**Root cause:** Local MinerU ZIP structure is `{stem}/auto/{stem}.md` + `{stem}/auto/images/`. Knowhere expects `full.md` and `images/` at the output dir root.

**Fix:** Added `_flatten_extracted_zip()` that moves all files to the output dir root, then renames the first `.md` file to `full.md`.

### 4. `MINERU_UPLOAD_MODE_ENABLED` set back to `true`

**Symptom:** Env override not taking effect as expected.

**Root cause:** `.env.defaults` sets `MINERU_UPLOAD_MODE_ENABLED=true`. If you remove the override from `.env`, the default takes over. This is fine when using `MINERU_LOCAL_MODE` since `parse_via_full` short-circuits before reaching that code path.

## What To Do Next

- **Upgrade to newer Knowhere images:** When the upstream `knowhere` repo adds native local MinerU support, the patch can be removed. Check release notes.
- **Tune `MINERU_LOCAL_TIMEOUT`:** Default is 600s. Large PDFs may need more. Set in `.env` if needed.
- **Test with different backends:** The patch uses `backend=pipeline`. You could expose `MINERU_LOCAL_BACKEND` as an env var to switch between `pipeline`, `vlm-engine`, `hybrid-engine`, etc.
- **Handle shard concurrency:** For multi-shard PDFs, each shard hits `/file_parse` sequentially within the gevent worker. The local MinerU may not handle true parallel requests well — monitor for issues.
- **Consider upstream contribution:** The `parse_via_local` approach is clean enough to contribute back to the `knowhere` repo as a first-party feature.

## Key Takeaways

1. **Knowhere's MinerU integration is cloud-only by default.** It uses an async batch API pattern (upload → poll → download ZIP) that doesn't exist in the open-source MinerU server.
2. **The local MinerU `/file_parse` endpoint** is synchronous, returns JSON or ZIP directly, and has a different response schema than the cloud API.
3. **Volume mounting is the lightweight way to patch** a pre-built Docker image — avoids full rebuilds.
4. **Use `os.environ.get()` for new config flags** when you can't modify the `shared.core.config` settings class (it's baked into the image).
5. **Always verify the ZIP structure** of any new data source — the local MinerU ZIP has a `{stem}/auto/` nesting layer that the cloud ZIP doesn't.
6. **Vision-capable models are required** for PDF image analysis. DeepSeek's text-only models will fail when Knowhere sends `image_url` content blocks.
7. **`ALI_URL` is just an OpenAI-compatible base URL** — you can point it to OpenRouter, Together, or any compatible broker, not just DashScope.
