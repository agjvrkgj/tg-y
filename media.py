"""视频处理辅助：基于 ffmpeg/ffprobe 裁剪视频开头、探测视频信息。

设计上把「命令构造 / 输出解析」做成纯函数（便于单元测试），
真正调用子进程的部分是 async 包装。

需要系统已安装 ffmpeg 与 ffprobe（可用环境变量 FFMPEG_BIN / FFPROBE_BIN 指定路径）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import List, Optional

FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")


def have_ffmpeg() -> bool:
    return shutil.which(FFMPEG) is not None


def have_ffprobe() -> bool:
    return shutil.which(FFPROBE) is not None


def build_trim_cmd(src: str, dst: str, start_seconds: float, reencode: bool = False) -> List[str]:
    """构造「删除开头 start_seconds 秒」的 ffmpeg 命令。

    - reencode=False（默认）：流复制(-c copy)，速度快、不损质量；
      在 -i 之前用 -ss 做快速定位，并用 -avoid_negative_ts 规整时间戳。
    - reencode=True：重新编码(libx264/aac)，定位最精确但更慢，适合流复制后开头异常时回退。
    """
    if reencode:
        return [
            FFMPEG, "-y", "-ss", str(start_seconds), "-i", src,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-movflags", "+faststart", dst,
        ]
    return [
        FFMPEG, "-y", "-ss", str(start_seconds), "-i", src,
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", dst,
    ]


def build_probe_cmd(path: str) -> List[str]:
    """构造探测视频宽高与时长的 ffprobe 命令（输出 JSON）。"""
    return [
        FFPROBE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]


def parse_probe_output(text: str) -> dict:
    """解析 ffprobe 的 JSON 输出，返回 {width, height, duration(int 秒)}。"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {"width": 0, "height": 0, "duration": 0}
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    fmt = data.get("format") or {}
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    raw_dur = stream.get("duration") or fmt.get("duration")
    duration = 0
    if raw_dur not in (None, "", "N/A"):
        try:
            duration = int(float(raw_dur))
        except (ValueError, TypeError):
            duration = 0
    return {"width": width, "height": height, "duration": duration}


async def _run(cmd: List[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


async def trim_video(src: str, dst: str, start_seconds: float) -> bool:
    """删除视频开头 start_seconds 秒，写入 dst。成功返回 True。

    先尝试流复制；若失败或产物为空，回退为重新编码。
    """
    if not have_ffmpeg():
        return False
    code, _out, _err = await _run(build_trim_cmd(src, dst, start_seconds, reencode=False))
    if code == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
        return True
    # 回退：重新编码
    code, _out, _err = await _run(build_trim_cmd(src, dst, start_seconds, reencode=True))
    return code == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


async def probe_video(path: str) -> Optional[dict]:
    """探测视频信息，返回 {width, height, duration} 或 None。"""
    if not have_ffprobe():
        return None
    code, out, _err = await _run(build_probe_cmd(path))
    if code != 0:
        return None
    return parse_probe_output(out.decode("utf-8", "ignore"))
