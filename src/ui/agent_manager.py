"""Agent lifecycle management for the ChemGraph Streamlit UI."""

import asyncio
import json
import logging
import os
import re
import socket
import urllib.parse
import urllib.request
from pathlib import Path, PureWindowsPath
from typing import Optional

import streamlit as st
from langchain_core.tools import StructuredTool

logger = logging.getLogger(__name__)

# Windows absolute paths as they appear in tool results.  The MCP adapter
# returns a JSON array whose text field holds another JSON document, so
# separators may be escaped twice (``E:\\\\PyEnvs\\\\...`` = 4 backslashes);
# ``[\\/]+`` tolerates any escaping level.
_WIN_PATH_RE = re.compile(r"[A-Za-z]:(?:[\\/]+[\w][\w .()\-]*)+[\\/]+[\w .()\-]*\.[A-Za-z0-9]{1,6}")

#: Remote MCP tool sets, cached per (endpoint, mirror root) so a Streamlit
#: rerun does not reconnect (and re-handshake) with the remote server on
#: every interaction.
_REMOTE_TOOL_CACHE: dict[tuple, list] = {}


def resolve_mcp_url(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the remote MCP endpoint.

    Precedence: explicit argument -> ``CHEMGRAPH_MCP_URL`` -> ``[mcp] url`` in
    ``config.toml``.  Returns ``None`` when no remote endpoint is configured,
    which keeps the stock (fully local) behaviour.
    """
    if explicit:
        return explicit
    env_url = os.getenv("CHEMGRAPH_MCP_URL")
    if env_url:
        return env_url
    try:
        from ui.config import load_config

        cfg = load_config() or {}
        mcp_section = cfg.get("mcp") or {}
        url = mcp_section.get("url") if isinstance(mcp_section, dict) else None
        return url or cfg.get("mcp_url") or None
    except Exception:
        return None


def normalize_mcp_url(raw: str) -> str:
    """Normalize a user-entered MCP endpoint into a usable URL.

    Accepts bare ``host:port`` values and fixes the ``/mcp/`` path: a bare
    host gets ``/mcp/`` appended, and a path missing its trailing slash gets
    one (FastMCP redirects pathless requests, but the direct form is faster
    and matches what ``chemgraph run --mcp-url`` expects).

    Returns an empty string when nothing usable was entered.
    """
    url = (raw or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if path in ("/", ""):
        url = url.rstrip("/") + "/mcp/"
    elif not url.endswith("/"):
        url = url + "/"
    return url


def probe_mcp_url(url: str, timeout: float = 2.0):
    """Check that an MCP endpoint's host is reachable over TCP.

    A full MCP handshake is not attempted here on purpose: the probe runs on
    every sidebar rerun, so it must stay cheap.  ``build_toolset`` performs
    the real handshake when the agent is (re)built.

    Returns ``(ok, message)``.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        return False, f"URL 无法解析: {exc}"
    host = parsed.hostname
    if not host:
        return False, "URL 里没有主机名"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} 可达"
    except OSError as exc:
        return False, f"{host}:{port} 不可达 ({exc})"


def _local_default_tools() -> list:
    """The same default tool set ``single_agent.ChemGraphAgent`` would build."""
    from chemgraph.tools.ase_tools import extract_output_json, run_ase
    from chemgraph.tools.cheminformatics_tools import (
        molecule_name_to_smiles,
        smiles_to_coordinate_file,
    )
    from chemgraph.tools.generic_tools import calculator

    return [
        smiles_to_coordinate_file,
        run_ase,
        molecule_name_to_smiles,
        extract_output_json,
        calculator,
    ]


def _http_artifact_base(mcp_url: str) -> str:
    """Derive the artifact HTTP base (``scheme://host:port``) from an MCP URL."""
    parts = urllib.parse.urlsplit(mcp_url)
    return f"{parts.scheme}://{parts.netloc}"


def _normalize_remote_path(raw: str) -> str:
    """Collapse escaped separator runs (``\\\\`` / ``\\\\\\\\``) to plain paths."""
    return re.sub(r"\\{2,}", lambda _m: "\\", raw.replace("/", "\\"))


def _escape_level(raw: str) -> int:
    """Backslashes per separator run in the *original* matched text."""
    m = re.search(r"\\{2,}", raw)
    return len(m.group(0)) if m else 1


def _escape_path(path: str, level: int) -> str:
    """Re-escape a plain path to match the escaping level of the source text."""
    return path.replace("\\", "\\" * level) if level > 1 else path


