from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import io
import json
import zipfile
import base64
import hashlib
import pickle
import re
import sys
import types
import time
from collections import defaultdict
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from difflib import get_close_matches

app = FastAPI(title="Eco RenPy Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://karbitprime.fun",
        "https://www.karbitprime.fun",
        "http://localhost:3000",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
RENPY_RULES_URL = "https://script.google.com/macros/s/AKfycbzeCFEGNVwhnwYrdp6JlIh8sJOa0zYSe8w8TneyRQ-2swWwd7WoukEUb95n_3SzRy-dqg/exec"

MONEY_RE = re.compile(
    r"(gold|money|cash|coin|coins|credit|credits|wallet|funds|balance|bank|saldo|uang|duit|emas|เงิน|ทอง|เหรียญ|เครดิต)",
    re.IGNORECASE,
)

BAD_CANDIDATE_RE = re.compile(
    r"(__dict__|(^|\.)(_state|state)(\.|$)|(^|\.)(c_\d+)(\.|$)|usedgold|spentgold|costgold|requiregold|needgold|consumegold|paygold|stsgold|_stsusedgold)",
    re.IGNORECASE,
)

_PLACEHOLDER_CACHE = {}
_RULES_CACHE = None
_RULES_CACHE_TS = 0.0
_RULES_CACHE_TTL = 60.0


def ensure_fake_module(module_name: str):
    parts = module_name.split(".")
    parent = None
    full = ""

    for part in parts:
        full = part if not full else f"{full}.{part}"

        if full not in sys.modules:
            mod = types.ModuleType(full)
            sys.modules[full] = mod
            if parent is not None:
                setattr(parent, part, mod)

        parent = sys.modules[full]

    return sys.modules[module_name]


def register_fake_global(module_name: str, name: str, obj):
    mod = ensure_fake_module(module_name)

    try:
        obj.__module__ = module_name
    except Exception:
        pass

    try:
        obj.__name__ = name
    except Exception:
        pass

    try:
        obj.__qualname__ = name
    except Exception:
        pass

    setattr(mod, name, obj)
    return obj


class GenericPlaceholder:
    def __new__(cls, *args, **kwargs):
        obj = object.__new__(cls)
        obj._args = list(args)
        obj._kwargs = kwargs
        return obj

    def __setstate__(self, state):
        self._state = state
        if isinstance(state, dict):
            self.__dict__.update(state)


def make_revertable_list(module_name: str):
    class _RevertableList(list):
        def __setstate__(self, state):
            self._state = state
    return register_fake_global(module_name, "RevertableList", _RevertableList)


def make_revertable_dict(module_name: str):
    class _RevertableDict(dict):
        def __setstate__(self, state):
            self._state = state
    return register_fake_global(module_name, "RevertableDict", _RevertableDict)


def make_revertable_set(module_name: str):
    class _RevertableSet(set):
        def __setstate__(self, state):
            self._state = state
    return register_fake_global(module_name, "RevertableSet", _RevertableSet)


RenpyRevertableList = make_revertable_list("renpy.revertable")
RenpyPythonRevertableList = make_revertable_list("renpy.python")
RenpyRevertableDict = make_revertable_dict("renpy.revertable")
RenpyPythonRevertableDict = make_revertable_dict("renpy.python")
RenpyRevertableSet = make_revertable_set("renpy.revertable")
RenpyPythonRevertableSet = make_revertable_set("renpy.python")


def get_placeholder_type(module: str, name: str):
    key = (module, name)
    if key not in _PLACEHOLDER_CACHE:
        cls = type(name, (GenericPlaceholder,), {})
        register_fake_global(module, name, cls)
        _PLACEHOLDER_CACHE[key] = cls
    return _PLACEHOLDER_CACHE[key]


class LenientUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        mapping = {
            ("renpy.revertable", "RevertableList"): RenpyRevertableList,
            ("renpy.python", "RevertableList"): RenpyPythonRevertableList,
            ("renpy.revertable", "RevertableDict"): RenpyRevertableDict,
            ("renpy.python", "RevertableDict"): RenpyPythonRevertableDict,
            ("renpy.revertable", "RevertableSet"): RenpyRevertableSet,
            ("renpy.python", "RevertableSet"): RenpyPythonRevertableSet,
            ("collections", "defaultdict"): defaultdict,
        }

        if (module, name) in mapping:
            return mapping[(module, name)]

        if module == "builtins":
            import builtins
            if hasattr(builtins, name):
                return getattr(builtins, name)

        return get_placeholder_type(module, name)


def tolerant_load_pickle(data: bytes):
    return LenientUnpickler(io.BytesIO(data)).load()


def candidate_score(path: str) -> int:
    p = str(path or "").lower()

    if BAD_CANDIDATE_RE.search(p):
        return 1000

    if p == "store.money" or p.endswith(".money"):
        return 0
    if p == "store.cash" or p.endswith(".cash"):
        return 1
    if p == "store.gold" or p.endswith(".gold"):
        return 2
    if p == "store.coin" or p.endswith(".coin"):
        return 3
    if p == "store.coins" or p.endswith(".coins"):
        return 4
    if p == "store.credit" or p.endswith(".credit"):
        return 5
    if p == "store.credits" or p.endswith(".credits"):
        return 6

    if "money" in p:
        return 10
    if "cash" in p:
        return 11
    if "gold" in p:
        return 12
    if "coin" in p or "เหรียญ" in p:
        return 13
    if "credit" in p or "เครดิต" in p:
        return 14
    if "wallet" in p:
        return 15
    if "fund" in p:
        return 16
    if "balance" in p:
        return 17
    if "เงิน" in p or "uang" in p or "duit" in p:
        return 18
    if "ทอง" in p or "emas" in p:
        return 19

    return 50


def should_include_candidate(path: str) -> bool:
    p = str(path or "").strip().lower()
    if not p:
        return False
    if BAD_CANDIDATE_RE.search(p):
        return False
    return True


def scan_money_candidates(obj, path="", out=None, seen=None, depth=0, max_depth=8):
    if out is None:
        out = []
    if seen is None:
        seen = set()

    if depth > max_depth:
        return out

    if isinstance(obj, (dict, list, tuple, set)) or hasattr(obj, "__dict__"):
        obj_id = id(obj)
        if obj_id in seen:
            return out
        seen.add(obj_id)

    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            new_path = f"{path}.{key_str}" if path else key_str

            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if MONEY_RE.search(new_path) and should_include_candidate(new_path):
                    out.append({
                        "path": new_path,
                        "value": value,
                        "score": candidate_score(new_path),
                    })

            if isinstance(value, (dict, list, tuple, set)) or hasattr(value, "__dict__"):
                scan_money_candidates(value, new_path, out, seen, depth + 1, max_depth)

    elif isinstance(obj, (list, tuple, set)):
        for idx, value in enumerate(obj):
            new_path = f"{path}.{idx}" if path else str(idx)

            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if MONEY_RE.search(new_path) and should_include_candidate(new_path):
                    out.append({
                        "path": new_path,
                        "value": value,
                        "score": candidate_score(new_path),
                    })

            if isinstance(value, (dict, list, tuple, set)) or hasattr(value, "__dict__"):
                scan_money_candidates(value, new_path, out, seen, depth + 1, max_depth)

    elif hasattr(obj, "__dict__"):
        scan_money_candidates(vars(obj), f"{path}.__dict__" if path else "__dict__", out, seen, depth + 1, max_depth)

    return out


def get_scan_root(parsed):
    if isinstance(parsed, tuple) and len(parsed) >= 1 and isinstance(parsed[0], dict):
        return parsed[0]
    return parsed


def set_value_by_path(obj, path: str, new_value):
    if isinstance(obj, dict) and path in obj:
        obj[path] = new_value
        return True

    parts = path.split(".")
    ref = obj

    for i, part in enumerate(parts[:-1]):
        if isinstance(ref, dict):
            remaining = ".".join(parts[i:])
            if remaining in ref:
                ref[remaining] = new_value
                return True

            if part not in ref:
                raise KeyError(f"Path tidak ditemukan: {path}")
            ref = ref[part]

        elif isinstance(ref, list):
            idx = int(part)
            ref = ref[idx]

        elif isinstance(ref, tuple):
            idx = int(part)
            ref = ref[idx]

        else:
            raise TypeError(f"Tidak bisa menelusuri path pada tipe: {type(ref).__name__}")

    last = parts[-1]

    if isinstance(ref, dict):
        if last in ref:
            ref[last] = new_value
            return True

        full_path = ".".join(parts)
        if full_path in ref:
            ref[full_path] = new_value
            return True

        raise KeyError(f"Path tidak ditemukan: {path}")

    if isinstance(ref, list):
        idx = int(last)
        ref[idx] = new_value
        return True

    raise TypeError(f"Tidak bisa set nilai pada tipe: {type(ref).__name__}")


def get_value_by_path(obj, path: str):
    if isinstance(obj, dict) and path in obj:
        return obj[path]

    parts = path.split(".")
    ref = obj

    for i, part in enumerate(parts):
        if isinstance(ref, dict):
            remaining = ".".join(parts[i:])
            if remaining in ref:
                return ref[remaining]

            if part not in ref:
                return None
            ref = ref[part]

        elif isinstance(ref, list):
            try:
                ref = ref[int(part)]
            except Exception:
                return None

        elif isinstance(ref, tuple):
            try:
                ref = ref[int(part)]
            except Exception:
                return None

        else:
            return None

    return ref


def normalize_path_for_match(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(path or "").lower())


def resolve_rule_candidate(scan_root, rule_path: str, candidates: list):
    rule_path = str(rule_path or "").strip()
    if not rule_path:
        return None

    exact_value = get_value_by_path(scan_root, rule_path)
    if isinstance(exact_value, (int, float)) and not isinstance(exact_value, bool):
        return {
            "path": rule_path,
            "value": exact_value,
            "match_mode": "exact",
        }

    for item in candidates:
        if item["path"] == rule_path:
            return {
                "path": item["path"],
                "value": item["value"],
                "match_mode": "candidate-exact",
            }

    norm_rule = normalize_path_for_match(rule_path)
    if norm_rule:
        for item in candidates:
            if normalize_path_for_match(item["path"]) == norm_rule:
                return {
                    "path": item["path"],
                    "value": item["value"],
                    "match_mode": "normalized",
                }

    if norm_rule and candidates:
        norm_to_item = {}
        norm_keys = []

        for item in candidates:
            norm_item = normalize_path_for_match(item["path"])
            if not norm_item:
                continue
            if norm_item not in norm_to_item:
                norm_to_item[norm_item] = item
                norm_keys.append(norm_item)

        close = get_close_matches(norm_rule, norm_keys, n=1, cutoff=0.82)
        if close:
            matched = norm_to_item[close[0]]
            return {
                "path": matched["path"],
                "value": matched["value"],
                "match_mode": "fuzzy",
            }

    return None


def load_renpy_rules(force_refresh: bool = False):
    global _RULES_CACHE, _RULES_CACHE_TS

    now = time.time()
    if (
        _RULES_CACHE is not None
        and not force_refresh
        and (now - _RULES_CACHE_TS) < _RULES_CACHE_TTL
    ):
        return _RULES_CACHE

    try:
        url = f"{RENPY_RULES_URL}?t={int(now)}"
        with urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)

        rows = data if isinstance(data, list) else data.get("rows", [])

        rules = []
        for row in rows:
            enabled = str(row.get("enabled", "true")).strip().lower()
            if enabled in ("false", "0", "no", "off"):
                continue

            money_path = str(row.get("moneyPath", "") or row.get("path", "")).strip()
            if not money_path:
                continue

            rules.append({
                "gameSlug": str(row.get("gameSlug", "") or row.get("slug", "")).strip().lower(),
                "title": str(row.get("title", "") or row.get("gameTitle", "") or row.get("name", "")).strip(),
                "moneyPath": money_path,
                "label": str(row.get("label", "") or money_path).strip(),
                "enabled": True,
            })

        _RULES_CACHE = rules
        _RULES_CACHE_TS = now
        return rules

    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        _RULES_CACHE = []
        _RULES_CACHE_TS = now
        return _RULES_CACHE


