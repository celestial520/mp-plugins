"""
115秒传助手 · 整理通知（U115TransferNotify）

作用：MoviePilot 整理完成后，把整理出来的文件路径**直接推给 115秒传助手**，
让它立刻尝试秒传，不用等它自己的每日扫描。

为什么不是"只发个通知"：
  MP 整理会把文件从下载目录**搬到媒体库**，115秒传助手按自己的监控目录去找是找不到的，
  所以必须把"整理后的真实路径"带过去。

配置要点：
  · 助手地址：例如 http://192.168.1.10:9191
  · API Key：在助手网页「外部触发」里复制
  · 路径映射：MP 容器里的路径 -> 助手容器里的路径，每行一条，写法 `MP路径 => 助手路径`
"""

from collections import deque
from hashlib import md5
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType


class U115TransferNotify(_PluginBase):
    """
    整理完成后通知 115秒传助手
    """

    plugin_name = "115秒传助手·整理通知"
    plugin_desc = "MoviePilot 整理完成后，把整理出来的文件夹（或文件清单）推给 115秒传助手，让它立刻尝试秒传"
    plugin_icon = ""
    plugin_version = "1.1"
    plugin_author = "celestial520"
    author_url = "https://github.com/celestial520/mp-plugins"
    plugin_config_prefix = "u115transfernotify_"
    plugin_order = 21
    auth_level = 1

    # ---------------------------------------------------------------- 配置
    _enabled: bool = False
    _helper_url: str = ""
    _api_key: str = ""
    _push_mode: str = "dir"           # dir=推整理后的文件夹 / files=推文件清单
    _path_map_raw: str = ""
    _include_all_files: bool = True
    _include_sub: bool = False
    _timeout: int = 15
    _notify_mp: bool = False

    # ---------------------------------------------------------------- 运行态
    _logs = deque(maxlen=20)
    _recent: Dict[str, float] = {}

    def init_plugin(self, config: dict = None):
        self._logs = deque(maxlen=20)
        self._recent = {}
        if config:
            self._enabled = bool(config.get("enabled"))
            self._helper_url = str(config.get("helper_url") or "").strip().rstrip("/")
            self._api_key = str(config.get("api_key") or "").strip()
            self._push_mode = str(config.get("push_mode") or "dir").strip()
            self._path_map_raw = str(config.get("path_map") or "")
            self._include_all_files = bool(config.get("include_all_files", True))
            self._include_sub = bool(config.get("include_sub", False))
            self._timeout = max(5, min(120, int(config.get("timeout") or 15)))
            self._notify_mp = bool(config.get("notify_mp", False))
        if self._enabled:
            logger.info(
                f"【整理通知】已启用：{self._helper_url or '(未填地址)'}"
                f"，包含字幕/音轨={'是' if self._include_sub else '否'}"
            )

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        self._recent = {}

    # ---------------------------------------------------------------- 事件

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        self._handle(event, main=True)

    @eventmanager.register(
        [EventType.SubtitleTransferComplete, EventType.AudioTransferComplete]
    )
    def on_extra_transfer_complete(self, event: Event):
        self._handle(event, main=False)

    def _handle(self, event: Event, main: bool) -> None:
        try:
            if not self._enabled:
                return
            if not main and not self._include_sub:
                return
            if not self._helper_url or not self._api_key:
                return

            data = getattr(event, "event_data", None) or {}
            info = data.get("transferinfo")
            if not info or not getattr(info, "success", True):
                return

            # 整理到网盘存储的没有"本地文件"，跳过
            target_item = getattr(info, "target_item", None)
            storage = str(getattr(target_item, "storage", "") or "")
            if storage and storage != "local":
                logger.debug(f"【整理通知】目标是 {storage} 存储，跳过")
                return

            paths = self._collect_paths(info)
            if not paths:
                return

            # 短时间去重：同一批次可能触发多次事件
            signature = md5("|".join(sorted(paths)).encode()).hexdigest()
            import time as _time

            now = _time.time()
            if self._recent.get(signature) and now - self._recent[signature] < 60:
                return
            self._recent[signature] = now
            if len(self._recent) > 100:
                self._recent = {
                    k: v for k, v in self._recent.items() if now - v < 3600
                }

            mapped = [self._map_path(p) for p in paths]
            ok, detail = self._push(mapped)
            self._record(paths, mapped, ok, detail)
            if ok and self._notify_mp:
                try:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title=f"115秒传助手：已推送 {len(mapped)} 个文件",
                        text="\n".join(mapped[:10]),
                    )
                except Exception as err:
                    logger.debug(f"【整理通知】发送 MP 通知失败：{err}")
        except Exception as err:
            logger.error(f"【整理通知】处理事件出错：{err}")

    def _collect_paths(self, info) -> List[str]:
        """
        取整理后的目标路径：
          dir   -> 整理出来的文件夹（整批一起交给助手，附属文件也不会漏）
          files -> file_list_new 里的文件清单
        """
        target_dir = getattr(info, "target_diritem", None)
        target_item = getattr(info, "target_item", None)

        if self._push_mode == "dir":
            path = str(getattr(target_dir, "path", "") or "") if target_dir else ""
            if not path and target_item is not None:
                # 没有目录项（单文件整理）就退回文件本身
                path = str(getattr(target_item, "path", "") or "")
            return [path] if path else []

        if not self._include_all_files and target_item is not None:
            path = str(getattr(target_item, "path", "") or "")
            return [path] if path else []

        files = getattr(info, "file_list_new", None) or []
        paths = [str(x) for x in files if str(x).strip()]
        if not paths:
            if target_dir is not None and getattr(target_dir, "path", None):
                paths = [str(target_dir.path)]
            elif target_item is not None and getattr(target_item, "path", None):
                paths = [str(target_item.path)]
        return paths[:200]

    # ---------------------------------------------------------------- 路径映射

    def _parse_map(self) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        for line in str(self._path_map_raw or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for sep in ("=>", "->", "＝>", "="):
                if sep in line:
                    left, _, right = line.partition(sep)
                    left = left.strip().rstrip("/")
                    right = right.strip().rstrip("/")
                    if left and right:
                        pairs.append((left, right))
                    break
        # 长前缀优先
        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        return pairs

    def _map_path(self, path: str) -> str:
        text = str(path or "")
        for left, right in self._parse_map():
            if text == left or text.startswith(left + "/"):
                return right + text[len(left) :]
        return text

    # ---------------------------------------------------------------- 推送

    def _push(self, paths: List[str]) -> Tuple[bool, str]:
        url = f"{self._helper_url}/api/trigger"
        try:
            resp = requests.post(
                url,
                json={"paths": paths},
                headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except Exception as err:
            logger.error(f"【整理通知】请求 {url} 失败：{err}")
            return False, f"请求失败：{err}"
        if resp.status_code == 401:
            logger.error("【整理通知】API Key 不正确，请到助手网页重新复制")
            return False, "API Key 不正确"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}：{resp.text[:120]}"
        try:
            data = resp.json()
        except Exception:
            data = {}
        accepted = data.get("accepted")
        skipped = data.get("skipped") or []
        logger.info(
            f"【整理通知】已推送 {len(paths)} 个路径，助手接受 {accepted}，跳过 {len(skipped)}"
        )
        if skipped:
            logger.debug(f"【整理通知】跳过原因：{skipped[:3]}")
        return True, f"接受 {accepted}，跳过 {len(skipped)}"

    def _record(self, origin: List[str], mapped: List[str], ok: bool, detail: str) -> None:
        import time as _time

        self._logs.appendleft(
            {
                "time": _time.strftime("%m-%d %H:%M:%S"),
                "count": len(mapped),
                "ok": ok,
                "detail": detail,
                "files": mapped[:5],
            }
        )

    # ---------------------------------------------------------------- API

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/stat",
                "endpoint": self.api_stat,
                "methods": ["GET"],
                "summary": "查看最近推送记录",
                "description": "查看整理完成后推送给 115秒传助手的记录",
            },
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["GET", "POST"],
                "summary": "测试与助手的连通性",
                "description": "用当前配置请求助手 /api/ping，返回助手的任务列表",
            },
        ]

    def api_stat(self):
        import time as _time

        return {
            "version": self.plugin_version,
            "enabled": self._enabled,
            "helper_url": self._helper_url,
            "path_map": self._parse_map(),
            "include_all_files": self._include_all_files,
            "include_sub": self._include_sub,
            "logs": list(self._logs),
            "now": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def api_test(self):
        if not self._helper_url or not self._api_key:
            return {"success": False, "message": "先填助手地址和 API Key"}
        try:
            resp = requests.get(
                f"{self._helper_url}/api/ping",
                headers={"X-API-Key": self._api_key},
                timeout=self._timeout,
            )
        except Exception as err:
            return {"success": False, "message": f"连接失败：{err}"}
        if resp.status_code == 401:
            return {"success": False, "message": "API Key 不正确"}
        if resp.status_code >= 400:
            return {"success": False, "message": f"HTTP {resp.status_code}"}
        return {"success": True, "data": resp.json()}

    def get_actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "test",
                "name": "测试与助手连通性",
                "func": self.action_test,
            }
        ]

    def action_test(self, **kwargs) -> None:
        result = self.api_test()
        if result.get("success"):
            tasks = (result.get("data") or {}).get("tasks") or []
            names = "、".join(
                f"{t.get('name')}({t.get('watch_dir')})" for t in tasks[:5]
            )
            logger.info(f"【整理通知】连接成功，助手当前任务：{names or '无'}")
            if self._notify_mp:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="115秒传助手：连接正常",
                    text=f"助手任务：{names or '无'}",
                )
        else:
            logger.error(f"【整理通知】连接失败：{result.get('message')}")

    # ---------------------------------------------------------------- 表单

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "include_all_files",
                                            "label": "推送整批文件（含附属文件）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "include_sub",
                                            "label": "字幕/音轨整理也推送",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "push_mode",
                                            "label": "推送什么给助手",
                                            "items": [
                                                {
                                                    "title": "整理后的文件夹（推荐）",
                                                    "value": "dir",
                                                },
                                                {
                                                    "title": "文件清单（只推具体文件）",
                                                    "value": "files",
                                                },
                                            ],
                                            "hint": "推文件夹的话，助手会把里面的文件连同子目录一起处理，附属文件也不会漏",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "helper_url",
                                            "label": "115秒传助手地址",
                                            "placeholder": "http://192.168.1.10:9191",
                                            "hint": "助手网页的端口，默认 9191（不是 9191 就填实际端口）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_key",
                                            "label": "API Key",
                                            "placeholder": "在助手网页「外部触发」里复制",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_map",
                                            "label": "路径映射（两边路径不一样时才填）",
                                            "rows": 3,
                                            "placeholder": "# 一般不用填\n# /media => /watch",
                                            "hint": "如果 MP 和助手挂的是同一个目录、容器内路径也一样，这里留空即可。"
                                            "不一样就写「MP里的路径 => 助手里的路径」，右边要落在助手的某个监控目录内",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "请求超时（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify_mp",
                                            "label": "推送后在 MP 里也发通知",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "MP 整理完成后，插件把「整理出来的文件夹」告诉助手，"
                                                "助手收到就立刻对这个文件夹做秒传（目录结构照原样建到 115）。"
                                                "所以助手的监控目录里要配成 MP 的媒体库目录，"
                                                "两边路径一致时「路径映射」留空即可。"
                                                "保存后可以点插件菜单里的「测试与助手连通性」确认配置。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "push_mode": "dir",
            "helper_url": "",
            "api_key": "",
            "path_map": "",
            "include_all_files": True,
            "include_sub": False,
            "timeout": 15,
            "notify_mp": False,
        }

    def get_page(self) -> List[dict]:
        return []
