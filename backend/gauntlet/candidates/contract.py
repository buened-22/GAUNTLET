"""Candidate contract (version 1): manifest, layout rules, static validation and canonical hashing.

Nothing in this module imports or executes candidate code. Validation is purely static (file system
inspection, JSON parsing and ``ast.parse``); passing it does not make a candidate trustworthy.

Layout
------
A candidate is a directory containing ``gauntlet_candidate.json`` and Python source files::

    {
      "contract_version": "1",
      "name": "my-candidate",                       # ^[a-z0-9][a-z0-9._-]{0,63}$
      "entry": {"module": "candidate", "class": "Implementation"},
      "strategy": "A",                              # A: one authoritative column kept in sync on every write
                                                    # B: both columns written by the new version, reconciled
      "description": "...",                         # optional
      "producer": {...}                             # optional, recorded as *claimed* metadata only
    }

The entry class provides the new-version and migration operations of the existing implementation
contract (``gauntlet/impls/base.py``)::

    v2_create_order, v2_set_total, v2_add_charge, v2_get_order,
    migrator_expand, migrator_backfill_batch, migrator_finalize, migrator_contract

It must NOT define ``v1_*`` or ``app_*`` methods: the legacy application (V1) and instance registration
are harness-owned and identical for every implementation (legacy code cannot change during a rolling
migration). The loader composes the candidate class with them.

Allowed imports: the Python standard library except process, network, FFI and file-tree modules (see
``FORBIDDEN_IMPORTS``), ``gauntlet.impls.base`` (AppError, Db, ImplementationBase, claim_key,
store_response, current_stage), ``gauntlet.money``, and the candidate's own modules. Importing anything
else from ``gauntlet`` (references, mutants, oracle, scenarios) or a database driver is rejected.

Canonical source hash
---------------------
Files are collected recursively. Directories named ``__pycache__`` or ``.git`` and files named
``*.pyc``, ``*.pyo`` or ``.DS_Store`` are excluded (and listed as excluded). Every other file must be a
regular file (no symbolic link, junction or other reparse point) with an allowed extension, UTF-8 text
(a leading BOM is removed); line endings are normalized to LF. File metadata (timestamps, permissions,
owners) is ignored. The hash is SHA-256 over the canonical JSON
``{"contract_version", "files": [[relative/posix/path, sha256(normalized bytes), length], ...]}`` with
files sorted by path. Directory names, display names and file order therefore do not affect the hash;
any content change does.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from gauntlet.candidates import CONTRACT_VERSION

MANIFEST = "gauntlet_candidate.json"
SUPPORTED_CONTRACT_VERSIONS = (CONTRACT_VERSION,)
ALLOWED_EXTENSIONS = (".py", ".json", ".md", ".txt")
EXCLUDED_DIRS = ("__pycache__", ".git")
EXCLUDED_FILES = (".DS_Store",)
EXCLUDED_SUFFIXES = (".pyc", ".pyo")

LIMITS = {
    "max_files": 64,
    "max_file_bytes": 256 * 1024,
    "max_total_bytes": 1024 * 1024,
    "max_depth": 4,
    "max_manifest_bytes": 16 * 1024,
}

REQUIRED_METHODS = (
    "v2_create_order", "v2_set_total", "v2_add_charge", "v2_get_order",
    "migrator_expand", "migrator_backfill_batch", "migrator_finalize", "migrator_contract",
)
HARNESS_OWNED_PREFIXES = ("v1_", "app_")
ALLOWED_GAUNTLET_MODULES = ("gauntlet.impls.base", "gauntlet.money")
FORBIDDEN_IMPORTS = (
    "subprocess", "socket", "ssl", "ctypes", "_ctypes", "multiprocessing", "concurrent", "asyncio",
    "threading", "_thread", "signal", "shutil", "tempfile", "pty", "urllib", "http", "ftplib", "smtplib",
    "telnetlib", "xmlrpc", "socketserver", "webbrowser", "winreg", "_winapi", "msvcrt", "mmap", "importlib",
    "runpy", "code", "codeop", "pickle", "marshal", "psycopg", "psycopg2", "sqlite3",
)
MANIFEST_KEYS = {"contract_version", "name", "entry", "strategy", "description", "producer"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass
class Validation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    manifest: dict | None = None
    files: list[dict] = field(default_factory=list)       # {"path", "sha256", "bytes"}
    excluded: list[str] = field(default_factory=list)
    source_hash: str | None = None
    contents: dict[str, bytes] = field(default_factory=dict, repr=False)  # normalized bytes by path

    def to_dict(self) -> dict:
        return {"ok": self.ok, "problems": self.problems, "manifest": self.manifest, "files": self.files,
                "excluded": self.excluded, "source_hash": self.source_hash}


def _is_link(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE)  # Windows junctions / reparse points


def normalize_text(raw: bytes) -> bytes:
    """UTF-8 (BOM removed), LF line endings. Raises UnicodeDecodeError for non-UTF-8 content."""
    text = raw.decode("utf-8")
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def tree_hash(files: list[dict]) -> str:
    doc = {"contract_version": CONTRACT_VERSION,
           "files": [[f["path"], f["sha256"], f["bytes"]] for f in sorted(files, key=lambda f: f["path"])]}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def collect(root: Path, limits: dict | None = None) -> Validation:
    """Collect and canonicalize files under ``root`` (no code is executed)."""
    lim = {**LIMITS, **(limits or {})}
    v = Validation(ok=False)
    try:
        root = Path(root)
        if _is_link(root) or not root.is_dir():
            v.problems.append("candidate path is not a regular directory (or is a link)")
            return v
        root = root.resolve(strict=True)
    except OSError as exc:
        v.problems.append(f"candidate directory is not accessible: {exc.strerror}")
        return v
    total = 0
    seen_folded: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root)
        if len(rel_dir.parts) > lim["max_depth"]:
            v.problems.append(f"directory nesting deeper than {lim['max_depth']}: {rel_dir.as_posix()}")
            dirnames[:] = []
            continue
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix()
            if d in EXCLUDED_DIRS:
                v.excluded.append(rel + "/")
            elif _is_link(here / d):
                v.problems.append(f"{rel}: directory link / junction is not allowed")
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            p = here / name
            rel = (rel_dir / name).as_posix()
            if name in EXCLUDED_FILES or name.endswith(EXCLUDED_SUFFIXES):
                v.excluded.append(rel)
                continue
            if _is_link(p):
                v.problems.append(f"{rel}: symbolic link / reparse point is not allowed")
                continue
            try:
                real = p.resolve(strict=True)
                real.relative_to(root)
            except (OSError, ValueError):
                v.problems.append(f"{rel}: resolves outside the candidate directory")
                continue
            if not stat.S_ISREG(os.lstat(p).st_mode):
                v.problems.append(f"{rel}: not a regular file")
                continue
            if not name.endswith(ALLOWED_EXTENSIONS):
                v.problems.append(f"{rel}: file type not allowed (allowed: {', '.join(ALLOWED_EXTENSIONS)})")
                continue
            folded = rel.casefold()
            if folded in seen_folded:
                v.problems.append(f"{rel}: differs only in case from {seen_folded[folded]}")
                continue
            seen_folded[folded] = rel
            size = os.lstat(p).st_size
            if size > lim["max_file_bytes"]:
                v.problems.append(f"{rel}: {size} bytes exceeds the per-file limit of {lim['max_file_bytes']}")
                continue
            total += size
            if total > lim["max_total_bytes"]:
                v.problems.append(f"candidate exceeds the total size limit of {lim['max_total_bytes']} bytes")
                return v
            if len(v.files) >= lim["max_files"]:
                v.problems.append(f"candidate has more than {lim['max_files']} files")
                return v
            try:
                data = normalize_text(p.read_bytes())
            except UnicodeDecodeError:
                v.problems.append(f"{rel}: not UTF-8 text")
                continue
            v.contents[rel] = data
            v.files.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    v.files.sort(key=lambda f: f["path"])
    return v


def _check_manifest(v: Validation) -> None:
    raw = v.contents.get(MANIFEST)
    if raw is None:
        v.problems.append(f"missing {MANIFEST}")
        return
    if len(raw) > LIMITS["max_manifest_bytes"]:
        v.problems.append(f"{MANIFEST} is larger than {LIMITS['max_manifest_bytes']} bytes")
        return
    try:
        m = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        v.problems.append(f"{MANIFEST} is not valid JSON: {exc}")
        return
    if not isinstance(m, dict):
        v.problems.append(f"{MANIFEST} must be a JSON object")
        return
    unknown = sorted(set(m) - MANIFEST_KEYS)
    if unknown:
        v.problems.append(f"{MANIFEST}: unknown keys {unknown}")
    cv = m.get("contract_version")
    if cv not in SUPPORTED_CONTRACT_VERSIONS:
        v.problems.append(f"unsupported contract_version {cv!r} (supported: {list(SUPPORTED_CONTRACT_VERSIONS)})")
    if not isinstance(m.get("name"), str) or not NAME_RE.match(m["name"]):
        v.problems.append("name must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    e = m.get("entry")
    if not (isinstance(e, dict) and set(e) == {"module", "class"} and isinstance(e.get("module"), str)
            and isinstance(e.get("class"), str) and IDENT_RE.match(e["class"])
            and all(IDENT_RE.match(part) for part in e["module"].split("."))):
        v.problems.append('entry must be {"module": "<dotted.module>", "class": "<ClassName>"}')
    if m.get("strategy") not in ("A", "B"):
        v.problems.append('strategy must be "A" or "B"')
    for k in ("description",):
        if k in m and not isinstance(m[k], str):
            v.problems.append(f"{k} must be a string")
    if "producer" in m and not isinstance(m["producer"], dict):
        v.problems.append("producer must be an object")
    v.manifest = m


def _module_path(module: str) -> str:
    return module.replace(".", "/") + ".py"


def _local_modules(v: Validation) -> set[str]:
    mods = set()
    for f in v.files:
        if f["path"].endswith(".py"):
            parts = f["path"][:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                mods.add(".".join(parts))
                mods.add(parts[0])
    return mods


def _check_sources(v: Validation) -> None:
    local = _local_modules(v)
    stdlib = set(sys.stdlib_module_names)
    trees: dict[str, ast.Module] = {}
    for f in v.files:
        if not f["path"].endswith(".py"):
            continue
        top = f["path"].split("/")[0].removesuffix(".py")
        if top in stdlib or top in ("gauntlet", "psycopg", "psycopg2") or top.startswith("_"):
            v.problems.append(f"{f['path']}: module name {top!r} would shadow a standard library or harness module")
        try:
            tree = ast.parse(v.contents[f["path"]].decode("utf-8"), filename=f["path"])
        except SyntaxError as exc:
            v.problems.append(f"{f['path']}: syntax error at line {exc.lineno}: {exc.msg}")
            continue
        trees[f["path"]] = tree
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import within the candidate package
                    continue
                names = [node.module or ""]
            for n in names:
                root = n.split(".")[0]
                if root == "gauntlet":
                    if not any(n == m or n.startswith(m + ".") for m in ALLOWED_GAUNTLET_MODULES):
                        v.problems.append(f"{f['path']}: import of {n!r} is not allowed (allowed harness modules: "
                                          f"{', '.join(ALLOWED_GAUNTLET_MODULES)})")
                elif root in FORBIDDEN_IMPORTS:
                    v.problems.append(f"{f['path']}: import of {n!r} is not allowed")
                elif root not in stdlib and root not in local:
                    v.problems.append(f"{f['path']}: import of {n!r} is not allowed (not standard library, "
                                      "contract module or candidate module)")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("__import__", "exec", "eval", "compile"):
                v.problems.append(f"{f['path']}: call to {node.func.id}() is not allowed")
    m = v.manifest
    if not m or not isinstance(m.get("entry"), dict):
        return
    entry_path = _module_path(m["entry"].get("module", ""))
    if entry_path not in v.contents and entry_path.replace(".py", "/__init__.py") not in v.contents:
        v.problems.append(f"entry module {m['entry'].get('module')!r} not found ({entry_path})")
        return
    tree = trees.get(entry_path) or trees.get(entry_path.replace(".py", "/__init__.py"))
    if tree is None:
        return
    classes = {n.name: n for t in trees.values() for n in ast.walk(t) if isinstance(n, ast.ClassDef)}
    entry = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == m["entry"].get("class")), None)
    if entry is None:
        v.problems.append(f"entry class {m['entry'].get('class')!r} not defined at top level of {entry_path}")
        return
    methods: set[str] = set()
    todo, seen = [entry], set()
    while todo:
        c = todo.pop()
        if c.name in seen:
            continue
        seen.add(c.name)
        methods |= {n.name for n in c.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for b in c.bases:
            bn = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else None
            if bn in classes:
                todo.append(classes[bn])
    missing = [x for x in REQUIRED_METHODS if x not in methods]
    if missing:
        v.problems.append(f"entry class is missing required methods: {missing}")
    owned = sorted(x for x in methods if x.startswith(HARNESS_OWNED_PREFIXES))
    if owned:
        v.problems.append(f"entry class defines harness-owned methods {owned}: the legacy app (v1_*) and "
                          "instance registration (app_*) are provided by the harness")


def validate(root: Path, limits: dict | None = None) -> Validation:
    v = collect(root, limits)
    if v.problems:
        return v
    _check_manifest(v)
    if not v.problems:
        _check_sources(v)
    v.ok = not v.problems
    if v.ok:
        v.source_hash = tree_hash(v.files)
    return v
