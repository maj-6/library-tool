# Library Tool cloud image processor

This directory is a deployable, provider-neutral worker for the Android
post-capture requests. Google Cloud Run Jobs are the recommended first host.
The worker keeps the camera original immutable, writes corrected artifacts to
a separate private bucket, and records a versioned result without changing the
strict Android v1 request document.

The durable flow is:

```text
Android uploads originals + capture row
  -> migration 015 snapshots valid per-photo requests as queued jobs
  -> capture status becomes processing (desktop import waits)
  -> worker claims a lease, verifies SHA-256, and processes the photo
  -> private display/OCR/thumbnail/transform artifacts are uploaded
  -> job result becomes owner-readable
  -> after all jobs are terminal, capture status returns to pending
  -> normal desktop import can download and remove the original transport
```

## What the first processor does

- Validates file type, decoded dimensions, byte/megapixel limits, and the
  immutable Android SHA-256 before processing.
- Applies EXIF orientation, page-boundary detection, padded perspective
  correction, and display-safe illumination/contrast cleanup.
- Uses the Android date preset exactly: newer books receive stronger
  normalization; older books retain more paper tone and page-edge context.
- Runs an actual cubic-sheet page-curvature pass with `page-dewarp` when text
  evidence supports it. A sparse/uncertain page falls back to the safe
  projective result and records the skipped operation.
- Produces separate color display, stronger grayscale OCR, bordered thumbnail,
  and transform-manifest artifacts. Nonlinear transforms explicitly require
  OCR in corrected-image coordinates; they are never misrepresented as a 3x3
  homography.
- Treats spine crops as a distinct role and returns no speculative crop when
  confidence is low. A later OCR stage should store `spine_title` separately
  from the published title.

`page-dewarp` is MIT-licensed and the CPU image stack is suitable for a
scale-to-zero job. UVDoc is the intended next backend for sparse title pages;
its model/runtime is deliberately not hidden inside this first small image.

## Android integration

Android polls its owner-readable `photo_processing_jobs`, validates each result
against the exact request, source revision, hashes, artifact path, and geometry
contract, and downloads corrected display JPEGs through authenticated private
Storage. It verifies MIME type, byte count, checksum, dimensions, and complete
JPEG structure before atomically promoting a separate local display revision;
the camera original remains immutable for comparison and recovery. Captures
with `processing_request: null` are not guessed at or queued.

The result schema closes two gaps that the request-only v1 document cannot:

- `derived_from` binds every result to the exact source original/display hash
  and revision.
- Artifact bucket/path/checksum/size/dimensions are separate from local Android
  basenames and from `captures.photos`.
- Geometry names both coordinate planes explicitly. The worker always processes
  the immutable original; it exposes an Android display-base homography only
  when that display hash matches the original hash. Otherwise the corrected
  artifact must be re-OCRed, avoiding a transform against the wrong revision.

## 1. Prepare Supabase

1. Apply the ordered migrations through
   `docs/cloud/migrations/015_photo_processing_jobs.sql`. For an existing
   project, `python tools/cloud_setup.py check` identifies pending migrations.
2. Create/check all buckets:

   ```powershell
   python tools/cloud_setup.py buckets --apply
   python tools/cloud_setup.py check
   ```

   This adds or repairs the private `capture-derivatives` bucket and enforces
   32 MiB/MIME restrictions on both capture buckets. Keep RLS enabled.
3. In **Project Settings -> API Keys**, create a dedicated backend secret key
   for this processor. Prefer the current `sb_secret_...` format. Never place it
   in Android, desktop settings, source control, build arguments, or logs.

The worker sends an opaque `sb_secret_...` only in the `apikey` header. It does
not incorrectly treat the modern key as a bearer JWT. Legacy service-role JWTs
remain supported during migration.

The enqueue trigger accepts at most 32 assets per capture, 64 live jobs per
owner, and 256 new jobs per owner per hour. Requests above those guardrails are
not queued; adjust the constants in migration 015 only after measuring normal
use and setting a corresponding budget alert.

## 2. Test locally

Python 3.11+ and Docker are the supported paths. Copy `.env.example` to `.env`
inside this directory and fill only your local copy.

```powershell
python -m venv services/image_processor/.venv
services/image_processor/.venv/Scripts/python -m pip install -r services/image_processor/requirements-dev.txt
services/image_processor/.venv/Scripts/python -m pip install --no-deps page-dewarp==0.3.4
services/image_processor/.venv/Scripts/python -m pip install --no-deps -e services/image_processor
services/image_processor/.venv/Scripts/python -m pytest services/image_processor/tests
```

Build the same container that will run in production:

```powershell
docker build -t whl-image-processor:local services/image_processor
docker run --rm --env-file services/image_processor/.env whl-image-processor:local python -m whl_image_processor.worker --limit 10
```

The container installs `requirements.lock`; update that lock deliberately when
upgrading the image stack. `page-dewarp` is installed without dependencies
because its GUI OpenCV requirement is intentionally supplied by the compatible
headless OpenCV wheel.

The worker is idempotent: content hashes are part of immutable output names,
and job claims use a state/attempt conditional update. A crashed lease becomes
retryable on the next run.

## 3. Deploy as a Google Cloud Run Job

These commands assume PowerShell, an existing Google Cloud project with
billing, and an authenticated `gcloud` CLI. Substitute your project URL and
names; choose a region near the Supabase project.

