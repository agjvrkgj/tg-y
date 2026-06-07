"""Web 管理界面（FastAPI）。

特性：
- 面板访问密码（环境变量 WEB_PASSWORD 或面板内设置），基于 Cookie 会话。
- 多 Telegram 账号管理：每个账号独立登录、配置、启停、日志。

启动：python web.py            （默认 http://127.0.0.1:8000）
或：   uvicorn web:app --port 8000
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from forwarder import AccountManager

app = FastAPI(title="TG 频道视频搬运")
manager = AccountManager()

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "tg_session"
SESSION_TTL = 7 * 24 * 3600  # 7 天

# token -> 过期 epoch
_sessions: Dict[str, float] = {}


# ------------------------- 鉴权 -------------------------
def _new_token() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _token_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(token, None)
        return False
    return True


def is_authed(token: Optional[str]) -> bool:
    # 未设置密码 => 视为已授权（开放访问）
    if not manager.password_enabled():
        return True
    return _token_valid(token)


def require_auth(tg_session: Optional[str] = Cookie(default=None)):
    if not is_authed(tg_session):
        raise HTTPException(status_code=401, detail="未授权，请先登录面板。")
    return True


# ------------------------- 请求体 -------------------------
class ContentFilterBody(BaseModel):
    block_keywords: list[str] = []
    remove_links: bool = False
    remove_mentions: bool = False
    replacement: str = ""
    drop_on_match: bool = False


class SettingsBody(BaseModel):
    name: str = "新账号"
    api_id: int = 0
    api_hash: str = ""
    session_name: str = ""
    source_channels: list[str] = []
    target_channel: str = ""
    mode: str = "copy"
    keep_caption: bool = True
    backfill_limit: int = 0
    send_delay: int = 3
    content_filter: ContentFilterBody = ContentFilterBody()
    trim_start_seconds: int = 0
    album_cover: bool = True


class PhoneBody(BaseModel):
    phone: str


class CodeBody(BaseModel):
    code: str
    password: Optional[str] = None


class PanelLoginBody(BaseModel):
    password: str


class SetPasswordBody(BaseModel):
    new_password: str
    current_password: Optional[str] = None


class AddAccountBody(BaseModel):
    name: str = "新账号"


class GroupSettingsBody(BaseModel):
    name: str = "新负载均衡组"
    member_ids: list[str] = []
    source_channels: list[str] = []
    target_channel: str = ""
    mode: str = "copy"
    keep_caption: bool = True
    per_account_delay: int = 5
    strategy: str = "balanced"
    backfill_limit: int = 0
    content_filter: ContentFilterBody = ContentFilterBody()
    trim_start_seconds: int = 0
    album_cover: bool = True


class AddGroupBody(BaseModel):
    name: str = "新负载均衡组"


# ------------------------- 页面 -------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


# ------------------------- 面板鉴权 API -------------------------
@app.get("/api/auth/status")
async def auth_status(tg_session: Optional[str] = Cookie(default=None)):
    return {
        "password_enabled": manager.password_enabled(),
        "authed": is_authed(tg_session),
    }


@app.post("/api/auth/login")
async def auth_login(body: PanelLoginBody, response: Response):
    if not manager.password_enabled():
        return {"ok": True, "message": "面板未设置密码，无需登录。"}
    if not manager.check_password(body.password):
        raise HTTPException(status_code=401, detail="密码错误。")
    token = _new_token()
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, samesite="lax", max_age=SESSION_TTL
    )
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout(response: Response, tg_session: Optional[str] = Cookie(default=None)):
    if tg_session:
        _sessions.pop(tg_session, None)
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.post("/api/auth/set-password")
async def set_password(body: SetPasswordBody, tg_session: Optional[str] = Cookie(default=None)):
    if os.getenv("WEB_PASSWORD"):
        raise HTTPException(
            status_code=400, detail="当前密码由环境变量 WEB_PASSWORD 控制，无法在面板修改。"
        )
    # 已设密码时，修改需先通过鉴权（或提供当前密码）
    if manager.password_enabled():
        if not (_token_valid(tg_session) or manager.check_password(body.current_password or "")):
            raise HTTPException(status_code=401, detail="请提供正确的当前密码。")
    manager.set_password(body.new_password)
    # 重置所有会话，强制重新登录
    _sessions.clear()
    return {"ok": True, "message": "密码已更新，请重新登录。"}


# ------------------------- 账号管理 API -------------------------
@app.get("/api/accounts")
async def list_accounts(_: bool = Depends(require_auth)):
    return {"accounts": await manager.list_status()}


@app.post("/api/accounts")
async def add_account(body: AddAccountBody, _: bool = Depends(require_auth)):
    svc = manager.add_account(body.name)
    return await svc.status()


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: str, _: bool = Depends(require_auth)):
    try:
        await manager.remove_account(account_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


def _svc(account_id: str):
    try:
        return manager.get(account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/accounts/{account_id}/status")
async def account_status(account_id: str, _: bool = Depends(require_auth)):
    return await _svc(account_id).status()


@app.get("/api/accounts/{account_id}/logs")
async def account_logs(account_id: str, limit: int = 200, _: bool = Depends(require_auth)):
    return {"logs": _svc(account_id).get_logs(limit)}


@app.post("/api/accounts/{account_id}/settings")
async def account_settings(account_id: str, body: SettingsBody, _: bool = Depends(require_auth)):
    try:
        await manager.apply_settings(account_id, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return await _svc(account_id).status()


@app.post("/api/accounts/{account_id}/login/send-code")
async def account_send_code(account_id: str, body: PhoneBody, _: bool = Depends(require_auth)):
    try:
        await _svc(account_id).send_code(body.phone)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": "验证码已发送，请查收 Telegram。"}


@app.post("/api/accounts/{account_id}/login/verify")
async def account_verify(account_id: str, body: CodeBody, _: bool = Depends(require_auth)):
    try:
        await _svc(account_id).sign_in(body.code, body.password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": "登录成功。"}


@app.post("/api/accounts/{account_id}/logout")
async def account_logout(account_id: str, _: bool = Depends(require_auth)):
    await _svc(account_id).logout()
    return {"ok": True}


@app.post("/api/accounts/{account_id}/start")
async def account_start(account_id: str, _: bool = Depends(require_auth)):
    try:
        await _svc(account_id).start()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return await _svc(account_id).status()


@app.post("/api/accounts/{account_id}/stop")
async def account_stop(account_id: str, _: bool = Depends(require_auth)):
    await _svc(account_id).stop()
    return await _svc(account_id).status()


# ------------------------- 负载均衡组 API -------------------------
@app.get("/api/groups")
async def list_groups(_: bool = Depends(require_auth)):
    return {"groups": await manager.list_group_status()}


@app.post("/api/groups")
async def add_group(body: AddGroupBody, _: bool = Depends(require_auth)):
    grp = manager.add_group(body.name)
    return await grp.status()


def _grp(group_id: str):
    try:
        return manager.get_group(group_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, _: bool = Depends(require_auth)):
    try:
        await manager.remove_group(group_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.get("/api/groups/{group_id}/status")
async def group_status(group_id: str, _: bool = Depends(require_auth)):
    return await _grp(group_id).status()


@app.get("/api/groups/{group_id}/logs")
async def group_logs(group_id: str, limit: int = 200, _: bool = Depends(require_auth)):
    return {"logs": _grp(group_id).get_logs(limit)}


@app.post("/api/groups/{group_id}/settings")
async def group_settings(group_id: str, body: GroupSettingsBody, _: bool = Depends(require_auth)):
    try:
        await manager.apply_group_settings(group_id, body.model_dump())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return await _grp(group_id).status()


@app.post("/api/groups/{group_id}/start")
async def group_start(group_id: str, _: bool = Depends(require_auth)):
    try:
        await _grp(group_id).start()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))
    return await _grp(group_id).status()


@app.post("/api/groups/{group_id}/stop")
async def group_stop(group_id: str, _: bool = Depends(require_auth)):
    await _grp(group_id).stop()
    return await _grp(group_id).status()


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
