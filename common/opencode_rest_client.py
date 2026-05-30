import json
from pathlib import Path
import requests
from typing import Any, Dict, Optional, cast


_GLOBAL_SCHEMA: Optional[Dict[str, Any]] = None
_GLOBAL_OPERATION_MAP: Optional[Dict[str, Dict[str, Any]]] = None
_GLOBAL_SCHEMA_SOURCE: Optional[str] = None


def _default_schema_file() -> Path:
    source_root = Path(__file__).parents[1] / "opencode_api.json"
    package_root = Path(__file__).parents[2] / "opencode_api.json"
    installed_root = Path(__file__).parents[3] / "opencode_api.json"
    for candidate in (source_root, package_root, installed_root):
        if candidate.is_file():
            return candidate
    return source_root


class OpenCodeApiError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class OpenCodeRestClient:
    """
    REST SDK pro OpenCode API.

    OpenAPI schema je dostupná na /doc. Třída si ji umí načíst a používá ji
    pro mapování operationId → HTTP metoda + cesta.
    """

    DEFAULT_BASE_URL = "http://10.0.0.247:4096"
    DEFAULT_SCHEMA_FILE = _default_schema_file()

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        directory: Optional[str] = None,
        timeout: float = 30.0,
        schema_url: Optional[str] = None,
        schema_file: Optional[Path] = DEFAULT_SCHEMA_FILE,
        session: Optional[requests.Session] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self.timeout = timeout
        self.schema_url = schema_url or f"{self.base_url}/doc"
        self.schema_file = schema_file
        self.session = session or requests.Session()
        self.headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self._schema: Optional[Dict[str, Any]] = None
        self._operation_map: Optional[Dict[str, Dict[str, Any]]] = None

    # --- Schema / OpenAPI helpers ---

    def load_schema(self, force: bool = False) -> Dict[str, Any]:
        global _GLOBAL_SCHEMA
        global _GLOBAL_OPERATION_MAP
        global _GLOBAL_SCHEMA_SOURCE

        schema_source = str(self.schema_file) if self.schema_file is not None else self.schema_url

        if (
            not force
            and _GLOBAL_SCHEMA is not None
            and _GLOBAL_OPERATION_MAP is not None
            and _GLOBAL_SCHEMA_SOURCE == schema_source
        ):
            self._schema = _GLOBAL_SCHEMA
            self._operation_map = _GLOBAL_OPERATION_MAP
            return self._schema

        if self._schema is not None and not force:
            return self._schema

        if self.schema_file is not None and self.schema_file.is_file():
            try:
                self._schema = json.loads(self.schema_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise OpenCodeApiError(f"Nepodařilo se načíst OpenAPI schema ze souboru: {exc}") from exc
        else:
            try:
                response = self.session.get(self.schema_url, timeout=self.timeout)
                response.raise_for_status()
                self._schema = response.json()
            except requests.RequestException as exc:
                raise OpenCodeApiError(f"Nepodařilo se načíst OpenAPI schema: {exc}") from exc
            except ValueError as exc:
                raise OpenCodeApiError("OpenAPI schema není validní JSON") from exc

        if not isinstance(self._schema, dict):
            raise OpenCodeApiError("OpenAPI schema musí být objekt")

        schema = cast(Dict[str, Any], self._schema)
        self._operation_map = self._build_operation_map(schema)
        _GLOBAL_SCHEMA = schema
        _GLOBAL_OPERATION_MAP = self._operation_map
        _GLOBAL_SCHEMA_SOURCE = schema_source
        return schema

    def list_operations(self) -> list[str]:
        self._ensure_schema()
        assert self._operation_map is not None
        return sorted(self._operation_map.keys())

    def get_operation(self, operation_id: str) -> Dict[str, Any]:
        self._ensure_schema()
        assert self._operation_map is not None
        operation = self._operation_map.get(operation_id)
        if not operation:
            raise ValueError(f"Neznámé operationId: {operation_id}")
        return operation

    def _ensure_schema(self) -> None:
        if self._schema is None or self._operation_map is None:
            self.load_schema()

    @staticmethod
    def _build_operation_map(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        operations: Dict[str, Dict[str, Any]] = {}
        for path, methods in schema.get("paths", {}).items():
            for method, info in methods.items():
                if not isinstance(info, dict):
                    continue
                operation_id = info.get("operationId")
                if not operation_id:
                    continue
                operations[operation_id] = {
                    "method": method.upper(),
                    "path": path,
                    "parameters": info.get("parameters", []),
                    "requestBody": info.get("requestBody"),
                }
        return operations

    # --- Low-level HTTP ---

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        merged_headers = {**self.headers, **(headers or {})}

        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_data,
                headers=merged_headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise OpenCodeApiError(f"Request failed: {exc}") from exc

        if response.status_code >= 400:
            raise self._build_error(response)

        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: requests.Response) -> Any:
        content_type = response.headers.get("Content-Type", "")
        if not response.content:
            return None
        if "application/json" in content_type:
            return response.json()
        return response.text

    @staticmethod
    def _build_error(response: requests.Response) -> OpenCodeApiError:
        status_code = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = response.text

        message = None
        if isinstance(data, dict):
            message = data.get("message") or data.get("error") or data.get("detail")

        if not message:
            message = f"API Error {status_code}"

        return OpenCodeApiError(message, status_code=status_code, details=data)

    # --- OpenAPI-driven call ---

    def call(
        self,
        operation_id: str,
        *,
        path_params: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        operation = self.get_operation(operation_id)
        path_template = operation["path"]

        if "{" in path_template and "}" in path_template:
            if not path_params:
                raise ValueError(f"Operation {operation_id} vyžaduje path parametry")
            try:
                path = path_template.format(**path_params)
            except KeyError as exc:
                raise ValueError(f"Chybí path parametr '{exc.args[0]}' pro operation {operation_id}") from exc
        else:
            path = path_template

        params = {**(query or {})}
        if self.directory and self._has_query_param(operation, "directory"):
            params.setdefault("directory", self.directory)

        return self.request(
            operation["method"],
            path,
            params=params,
            json_data=body,
            headers=headers,
            timeout=timeout,
        )

    @staticmethod
    def _has_query_param(operation: Dict[str, Any], name: str) -> bool:
        for param in operation.get("parameters", []):
            if param.get("in") == "query" and param.get("name") == name:
                return True
        return False

    # --- Convenience wrappers (nejčastější endpointy) ---

    def health(self) -> Any:
        return self.call("global.health")

    def dispose_global(self) -> Any:
        return self.call("global.dispose")

    def project_list(self, directory: Optional[str] = None) -> Any:
        return self.call("project.list", query=self._directory_query(directory))

    def project_current(self, directory: Optional[str] = None) -> Any:
        return self.call("project.current", query=self._directory_query(directory))

    def project_update(
        self,
        project_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "project.update",
            path_params={"projectID": project_id},
            query=self._directory_query(directory),
            body=data,
        )

    def pty_list(self, directory: Optional[str] = None) -> Any:
        return self.call("pty.list", query=self._directory_query(directory))

    def pty_create(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call(
            "pty.create",
            query=self._directory_query(directory),
            body=data,
        )

    def pty_get(self, pty_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "pty.get",
            path_params={"ptyID": pty_id},
            query=self._directory_query(directory),
        )

    def pty_update(
        self,
        pty_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "pty.update",
            path_params={"ptyID": pty_id},
            query=self._directory_query(directory),
            body=data,
        )

    def pty_remove(self, pty_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "pty.remove",
            path_params={"ptyID": pty_id},
            query=self._directory_query(directory),
        )

    def config_get(self, directory: Optional[str] = None) -> Any:
        return self.call("config.get", query=self._directory_query(directory))

    def config_update(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call(
            "config.update",
            query=self._directory_query(directory),
            body=data,
        )

    def session_list(self, directory: Optional[str] = None, **query: Any) -> Any:
        return self.call(
            "session.list",
            query=self._directory_query(directory, query),
        )

    def session_status(self, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.status",
            query=self._directory_query(directory),
        )

    def session_create(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call(
            "session.create",
            query=self._directory_query(directory),
            body=data,
        )

    def session_get(self, session_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.get",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    def session_update(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.update",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
        )

    def session_abort(
        self,
        session_id: str,
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.abort",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    def session_delete(self, session_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.delete",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    def session_messages(
        self,
        session_id: str,
        directory: Optional[str] = None,
        **query: Any,
    ) -> Any:
        return self.call(
            "session.messages",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory, query),
        )

    def session_children(self, session_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.children",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    def session_prompt(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        return self.call(
            "session.prompt",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
            timeout=timeout,
        )

    def session_prompt_async(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.prompt_async",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
        )

    def session_command(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.command",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
        )

    def session_shell(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.shell",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
        )

    def file_list(self, path: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "file.list",
            query=self._directory_query(directory, {"path": path}),
        )

    def file_read(self, path: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "file.read",
            query=self._directory_query(directory, {"path": path}),
        )

    def find_text(self, pattern: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "find.text",
            query=self._directory_query(directory, {"pattern": pattern}),
        )

    def find_files(
        self,
        query_str: str,
        directory: Optional[str] = None,
        **extra: Any,
    ) -> Any:
        return self.call(
            "find.files",
            query=self._directory_query(directory, {"query": query_str, **extra}),
        )

    def find_symbols(self, query_str: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "find.symbols",
            query=self._directory_query(directory, {"query": query_str}),
        )

    # --- Global config ---

    def global_config_get(self) -> Any:
        return self.call("global.config.get")

    def global_config_update(self, data: Dict[str, Any]) -> Any:
        return self.call("global.config.update", body=data)

    def global_upgrade(self, target: Optional[str] = None) -> Any:
        body: Dict[str, Any] = {}
        if target is not None:
            body["target"] = target
        return self.call("global.upgrade", body=body)

    # --- Auth ---

    def auth_set(self, provider_id: str, data: Dict[str, Any]) -> Any:
        return self.call("auth.set", path_params={"providerID": provider_id}, body=data)

    def auth_remove(self, provider_id: str) -> Any:
        return self.call("auth.remove", path_params={"providerID": provider_id})

    # --- Project ---

    def project_init_git(self, directory: Optional[str] = None) -> Any:
        return self.call("project.initGit", query=self._directory_query(directory))

    # --- Session (extended) ---

    def session_todo(self, session_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.todo",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    def session_init(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.init",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
        )

    def session_fork(
        self,
        session_id: str,
        message_id: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> Any:
        body: Dict[str, Any] = {}
        if message_id is not None:
            body["messageID"] = message_id
        return self.call(
            "session.fork",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=body,
        )

    def session_share(self, session_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.share",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    def session_unshare(self, session_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.unshare",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    def session_diff(
        self,
        session_id: str,
        message_id: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> Any:
        query = self._directory_query(directory)
        if message_id is not None:
            query["messageID"] = message_id
        return self.call(
            "session.diff",
            path_params={"sessionID": session_id},
            query=query,
        )

    def session_summarize(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.summarize",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
        )

    def session_message(
        self,
        session_id: str,
        message_id: str,
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.message",
            path_params={"sessionID": session_id, "messageID": message_id},
            query=self._directory_query(directory),
        )

    def session_delete_message(
        self,
        session_id: str,
        message_id: str,
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.deleteMessage",
            path_params={"sessionID": session_id, "messageID": message_id},
            query=self._directory_query(directory),
        )

    def session_revert(
        self,
        session_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "session.revert",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
            body=data,
        )

    def session_unrevert(self, session_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "session.unrevert",
            path_params={"sessionID": session_id},
            query=self._directory_query(directory),
        )

    # --- Parts ---

    def part_delete(
        self,
        session_id: str,
        message_id: str,
        part_id: str,
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "part.delete",
            path_params={"sessionID": session_id, "messageID": message_id, "partID": part_id},
            query=self._directory_query(directory),
        )

    def part_update(
        self,
        session_id: str,
        message_id: str,
        part_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "part.update",
            path_params={"sessionID": session_id, "messageID": message_id, "partID": part_id},
            query=self._directory_query(directory),
            body=data,
        )

    # --- Permissions ---

    def permission_list(self, directory: Optional[str] = None) -> Any:
        return self.call("permission.list", query=self._directory_query(directory))

    def permission_reply(
        self,
        request_id: str,
        reply: str,
        message: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> Any:
        body: Dict[str, Any] = {"reply": reply}
        if message is not None:
            body["message"] = message
        return self.call(
            "permission.reply",
            path_params={"requestID": request_id},
            query=self._directory_query(directory),
            body=body,
        )

    # --- Questions ---

    def question_list(self, directory: Optional[str] = None) -> Any:
        return self.call("question.list", query=self._directory_query(directory))

    def question_reply(
        self,
        request_id: str,
        answers: list[list[str]],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "question.reply",
            path_params={"requestID": request_id},
            query=self._directory_query(directory),
            body={"answers": answers},
        )

    def question_reject(self, request_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "question.reject",
            path_params={"requestID": request_id},
            query=self._directory_query(directory),
        )

    # --- Providers ---

    def provider_list(self, directory: Optional[str] = None) -> Any:
        return self.call("provider.list", query=self._directory_query(directory))

    def provider_auth(self, directory: Optional[str] = None) -> Any:
        return self.call("provider.auth", query=self._directory_query(directory))

    def provider_oauth_authorize(
        self,
        provider_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "provider.oauth.authorize",
            path_params={"providerID": provider_id},
            query=self._directory_query(directory),
            body=data,
        )

    def provider_oauth_callback(
        self,
        provider_id: str,
        data: Dict[str, Any],
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "provider.oauth.callback",
            path_params={"providerID": provider_id},
            query=self._directory_query(directory),
            body=data,
        )

    # --- Config ---

    def config_providers(self, directory: Optional[str] = None) -> Any:
        return self.call("config.providers", query=self._directory_query(directory))

    # --- Tools ---

    def tool_ids(self, directory: Optional[str] = None) -> Any:
        return self.call("tool.ids", query=self._directory_query(directory))

    def tool_list(self, provider: str, model: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "tool.list",
            query=self._directory_query(directory, {"provider": provider, "model": model}),
        )

    # --- File status ---

    def file_status(self, directory: Optional[str] = None) -> Any:
        return self.call("file.status", query=self._directory_query(directory))

    # --- MCP ---

    def mcp_status(self, directory: Optional[str] = None) -> Any:
        return self.call("mcp.status", query=self._directory_query(directory))

    def mcp_add(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call("mcp.add", query=self._directory_query(directory), body=data)

    def mcp_connect(self, name: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "mcp.connect",
            path_params={"name": name},
            query=self._directory_query(directory),
        )

    def mcp_disconnect(self, name: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "mcp.disconnect",
            path_params={"name": name},
            query=self._directory_query(directory),
        )

    def mcp_auth_start(self, name: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "mcp.auth.start",
            path_params={"name": name},
            query=self._directory_query(directory),
        )

    def mcp_auth_remove(self, name: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "mcp.auth.remove",
            path_params={"name": name},
            query=self._directory_query(directory),
        )

    def mcp_auth_callback(
        self,
        name: str,
        code: str,
        directory: Optional[str] = None,
    ) -> Any:
        return self.call(
            "mcp.auth.callback",
            path_params={"name": name},
            query=self._directory_query(directory),
            body={"code": code},
        )

    # --- Commands / Agents / Skills ---

    def command_list(self, directory: Optional[str] = None) -> Any:
        return self.call("command.list", query=self._directory_query(directory))

    def app_agents(self, directory: Optional[str] = None) -> Any:
        return self.call("app.agents", query=self._directory_query(directory))

    def app_skills(self, directory: Optional[str] = None) -> Any:
        return self.call("app.skills", query=self._directory_query(directory))

    def app_log(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call("app.log", query=self._directory_query(directory), body=data)

    # --- LSP / Formatter ---

    def lsp_status(self, directory: Optional[str] = None) -> Any:
        return self.call("lsp.status", query=self._directory_query(directory))

    def formatter_status(self, directory: Optional[str] = None) -> Any:
        return self.call("formatter.status", query=self._directory_query(directory))

    # --- Path / VCS ---

    def path_get(self, directory: Optional[str] = None) -> Any:
        return self.call("path.get", query=self._directory_query(directory))

    def vcs_get(self, directory: Optional[str] = None) -> Any:
        return self.call("vcs.get", query=self._directory_query(directory))

    # --- Instance ---

    def instance_dispose(self, directory: Optional[str] = None) -> Any:
        return self.call("instance.dispose", query=self._directory_query(directory))

    # --- Workspaces (experimental) ---

    def workspace_create(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call(
            "experimental.workspace.create",
            query=self._directory_query(directory),
            body=data,
        )

    def workspace_list(self, directory: Optional[str] = None) -> Any:
        return self.call("experimental.workspace.list", query=self._directory_query(directory))

    def workspace_remove(self, workspace_id: str, directory: Optional[str] = None) -> Any:
        return self.call(
            "experimental.workspace.remove",
            path_params={"id": workspace_id},
            query=self._directory_query(directory),
        )

    # --- Worktrees (experimental) ---

    def worktree_create(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call(
            "worktree.create",
            query=self._directory_query(directory),
            body=data,
        )

    def worktree_list(self, directory: Optional[str] = None) -> Any:
        return self.call("worktree.list", query=self._directory_query(directory))

    def worktree_remove(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call(
            "worktree.remove",
            query=self._directory_query(directory),
            body=data,
        )

    def worktree_reset(self, data: Dict[str, Any], directory: Optional[str] = None) -> Any:
        return self.call(
            "worktree.reset",
            query=self._directory_query(directory),
            body=data,
        )

    # --- Experimental session / resource lists ---

    def experimental_session_list(self, directory: Optional[str] = None, **query: Any) -> Any:
        return self.call(
            "experimental.session.list",
            query=self._directory_query(directory, query),
        )

    def experimental_resource_list(self, directory: Optional[str] = None) -> Any:
        return self.call(
            "experimental.resource.list",
            query=self._directory_query(directory),
        )

    # --- Helpers ---

    @staticmethod
    def _directory_query(
        directory: Optional[str],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        query = {**(extra or {})}
        if directory:
            query["directory"] = directory
        return query
