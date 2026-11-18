#!/usr/bin/env python3
"""
errlog — registra los errores que encuentra un agente de código y cómo los soluciona.

Funciona con Claude Code, Codex CLI, Gemini CLI y Cursor a través de sus hooks, y con
cualquier otro agente a través de `errlog run -- <comando>`.

Ciclo de un error:
  1. Un comando falla (tests, build, lint…)      → se abre un error con su firma y su salida.
  2. El agente edita archivos                     → se anotan como intentos del error abierto.
  3. El mismo comando vuelve a pasar              → el error se marca resuelto y se guarda el diff.
  4. Al terminar el turno                         → se pide al agente la causa y la solución
                                                    (`errlog note`), una sola vez por error.

Con AiFlowin (instalación global, `install --global --api URL --code CODE`):
  - Al abrir un error, el script lo busca en la red de AiFlowin. Si hay una solución conocida, la
    añade al contexto del agente; si no, el servidor publica el error. El agente no llama a nada.
  - Los intentos, la solución (el diff) y las notas se envían en segundo plano desde una cola.
  - Los registros van a ~/.aiflowin/projects/<proyecto>/ y ningún repositorio cambia.

Sin AiFlowin (instalación por proyecto, `install`): todo se guarda en <proyecto>/.errlog/log/:
errors.json, events.jsonl, patches/ y ERRORES.md.

Comandos:
  install   Copia errlog al proyecto (o, con --global, al usuario) y configura los hooks.
  uninstall Quita los hooks globales y la credencial.
  flush     Envía la cola pendiente a AiFlowin (lo lanza el propio hook en segundo plano).
  hook      Punto de entrada de los hooks (lee el JSON del agente por stdin).
  run       Ejecuta un comando y registra el resultado (para agentes sin hooks).
  note      Añade la causa y la solución de un error.
  list      Lista los errores.
  show      Muestra un error completo.
  report    Regenera ERRORES.md.

Solo usa la librería estándar de Python 3.8+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

VERSION = "0.4.3"
AIFLOWIN_HOME = Path(os.environ.get("AIFLOWIN_HOME") or Path.home() / ".aiflowin")
CREDENTIALS = AIFLOWIN_HOME / "credentials"
OUTBOX = AIFLOWIN_HOME / "outbox.jsonl"
AGENTS = ("claude", "codex", "gemini", "cursor")
# Solo como plugin (sin instalador global): Antigravity CLI y el estándar Agent Plugins (Copilot CLI, VS Code…).
HOOK_AGENTS = AGENTS + ("antigravity", "agentplugins")

DEFAULT_CONFIG = {
    # Comandos que no se registran aunque devuelvan error (búsquedas, inspección, etc.).
    "ignore_commands": [
        r"^(grep|egrep|rg|ag|ack|find|fd|ls|tree|cat|bat|head|tail|less|more|wc|which|whereis|type|command|"
        r"test|\[|diff|cmp|stat|file|pgrep|ps|lsof|echo|printf|true|false|sleep|kill|pkill|open|cd|pwd|env|"
        r"jq|curl|wget|ping|history|mkdir|rm|mv|cp|touch|chmod)\b",
        r"^git (status|diff|log|show|grep|rev-parse|ls-files|branch|remote|stash list|config|fetch|blame)\b",
    ],
    # Si no está vacía, solo se registran los comandos que coincidan.
    "only_commands": [],
    # Pedir al agente la causa y la solución al terminar el turno. None: sí en un proyecto (.errlog),
    # no en la instalación global o de plugin (allí basta con el diff y no se gastan tokens).
    "ask_for_notes": None,
    "max_excerpt_lines": 30,
    "max_patch_lines": 400,
    # Red de AiFlowin. enabled None: activa en cuanto hay credencial; False: nunca.
    "aiflowin": {"enabled": None, "report_timeout_s": 3, "max_context_chars": 800, "connect_hint_hours": 20},
}

ERROR_LINE_RE = re.compile(
    r"(error|Error|ERROR|ERR!|FAIL|Fail|failed|Failed|FAILED|Exception|Traceback|panic|fatal|Fatal|"
    r"✗|✘|×|AssertionError|TypeError|SyntaxError|ReferenceError|NameError|ImportError|ModuleNotFoundError|"
    r"cannot find|Cannot find|not found|undefined|segmentation fault|exit status [1-9])")
SECRET_RES = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{10,}"), re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
]


# --------------------------------------------------------------------------------------
# Rutas, bloqueo y almacenamiento
# --------------------------------------------------------------------------------------

def project_root(payload: dict | None = None) -> Path:
    here = Path(__file__).resolve().parent
    if here.name == ".errlog":
        return here.parent
    for var in ("CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "GEMINI_PROJECT_DIR"):
        if os.environ.get(var):
            return Path(os.environ[var]).resolve()
    payload = payload or {}
    roots = payload.get("workspace_roots") or payload.get("workspacePaths") or []
    start = payload.get("cwd") or (roots[0] if roots else None)
    start = Path(start) if start and Path(start).is_dir() else Path.cwd()
    top = git(["rev-parse", "--show-toplevel"], start)
    return Path(top) if top else start.resolve()


def read_json_file(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def merged_config(root: Path) -> dict:
    """Valores por defecto < ~/.aiflowin/config.json (global) < <proyecto>/.errlog/config.json."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in (AIFLOWIN_HOME / "config.json", root / ".errlog" / "config.json"):
        extra = read_json_file(path, {}) or {}
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if cfg.get("ask_for_notes") is None:
        cfg["ask_for_notes"] = Path(__file__).resolve().parent.name == ".errlog"
    return cfg


