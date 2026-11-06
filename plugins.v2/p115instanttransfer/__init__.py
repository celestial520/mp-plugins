# -*- coding: utf-8 -*-
"""
P115InstantTransfer —— MoviePilot V2 插件
=====================================================================
监控一个本地文件夹，发现媒体文件后验证是否能在 115 网盘「秒传」：

  · 能秒传  -> 立即秒传（零带宽），成功后把源文件移入【回收目录】，并发送微信通知
  · 不能秒传 -> 拦截（不上传任何字节），记录到待重试列表，次日同一时间再次验证
  · 连续失败超过设定天数仍无法秒传 -> 放弃（移出待重试，可选通知）

依赖：p115client（见同目录 requirements.txt）

安装：
  1. 通过 MP 外部插件仓库 https://github.com/celestial520/mp-plugins.git 安装「115秒传监控」，
     或把 plugins.v2/p115instanttransfer 整个复制到 MoviePilot 的 app/plugins/ 目录下
     （绿联 NAS Docker 部署时，通常映射到容器内的 /app/app/plugins/，即你 docker 挂载的
       config 目录里的 plugins/，具体看你的 compose 挂载）
  2. 重启 MoviePilot（或等待热重载）
  3. 在插件市场启用「115秒传监控」，按需填写：监控文件夹、回收目录、115 Cookie、目标 CID 等
  4. 微信通知会自动走你在 MP【设定-通知】里已经配好的企业微信/微信渠道
=====================================================================
"""
import hashlib
import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase
from app.core.event import eventmanager
from app.schemas import Notification
from app.schemas.types import EventType, NotificationType
from app.db.plugindata_oper import PluginDataOper
from app.log import logger


