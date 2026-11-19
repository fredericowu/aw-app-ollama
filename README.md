# Ollama for AW Workspace

Run local models with **Ollama 0.40.16** and the unmodified, branded
**Open WebUI 0.13.4**. No model is downloaded on installation.

## Getting started

Install `ollama` from the AW marketplace after the prerequisites below land.
Open the Ollama app, create the first Open WebUI administrator account,
then use **Admin Settings → Models** to explicitly pull a model.
For CPU smoke testing choose `smollm2:135m` (check its model license before use).
Watch download progress and failures there; select it in chat and send a prompt.
Workspace → Settings → Ollama provides status and server controls. Saving
server settings recreates affected containers and interrupts active requests.
Changing a default never pulls, loads, or deletes a model.

## Controls and ownership

| Control | Owner |
| --- | --- |
| Chat, streaming, history, model picker | Open WebUI |
| Pull progress/cancel/failure, update, delete, create, unload | Open WebUI Admin Settings → Models |
| Temperature, top-p, system prompts, tools, model inference options | Open WebUI |
| Model visibility, user roles, account credentials | Open WebUI |
| Workspace default model, installed/loaded status | Workspace Settings |
| Fixed `ollama serve` command (read-only) | Workspace Settings |
| Context default, keep-alive, parallelism, queue, loaded model limit | Workspace Settings |
| Load timeout, flash attention, KV cache, cloud-disable | Workspace Settings |
| Requested accelerator and actual offload evidence | Workspace Settings |
| Resource limits and granting host devices | Workspace host/framework |

The gateway adapts the authenticated `/api/config` response's `default_models`
field when a workspace default is selected. It does not disable WebUI persistent
configuration or change the upstream image, branding, credentials, or user/chat
preferences. Clearing the workspace default restores WebUI's own global default.
The selected model must already be pulled and accessible to the WebUI user.

## Defaults and safety

* Primary: 2 CPUs / 4096 MB. WebUI: 1 CPU / 2048 MB. Allocate at least 6 GB
  plus workspace overhead; model files and upstream images need additional disk.
* Context 4096, one parallel request, one loaded model, queue 32, keep-alive 5m,
  load timeout 5m, f16 KV cache, flash attention off, Ollama cloud disabled.
  Per-request WebUI overrides can increase memory requirements. Large models
  may exceed the memory limit; choose a smaller model or change host resources.
