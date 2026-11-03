"""Isolated real-TLS container smoke; no model calls, deployment, or user data.

Run only after the tested application image has been built. Every Docker resource
is privately named and labelled; cleanup only removes resources carrying this
run's label. Credentials and the temporary TLS private key never enter evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

PROXY = "caddy:2.11.7-alpine@sha256:24ec7b05bc532c7bb57f727030c5056e257ff7138b4b50460306819ff79362bd"
LABEL = "io.rewear.container-smoke"


class SmokeFailure(Exception):
    pass


def digest(value):
    return hashlib.sha256(value).hexdigest()


class Smoke:
    def __init__(self, image, output):
        self.image, self.output = image, output
        self.ident = secrets.token_hex(6)
        self.label = f"{LABEL}={self.ident}"
        self.prefix = f"rewear-smoke-{self.ident}"
        self.containers, self.volumes, self.networks = [], [], []
        self.private = set()
        self.report = {
            "version": 1,
            "run_id": self.ident,
            "started_at": datetime.now(UTC).isoformat(),
            "script_sha256": digest(Path(__file__).read_bytes()),
            "scope": "isolated Docker and HTTPS smoke; offline demo only; no model quality claim",
            "checks": [],
            "status": "running",
        }
        self.output.mkdir(parents=True, exist_ok=False)

    def command(self, arguments, *, timeout=60, check=True):
        try:
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise SmokeFailure(f"command timeout: {arguments[0]}") from None
        if check and result.returncode:
            # Do not expose arguments, environment, request bodies or stderr.
            raise SmokeFailure(f"command failed: {arguments[0]} (exit {result.returncode})")
        return result

    def docker(self, *arguments, **kwargs):
        return self.command(["docker", *arguments], **kwargs)

    def check(self, name, value, **details):
        self.report["checks"].append({"name": name, "passed": bool(value), **details})
        if not value:
            raise SmokeFailure(name)

    def image_info(self, reference, *, pull=False):
        result = self.docker("image", "inspect", reference, check=False)
        if result.returncode and pull:
            self.docker("pull", reference, timeout=180)
            result = self.docker("image", "inspect", reference)
        if result.returncode:
            raise SmokeFailure("requested application image is not available locally")
        info = json.loads(result.stdout)[0]
        return {
            "reference": reference,
            "id": info["Id"],
            "repo_digests": info.get("RepoDigests", []),
            "architecture": info["Architecture"],
            "os": info["Os"],
            "configured_user": info["Config"].get("User"),
        }

    def volume(self, suffix):
        name = f"{self.prefix}-{suffix}"
        self.volumes.append(name)
        self.docker("volume", "create", "--label", self.label, name)
        return name

    def certificate(self, temporary):
        ca, key, cert = temporary / "ca.crt", temporary / "server.key", temporary / "server.crt"
        self.command(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=Rewear isolated smoke CA",
                "-keyout",
                str(temporary / "ca.key"),
                "-out",
                str(ca),
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ]
        )
        self.command(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-subj",
                "/CN=localhost",
                "-keyout",
                str(key),
                "-out",
                str(temporary / "server.csr"),
            ]
        )
        extensions = temporary / "server.ext"
        extensions.write_text(
            "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
            "basicConstraints=critical,CA:FALSE\n"
            "extendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        )
        self.command(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(temporary / "server.csr"),
                "-CA",
                str(ca),
                "-CAkey",
                str(temporary / "ca.key"),
                "-CAcreateserial",
                "-days",
                "1",
                "-sha256",
                "-extfile",
                str(extensions),
                "-out",
                str(cert),
            ]
        )
        self.report["tls"] = {
            "ca_sha256": digest(ca.read_bytes()),
            "certificate_sha256": digest(cert.read_bytes()),
            "san": ["localhost", "127.0.0.1"],
            "client_verifies_chain_and_hostname": True,
            "acme": False,
        }
        (self.output / "ca.crt").write_bytes(ca.read_bytes())
        return ssl.create_default_context(cafile=ca)

    def app(self, suffix, volume):
        name = f"{self.prefix}-{suffix}"
        self.containers.append(name)
        self.docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            self.label,
            "--network",
            self.network,
            "--publish",
            f"127.0.0.1:{self.port}:8443",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=536870912,mode=1777",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "256",
            "--memory",
            "2g",
            "--stop-timeout",
            "45",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/rewear",
            "--env",
            f"REWEAR_PUBLIC_ORIGIN={self.origin}",
            "--env",
            "REWEAR_DATA_DIR=/var/lib/rewear/local-unused",
            "--env",
            "MODEL_API_KEY=",
            "--env",
            "REWEAR_VISION_API_KEY=",
            "--env",
            "REWEAR_IMAGE_SEARCH=disabled",
            "--env",
            "HOME=/nonexistent",
            "--env",
            "OLLAMA_URL=http://127.0.0.1:9",
            "--env",
            "META_API_URL=http://127.0.0.1:9/v1",
            self.image,
            "rewear",
            "--hosted",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--hosted-data-dir",
            "/var/lib/rewear/data",
            "--max-accounts",
            "4",
            "--max-active-tenants",
            "2",
            "--max-jobs-per-tenant",
            "2",
            "--max-media-mib-per-tenant",
            "64",
        )
        info = json.loads(self.docker("inspect", name).stdout)[0]
        host, config = info["HostConfig"], info["Config"]
        self.check(f"{suffix}: image default is nonroot", config["User"] in {"10001", "10001:10001"})
        self.check(f"{suffix}: readonly root", host["ReadonlyRootfs"] is True)
        self.check(f"{suffix}: capabilities dropped", "ALL" in host["CapDrop"])
        self.check(
            f"{suffix}: only loopback published",
            list(host["PortBindings"]) == ["8443/tcp"]
            and all(binding["HostIp"] == "127.0.0.1" for binding in host["PortBindings"]["8443/tcp"]),
        )
        bindings = info["NetworkSettings"]["Ports"].get("8443/tcp") or []
        self.check(
            f"{suffix}: actual host mapping",
            bindings == [{"HostIp": "127.0.0.1", "HostPort": str(self.port)}],
        )
        probe = self.docker(
            "exec",
            name,
            "python",
            "-c",
            "import json,os,pathlib; from wardrobe.config import Settings; s=Settings(); "
            "print(json.dumps({'uid':os.getuid(),'key':bool(s.api_key),"
            "'google':s.image_search_enabled,'local_data_exists':pathlib.Path(s.data_dir).exists()}))",
        )
        runtime = json.loads(probe.stdout)
        self.check(f"{suffix}: uid10001", runtime["uid"] == 10001)
        self.check(f"{suffix}: no provider credentials", not runtime["key"] and not runtime["google"])
        self.check(f"{suffix}: unused local database absent", not runtime["local_data_exists"])
        return name

    def proxy(self, app, temporary, suffix):
        name = f"{self.prefix}-{suffix}"
        self.containers.append(name)
        self.docker(
            "run",
            "-d",
            "--name",
            name,
            "--label",
            self.label,
            "--network",
            f"container:{app}",
            "--read-only",
            "--tmpfs",
            "/data:rw,nosuid,noexec,size=16777216",
            "--tmpfs",
            "/config:rw,nosuid,noexec,size=16777216",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "NET_BIND_SERVICE",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "128",
            "--memory",
            "256m",
            "--mount",
            f"type=bind,source={temporary},target=/fixture,readonly",
            PROXY,
            "caddy",
            "run",
            "--config",
            "/fixture/Caddyfile",
            "--adapter",
            "caddyfile",
        )
        info = json.loads(self.docker("inspect", name).stdout)[0]
        app_id = self.docker("inspect", "--format", "{{.Id}}", app).stdout.strip()
        self.check(
            f"{suffix}: shared network namespace", info["HostConfig"]["NetworkMode"] == f"container:{app_id}"
        )
        return name

    def client(self, context):
        return httpx.Client(base_url=self.origin, verify=context, trust_env=False, timeout=12)

    def ready(self, client):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            try:
                result = client.get("/healthz")
                if result.status_code == 200 and result.json() == {"status": "ok"}:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.25)
        raise SmokeFailure("HTTPS liveness was not ready within 35s")

    def mutation(self, identity=None):
        result = {"Origin": self.origin, "X-Rewear-Request": "1"}
        if identity is not None:
            result["X-CSRF-Token"] = identity["csrf_token"]
        return result

    def auth(self, client, name, password, *, register=False):
        response = client.post(
            "/auth/register" if register else "/auth/login",
            json={"username": name, "password": password},
            headers=self.mutation(),
        )
        self.check(f"{name}: {'register' if register else 'login'}", response.status_code == 200)
        identity = response.json()
        for key in ("csrf_token", "recovery_code"):
            if identity.get(key):
                self.private.add(identity[key])
        for cookie in client.cookies.jar:
            self.private.add(cookie.value)
        cookie = response.headers.get("set-cookie", "")
        self.check(
            f"{name}: secure cookie",
            cookie.startswith("__Host-rewear_session=")
            and all(value in cookie.lower() for value in ("secure", "httponly", "samesite=lax", "path=/"))
            and "domain=" not in cookie.lower(),
        )
        return identity

    def bound(self, response, identity, name, status=200):
        self.check(name, response.status_code == status, http_status=response.status_code)
        self.check(
            f"{name}: account/cache boundary",
            response.headers.get("x-rewear-account") == identity["user"]["id"]
            and "private" in response.headers.get("cache-control", "")
            and "no-store" in response.headers.get("cache-control", "")
            and "cookie" in response.headers.get("vary", "").lower(),
        )
        return response

    def catalogue(self, client, identity, job, media_sha, label):
        jobs = self.bound(client.get("/api/jobs"), identity, f"{label}: jobs").json()["jobs"]
        self.check(f"{label}: only own job", [entry["id"] for entry in jobs] == [job["id"]])
        media = self.bound(client.get(job["source_url"]), identity, f"{label}: source")
        self.check(f"{label}: unchanged source", digest(media.content) == media_sha)
        exported = self.bound(client.get("/api/export?format=json"), identity, f"{label}: export").json()
        self.check(
            f"{label}: own exported items",
            len(exported["items"]) == 3 and all(item["job_id"] == job["id"] for item in exported["items"]),
        )

    def logs(self, name, suffix=""):
        result = self.docker("logs", "--tail", "200", name, timeout=15, check=False)
        value = result.stdout + result.stderr
        for secret in sorted(self.private, key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        (self.output / f"{name}{suffix}.log").write_text(value)

    def remove_container(self, name):
        self.logs(name)
        self.docker("rm", "-f", name)
        self.containers.remove(name)

    def maintenance(self, name, arguments, mounts):
        self.containers.append(name)
        result = self.docker(
            "run",
            "--name",
            name,
            "--label",
            self.label,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,noexec,size=67108864",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            *mounts,
            self.image,
            "rewear",
            *arguments,
        )
        return json.loads(result.stdout)

    def run(self):
        self.report["application_image"] = self.image_info(self.image)
        # Bind every app and maintenance invocation to the inspected image,
        # even if another build changes the mutable tag while this test runs.
        self.image = self.report["application_image"]["id"]
        self.report["proxy_image"] = self.image_info(PROXY, pull=True)
        self.check(
            "pinned Caddy digest", PROXY.split("@", 1)[1] in self.report["proxy_image"]["repo_digests"][0]
        )
        self.network = f"{self.prefix}-network"
        self.networks.append(self.network)
        self.docker("network", "create", "--label", self.label, self.network)
        network = json.loads(self.docker("network", "inspect", self.network).stdout)[0]
        self.check(
            "dedicated labelled bridge",
            network["Driver"] == "bridge" and network["Labels"].get(LABEL) == self.ident,
        )
        self.report["network"] = {"dedicated_bridge": True, "external_egress_blocked": False}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.origin = f"https://127.0.0.1:{self.port}"
        self.report["origin"] = self.origin
        volume = self.volume("data")
        with tempfile.TemporaryDirectory(prefix="rewear-container-tls-") as path:
            temporary = Path(path)
            context = self.certificate(temporary)
            (temporary / "Caddyfile").write_text(
                "{\n admin off\n auto_https off\n}\n"
                ":8443 {\n tls /fixture/server.crt /fixture/server.key\n"
                " request_body {\n max_size 257MiB\n }\n reverse_proxy 127.0.0.1:8765\n}\n"
            )
            self.report["proxy_config_sha256"] = digest((temporary / "Caddyfile").read_bytes())
            (self.output / "Caddyfile").write_bytes((temporary / "Caddyfile").read_bytes())
            app = self.app("app", volume)
            proxy = self.proxy(app, temporary, "proxy")
            with self.client(context) as anon, self.client(context) as alice, self.client(context) as bob:
                self.ready(anon)
                untrusted = False
                try:
                    with self.client(ssl.create_default_context()) as unknown_ca:
                        unknown_ca.get("/healthz")
                except httpx.ConnectError:
                    untrusted = True
                self.check("untrusted TLS is rejected", untrusted)
                self.check("anonymous root redirects to login", anon.get("/").status_code == 303)
                self.check("anonymous API is denied", anon.get("/api/jobs").status_code == 401)
                self.check("anonymous export is denied", anon.get("/api/export").status_code == 401)
                password = "isolated-smoke-" + secrets.token_urlsafe(20)
                self.private.add(password)
                a = self.auth(alice, "smoke-alice", password, register=True)
                b = self.auth(bob, "smoke-bob", password, register=True)
                self.check("distinct accounts", a["user"]["id"] != b["user"]["id"])
                health = self.bound(alice.get("/api/health"), a, "authenticated health").json()
                reconstruction = health.get("reconstruction") or {}
                self.check(
                    "affordable three-candidate reconstruction contract",
                    reconstruction.get("default_mode") == "preview"
                    and reconstruction.get("preview_candidates") == 3
                    and reconstruction.get("preview_standard_estimate_usd") == 0.03,
                )
                usage = health.get("model_usage") or {}
                self.check(
                    "privacy-safe model cost ledger",
                    usage.get("currency") == "USD"
                    and usage.get("event_count") == 0
                    and usage.get("estimated_usd") == 0.0,
                )
                script = alice.get("/static/app.js")
                self.check(
                    "packaged free shopping and preview selection UI",
                    script.status_code == 200
                    and all(
                        label in script.text
                        for label in (
                            "Google 購物",
                            "Bing Shopping",
                            "Google 網頁搜尋",
                            "preview-candidate-picker",
                        )
                    ),
                )
                self.bound(alice.post("/api/demo", headers=self.mutation()), a, "CSRF required", 403)
                wrong_origin = {**self.mutation(a), "Origin": "https://example.invalid"}
                self.bound(alice.post("/api/demo", headers=wrong_origin), a, "Origin required", 403)
                first = self.bound(alice.post("/api/demo", headers=self.mutation(a)), a, "Alice demo").json()
                second = self.bound(bob.post("/api/demo", headers=self.mutation(b)), b, "Bob demo").json()
                self.check("demo jobs independently generated", first["id"] != second["id"])
                image = self.bound(alice.get(first["source_url"]), a, "Alice image")
                image_hash = digest(image.content)
                self.check("demo image is actual JPEG", image.content.startswith(b"\xff\xd8"))
                self.check("anonymous media denied", anon.get(first["source_url"]).status_code == 401)
                for owner, intruder, identity, tag in ((first, bob, b, "Bob"), (second, alice, a, "Alice")):
                    for url in (
                        f"/api/jobs/{owner['id']}",
                        owner["source_url"],
                        f"/api/export?job_id={owner['id']}",
                    ):
                        self.bound(intruder.get(url), identity, f"{tag}: foreign read denied", 404)
                    self.bound(
                        intruder.delete(f"/api/jobs/{owner['id']}", headers=self.mutation(identity)),
                        identity,
                        f"{tag}: foreign delete denied",
                        404,
                    )
                self.catalogue(alice, a, first, image_hash, "Alice initial")
                self.catalogue(bob, b, second, image_hash, "Bob initial")
                csv = self.bound(alice.get("/api/export?format=csv"), a, "CSV export")
                self.check(
                    "CSV carries only Alice job", first["id"] in csv.text and second["id"] not in csv.text
                )
                replay = self.bound(
                    alice.post("/api/demo", headers=self.mutation(a)), a, "demo replay"
                ).json()
                self.check("demo replay avoids duplicate work", replay["id"] == first["id"])
                storage = self.bound(alice.get("/api/storage"), a, "storage accounting").json()
                self.check(
                    "configured storage limits", storage["limits"] == {"jobs": 2, "media_bytes": 64 * 1024**2}
                )
                self.check(
                    "no model task reservations",
                    storage["usage"]["reserved_bytes"] == storage["usage"]["reserved_jobs"] == 0,
                )
                old_cookie = alice.cookies.get("__Host-rewear_session")
                self.bound(alice.post("/auth/logout", headers=self.mutation(a)), a, "logout", 204)
                self.check("logout invalidates session", alice.get("/api/jobs").status_code == 401)
                self.check(
                    "replayed logged-out cookie denied",
                    anon.get(
                        "/api/jobs", headers={"Cookie": f"__Host-rewear_session={old_cookie}"}
                    ).status_code
                    == 401,
                )
                a = self.auth(alice, "smoke-alice", password)
                self.logs(app, "-before-restart")
                self.docker("stop", "--time", "10", proxy)
                self.docker("stop", "--time", "45", app)
                self.docker("start", app)
                self.docker("start", proxy)
                self.ready(anon)
                self.check(
                    "session survives restart", alice.get("/auth/session").json().get("authenticated") is True
                )
                self.catalogue(alice, a, first, image_hash, "Alice after restart")
                self.catalogue(bob, b, second, image_hash, "Bob after restart")
                self.docker("stop", "--time", "10", proxy)
                self.docker("stop", "--time", "45", app)
                backup = self.maintenance(
                    f"{self.prefix}-backup",
                    [
                        "backup",
                        "--data-dir",
                        "/var/lib/rewear/data",
                        "--output",
                        "/var/lib/rewear/smoke.zip",
                        "--mode",
                        "hosted",
                    ],
                    ["--mount", f"type=volume,source={volume},target=/var/lib/rewear"],
                )
                self.check(
                    "image offline backup CLI", backup["status"] == "completed" and backup["mode"] == "hosted"
                )
                restored_volume = self.volume("restored")
                restored = self.maintenance(
                    f"{self.prefix}-restore",
                    ["restore", "--archive", "/input/smoke.zip", "--destination", "/var/lib/rewear/data"],
                    [
                        "--mount",
                        f"type=volume,source={volume},target=/input,readonly",
                        "--mount",
                        f"type=volume,source={restored_volume},target=/var/lib/rewear",
                    ],
                )
                self.check(
                    "image offline restore CLI",
                    restored["status"] == "completed" and restored["files"] == backup["files"],
                )
                self.report["maintenance"] = {"backup": backup, "restore": restored}
                self.remove_container(proxy)
                self.remove_container(app)
                restored_app = self.app("restored-app", restored_volume)
                self.proxy(restored_app, temporary, "restored-proxy")
                self.ready(anon)
                alice.cookies.clear()
                bob.cookies.clear()
                a = self.auth(alice, "smoke-alice", password)
                b = self.auth(bob, "smoke-bob", password)
                self.catalogue(alice, a, first, image_hash, "Alice after restore")
                self.catalogue(bob, b, second, image_hash, "Bob after restore")
                self.report["demo_source_sha256"] = image_hash
                self.report["model_requests"] = 0
                self.report["model_requests_basis"] = (
                    "only offline demo endpoints used; no credentials; provider URLs loopback port 9"
                )
                self.report["status"] = "passed"

    def cleanup(self):
        failures = []
        for name in reversed(self.containers):
            try:
                value = self.docker(
                    "inspect", "--format", f'{{{{index .Config.Labels "{LABEL}"}}}}', name, check=False
                )
                if value.returncode == 0 and value.stdout.strip() == self.ident:
                    self.logs(name)
                    self.docker("rm", "-f", name)
            except SmokeFailure:
                failures.append(f"container:{name}")
        for kind, names in (("volume", self.volumes), ("network", self.networks)):
            for name in reversed(names):
                try:
                    value = self.docker(
                        kind, "inspect", "--format", f'{{{{index .Labels "{LABEL}"}}}}', name, check=False
                    )
                    if value.returncode == 0 and value.stdout.strip() == self.ident:
                        self.docker(kind, "rm", name)
                except SmokeFailure:
                    failures.append(f"{kind}:{name}")
        self.report["cleanup_failures"] = failures
        self.report["finished_at"] = datetime.now(UTC).isoformat()
        if failures:
            self.report["status"] = "failed"
        (self.output / "report.json").write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="rewear-studio:hosted-20261022")
    parser.add_argument(
        "--output", type=Path, required=True, help="A new evidence directory; never overwritten"
    )
    args = parser.parse_args()
    smoke = Smoke(args.image, args.output)
    started = time.monotonic()
    try:
        smoke.run()
    except Exception as error:
        smoke.report["status"] = "failed"
        smoke.report["error"] = str(error) if isinstance(error, SmokeFailure) else type(error).__name__
    finally:
        smoke.report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        smoke.cleanup()
    print(
        json.dumps(
            {
                "status": smoke.report["status"],
                "report": str(args.output / "report.json"),
                "checks": len(smoke.report["checks"]),
            }
        )
    )
    raise SystemExit(0 if smoke.report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
