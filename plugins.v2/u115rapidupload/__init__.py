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
import os
import threading
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
    plugin_desc = "监控本地目录（支持下载器监控/目录实时监控/定时巡逻三种触发），能秒传就传 115、通知并删除源文件；不能秒传先拦截，之后每天再验证"
    plugin_icon = ""
    plugin_version = "1.3"
    plugin_author = "celestial520"
    author_url = "https://github.com/celestial520/mp-plugins"
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
    # 秒传失败后，在"第二天"的哪个时刻再验证（HH:MM）
    _retry_time: str = "09:00"
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
    # 目录监控：用文件系统事件实时感知新文件
    _trigger_watch: bool = True
    # 文件事件落定等待（秒）：最后一次写入后静置多久才处理
    _watch_settle_sec: int = 60
    # 下载器监控：查 MP 下载器里已完成的任务，命中监控目录就立刻触发
    _trigger_downloader: bool = True
    # 每天定时清理监控目录下的空文件夹（不删监控目录本身）
    _cleanup_enabled: bool = True
    # 清理空文件夹的时刻（HH:MM）
    _cleanup_time: str = "03:30"

    # ---------------------------------------------------------------- 运行时
    _state: Dict[str, dict] = {}
    _running: bool = False
    _last_run_at: float = 0.0
    _last_result: List[dict] = []
    _pending_tasks: List[dict] = []
    # 文件系统事件待处理：path -> 最后一次变化的 mtime/时间戳
    _fs_pending: Dict[str, float] = {}
    _stop_watch: Optional[threading.Event] = None
    _watch_thread: Optional[threading.Thread] = None
    # 下载器监控里已经处理过的 hash，避免重复触发
    _seen_torrents: Dict[str, float] = {}
    _trigger_stats: Dict[str, Any] = {}

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
            self._retry_time = self._normalize_daily_time(config.get("retry_time")) or "09:00"
            self._min_size_mb = self._to_int(config.get("min_size_mb"), 100, 0, 1024 * 1024)
            self._ext_filter = str(config.get("ext_filter") or "").strip()
            mode = str(config.get("transfer_mode") or "").strip().lower()
            if mode not in ("move", "copy"):
                # 兼容旧配置：没有 transfer_mode 时按原来的开关推导
                mode = "move" if bool(config.get("delete_source", True)) else "copy"
            self._transfer_mode = mode
            self._delete_source = mode == "move"
            self._real_upload = bool(config.get("real_upload", False))
            self._notify = bool(config.get("notify", True))
            self._notify_hold = bool(config.get("notify_hold", True))
            self._trigger_watch = bool(config.get("trigger_watch", True))
            self._watch_settle_sec = self._to_int(
                config.get("watch_settle_sec"), 60, 5, 3600
            )
            self._trigger_downloader = bool(config.get("trigger_downloader", True))
            self._cleanup_enabled = bool(config.get("cleanup_enabled", True))
            self._cleanup_time = self._normalize_daily_time(config.get("cleanup_time")) or "03:30"
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
                f"115 {self._target_dir}，巡逻间隔 {self._interval_min} 分钟，"
                f"目录监控={'开' if self._trigger_watch else '关'}，"
                f"下载器监控={'开' if self._trigger_downloader else '关'}，"
                f"每日重试={self._retry_time}，"
                f"空目录清理={'每天 ' + self._cleanup_time if self._cleanup_enabled else '关'}"
            )
            self._start_watcher()

    def stop_service(self):
        self._pending_tasks = []
        self._stop_watcher()
        self._save_state()

    def get_state(self) -> bool:
        return self._enabled

    # ================================================================ 定时服务

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        services: List[Dict[str, Any]] = [
            {
                "id": "U115RapidUpload",
                "name": "115秒传·巡逻扫描",
                "trigger": "interval",
                "func": self.check_once,
                "kwargs": {"minutes": max(1, int(self._interval_min or 30))},
            }
        ]
        # 目录监控 / 下载器监控：每分钟把事件攒起来的文件推进处理
        if self._trigger_watch or self._trigger_downloader:
            services.append(
                {
                    "id": "U115RapidTrigger",
                    "name": "115秒传·文件与下载器监控",
                    "trigger": "interval",
                    "func": self.monitor_tick,
                    "kwargs": {"seconds": 60},
                }
            )
        # 每天到点重试验证（把昨天拦截的文件再验一遍）
        hour, minute = self._parse_time(self._retry_time)
        if hour is not None:
            services.append(
                {
                    "id": "U115RapidRetry",
                    "name": f"115秒传·每日重试 {self._retry_time}",
                    "trigger": "cron",
                    "func": self.daily_patrol,
                    "kwargs": {"hour": hour, "minute": minute},
                }
            )
        # 每天到点清理监控目录下的空文件夹
        ch, cm = self._parse_time(self._cleanup_time)
        if self._cleanup_enabled and ch is not None:
            services.append(
                {
                    "id": "U115RapidCleanup",
                    "name": f"115秒传·清理空目录 {self._cleanup_time}",
                    "trigger": "cron",
                    "func": self.cleanup_empty_dirs,
                    "kwargs": {"hour": ch, "minute": cm},
                }
            )
        return services

    # ================================================================ 触发源

    def daily_patrol(self, **kwargs) -> None:
        """
        每天到点跑一次：把到达重试时间的文件（含昨天拦截的）重新验证
        """
        logger.info("【115秒传】每日定时巡逻开始")
        self.check_once(force=True)

    def monitor_tick(self, **kwargs) -> None:
        """
        每分钟执行：处理目录监控攒下的文件事件 + 检查下载器已完成任务
        """
        if not self._enabled:
            return
        triggered: List[Path] = []
        try:
            triggered.extend(self._consume_fs_events())
        except Exception as err:
            logger.error(f"【115秒传】处理目录事件失败：{err}")
        if self._trigger_downloader:
            try:
                triggered.extend(self._downloader_completed_paths())
            except Exception as err:
                logger.debug(f"【115秒传】检查下载器任务失败：{err}")
        if not triggered:
            return
        # 去重后立刻处理，且跳过"稳定期"检查（下载器/事件已经说明写完了）
        unique: List[Path] = []
        seen = set()
        for path in triggered:
            key = path.as_posix()
            if key in seen or not path.exists() or not path.is_file():
                continue
            seen.add(key)
            unique.append(path)
        if unique:
            logger.info(f"【115秒传】监控触发：{len(unique)} 个文件立即处理")
            self._trigger_stats["last_trigger_at"] = time.strftime("%m-%d %H:%M:%S")
            self._trigger_stats["last_trigger_count"] = len(unique)
            self.check_once(paths=unique, force_stable=True)

    def _start_watcher(self) -> None:
        """
        目录监控：用 watchfiles 监听监控目录，只记录变化，真正的处理交给 monitor_tick
        """
        if not self._trigger_watch or not self._watch_dir:
            return
        try:
            import watchfiles  # noqa: F401
        except Exception as err:
            logger.warn(f"【115秒传】当前环境没有 watchfiles，目录监控退化为巡逻扫描：{err}")
            self._trigger_watch = False
            return
        if self._watch_thread and self._watch_thread.is_alive():
            return
        watch_path = Path(self._watch_dir)
        if not watch_path.exists():
            logger.warn(f"【115秒传】监控目录不存在，目录监控未启动：{watch_path}")
            return
        self._stop_watch = threading.Event()

        def _loop():
            from watchfiles import watch

            try:
                for changes in watch(
                    str(watch_path),
                    stop_event=self._stop_watch,
                    recursive=self._recursive,
                    debounce=2000,
                    step=500,
                ):
                    if self._stop_watch.is_set():
                        break
                    now = time.time()
                    for _change, raw in changes:
                        self._fs_pending[str(raw)] = now
            except Exception as err:
                logger.error(f"【115秒传】目录监控线程退出：{err}")

        self._watch_thread = threading.Thread(
            target=_loop, name="U115RapidWatcher", daemon=True
        )
        self._watch_thread.start()
        logger.info(f"【115秒传】目录监控已启动：{watch_path}（递归={self._recursive}）")

    def _stop_watcher(self) -> None:
        try:
            if self._stop_watch:
                self._stop_watch.set()
        except Exception:
            pass
        self._watch_thread = None
        self._fs_pending = {}

    def _consume_fs_events(self) -> List[Path]:
        """
        取出静置超过 watch_settle_sec 的文件事件，返回可以处理的文件
        """
        if not self._fs_pending:
            return []
        now = time.time()
        settle = max(5, int(self._watch_settle_sec or 60))
        ready: List[Path] = []
        remain: Dict[str, float] = {}
        for raw, stamp in list(self._fs_pending.items()):
            path = Path(raw)
            # 目录变化时，把目录里的文件也带上（下载工具通常先建目录再写文件）
            if path.is_dir():
                if now - float(stamp) < settle:
                    remain[raw] = stamp
                    continue
                try:
                    for child in path.rglob("*") if self._recursive else path.glob("*"):
                        if child.is_file():
                            ready.append(child)
                except Exception:
                    pass
                continue
            if not path.exists():
                continue
            if now - float(stamp) < settle:
                remain[raw] = stamp
                continue
            ready.append(path)
        self._fs_pending = remain
        return ready

    def _downloader_completed_paths(self) -> List[Path]:
        """
        下载器监控：查 MoviePilot 里各下载器"已完成"的任务，
        任务保存路径落在监控目录内的，立刻把对应文件交给秒传验证。
        """
        try:
            from app.core.module import ModuleManager
            from app.schemas.types import ModuleType
        except Exception as err:
            logger.debug(f"【115秒传】无法加载下载器模块接口：{err}")
            return []

        watch_root = Path(self._watch_dir) if self._watch_dir else None
        if not watch_root:
            return []

        result: List[Path] = []
        try:
            modules = list(
                ModuleManager().get_running_type_modules(ModuleType.Downloader)
            )
        except Exception as err:
            logger.debug(f"【115秒传】获取下载器列表失败：{err}")
            return []

        for module in modules:
            getter = getattr(module, "get_completed_torrents", None)
            if not getter:
                continue
            try:
                torrents = getter() or []
            except Exception as err:
                logger.debug(f"【115秒传】查询下载器已完成任务失败：{err}")
                continue
            for torrent in torrents:
                try:
                    torrent_hash = str(getattr(torrent, "hash", "") or "")
                    if torrent_hash and self._seen_torrents.get(torrent_hash):
                        continue
                    save_path = getattr(torrent, "path", None)
                    if not save_path:
                        continue
                    save_path = Path(save_path)
                    if not save_path.exists():
                        continue
                    try:
                        if not save_path.is_relative_to(watch_root):
                            continue
                    except AttributeError:
                        # Python 3.8 兼容
                        if watch_root.as_posix() not in save_path.as_posix():
                            continue
                    if torrent_hash:
                        self._seen_torrents[torrent_hash] = time.time()
                    if save_path.is_file():
                        result.append(save_path)
                    else:
                        for child in save_path.rglob("*") if self._recursive else save_path.glob("*"):
                            if child.is_file():
                                result.append(child)
                    logger.info(
                        f"【115秒传】下载器任务已完成：{getattr(torrent, 'title', None) or save_path.name}"
                    )
                except Exception:
                    continue
        # 清理过期的 hash 记录
        if len(self._seen_torrents) > 500:
            cutoff = time.time() - 7 * 24 * 3600
            self._seen_torrents = {
                k: v for k, v in self._seen_torrents.items() if v > cutoff
            }
        return result

    # ================================================================ 主流程

    def check_once(
        self,
        force: bool = False,
        paths: Optional[List[Path]] = None,
        force_stable: bool = False,
        **kwargs,
    ) -> None:
        """
        扫描一轮。
        :param force: 忽略"每天只验证一次"的限制（手动触发/每日巡逻用）
        :param paths: 只处理指定文件（目录监控/下载器监控触发时传入）
        :param force_stable: 跳过"文件稳定"检查（已由事件确认写完）
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

            if paths:
                candidates = [p for p in paths if self._accept(p)]
                logger.info(f"【115秒传】监控触发处理 {len(candidates)} 个文件")
            else:
                candidates = self._list_candidates(watch)
            if not candidates:
                logger.debug("【115秒传】本轮没有待处理文件")
                self._finish(run_result)
                return

            for path in candidates:
                try:
                    item = self._process_file(storage, cid, path, force, force_stable)
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

    def _process_file(
        self,
        storage,
        cid: str,
        path: Path,
        force: bool = False,
        force_stable: bool = False,
    ) -> Optional[dict]:
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

        # 文件可能还在写入（监控事件已确认写完时可跳过）
        if not force and not force_stable and self._stable_minutes > 0:
            if time.time() - mtime < self._stable_minutes * 60:
                logger.debug(f"【115秒传】{path.name} 刚变更，等它稳定后再处理")
                return None

        # 不能秒传的，等到「第二天 _retry_time」再验证
        last_try = float(record.get("last_try") or 0)
        if not force and last_try:
            next_at = self._next_retry_ts(last_try)
            if next_at and time.time() < next_at:
                left = int((next_at - time.time()) / 60)
                logger.debug(
                    f"【115秒传】{path.name} 下次验证 "
                    f"{time.strftime('%m-%d %H:%M', time.localtime(next_at))}（约 {left} 分钟后）"
                )
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
                    "note": "网盘已有同名文件，"
                    + ("已移动（删除源文件）" if deleted else "已复制（保留源文件）"),
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
                "note": "已移动（删除源文件）" if deleted else "已复制（保留源文件）",
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
                        "note": "已移动（删除源文件）" if deleted else "已复制（保留源文件）",
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
            next_ts = self._next_retry_ts(record.get("last_try") or time.time())
            next_try = (
                time.strftime("%m-%d %H:%M", time.localtime(next_ts))
                if next_ts
                else "明天"
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

    def cleanup_empty_dirs(self, **kwargs) -> None:
        """
        每天定时清理：删除监控目录下已经空了的子文件夹。
        注意：监控目录本身（watch_dir）永远不会被删除。
        """
        if not self._cleanup_enabled:
            return
        root = Path(self._watch_dir) if self._watch_dir else None
        if not root or not root.exists() or not root.is_dir():
            return
        removed: List[str] = []
        try:
            for current, dirs, files in os.walk(root, topdown=False):
                cur = Path(current)
                if cur == root:
                    # 监控目录本身必须保留
                    continue
                try:
                    if any(cur.iterdir()):
                        continue
                    cur.rmdir()
                    removed.append(cur.relative_to(root).as_posix())
                    logger.info(f"【115秒传】清理空目录：{cur}")
                except OSError:
                    continue
        except Exception as err:
            logger.error(f"【115秒传】清理空目录出错：{err}")
        if removed:
            self._trigger_stats["last_cleanup_at"] = time.strftime("%m-%d %H:%M:%S")
            self._trigger_stats["last_cleanup_count"] = len(removed)
            if self._notify:
                try:
                    self.post_message(
                        mtype=NotificationType.Plugin,
                        title=f"115秒传：清理了 {len(removed)} 个空文件夹",
                        text="\n".join(removed[:15]),
                    )
                except Exception as err:
                    logger.error(f"【115秒传】发送清理通知失败：{err}")

    def action_run_now(self, **kwargs) -> None:
        """
        插件动作：立即运行一次（忽略重试时间限制，全部重新验证一遍）
        """
        logger.info("【115秒传】手动触发：立即运行一次")
        self.check_once(force=True)

    def get_actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "run_now",
                "name": "立即运行一次",
                "func": self.action_run_now,
            }
        ]

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
        result: List[Path] = []
        for path in iterator:
            try:
                if self._accept(path):
                    result.append(path)
            except Exception:
                continue
        result.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0)
        return result

    def _accept(self, path: Path) -> bool:
        """
        单个文件是否符合处理条件（扩展名/体积/隐藏文件）
        """
        try:
            path = Path(path)
            if not path.is_file():
                return False
            if path.name.startswith("."):
                return False
            allowed = self._ext_set()
            if allowed and path.suffix.lower().lstrip(".") not in allowed:
                return False
            min_size = max(0, int(self._min_size_mb or 0)) * 1024 * 1024
            if path.stat().st_size < min_size:
                return False
            return True
        except Exception:
            return False

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
    def _normalize_daily_time(value: Any) -> str:
        """
        把「07:30」「7:30」「0730」统一成 HH:MM，非法返回空串（=关闭）
        """
        raw = str(value or "").strip().replace("：", ":")
        if not raw:
            return ""
        try:
            if ":" in raw:
                parts = raw.split(":")
                hour, minute = int(parts[0]), int(parts[1])
            elif len(raw) in (3, 4) and raw.isdigit():
                hour, minute = int(raw[:-2]), int(raw[-2:])
            else:
                return ""
        except Exception:
            return ""
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return ""
        return f"{hour:02d}:{minute:02d}"

    def _parse_time(self, value: Any) -> Tuple[Optional[int], Optional[int]]:
        raw = self._normalize_daily_time(value)
        if not raw:
            return None, None
        hour, minute = raw.split(":")
        return int(hour), int(minute)

    def _next_retry_ts(self, last_try: float) -> Optional[float]:
        """
        「第二天 _retry_time」对应的时间戳：
        以 last_try 当天为基准往后一天，取那天的 HH:MM。
        """
        hour, minute = self._parse_time(self._retry_time)
        if hour is None:
            return None
        try:
            base = time.localtime(float(last_try or time.time()))
            day = time.mktime(
                (base.tm_year, base.tm_mon, base.tm_mday, 0, 0, 0, 0, 0, -1)
            )
            return day + 24 * 3600 + hour * 3600 + minute * 60
        except Exception:
            return None

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
            "retry_time": self._retry_time,
            "transfer_mode": self._transfer_mode,
            "delete_source": self._delete_source,
            "real_upload": self._real_upload,
            "trigger_watch": self._trigger_watch,
            "watch_settle_sec": self._watch_settle_sec,
            "trigger_downloader": self._trigger_downloader,
            "cleanup_enabled": self._cleanup_enabled,
            "cleanup_time": self._cleanup_time,
            "watch_thread_alive": bool(self._watch_thread and self._watch_thread.is_alive()),
            "fs_pending": len(self._fs_pending),
            "trigger_stats": self._trigger_stats,
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

    # ================================================================ 路径候选

    def _local_dir_items(self) -> List[str]:
        """
        给「本地监控目录」下拉框准备候选路径（MP 的 JSON 表单没有文件夹浏览器，
        所以做成"下拉可选 + 也可以手填"）
        """
        items: List[str] = []
        current = (self._watch_dir or "").strip()
        if current:
            items.append(current)
        bases = ["/media", "/downloads", "/video", "/mnt", "/config", "/"]
        for base in bases:
            try:
                base_path = Path(base)
                if not base_path.exists() or not base_path.is_dir():
                    continue
                if base not in items:
                    items.append(base)
                sub_dirs = [c for c in sorted(base_path.iterdir()) if c.is_dir()][:20]
                for sub in sub_dirs:
                    text = sub.as_posix()
                    if text in items:
                        continue
                    items.append(text)
                    try:
                        for grand in [c for c in sorted(sub.iterdir()) if c.is_dir()][:10]:
                            gtext = grand.as_posix()
                            if gtext not in items:
                                items.append(gtext)
                    except Exception:
                        pass
            except Exception:
                continue
        return items[:150]

    def _115_dir_items(self) -> List[str]:
        """
        给「115 目标目录」下拉框准备候选（列出网盘根目录下的一级文件夹）
        """
        cached = self._trigger_stats.get("_115_items")
        if cached and time.time() - float(cached[1]) < 600:
            return list(cached[0])
        items: List[str] = []
        current = (self._target_dir or "/").strip() or "/"
        items.append(current)
        items.append("/")
        try:
            if U115Pan:
                storage = U115Pan()
                if storage.check():
                    root = type(
                        "RootItem", (), {"path": "/", "type": "dir", "fileid": "0"}
                    )()
                    for folder in storage.list(root) or []:
                        if getattr(folder, "type", "") == "dir":
                            path_text = getattr(folder, "path", None)
                            if path_text:
                                items.append(str(path_text).rstrip("/") or "/")
        except Exception as err:
            logger.debug(f"【115秒传】获取 115 目录列表失败：{err}")
        items = list(dict.fromkeys(items))
        self._trigger_stats["_115_items"] = (items, time.time())
        return items[:100]

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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "transfer_mode",
                                            "label": "秒传后的处理方式",
                                            "items": [
                                                {
                                                    "title": "移动（删除本地源文件）",
                                                    "value": "move",
                                                },
                                                {
                                                    "title": "复制（保留本地源文件）",
                                                    "value": "copy",
                                                },
                                            ],
                                            "item-title": "title",
                                            "item-value": "value",
                                            "hint": "只影响本地文件；115 网盘上的文件两种方式都会创建",
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
                                        "component": "VAutocomplete",
                                        "props": {
                                            "model": "watch_dir",
                                            "label": "本地监控目录",
                                            "items": self._local_dir_items(),
                                            "placeholder": "点右侧箭头选择，或直接输入容器内路径",
                                            "hint": "要监控哪个文件夹。点开下拉可看到常用目录，也可以手动输入",
                                            "clearable": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VAutocomplete",
                                        "props": {
                                            "model": "target_dir",
                                            "label": "115 目标目录",
                                            "items": self._115_dir_items(),
                                            "placeholder": "点右侧箭头选择网盘目录，或直接输入",
                                            "hint": "网盘里的路径，不存在会自动创建；根目录填 /",
                                            "clearable": True,
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
                                            "model": "stable_minutes",
                                            "label": "文件稳定（分钟）",
                                            "type": "number",
                                            "hint": "只有巡逻扫描会等，监控触发不等",
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ext_filter",
                                            "label": "扩展名白名单",
                                            "placeholder": "mkv,mp4,ts,iso",
                                            "hint": "逗号分隔，留空不限",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "retry_time",
                                            "label": "不能秒传时，第二天几点再检测",
                                            "type": "time",
                                            "hint": "点右侧时钟图标选择时间",
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
                                            "model": "cleanup_enabled",
                                            "label": "每天清理空文件夹",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cleanup_time",
                                            "label": "每天几点清理空文件夹",
                                            "type": "time",
                                            "hint": "只删空子目录，监控目录本身永远保留",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "trigger_watch",
                                            "label": "目录监控（文件变化实时触发）",
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
                                            "model": "trigger_downloader",
                                            "label": "下载器监控（下载完成即触发）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "watch_settle_sec",
                                            "label": "文件写入静置（秒）",
                                            "type": "number",
                                            "hint": "最后一次变化后静置多久才处理",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "recursive", "label": "递归扫描子目录"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送微信通知"},
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
                                            "model": "notify_hold",
                                            "label": "文件被拦截时也通知",
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
                                                "工作流程：发现监控目录里白名单文件 → 计算特征值 → 调 115 秒传接口验证。"
                                                "能秒传：创建网盘文件、发通知，再按上面选的"
                                                "「移动」删掉本地源文件 /「复制」保留本地源文件"
                                                "（空文件夹统一留到每天定时清理）。"
                                                "不能秒传：原样保留，等到「第二天设定的时间」再验证。"
                                                "触发方式三种可同时开：下载器监控 / 目录监控 / 巡逻扫描。"
                                                "想马上跑一次，用插件卡片菜单里的「立即运行一次」。"
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
            "retry_time": "09:00",
            "min_size_mb": 100,
            "ext_filter": "mkv,mp4,ts,iso,m2ts,avi,rmvb,flac,ape,wav",
            "transfer_mode": "move",
            "real_upload": False,
            "cleanup_enabled": True,
            "cleanup_time": "03:30",
            "notify": True,
            "notify_hold": True,
            "trigger_watch": True,
            "watch_settle_sec": 60,
            "trigger_downloader": True,
        }

    def get_page(self) -> List[dict]:
        return []
