# Library Tool Capture 0.5.2-alpha.6

Prerelease. `versionCode` 37.

This release repairs capture batches that could remain at **Syncing 1/N** after
using **Retry reading details**. In 0.5.2-alpha.5, one permanent processing
failure failed the shared WorkManager chain and cancelled every capture behind
it. Their durable `reprocess.pending` holds remained, so upload correctly kept
the local photos but waited forever.

Processing retries are now finite and a terminal result completes its
WorkManager unit successfully after recording the entry-local error. That lets
the rest of the serial batch run. On first launch after updating, the app also
detects pending holds whose old retry chain is terminal or missing and rebuilds
the chain, recovering batches already stranded by alpha.5 without deleting or
re-authorizing their captures.

The sync button offers **Retry sync X/Y** while a batch is waiting or retrying.
An explicit retry safely replaces the old upload continuation while preserving
the frozen target set and its completed-item accounting.

Cloud upload work now declares a connected-network constraint; LAN-only Wi-Fi
remains unconstrained so local desktop sync still works without validated
Internet. Auto mode hands cloud work to a newly constrained continuation after
the destination is frozen. WorkManager is updated to 2.11.2 for its Android
15/16 continuation and background-network fixes.

Finally, JPEG uploads use bounded connect, read, write, and whole-call timeouts.
If a peer accepts a connection but stops consuming the body, WorkManager can
retry or move on instead of leaving one worker blocked indefinitely.