```powershell
$ProjectId = "your-google-project"
$Region = "us-central1"
$Repository = "whl-services"
$Image = "$Region-docker.pkg.dev/$ProjectId/$Repository/image-processor:0.1.0"
$WorkerServiceAccount = "whl-image-processor@$ProjectId.iam.gserviceaccount.com"

gcloud config set project $ProjectId
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com
gcloud artifacts repositories create $Repository --repository-format=docker --location=$Region
gcloud iam service-accounts create whl-image-processor --display-name="WHL image processor"
gcloud builds submit services/image_processor --tag $Image
```

Create a Secret Manager secret named `whl-supabase-secret` in the Cloud
Console and add the dedicated Supabase key as version 1. Using the Console here
keeps the credential out of shell history. Then authorize only the worker:

```powershell
gcloud secrets add-iam-policy-binding whl-supabase-secret --member="serviceAccount:$WorkerServiceAccount" --role="roles/secretmanager.secretAccessor"
```

Deploy one CPU worker. `MAX_ATTEMPTS` controls application attempts; the small
Cloud Run retry count is only for process/infrastructure failure.

```powershell
gcloud run jobs deploy whl-image-processor --image=$Image --region=$Region --service-account=$WorkerServiceAccount --command=python --args="-m,whl_image_processor.worker,--limit,10" --cpu=2 --memory=4Gi --task-timeout=30m --max-retries=1 --set-env-vars="SUPABASE_URL=https://your-project-ref.supabase.co,CURVATURE_BACKEND=page-dewarp,PROCESSOR_VERSION=0.1.0,LEASE_SECONDS=2100" --set-secrets="SUPABASE_SECRET_KEY=whl-supabase-secret:1"
gcloud run jobs execute whl-image-processor --region=$Region --wait
```

Verify the execution logs and confirm a test row moves through
`processing -> pending`, with a completed job and four private artifacts,
before adding a schedule.

## 4. Schedule it

Create a separate invoker identity and grant only job invocation:

```powershell
$SchedulerServiceAccount = "whl-image-scheduler@$ProjectId.iam.gserviceaccount.com"
gcloud iam service-accounts create whl-image-scheduler --display-name="WHL image scheduler"
gcloud run jobs add-iam-policy-binding whl-image-processor --region=$Region --member="serviceAccount:$SchedulerServiceAccount" --role="roles/run.invoker"
gcloud scheduler jobs create http whl-image-processor-every-5-minutes --location=$Region --schedule="*/5 * * * *" --uri="https://run.googleapis.com/v2/projects/$ProjectId/locations/$Region/jobs/whl-image-processor:run" --http-method=POST --oauth-service-account-email=$SchedulerServiceAccount
```

Cloud Scheduler starts a bounded drain every five minutes. This keeps idle-run
cost lower during prerelease use; reduce the interval only after measuring
queue latency and spend. Concurrent executions are safe because only one
conditional claim wins each row. Raise `--limit` before adding parallel tasks;
the initial settings are intentionally conservative. Keep `LEASE_SECONDS`
longer than the platform task timeout so a slow but live worker cannot be
reclaimed. Terminal writes also compare the attempt number, so a stale worker
cannot overwrite a newer lease.

## Optional HTTP service

Platforms without a native job runner can start the default FastAPI command
and call `POST /v1/drain` with JSON `{"limit": 10}` and the secret header
`X-Image-Processor-Token`. Set `IMAGE_PROCESSOR_ADMIN_TOKEN` to a random value
of at least 32 characters and also use the platform's private-network/IAM
control. `/healthz` and `/readyz` contain no credentials or external data.

## Hosting alternatives

| Host | Fit | Tradeoff |
|---|---|---|
| Google Cloud Run Jobs | Recommended. Long-running CPU jobs, scale to zero, straightforward Secret Manager and Scheduler integration. | GPU quota/regions are constrained; add an L4 profile only if a learned dewarper needs it. |
| Azure Container Apps Jobs | Closest equivalent; event-driven KEDA jobs and serverless T4/A100 profiles are available. | More environment/workload-profile setup and GPU quota approval. |
| AWS Batch on Fargate | Strong if the project already uses AWS; queueing/retries are managed and CPU/memory ranges are broad. | More IAM/ECR/Batch wiring; Fargate has no GPU support. |
| Render Workflows | Lowest-ceremony CPU prototype with managed retries and scale-to-zero tasks. | Workflows are beta and no Render GPU tier is currently published. |
| AWS Lambda/App Runner | Useful only for a small submission API or thumbnails. | Their request/runtime/storage limits are a poor match for robust multi-photo dewarping. |

Primary references: [Cloud Run Jobs](https://cloud.google.com/run/docs/create-jobs),
[scheduled execution](https://docs.cloud.google.com/run/docs/execute-jobs-on-schedule),
[Cloud Run job secrets](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets),
[Azure Container Apps Jobs](https://learn.microsoft.com/azure/container-apps/jobs-get-started-cli),
[AWS Batch/Fargate](https://docs.aws.amazon.com/batch/latest/userguide/fargate.html),
and [Render Workflows](https://render.com/docs/workflows).

## Operations and safety

- Alert on jobs left `running` beyond the lease, terminal failures, growing
  queue age, checksum mismatches, and repeated page-dewarp fallbacks.
- Keep originals until normal desktop import completes. Never let a cleanup
  policy delete `captures` objects for `processing` rows.
- Add a derivative retention policy only after Android acknowledges a result.
- Validate against a dated corpus: sparse title pages, gutter curvature, dark
  bindings, foxing, marginalia, illustrations, and vertical spine lettering.
- Run corrected-image OCR before displaying bounding boxes on a nonlinear
  result. A homography may transform polygon vertices; a cubic/dense warp may
  not be approximated by moving only four corners.