* Auto accelerator is optional: no GPU grant never blocks installation.
  CPU mode masks CUDA/ROCm/Vulkan device selection. CPU is the supported
  workspace configuration for this release. Workspace GPU grants expose DRM
  render nodes, `/dev/kfd`, and `keep-groups`. Ollama's NVIDIA Docker path
  requires the NVIDIA Container Toolkit and `--gpus` device injection.
  AMD/DRM offload with the pinned image is unverified on this workspace.
  See the [Ollama Docker documentation](https://docs.ollama.com/docker).
* Status derives effective offload from Ollama `/api/ps` `size_vram`:
  GPU means at least one loaded model has offloaded memory (possibly partial).
  Idle/unknown never means GPU success or CPU fallback. Observe during inference.
* All runtime fields have bounds/enums/patterns and are validated again before
  launching the child. No custom executable, argv, shell command, or extra
  environment-variable surface is accepted. Quantized KV cache needs flash
  attention. Keep-alive is 0 or 1–99 seconds/minutes; load timeout is 1/5/10/15m.
* No host port is published, including unauthenticated Ollama port 11434.
  Only Open WebUI is reverse-proxied. WebUI auth stays enabled behind the
  workspace identity gate. First signup owns admin; later signups require
  the WebUI administrator's normal account approvals.
* This app requires no provider secret. WebUI credentials and its generated
  session secret stay in the UI data volume. Status returns no passwords,
  session headers, environment dumps, or credential-bearing upstream errors.
  Browser access is workspace-gated; the workspace network remains trusted.

## Persistence and lifecycle

`$AW_APP_DATA/ollama` → `/var/lib/ollama/.ollama` stores model blobs/manifests/keys.
`$AW_APP_DATA/open-webui` → `/app/backend/data` stores chats, accounts, and UI
configuration. Both resolve under `$AW_WORKSPACE_HOME/data/ollama`; neither is
inside the disposable package directory. Framework config is also durable.
Restart, update and normal uninstall/reinstall retain these directories.
Explicit data purge is destructive. This app never adopts an older unmanaged
WebUI container or its volumes. Migration requires a separate request.

The Python gateway supervises fixed argv `ollama serve`, exits on child death,
and terminates the child on container shutdown. Docker health and app doctor
both require Ollama `/api/version` and WebUI `/ready` (startup + DB readiness).
Model inventory failures are separately visible and do not claim installed state.

## Delivery and prerequisites

App releases are manually versioned through PRs. Set the manifest `version`
and `runtime.image` tag to the new version in the app PR. Check that the Git
tag, GHCR image tag, and catalog version are available before merging a release
PR. Use `[skip release]` in the merge commit for changes that keep the published
image tag. Reserve manual build dispatches for an unpublished version.

1. Merge through the required tests and code review. The post-merge
   `release.yml` builds the manifest's image for Linux amd64 and arm64.
2. Confirm the build succeeded for the exact merged commit. Record the image
   index digest and verify both platforms and an anonymous pull. A first GHCR
   publication may require a package owner to make it public.
3. Push the app Git tag `v<version>` at that exact commit. Confirm the remote
   tag resolves to the commit and its manifest points to the verified image.
   Stop if an existing version, Git ref, or image points to different content,
   or its provenance cannot be verified. Keep published tags intact.
4. From a marketplace branch, use its official sync script with the manifest
   from the merged app commit, then validate and open an `apps.json`-only PR.

   ```sh
   python scripts/sync_catalog_entry.py /path/to/merged/aw-app.json apps.json "v${VERSION}" \
     --repo tekflox/aw-app-ollama --write
   python tests/validate_apps.py
   ```

5. After the marketplace PR passes review and merges, verify the catalog
   version and tag resolve to the same manifest and public image digest.
   Before installing, verify pre-save config validation on the deployed
   workspace. Dispatch QA only after merge and deployment.

Two architecture prerequisites are pending:

1. Server-side schema validation before config persistence/recreation:
   card `41b5bf3b-9510-810f-84d5-d358186def1e`. The app's defense-in-depth
   validator prevents unsafe child startup, but cannot preserve the old runtime
   when an older framework has already saved invalid config. **Do not claim
   install-ready until this lands.**
2. End-to-end optional GPU host grants:
   card `41b5bf3b-9510-815a-9594-d88895152865`. CPU-first is supported after
   prerequisite 1; supported GPU delivery cannot be claimed until 2 lands.

## Verification

```sh
pip install -r requirements-dev.txt
python -m pytest tests/ -q
ruff check .
python ../aw-marketplace/scripts/validate_manifest.py aw-app.json
docker build -t aw-ollama-test container
```

See [the QA procedure](tests/QA.md) for marketplace, actual UI inference,
persistence/update, invalid config, download failures, and GPU coverage.

## Upstream references and licensing

* [Pinned Open WebUI model management](https://github.com/open-webui/open-webui/blob/v0.13.4/src/lib/components/admin/Settings/Models.svelte)
* [WebUI config](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/config.py)
* [WebUI readiness/default response](https://github.com/open-webui/open-webui/blob/v0.13.4/backend/open_webui/main.py)
* [Ollama environment controls](https://github.com/ollama/ollama/blob/v0.40.16/envconfig/config.go)
* [Open WebUI license](https://github.com/open-webui/open-webui/blob/v0.13.4/LICENSE)
  requires preserving branding; the bundled upstream UI is unmodified.
* [Ollama MIT license](https://github.com/ollama/ollama/blob/v0.40.16/LICENSE).
  Model licenses are separate. This app does not preselect or distribute models.