def data_dir(root: Path) -> Path:
    if Path(__file__).resolve().parent.name == ".errlog":
        return root / ".errlog" / "log"
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", root.name)[:40] or "root"
    return AIFLOWIN_HOME / "projects" / f"{slug}-{hashlib.sha1(str(root).encode()).hexdigest()[:10]}"


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.dir = data_dir(root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.errors_path = self.dir / "errors.json"
        self.events_path = self.dir / "events.jsonl"
        self.config = merged_config(root)
        # Trabajo para después de guardar, fuera del bloqueo: (tipo, id del error).
        self.pending: list = []

    @contextmanager
    def lock(self):
        with open(self.dir / ".lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def load(self) -> dict:
        try:
            return json.loads(self.errors_path.read_text())
        except Exception:
            return {"next": 1, "errors": {}, "pending_edits": {}}

    def save(self, data: dict) -> None:
        tmp = self.errors_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(self.errors_path)
        write_report(self, data)

    def event(self, **kw) -> None:
        kw.setdefault("ts", now())
        with open(self.events_path, "a") as fh:
            fh.write(json.dumps(kw, ensure_ascii=False) + "\n")


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def git(args: list, cwd: Path, timeout: int = 10) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def scrub(text: str, root: Path) -> str:
    for rx in SECRET_RES:
        text = rx.sub("<REDACTED>", text)
    return text.replace(str(root), ".").replace(str(Path.home()), "~")


# --------------------------------------------------------------------------------------
# Normalización de comandos y errores
# --------------------------------------------------------------------------------------

MULTI_WORD = {"npm", "pnpm", "yarn", "bun", "npx", "bunx", "uv", "poetry", "pipenv", "cargo", "go", "make",
              "dotnet", "mvn", "gradle", "./gradlew", "deno", "mix", "bundle", "rake", "rails", "php", "composer",
              "docker", "turbo", "nx"}
INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php", "bash", "sh", "zsh", "tsx", "ts-node",
                "Rscript", "julia", "lua", "swift", "java", "dotnet-script"}


def _clean_segment(c: str) -> str:
    c = re.sub(r"\s*\|\s*(tail|head|grep|less|cat|tee)\b.*$", "", c.strip())
    c = re.sub(r"\s*(2>&1|2>/dev/null|>\s*/dev/null|1>&2)", "", c)
    c = re.sub(r"\s*\|\|\s*(true|:)\s*$", "", c)
    c = re.sub(r"^(\s*[A-Z_][A-Z0-9_]*=\S*\s+)+", "", c)
    c = re.sub(r"^(timeout\s+\d+\S*|time)\s+", "", c)
    return re.sub(r"\s+", " ", c).strip()


def command_key(cmd: str, cfg: dict | None = None) -> str:
    """Comando normalizado. Quita lo que no es el comando que se comprueba:
    `cd x &&`, `ls &&`, `; echo EXIT=$?`, variables, redirecciones, `| tail`, `|| true`."""
    ignore = (cfg or DEFAULT_CONFIG).get("ignore_commands") or []
    segs = [_clean_segment(s) for s in re.split(r"\s*(?:&&|;|\n)\s*", cmd or "")]
    segs = [s for s in segs if s and not any(re.search(p, s) for p in ignore)]
    return " && ".join(segs)


MASKED_RE = re.compile(r";\s*echo\b|\|\|\s*(true|:)|\|\s*(tail|head|grep|tee|less|cat)\b|;\s*exit\s+0")
# Pruebas fuertes de que el comando falló. Una línea «XError: …» suelta no lo es: también sale al
# mostrar datos o logs. Python la acompaña de su Traceback y Node de las líneas «at …» de su pila.
HARD_FAIL_RE = re.compile(
    r"Traceback \(most recent call last\)|npm ERR!|error TS\d+|error\[E\d+\]|"
    r"^\s+at .+[(\s][^\s()]+:\d+:\d+\)?$|"
    r"panicked at|^FAILED\b|\b[1-9]\d* (failed|errors?)\b|Segmentation fault|command not found|"
    r"^\s*E\s+AssertionError", re.M)


def effective_exit_code(cmd: str, code, output: str):
    """El código real cuando el agente lo tapa con `; echo EXIT=$?`, `| tail` o `|| true`."""
    if code not in (0, None):
        return code, False
    m = re.findall(r"(?i)\bEXIT(?:_?CODE|_?STATUS)?\s*[=:]\s*(-?\d+)", output or "")
    if m and int(m[-1]) != 0:
        return int(m[-1]), False
    if MASKED_RE.search(cmd or "") and HARD_FAIL_RE.search(output or ""):
        return 1, True
    return code, False


def command_base(key: str) -> str:
    toks = key.split()
    if not toks:
        return ""
    prog = Path(toks[0]).name
    if prog in ("python", "python3") and len(toks) > 2 and toks[1] == "-m":
        return f"{prog} -m {toks[2]}"
    if prog in INTERPRETERS:  # el script cuenta: `python3 a.py` y `python3 b.py` son cosas distintas
        arg = next((t for t in toks[1:] if not t.startswith("-")), "")
        return f"{prog} {arg}".strip()
    if prog in MULTI_WORD and len(toks) > 1:
        if toks[1] in ("run", "exec", "x", "-m") and len(toks) > 2:
            return f"{prog} {toks[1]} {toks[2]}"
        return f"{prog} {toks[1]}"
    return prog


STRONG_ERROR_RE = re.compile(
    r"^\s*(E\s+)?[\w.]*(Error|Exception)\b:|\berror(\[\w+\])?\s*(TS\d+)?:|ERR!|^\s*FAILED\b|^\s*FAIL\b|"
    r"\bpanic:|\bfatal:|AssertionError|\bfailed\b.*\bassert", re.I)
EXIT_ONLY_RE = re.compile(r"^\s*exit(ed with)?[ _]?(code|status)\W+-?\d+\s*$", re.I)
NOISE_PREFIX_RE = re.compile(r"^\s*(Stdout|Stderr|Output|Error):\s*", re.I)


def error_excerpt(output: str, max_lines: int) -> tuple[str, str]:
    """Devuelve (línea principal del error, fragmento relevante de la salida)."""
    lines = [l.rstrip() for l in (output or "").splitlines() if l.strip()]
    lines = [l for l in lines if not EXIT_ONLY_RE.match(l)] or lines
    if not lines:
        return "", ""
    strong = next((i for i, l in enumerate(lines) if STRONG_ERROR_RE.search(l)), None)
    weak = next((i for i, l in enumerate(lines) if ERROR_LINE_RE.search(l)), None)
    idx = strong if strong is not None else weak
    if idx is None:
        main, chunk = lines[-1], lines[-max_lines:]
    else:
        start = min(weak if weak is not None else idx, idx)
        main, chunk = lines[idx], lines[max(0, start - 3): max(0, start - 3) + max_lines]
    return NOISE_PREFIX_RE.sub("", main).strip()[:300], "\n".join(chunk)[:4000]


def signature(base: str, main_line: str) -> str:
    norm = re.sub(r"(/[^\s:'\"]+)+", "<path>", main_line)
    norm = re.sub(r"0x[0-9a-fA-F]+|\d+(\.\d+)?", "N", norm)
    norm = re.sub(r"\s+", " ", norm).strip().lower()
    return hashlib.sha1(f"{base}|{norm}".encode()).hexdigest()[:10]


def find_exit_code(obj, depth: int = 3):
    """Busca el código de salida en la respuesta de la herramienta, sea JSON o texto."""
    if depth < 0 or obj is None:
        return None
    if isinstance(obj, bool):
        return None
    if isinstance(obj, dict):
        for k in ("exit_code", "exitCode", "returncode", "return_code", "status_code"):
            v = obj.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        for v in obj.values():
            r = find_exit_code(v, depth - 1)
            if r is not None:
                return r
        return None
    if isinstance(obj, list):
        for v in obj[:20]:
            r = find_exit_code(v, depth - 1)
            if r is not None:
                return r
        return None
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "{[":
            try:
                return find_exit_code(json.loads(s), depth - 1)
            except Exception:
                pass
        m = re.findall(r"(?im)^\W*exit(?:ed with)?[ _]?code\W+(-?\d+)|process exited with code (-?\d+)", s)
        if m:
            return int(m[-1][0] or m[-1][1])
    return None


def as_text(obj) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        parts = [str(obj[k]) for k in ("stdout", "stderr", "output", "llmContent", "error", "content", "result")
                 if obj.get(k)]
        return "\n".join(parts) if parts else json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, list):
        return "\n".join(as_text(x) for x in obj)
    return str(obj)


def patch_files(patch: str) -> list:
    return re.findall(r"(?m)^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch or "")


# --------------------------------------------------------------------------------------
# Adaptadores: convierten el JSON de cada agente en eventos comunes
#   {"type": "cmd", "command", "exit_code", "output"}
#   {"type": "edit", "files": [...]}
#   {"type": "stop", "active": bool}
# --------------------------------------------------------------------------------------

def adapt_claude(p: dict) -> list:
    ev = p.get("hook_event_name")
    tool, inp = p.get("tool_name"), p.get("tool_input") or {}
    if ev == "SessionStart":
        return [{"type": "session_start"}]
    if ev == "Stop":
        return [{"type": "stop", "active": bool(p.get("stop_hook_active"))}]
    if tool == "Bash" and not inp.get("run_in_background"):
        if ev == "PostToolUseFailure":
            if p.get("is_interrupt"):
                return []
            err = str(p.get("error") or "")
            code = find_exit_code(err)
            return [{"type": "cmd", "command": inp.get("command", ""), "exit_code": code if code is not None else 1,
                     "output": err}]
        if ev == "PostToolUse":
            resp = p.get("tool_response") or {}
            if isinstance(resp, dict) and resp.get("interrupted"):
                return []
            code = find_exit_code(resp)
            return [{"type": "cmd", "command": inp.get("command", ""), "exit_code": code or 0, "output": as_text(resp)}]
    if ev == "PostToolUse" and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        f = inp.get("file_path") or inp.get("notebook_path")
        return [{"type": "edit", "files": [f]}] if f else []
    return []


def adapt_codex(p: dict) -> list:
    ev = p.get("hook_event_name")
    tool, inp = p.get("tool_name"), p.get("tool_input") or {}
    if ev == "SessionStart":
        return [{"type": "session_start"}]
    if ev == "Stop":
        return [{"type": "stop", "active": bool(p.get("stop_hook_active"))}]
    if ev != "PostToolUse":
        return []
    if tool == "apply_patch":
        files = patch_files(inp.get("command") or inp.get("patch") or as_text(inp))
        return [{"type": "edit", "files": files}] if files else []
    if tool == "Bash":
        resp = p.get("tool_response")
        code = find_exit_code(resp)
        if code is None:
            code = 1 if isinstance(resp, dict) and resp.get("error") else 0
        cmd = inp.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd[2:] if cmd[:2] in (["bash", "-lc"], ["bash", "-c"], ["sh", "-c"]) else cmd))
        return [{"type": "cmd", "command": cmd or "", "exit_code": code, "output": as_text(resp)}]
    return []


def adapt_gemini(p: dict) -> list:
    ev = p.get("hook_event_name")
    tool, inp = p.get("tool_name"), p.get("tool_input") or {}
    if ev == "SessionStart":
        return [{"type": "session_start"}]
    if ev == "AfterAgent":
        return [{"type": "stop", "active": bool(p.get("stop_hook_active"))}]
    if ev != "AfterTool":
        return []
    if tool == "run_shell_command":
        resp = p.get("tool_response") or {}
        text = as_text(resp.get("llmContent") if isinstance(resp, dict) else resp)
        code = find_exit_code(text)
        if code is None:
            code = 1 if isinstance(resp, dict) and resp.get("error") else 0
        if isinstance(resp, dict) and resp.get("error"):
            text = f"{text}\n{as_text(resp['error'])}"
        return [{"type": "cmd", "command": inp.get("command", ""), "exit_code": code, "output": text}]
    if tool in ("write_file", "replace", "edit"):
        f = inp.get("file_path") or inp.get("absolute_path") or inp.get("path")
        return [{"type": "edit", "files": [f]}] if f else []
    return []