def _mirror_one_file(remote_path: str, http_base: str, mirror_dir: Path) -> Optional[Path]:
    """Download one server artifact into ``mirror_dir``; return the local path.

    The server exposes files under its artifact root at
    ``<http_base>/artifacts/<filename>``.  Downloading by basename keeps the
    client from ever naming an arbitrary server path.  Returns ``None`` when
    the file cannot be fetched (not served, network error).
    """
    name = PureWindowsPath(remote_path).name
    if not name:
        return None
    mirror_dir.mkdir(parents=True, exist_ok=True)
    dst = mirror_dir / name
    if dst.exists():
        stem, suffix = dst.stem, dst.suffix
        i = 1
        while dst.exists():
            dst = mirror_dir / f"{stem}_{i}{suffix}"
            i += 1
    url = f"{http_base}/artifacts/{urllib.parse.quote(name)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(dst, "wb") as f:
            f.write(resp.read())
    except OSError:
        return None
    return dst


def _replace_path_in_text(text: str, raw_match: str, local_path: str) -> str:
    """Swap a remote artifact path for its local mirror in a tool result.

    ``raw_match`` is the exact substring found in the text — separators may be
    escaped to any level (JSON-in-JSON gives 4 backslashes).  The local path
    is re-escaped to the same level so the surrounding JSON stays valid.
    """
    return text.replace(raw_match, _escape_path(local_path, _escape_level(raw_match)))


def _wrap_with_mirror(tool, http_base: str, mirror_root: Path):
    """Wrap one remote tool so artifact paths in its result are mirrored.

    The wrapper calls the inner tool (async — the MCP adapter tools are
    async-only), downloads every artifact the result references from the
    server's ``/artifacts/`` HTTP endpoint into ``mirror_root``, and rewrites
    the paths in the result to the local copies.  A download failure never
    fails the tool: the original remote path stays in the result and the
    agent still answers.
    """
    if tool.name == "read_artifact":
        # The mirror's own transport; exposing it wrapped would recurse.
        return tool

    async def _acall(**kwargs):
        result = await tool.ainvoke(kwargs)
        try:
            if isinstance(result, bytes):
                result = result.decode("utf-8", "replace")
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            paths = {m.group(0) for m in _WIN_PATH_RE.finditer(result)}
            for raw in paths:
                plain = _normalize_remote_path(raw)
                local = await asyncio.to_thread(
                    _mirror_one_file, plain, http_base, mirror_root
                )
                if local is not None:
                    result = _replace_path_in_text(result, raw, str(local))
        except Exception as exc:  # mirroring must never break the tool call
            logger.warning("artifact mirroring failed for %s: %s", tool.name, exc)
        return result

    return StructuredTool.from_function(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=_acall,
    )


def build_toolset(
    mcp_url: Optional[str] = None,
    force_local: bool = False,
    mirror_root: Optional[str] = None,
):
    """Build the agent tool set, preferring remote MCP tools on name clashes.

    Returns ``(tools, url)``.  ``tools`` is ``None`` when no remote endpoint
    is used — either because ``force_local`` is set (the sidebar toggle chose
    local) or because no endpoint is configured / reachable — and the caller
    falls back to ChemGraph's own defaults.  ``url`` is the resolved endpoint
    so the caller can report which one was attempted.  Note that
    ``mcp_url=None`` alone means "auto-resolve from env/config", NOT "local":
    the sidebar must pass ``force_local=True`` to pin computation to this
    machine.

    When ``mirror_root`` is given alongside a remote endpoint, every remote
    tool is wrapped so the artifacts it produces (xyz/traj/json/...) are
    downloaded from the server's ``/artifacts/`` HTTP endpoint into
    ``mirror_root`` and the paths in the tool result are rewritten to the
    local copies.  The UI's artifact pipeline (downloads, 3D viewer,
    manifest) then works identically for local and remote runs.
    """
    if force_local:
        return None, None
    url = resolve_mcp_url(mcp_url)
    if not url:
        return None, None
    cache_key = (url, mirror_root)
    if cache_key in _REMOTE_TOOL_CACHE:
        return _REMOTE_TOOL_CACHE[cache_key], url

    from chemgraph.cli.mcp_utils import load_mcp_tools_from_config

    remote_tools = load_mcp_tools_from_config(
        url=url, server_name="ChemGraph Remote Compute"
    )
    if not remote_tools:
        return None, url

    remote_names = {getattr(tool, "name", "") for tool in remote_tools}
    # Remote wins: dropping a same-named local tool is what actually moves the
    # computation off this machine (e.g. local ``run_ase`` -> remote ``run_ase``).
    merged = [
        tool for tool in _local_default_tools() if tool.name not in remote_names
    ] + list(remote_tools)
    if mirror_root:
        http_base = _http_artifact_base(url)
        merged = [
            _wrap_with_mirror(tool, http_base, Path(mirror_root) / "remote_artifacts")
            for tool in merged
        ]
    _REMOTE_TOOL_CACHE[cache_key] = merged
    return merged, url


