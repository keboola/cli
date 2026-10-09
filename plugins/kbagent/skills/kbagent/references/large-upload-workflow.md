# Large File Upload Workflow

Loading one very large CSV (100+ GB, optionally `.csv.gz`) into ONE Storage
table. Everything here needs the version that added S3 multipart upload,
`--no-wait` and `storage job-detail` *(since vNEXT, #834)* -- check
`kbagent version` first. On older versions an AWS upload stops at 5 GiB and
there is no way to follow the import job.

A table upload is two phases:

1. **Cloud upload** -- kbagent sends the file to the stack's file storage.
   On AWS stacks a file above 64 MiB goes up as an S3 multipart upload
   (64 MiB parts, scaled up to fit 10,000 parts, 4 in parallel, per-part
   retry; peak memory ~256 MiB regardless of file size).
2. **Import job** -- Storage imports the uploaded file into the table
   asynchronously. For a big file this takes long; follow it by job id.

## Quick reference

| Command | Purpose |
|---------|---------|
| `storage upload-table --no-wait --progress` | Upload the file (progress lines on stderr), enqueue the import, return `file_id` + `job_id` |
| `storage job-detail --job-id ID [--wait]` | Show / wait for the import job |
| `storage load-file --file-id ID` | Re-import an already-uploaded file (no re-upload) |
| `storage file-detail --file-id ID` | Check the uploaded file still exists |
| `storage table-detail --table-id ID` | Verify row count after the import |

## 1. Prepare

**Compress it.** Upload time dominates. A `.csv.gz` is uploaded byte-for-byte
and Storage imports it directly, so gzip usually cuts the upload several
times over:

```bash
gzip -k big.csv          # -> big.csv.gz, keeps the original
```

**Pick the target table.**

- *Auto-create* (default): if the table does not exist, kbagent creates the
  bucket and table from the CSV header (read through gzip for `.csv.gz`).
  Every column is an untyped STRING.
- *Pre-created typed table*: create it first with `storage create-table`
  (see `storage-types-workflow.md`), then upload with `--no-auto-create` so a
  typo in `--table-id` fails instead of creating a second table.

**Pick full or incremental.**

- Full load (default) replaces the table content.
- `--incremental` appends. Running an incremental import twice duplicates
  every row -- that is why step 4 matters.

**Check the throughput budget (AWS).** The S3 credentials for the upload last
**12 hours**; the whole upload must finish inside that window.

| File size (as uploaded) | Minimum sustained throughput |
|---|---|
| 50 GB | ~1.2 MB/s |
| 100 GB | ~2.3 MB/s |
| 200 GB | ~4.6 MB/s |

Measured upload speed below that -> compress harder, or upload from a host
closer to the stack (for example a VM in the same cloud region).

**Do not touch the source file during the upload.** kbagent checks its size
and mtime; a file that changes mid-upload fails the upload. Do not upload a
file that another process is still writing.

## 2. Upload and enqueue the import

```bash
kbagent --json storage upload-table \
  --project ALIAS \
  --table-id in.c-raw.events \
  --file ./big.csv.gz \
  --no-wait --progress > upload.json 2> progress.log

JOB_ID=$(jq -r '.data.job_id' upload.json)
FILE_ID=$(jq -r '.data.file_id' upload.json)
```

- `--no-wait` still waits for the **cloud upload** (that is the long part on a
  slow link); it only skips waiting for the import.
- The result carries `file_id`, `job_id`, `job_status`; `imported_rows` is
  `null` while the job is pending.
- **Save `job_id` and `file_id` before anything else.** They are the only
  handles for following and recovering the import.
- In human mode (no `--json`) a progress bar on stderr shows the upload, and
  the command prints the job id plus the follow-up command.
- **Use `--progress` for long runs** *(since vNEXT)*. It reports progress on
  stderr even with `--json` and without a terminal, so stdout stays clean JSON
  and `progress.log` gets one line every 10 s plus a final line:

  ```
  upload big.csv.gz: 42.0% 4.20/10.00 GiB, 67.30 MiB/s, elapsed 0:01:03, ETA 0:01:27
  ```

  Speed and ETA are measured over the last 30 s, so they follow a change in
  link speed; the final line gives the overall average and ends in `done` or
  `failed`. Check it with `tail -n 1 progress.log` instead of guessing whether
  a silent upload is still alive. Compare the ETA with the 12-hour credential
  window from step 1. The same flag works on `file-upload`, `download-table`,
  `file-download` and `unload-table --download`.

Run it in a background shell or `nohup` if the upload outlasts your session:
a foreground agent shell with a ~2-minute timeout kills a multi-hour upload.

## 3. Follow the import job

```bash
# Block until the job finishes (or the budget runs out)
kbagent --json storage job-detail --project ALIAS --job-id "$JOB_ID" \
  --wait --timeout 3600

# Or a single non-blocking look, e.g. in a polling loop
kbagent --json storage job-detail --project ALIAS --job-id "$JOB_ID"
```

| `status` | Meaning | Exit code | Next step |
|---|---|---|---|
| `waiting` | Queued, not started | 0 | Poll again later |
| `processing` | Import running | 0 | Poll again later |
| `success` | Imported | 0 | Read `imported_rows`, verify with `table-detail` |
| `error` | Import failed | 1 (`STORAGE_JOB_FAILED`) | Read `error`, fix, go to step 5 |

A `job-detail --wait` timeout exits **4** (`STORAGE_JOB_TIMEOUT`,
`retryable: true` -- repeating a read is safe). It means only that *kbagent
stopped waiting* -- the job keeps running server-side. Run `job-detail --wait`
again.

Job ids are project-scoped: `job-detail` takes no `--branch` and ignores the
active branch.

The same pattern works with `upload-table` without `--no-wait`: the default
`--wait` waits up to `--timeout` (default 600 s). When it times out, the error
names `job_id` and `file_id` -- continue with this step.

## 4. What NOT to do

- **Never re-run `upload-table` while the import job is `waiting` or
  `processing`.** Exit 4 on a timeout does not mean the import failed. A
  second run uploads the file again AND imports it again: with
  `--incremental` every row lands twice.
- Do not treat an `upload-table` / `load-file` `STORAGE_JOB_TIMEOUT` as
  retryable even though it exits 4 -- that payload says `retryable: false` for
  this reason. (Only the read-only `job-detail --wait` timeout is retryable.)
- Do not delete or overwrite the local file until the job reports `success`
  -- it is your fallback if the uploaded file expires.

## 5. Recover from a failed import

The upload already succeeded, so import the uploaded file again instead of
uploading 100+ GB a second time:

```bash
kbagent --json storage load-file \
  --project ALIAS \
  --file-id "$FILE_ID" \
  --table-id in.c-raw.events \
  --no-wait
```

- Fix the cause first (read `error` from `job-detail`: wrong delimiter ->
  `--delimiter`, column mismatch against a pre-created table, ...).
- If **enqueueing** the import failed right after the upload, the
  `upload-table` error itself carries `file_id` -- use it the same way.
- The uploaded file is a normal Storage File: it expires after **15 days**
  (it is not permanent). After that, `storage file-detail --file-id` fails and
  you must upload again.
- A failed **upload** (network drop, expired credentials) cannot be resumed:
  the next run starts from the first byte. An aborted multipart upload may
  leave orphaned parts the Storage token cannot remove.

## 6. Verify

```bash
kbagent --json storage table-detail --project ALIAS --table-id in.c-raw.events
```

Compare `rows_count` with the source line count (minus the header) and with
`imported_rows` from `job-detail`.

## Platform notes

- **AWS stacks**: everything above applies (multipart, 12 h credential
  window, no 5 GiB ceiling).
- **Azure and GCP stacks**: the upload path is unchanged -- those stacks
  already streamed the file, without the multipart numbers above (a chunked
  upload there is a follow-up in #834). `--no-wait`, `job-detail` and
  `load-file --file-id` recovery work the same on every stack.
- **Memory**: the CLI needs ~256 MiB for the upload (4 parts x 64 MiB; more
  only when parts are scaled up for files above ~640 GB).
- **`kbagent serve`**: the upload route streams the request body to a temp
  file instead of holding it in memory, so the serve host needs free temp
  disk at least the size of the file. Over serve, the upload route accepts
  `wait` / `timeout` and the job is read with
  `GET /storage/jobs/{project}/{job_id}?wait=&timeout=`.
- **SDK**: `Client.upload_table(..., wait=False)` returns `file_id`,
  `job_id`, `job_status` the same way, and `Client.storage_job(job_id,
  wait=..., timeout=...)` returns a typed `StorageJobResult` (see `docs/sdk.md`).
