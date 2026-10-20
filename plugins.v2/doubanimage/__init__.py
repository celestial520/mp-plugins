import base64
import hashlib
import hmac
import re
import time
from collections import deque
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
    """

    plugin_name = "豆瓣通知图片代理"
    plugin_desc = "把通知里的豆瓣海报换成本机代理地址，并自动改用横版剧照，解决微信/企业微信通知图片不显示或被裁切"
    plugin_icon = ""
    plugin_version = "1.2"
    plugin_author = "celestial520"
    author_url = "https://github.com/celestial520"
    plugin_config_prefix = "doubanimage_"
    plugin_order = 1
    auth_level = 1

    _enabled: bool = False
    # 是否把竖版海报换成横版剧照
    _landscape: bool = True
    # 简单内存缓存：url -> (content, media_type)
    _cache: Dict[str, Tuple[bytes, str]] = {}
    _cache_limit: int = 300
    # 图片尺寸缓存：url -> (width, height)
    _size_cache: Dict[str, Tuple[int, int]] = {}
    # 横版图缓存：douban_id -> (url, 时间戳)；url 为 None 表示查过但没有
    _landscape_cache: Dict[str, Tuple[Optional[str], float]] = {}
    _landscape_ttl: int = 6 * 3600
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
    _cache_buster = "v12"

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
                                            "label": "竖版海报自动换横版剧照",
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
                                                "「竖版海报自动换横版剧照」开启时，"
                                                "若封面是 2:3 竖图，会改取该条目豆瓣剧照里的横版图。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"enabled": False, "landscape": True}

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

        # 关键：竖版海报 -> 横版剧照
        final_image = image
        try:
            final_image = self.ensure_landscape(image, mediainfo)
        except Exception as err:
            logger.warn(f"横版剧照替换失败，继续使用原图：{err}")
            final_image = image

        proxy_url = self.build_proxy_url(final_image)
        if not proxy_url:
            self._record_rewrite(final_image, None, "非豆瓣图片，保持原样")
            return
        try:
            message.image = proxy_url
            suffix = "" if final_image == image else "（已换横版剧照）"
            self._record_rewrite(image, proxy_url, f"已改写为代理地址{suffix}")
            logger.info(f"豆瓣通知图片已改写为代理地址：{proxy_url}")
        except Exception as err:
            self._record_rewrite(image, None, f"改写失败：{err}")
            logger.warn(f"改写通知图片失败：{err}")

    # ------------------------------------------------------------------ 横版替换

    def ensure_landscape(self, image: str, mediainfo) -> str:
        """
        封面若是竖图，改用该条目真正的横版剧照。
        """
        if not self._landscape or mediainfo is None:
            return image
        if not self.is_douban_image(image):
            return image

        size = self._probe_size(image)
        if not size:
            self._record_landscape(image, None, "无法探测尺寸，保持原图")
            return image
        width, height = size
        ratio = width / float(height or 1)
        if ratio >= self._landscape_ratio:
            self._record_landscape(image, None, f"{width}x{height} 已是横图，无需替换")
            return image

        douban_id = str(getattr(mediainfo, "douban_id", "") or "").strip()
        if not douban_id:
            self._record_landscape(image, None, f"{width}x{height} 竖图，但没有 douban_id")
            return image

        is_movie = self._is_movie(mediainfo)
        landscape = self._find_landscape(douban_id, is_movie)
        if not landscape:
            self._record_landscape(
                image, None, f"{width}x{height} 竖图，豆瓣剧照里没找到横图"
            )
            return image
        self._record_landscape(image, landscape, f"{width}x{height} 竖图 -> 横版剧照")
        return landscape

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
        """
        key = f"{'movie' if is_movie else 'tv'}:{douban_id}"
        cached = self._landscape_cache.get(key)
        if cached and time.time() - cached[1] < self._landscape_ttl:
            return cached[0]

        photos = self._fetch_photos(douban_id, is_movie)
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

    def build_proxy_url(self, url: str) -> Optional[str]:
        if not self.is_douban_image(url):
            return None
        domain = str(getattr(settings, "APP_DOMAIN", "") or "").strip().rstrip("/")
        if not domain:
            logger.warn("未配置 APP_DOMAIN（对外访问地址），无法生成图片代理地址")
            self._record_rewrite(url, None, "未配置 APP_DOMAIN")
            return None
        return (
            f"{domain}/api/v1/plugin/{self.plugin_id()}/img"
            f"?url={quote(url, safe='')}&{self._cache_buster}=1"
        )

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

    def _fetch(self, url: str) -> Tuple[Optional[bytes], str, str]:
        """
        取图：优先 .jpg 版本，失败再退回原始地址。返回 (内容, MIME, 说明)
        """
        cached = self._cache.get(url)
        if cached:
            return cached[0], cached[1], "cache"

        detail = ""
        for candidate in dict.fromkeys([self.prefer_jpeg_url(url), url]):
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
            self._cache[url] = (content, media_type)
            return content, media_type, detail or "ok"

        return None, "image/jpeg", detail or "取图失败"

    def img(self, request: Request, url: str = Query(..., description="豆瓣图片地址")):
        """
        匿名图片代理：补 Referer 取图后返回。
        HEAD 只回响应头（企业微信抓封面时用 HEAD 探测），GET 回完整图片。
        """
        if not self._enabled:
            return Response(status_code=404)
        if not url or not self.is_douban_image(url):
            return Response(status_code=400)

        content, media_type, detail = self._fetch(url)
        if not content:
            self._record_hit(request, 502, 0, detail)
            return Response(status_code=502)

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
        诊断接口：最近的通知地址改写、横版替换与图片抓取记录。
        """
        domain = str(getattr(settings, "APP_DOMAIN", "") or "").strip()
        return {
            "enabled": self._enabled,
            "landscape": self._landscape,
            "app_domain": domain,
            "plugin_id": self.plugin_id(),
            "cache_size": len(self._cache),
            "size_cache": len(self._size_cache),
            "landscape_cache": len(self._landscape_cache),
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