def collect_sheet_matches(scan_root, rules, candidates):
    matches = []
    seen_paths = set()

    for rule in rules:
        matched = resolve_rule_candidate(scan_root, rule.get("moneyPath", ""), candidates)
        if not matched:
            continue

        path = matched["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)

        matches.append({
            "path": path,
            "value": matched["value"],
            "gameSlug": rule.get("gameSlug", ""),
            "title": rule.get("title", ""),
            "label": rule.get("label", path),
            "moneyPath": rule.get("moneyPath", path),
            "match_mode": matched["match_mode"],
        })

    return matches


@app.get("/")
def root():
    return {"ok": True, "message": "Eco backend aktif"}


@app.post("/api/renpy/reload-rules")
async def reload_rules():
    rules = load_renpy_rules(force_refresh=True)
    return JSONResponse({
        "ok": True,
        "rule_count": len(rules),
        "paths": [r["moneyPath"] for r in rules[:50]],
    })


@app.post("/api/renpy/inspect")
async def renpy_inspect(file: UploadFile = File(...)):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File terlalu besar.")

    result = {
        "ok": True,
        "filename": file.filename,
        "size": len(content),
        "is_zip": False,
        "entries": [],
        "has_log": False,
        "has_json": False,
        "has_screenshot": False,
        "json_preview": None,
    }

    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            names = zf.namelist()
            result["is_zip"] = True
            result["entries"] = names
            result["has_log"] = "log" in names
            result["has_json"] = "json" in names
            result["has_screenshot"] = "screenshot.png" in names

            if "json" in names:
                try:
                    json_text = zf.read("json").decode("utf-8", errors="replace")
                    parsed = json.loads(json_text)
                    if isinstance(parsed, dict):
                        result["json_preview"] = {k: parsed[k] for k in list(parsed.keys())[:10]}
                    else:
                        result["json_preview"] = parsed
                except Exception as e:
                    result["json_preview"] = {"error": f"Gagal parse entry json: {str(e)}"}

            return JSONResponse(result)

    except zipfile.BadZipFile:
        return JSONResponse({
            "ok": False,
            "filename": file.filename,
            "size": len(content),
            "error": "File ini bukan ZIP Ren'Py yang valid.",
        })


def build_candidates_and_matches(file_name: str, content: bytes):
    with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
        names = zf.namelist()
        if "log" not in names:
            raise HTTPException(status_code=400, detail="Entry 'log' tidak ditemukan.")
        log_bytes = zf.read("log")

    try:
        parsed = tolerant_load_pickle(log_bytes)
        scan_root = get_scan_root(parsed)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Gagal parse pickle log: {type(e).__name__}: {str(e)}",
        )

    raw_candidates = scan_money_candidates(scan_root)
    seen = set()
    unique_candidates = []
    for item in sorted(raw_candidates, key=lambda x: (x["score"], x["path"])):
        sig = (item["path"], item["value"])
        if sig in seen:
            continue
        seen.add(sig)
        unique_candidates.append({
            "path": item["path"],
            "value": item["value"],
        })

    rules = load_renpy_rules()
    matched_paths = collect_sheet_matches(scan_root, rules, unique_candidates)

    if matched_paths:
        display_candidates = [
            {"path": item["path"], "value": item["value"]}
            for item in matched_paths
        ]
        used_sheet_match = True
    else:
        display_candidates = unique_candidates[:100]
        used_sheet_match = False

    return {
        "filename": file_name,
        "parsed": parsed,
        "scan_root": scan_root,
        "log_bytes": log_bytes,
        "entries": names,
        "matched_paths": matched_paths,
        "candidates": display_candidates,
        "used_sheet_match": used_sheet_match,
        "parsed_type": type(parsed).__name__,
        "scan_root_type": type(scan_root).__name__,
    }


