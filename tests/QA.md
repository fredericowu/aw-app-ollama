# Release verification

Record outputs/screenshots on the Kanban card. A checkbox here is a procedure,
not a claim that a test has passed. QA runs only against merged/deployed work.

1. After schema-validation prerequisite merges, install through marketplace:
   `aw-workspace-cli marketplace install ollama`. Check declared/granted
   permissions, sidecars and primary. Record baseline doctor problems first;
   `aw-workspace-cli doctor` must show the new Ollama check healthy.
2. Verify anonymous pulls of both pinned images. Compare actual primary
   container image ID against the installed resolved image ID. A version label
   alone is insufficient (known marketplace update false-green).
3. CPU-only: first open shows branded WebUI login/signup. Create admin; do not
   reuse/migrate old unmanaged UI state. Verify neither volume contains model
   downloads until explicit pull. Pull `smollm2:135m` in Admin Settings → Models;
   capture progress. Send a prompt; capture visible streamed reply and network
   WebSocket/SSE success. `/workspace/status` must report CPU during inference.
4. Select that model in Workspace Settings; open a new browser session/chat and
   confirm it is the default. Existing chat model selections remain usable.
   Clear default; WebUI global default applies again. Restart and confirm
   workspace configuration, WebUI account/chat, model blobs/manifests persist.
5. Record blob hashes and image ID. Perform marketplace update and container
   recreation; verify actual resolved image ID, unchanged blobs, saved chat and
   a second successful prompt, with no model pull request/re-download.
6. Try invalid model names (`x;touch /tmp/pwn`, `../x`), queue 999999,
   parallel 0, invalid boolean/duration/enum and unknown command settings through
   POST `/api/apps/ollama/config`. After prerequisite 1: expect 4xx **before**
   config persistence/runtime replacement; old service/config remain healthy.
   Quantized KV requires flash attention; use f16 for CPU baseline.
7. Pull a deliberately nonexistent model in WebUI: error is visible; cancel a
   real pull, retry, and recover without reinstall. Stop WebUI sidecar: aggregate
   health fails while Ollama stays identifiable; restart and verify recovery.
   Stop/kill Ollama child: supervisor exits, normal app restart restores health.
8. Uninstall/reinstall without purge: accounts, chat, config and model files
   survive. No new pull occurs before the second inference.
9. No-GPU/unsupported host: optional grant does not fail install; requested auto
   is visible, idle before load and CPU during inference. After prerequisite 2,
   on supported NVIDIA host, grant GPU and verify actual `size_vram > 0` during
   inference. Repeat forced CPU. Do not mark GPU verified on this CPU-only host.
10. Verify status has no tokens/passwords/cookies and Ollama 11434 has no host
    publish. Unauthenticated WebUI API remains protected. Large-model failures
    should give actionable OOM/load guidance; do not promise CPU performance.
