from __future__ import annotations

import subprocess
from pathlib import Path


class KubectlError(RuntimeError):
    def __init__(self, message: str, returncode: int, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class OpenCodeManifestParser:
    CONTAINER_NAME = "opencode"
    INGRESS_DOMAIN_SUFFIX = "dev.agentis.cz"

    def __init__(
        self,
        namespace: str,
        workspace_path: str,
        main_dir: str | None = None,
        agentis_url: str | None = None,
    ) -> None:
        namespace_value = namespace.strip()
        workspace_value = workspace_path.strip()

        if not namespace_value:
            raise ValueError("namespace must not be empty")
        if not workspace_value:
            raise ValueError("workspace_path must not be empty")

        self.namespace = namespace_value
        self.workspace_path = workspace_value
        self.main_dir = main_dir.strip() if main_dir else workspace_value
        self.agentis_url = agentis_url.strip() if agentis_url and agentis_url.strip() else None

    def parse_text(self, manifest_text: str) -> str:
        result = manifest_text
        result = result.replace("[%NAMESPACE%]", self.namespace)
        result = result.replace("[%WORKDIR%]", self.workspace_path)
        result = result.replace("[%MAIN_DIR%]", self.main_dir)
        result = result.replace("[%AGENTIS_URL%]", self.agentis_url or "")
        return result

    def parse_file(self, source_path: str | Path, target_path: str | Path | None = None) -> str:
        source = Path(source_path)
        parsed = self.parse_text(source.read_text(encoding="utf-8"))
        if target_path is not None:
            Path(target_path).write_text(parsed, encoding="utf-8")
        return parsed

    def apply(self, source_path: str | Path) -> str:
        manifest = self.parse_file(source_path)
        return self._kubectl_stdin(["apply", "-f", "-"], manifest)

    def delete(self, source_path: str | Path, ignore_not_found: bool = True) -> str:
        manifest = self.parse_file(source_path)
        args = ["delete", "-f", "-", "--wait=false"]
        if ignore_not_found:
            args.append("--ignore-not-found=true")
        return self._kubectl_stdin(args, manifest)

    def restart(self) -> str:
        return self._kubectl(
            [
                "rollout",
                "restart",
                f"deployment/{self.CONTAINER_NAME}",
                "-n",
                self.namespace,
            ]
        )

    def _kubectl(self, args: list[str]) -> str:
        result = subprocess.run(["kubectl", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise KubectlError(
                f"kubectl {' '.join(args)} failed: {result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result.stdout

    def _kubectl_stdin(self, args: list[str], stdin_data: str) -> str:
        result = subprocess.run(["kubectl", *args], input=stdin_data, capture_output=True, text=True)
        if result.returncode != 0:
            raise KubectlError(
                f"kubectl {' '.join(args)} failed: {result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result.stdout