@app.post("/api/renpy/find-candidates")
async def renpy_find_candidates(file: UploadFile = File(...)):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File terlalu besar.")

    try:
        result = build_candidates_and_matches(file.filename, content)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File ini bukan ZIP Ren'Py yang valid.")

    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "entries": result["entries"],
        "parsed_type": result["parsed_type"],
        "scan_root_type": result["scan_root_type"],
        "used_sheet_match": result["used_sheet_match"],
        "matched_count": len(result["matched_paths"]),
        "matched_paths": result["matched_paths"][:100],
        "candidate_count": len(result["candidates"]),
        "candidates": result["candidates"][:100],
    })


@app.post("/api/renpy/edit-money")
async def renpy_edit_money(
    file: UploadFile = File(...),
    path: str = "",
    value: int = 999999,
):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File terlalu besar.")

    original_entries = {}
    names = []

    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            names = zf.namelist()
            if "log" not in names:
                raise HTTPException(status_code=400, detail="Entry 'log' tidak ditemukan.")
            for name in names:
                original_entries[name] = zf.read(name)
            log_bytes = original_entries["log"]
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File ini bukan ZIP Ren'Py yang valid.")

    try:
        parsed = tolerant_load_pickle(log_bytes)
        scan_root = get_scan_root(parsed)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Gagal parse pickle log: {type(e).__name__}: {str(e)}",
        )

    auto_candidates = scan_money_candidates(scan_root)
    auto_candidates = sorted(auto_candidates, key=lambda x: (x["score"], x["path"]))
    rules = load_renpy_rules()
    matched_paths = collect_sheet_matches(scan_root, rules, auto_candidates)

    if not path and matched_paths:
        path = matched_paths[0]["path"]

    if not path and auto_candidates:
        path = auto_candidates[0]["path"]

    if not path:
        raise HTTPException(status_code=400, detail="Path target tidak ditemukan dari rules maupun scan otomatis.")

    try:
        set_value_by_path(scan_root, path, value)
        new_log_bytes = pickle.dumps(parsed, protocol=4)
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Gagal edit pickle log: {type(e).__name__}: {str(e)}",
        )

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            if name == "log":
                zf.writestr("log", new_log_bytes)
            else:
                zf.writestr(name, original_entries[name])

    edited_bytes = out.getvalue()
    edited_name = file.filename[:-5] + "-edited.save" if file.filename.endswith(".save") else file.filename + "-edited.save"

    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "edited_filename": edited_name,
        "path": path,
        "used_sheet_match": bool(matched_paths),
        "matched_paths": matched_paths[:100],
        "new_value": value,
        "old_log_sha256": hashlib.sha256(log_bytes).hexdigest(),
        "new_log_sha256": hashlib.sha256(new_log_bytes).hexdigest(),
        "edited_save_base64": base64.b64encode(edited_bytes).decode("ascii"),
        "warning": "Save berhasil direpack, tetapi game dengan entry signatures bisa saja menolak file jika signature ikut diverifikasi.",
    })
