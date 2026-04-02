from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import io
import json
import zipfile
import base64
import hashlib
import pickle
import re
import sys
import types
from collections import defaultdict
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

app = FastAPI(title="Eco RenPy Backend")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

RENPY_RULES_URL = "https://script.google.com/macros/s/AKfycbzeCFEGNVwhnwYrdp6JlIh8sJOa0zYSe8w8TneyRQ-2swWwd7WoukEUb95n_3SzRy-dqg/exec"

MONEY_RE = re.compile(
    r"(gold|money|cash|coin|coins|credit|credits|wallet|funds|balance|bank|saldo|uang|duit|emas|เงิน|ทอง|เหรียญ|เครดิต)",
    re.IGNORECASE,
)

_PLACEHOLDER_CACHE = {}
_RULES_CACHE = None


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
    p = path.lower()

    if p == "store.money" or p.endswith(".money"):
        return 0
    if "money" in p:
        return 1
    if p == "store.gold" or p.endswith(".gold"):
        return 2
    if "gold" in p:
        return 3
    if "cash" in p:
        return 4
    if "coin" in p or "เหรียญ" in p:
        return 5
    if "credit" in p or "เครดิต" in p:
        return 6
    if "wallet" in p:
        return 7
    if "fund" in p:
        return 8
    if "balance" in p:
        return 9
    if "เงิน" in p or "uang" in p or "duit" in p:
        return 10
    if "ทอง" in p or "emas" in p:
        return 11
    return 50


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
                if MONEY_RE.search(new_path):
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
                if MONEY_RE.search(new_path):
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


def load_renpy_rules(force_refresh: bool = False):
    global _RULES_CACHE

    if _RULES_CACHE is not None and not force_refresh:
        return _RULES_CACHE

    try:
        with urlopen(RENPY_RULES_URL, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)

        rows = data if isinstance(data, list) else data.get("rows", [])

        rules = {}
        for row in rows:
            slug = str(row.get("gameSlug", "") or row.get("slug", "")).strip().lower()
            money_path = str(row.get("moneyPath", "") or row.get("path", "")).strip()
            label = str(row.get("label", "")).strip() or money_path
            enabled = str(row.get("enabled", "true")).strip().lower()

            if not slug or not money_path:
                continue
            if enabled in ("false", "0", "no", "off"):
                continue

            rules[slug] = {
                "moneyPath": money_path,
                "label": label
            }

        _RULES_CACHE = rules
        return rules

    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        _RULES_CACHE = {}
        return _RULES_CACHE


def get_rule_for_slug(slug: str):
    slug = str(slug or "").strip().lower()
    if not slug:
        return None
    rules = load_renpy_rules()
    return rules.get(slug)


@app.get("/")
def root():
    return {"ok": True, "message": "Eco backend aktif"}


@app.post("/api/renpy/test-upload")
async def test_upload(file: UploadFile = File(...), slug: str = ""):
    content = await file.read()
    rule = get_rule_for_slug(slug) if slug else None
    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "size": len(content),
        "slug": slug,
        "rule_path": rule.get("moneyPath") if rule else None,
    })


