"""
115 秒传搬运（U115RapidUpload）

需求背景：
  监控一个本地目录，文件落进来后先验证 115 是否能"秒传"（服务器已有相同内容）。
  · 能秒传 → 立即创建网盘文件（不消耗上传流量）→ 发微信通知 → 删除本地源文件；
  · 不能秒传 → 拦截，不传，保持原样，第二天再验证一次，直到能秒传为止。

实现要点：
  · 完全复用 MoviePilot 自带的 115 存储实现（app.modules.filemanager.storages.u115.U115Pan），
    也就是你在 MP 里授权过的「115 开放平台（OpenAPI）」令牌，插件不保存、不搬运任何凭证；
  · 秒传检测 = POST /open/upload/init（fileid=SHA1、preid=前128MB的SHA1、target=U_1_{目录CID}），
    返回 data.status == 2 表示秒传成功（网盘侧文件已创建）；
  · 遇到 700/701 二次认证（sign_check）会按官方要求计算区间 SHA1 后重新 init，提高命中率；
  · 大文件 SHA1 用 4MB 分块单遍同时算全量与前 128MB，避免读两遍盘。
"""

import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

# MoviePilot 内置的 115 存储（走 115 开放平台授权）
try:
    from app.modules.filemanager.storages.u115 import U115Pan

    _U115_IMPORT_ERROR: Optional[str] = None
except Exception as _err:  # pragma: no cover - 仅在版本不匹配时触发
    U115Pan = None
    _U115_IMPORT_ERROR = str(_err)