def initialize_agent(
    model_name: str,
    workflow_type: str,
    structured_output: bool,
    return_option: str,
    generate_report: bool,
    human_supervised: bool,
    recursion_limit: int,
    base_url: Optional[str],
    argo_user: Optional[str],
    log_dir: Optional[str] = None,
    mcp_url: Optional[str] = None,
    force_local: bool = False,
):
    """Create a :class:`ChemGraph` agent instance.

    No ``@st.cache_resource`` -- the caller (``_auto_initialize_agent``
    in ``main_interface.py``) already manages caching via
    ``st.session_state.agent`` and ``st.session_state.last_config``.
    Using the decorator caused failed initialisations (``None``) to be
    permanently cached with no way to retry.

    Parameters
    ----------
    model_name : str
        LLM model identifier.
    workflow_type : str
        ChemGraph workflow name.
    structured_output : bool
        Whether structured final output is requested.
    return_option : str
        Agent return mode.
    generate_report : bool
        Whether report generation is enabled.
    human_supervised : bool
        Whether human-supervision tools are enabled.
    recursion_limit : int
        LangGraph recursion limit.
    base_url : str, optional
        Custom model endpoint URL.
    argo_user : str, optional
        Argo username for Argo-hosted models.
    log_dir : str, optional
        Directory for ChemGraph run logs.

    Returns
    -------
    ChemGraph or None
        Initialized agent, or ``None`` if initialization fails.
    """
    try:
        from chemgraph.agent.llm_agent import ChemGraph

        tools, remote_url = build_toolset(
            mcp_url, force_local=force_local, mirror_root=log_dir
        )
        if remote_url and tools is None:
            st.warning(
                f"远程算力端点不可达，已回退到本地计算：{remote_url}"
            )
        # Track the effective compute location for the sidebar display; reset
        # it when the agent was built without remote tools so a stale URL
        # from a previous mode never lingers in the session state.
        st.session_state["remote_mcp_url"] = remote_url if tools is not None else None

        return ChemGraph(
            model_name=model_name,
            workflow_type=workflow_type,
            base_url=base_url,
            argo_user=argo_user,
            structured_output=structured_output,
            generate_report=generate_report,
            return_option=return_option,
            recursion_limit=recursion_limit,
            human_supervised=human_supervised,
            log_dir=log_dir,
            tools=tools,
        )
    except Exception as exc:
        st.error(f"Failed to initialize agent: {exc}")
        return None


#: Attributes that identify a conversation and its persistence bookkeeping.
_CONVERSATION_ATTRIBUTES = (
    "uuid",
    "session_store",
    "_session_created",
    "_saved_message_keys",
    "_session_title",
)


def transfer_conversation_state(source, target) -> None:
    """Move an active conversation from one agent instance to another.

    Rebuilding the agent (for example after a provider API key is replaced)
    compiles a fresh graph with an empty in-memory checkpointer, which would
    drop the LangGraph thread state, any pending human-input interrupt, and
    the session bookkeeping that prevents duplicate history writes. Reuse the
    previous checkpointer and session identity on the new instance so the
    conversation continues under the new credentials.

    Parameters
    ----------
    source : ChemGraph
        Agent that currently owns the conversation.
    target : ChemGraph
        Newly constructed agent with the same workflow configuration.
    """
    if source is None or target is None or source is target:
        return
    for name in _CONVERSATION_ATTRIBUTES:
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))
    source_workflow = getattr(source, "workflow", None)
    target_workflow = getattr(target, "workflow", None)
    checkpointer = getattr(source_workflow, "checkpointer", None)
    if checkpointer is not None and target_workflow is not None:
        target_workflow.checkpointer = checkpointer
    if getattr(source, "checkpointer", None) is not None:
        target.checkpointer = source.checkpointer


def run_async_callable(fn):
    """Run an async callable and return its result in a sync context.

    Parameters
    ----------
    fn : Callable
        Zero-argument callable returning an awaitable.

    Returns
    -------
    Any
        Result produced by the awaited callable.
    """
    from chemgraph.utils.async_utils import run_async_callable as _impl

    return _impl(fn)
