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

    v1.1 修复（封面空白/灰块的真凶）：
      企业微信/微信抓取图文封面时会先发 HEAD 请求探测资源（Content-Length / Content-Type），
      v1.0 的接口只注册了 GET，HEAD 返回 405 Method Not Allowed，
      抓图流程直接判定为"取不到图"，于是卡片有、封面空白。
      本版把 HEAD 一并注册，并对 HEAD 只返回响应头（不传 body）。
    """

    plugin_name = "豆瓣通知图片代理"
    plugin_desc = "把通知里的豆瓣海报换成本机代理地址，解决微信/企业微信通知图片不显示"
    plugin_icon = ""
    plugin_version = "1.1"
    plugin_author = "celestial520"
    author_url = "https://github.com/celestial520"
    plugin_config_prefix = "doubanimage_"
    plugin_order = 1
    auth_level = 1

    _enabled: bool = False
    # 简单内存缓存：url -> (content, media_type)
    _cache: Dict[str, Tuple[bytes, str]] = {}
    _cache_limit: int = 300
    # 诊断信息（最近若干次改写 / 抓取）
    _rewrites: Deque[dict] = deque(maxlen=20)
    _hits: Deque[dict] = deque(maxlen=40)

    _referer = "https://movie.douban.com/"
    _ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # 版本参数：企业微信会按 picurl 缓存抓图结果，带上版本号可绕过旧的失败缓存
    _cache_buster = "v11"

    def init_plugin(self, config: dict = None):
        self._cache = {}
        self._rewrites = deque(maxlen=20)
        self._hits = deque(maxlen=40)
        if config:
            self._enabled = bool(config.get("enabled"))

    def stop_service(self):
        self._cache = {}

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
                "description": "查看最近的地址改写与图片抓取记录，用于排查封面不显示的问题",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
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
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"enabled": False}

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

        proxy_url = self.build_proxy_url(image)
        if not proxy_url:
            self._record_rewrite(image, None, "非豆瓣图片，保持原样")
            return
        try:
            message.image = proxy_url
            self._record_rewrite(image, proxy_url, "已改写为代理地址")
            logger.info(f"豆瓣通知图片已改写为代理地址：{proxy_url}")
        except Exception as err:
            self._record_rewrite(image, None, f"改写失败：{err}")
            logger.warn(f"改写通知图片失败：{err}")

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

    @staticmethod
    def to_jpeg_if_needed(content: bytes, media_type: str) -> Tuple[bytes, str]:
        """
        兜底：万一拿到的还是 webp，用 Pillow 转成 jpeg（Pillow 不可用则原样返回）。
        """
        if "webp" not in (media_type or "").lower():
            return content, media_type
        try:
            from PIL import Image

            with Image.open(BytesIO(content)) as image:
                buffer = BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=88)
                return buffer.getvalue(), "image/jpeg"
        except Exception as err:
            logger.warn(f"webp 转 jpeg 失败，按原格式返回：{err}")
            return content, media_type

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

            content, media_type = self.to_jpeg_if_needed(resp.content, media_type)
            if len(content) > 5 * 1024 * 1024:
                detail = "图片超过 5MB，企业微信可能不展示"
                logger.warn(detail)

            if len(self._cache) >= self._cache_limit:
                self._cache.clear()
            self._cache[url] = (content, media_type)
            return content, media_type, "ok"

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
        诊断接口：最近的通知地址改写与图片抓取记录。
        """
        domain = str(getattr(settings, "APP_DOMAIN", "") or "").strip()
        return {
            "enabled": self._enabled,
            "app_domain": domain,
            "plugin_id": self.plugin_id(),
            "cache_size": len(self._cache),
            "rewrites": list(self._rewrites),
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
