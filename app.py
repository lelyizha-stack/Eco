from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
import io
import json
import zipfile
import base64
import hashlib

app = FastAPI(title="Eco RenPy Backend")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


@app.get("/")
def root():
    return {"ok": True, "message": "Eco backend aktif"}


@app.post("/api/renpy/test-upload")
async def test_upload(file: UploadFile = File(...)):
    content = await file.read()
    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "size": len(content)
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
        "json_preview": None
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
            "error": "File ini bukan ZIP Ren'Py yang valid."
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
                "log_base64": base64.b64encode(log_bytes).decode("ascii")
            })

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="File ini bukan ZIP Ren'Py yang valid.")