def adapt_cursor(p: dict) -> list:
    ev = p.get("hook_event_name")
    tool, inp = p.get("tool_name"), p.get("tool_input") or {}
    if ev == "sessionStart":
        return [{"type": "session_start"}]
    if ev == "stop":
        return [{"type": "stop", "active": int(p.get("loop_count") or 0) > 0}]
    if ev == "afterFileEdit":
        return [{"type": "edit", "files": [p["file_path"]]}] if p.get("file_path") else []
    if tool != "Shell":
        return []
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            inp = {"command": inp}
    cmd = inp.get("command", "") if isinstance(inp, dict) else ""
    if ev == "postToolUseFailure":
        if p.get("is_interrupt") or p.get("failure_type") == "permission_denied":
            return []
        err = str(p.get("error_message") or "")
        code = find_exit_code(err)
        return [{"type": "cmd", "command": cmd, "exit_code": code if code is not None else 1, "output": err}]
    if ev == "postToolUse":
        out = p.get("tool_output")
        code = find_exit_code(out)
        return [{"type": "cmd", "command": cmd, "exit_code": code or 0, "output": as_text(out)}]
    return []


def args_dict(args) -> dict:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return {"command": args}
    return args if isinstance(args, dict) else {}


def transcript_output(path, command: str, limit: int = 6000) -> str:
    """Antigravity no da la salida del comando al hook: se busca tras el comando en el final del transcript."""
    try:
        f = Path(path)
        if not command or not f.is_file():
            return ""
        with open(f, "rb") as fh:
            fh.seek(max(0, f.stat().st_size - 512_000))
            tail = fh.read().decode("utf-8", "replace")
        escaped = json.dumps(command, ensure_ascii=False)[1:-1]
        idx = max(tail.rfind(command), tail.rfind(escaped))
        if idx < 0:
            return ""
        after = tail[idx + len(command): idx + len(command) + limit]
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/", "r": ""}
        return re.sub(r'\\(u[0-9a-fA-F]{4}|[nt"\\/r])',
                      lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "u" else escapes[m.group(1)], after)
    except Exception:
        return ""


ANTIGRAVITY_EDIT_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content")


def adapt_antigravity(p: dict) -> list:
    """Antigravity CLI: el evento llega por --event (su payload no lo trae) y PostToolUse no incluye la salida."""
    ev = p.get("hook_event_name")
    if ev == "SessionStart":
        return [{"type": "session_start"}]
    if ev == "PostInvocation":
        return [{"type": "deliver"}]
    if ev == "Stop":
        return [{"type": "stop", "active": False}]
    if ev != "PostToolUse":
        return []
    call = p.get("toolCall") or {}
    tool, args = call.get("name"), args_dict(call.get("args"))
    if tool == "run_command":
        cmd = args.get("CommandLine") or args.get("command") or ""
        err = as_text(p.get("error")).strip()
        if not err:
            return [{"type": "cmd", "command": cmd, "exit_code": 0, "output": ""}]
        code = find_exit_code(err)
        if code is None:
            m = re.search(r"exit status (-?\d+)", err)
            code = int(m.group(1)) if m else 1
        output = f"{transcript_output(p.get('transcriptPath'), cmd)}\n{err}"
        return [{"type": "cmd", "command": cmd, "exit_code": code, "output": output}]
    if tool in ANTIGRAVITY_EDIT_TOOLS:
        f = args.get("TargetFile") or args.get("AbsolutePath") or args.get("file_path") or args.get("path")
        return [{"type": "edit", "files": [f]}] if f else []
    return []


SHELL_TOOLS = ("Bash", "Shell", "bash", "shell", "run_shell_command", "run_command", "terminal", "runInTerminal")


def adapt_agentplugins(p: dict) -> list:
    """Hooks portables del estándar Agent Plugins 1.1 (hooks.json en la raíz del plugin)."""
    ev = p.get("hook_event_name")
    if ev == "SessionStart":
        return [{"type": "session_start"}]
    if ev == "Stop":
        return [{"type": "stop", "active": bool(p.get("continuation_active"))}]
    if ev != "PostToolUse":
        return []
    profile, tool, inp = p.get("tool_profile"), p.get("tool_name"), args_dict(p.get("tool_input"))
    if profile == "shell" or tool in SHELL_TOOLS:
        result = p.get("tool_result")
        cmd = inp.get("command") or inp.get("CommandLine") or inp.get("cmd") or ""
        if isinstance(cmd, list):
            cmd = " ".join(map(str, cmd))
        code = find_exit_code(result)
        if code is None:
            code = 1 if isinstance(result, dict) and (result.get("error") or result.get("is_error")) else 0
        return [{"type": "cmd", "command": cmd, "exit_code": code, "output": as_text(result)}]
    if profile == "file.modify":
        f = inp.get("file_path") or inp.get("path") or inp.get("filePath") or inp.get("TargetFile")
        return [{"type": "edit", "files": [f]}] if f else []
    return []


ADAPTERS = {"claude": adapt_claude, "codex": adapt_codex, "gemini": adapt_gemini, "cursor": adapt_cursor,
            "antigravity": adapt_antigravity, "agentplugins": adapt_agentplugins}


def detect_agent(p: dict) -> str:
    """Para `--agent auto`: qué agente llama, por su payload y su entorno."""
    ev = p.get("hook_event_name") or ""
    if "toolCall" in p or "workspacePaths" in p or "conversationId" in p:
        return "antigravity"
    if "tool_profile" in p or "continuation_active" in p:
        return "agentplugins"
    if os.environ.get("CURSOR_VERSION") or "cursor_version" in p or "conversation_id" in p or ev[:1].islower():
        return "cursor"
    if ev in ("AfterTool", "AfterAgent", "BeforeTool", "BeforeAgent", "BeforeModel", "AfterModel") or \
            ("timestamp" in p and "permission_mode" not in p):
        return "gemini"
    if "turn_id" in p or (os.environ.get("PLUGIN_ROOT") and not os.environ.get("CLAUDE_PROJECT_DIR")):
        return "codex"
    return "claude"


# --------------------------------------------------------------------------------------
# Lógica: abrir, seguir y resolver errores
# --------------------------------------------------------------------------------------

def should_track(key: str, cfg: dict) -> bool:
    if not key:
        return False
    if cfg.get("only_commands"):
        return any(re.search(p, key) for p in cfg["only_commands"])
    return not any(re.search(p, key) for p in cfg.get("ignore_commands") or [])


def handle_cmd(store: Store, data: dict, ev: dict, agent: str, session: str | None) -> None:
    if re.search(r"errlog(\.py)?\b", ev["command"] or ""):
        return  # `errlog run` ya se registra a sí mismo
    key = command_key(ev["command"], store.config)
    if not should_track(key, store.config):
        return
    base = command_base(key)
    code, inferred = effective_exit_code(ev["command"], ev["exit_code"], ev.get("output", ""))
    errors = data["errors"]
    if code not in (0, None) and code != 130:
        main, excerpt = error_excerpt(ev.get("output", ""), store.config["max_excerpt_lines"])
        main, excerpt = scrub(main, store.root), scrub(excerpt, store.root)
        sig = signature(base, main)
        cur = next((e for e in errors.values() if e["status"] == "open" and e["cmd_key"] == key), None)
        if cur:
            cur["attempts"] += 1
            cur["last_failure_at"] = now()
            if sig not in cur["signatures"]:
                cur["signatures"].append(sig)
                cur["history"].append({"ts": now(), "error_line": main})
            cur["error_line_last"], cur["excerpt_last"] = main, excerpt
            store.event(kind="failure", id=cur["id"], agent=agent, exit_code=code, signature=sig)
            store.pending.append(("attempt", cur["id"]))
            return
        eid = f"E{data['next']:04d}"
        data["next"] += 1
        baseline = snapshot_tree(store)
        errors[eid] = {
            "id": eid, "status": "open", "agent": agent, "session": session, "opened_at": now(),
            "command": scrub(ev["command"], store.root)[:500], "cmd_key": key, "cmd_base": base,
            "exit_code": code, "exit_code_inferred": inferred, "error_line": main, "excerpt": excerpt, "signatures": [sig],
            "history": [{"ts": now(), "error_line": main}], "attempts": 1, "files_touched": [],
            "git_baseline": baseline,
        }
        store.event(kind="opened", id=eid, agent=agent, command=key, exit_code=code, signature=sig)
        store.pending.append(("report", eid))
        return
    # Éxito: resuelve los errores abiertos del mismo comando (o de la misma herramienta)
    for e in list(errors.values()):
        if e["status"] != "open":
            continue
        match = "exact" if e["cmd_key"] == key else ("same_tool" if e["cmd_base"] == base and base else None)
        if not match:
            continue
        e.update({"status": "resolved", "resolved_at": now(), "resolved_by_agent": agent,
                  "resolved_with": key, "match": match, "needs_note": True})
        e["fix"] = build_fix(store, e)
        store.event(kind="resolved", id=e["id"], agent=agent, command=key, match=match,
                    files=e["fix"].get("files"))
        store.pending.append(("resolve", e["id"]))


