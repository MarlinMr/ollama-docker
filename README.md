# Offline Ollama model registry

An admin-populated, read-only model library for offline Ollama servers. The
internet-connected storage machine downloads models once; clients pull only the
models they need. There is no upstream proxy or download-on-demand endpoint.

## Storage machine

Requires Docker with Compose. Copy `.env.example` to `.env`, set `MODEL_STORAGE`
to a directory on the large disk, and set `REGISTRY_BIND` to the machine's LAN IP.
The default bind address is localhost for safe testing.

```sh
docker compose up -d registry
docker compose --profile admin up -d --wait admin
docker compose exec admin ollama pull smollm2:135m
docker compose stop admin
```

Repeat the last three commands to add models. Substitute any downloadable model
from the public Ollama library, including `namespace/model:tag` names. Admin
access means shell/Docker access on this machine; there is no public admin API.
The admin Ollama API binds only to loopback **inside** its container and has no
published port. It does not need a GPU.

Downloaded `blobs` and `manifests` are served directly from Ollama's storage
layout: no second copy, repacking, or `ollama push` step is needed. The registry
mounts this directory read-only and has no upstream proxy configuration.
Failed/missing pulls return 404 rather than fetching upstream. For enforced
egress isolation, also block registry container egress in your network firewall;
Docker internal-only networks disable the port publishing needed by LAN clients.

## Offline Ollama server

Replace `models.internal` with the storage machine's reachable hostname or IP:

```sh
ollama pull models.internal:5000/library/smollm2:135m --insecure
ollama run models.internal:5000/library/smollm2:135m
ollama rm models.internal:5000/library/smollm2:135m
```

`--insecure` permits this HTTP registry. Use only on a trusted isolated LAN, and
restrict the published port to approved clients with your network firewall
(Docker-published ports may bypass ordinary host firewall rules). For untrusted
networks, add TLS and access controls before deployment. Anyone who can reach
the endpoint can read all stored models; it is not a multi-tenant registry.

If Ollama itself runs in Docker, its **server container** must be able to resolve
and reach `models.internal`; the CLI machine's connectivity is not enough.
Use fully qualified model names: plain `ollama pull smollm2:135m` still targets
the public registry. The client needs space for its selected models, not for
the full library or a separate archive. Leave some free space for downloads.

### Optional: keep Ollama's default registry address

To use plain `ollama pull smollm2:135m` without an internal hostname or
`--insecure`, you can override `registry.ollama.ai` within the offline network.
**This is not enabled by the supplied configuration.** It requires:

1. Internal DNS resolving `registry.ollama.ai` to the storage machine for the
   offline Ollama servers, including their containers if applicable.
2. A certificate with `registry.ollama.ai` in its subject alternative names,
   signed by your internal CA. You cannot obtain a publicly trusted certificate
   for Ollama's domain.
3. That CA installed in the trust store used by the **Ollama daemon**, not just
   the CLI machine. For containerized Ollama, install it inside the container
   and restart the daemon after updating trust.
4. Nginx serving HTTPS, with the existing model-serving locations unchanged.
   Replace `listen 8080;` and `server_name _;` in `nginx.conf` with:

   ```nginx
   listen 8443 ssl;
   server_name registry.ollama.ai;
   ssl_certificate /certs/registry.crt;
   ssl_certificate_key /certs/registry.key;
   ```

   In the registry service in `compose.yaml`, replace the port mapping with:

   ```yaml
   ports:
     - "${REGISTRY_BIND:-127.0.0.1}:443:8443"
   ```

   Add `./certs:/certs:ro` to its existing `volumes` list. Supply the certificate
   chain and private key at those paths, protect the key, and set
   `REGISTRY_BIND` to the storage machine's LAN IP. Recreate the registry after
   making these changes. `REGISTRY_PORT` is no longer used by this mapping.

Keep the admin downloader's DNS pointed at the **real public registry**, not
the internal override, so admins can still download new models. Use split DNS
or a client-only override rather than changing resolution for the entire
storage machine. Missing models still fail locally; this does not enable an
upstream proxy. This optional DNS/TLS setup is not covered by the automated tests.

## Maintenance and limitations

- Ollama is pinned to `0.40.12`, the current stable release at setup time. Test
  compatibility again when upgrading the client or admin image.
- The endpoint supports manifest/tag reads, blobs, HEAD, and ranged downloads.
  It is not a general OCI registry: no push, catalog, tag listing, or deletion API.
- Only locally downloaded models work offline, not cloud-only models.
- `docker compose exec admin ollama list` lists the central library while admin
  is running. Do not run `ollama rm` on the admin container unless you intend to
  remove a model centrally. Avoid central deletion while clients are pulling.
- Back up `MODEL_STORAGE` (both manifests and blobs). `docker compose down`
  does not delete this bind-mounted data.
- The registry advertises only local relative redirects; model payloads never
  redirect to public object storage. Unknown routes and writes are rejected.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests start a separate registry with synthetic model data and no upstream
configuration. They check manifests, missing models, local redirects, ranges/resume,
and rejected writes, without downloading a model. Docker must be available.

For a real end-to-end pull using the pinned Ollama image:

```sh
RUN_OLLAMA_INTEGRATION=1 python3 -m unittest discover -s tests -v
```

This additionally downloads `all-minilm:22m` (~46 MB) into temporary storage,
then pulls it into a fresh Ollama container on an internal-only network with no
internet access. It also checks a missing model, client deletion, and central
copy retention. Test containers, volumes, networks, and model files are cleaned
up afterward; Docker images stay cached. No running host Ollama instance is used.
