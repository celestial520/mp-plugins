import base64
import hashlib
import hmac
import re
import time
from collections import deque
from io import BytesIO
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests
from fastapi import Query, Request
from fastapi.responses import Response

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class DoubanImage(_PluginBase):
    """
    豆瓣通知图片代理

    问题背景：
      豆瓣图片 CDN 对不带 Referer 的请求一律返回 418，且常返回 webp；
      而企业微信的图文消息封面是由企业微信服务器主动抓取的（不带 Referer），
      所以 MoviePilot 直接把豆瓣海报地址作为 picurl 时，封面一定裂图。

    本插件做两件事：
      1. 发送通知前（post_message 钩子）把豆瓣图片地址改写为本机代理地址；
      2. 代理接口匿名对外开放，服务端补上 Referer 去取图，并保证返回 JPEG。

    v1.1：修复封面空白 —— 企业微信抓封面会先发 HEAD 探测，
          只有 GET 会返回 405，导致卡片有、封面空白。本版同时注册 GET/HEAD。

    v1.2：修复"封面是竖版海报被随意裁切" ——
          MP 的 get_message_image() 取 backdrop_path 作封面，而豆瓣数据源把
          「剧照第一张」直接当 backdrop，那张经常是竖图（例如 1080x1619）；
          没有 backdrop 时更会直接回退到竖版海报（2:3），企业微信按横版卡片
          裁切后就成了"随便切一刀"的观感。
          本版在发送前探测图片真实宽高，若判定为竖图，则回豆瓣的剧照接口
          （/movie|tv/{id}/photos）按宽高挑一张真正的横版剧照替换，
          从根上换成"专门的横屏海报"，而不是硬裁竖图。

    v1.3：改成"优先官方横图，没有就自己裁" ——
          实测 MP 网页订阅卡片里那张"横版背景"并不是独立的一张横图：
          订阅记录里的 backdrop 就是 1080x1619 的竖图，网页用 CSS 裁上部显示，
          而企业微信裁的是正中，于是只剩躯干。
          本版两级策略：
            ① 该条目豆瓣剧照里若有横图（宽高比≥1.35），直接用官方横图；
            ② 没有横图时，用 Pillow 把竖图**按上部**裁成 16:9（复现网页观感）。

    v1.4：修复 v1.3 完全没生效 ——
          MP 的 post_message 钩子只会传 message，**从不传 mediainfo**
          （见 ChainBase.post_message → messagequeue.send_message("post_message", message=...)），
          而 v1.3 的横版处理第一行就是 `if mediainfo is None: return`，
          所以竖图从来没被处理过，企业微信照旧从正中裁。
          本版改为不依赖 mediainfo：
            · 从通知标题反查豆瓣条目（/j/subject_suggest）拿到 douban_id；
            · 查不到条目或没有横图时，让代理端按上部裁成 16:9；
            · 代理端再加一道兜底：任何竖图默认自动裁成 16:9，
              即使钩子没来得及加参数也不会再出现"正中裁躯干"。
    """

    plugin_name = "豆瓣通知图片代理"
    plugin_desc = "把通知里的豆瓣海报换成本机代理地址；竖版封面优先换成官方横版剧照，没有则按上部裁成 16:9，解决微信/企业微信通知图片不显示或被裁得只剩中间一条"
    plugin_icon = ""
    plugin_version = "1.4"
    plugin_author = "celestial520"
    author_url = "https://github.com/celestial520"
    plugin_config_prefix = "doubanimage_"
    plugin_order = 1
    auth_level = 1

    _enabled: bool = False
    # 是否处理竖版封面
    _landscape: bool = True
    # True=优先用豆瓣横版剧照（有就用 A）；False=直接把竖图按上部裁成 16:9（B）
    _official_first: bool = True
    # 裁剪时保留的顶部起点（0.06 = 从 6% 处开始裁，避开顶部黑边/台标）
    _crop_top: float = 0.06
    # 简单内存缓存：key -> (content, media_type)
    _cache: Dict[str, Tuple[bytes, str]] = {}
    _cache_limit: int = 300
    # 图片尺寸缓存：url -> (width, height)
    _size_cache: Dict[str, Tuple[int, int]] = {}
    # 横版图缓存：douban_id -> (url, 时间戳)；url 为 None 表示查过但没有
    _landscape_cache: Dict[str, Tuple[Optional[str], float]] = {}
    _landscape_ttl: int = 6 * 3600
    # 标题 -> 豆瓣条目缓存：query -> (ctx, 时间戳)
    _subject_cache: Dict[str, Tuple[dict, float]] = {}
    _subject_ttl: int = 24 * 3600
    # 诊断信息（最近若干次改写 / 抓取 / 横版替换）
    _rewrites: Deque[dict] = deque(maxlen=20)
    _hits: Deque[dict] = deque(maxlen=40)
    _landscape_logs: Deque[dict] = deque(maxlen=20)

    _referer = "https://movie.douban.com/"
    _ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # 版本参数：企业微信会按 picurl 缓存抓图结果，带上版本号可绕过旧的失败缓存
    _cache_buster = "v14"

    # 豆瓣 App 接口（与 MP 内部 DoubanApi 一致，用于查剧照）
    _api_base = "https://frodo.douban.com/api/v2"
    _api_key = "0dad551ec0f84ed02907ff5c42e8ec70"
    _api_secret = "bf7dddc7c9cfe6f7"
    _api_ua = (
        "api-client/1 com.douban.frodo/7.22.0.beta9(231) Android/28 product/shark "
        "vendor/Xiaomi model/MI 9 brand/Xiaomi rom/android network/wifi platform/mobile"
    )
    # 判定为"横图"的最小宽高比（16:9≈1.78，3:2=1.5）
    _landscape_ratio = 1.35

    def init_plugin(self, config: dict = None):
        self._cache = {}
        self._size_cache = {}
        self._landscape_cache = {}
        self._rewrites = deque(maxlen=20)
        self._hits = deque(maxlen=40)
        self._landscape_logs = deque(maxlen=20)
        if config:
            self._enabled = bool(config.get("enabled"))
            self._landscape = bool(config.get("landscape", True))
            self._official_first = bool(config.get("official_first", True))

    def stop_service(self):
        self._cache = {}
        self._size_cache = {}
        self._landscape_cache = {}

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/img",
                "endpoint": self.img,
                # 关键：企业微信抓图会先发 HEAD，只注册 GET 会返回 405 导致封面空白
                "methods": ["GET", "HEAD"],
                "summary": "豆瓣图片代理",
                "description": "为通知封面提供免登录的豆瓣图片代理，自动补 Referer，支持 GET/HEAD",
                "allow_anonymous": True,
            },
            {
                "path": "/stat",
                "endpoint": self.stat,
                "methods": ["GET"],
                "summary": "代理诊断信息",
                "description": "查看最近的地址改写、横版替换与图片抓取记录，用于排查封面问题",
                "allow_anonymous": True,
            },
        ]

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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "landscape",
                                            "label": "竖版封面自动转横版",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "official_first",
                                            "label": "优先使用豆瓣横版剧照",
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
                                                "启用后，通知中的豆瓣海报会自动改写为："
                                                "你的 APP_DOMAIN + /api/v1/plugin/DoubanImage/img?url=...。"
                                                "该代理需要能从公网访问（企业微信服务器要抓图），"
                                                "并且同时支持 GET / HEAD 两种请求。"
                                                "「竖版封面自动转横版」开启时，封面若是 2:3 竖图，"
                                                "先找该条目的官方横版剧照；"
                                                "找不到横图才按上部裁成 16:9（与 MP 网页卡片的观感一致）。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"enabled": False, "landscape": True, "official_first": True}

    def get_page(self) -> List[dict]:
        return []

    def get_module(self) -> Dict[str, Any]:
        return {"post_message": self.post_message}

    # ------------------------------------------------------------------ 通知改写

    def post_message(self, message=None, mediainfo=None, **kwargs) -> None:
        """
        发送通知前改写封面地址。返回 None，不拦截原发送流程。
        """
        if not self._enabled or not message:
            return
        image = getattr(message, "image", None)
        if not self._is_http_url(image) and mediainfo is not None:
            # 有些通知类型没有带 image，尝试从媒体信息里补一张，至少让卡片有封面
            try:
                image = mediainfo.get_message_image() or image
            except Exception as err:
                logger.debug(f"豆瓣通知图片代理：读取媒体图片失败 {err}")
        if not self._is_http_url(image):
            self._record_rewrite(None, None, "该通知没有图片，跳过")
            return

        # 关键：竖版封面 -> 横版（优先官方横图，没有则裁上部）
        # 注意：MP 的钩子不会传 mediainfo，所以条目信息要靠标题反查
        ctx = self._resolve_context(message, mediainfo)
        final_image, extra = image, {}
        try:
            final_image, extra = self.ensure_landscape(image, ctx)
        except Exception as err:
            logger.warn(f"横版封面处理失败，继续使用原图：{err}")
            final_image, extra = image, {}

        proxy_url = self.build_proxy_url(final_image, extra)
        if not proxy_url:
            self._record_rewrite(final_image, None, "非豆瓣图片，保持原样")
            return
        try:
            message.image = proxy_url
            if final_image != image:
                suffix = "（已换横版剧照）"
            elif extra:
                suffix = "（代理端裁成横版）"
            else:
                suffix = ""
            self._record_rewrite(image, proxy_url, f"已改写为代理地址{suffix}")
            logger.info(f"豆瓣通知图片已改写为代理地址：{proxy_url}")
        except Exception as err:
            self._record_rewrite(image, None, f"改写失败：{err}")
            logger.warn(f"改写通知图片失败：{err}")

    # ------------------------------------------------------------------ 横版处理

    def ensure_landscape(
        self, image: str, ctx: Optional[dict] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """
        封面若是竖图，返回「横版地址 + 额外参数」：
          - 打开「优先使用豆瓣横版剧照」：先尝试换成本条目的官方横版剧照（A）
          - 没有官方横图：地址不变，交给代理端按上部裁成 16:9（B）
        ctx 由 _resolve_context() 得到，包含 douban_id / is_movie 等信息。
        """
        if not self._landscape:
            return image, {}
        if not self.is_douban_image(image):
            return image, {}

        size = self._probe_size(image)
        if not size:
            # 探测不到尺寸也照样让代理端兜底裁，避免又回到"正中裁"
            self._record_landscape(image, None, "无法探测尺寸，交由代理端兜底裁剪")
            return image, {"fit": "16x9", "top": self._crop_top}
        width, height = size
        ratio = width / float(height or 1)
        if ratio >= self._landscape_ratio:
            self._record_landscape(image, None, f"{width}x{height} 已是横图，无需处理")
            return image, {}

        ctx = ctx or {}
        douban_id = str(ctx.get("douban_id") or "").strip()
        if self._official_first:
            if douban_id:
                landscape = self._find_landscape(douban_id, bool(ctx.get("is_movie", True)))
                if landscape:
                    self._record_landscape(
                        image,
                        landscape,
                        f"{width}x{height} 竖图 -> 官方横版剧照"
                        f"（条目 {douban_id}，来源：{ctx.get('from') or '标题反查'}）",
                    )
                    return landscape, {}
                self._record_landscape(
                    image,
                    None,
                    f"{width}x{height} 竖图，条目 {douban_id} 没有可用横图，改裁上部",
                )
            else:
                self._record_landscape(
                    image,
                    None,
                    f"{width}x{height} 竖图，未解析到豆瓣条目，改裁上部",
                )

        self._record_landscape(
            image, None, f"{width}x{height} 竖图 -> 代理端按上部裁成 16:9"
        )
        return image, {"fit": "16x9", "top": self._crop_top}

    # ------------------------------------------------------------------ 条目解析

    # 通知标题里的干扰词（订阅/下载/入库等状态描述）
    _title_noise = (
        "已添加订阅", "添加订阅", "已退订", "开始下载", "下载完成", "下载失败",
        "整理入库", "已入库", "入库完成", "入库", "开始搜索", "搜索结果",
        "更新", "已更新", "订阅", "完成", "成功", "失败", "通知",
    )

    def _resolve_context(self, message, mediainfo=None) -> dict:
        """
        mp 的 post_message 钩子只传 message，不传 mediainfo，
        所以这里先从 mediainfo 取（老版本兼容），取不到再用通知标题反查豆瓣。
        """
        # 1) 有 mediainfo 就直接用（某些调用方会传）
        if mediainfo is not None:
            douban_id = str(getattr(mediainfo, "douban_id", "") or "").strip()
            if douban_id:
                return {
                    "douban_id": douban_id,
                    "is_movie": self._is_movie(mediainfo),
                    "from": "mediainfo",
                }

        # 2) 从标题/正文里解析片名与年份，再查豆瓣条目
        title = ""
        text = ""
        try:
            title = str(getattr(message, "title", "") or "")
            text = str(getattr(message, "text", "") or "")
        except Exception:
            pass
        name, year = self._parse_title(title, text)
        if not name:
            return {}
        key = f"{name}|{year}"
        cached = self._subject_cache.get(key)
        if cached and time.time() - cached[1] < self._subject_ttl:
            return cached[0]
        ctx = self._search_subject(name, year) or {}
        self._subject_cache[key] = (ctx, time.time())
        if len(self._subject_cache) > self._cache_limit:
            self._subject_cache.clear()
        return ctx

    def _parse_title(self, title: str, text: str = "") -> Tuple[str, str]:
        """
        从通知标题解析 (片名, 年份)。
        典型标题：'我不是大师 (2026) S01 已添加订阅' / '【豆瓣】我不是大师 (2026) 开始下载'
        """
        raw = (title or "").strip() or (text or "").strip()
        if not raw:
            return "", ""
        # 去掉 HTML 标签与常见前缀
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = re.sub(r"^[【\[（(][^】\]）)]{0,8}[】\]）)]", " ", raw).strip()
        match = re.match(
            r"^(?P<name>.{1,40}?)\s*[（(\[]\s*(?P<year>(?:19|20)\d{2})\s*[)）\]]",
            raw,
        )
        if match:
            return match.group("name").strip(" -·:："), match.group("year")
        # 没年份：取首行/首段，再剥掉状态词与季集标记
        name = re.split(r"[\n\r|｜]", raw)[0]
        name = re.sub(r"[（(]\s*S\d{1,2}(E\d{1,4})?\s*[)）]", " ", name, flags=re.I)
        name = re.sub(r"\bS\d{1,2}(E\d{1,4})?\b", " ", name, flags=re.I)
        for noise in self._title_noise:
            name = name.replace(noise, " ")
        name = re.sub(r"[（(]\s*(19|20)\d{2}\s*[)）]", " ", name)
        name = re.sub(r"[\s\-—·:：]+", " ", name).strip()
        return name[:40], ""

    def _search_subject(self, name: str, year: str = "") -> Optional[dict]:
        """
        用豆瓣的建议接口按片名查条目，返回 {douban_id, is_movie, from}。
        该接口是网页端公开接口，不需要签名。
        """
        url = "https://movie.douban.com/j/subject_suggest"
        headers = {
            "User-Agent": self._ua,
            "Referer": "https://movie.douban.com/",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        data = None
        for proxies in self._proxy_candidates():
            try:
                resp = requests.get(
                    url, params={"q": name}, headers=headers, timeout=8, proxies=proxies
                )
            except Exception as err:
                logger.debug(f"豆瓣搜索请求异常：{err}")
                continue
            if resp.status_code != 200:
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if isinstance(data, list):
                break
        if not isinstance(data, list) or not data:
            logger.info(f"豆瓣搜索没有结果：{name}（{year}）")
            return None

        best: Optional[Tuple[int, dict]] = None
        for item in data:
            if not isinstance(item, dict):
                continue
            douban_id = str(item.get("id") or "").strip()
            if not douban_id:
                continue
            title = str(item.get("title") or "").strip()
            sub_title = str(item.get("sub_title") or "").strip()
            item_year = str(item.get("year") or "").strip()
            score = 0
            if self._norm_title(title) == self._norm_title(name):
                score += 100
            elif self._norm_title(name) and self._norm_title(name) in self._norm_title(sub_title):
                score += 40
            if year and item_year == str(year):
                score += 60
            elif year and item_year:
                score -= 30
            # 建议接口的 type 不可靠（剧集也会标 movie），这里只作为初值
            is_movie = str(item.get("type") or "").lower() != "tv"
            candidate = {
                "douban_id": douban_id,
                "is_movie": is_movie,
                "from": f"标题反查 {name}{(' ' + year) if year else ''}",
            }
            if best is None or score > best[0]:
                best = (score, candidate)
        if not best or best[0] <= 0:
            logger.info(f"豆瓣搜索结果匹配度不足，放弃：{name}（{year}）")
            return None
        return best[1]

    @staticmethod
    def _norm_title(value: str) -> str:
        return re.sub(r"[\s·:：\-—_（）()\[\]【】,.，。!！?？'\"“”‘’]+", "", value or "")

    @staticmethod
    def _is_movie(mediainfo) -> bool:
        try:
            media_type = getattr(mediainfo, "type", None)
            text = getattr(media_type, "value", media_type)
            text = str(text or "")
            if "TV" in text.upper() or "电视剧" in text or "剧集" in text:
                return False
            if "电影" in text or "MOVIE" in text.upper():
                return True
        except Exception:
            pass
        # 兜底：有季集信息当剧集处理
        try:
            if getattr(mediainfo, "season", None) or getattr(mediainfo, "begin_season", None):
                return False
        except Exception:
            pass
        return True

    def _find_landscape(self, douban_id: str, is_movie: bool) -> Optional[str]:
        """
        查豆瓣剧照，返回一张横版大图地址（带缓存）。
        剧照接口分 movie / tv，而搜索接口给的 type 不一定准，
        所以先用推断的类型查，查不到再换另一种。
        """
        key = str(douban_id)
        cached = self._landscape_cache.get(key)
        if cached and time.time() - cached[1] < self._landscape_ttl:
            return cached[0]

        photos = None
        for kind_movie in (is_movie, not is_movie):
            photos = self._fetch_photos(douban_id, kind_movie)
            if photos:
                break
        picked = None
        if photos:
            for candidate in self._landscape_candidates(photos)[:3]:
                if self._is_usable_image(candidate):
                    picked = candidate
                    break
        self._landscape_cache[key] = (picked, time.time())
        if not picked:
            logger.info(f"豆瓣剧照未找到可用横图：{key}（共 {len(photos or [])} 张）")
        return picked

    def _fetch_photos(self, douban_id: str, is_movie: bool) -> Optional[list]:
        """
        调用豆瓣 App 接口取剧照列表。直连不通时尝试 MP 配置的代理。
        """
        kind = "movie" if is_movie else "tv"
        path = f"/api/v2/{kind}/{douban_id}/photos"
        url = f"https://frodo.douban.com{path}"
        ts = time.strftime("%Y%m%d")
        params = {
            "apiKey": self._api_key,
            "start": 0,
            "count": 30,
            "os_rom": "android",
            "_ts": ts,
            "_sig": self._sign(path, ts),
        }
        headers = {"User-Agent": self._api_ua, "Referer": "https://m.douban.com/"}

        for proxies in self._proxy_candidates():
            try:
                resp = requests.get(
                    url, params=params, headers=headers, timeout=6, proxies=proxies
                )
            except Exception as err:
                logger.debug(f"豆瓣剧照接口请求异常（proxies={bool(proxies)}）：{err}")
                continue
            if resp.status_code != 200:
                logger.debug(
                    f"豆瓣剧照接口返回 {resp.status_code}（proxies={bool(proxies)}）"
                )
                continue
            try:
                data = resp.json() or {}
            except Exception:
                continue
            photos = data.get("photos")
            if isinstance(photos, list):
                return photos
        return None

    @staticmethod
    def _proxy_candidates() -> List[Optional[dict]]:
        """
        先直连，再试 MP 里配置的 HTTP 代理。
        """
        candidates: List[Optional[dict]] = [None]
        proxy = str(getattr(settings, "PROXY_HOST", "") or "").strip()
        if proxy:
            candidates.append({"http": proxy, "https": proxy})
        return candidates

    @classmethod
    def _sign(cls, path: str, ts: str) -> str:
        raw = "&".join(["GET", quote(path, safe=""), ts])
        digest = hmac.new(
            cls._api_secret.encode(), raw.encode(), hashlib.sha1
        ).digest()
        return base64.b64encode(digest).decode()

    def _landscape_candidates(self, photos: list) -> List[str]:
        """
        从剧照里挑出所有横图候选，按"越接近 16:9、宽度越大"排序。
        直链大图优先，带签名的地址作为备选。
        """
        scored: List[Tuple[float, str]] = []
        for photo in photos:
            image = ((photo or {}).get("image") or {}).get("large") or {}
            url = image.get("url") or ""
            width = image.get("width") or 0
            height = image.get("height") or 0
            if not url or not width or not height:
                continue
            ratio = float(width) / float(height)
            if ratio < self._landscape_ratio:
                continue
            # 越接近 16:9、宽度越大越优先
            score = abs(ratio - 16 / 9.0) - min(width, 1920) / 100000.0
            big = self._photo_big_url(url)
            if big:
                scored.append((score, big))
            signed = self._signed_jpeg_url(url)
            if signed and signed != big:
                scored.append((score + 0.5, signed))
        scored.sort(key=lambda item: item[0])
        return list(dict.fromkeys(url for _, url in scored))

    @staticmethod
    def _signed_jpeg_url(url: str) -> Optional[str]:
        """
        豆瓣部分剧照只给带签名的临时地址，默认 format/webp；
        企业微信不认 webp，改参数要 jpg（签名只校验 path，改 query 有效）。
        """
        if "qnmob3-sign.doubanio.com" not in url:
            return None
        return url.replace("format/webp", "format/jpg")

    def _is_usable_image(self, url: str) -> bool:
        """
        快速校验候选图能不能用：200、是图片、不是豆瓣 12 字节的占位图。
        """
        try:
            resp = requests.get(
                url,
                timeout=5,
                stream=True,
                headers={
                    "Referer": self._referer,
                    "User-Agent": self._ua,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
        except Exception as err:
            logger.debug(f"候选横图请求异常：{err} {url}")
            return False
        try:
            if resp.status_code != 200:
                logger.debug(f"候选横图 HTTP {resp.status_code}：{url}")
                return False
            total = int(resp.headers.get("Content-Length") or 0)
            if 0 < total < 1024:
                logger.debug(f"候选横图体积过小（{total}B）：{url}")
                return False
            head = b""
            for chunk in resp.iter_content(64):
                head += chunk
                if len(head) >= 32:
                    break
            return (
                head[:2] == b"\xff\xd8"
                or head[:4] == b"RIFF"
                or head[:8] == b"\x89PNG\r\n\x1a\n"
                or head[:6] in (b"GIF87a", b"GIF89a")
            )
        except Exception:
            return False
        finally:
            try:
                resp.close()
            except Exception:
                pass

    @staticmethod
    def _photo_big_url(url: str) -> Optional[str]:
        """
        豆瓣剧照的 img*.doubanio.com 直链比带签名的临时地址更稳，优先用它。
        """
        try:
            match = re.search(r"/public/(p\d+)", url)
            if match:
                return (
                    "https://img3.doubanio.com/view/photo/l/public/"
                    f"{match.group(1)}.jpg"
                )
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ 图片尺寸探测

    def _probe_size(self, url: str) -> Optional[Tuple[int, int]]:
        """
        读取图片头部，解析真实宽高（不依赖 Pillow）。
        支持 JPEG / PNG / WebP。
        """
        cached = self._size_cache.get(url)
        if cached:
            return cached
        candidate = self.prefer_jpeg_url(url)
        try:
            resp = requests.get(
                candidate,
                timeout=8,
                stream=True,
                headers={
                    "Referer": self._referer,
                    "User-Agent": self._ua,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            if resp.status_code != 200:
                logger.debug(f"探测图片尺寸失败：HTTP {resp.status_code} {candidate}")
                return None
            buffer = b""
            try:
                for chunk in resp.iter_content(8192):
                    buffer += chunk
                    if len(buffer) >= 65536:
                        break
            finally:
                resp.close()
        except Exception as err:
            logger.debug(f"探测图片尺寸异常：{err} {candidate}")
            return None

        size = self._parse_image_size(buffer)
        if size:
            if len(self._size_cache) > self._cache_limit:
                self._size_cache.clear()
            self._size_cache[url] = size
        return size

    @staticmethod
    def _parse_image_size(data: bytes) -> Optional[Tuple[int, int]]:
        if len(data) < 32:
            return None
        # JPEG
        if data[:2] == b"\xff\xd8":
            index = 2
            length = len(data)
            sof_markers = {
                0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6,
                0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
            }
            while index + 9 < length:
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                if marker == 0xFF:
                    index += 1
                    continue
                if marker in (0xD9, 0xDA):
                    break
                segment = int.from_bytes(data[index + 2:index + 4], "big")
                if segment < 2:
                    break
                if marker in sof_markers:
                    height = int.from_bytes(data[index + 5:index + 7], "big")
                    width = int.from_bytes(data[index + 7:index + 9], "big")
                    if width and height:
                        return width, height
                    return None
                index += 2 + segment
            return None
        # PNG
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return (width, height) if width and height else None
        # WebP
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                width = int.from_bytes(data[24:27], "little") + 1
                height = int.from_bytes(data[27:30], "little") + 1
                return width, height
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
                return width, height
            if chunk == b"VP8 ":
                index = data.find(b"\x9d\x01\x2a", 20)
                if index > 0:
                    width = int.from_bytes(data[index + 3:index + 5], "little") & 0x3FFF
                    height = int.from_bytes(data[index + 5:index + 7], "little") & 0x3FFF
                    return width, height
        return None

    # ------------------------------------------------------------------ 基础判定

    @staticmethod
    def _is_http_url(url: Any) -> bool:
        return isinstance(url, str) and url.lower().startswith(("http://", "https://"))

    @staticmethod
    def is_douban_image(url: str) -> bool:
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").lower().rstrip(".")
        return hostname == "doubanio.com" or hostname.endswith(".doubanio.com")

    def plugin_id(self) -> str:
        """
        运行时插件ID。MP 用「插件类名」作为 plugin_id 并拼进路由前缀
        （见 PluginManager.load_plugins: plugin_id = plugin.__name__），
        与插件目录名的大小写无关，所以这里必须取类名。
        """
        try:
            name = type(self).__name__
            if name:
                return name
        except Exception:
            pass
        return "doubanimage"

    def build_proxy_url(
        self, url: str, extra: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        if not self.is_douban_image(url):
            return None
        domain = str(getattr(settings, "APP_DOMAIN", "") or "").strip().rstrip("/")
        if not domain:
            logger.warn("未配置 APP_DOMAIN（对外访问地址），无法生成图片代理地址")
            self._record_rewrite(url, None, "未配置 APP_DOMAIN")
            return None
        parts = [f"url={quote(url, safe='')}", f"{self._cache_buster}=1"]
        for key, value in (extra or {}).items():
            if value is None or value == "":
                continue
            parts.append(f"{key}={quote(str(value), safe='')}")
        return f"{domain}/api/v1/plugin/{self.plugin_id()}/img?" + "&".join(parts)

    # ------------------------------------------------------------------ 图片代理

    @staticmethod
    def prefer_jpeg_url(url: str) -> str:
        """
        豆瓣 CDN 同一张图同时支持 .webp / .jpg 后缀，带 Referer 时 .jpg 返回真 JPEG。
        企业微信图文封面只认 JPG/PNG，所以优先按 .jpg 取，省掉转码依赖。
        """
        try:
            parsed = urlparse(url)
            if parsed.path.lower().endswith(".webp"):
                return parsed._replace(path=parsed.path[:-5] + ".jpg").geturl()
        except Exception:
            pass
        return url

    @classmethod
    def _candidates(cls, url: str) -> List[str]:
        """
        豆瓣图有多个镜像子域（img1/img2/img3/img9）。
        某个子域被风控返回 HTML 时，换一个子域往往就能取到，顺带降低 502 概率。
        """
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        urls = [url]
        if re.match(r"^img\d+\.doubanio\.com$", host):
            for name in ("img1", "img2", "img3", "img9"):
                mirror = f"{name}.doubanio.com"
                if mirror != host:
                    urls.append(
                        parsed._replace(
                            netloc=parsed.netloc.replace(host, mirror)
                        ).geturl()
                    )
        ordered: List[str] = []
        for item in urls[:3]:
            ordered.append(cls.prefer_jpeg_url(item))
            ordered.append(item)
        return ordered

    def _fetch(self, url: str, cache_key: Optional[str] = None) -> Tuple[Optional[bytes], str, str]:
        """
        取图：优先 .jpg 版本，失败再退回原始地址。返回 (内容, MIME, 说明)
        """
        key = cache_key or url
        cached = self._cache.get(key)
        if cached:
            return cached[0], cached[1], "cache"

        detail = ""
        for candidate in dict.fromkeys(self._candidates(url)):
            try:
                resp = requests.get(
                    candidate,
                    timeout=15,
                    headers={
                        "Referer": self._referer,
                        "User-Agent": self._ua,
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    },
                )
            except Exception as err:
                detail = f"请求异常：{err}"
                logger.error(f"请求豆瓣图片出错：{err} {candidate}")
                continue
            if resp.status_code != 200 or not resp.content:
                detail = f"上游 HTTP {resp.status_code}"
                logger.warn(f"豆瓣图片返回异常：{resp.status_code} {candidate}")
                continue

            media_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not media_type.startswith("image/"):
                detail = f"上游返回非图片：{media_type or 'unknown'}"
                logger.warn(f"豆瓣图片返回非图片内容：{media_type} {candidate}")
                continue

            content = resp.content
            if len(content) <= 1024 and media_type == "image/png":
                # 豆瓣对部分受限图片会返回 12 字节的占位 PNG
                detail = "上游返回占位图（原图不可用）"
                logger.warn(f"{detail}：{candidate}")
                continue

            if len(content) > 5 * 1024 * 1024:
                detail = "图片超过 5MB，企业微信可能不展示"
                logger.warn(detail)

            if len(self._cache) >= self._cache_limit:
                self._cache.clear()
            self._cache[key] = (content, media_type)
            return content, media_type, detail or "ok"

        return None, "image/jpeg", detail or "取图失败"

    def _crop_landscape(
        self, content: bytes, top_pct: float, target_ratio: float = 16 / 9
    ) -> Optional[bytes]:
        """
        把竖图按「靠上」的位置裁成 target_ratio 的横图（16:9）。
        企业微信是从正中裁的，正中对竖版海报来说往往只剩躯干；
        网页端卡片也是裁上部，所以这里对齐网页的观感。
        """
        try:
            from PIL import Image
        except Exception:
            logger.warn("当前环境缺少 Pillow，无法裁剪封面")
            return None
        try:
            with Image.open(BytesIO(content)) as source:
                image = source.convert("RGB")
                width, height = image.size
                if width <= 0 or height <= 0:
                    return None
                if width / float(height) >= 1.3:
                    return None
                crop_height = min(int(width / target_ratio), height)
                top = int(height * max(0.0, min(float(top_pct), 0.45)))
                if top + crop_height > height:
                    top = max(0, height - crop_height)
                cropped = image.crop((0, top, width, top + crop_height))
                if cropped.width > 1280:
                    cropped = cropped.resize(
                        (1280, int(round(1280 / target_ratio))), Image.LANCZOS
                    )
                buffer = BytesIO()
                cropped.save(buffer, format="JPEG", quality=88, optimize=True)
                return buffer.getvalue()
        except Exception as err:
            logger.warn(f"裁剪封面失败：{err}")
            return None

    def img(
        self,
        request: Request,
        url: str = Query(..., description="豆瓣图片地址"),
        fit: Optional[str] = Query(None, description="横版裁剪：16x9"),
        top: Optional[float] = Query(None, description="裁剪起点（0~0.45），默认取插件设置"),
        auto: int = Query(1, description="未指定 fit 时，竖图自动裁成 16:9（1=开）"),
    ):
        """
        匿名图片代理：补 Referer 取图后返回。
        HEAD 只回响应头（企业微信抓封面时用 HEAD 探测），GET 回完整图片。
        fit=16x9 时按上部裁成横版后再返回；未指定 fit 且 auto=1 时，
        竖图也会自动裁成 16:9（兜底，防止钩子没来得及加参数又变回正中裁）。
        """
        if not self._enabled:
            return Response(status_code=404)
        if not url or not self.is_douban_image(url):
            return Response(status_code=400)

        crop_top = float(top) if top is not None else float(self._crop_top)
        want_crop = (fit or "").lower() == "16x9" or (bool(auto) and not fit)

        cache_key = url
        if want_crop:
            cache_key = f"{url}|crop|{round(crop_top, 3)}"

        content, media_type, detail = self._fetch(url, cache_key)
        if not content:
            self._record_hit(request, 502, 0, detail)
            return Response(status_code=502)

        if want_crop:
            cropped = self._crop_landscape(content, crop_top)
            if cropped:
                content = cropped
                media_type = "image/jpeg"
                detail = f"{detail}+crop16x9"

        headers = {
            "Cache-Control": "public, max-age=86400",
            "Content-Type": media_type,
            "Content-Length": str(len(content)),
            "Access-Control-Allow-Origin": "*",
        }
        self._record_hit(request, 200, len(content), detail)
        if request.method.upper() == "HEAD":
            return Response(status_code=200, headers=headers)
        return Response(content=content, media_type=media_type, headers=headers)

    def stat(self):
        """
        诊断接口：最近的通知地址改写、横版处理与图片抓取记录。
        """
        domain = str(getattr(settings, "APP_DOMAIN", "") or "").strip()
        return {
            "enabled": self._enabled,
            "version": self.plugin_version,
            "landscape": self._landscape,
            "official_first": self._official_first,
            "crop_top": self._crop_top,
            "app_domain": domain,
            "plugin_id": self.plugin_id(),
            "cache_size": len(self._cache),
            "size_cache": len(self._size_cache),
            "landscape_cache": len(self._landscape_cache),
            "subject_cache": len(self._subject_cache),
            "rewrites": list(self._rewrites),
            "landscape_logs": list(self._landscape_logs),
            "hits": list(self._hits),
        }

    # ------------------------------------------------------------------ 诊断记录

    def _record_rewrite(self, image: Optional[str], proxy_url: Optional[str], detail: str) -> None:
        try:
            self._rewrites.appendleft(
                {
                    "time": time.strftime("%m-%d %H:%M:%S"),
                    "image": image,
                    "proxy": proxy_url,
                    "detail": detail,
                }
            )
        except Exception:
            pass

    def _record_landscape(self, image: str, replaced: Optional[str], detail: str) -> None:
        try:
            self._landscape_logs.appendleft(
                {
                    "time": time.strftime("%m-%d %H:%M:%S"),
                    "image": image,
                    "replaced": replaced,
                    "detail": detail,
                }
            )
            if replaced:
                logger.info(f"通知封面已换成横版剧照：{replaced}")
        except Exception:
            pass

    def _record_hit(self, request: Request, status: int, size: int, detail: str) -> None:
        try:
            user_agent = ""
            try:
                user_agent = (request.headers.get("user-agent") or "")[:80]
            except Exception:
                pass
            self._hits.appendleft(
                {
                    "time": time.strftime("%m-%d %H:%M:%S"),
                    "method": request.method.upper(),
                    "status": status,
                    "size": size,
                    "ua": user_agent,
                    "detail": detail,
                }
            )
            logger.info(
                f"豆瓣图片代理命中：{request.method.upper()} {status} {size}B ({detail}) UA={user_agent}"
            )
        except Exception:
            pass