def snapshot_tree(store: Store) -> str | None:
    """Foto del árbol de trabajo, incluidos los archivos sin seguimiento, sin tocar el índice real.

    Usa un índice temporal copiado del real (así git reutiliza su caché y es rápido) y devuelve
    el hash del árbol. Respeta .gitignore.
    """
    root = store.root
    gitdir = git(["rev-parse", "--absolute-git-dir"], root)
    if not gitdir:
        return None
    tmp = store.dir / f".index-{os.getpid()}"
    try:
        real = Path(gitdir) / "index"
        if real.exists():
            shutil.copy2(real, tmp)
        env = dict(os.environ, GIT_INDEX_FILE=str(tmp))
        subprocess.run(["git", "add", "-A", "--", "."], cwd=root, env=env, capture_output=True, timeout=20)
        r = subprocess.run(["git", "write-tree"], cwd=root, env=env, capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def build_fix(store: Store, e: dict) -> dict:
    root, fix = store.root, {"files": sorted(set(e["files_touched"]))}
    base = e.get("git_baseline")
    cur = snapshot_tree(store) if base else None
    if not (base and cur):
        return fix
    paths = [p for p in fix["files"] if not os.path.isabs(p)]
    if fix["files"] and not paths:
        return fix
    stat = git(["diff", "--stat", base, cur, "--", *paths], root) or ""
    diff = git(["diff", base, cur, "--", *paths], root) or ""
    if not paths:  # sin hooks de edición (p. ej. `errlog run`): todos los cambios desde el error
        fix["files"] = (git(["diff", "--name-only", base, cur], root) or "").splitlines()
    if diff:
        lines = diff.splitlines()
        cap = store.config["max_patch_lines"]
        text = "\n".join(lines[:cap]) + (f"\n… {len(lines) - cap} líneas más" if len(lines) > cap else "")
        pdir = store.dir / "patches"
        pdir.mkdir(exist_ok=True)
        (pdir / f"{e['id']}.patch").write_text(scrub(text, root) + "\n")
        fix["patch"] = f"patches/{e['id']}.patch"
        fix["diff_stat"] = (stat or "").strip()[-1500:]
        fix["lines_changed"] = sum(1 for l in lines if l[:1] in "+-" and l[:3] not in ("+++", "---"))
    return fix


def handle_edit(store: Store, data: dict, ev: dict) -> None:
    files = []
    for f in ev.get("files") or []:
        p = Path(f)
        if p.is_absolute():
            try:
                f = str(p.resolve().relative_to(store.root))
            except Exception:
                f = str(p)
        files.append(f)
    opened = [e for e in data["errors"].values() if e["status"] == "open"]
    for e in opened:
        for f in files:
            if f not in e["files_touched"]:
                e["files_touched"].append(f)
    if opened and files:
        store.event(kind="edit", ids=[e["id"] for e in opened], files=files)


def stop_message(data: dict, store: Store) -> str | None:
    if not store.config.get("ask_for_notes"):
        return None
    pending = [e for e in data["errors"].values() if e.get("needs_note") and not e.get("asked")]
    if not pending:
        return None
    for e in pending:
        e["asked"] = True
    script = ".errlog/errlog.py" if Path(__file__).resolve().parent.name == ".errlog" else str(Path(__file__).resolve())
    lines = ["errlog: has resuelto estos errores. Registra en una línea la causa y la solución de cada uno, "
             "y no hagas nada más:"]
    for e in pending:
        lines.append(f"- {e['id']}: `{e['cmd_key'][:80]}` → {e['error_line'][:140]}")
    lines.append(f'Comando: python3 {script} note <ID> --cause "..." --fix "..."')
    return "\n".join(lines)


def stop_output(agent: str, msg: str | None) -> dict:
    if not msg:
        return {}
    if agent in ("claude", "codex"):
        return {"decision": "block", "reason": msg}
    if agent == "gemini":
        return {"decision": "deny", "reason": msg}
    if agent == "cursor":
        return {"followup_message": msg}
    if agent in ("antigravity", "agentplugins"):
        return {"decision": "continue", "reason": msg}
    return {}


# --------------------------------------------------------------------------------------
# AiFlowin: buscar el error en la red, publicarlo y enviar intentos y solución
#
# El agente no llama a nada: este script habla con la API con su propio token
# (~/.aiflowin/credentials). La búsqueda es síncrona (para poder darle la solución al
# agente); todo lo demás va a una cola que un proceso en segundo plano envía.
# --------------------------------------------------------------------------------------

LANGUAGES = [
    (r"^(python3?|pytest|pip3?|uv|poetry|pipenv|ruff|mypy|django-admin|flask|uvicorn|alembic)\b", "python"),
    (r"^(npm|pnpm|yarn|bun|bunx|npx|node|tsc|tsx|ts-node|vitest|jest|eslint|next|vite|deno|turbo|nx)\b", "javascript"),
    (r"^(cargo|rustc)\b", "rust"),
    (r"^go\b", "go"),
    (r"^(mvn|gradle|\./gradlew|java|javac)\b", "java"),
    (r"^dotnet\b", "csharp"),
    (r"^(ruby|bundle|rake|rails|rspec)\b", "ruby"),
    (r"^(php|composer|artisan)\b", "php"),
    (r"^(swift|xcodebuild)\b", "swift"),
]
KNOWN_STACK = {
    "next", "react", "vue", "nuxt", "svelte", "@sveltejs/kit", "astro", "express", "fastify", "@nestjs/core",
    "prisma", "drizzle-orm", "typeorm", "vite", "vitest", "jest", "tailwindcss", "typescript", "stripe",
    "@supabase/supabase-js", "next-auth", "django", "flask", "fastapi", "sqlalchemy", "pydantic", "pytest",
    "alembic", "celery", "numpy", "pandas", "torch", "rails", "laravel/framework", "spring-boot",
}


def aiflowin_enabled(store: Store) -> bool:
    return (store.config.get("aiflowin") or {}).get("enabled") is not False and CREDENTIALS.exists()


def connect_hint(store: Store) -> str | None:
    """Sin credencial (instalado como plugin o con setup.sh): pide al agente que conecte errlog,
    una sola vez cada `connect_hint_hours`. Es lo único que cuesta tokens antes de conectarse."""
    cfg = store.config.get("aiflowin") or {}
    if CREDENTIALS.exists() or cfg.get("enabled") is False or Path(__file__).resolve().parent.name == ".errlog":
        return None
    stamp = AIFLOWIN_HOME / ".connect-hint"
    hours = float(cfg.get("connect_hint_hours") or 20)
    if stamp.exists() and dt.datetime.now().timestamp() - stamp.stat().st_mtime < hours * 3600:
        return None
    AIFLOWIN_HOME.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    script = Path(__file__).resolve()
    return ("AiFlowin errlog está instalado pero sin conectar en esta máquina. Llama primero a `whoami` "
            "del MCP de AiFlowin. Solo si `access` es `full` e `issues_enabled` es `true`, llama a "
            "su herramienta `setup_errlog` y ejecuta "
            f'`python3 "{script}" connect --api <api_url> --code <code>` con los valores `api_url` y `code` '
            "que devuelve. En otro caso, sigue trabajando sin conectar errlog.")


def credentials() -> dict:
    return read_json_file(CREDENTIALS, {}) or {}


def api_request(method: str, path: str, body: dict | None, timeout: float, creds: dict | None = None) -> dict:
    creds = creds if creds is not None else credentials()
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"errlog/{VERSION}"}
    if creds.get("token"):
        headers["Authorization"] = f"Bearer {creds['token']}"
    req = urllib.request.Request(creds.get("api_url", "").rstrip("/") + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
    return json.loads(raw) if raw else {}


def detect_language(root: Path, cmd_base: str) -> str | None:
    for pattern, lang in LANGUAGES:
        if re.search(pattern, cmd_base or ""):
            if lang == "javascript" and (root / "tsconfig.json").exists():
                return "typescript"
            return lang
    return None


def detect_stack(store: Store) -> list:
    cache = store.dir / "stack.json"
    cached = read_json_file(cache)
    if isinstance(cached, list):
        return cached
    names: set = set()
    pkg = read_json_file(store.root / "package.json", {}) or {}
    for section in ("dependencies", "devDependencies"):
        names |= set((pkg.get(section) or {}).keys())
    for name in ("pyproject.toml", "requirements.txt", "Gemfile", "composer.json"):
        path = store.root / name
        if path.is_file() and path.stat().st_size < 200_000:
            names |= {m.lower() for m in re.findall(r"[\"']?([A-Za-z0-9_.@/\-]+)", path.read_text(errors="replace"))}
    stack = sorted(n for n in names if n.lower() in KNOWN_STACK)[:20]
    try:
        cache.write_text(json.dumps(stack))
    except Exception:
        pass
    return stack


def report_body(store: Store, e: dict, agent: str) -> dict:
    return {
        "error_line": e["error_line"] or e["cmd_key"], "excerpt": e["excerpt"], "command_base": e["cmd_base"],
        "language": detect_language(store.root, e["cmd_base"]), "stack": detect_stack(store),
        "environment": {"os": sys.platform, "machine": platform.machine(), "python": platform.python_version(),
                        "agent": agent, "errlog": VERSION},
        "local_ref": e["id"],
    }


def solution_context(store: Store, e: dict, result: dict) -> str | None:
    """Lo único que lee el agente: la solución más confirmada, breve, y la ruta del diff completo."""
    solutions = result.get("solutions") or []
    if not solutions:
        return None
    best = solutions[0]
    limit = int((store.config.get("aiflowin") or {}).get("max_context_chars") or 800)
    patch_path = None
    if best.get("diff"):
        sdir = store.dir / "solutions"
        sdir.mkdir(exist_ok=True)
        patch_path = sdir / f"{best['id']}.patch"
        patch_path.write_text(best["diff"])
    kind = "el mismo error" if result.get("match") == "exact" else "un error parecido"
    lines = [f"AiFlowin: {kind} ya está resuelto en la red "
             f"(confirmado {best.get('confirmations', 0)} veces). errlog {e['id']}: {e['error_line'][:160]}"]
    if best.get("cause"):
        lines.append(f"Causa: {best['cause']}")
    if best.get("fix"):
        lines.append(f"Solución: {best['fix']}")
    if best.get("files"):
        lines.append("Archivos: " + ", ".join(best["files"][:8]))
    if patch_path:
        lines.append(f"Diff completo de esa solución: {patch_path} (léelo solo si lo necesitas)")
    if not best.get("cause") and not best.get("fix") and best.get("diff"):
        lines.append("Diff (inicio):\n" + "\n".join(best["diff"].splitlines()[:20]))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def changed_lines(diff: str | None) -> list:
    return [re.sub(r"\s+", " ", line[1:]).strip() for line in (diff or "").splitlines()
            if line[:1] in "+-" and line[:3] not in ("+++", "---") and line[1:].strip()]


def applied_offered(store: Store, solution_id: str, patch: str | None, threshold: float = 0.6) -> bool:
    """¿El arreglo del agente se parece a la solución que se le ofreció? Solo entonces cuenta como
    confirmación; si no, su arreglo se publica como una solución nueva."""
    import difflib
    offered_path = store.dir / "solutions" / f"{solution_id}.patch"
    if not patch or not offered_path.exists():
        return False
    mine, theirs = changed_lines(patch), changed_lines(offered_path.read_text())
    if not mine or not theirs:
        return False
    return difflib.SequenceMatcher(None, "\n".join(mine), "\n".join(theirs)).ratio() >= threshold


def context_output(agent: str, event: str, message: str) -> dict:
    if agent in ("claude", "codex", "gemini"):
        return {"hookSpecificOutput": {"hookEventName": event or "PostToolUse", "additionalContext": message}}
    if agent in ("cursor", "agentplugins"):
        return {"additional_context": message}
    if agent == "antigravity" and event in ("SessionStart", "PostInvocation"):
        # PostToolUse no admite contexto en Antigravity: lo entrega PostInvocation (ver sync_pending).
        return {"injectSteps": [{"systemMessage": message}]}
    return {}


def enqueue(kind: str, store: Store, eid: str, body: dict) -> None:
    item = {"kind": kind, "data_dir": str(store.dir), "eid": eid, "body": body, "ts": now(), "tries": 0}
    AIFLOWIN_HOME.mkdir(parents=True, exist_ok=True)
    with locked(AIFLOWIN_HOME / ".outbox.lock"):
        with open(OUTBOX, "a") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")


@contextmanager
def locked(path: Path, blocking: bool = True):
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def spawn_flush() -> None:
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "flush"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except Exception:
        pass


def update_error(ddir: Path, eid: str, fields: dict) -> dict | None:
    """Guarda en el error local lo que devuelve AiFlowin (report_id, coincidencia, estado del envío)."""
    path = ddir / "errors.json"
    with locked(ddir / ".lock"):
        data = read_json_file(path)
        if not data or eid not in data.get("errors", {}):
            return None
        e = data["errors"][eid]
        e.setdefault("aiflowin", {}).update(fields)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(path)
        return e


def uninformative(e: dict) -> bool:
    """Sin el texto del error (p. ej. solo «exit status 1») no se puede buscar ni publicar: se queda en local."""
    return not (e.get("error_line") or "").strip() or bool(EXIT_ONLY_RE.match(e.get("error_line") or ""))


def sync_pending(store: Store, agent: str) -> list:
    """Después de guardar: busca los errores nuevos (síncrono) y encola el resto. Devuelve el contexto
    que hay que darle al agente."""
    work, store.pending = store.pending, []
    if not work or not aiflowin_enabled(store):
        return []
    data = store.load()
    contexts, queued = [], False
    timeout = float((store.config.get("aiflowin") or {}).get("report_timeout_s") or 3)
    for kind, eid in work:
        e = data["errors"].get(eid)
        if not e or uninformative(e):
            continue
        if kind == "report":
            body = report_body(store, e, agent)
            try:
                result = api_request("POST", "/v1/issues/report", body, timeout)
            except Exception as exc:  # sin red, lento o rechazado: lo reintenta la cola
                store.event(kind="aiflowin_report_deferred", id=eid, reason=type(exc).__name__)
                enqueue("report", store, eid, body)
                queued = True
                continue
            fields = {k: result.get(k) for k in ("report_id", "issue_id", "match")}
            solutions = result.get("solutions") or []
            fields["offered_solution_id"] = solutions[0]["id"] if solutions else None
            fields["solutions_offered"] = len(solutions)
            update_error(store.dir, eid, fields)
            store.event(kind="aiflowin_reported", id=eid, match=result.get("match"), solutions=len(solutions))
            message = solution_context(store, e, result)
            if message:
                contexts.append(message)
        elif kind == "attempt":
            enqueue("attempt", store, eid, {"error_line": e.get("error_line_last") or e["error_line"],
                                            "diff_stat": ", ".join(e.get("files_touched") or [])[:2000] or None,
                                            "outcome": "failed"})
            queued = True
        elif kind == "resolve":
            fix = e.get("fix") or {}
            patch = (store.dir / fix["patch"]).read_text() if fix.get("patch") and (store.dir / fix["patch"]).exists() else None
            offered = (read_json_file(store.errors_path, {}) or {}).get("errors", {}).get(eid, {}) \
                .get("aiflowin", {}).get("offered_solution_id")
            used = bool(offered) and applied_offered(store, offered, patch)
            enqueue("resolve", store, eid, {"diff": patch, "diff_stat": fix.get("diff_stat"),
                                            "files": fix.get("files") or [], "cause": e.get("cause"),
                                            "fix": e.get("solution"),
                                            "used_solution_id": offered if used else None})
            queued = True
        elif kind == "note" and (e.get("cause") or e.get("solution")):
            enqueue("note", store, eid, {"cause": e.get("cause"), "fix": e.get("solution")})
            queued = True
    if contexts and agent in ("cursor", "antigravity"):
        # Si Cursor no muestra el contexto del hook, se repite en el aviso al terminar el turno.
        with store.lock():
            data = store.load()
            data["pending_context"] = "\n\n".join(contexts)
            store.save(data)
    if queued:
        spawn_flush()
    return contexts


def retry_outbox(every_s: int = 60) -> None:
    """Si quedó algo en la cola (sin red, servidor caído), reintenta como mucho una vez por minuto."""
    try:
        if not OUTBOX.exists() or OUTBOX.stat().st_size == 0:
            return
        stamp = AIFLOWIN_HOME / ".flush.lock"
        if stamp.exists() and dt.datetime.now().timestamp() - stamp.stat().st_mtime < every_s:
            return
        spawn_flush()
    except Exception:
        pass


def cmd_flush(a) -> int:
    """Envía la cola. Un solo proceso a la vez; lo que falla por la red se reintenta en el siguiente."""
    if not CREDENTIALS.exists() or not OUTBOX.exists():
        return 0
    with locked(AIFLOWIN_HOME / ".flush.lock", blocking=False) as got:
        if not got:
            return 0
        os.utime(AIFLOWIN_HOME / ".flush.lock")
        with locked(AIFLOWIN_HOME / ".outbox.lock"):
            lines = OUTBOX.read_text().splitlines()
            OUTBOX.write_text("")
        items = [json.loads(line) for line in lines if line.strip()]
        keep, offline = [], False
        creds = credentials()
        for it in items:
            if offline or it.get("tries", 0) >= 30:
                if it.get("tries", 0) < 30:
                    keep.append(it)
                continue
            ddir = Path(it["data_dir"])
            e = (read_json_file(ddir / "errors.json", {}) or {}).get("errors", {}).get(it["eid"]) or {}
            meta = e.get("aiflowin") or {}
            try:
                if it["kind"] == "report":
                    result = api_request("POST", "/v1/issues/report", it["body"], 15, creds)
                    solutions = result.get("solutions") or []
                    update_error(ddir, it["eid"], {
                        "report_id": result.get("report_id"), "issue_id": result.get("issue_id"),
                        "match": result.get("match"), "solutions_offered": len(solutions),
                        "offered_solution_id": solutions[0]["id"] if solutions else None, "late": True})
                    continue
                report_id = meta.get("report_id")
                if not report_id:
                    pending_report = any(k["kind"] == "report" and k["eid"] == it["eid"] and k["data_dir"] == it["data_dir"]
                                         for k in keep)
                    if pending_report:
                        keep.append(it)
                    continue
                if it["kind"] == "attempt":
                    api_request("POST", f"/v1/issues/reports/{report_id}/attempts", it["body"], 15, creds)
                elif it["kind"] == "resolve":
                    body = dict(it["body"])
                    if not body.get("used_solution_id"):
                        body.pop("used_solution_id", None)
                    result = api_request("POST", f"/v1/issues/reports/{report_id}/resolve", body, 15, creds)
                    update_error(ddir, it["eid"], {"solution_id": result.get("solution_id"),
                                                   "confirmed": result.get("confirmed"), "resolved_synced": True})
                elif it["kind"] == "note":
                    api_request("PATCH", f"/v1/issues/reports/{report_id}/note", it["body"], 15, creds)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    it["tries"] = it.get("tries", 0) + 1
                    keep.append(it)
                    offline = True
                else:  # 4xx: no tiene arreglo reintentando (token revocado, report ajeno, datos no válidos)
                    log_hook_error(None, "flush", RuntimeError(f"{it['kind']} {it['eid']}: HTTP {exc.code}"))
            except Exception:
                it["tries"] = it.get("tries", 0) + 1
                keep.append(it)
                offline = True
        if keep:
            with locked(AIFLOWIN_HOME / ".outbox.lock"):
                newer = OUTBOX.read_text() if OUTBOX.exists() else ""
                OUTBOX.write_text("".join(json.dumps(k, ensure_ascii=False) + "\n" for k in keep) + newer)
    return 0


# --------------------------------------------------------------------------------------
# Informe
# --------------------------------------------------------------------------------------

def fmt_duration(a: str, b: str) -> str:
    try:
        s = int((dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds())
    except Exception:
        return "—"
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else (f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s")


def write_report(store: Store, data: dict) -> None:
    errs = sorted(data["errors"].values(), key=lambda e: e["opened_at"], reverse=True)
    opened = [e for e in errs if e["status"] == "open"]
    resolved = [e for e in errs if e["status"] == "resolved"]
    out = ["# Errores y soluciones", "",
           f"{len(errs)} errores · {len(resolved)} resueltos · {len(opened)} abiertos · "
           f"actualizado {now()}", ""]
    if opened:
        out += ["## Abiertos", ""]
        for e in opened:
            out += [f"### {e['id']} · `{e['cmd_key'][:100]}`", "",
                    f"- **Agente:** {e['agent']} · **Desde:** {e['opened_at']} · **Intentos:** {e['attempts']}",
                    f"- **Error:** {e.get('error_line_last') or e['error_line']}", ""]
    if resolved:
        out += ["## Resueltos", ""]
        for e in resolved:
            fix = e.get("fix") or {}
            out += [f"### {e['id']} · `{e['cmd_key'][:100]}`", "",
                    f"- **Agente:** {e['agent']} · **Intentos:** {e['attempts']} · "
                    f"**Tiempo:** {fmt_duration(e['opened_at'], e['resolved_at'])} · **Coincidencia:** {e.get('match')}",
                    f"- **Error:** {e['error_line']}",
                    f"- **Causa:** {e.get('cause') or '_pendiente_'}",
                    f"- **Solución:** {e.get('solution') or '_pendiente_'}",
                    f"- **Archivos:** {', '.join(fix.get('files') or []) or '—'}"]
            if fix.get("patch"):
                out.append(f"- **Diff:** [{fix['patch']}]({fix['patch']}) ({fix.get('lines_changed', 0)} líneas)")
            if len(e.get("history") or []) > 1:
                out.append("- **Evolución del error:** " + " → ".join(h["error_line"][:60] for h in e["history"]))
            out += ["", "<details><summary>Salida del error</summary>", "", "```", e["excerpt"], "```", "",
                    "</details>", ""]
    (store.dir / "ERRORES.md").write_text("\n".join(out) + "\n")


# --------------------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------------------

def cmd_hook(a) -> int:
    """Nunca rompe el agente: ante cualquier fallo interno, sale con 0 y JSON vacío."""
    out: dict = {}
    contexts: list = []
    root = None
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if a.event and not payload.get("hook_event_name"):
            payload["hook_event_name"] = a.event
        if a.agent == "auto":
            a.agent = detect_agent(payload)
        root = project_root(payload)
        store = Store(root)
        events = ADAPTERS[a.agent](payload)
        if a.debug or os.environ.get("ERRLOG_DEBUG"):
            with open(store.dir / "hook-payloads.jsonl", "a") as fh:
                fh.write(json.dumps({"agent": a.agent, "payload": payload}, ensure_ascii=False) + "\n")
        if any(ev["type"] == "session_start" for ev in events):
            hint = connect_hint(store)
            if hint:
                contexts.append(hint)
            events = [ev for ev in events if ev["type"] != "session_start"]
        if any(ev["type"] == "deliver" for ev in events):
            with store.lock():
                data = store.load()
                pending_ctx = data.pop("pending_context", None)
                if pending_ctx:
                    store.save(data)
                    contexts.append(pending_ctx)
            events = [ev for ev in events if ev["type"] != "deliver"]
        if events:
            session = payload.get("session_id") or payload.get("conversation_id") or payload.get("conversationId")
            with store.lock():
                data = store.load()
                changed = False
                for ev in events:
                    if ev["type"] == "cmd":
                        handle_cmd(store, data, ev, a.agent, session)
                        changed = True
                    elif ev["type"] == "edit":
                        handle_edit(store, data, ev)
                        changed = True
                    elif ev["type"] == "stop" and not ev["active"]:
                        msg = stop_message(data, store)
                        pending_ctx = data.pop("pending_context", None)
                        if a.agent == "cursor" and pending_ctx and not msg:
                            msg = pending_ctx
                        out = stop_output(a.agent, msg)
                        changed = True
                if changed:
                    store.save(data)
            contexts += sync_pending(store, a.agent)
        retry_outbox()
    except Exception as exc:  # noqa: BLE001
        log_hook_error(root, a.agent, exc)
        out = {}
    if contexts and not out:
        out = context_output(a.agent, payload.get("hook_event_name") or "", "\n\n".join(contexts))
    print(json.dumps(out))
    return 0


def log_hook_error(root, agent: str, exc: BaseException) -> None:
    try:
        target = data_dir(root) if root else AIFLOWIN_HOME
        target.mkdir(parents=True, exist_ok=True)
        with open(target / "hook-errors.log", "a") as fh:
            fh.write(f"{now()} {agent} {type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def cmd_run(a) -> int:
    if not a.command:
        print("Uso: errlog run -- <comando>", file=sys.stderr)
        return 2
    cmd = a.command[1:] if a.command[0] == "--" else a.command
    shell_cmd = " ".join(cmd) if len(cmd) > 1 else cmd[0]
    proc = subprocess.Popen(shell_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            errors="replace")
    collected = []
    for line in proc.stdout:
        sys.stdout.write(line)
        collected.append(line)
        if len(collected) > 5000:
            collected = collected[-3000:]
    code = proc.wait()
    store = Store(project_root())
    with store.lock():
        data = store.load()
        handle_cmd(store, data, {"type": "cmd", "command": shell_cmd, "exit_code": code, "output": "".join(collected)},
                   a.agent or os.environ.get("ERRLOG_AGENT", "run"), None)
        store.save(data)
    for message in sync_pending(store, a.agent or "run"):
        print("\n" + message, file=sys.stderr)
    return code


def cmd_note(a) -> int:
    store = Store(project_root())
    with store.lock():
        data = store.load()
        e = data["errors"].get(a.id.upper())
        if not e:
            print(f"No existe el error {a.id}", file=sys.stderr)
            return 1
        if a.cause:
            e["cause"] = a.cause.strip()
        if a.fix:
            e["solution"] = a.fix.strip()
        e["needs_note"] = not (e.get("cause") and e.get("solution"))
        e["noted_at"] = now()
        store.event(kind="note", id=e["id"])
        store.pending.append(("note", e["id"]))
        store.save(data)
    sync_pending(store, "note")
    print(f"Anotado {e['id']}.")
    return 0


def cmd_list(a) -> int:
    data = Store(project_root()).load()
    rows = sorted(data["errors"].values(), key=lambda e: e["opened_at"])
    if a.open:
        rows = [e for e in rows if e["status"] == "open"]
    if not rows:
        print("Sin errores registrados.")
    for e in rows:
        mark = "●" if e["status"] == "open" else ("✓" if not e.get("needs_note") else "✓?")
        print(f"{mark} {e['id']}  {e['agent']:7} {e['attempts']:>2} int.  {e['cmd_key'][:50]:50}  {e['error_line'][:70]}")
    return 0


def cmd_show(a) -> int:
    store = Store(project_root())
    e = store.load()["errors"].get(a.id.upper())
    if not e:
        print(f"No existe el error {a.id}", file=sys.stderr)
        return 1
    print(json.dumps(e, indent=2, ensure_ascii=False))
    patch = (e.get("fix") or {}).get("patch")
    if patch and (store.dir / patch).exists():
        print("\n" + (store.dir / patch).read_text())
    return 0


def cmd_report(a) -> int:
    store = Store(project_root())
    write_report(store, store.load())
    print(store.dir / "ERRORES.md")
    return 0


# --------------------------------------------------------------------------------------
# Instalación
# --------------------------------------------------------------------------------------

HOOK_CMDS = {
    "claude": 'python3 "$CLAUDE_PROJECT_DIR/.errlog/errlog.py" hook --agent claude',
    "codex": 'python3 "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.errlog/errlog.py" hook --agent codex',
    "gemini": 'python3 "$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.errlog/errlog.py" hook --agent gemini',
    "cursor": "python3 .errlog/errlog.py hook --agent cursor",
}

AGENT_RULES = """
## Registro de errores (errlog)

Los hooks de este proyecto registran automáticamente los comandos que fallan y su solución en
`.errlog/log/ERRORES.md`. Trabaja con normalidad; no hace falta usar errlog para ejecutar comandos.

- Cuando errlog te pida la causa y la solución de un error, anótalas en una línea cada una:
  `python3 .errlog/errlog.py note <ID> --cause "..." --fix "..."`
- Para ver los errores abiertos: `python3 .errlog/errlog.py list --open`.
"""

AGENT_RULES_NO_HOOKS = """
- Solo si tu herramienta no tiene hooks de errlog configurados, ejecuta tests y build así para registrarlos:
  `python3 .errlog/errlog.py run -- <comando>`
"""


def merge_json(path: Path, update) -> bool:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text() or "{}")
        except Exception:
            print(f"  ✗ {path} no es JSON válido; no lo toco.")
            return False
    new = update(json.loads(json.dumps(data)))
    if new == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    return True


def add_hook(cfg: dict, event: str, matcher: str | None, command: str, extra: dict | None = None,
             flat: bool = False) -> dict:
    hooks = cfg.setdefault("hooks", {})
    groups = hooks.setdefault(event, [])
    existing = [h.get("command") for g in groups if isinstance(g, dict) for h in (g.get("hooks") or [g])
                if isinstance(h, dict)]
    if command in existing:
        return cfg
    if flat:  # formato de Cursor: lista plana de {command, matcher}
        entry = {"command": command, **(extra or {})}
        if matcher:
            entry["matcher"] = matcher
        groups.append(entry)
    else:
        group = {"hooks": [{"type": "command", "command": command, **(extra or {})}]}
        if matcher:
            group = {"matcher": matcher, **group}
        groups.append(group)
    return cfg


def install_agent(agent: str, root: Path, command: str | None = None) -> str:
    """Hooks de un agente en <root>/.claude, .codex, .gemini o .cursor. Con root = $HOME son los del usuario."""
    c = command or HOOK_CMDS[agent]
    if agent == "claude":
        def up(cfg):
            add_hook(cfg, "PostToolUse", "Bash|Edit|Write|MultiEdit|NotebookEdit", c, {"timeout": 15})
            add_hook(cfg, "PostToolUseFailure", "Bash", c, {"timeout": 15})
            add_hook(cfg, "Stop", None, c, {"timeout": 15})
            add_hook(cfg, "SessionStart", "startup", c, {"timeout": 15})
            return cfg
        path = root / ".claude" / "settings.json"
    elif agent == "codex":
        def up(cfg):
            add_hook(cfg, "PostToolUse", "^(Bash|apply_patch)$", c, {"timeout": 15})
            add_hook(cfg, "Stop", None, c, {"timeout": 15})
            add_hook(cfg, "SessionStart", None, c, {"timeout": 15})
            return cfg
        path = (codex_home() if root == Path.home() else root / ".codex") / "hooks.json"
    elif agent == "gemini":
        def up(cfg):
            add_hook(cfg, "AfterTool", "run_shell_command|write_file|replace", c, {"name": "errlog", "timeout": 15000})
            add_hook(cfg, "AfterAgent", None, c, {"name": "errlog", "timeout": 15000})
            add_hook(cfg, "SessionStart", None, c, {"name": "errlog", "timeout": 15000})
            return cfg
        path = root / ".gemini" / "settings.json"
    else:
        def up(cfg):
            cfg.setdefault("version", 1)
            add_hook(cfg, "postToolUse", "Shell", c, {"timeout": 15}, flat=True)
            add_hook(cfg, "postToolUseFailure", "Shell", c, {"timeout": 15}, flat=True)
            add_hook(cfg, "afterFileEdit", None, c, {"timeout": 15}, flat=True)
            add_hook(cfg, "stop", None, c, {"timeout": 15, "loop_limit": 1}, flat=True)
            add_hook(cfg, "sessionStart", None, c, {"timeout": 15}, flat=True)
            return cfg
        path = root / ".cursor" / "hooks.json"
    changed = merge_json(path, up)
    return f"{path.relative_to(root)} {'actualizado' if changed else 'sin cambios'}"


def cmd_install(a) -> int:
    root = Path(a.project).expanduser().resolve()
    agents = [x.strip() for x in (a.agents or ",".join(AGENTS)).split(",") if x.strip()]
    bad = [x for x in agents if x not in AGENTS]
    if bad:
        print(f"Agentes desconocidos: {', '.join(bad)}. Opciones: {', '.join(AGENTS)}", file=sys.stderr)
        return 2
    d = root / ".errlog"
    d.mkdir(parents=True, exist_ok=True)
    me = Path(__file__).resolve()
    if me != (d / "errlog.py").resolve():
        shutil.copy2(me, d / "errlog.py")
    (d / "errlog.py").chmod(0o755)
    (d / ".gitignore").write_text("log/\n")
    if not (d / "config.json").exists():
        (d / "config.json").write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n")
    print(f"✓ .errlog/ instalado en {root}")
    for ag in agents:
        print(f"✓ {ag}: {install_agent(ag, root)}")
    if not a.no_rules:
        for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            p = root / name
            if name == "CLAUDE.md" and "claude" not in agents:
                continue
            if name == "GEMINI.md" and ("gemini" not in agents or not p.exists()):
                continue
            text = p.read_text() if p.exists() else ""
            if "errlog" not in text:
                rules = AGENT_RULES + (AGENT_RULES_NO_HOOKS if name == "AGENTS.md" else "")
                p.write_text(text.rstrip() + ("\n" if text else "") + rules)
                print(f"✓ Regla añadida a {name}")
    if not git(["rev-parse", "--is-inside-work-tree"], root):
        print("! El proyecto no es un repositorio git: los errores se registran, pero sin diff de la solución.")
    if "codex" in agents:
        print("! Codex: revisa y confía en el hook con /hooks (los hooks nuevos no se ejecutan hasta entonces).")
    if "gemini" in agents:
        print("! Gemini: si tu versión no ejecuta hooks, actívalos en la configuración (versiones antiguas).")
    print("Prueba: python3 .errlog/errlog.py list")
    return 0


AGENT_DIRS = {"claude": ".claude", "codex": ".codex", "gemini": ".gemini", "cursor": ".cursor"}
AGENT_FILES = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json",
               "gemini": ".gemini/settings.json", "cursor": ".cursor/hooks.json"}


def global_command(agent: str) -> str:
    return f'python3 "{AIFLOWIN_HOME / "errlog.py"}" hook --agent {agent}'


def exchange_code(api_url: str, code: str) -> dict:
    result = api_request("POST", "/v1/scripts/exchange", {"code": code}, 20, {"api_url": api_url})
    return {"api_url": result.get("api_url") or api_url, "token": result["token"], "token_id": result["token_id"],
            "agent": result.get("agent"), "workspace": result.get("workspace"), "created_at": now()}


def plugin_errlog(agent: str) -> Path | None:
    """errlog del plugin de AiFlowin, si está instalado para ese agente: sus hooks ya están puestos."""
    home = Path.home()
    patterns = {
        "claude": [(home / ".claude" / "plugins", "cache/*/aiflowin/*/errlog.py")],
        "codex": [(codex_home() / "plugins", "cache/*/aiflowin/*/errlog.py")],
        "gemini": [(home / ".gemini" / "extensions", "aiflowin/errlog.py")],
        "cursor": [(home / ".cursor" / "plugins", "local/aiflowin/errlog.py"),
                   (home / ".cursor" / "plugins", "cache/*/aiflowin/*/errlog.py")],
    }
    for base, pattern in patterns.get(agent, []):
        found = sorted(base.glob(pattern)) if base.is_dir() else []
        if found:
            return found[-1]
    return None


def cmd_install_global(a) -> int:
    """Instalación para el usuario: un script, una credencial y los hooks de cada agente. Ningún repo cambia."""
    home = Path.home()
    agents = [x.strip() for x in a.agents.split(",") if x.strip()] if a.agents else \
        [ag for ag, d in AGENT_DIRS.items() if (home / d).is_dir()]
    bad = [x for x in agents if x not in AGENTS]
    if bad:
        print(f"Agentes desconocidos: {', '.join(bad)}. Opciones: {', '.join(AGENTS)}", file=sys.stderr)
        return 2
    AIFLOWIN_HOME.mkdir(parents=True, exist_ok=True)
    AIFLOWIN_HOME.chmod(0o700)
    if a.code:
        if not a.api:
            print("Falta --api", file=sys.stderr)
            return 2
        creds = save_credentials(a.api, a.code)
        if not creds:
            return 1
        print(f"✓ Conectado a AiFlowin como «{(creds.get('agent') or {}).get('name')}» "
              f"({(creds.get('workspace') or {}).get('name')})")
    elif not CREDENTIALS.exists():
        print("· Sin conectar todavía: al abrir una sesión, el agente lo conectará con el MCP (setup_errlog).")
    me = Path(__file__).resolve()
    target = AIFLOWIN_HOME / "errlog.py"
    if me != target.resolve():
        shutil.copy2(me, target)
    target.chmod(0o755)
    cfg_path = AIFLOWIN_HOME / "config.json"
    cfg = read_json_file(cfg_path, {}) or {}
    cfg.setdefault("ask_for_notes", False)
    cfg.setdefault("aiflowin", {}).pop("enabled", None)  # activo en cuanto hay credencial
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"✓ errlog {VERSION} en {target}")
    for ag in agents:
        if plugin_errlog(ag):
            # El plugin ya trae el MCP y los hooks: otra copia haría que cada hook corriera dos veces.
            print(f"✓ {ag}: plugin de AiFlowin ya instalado; usa esta conexión, sin añadir hooks")
            continue
        line = install_agent(ag, home, global_command(ag))
        if a.mcp_url:
            line += " · " + register_mcp(ag, a.mcp_url)
        print(f"✓ {ag}: {line}")
    if not agents:
        print("! No encontré ningún agente (~/.claude, ~/.codex, ~/.gemini, ~/.cursor). Usa --agents.")
    if "codex" in agents:
        print("! Codex: abre /hooks y confía en el hook de errlog (Codex no ejecuta hooks nuevos hasta entonces).")
    if CREDENTIALS.exists():
        print("Listo. errlog está conectado a AiFlowin.")
    else:
        print("MCP y hooks instalados. Inicia sesión en AiFlowin; errlog se conectará cuando tengas acceso.")
    return 0


def save_credentials(api_url: str, code: str) -> dict | None:
    """Canjea el código de setup_errlog y guarda la credencial. Devuelve None (y lo explica) si falla."""
    try:
        creds = exchange_code(api_url.rstrip("/"), code)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read() or b"{}").get("detail", "")
        except Exception:
            pass
        print(f"✗ AiFlowin rechazó el código ({exc.code}). {detail}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"✗ No se pudo contactar con AiFlowin en {api_url}: {exc}", file=sys.stderr)
        return None
    AIFLOWIN_HOME.mkdir(parents=True, exist_ok=True)
    AIFLOWIN_HOME.chmod(0o700)
    CREDENTIALS.write_text(json.dumps(creds, indent=2) + "\n")
    CREDENTIALS.chmod(0o600)
    return creds


def cmd_connect(a) -> int:
    """Solo conecta (credencial): para cuando errlog ya está instalado como plugin o con setup.sh."""
    creds = save_credentials(a.api, a.code)
    if not creds:
        return 1
    print(f"✓ errlog conectado a AiFlowin como «{(creds.get('agent') or {}).get('name')}». "
          "Los errores se buscan y se comparten sin gastar tokens del agente.")
    return 0


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def register_mcp(agent: str, url: str) -> str:
    """Añade el servidor MCP de AiFlowin a la configuración de usuario del agente, sin duplicarlo."""
    home = Path.home()
    if agent == "claude":
        if not shutil.which("claude"):
            return "MCP: no encuentro el comando claude; añádelo con: claude mcp add --scope user --transport http aiflowin " + url
        if subprocess.run(["claude", "mcp", "get", "aiflowin"], capture_output=True, timeout=30).returncode == 0:
            return "MCP aiflowin ya estaba"
        r = subprocess.run(["claude", "mcp", "add", "--scope", "user", "--transport", "http", "aiflowin", url],
                           capture_output=True, text=True, timeout=30)
        return "MCP aiflowin añadido" if r.returncode == 0 else f"MCP: claude mcp add falló: {r.stderr.strip()[:200]}"
    if agent == "codex":
        path = codex_home() / "config.toml"
        text = path.read_text() if path.exists() else ""
        if re.search(r"(?m)^\[mcp_servers\.aiflowin\]", text):
            return "MCP aiflowin ya estaba"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(".toml.bak"))
        path.write_text(text.rstrip() + ("\n\n" if text.strip() else "") + f'[mcp_servers.aiflowin]\nurl = "{url}"\n')
        return "MCP aiflowin añadido (inicia sesión con: codex mcp login aiflowin)"
    if agent == "cursor":
        changed = merge_json(home / ".cursor" / "mcp.json",
                             lambda cfg: (cfg.setdefault("mcpServers", {}).setdefault("aiflowin", {"url": url}), cfg)[1])
        return "MCP aiflowin añadido" if changed else "MCP aiflowin ya estaba"
    if agent == "gemini":
        changed = merge_json(home / ".gemini" / "settings.json",
                             lambda cfg: (cfg.setdefault("mcpServers", {}).setdefault("aiflowin", {"httpUrl": url}), cfg)[1])
        return "MCP aiflowin añadido" if changed else "MCP aiflowin ya estaba"
    return ""


