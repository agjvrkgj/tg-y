"""Web 管理界面（FastAPI）。

启动：python web.py   （默认 http://127.0.0.1:8000）
或：   uvicorn web:app --host 0.0.0.0 --port 8000

浏览器打开后可：填写 API 凭证与频道、用手机号登录、启动/停止监控、查看状态与日志。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from forwarder import ForwarderService

app = FastAPI(title="TG 频道视频搬运")
service = ForwarderService()

STATIC_DIR = Path(__file__).parent / "static"


# ------------------------- 请求体模型 -------------------------
class SettingsBody(BaseModel):
    api_id: int = 0
    api_hash: str = ""
    session_name: str = "forwarder"
    source_channels: list[str] = []
    target_channel: str = ""
    mode: str = "copy"
    keep_caption: bool = True
    backfill_limit: int = 0
    send_delay: int = 3


class PhoneBody(BaseModel):
    phone: str


class CodeBody(BaseModel):
    code: str
    password: Optional[str] = None


# ------------------------- 页面 -------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ------------------------- API -------------------------
@app.get("/api/status")
async def api_status():
    return await service.status()


@app.get("/api/logs")
async def api_logs(limit: int = 200):
    return {"logs": service.get_logs(limit)}


@app.post("/api/settings")
async def api_settings(body: SettingsBody):
    try:
        await service.apply_settings(body.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return await service.status()


@app.post("/api/login/send-code")
async def api_send_code(body: PhoneBody):
    try:
        await service.send_code(body.phone)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": "验证码已发送，请查收 Telegram。"}


@app.post("/api/login/verify")
async def api_verify(body: CodeBody):
    try:
        await service.sign_in(body.code, body.password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": "登录成功。"}


@app.post("/api/logout")
async def api_logout():
    await service.logout()
    return {"ok": True}


@app.post("/api/start")
async def api_start():
    try:
        await service.start()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return await service.status()


@app.post("/api/stop")
async def api_stop():
    await service.stop()
    return await service.status()


@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc: HTTPException):  # noqa: ANN001
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def main():
    import uvicorn

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
