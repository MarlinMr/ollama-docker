"""Protocol tests against the real nginx container; no model downloads needed."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RegistryTests(unittest.TestCase):
    @classmethod
    def compose(cls, *args):
        return subprocess.check_output(
            ["docker", "compose", "-p", cls.project, "-f", str(ROOT / "compose.yaml"), *args],
            env=cls.env, text=True,
        )

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="ollama-registry-")
        cls.project = f"ollama-registry-test-{os.getpid()}"
        cls.env = dict(os.environ, MODEL_STORAGE=cls.temp.name,
                       REGISTRY_BIND="127.0.0.1", REGISTRY_PORT="0")
        cls.addClassCleanup(cls.temp.cleanup)
        if os.environ.get("RUN_OLLAMA_INTEGRATION") == "1":
            # The admin container writes as root. Restore ownership only within
            # this test's freshly created directory before Python removes it.
            cls.addClassCleanup(lambda: subprocess.run(
                ["docker", "run", "--rm", "--network", "none", "--user", "0",
                 "--volume", f"{cls.temp.name}:/models", "--entrypoint", "chown",
                 "nginx:1.28.0-alpine", "-R", f"{os.getuid()}:{os.getgid()}", "/models"],
                check=True, timeout=30,
            ))
        cls.addClassCleanup(lambda: cls.compose("--profile", "admin", "down", "--volumes"))
        # TemporaryDirectory is private by default; nginx workers need read access.
        Path(cls.temp.name).chmod(0o755)
        cls.data = bytes(range(256)) * 4096
        cls.digest = hashlib.sha256(cls.data).hexdigest()
        blobs = Path(cls.temp.name) / "blobs"
        blobs.mkdir()
        (blobs / f"sha256-{cls.digest}").write_bytes(cls.data)
        manifests = Path(cls.temp.name) / "manifests/registry.ollama.ai/library/test"
        manifests.mkdir(parents=True)
        cls.manifest = json.dumps({"schemaVersion": 2, "layers": []}).encode()
        (manifests / "latest").write_bytes(cls.manifest)
        cls.compose("up", "-d", "registry")
        address = cls.compose("port", "registry", "8080").strip()
        cls.base = f"http://{address}"
        cls.blob = f"/v2/library/test/blobs/sha256:{cls.digest}"
        for _ in range(50):
            try:
                with urllib.request.urlopen(cls.base + "/v2/", timeout=2):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError(f"registry did not become ready at {cls.base}\n"
                               + cls.compose("logs", "registry"))

    def request(self, path, method="GET", headers=None, redirects=True):
        opener = urllib.request.build_opener() if redirects else urllib.request.build_opener(NoRedirect)
        request = urllib.request.Request(self.base + path, method=method, headers=headers or {})
        try:
            return opener.open(request, timeout=5)
        except urllib.error.HTTPError as response:
            return response

    def test_manifest(self):
        with self.request("/v2/library/test/manifests/latest") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), self.manifest)
            self.assertIn("manifest.v2+json", response.headers["Content-Type"])

    def test_missing_and_private_paths(self):
        for path in ("/v2/library/missing/manifests/latest", "/", "/models/", "/v2/_catalog",
                     "/v2/library/test/blobs/sha256:" + "0" * 64):
            with self.subTest(path=path), self.request(path) as response:
                self.assertEqual(response.status, 404)

    def test_head(self):
        with self.request(self.blob, method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.headers["Content-Length"]), len(self.data))
            self.assertEqual(response.read(), b"")

    def test_local_redirect_and_full_download(self):
        with self.request(self.blob, redirects=False) as response:
            self.assertEqual(response.status, 307)
            self.assertEqual(response.headers["Location"], f"/objects/sha256-{self.digest}")
        with self.request(self.blob) as response:
            self.assertEqual(response.read(), self.data)
            self.assertTrue(response.url.startswith(self.base + "/"))
            self.assertEqual(response.headers["Location"], f"/objects/sha256-{self.digest}")

    def test_resumed_download(self):
        split = 123456
        with self.request(self.blob, headers={"Range": f"bytes=0-{split - 1}"}) as response:
            self.assertEqual(response.status, 206)
            first = response.read()
        with self.request(self.blob, headers={"Range": f"bytes={split}-"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"],
                             f"bytes {split}-{len(self.data) - 1}/{len(self.data)}")
            self.assertEqual(first + response.read(), self.data)

    def test_invalid_range(self):
        with self.request(self.blob, headers={"Range": f"bytes={len(self.data)}-"}) as response:
            self.assertEqual(response.status, 416)

    def test_manifest_head(self):
        with self.request("/v2/library/test/manifests/latest", method="HEAD") as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(int(response.headers["Content-Length"]), len(self.manifest))
            self.assertEqual(response.read(), b"")

    def test_writes_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/v2/", self.blob, f"/objects/sha256-{self.digest}",
                         "/v2/library/test/manifests/latest"):
                with self.subTest(method=method, path=path), self.request(path, method) as response:
                    self.assertEqual(response.status, 405)

    @unittest.skipUnless(os.environ.get("RUN_OLLAMA_INTEGRATION") == "1",
                         "set RUN_OLLAMA_INTEGRATION=1 to download a real test model")
    def test_real_ollama_pull_without_internet(self):
        # This opt-in test downloads ~46 MB plus the pinned Ollama image.
        self.compose("--profile", "admin", "up", "-d", "--wait", "admin")
        # Capture progress output rather than printing terminal control codes.
        subprocess.check_output(
            ["docker", "compose", "-p", self.project, "-f", str(ROOT / "compose.yaml"),
             "exec", "-T", "admin", "ollama", "pull", "all-minilm:22m"],
            env=self.env, stderr=subprocess.STDOUT, timeout=180,
        )
        self.compose("stop", "admin")
        config = json.loads(self.compose("--profile", "admin", "config", "--format", "json"))
        image = config["services"]["admin"]["image"]
        network = self.project + "-offline"
        client = self.project + "-client"
        registry = self.compose("ps", "-q", "registry").strip()

        def docker(*args, check=True, timeout=120):
            return subprocess.run(["docker", *args], check=check, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=timeout)

        docker("network", "create", "--internal", network)
        try:
            docker("network", "connect", "--alias", "registry", network, registry)
            docker("run", "--detach", "--name", client, "--network", network,
                   "--tmpfs", "/root/.ollama", "--tmpfs", "/models",
                   "--env", "OLLAMA_MODELS=/models", "--env", "OLLAMA_NO_CLOUD=1",
                   image, "serve")
            for _ in range(50):
                if docker("exec", client, "ollama", "list", check=False).returncode == 0:
                    break
                time.sleep(0.2)
            else:
                self.fail("isolated Ollama server did not become ready")
            model = "registry:8080/library/all-minilm:22m"
            result = docker("exec", client, "ollama", "pull", model, "--insecure")
            self.assertEqual(result.returncode, 0, result.stdout)
            listed = docker("exec", client, "ollama", "list").stdout
            self.assertIn(model, listed)
            missing = docker("exec", client, "ollama", "pull",
                             "registry:8080/library/absent:latest", "--insecure", check=False)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("not exist", missing.stdout.lower())
            docker("exec", client, "ollama", "rm", model)
            self.assertNotIn(model, docker("exec", client, "ollama", "list").stdout)
            # A client removal must not remove the central copy.
            with self.request("/v2/library/all-minilm/manifests/22m") as response:
                self.assertEqual(response.status, 200)
        finally:
            docker("rm", "-f", client, check=False)
            docker("network", "disconnect", network, registry, check=False)
            docker("network", "rm", network, check=False)


if __name__ == "__main__":
    unittest.main()