def cmd_uninstall(a) -> int:
    home = Path.home()
    marker = str(AIFLOWIN_HOME / "errlog.py")
    for ag, rel in AGENT_FILES.items():
        path = home / rel

        def strip(cfg, marker=marker):
            for event, groups in list((cfg.get("hooks") or {}).items()):
                kept = [g for g in groups if marker not in json.dumps(g)]
                if kept:
                    cfg["hooks"][event] = kept
                else:
                    del cfg["hooks"][event]
            return cfg
        if path.exists() and merge_json(path, strip):
            print(f"✓ Hooks quitados de {path}")
    if CREDENTIALS.exists():
        CREDENTIALS.unlink()
        print("✓ Credencial borrada (revócala también en la app de AiFlowin si quieres).")
    print(f"Los registros locales siguen en {AIFLOWIN_HOME / 'projects'}.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="errlog", description="Registro de errores y soluciones de agentes de código.")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="Instala errlog y configura los hooks")
    p.add_argument("--global", dest="global_", action="store_true",
                   help="Para el usuario (~/.aiflowin), sin tocar ningún proyecto")
    p.add_argument("--api", help="URL de la API de AiFlowin (con --global)")
    p.add_argument("--code", help="Código de un solo uso de setup_errlog (con --global)")
    p.add_argument("--mcp-url", help="Registra también el servidor MCP de AiFlowin en cada agente (con --global)")
    p.add_argument("--project", default=".")
    p.add_argument("--agents", default=None, help="Lista separada por comas: " + ",".join(AGENTS))
    p.add_argument("--no-rules", action="store_true", help="No añade la regla a AGENTS.md / CLAUDE.md")
    p.set_defaults(func=lambda a: cmd_install_global(a) if a.global_ else cmd_install(a))

    p = sub.add_parser("connect", help="Conecta errlog con AiFlowin (código de setup_errlog)")
    p.add_argument("--api", required=True, help="URL de la API de AiFlowin")
    p.add_argument("--code", required=True, help="Código de un solo uso de setup_errlog")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("uninstall", help="Quita los hooks globales y la credencial")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("flush", help="Envía a AiFlowin lo que quede en la cola")
    p.set_defaults(func=cmd_flush)

    p = sub.add_parser("hook", help="Entrada de los hooks (JSON por stdin)")
    p.add_argument("--agent", required=True, choices=HOOK_AGENTS + ("auto",))
    p.add_argument("--event", help="Nombre del evento, para agentes cuyo payload no lo trae (Antigravity)")
    p.add_argument("--debug", action="store_true", help="Guarda cada payload en log/hook-payloads.jsonl")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("run", help="Ejecuta un comando y registra el resultado")
    p.add_argument("--agent", help="Nombre del agente para el registro")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("note", help="Añade causa y solución a un error")
    p.add_argument("id")
    p.add_argument("--cause")
    p.add_argument("--fix")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("list", help="Lista los errores")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="Muestra un error y su diff")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("report", help="Regenera ERRORES.md")
    p.set_defaults(func=cmd_report)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