class U115RapidUpload(_PluginBase):
    """
    115 秒传搬运
    """

    plugin_name = "115秒传搬运"
    plugin_desc = "监控本地目录，文件能秒传就传 115、通知并删除源文件；不能秒传就先拦截，第二天再验证"
    plugin_icon = ""
    plugin_version = "1.0"
    plugin_author = "celestial520"
    author_url = "https://github.com/celestial520"
    plugin_config_prefix = "u115rapid_"
    plugin_order = 20
    auth_level = 1

    # ---------------------------------------------------------------- 配置项
    _enabled: bool = False
    # 本地监控目录
    _watch_dir: str = ""
    # 115 目标目录（网盘路径，如 /电影）
    _target_dir: str = "/"
    # 是否递归扫描子目录
    _recursive: bool = True
    # 巡逻间隔（分钟）
    _interval_min: int = 30
    # 文件大小/修改时间需要稳定多少分钟才认为"下载完成"
    _stable_minutes: int = 5
    # 秒传失败后，隔多少小时再验证一次（默认 24 = 第二天）
    _retry_hours: int = 24
    # 体积下限（MB），小于该值跳过
    _min_size_mb: int = 100
    # 扩展名白名单（逗号分隔，留空表示不限）
    _ext_filter: str = "mkv,mp4,ts,iso,m2ts,avi,rmvb,flac,ape,wav"
    # 秒传成功后删除本地源文件
    _delete_source: bool = True
    # 秒传失败时是否退化为真实上传（默认关闭 = 拦截）
    _real_upload: bool = False
    # 是否发送通知
    _notify: bool = True
    # 首次拦截时也发通知
    _notify_hold: bool = True

    # ---------------------------------------------------------------- 运行时
    _state: Dict[str, dict] = {}
    _running: bool = False
    _last_run_at: float = 0.0
    _last_result: List[dict] = []
    _pending_tasks: List[dict] = []

    # 115 目录 CID 缓存
    _cid_cache: Dict[str, str] = {}
    _preid_size = 128 * 1024 * 1024
    _read_chunk = 4 * 1024 * 1024
    _state_keep = 1000

    # ================================================================ 生命周期

    def init_plugin(self, config: dict = None):
        self._pending_tasks = []
        self._last_result = []
        if config:
            self._enabled = bool(config.get("enabled"))
            self._watch_dir = str(config.get("watch_dir") or "").strip()
            self._target_dir = str(config.get("target_dir") or "/").strip() or "/"
            self._recursive = bool(config.get("recursive", True))
            self._interval_min = self._to_int(config.get("interval_min"), 30, 1, 1440)
            self._stable_minutes = self._to_int(config.get("stable_minutes"), 5, 0, 1440)
            self._retry_hours = self._to_int(config.get("retry_hours"), 24, 1, 24 * 30)
            self._min_size_mb = self._to_int(config.get("min_size_mb"), 100, 0, 1024 * 1024)
            self._ext_filter = str(config.get("ext_filter") or "").strip()
            self._delete_source = bool(config.get("delete_source", True))
            self._real_upload = bool(config.get("real_upload", False))
            self._notify = bool(config.get("notify", True))
            self._notify_hold = bool(config.get("notify_hold", True))
        # 载入历史状态
        try:
            data = self.get_data("state")
            self._state = dict(data) if isinstance(data, dict) else {}
        except Exception as err:
            logger.error(f"【115秒传】读取状态失败：{err}")
            self._state = {}
        if self._enabled:
            logger.info(
                f"【115秒传】已启用：监控 {self._watch_dir or '(未设置)'} -> "
                f"115 {self._target_dir}，间隔 {self._interval_min} 分钟"
            )

    def stop_service(self):
        self._pending_tasks = []
        self._save_state()

    def get_state(self) -> bool:
        return self._enabled

    # ================================================================ 定时服务

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        return [
            {
                "id": "U115RapidUpload",
                "name": "115秒传监控",
                "trigger": "interval",
                "func": self.check_once,
                "kwargs": {"minutes": max(1, int(self._interval_min or 30))},
            }
        ]

    # ================================================================ 主流程

    def check_once(self, force: bool = False, **kwargs) -> None:
        """
        扫描一轮。force=True 时忽略"每天只验证一次"的限制（手动触发用）。
        """
        if not self._enabled and not force:
            return
        if self._running:
            logger.info("【115秒传】上一轮还在跑，跳过本次")
            return
        self._running = True
        self._last_run_at = time.time()
        run_result: List[dict] = []
        try:
            if not U115Pan:
                logger.error(f"【115秒传】当前版本无法加载 MP 的 115 存储：{_U115_IMPORT_ERROR}")
                self._finish(run_result)
                return
            watch = Path(self._watch_dir) if self._watch_dir else None
            if not watch or not watch.exists() or not watch.is_dir():
                logger.warn(f"【115秒传】监控目录不存在：{self._watch_dir}")
                self._finish(run_result)
                return

            storage = self._get_storage()
            if not storage:
                self._finish(run_result)
                return

            cid = self._resolve_target_cid(storage)
            if not cid:
                logger.error(f"【115秒传】无法解析 115 目标目录：{self._target_dir}")
                self._finish(run_result)
                return

            candidates = self._list_candidates(watch)
            if not candidates:
                logger.debug("【115秒传】本轮没有待处理文件")
                self._finish(run_result)
                return

            for path in candidates:
                try:
                    item = self._process_file(storage, cid, path, force)
                except Exception as err:
                    logger.error(f"【115秒传】处理 {path.name} 出错：{err}")
                    item = {"file": path.name, "status": "error", "note": str(err)}
                if item:
                    run_result.append(item)
                self._save_state()
        finally:
            self._finish(run_result)
            self._running = False

    def _finish(self, run_result: List[dict]) -> None:
        self._last_result = run_result[-30:]
        self._save_state()
        if run_result:
            logger.info(
                "【115秒传】本轮结果："
                + "；".join(f"{i['file']}={i['status']}" for i in run_result)
            )
            self._notify_result(run_result)

    def _process_file(self, storage, cid: str, path: Path, force: bool = False) -> Optional[dict]:
        """
        处理单个文件，返回结果字典
        """
        try:
            stat = path.stat()
        except Exception:
            return None
        size, mtime = int(stat.st_size), int(stat.st_mtime)
        key = self._file_key(path)
        record = self._state.get(key) or {}

        # 文件内容变了（重新下载/替换），重置记录
        if record.get("size") != size or record.get("mtime") != mtime:
            if record:
                logger.info(f"【115秒传】{path.name} 内容已变化，重新验证")
            record = {"size": size, "mtime": mtime, "first_seen": time.time(), "tries": 0}
            self._state[key] = record

        if record.get("status") == "done":
            return None

        # 文件可能还在写入
        if not force and self._stable_minutes > 0:
            if time.time() - mtime < self._stable_minutes * 60:
                logger.debug(f"【115秒传】{path.name} 刚变更，等它稳定后再处理")
                return None

        # 每天只验证一次
        last_try = float(record.get("last_try") or 0)
        if not force and last_try:
            gap = max(1, int(self._retry_hours or 24)) * 3600
            if time.time() - last_try < gap:
                left = int((gap - (time.time() - last_try)) / 60)
                logger.debug(f"【115秒传】{path.name} 距上次验证不足，约 {left} 分钟后再试")
                return None

        # 网盘上已有同名文件（说明之前已经传过），直接收尾
        try:
            target_path = Path(self._target_dir or "/") / path.name
            if storage.get_item(target_path) is not None:
                record["status"] = "done"
                record["note"] = "网盘已有同名文件"
                deleted = self._delete_local(path)
                return {
                    "file": path.name,
                    "status": "已存在",
                    "size": self._fmt_size(size),
                    "note": "已删除源文件" if deleted else record["note"],
                    "tries": record.get("tries") or 0,
                }
        except Exception as err:
            logger.debug(f"【115秒传】检查网盘同名文件失败：{err}")

        # 计算特征值（带缓存）
        sha1 = record.get("sha1")
        preid = record.get("preid")
        if not sha1 or not preid:
            started = time.time()
            sha1, preid = self._hash_pair(path)
            logger.info(
                f"【115秒传】{path.name} 特征值计算完成（{self._fmt_size(size)}，"
                f"耗时 {int(time.time() - started)}s）"
            )
            record["sha1"], record["preid"] = sha1, preid

        record["last_try"] = time.time()
        record["tries"] = int(record.get("tries") or 0) + 1

        name = path.name
        ok, status_code, detail = self._try_rapid(
            storage, cid, name, size, sha1, preid, local_path=path
        )

        if ok:
            record["status"] = "done"
            record["note"] = "秒传成功"
            logger.info(f"【115秒传】{name} 秒传成功（第 {record['tries']} 次验证）")
            # 校验网盘上确实有这个文件（失败也不影响秒传结果）
            try:
                target_path = Path(self._target_dir or "/") / name
                if storage.get_item(target_path) is None:
                    time.sleep(2)
                    if storage.get_item(target_path) is None:
                        logger.warn(f"【115秒传】{name} 秒传返回成功，但网盘暂未查到，请稍后核对")
            except Exception as err:
                logger.debug(f"【115秒传】核对网盘文件失败：{err}")
            # 删除源文件
            deleted = self._delete_local(path)
            if deleted:
                record["deleted"] = True
            return {
                "file": name,
                "status": "秒传成功",
                "size": self._fmt_size(size),
                "note": "已删除源文件" if deleted else detail,
                "tries": record["tries"],
            }

        if status_code == "need_upload":
            if self._real_upload:
                logger.info(f"【115秒传】{name} 无法秒传，按配置执行真实上传")
                record["note"] = "改为真实上传"
                self._save_state()
                uploaded = self._real_upload_file(storage, path)
                if uploaded:
                    record["status"] = "done"
                    record["note"] = "真实上传成功"
                    deleted = self._delete_local(path)
                    if deleted:
                        record["deleted"] = True
                    return {
                        "file": name,
                        "status": "上传成功",
                        "size": self._fmt_size(size),
                        "note": "已删除源文件" if deleted else "",
                        "tries": record["tries"],
                    }
                record["note"] = "真实上传失败"
                return {
                    "file": name,
                    "status": "上传失败",
                    "size": self._fmt_size(size),
                    "note": detail,
                    "tries": record["tries"],
                }

            record["status"] = "held"
            record["note"] = detail
            next_try = time.strftime(
                "%m-%d %H:%M", time.localtime(time.time() + max(1, self._retry_hours) * 3600)
            )
            logger.info(f"【115秒传】{name} 暂不能秒传，已拦截，{next_try} 后再验证")
            return {
                "file": name,
                "status": "暂不能秒传",
                "size": self._fmt_size(size),
                "note": f"已拦截，{next_try} 后再试",
                "tries": record["tries"],
                "first_hold": record["tries"] == 1,
            }

        # 出错（授权/网络/接口异常）
        record["note"] = detail
        logger.warn(f"【115秒传】{name} 验证失败：{detail}")
        return {
            "file": name,
            "status": "验证失败",
            "size": self._fmt_size(size),
            "note": detail,
            "tries": record["tries"],
        }

    # ================================================================ 115 交互

    def _get_storage(self):
        """
        取 MP 里已授权的 115 存储实例（WeakSingleton，配置与令牌都来自 MP）
        """
        try:
            storage = U115Pan()
        except Exception as err:
            logger.error(f"【115秒传】获取 115 存储实例失败：{err}")
            return None
        try:
            if not storage.check():
                logger.error(
                    "【115秒传】115 未授权或授权已失效，请在 MP 设置 → 存储 → 115网盘 重新授权"
                )
                self._notify_once(
                    "auth",
                    "115秒传搬运：115 未授权",
                    "MoviePilot 里的 115 网盘授权不可用，插件无法验证秒传。\n"
                    "请到 设置 → 存储 → 115网盘 重新授权（支持扫码/OpenAPI）。",
                )
                return None
        except Exception as err:
            logger.error(f"【115秒传】检查 115 授权状态失败：{err}")
            return None
        return storage

    def _resolve_target_cid(self, storage) -> Optional[str]:
        target = (self._target_dir or "/").strip() or "/"
        if target in ("/", ""):
            return "0"
        if target in self._cid_cache:
            return self._cid_cache[target]
        try:
            folder = storage.get_folder(Path(target))
        except Exception as err:
            logger.error(f"【115秒传】获取目标目录失败：{target} - {err}")
            return None
        if not folder or not getattr(folder, "fileid", None):
            logger.error(f"【115秒传】目标目录解析失败（可能路径不对）：{target}")
            return None
        self._cid_cache[target] = str(folder.fileid)
        return self._cid_cache[target]

    def _try_rapid(
        self,
        storage,
        cid: str,
        name: str,
        size: int,
        sha1: str,
        preid: str,
        local_path: Optional[Path] = None,
    ) -> Tuple[bool, str, str]:
        """
        验证/执行秒传。返回 (是否成功, 状态标识, 说明)
        115 的 /open/upload/init：data.status == 2 表示秒传命中（网盘文件已创建）
        """
        init_data = {
            "file_name": name,
            "file_size": size,
            "target": f"U_1_{cid}",
            "fileid": sha1,
            "preid": preid,
        }
        resp = self._request(storage, init_data)
        if resp is None:
            return False, "error", "接口无响应"
        data = resp.get("data") or {}
        if not resp.get("state") and not data:
            return False, "error", f"初始化失败：{resp.get('error') or resp.get('message')}"

        # 二次认证：115 要求核对文件的某个字节区间的 SHA1
        code = data.get("code")
        sign_check = data.get("sign_check")
        if code in (700, 701) and sign_check and local_path:
            sign_val = self._calc_sign_val(local_path, sign_check)
            if sign_val:
                init_data.update(
                    {
                        "pick_code": data.get("pick_code"),
                        "sign_key": data.get("sign_key"),
                        "sign_val": sign_val,
                    }
                )
                resp = self._request(storage, init_data)
                if resp is None:
                    return False, "error", "二次认证接口无响应"
                if not resp.get("state"):
                    return False, "error", f"二次认证失败：{resp.get('error')}"
                data = resp.get("data") or {}

        status = data.get("status")
        if status == 2:
            return True, "rapid", "秒传成功"
        if status == 1:
            return False, "need_upload", "115 上没有相同内容，需要真实上传"
        # 其他情况（如 code 700/701 但没拿到 sign_check）都按"不能秒传"处理
        return False, "need_upload", f"未命中秒传（status={status}, code={code}）"

    def _request(self, storage, init_data: dict) -> Optional[dict]:
        try:
            # 700/701 是"需要二次认证"，属于正常流程，不能当失败丢弃
            return storage._request_api(
                "POST",
                "/open/upload/init",
                data=init_data,
                accepted_codes=(0, 20004, 700, 701),
            )
        except Exception as err:
            logger.error(f"【115秒传】调用 /open/upload/init 异常：{err}")
            return None

    def _calc_sign_val(self, path: Path, sign_check: str) -> Optional[str]:
        """
        sign_check 形如 "2392148-2392298"，取该字节区间内容的 SHA1（大写）
        """
        try:
            start_text, end_text = str(sign_check).split("-")[:2]
            start, end = int(start_text), int(end_text)
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(max(0, end - start) + 1)
            return hashlib.sha1(chunk).hexdigest().upper()
        except Exception as err:
            logger.warn(f"【115秒传】计算二次认证值失败：{err}")
            return None

    def _real_upload_file(self, storage, path: Path) -> bool:
        """
        兜底：走 MP 自带的分片上传
        """
        try:
            folder = storage.get_folder(Path(self._target_dir or "/"))
            if not folder:
                logger.error(f"【115秒传】真实上传失败：目标目录不可用 {self._target_dir}")
                return False
            item = storage.upload(folder, path)
            return bool(item)
        except Exception as err:
            logger.error(f"【115秒传】真实上传异常：{err}")
            return False

    def _delete_local(self, path: Path) -> bool:
        """
        按配置删除本地源文件
        """
        if not self._delete_source:
            return False
        try:
            path.unlink()
            logger.info(f"【115秒传】已删除本地源文件：{path}")
            return True
        except Exception as err:
            logger.error(f"【115秒传】删除源文件失败：{path} - {err}")
            return False

    # ================================================================ 文件扫描

    def _list_candidates(self, watch: Path) -> List[Path]:
        try:
            iterator = watch.rglob("*") if self._recursive else watch.glob("*")
        except Exception as err:
            logger.error(f"【115秒传】遍历目录失败：{err}")
            return []
        allowed = self._ext_set()
        min_size = max(0, int(self._min_size_mb or 0)) * 1024 * 1024
        result: List[Path] = []
        for path in iterator:
            try:
                if not path.is_file():
                    continue
                if path.name.startswith("."):
                    continue
                if allowed and path.suffix.lower().lstrip(".") not in allowed:
                    continue
                if path.stat().st_size < min_size:
                    continue
                result.append(path)
            except Exception:
                continue
        result.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
        return result

    def _file_key(self, path: Path) -> str:
        try:
            base = Path(self._watch_dir)
            return path.relative_to(base).as_posix()
        except Exception:
            return path.as_posix()

    def _ext_set(self) -> set:
        raw = (self._ext_filter or "").replace("，", ",").replace(";", ",")
        return {i.strip().lower().lstrip(".") for i in raw.split(",") if i.strip()}

    def _hash_pair(self, path: Path) -> Tuple[str, str]:
        """
        单遍读盘，同时算全量 SHA1 与前 128MB 的 SHA1（115 的 fileid / preid）
        """
        sha1_all = hashlib.sha1()
        sha1_pre = hashlib.sha1()
        remain_pre = self._preid_size
        with open(path, "rb") as f:
            while True:
                chunk = f.read(self._read_chunk)
                if not chunk:
                    break
                sha1_all.update(chunk)
                if remain_pre > 0:
                    take = chunk[:remain_pre]
                    sha1_pre.update(take)
                    remain_pre -= len(take)
        return sha1_all.hexdigest(), sha1_pre.hexdigest()

    # ================================================================ 通知

    def _notify_result(self, run_result: List[dict]) -> None:
        if not self._notify or not run_result:
            return
        succeeded = [i for i in run_result if i["status"] in ("秒传成功", "上传成功")]
        held = [i for i in run_result if i["status"] == "暂不能秒传"]
        failed = [i for i in run_result if i["status"] in ("验证失败", "上传失败")]

        # 只关心：成功 / 首次拦截 / 失败
        first_holds = [i for i in held if i.get("first_hold")]
        if not succeeded and not failed and not (self._notify_hold and first_holds):
            return

        parts = f"成功 {len(succeeded)} 个，拦截 {len(held)} 个，失败 {len(failed)} 个"
        lines: List[str] = []
        for item in succeeded:
            lines.append(f"✅ {item['file']}（{item.get('size','')}）{item.get('note','')}")
        for item in first_holds if self._notify_hold else []:
            lines.append(f"⏸ {item['file']}（{item.get('size','')}）暂不能秒传，{item.get('note','')}")
        for item in failed:
            lines.append(f"❌ {item['file']}：{item.get('note','')}")

        title = "115秒传搬运"
        if succeeded:
            title = f"115秒传：{len(succeeded)} 个已入库"
        elif failed:
            title = "115秒传：处理失败"
        else:
            title = "115秒传：有文件暂不能秒传"

        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=title,
                text="\n".join([parts] + lines[:15]),
            )
        except Exception as err:
            logger.error(f"【115秒传】发送通知失败：{err}")

    def _notify_once(self, key: str, title: str, text: str) -> None:
        """
        同类提醒 6 小时内只发一次
        """
        if not self._notify:
            return
        record = self._state.get(f"!notify:{key}") or {}
        if time.time() - float(record.get("at") or 0) < 6 * 3600:
            return
        self._state[f"!notify:{key}"] = {"at": time.time()}
        try:
            self.post_message(mtype=NotificationType.Plugin, title=title, text=text)
        except Exception as err:
            logger.error(f"【115秒传】发送通知失败：{err}")

    # ================================================================ 状态持久化

    def _save_state(self) -> None:
        try:
            if len(self._state) > self._state_keep:
                keep = sorted(
                    self._state.items(),
                    key=lambda kv: float((kv[1] or {}).get("last_try") or 0),
                    reverse=True,
                )[: self._state_keep]
                self._state = dict(keep)
            self.save_data("state", self._state)
        except Exception as err:
            logger.debug(f"【115秒传】保存状态失败：{err}")

    @staticmethod
    def _to_int(value: Any, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except Exception:
            return default

    @staticmethod
    def _fmt_size(size: int) -> str:
        try:
            value = float(size)
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if value < 1024 or unit == "TB":
                    return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
                value /= 1024
        except Exception:
            pass
        return f"{size}B"

    # ================================================================ API

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/check",
                "endpoint": self.api_check,
                "methods": ["POST", "GET"],
                "summary": "立即扫描一轮",
                "description": "手动触发一次扫描，默认忽略「每天一次」的限制",
            },
            {
                "path": "/stat",
                "endpoint": self.api_stat,
                "methods": ["GET"],
                "summary": "查看状态",
                "description": "查看监控目录、目标目录、最近处理结果与文件状态",
            },
            {
                "path": "/clear",
                "endpoint": self.api_clear,
                "methods": ["POST", "GET"],
                "summary": "清空处理记录",
                "description": "清空插件内部的文件状态记录（不影响已传文件）",
            },
        ]

    def api_check(self, force: bool = True, **kwargs):
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        self.check_once(force=bool(force))
        return {"success": True, "data": self._last_result}

    def api_stat(self):
        storage_ok = False
        auth_error = ""
        if U115Pan:
            try:
                storage_ok = bool(U115Pan().check())
            except Exception as err:
                auth_error = str(err)
        else:
            auth_error = _U115_IMPORT_ERROR or "无法加载 115 存储"
        files = [
            {
                "file": k,
                "status": (v or {}).get("status"),
                "tries": (v or {}).get("tries"),
                "note": (v or {}).get("note"),
                "size": self._fmt_size(int((v or {}).get("size") or 0)),
                "last_try": time.strftime(
                    "%m-%d %H:%M:%S", time.localtime(float((v or {}).get("last_try") or 0))
                )
                if (v or {}).get("last_try")
                else None,
            }
            for k, v in list(self._state.items())
            if not k.startswith("!notify:")
        ][:50]
        return {
            "version": self.plugin_version,
            "enabled": self._enabled,
            "watch_dir": self._watch_dir,
            "target_dir": self._target_dir,
            "interval_min": self._interval_min,
            "retry_hours": self._retry_hours,
            "delete_source": self._delete_source,
            "real_upload": self._real_upload,
            "u115_authorized": storage_ok,
            "u115_error": auth_error,
            "last_run_at": time.strftime(
                "%m-%d %H:%M:%S", time.localtime(self._last_run_at)
            )
            if self._last_run_at
            else None,
            "last_result": self._last_result[-10:],
            "files": files,
        }

    def api_clear(self):
        self._state = {}
        self._save_state()
        return {"success": True}

    # ================================================================ 表单/页面

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
                                            "model": "delete_source",
                                            "label": "秒传成功后删除源文件",
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
                                            "model": "real_upload",
                                            "label": "不能秒传时改为真实上传",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "watch_dir",
                                            "label": "本地监控目录",
                                            "placeholder": "/downloads/115staging",
                                            "hint": "容器内路径，文件落进来就会被检测",
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
                                            "model": "target_dir",
                                            "label": "115 目标目录",
                                            "placeholder": "/电影",
                                            "hint": "网盘路径，不存在会自动创建；根目录填 /",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "interval_min",
                                            "label": "巡逻间隔（分钟）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry_hours",
                                            "label": "秒传失败后重试（小时）",
                                            "type": "number",
                                            "hint": "24 = 第二天再验证",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "stable_minutes",
                                            "label": "文件稳定（分钟）",
                                            "type": "number",
                                            "hint": "避免处理还在下载的文件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_size_mb",
                                            "label": "体积下限（MB）",
                                            "type": "number",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ext_filter",
                                            "label": "扩展名白名单",
                                            "placeholder": "mkv,mp4,ts,iso",
                                            "hint": "逗号分隔，留空表示不限制",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "recursive", "label": "递归扫描子目录"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送微信通知"},
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
                                                "本插件直接复用 MoviePilot 里已授权的「115网盘」存储"
                                                "（设置 → 存储 → 115网盘，OpenAPI 或扫码都行），"
                                                "不额外保存任何账号信息。"
                                                "流程：文件稳定后计算 SHA1 → 调用 115 秒传接口验证 → "
                                                "能秒传则创建网盘文件、发通知、删除源文件；"
                                                "不能秒传则原样保留，按设定的小时数后再验证一次。"
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
            "watch_dir": "",
            "target_dir": "/",
            "recursive": True,
            "interval_min": 30,
            "stable_minutes": 5,
            "retry_hours": 24,
            "min_size_mb": 100,
            "ext_filter": "mkv,mp4,ts,iso,m2ts,avi,rmvb,flac,ape,wav",
            "delete_source": True,
            "real_upload": False,
            "notify": True,
            "notify_hold": True,
        }

    def get_page(self) -> List[dict]:
        return []