class P115InstantTransfer(_PluginBase):
    # ---------------- 插件元信息 ----------------
    plugin_name = "115秒传监控"
    plugin_desc = "监控文件夹验证115网盘秒传：能秒传则秒传后移入回收目录并通知，不能则次日重试。"
    plugin_icon = ""
    plugin_version = "1.0"
    plugin_author = "auto-generated"
    auth_level = 1

    # ---------------- 运行期配置缓存 ----------------
    _monitor_dir: str = ""
    _recycle_dir: str = ""
    _cookies: str = ""
    _target_cid: int = 0
    _extensions: set = set()
    _min_size: int = 0

    # 这些后缀视为“仍在写入/下载中”，跳过
    _SKIP_SUFFIX = (".part", ".!qb", ".downloading", ".temp", ".aria2", ".qbdownload")
    # 文件 mtime 距现在不足该秒数则视为仍在写入，跳过（避免处理半截文件）
    _STABLE_SECONDS = 300

    def __init__(self):
        super().__init__()
        self.config: Dict[str, Any] = {}

    # ================= 生命周期 =================
    def init_plugin(self, config: dict = None):
        self.config = config or {}
        self._monitor_dir = (self.config.get("monitor_dir") or "").strip()
        self._recycle_dir = (self.config.get("recycle_dir") or "").strip()
        self._cookies = (self.config.get("cookies") or "").strip()
        try:
            self._target_cid = int(self.config.get("target_cid") or 0)
        except (ValueError, TypeError):
            self._target_cid = 0
        ext = self.config.get("extensions") or ""
        self._extensions = {e.strip().lower().lstrip(".") for e in ext.split(",") if e.strip()}
        try:
            self._min_size = int(float(self.config.get("min_size_mb") or 0)) * 1024 * 1024
        except (ValueError, TypeError):
            self._min_size = 0

    def get_state(self) -> bool:
        return bool(self.config.get("enabled"))

    def stop_service(self):
        pass

    # ================= 配置表单 =================
    @staticmethod
    def get_form() -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "notify", "label": "微信通知"}}
                            ]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "monitor_dir", "label": "监控文件夹路径",
                                           "placeholder": "/downloads/to115"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "recycle_dir", "label": "回收目录(秒传后源文件移入)",
                                           "placeholder": "/downloads/recycle115"}}
                            ]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "cookies", "label": "115 Cookie",
                                           "placeholder": "UID=...;CID=...;SEID=...;KID=...",
                                           "type": "password"}}
                            ]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "target_cid", "label": "115目标目录CID",
                                           "placeholder": "0 = 根目录"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "extensions", "label": "媒体扩展名(逗号分隔)",
                                           "placeholder": "mp4,mkv,mov,iso,ts"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "min_size_mb", "label": "最小大小(MB,0=不限制)"}}
                            ]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "scan_interval", "label": "扫描间隔(小时)"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "retry_hour", "label": "每日重试小时(0-23)"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "max_retry_days", "label": "放弃天数(0=永不)"}}
                            ]},
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "monitor_dir": "",
            "recycle_dir": "",
            "cookies": "",
            "target_cid": "0",
            "extensions": "mp4,mkv,mov,iso,ts,m2ts,avi,rmvb,flv,wmv,m4v",
            "min_size_mb": "0",
            "scan_interval": "1",
            "retry_hour": "3",
            "max_retry_days": "0",
        }

    # ================= 定时任务 =================
    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        try:
            scan_hours = max(1, int(float(self.config.get("scan_interval") or 1)))
        except (ValueError, TypeError):
            scan_hours = 1
        try:
            retry_hour = max(0, min(23, int(float(self.config.get("retry_hour") or 3))))
        except (ValueError, TypeError):
            retry_hour = 3
        return [
            {
                "id": "p115_scan",
                "name": "扫描并验证秒传",
                "trigger": "interval",
                "func": self.scan_and_process,
                "kwargs": {"hours": scan_hours},
            },
            {
                "id": "p115_retry",
                "name": "每日重试秒传",
                "trigger": "cron",
                "func": self.retry_pending,
                "kwargs": {"hour": retry_hour, "minute": 0},
            },
        ]

    # ================= 远程命令 =================
    def get_command(self) -> List[Dict[str, Any]]:
        return [{
            "cmd": "/p115_status",
            "event": EventType.PluginAction,
            "desc": "查看115秒传状态",
            "category": "115秒传",
            "data": {"action": "p115_status"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        # 本插件不注册自定义 API
        return []

    def get_page(self) -> List[Dict[str, Any]]:
        # 本插件无独立数据页面
        return []

    @eventmanager.register(EventType.PluginAction)
    def handle_event(self, event):
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "p115_status":
            return
        state = self.__load_state()
        pending = state.get("pending", {})
        given_up = state.get("given_up", {})
        done = state.get("done", [])
        lines = [
            f"监控目录：{self._monitor_dir or '未配置'}",
            f"115目标CID：{self._target_cid}",
            f"待重试文件数：{len(pending)}",
            f"已放弃文件数：{len(given_up)}",
            f"累计秒传成功：{len(done)}",
        ]
        if pending:
            lines.append("—— 待重试 ——")
            for p, info in pending.items():
                lines.append(f"· {info.get('name')} ({self.__human(info.get('size', 0))}) "
                             f"首见 {info.get('first_seen')}")
        self.__notify(title="115秒传插件状态", text="\n".join(lines))

    # ================= 主逻辑 =================
    def scan_and_process(self):
        """周期性扫描监控目录，发现新媒体文件即验证秒传。"""
        if not self._recycle_dir:
            logger.warning("【115秒传】未配置回收目录，已跳过（请在插件配置中填写）")
            return
        if not self._monitor_dir or not os.path.isdir(self._monitor_dir):
            logger.warning("【115秒传】监控目录未配置或不存在：%s", self._monitor_dir)
            return
        if not self._cookies:
            logger.warning("【115秒传】未配置115 Cookie")
            return

        state = self.__load_state()
        pending = state.setdefault("pending", {})
        given_up = state.setdefault("given_up", {})
        done = state.setdefault("done", [])
        processed = (set(pending.keys())
                     | set(given_up.keys())
                     | {d.get("src") for d in done if d.get("src")})

        client = self.__client()
        if client is None:
            return

        new_pending = 0
        for path in self.__iter_media(self._monitor_dir):
            if path in processed:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < self._min_size:
                continue
            sha1 = self.__sha1(path)
            if not sha1:
                continue
            ok, _ = self.__try_instant(client, path, size, sha1)
            if ok:
                self.__on_success(path, size, sha1, state)
            else:
                pending[path] = {
                    "name": os.path.basename(path),
                    "size": size,
                    "sha1": sha1,
                    "first_seen": self.__now(),
                    "last_try": self.__now(),
                }
                new_pending += 1
                logger.info("【115秒传】无法秒传，已拦截待重试：%s", path)
        if new_pending:
            logger.info("【115秒传】本次扫描新增 %d 个待重试文件", new_pending)
        self.__save_state(state)

    def retry_pending(self):
        """每日重试：对待重试列表里的文件再次验证秒传。"""
        state = self.__load_state()
        pending = state.get("pending", {})
        if not pending:
            return
        if not self._recycle_dir:
            logger.warning("【115秒传】未配置回收目录，跳过重试（请先配置）")
            return
        client = self.__client()
        if client is None:
            return

        try:
            max_days = int(float(self.config.get("max_retry_days") or 0))
        except (ValueError, TypeError):
            max_days = 0

        for path in list(pending.keys()):
            info = pending[path]
            size = info.get("size")
            sha1 = info.get("sha1")
            if not sha1 or not size or not os.path.exists(path):
                # 缺失信息或文件已不在，重新读取/移除
                if not os.path.exists(path):
                    logger.info("【115秒传】待重试文件已不存在，移除：%s", path)
                    pending.pop(path, None)
                    continue
                try:
                    size = os.path.getsize(path)
                    sha1 = self.__sha1(path)
                except OSError:
                    pending.pop(path, None)
                    continue
                info["size"] = size
                info["sha1"] = sha1

            ok, _ = self.__try_instant(client, path, size, sha1)
            if ok:
                self.__on_success(path, size, sha1, state)
                continue

            info["last_try"] = self.__now()
            # 是否放弃
            if max_days > 0:
                try:
                    first = datetime.strptime(info.get("first_seen", self.__now()), "%Y-%m-%d %H:%M:%S")
                    days = (datetime.now() - first).days
                except Exception:
                    days = 0
                if days >= max_days:
                    state.setdefault("given_up", {})[path] = {
                        "name": info.get("name"),
                        "first_seen": info.get("first_seen"),
                        "given_up_at": self.__now(),
                    }
                    pending.pop(path, None)
                    logger.info("【115秒传】超过 %d 天仍无法秒传，放弃：%s", max_days, path)
                    self.__notify(title="115秒传-已放弃",
                                  text=f"文件超过 {max_days} 天仍无法秒传，已停止重试：\n{info.get('name')}")
                    continue
            pending[path] = info

        self.__save_state(state)

    # ================= 内部方法 =================
    def __on_success(self, path: str, size: int, sha1: str, state: dict):
        """秒传成功：移入回收目录 + 记录 + 通知。"""
        name = os.path.basename(path)
        moved_to = None
        try:
            moved_to = self.__move_to_recycle(path)
        except Exception as e:
            logger.error("【115秒传】移动源文件失败 %s: %s", path, e)
        state.setdefault("done", []).append({
            "name": name,
            "size": size,
            "src": path,
            "moved_to": moved_to,
            "time": self.__now(),
        })
        state.setdefault("pending", {}).pop(path, None)
        self.__notify(
            title="115秒传成功",
            text=f"已秒传并移入回收目录：\n{name} ({self.__human(size)})"
                 + (f"\n回收路径：{moved_to}" if moved_to else "")
        )

    def __try_instant(self, client, path: str, size: int, sha1: str):
        """调用 115 秒传接口验证/执行秒传。返回 (是否秒传成功, 响应)。"""
        try:
            resp = self.__upload_init(client, path, size, sha1)
            if isinstance(resp, dict):
                reuse = resp.get("reuse")
                return reuse in (True, 1, "1"), resp
            return False, resp
        except Exception as e:
            logger.error("【115秒传】验证失败 %s: %s", path, e)
            return False, None

    def __upload_init(self, client, path: str, size: int, sha1: str):
        """优先用 client.upload_file_init；缺失时回退到 p115client.tool.upload.upload_init。"""
        if hasattr(client, "upload_file_init"):
            return client.upload_file_init(
                filename=os.path.basename(path),
                filesize=size,
                filesha1=sha1,
                pid=self._target_cid,
            )
        from p115client.tool.upload import upload_init
        return upload_init(
            client,
            path,
            pid=self._target_cid,
            filename=os.path.basename(path),
            filesha1=sha1,
            filesize=size,
        )

    def __client(self):
        try:
            from p115client import P115Client
            return P115Client(self._cookies)
        except Exception as e:
            logger.error("【115秒传】115 客户端初始化失败：%s", e)
            return None

    def __move_to_recycle(self, src: str) -> str:
        if not self._recycle_dir:
            raise ValueError("未配置回收目录")
        os.makedirs(self._recycle_dir, exist_ok=True)
        base = os.path.basename(src)
        dst = os.path.join(self._recycle_dir, base)
        if os.path.exists(dst):
            n, e = os.path.splitext(base)
            dst = os.path.join(self._recycle_dir, f"{n}.{int(time.time())}{e}")
        shutil.move(src, dst)
        return dst

    def __iter_media(self, root: str):
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过回收目录（若其位于监控目录之内）
            if self._recycle_dir and dirpath.startswith(self._recycle_dir):
                dirnames[:] = []
                continue
            for fn in filenames:
                if fn.startswith("."):
                    continue
                lower = fn.lower()
                if lower.endswith(self._SKIP_SUFFIX):
                    continue
                ext = os.path.splitext(lower)[1].lstrip(".")
                if self._extensions and ext not in self._extensions:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                if time.time() - mtime < self._STABLE_SECONDS:
                    continue
                yield full

    def __sha1(self, path: str, chunk: int = 4 * 1024 * 1024) -> str:
        h = hashlib.sha1()
        try:
            with open(path, "rb") as f:
                while True:
                    b = f.read(chunk)
                    if not b:
                        break
                    h.update(b)
        except OSError:
            return ""
        return h.hexdigest()

    # ---------------- 状态持久化 ----------------
    def __pid(self) -> str:
        return getattr(self, "plugin_id", self.__class__.__name__)

    def __load_state(self) -> dict:
        try:
            data = PluginDataOper().get_data(self.__pid(), "state")
        except Exception:
            data = None
        if not isinstance(data, dict):
            data = {}
        data.setdefault("pending", {})
        data.setdefault("given_up", {})
        data.setdefault("done", [])
        return data

    def __save_state(self, state: dict):
        try:
            PluginDataOper().save(self.__pid(), "state", state)
        except Exception as e:
            logger.error("【115秒传】保存状态失败：%s", e)

    # ---------------- 通知 ----------------
    def __notify(self, title: str, text: str, image: Optional[str] = None):
        if not self.config.get("notify", True):
            return
        try:
            from app.command import CommandChain
            CommandChain().post_message(Notification(
                mtype=NotificationType.Manual, title=title, text=text, image=image))
            return
        except Exception as e:
            logger.error("【115秒传】发送微信通知失败：%s", e)
        # 兜底：至少写入 MP 系统消息
        try:
            from app.helper.message import MessageHelper
            MessageHelper().put(title=title, message=text, role="plugin")
        except Exception:
            pass

    # ---------------- 工具 ----------------
    @staticmethod
    def __human(n: int) -> str:
        n = int(n or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n}{unit}"
            n /= 1024
        return f"{n:.1f}PB"

    @staticmethod
    def __now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
