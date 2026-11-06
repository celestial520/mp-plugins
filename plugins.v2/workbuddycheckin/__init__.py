# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到 —— MoviePilot V2 插件
=====================================================================
每天自动领取 WorkBuddy「Buddy 加油站」每日礼包积分：

  · 长效令牌（refresh token）在插件设置页填一次，之后由插件自动续期并回写插件数据
  · 每天 check_at 之后首次执行，失败按 retry_minutes 补签；MP 重启后也会自动补
  · 签到成功走 MP 内置消息渠道（CommandChain.post_message）通知，复用你已配好的企业微信
  · 只调官方接口，不模拟点击、不改客户端文件；令牌明文不写日志、不写通知

安装：
  1. 把 plugins.v2/workbuddycheckin 复制到 MoviePilot 的 app/plugins/ 目录，重启 MP
     （或通过 MP【插件】→ 外部插件仓库添加 mp-plugins 插件合集）
  2. 在插件设置页填写 refresh_token（用 tools/export_token.bat 在已登录的 Windows 上导出）
  3. 启用插件；通知走 MP【设定 - 通知】里已配好的企业微信/微信渠道
=====================================================================
"""
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import requests

from app.plugins import _PluginBase
from app.core.event import eventmanager
from app.schemas import Notification
from app.schemas.types import EventType, NotificationType
from app.db.plugindata_oper import PluginDataOper
from app.log import logger

CST = timezone(timedelta(hours=8))  # 固定 UTC+8，不依赖 MP 容器时区
TAG = "【WorkBuddy签到】"


class WorkBuddyCheckin(_PluginBase):
    # ---------------- 插件元信息 ----------------
    plugin_name = "WorkBuddy每日签到"
    plugin_desc = "每天自动领 WorkBuddy 每日礼包积分，令牌自续期，签到成功走 MP 内置渠道通知。"
    plugin_icon = ""
    plugin_version = "1.0"
    plugin_author = "celestial520"
    auth_level = 1

    def __init__(self):
        super().__init__()
        self.config: Dict[str, Any] = {}
        self._lock = threading.Lock()
        self._running = False
        self._last_result: Dict[str, Any] = {}

    # ================= 生命周期 =================
    def init_plugin(self, config: dict = None):
        self.config = config or {}
        logger.info("%s 插件已加载 | 启用=%s | 每日 %s | 通知=%s",
                    TAG, self.get_state(), self._check_at(), self._notify_enabled())

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
                                {"component": "VSwitch", "props": {"model": "notify", "label": "签到成功通知"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "notify_failure", "label": "失败也通知(每天1次)"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 3}, "content": [
                                {"component": "VSwitch", "props": {"model": "notify_already", "label": "已签到也通知"}}
                            ]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "refresh_token", "label": "长效令牌 refresh_token",
                                           "placeholder": "在已登录 WorkBuddy 的 Windows 上用 tools/export_token.bat 导出",
                                           "type": "password"}}
                            ]},
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "domain", "label": "接口域名",
                                           "placeholder": "www.workbuddy.cn"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "check_at", "label": "每日执行时间(HH:MM)",
                                           "placeholder": "09:00"}}
                            ]},
                            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                                {"component": "VTextField",
                                 "props": {"model": "retry_minutes", "label": "失败重试间隔(分钟)",
                                           "placeholder": "30"}}
                            ]},
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "notify_failure": True,
            "notify_already": False,
            "refresh_token": "",
            "domain": "www.workbuddy.cn",
            "check_at": "09:00",
            "retry_minutes": "30",
        }

    # ================= 定时任务 =================
    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        hour, minute = self._parse_time(self._check_at())
        return [
            {
                "id": "WorkBuddyCheckinDaily",
                "name": "WorkBuddy 每日签到",
                "trigger": "cron",
                "func": self.checkin_task,
                "kwargs": {"hour": hour, "minute": minute},
            },
            {
                "id": "WorkBuddyCheckinRetry",
                "name": "WorkBuddy 签到补签",
                "trigger": "interval",
                "func": self.checkin_task,
                "kwargs": {"minutes": self._retry_minutes()},
            },
        ]

    # ================= 远程命令 / API =================
    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {"cmd": "/wb_checkin", "event": EventType.PluginAction,
             "desc": "立即执行一次 WorkBuddy 签到", "category": "WorkBuddy签到",
             "data": {"action": "wb_checkin"}},
            {"cmd": "/wb_status", "event": EventType.PluginAction,
             "desc": "查看 WorkBuddy 签到状态", "category": "WorkBuddy签到",
             "data": {"action": "wb_status"}},
            {"cmd": "/wb_test", "event": EventType.PluginAction,
             "desc": "发送 WorkBuddy 通知测试消息", "category": "WorkBuddy签到",
             "data": {"action": "wb_test"}},
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_event(self, event):
        if not event:
            return
        data = getattr(event, "event_data", None) or {}
        action = data.get("action")
        if action == "wb_checkin":
            self.checkin_task(force=True)
            self._notify("WorkBuddy 手动签到完成", self._status_text(), force=True)
        elif action == "wb_status":
            self._notify("WorkBuddy 签到状态", self._status_text(), force=True)
        elif action == "wb_test":
            self._notify("WorkBuddy 通知测试",
                         "【测试消息】WorkBuddy 每日签到插件通知链路正常。\n"
                         "签到成功后你会收到同样格式的消息。", force=True)

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/status", "endpoint": self.api_status, "methods": ["GET"],
             "summary": "查看签到状态", "description": "今日状态、连签天数、本期累计、上次结果"},
            {"path": "/check", "endpoint": self.api_check, "methods": ["POST", "GET"],
             "summary": "立即签到", "description": "手动触发一次签到（已签到则幂等跳过）"},
            {"path": "/test-notify", "endpoint": self.api_test_notify, "methods": ["POST", "GET"],
             "summary": "发送测试通知", "description": "验证 MP 内置企业微信/微信通知渠道是否连通"},
        ]

    def api_status(self, **kwargs):
        return {"success": True, "data": self._status_dict()}

    def api_check(self, force: bool = True, **kwargs):
        if not self.get_state():
            return {"success": False, "message": "插件未启用"}
        self.checkin_task(force=bool(force))
        return {"success": True, "data": self._last_result}

    def api_test_notify(self, **kwargs):
        self._notify("WorkBuddy 通知测试",
                     "【测试消息】WorkBuddy 每日签到插件通知链路正常。", force=True)
        return {"success": True, "message": "测试消息已发送，请查看 MP 里配置的通知渠道"}

    def get_page(self) -> List[dict]:
        return []

    # ================= 配置项读取 =================
    def _check_at(self) -> str:
        return (self.config.get("check_at") or "09:00").strip()

    def _domain(self) -> str:
        return (self.config.get("domain") or "www.workbuddy.cn").strip()

    def _retry_minutes(self) -> int:
        try:
            return max(5, int(float(self.config.get("retry_minutes") or 30)))
        except (ValueError, TypeError):
            return 30

    def _notify_enabled(self) -> bool:
        return bool(self.config.get("notify", True))

    @staticmethod
    def _parse_time(value: str) -> Tuple[int, int]:
        try:
            parts = str(value or "09:00").split(":")
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except (ValueError, IndexError, TypeError):
            pass
        return 9, 0

    def _time_reached(self, t: datetime) -> bool:
        h, m = self._parse_time(self._check_at())
        return (t.hour, t.minute) >= (h, m)

    # ================= 插件数据（状态 + 轮换后的令牌） =================
    def _pid(self) -> str:
        return getattr(self, "plugin_id", self.__class__.__name__)

    def _load_state(self) -> dict:
        try:
            data = PluginDataOper().get_data(self._pid(), "state")
        except Exception as e:
            logger.error("%s 读取插件数据失败：%s", TAG, e)
            data = None
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict):
        try:
            PluginDataOper().save(self._pid(), "state", state)
        except Exception as e:
            logger.error("%s 保存插件数据失败：%s", TAG, e)

    @staticmethod
    def _fp(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] if token else ""

    def _resolve_token(self, state: dict) -> Tuple[str, bool]:
        """表单里换了新令牌（首次/重新导出）时优先用表单的，否则用插件数据里轮换后的。"""
        cfg_token = (self.config.get("refresh_token") or "").strip()
        fp = self._fp(cfg_token)
        if fp and fp != state.get("config_token_fp"):
            state["config_token_fp"] = fp
            state["refresh_token"] = cfg_token
            return cfg_token, True
        return (state.get("refresh_token") or cfg_token).strip(), False

    # ================= 官方接口 =================
    def _post(self, path: str, access_token: str = None, refresh_token: str = None) -> Tuple[int, dict]:
        url = "https://%s%s" % (self._domain(), path)
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WorkBuddyCheckin/1.0",
            "X-Domain": self._domain(),
        }
        if access_token:
            h["Authorization"] = "Bearer " + access_token
        if refresh_token:
            h["X-Refresh-Token"] = refresh_token
            h["X-Auth-Refresh-Source"] = "plugin"
        resp = requests.post(url, headers=h, json={}, timeout=25)
        try:
            return resp.status_code, resp.json()
        except Exception:
            return resp.status_code, {"_raw": (resp.text or "")[:200]}

    # ================= 主逻辑 =================
    def checkin_task(self, force: bool = False, **kwargs) -> None:
        """定时/手动入口。所有异常都在这里隔离，绝不影响 MP 主进程。"""
        if not self.get_state() and not force:
            return
        with self._lock:
            if self._running:
                return
            self._running = True
        try:
            self._run_once(force=bool(force))
        except Exception as e:
            logger.error("%s 执行失败（已隔离，不影响 MP）：%s", TAG, e)
            self._record_error(str(e))
        finally:
            self._running = False

    def _run_once(self, force: bool = False):
        t = datetime.now(CST)
        today = t.date().isoformat()
        state = self._load_state()
        done_today = (state.get("last_success_date") == today
                      and state.get("last_status") in ("success", "already", "inactive"))
        if not force and done_today:
            return
        if not force and not self._time_reached(t):
            return

        token, changed = self._resolve_token(state)
        if changed:
            self._save_state(state)
        if not token or len(token) < 40:
            raise RuntimeError("未配置 refresh_token，请在插件设置页填写（用 tools/export_token.bat 导出）")

        # 1) 长效令牌换新 access_token，并立刻回写轮换结果
        status, obj = self._post("/v2/plugin/auth/token/refresh", refresh_token=token)
        data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
        at = data.get("accessToken")
        if status != 200 or obj.get("code") != 0 or not at:
            raise RuntimeError("刷新令牌失败 HTTP=%s code=%s msg=%s（多半是令牌失效，请重新导出一次）"
                               % (status, obj.get("code"), obj.get("msg")))
        new_rt = data.get("refreshToken")
        if new_rt and new_rt != token:
            state["refresh_token"] = new_rt
            state["token_updated_at"] = t.strftime("%Y-%m-%d %H:%M:%S")
            self._save_state(state)  # 先落盘令牌，后续步骤失败也不丢
            logger.info("%s 长效令牌已轮换并回写插件数据（指纹 %s）", TAG, self._fp(new_rt))

        # 2) 查状态 → 没领才领，已领幂等跳过
        status, obj = self._post("/v2/billing/meter/checkin-activity-status", access_token=at)
        if status != 200 or obj.get("code") != 0:
            raise RuntimeError("查询签到状态失败 HTTP=%s code=%s msg=%s"
                               % (status, obj.get("code"), obj.get("msg")))
        info = obj.get("data") or {}
        result = {}
        if info.get("active") is False:
            outcome = "inactive"
        elif info.get("today_checked_in"):
            outcome = "already"
        else:
            _, claimed = self._post("/v2/billing/meter/daily-checkin", access_token=at)
            if claimed.get("code") == 0:
                outcome = "success"
                result = claimed.get("data") or {}
            elif claimed.get("code") == 10001 or "已签到" in str(claimed.get("msg") or ""):
                outcome = "already"
            else:
                raise RuntimeError("领取失败 code=%s msg=%s" % (claimed.get("code"), claimed.get("msg")))

        credit = result.get("today_credit") or result.get("credit") or info.get("today_credit") or 0
        streak = result.get("streak_days") or info.get("streak_days") or 0
        total = info.get("total_credits")
        state.update({
            "last_status": outcome,
            "last_success_date": today,
            "last_run": t.strftime("%Y-%m-%d %H:%M:%S"),
            "credit": credit,
            "streak_days": streak,
            "total_credits": total,
            "last_error": "",
        })
        self._save_state(state)
        self._last_result = {"status": outcome, "credit": credit,
                             "streak_days": streak, "total_credits": total}
        logger.info("%s %s | 今日 +%s 积分 | 连签 %s 天 | 本期累计 %s",
                    TAG, outcome, credit, streak, total)

        # 3) 通知
        if outcome == "success":
            self._notify(
                "WorkBuddy 签到成功 +%s 积分" % credit,
                "WorkBuddy 每日签到助手：今日积分已到账\n"
                "【签到成功】+%s 积分\n"
                "· 连签 %s 天\n"
                "· 本期累计 %s 分\n"
                "· 时间 %s\n\n"
                "明天 %s 继续自动领。" % (credit, streak, total, state["last_run"], self._check_at()),
            )
        elif outcome == "already" and self.config.get("notify_already"):
            self._notify("WorkBuddy 今日已签到", self._status_text())

    def _record_error(self, msg: str):
        state = self._load_state()
        state["last_error"] = msg
        state["last_run"] = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        self._save_state(state)
        if not self.config.get("notify_failure", True):
            return
        today = datetime.now(CST).date().isoformat()
        if state.get("last_failure_notify_date") == today:
            return  # 失败通知每天最多一次，避免每 30 分钟刷屏
        state["last_failure_notify_date"] = today
        self._save_state(state)
        self._notify(
            "WorkBuddy 签到失败，需要检查",
            "【签到失败】今日自动领积分没成功\n"
            "· 原因：%s\n"
            "· 提示：若写明令牌失效，请在 Windows 上重新运行 tools/export_token.bat，"
            "把新令牌填回插件设置页" % msg,
        )

    # ================= 通知（复用 MP 已配好的企业微信/微信渠道） =================
    def _notify(self, title: str, text: str, force: bool = False):
        if not force and not self._notify_enabled():
            return
        try:
            from app.command import CommandChain
            CommandChain().post_message(Notification(
                mtype=NotificationType.Manual, title=title, text=text))
            logger.info("%s 通知已发送：%s", TAG, title)
            return
        except Exception as e:
            logger.error("%s 发送通知失败：%s", TAG, e)
        # 兜底：至少写进 MP 系统消息
        try:
            from app.helper.message import MessageHelper
            MessageHelper().put(title=title, message=text, role="plugin")
        except Exception:
            pass

    # ================= 状态展示 =================
    def _status_dict(self) -> dict:
        s = self._load_state()
        return {
            "enabled": self.get_state(),
            "today": datetime.now(CST).date().isoformat(),
            "last_status": s.get("last_status") or "none",
            "last_success_date": s.get("last_success_date") or "",
            "last_run": s.get("last_run") or "",
            "credit": s.get("credit") or 0,
            "streak_days": s.get("streak_days") or 0,
            "total_credits": s.get("total_credits") or 0,
            "token_updated_at": s.get("token_updated_at") or "",
            "last_error": s.get("last_error") or "",
            "check_at": self._check_at(),
            "notify": self._notify_enabled(),
        }

    def _status_text(self) -> str:
        s = self._status_dict()
        names = {"success": "已领取", "already": "今日已签到",
                 "inactive": "活动未开放", "none": "尚未执行"}
        return ("今日状态：%s\n"
                "· 今日积分：+%s\n"
                "· 连签天数：%s 天\n"
                "· 本期累计：%s 分\n"
                "· 上次运行：%s\n"
                "· 令牌轮换：%s\n"
                "· 最近错误：%s\n"
                "· 每日执行：%s（UTC+8）"
                % (names.get(s["last_status"], s["last_status"]), s["credit"],
                   s["streak_days"], s["total_credits"], s["last_run"] or "无",
                   s["token_updated_at"] or "无", s["last_error"] or "无", s["check_at"]))
