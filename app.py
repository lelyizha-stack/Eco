from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

app = FastAPI(title="Eco RenPy Backend")

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
