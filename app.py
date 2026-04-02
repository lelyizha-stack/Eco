from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import io
import json
import zipfile
import base64
import hashlib
import pickle
import re
from collections import defaultdict

app = FastAPI(title="Eco RenPy Backend")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

MONEY_RE = re.compile(
    r"(gold|money|cash|coin|coins|credit|credits|wallet|funds|balance|bank|saldo|uang)",
    re.IGNORECASE,
)

_PLACEHOLDER_CACHE = {}


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


class RenpyRevertableList(list):
    def __setstate__(self, state):
        self._state = state


class RenpyRevertableDict(dict):
    def __setstate__(self, state):
        self._state = state


class RenpyRevertableSet(set):
    def __setstate__(self, state):
        self._state = state


def get_placeholder_type(module: str, name: str):
    key = (module, name)
    if key not in _PLACEHOLDER_CACHE:
        cls_name = f"Stub_{module.replace('.', '_')}_{name}"
        _PLACEHOLDER_CACHE[key] = type(cls_name, (GenericPlaceholder,), {})
    return _PLACEHOLDER_CACHE[key]


class LenientUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        mapping = {
            ("renpy.revertable", "RevertableList"): RenpyRevertableList,
            ("renpy.python", "RevertableList"): RenpyRevertableList,
            ("renpy.revertable", "RevertableDict"): RenpyRevertableDict,
            ("renpy.python", "RevertableDict"): RenpyRevertableDict,
            ("renpy.revertable", "RevertableSet"): RenpyRevertableSet,
            ("renpy.python", "RevertableSet"): RenpyRevertableSet,
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
    if "coin" in p:
        return 5
    if "credit" in p:
        return 6
    if "wallet" in p:
        return 7
    if "fund" in p:
        return 8
    if "balance" in p:
        return 9
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


@app.get("/")
def root():
    return {"ok": True, "message": "Eco backend aktif"}


@app.post("/api/renpy/test-upload")
async def test_upload(file: UploadFile = File(...)):
    content = await file.read()
    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "size": len(content),
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
            "error": "File ini bukan ZIP Ren'Py yang valid.",
        })


@app.post("/api/renpy/read-log")
async def renpy_read_log(file: UploadFile = File(...)):
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

            return JSONResponse({
                "ok": True,
                "filename": file.filename,
                "entries": names,
                "log_size": len(log_bytes),
                "log_sha256": hashlib.sha256(log_bytes).hexdigest(),
                "log_base64": base64.b64encode(log_bytes).decode("ascii"),
            })

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File ini bukan ZIP Ren'Py yang valid.")


@app.post("/api/renpy/find-candidates")
async def renpy_find_candidates(file: UploadFile = File(...)):
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

    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "entries": names,
        "parsed_type": type(parsed).__name__,
        "scan_root_type": type(scan_root).__name__,
        "candidate_count": len(unique_candidates),
        "candidates": unique_candidates[:100],
    })