@app.post("/api/renpy/inspect")
async def renpy_inspect(file: UploadFile = File(...), slug: str = ""):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File terlalu besar.")

    rule = get_rule_for_slug(slug) if slug else None

    result = {
        "ok": True,
        "filename": file.filename,
        "size": len(content),
        "slug": slug,
        "rule_path": rule.get("moneyPath") if rule else None,
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
                        result["json_preview"] = {
                            key: parsed[key]
                            for key in list(parsed.keys())[:10]
                        }
                    else:
                        result["json_preview"] = parsed
                except Exception as e:
                    result["json_preview"] = {
                        "error": f"Gagal parse entry json: {str(e)}"
                    }

            return JSONResponse(result)

    except zipfile.BadZipFile:
        return JSONResponse({
            "ok": False,
            "filename": file.filename,
            "size": len(content),
            "slug": slug,
            "rule_path": rule.get("moneyPath") if rule else None,
            "error": "File ini bukan ZIP Ren'Py yang valid.",
        })


@app.post("/api/renpy/read-log")
async def renpy_read_log(file: UploadFile = File(...), slug: str = ""):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File terlalu besar.")

    rule = get_rule_for_slug(slug) if slug else None

    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            names = zf.namelist()

            if "log" not in names:
                raise HTTPException(status_code=400, detail="Entry 'log' tidak ditemukan.")

            log_bytes = zf.read("log")

            return JSONResponse({
                "ok": True,
                "filename": file.filename,
                "slug": slug,
                "rule_path": rule.get("moneyPath") if rule else None,
                "entries": names,
                "log_size": len(log_bytes),
                "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
                "log_base64": base64.b64encode(log_bytes).decode("ascii"),
            })

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File ini bukan ZIP Ren'Py yang valid.")


@app.post("/api/renpy/find-candidates")
async def renpy_find_candidates(file: UploadFile = File(...), slug: str = ""):
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="File kosong.")

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File terlalu besar.")

    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            names = zf.namelist()

            if "log" not in names:
                raise HTTPException(status_code=400, detail="Entry 'log' tidak ditemukan.")

            log_bytes = zf.read("log")

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

    slug = str(slug or "").strip().lower()
    rule = get_rule_for_slug(slug) if slug else None

    candidates = scan_money_candidates(scan_root)

    seen = set()
    unique_candidates = []
    for item in sorted(candidates, key=lambda x: (x["score"], x["path"])):
        sig = (item["path"], item["value"])
        if sig in seen:
            continue
        seen.add(sig)
        unique_candidates.append({
            "path": item["path"],
            "value": item["value"],
        })

    if rule:
        rule_path = rule.get("moneyPath", "")
        rule_value = get_value_by_path(scan_root, rule_path)

        if isinstance(rule_value, (int, float)) and not isinstance(rule_value, bool):
            unique_candidates = [
                {"path": rule_path, "value": rule_value}
            ] + [c for c in unique_candidates if c["path"] != rule_path]

    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "slug": slug,
        "rule_path": rule.get("moneyPath") if rule else None,
        "entries": names,
        "parsed_type": type(parsed).__name__,
        "scan_root_type": type(scan_root).__name__,
        "candidate_count": len(unique_candidates),
        "candidates": unique_candidates[:100],
    })


@app.post("/api/renpy/edit-money")
async def renpy_edit_money(
    file: UploadFile = File(...),
    slug: str = "",
    path: str = "",
    value: int = 999999
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

    slug = str(slug or "").strip().lower()
    rule = get_rule_for_slug(slug) if slug else None

    if not path and rule:
        path = rule.get("moneyPath", "")

    if not path:
        auto_candidates = scan_money_candidates(scan_root)
        if auto_candidates:
            auto_candidates = sorted(auto_candidates, key=lambda x: (x["score"], x["path"]))
            path = auto_candidates[0]["path"]

    if not path:
        raise HTTPException(status_code=400, detail="Path uang tidak ditemukan dari slug maupun scan otomatis.")

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
    edited_name = file.filename
    if edited_name.endswith(".save"):
        edited_name = edited_name[:-5] + "-edited.save"
    else:
        edited_name = edited_name + "-edited.save"

    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "edited_filename": edited_name,
        "slug": slug,
        "rule_path": rule.get("moneyPath") if rule else None,
        "path": path,
        "new_value": value,
        "old_log_sha256": hashlib.sha256(log_bytes).hexdigest(),
        "new_log_sha256": hashlib.sha256(new_log_bytes).hexdigest(),
        "edited_save_base64": base64.b64encode(edited_bytes).decode("ascii"),
        "warning": "Save berhasil direpack, tetapi game dengan entry signatures bisa saja menolak file jika signature ikut diverifikasi.",
    })
