from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests
from fastapi import Query
from fastapi.responses import Response

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase


class DoubanImage(_PluginBase):
    """
    豆瓣通知图片代理

    问题背景：
      豆瓣图片 CDN 对不带 Referer 的请求一律返回 418，且返回 webp 格式；
      而企业微信的图文消息封面是由企业微信服务器主动抓取的（不带 Referer），
      所以 MoviePilot 直接把豆瓣海报地址作为 picurl 时，封面一定裂图。

    本插件做两件事：
      1. 发送通知前（post_message 钩子）把豆瓣图片地址改写为本机代理地址；
      2. 代理接口匿名对外开放，服务端补上 Referer 去取图，并把 webp 转成 jpeg。
    """

    plugin_name = "豆瓣通知图片代理"
    plugin_desc = "把通知里的豆瓣海报换成本机代理地址，解决微信/企业微信通知图片不显示"
    plugin_icon = ""
    plugin_version = "1.0"
    plugin_author = "WorkBuddy"
    author_url = ""
    plugin_config_prefix = "doubanimage_"
    plugin_order = 1
    auth_level = 1

    _enabled: bool = False
    # 简单内存缓存：url -> (content, media_type)
    _cache: Dict[str, Tuple[bytes, str]] = {}
    _cache_limit: int = 200

    _referer = "https://movie.douban.com/"
    _ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def init_plugin(self, config: dict = None):
        self._cache = {}
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
                "methods": ["GET"],
                "summary": "豆瓣图片代理",
                "description": "为通知封面提供免登录的豆瓣图片代理，自动补 Referer",
                "allow_anonymous": True,
            }
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
                                                "你的域名 + /api/v1/plugin/doubanimage/img?url=...。"
                                                "该代理需要能从公网访问（企业微信服务器要抓图）。"
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

    def post_message(self, message=None, **kwargs) -> None:
        """
        发送通知前改写封面地址。返回 None，不拦截原发送流程。
        """
        if not self._enabled or not message:
            return
        image = getattr(message, "image", None)
        if not image or not isinstance(image, str):
            return
        proxy_url = self.build_proxy_url(image)
        if proxy_url:
            try:
                message.image = proxy_url
                logger.info(f"豆瓣通知图片已改写为代理地址：{proxy_url}")
            except Exception as err:
                logger.warn(f"改写通知图片失败：{err}")

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
            return None
        return (
            f"{domain}/api/v1/plugin/{self.plugin_id()}/img"
            f"?url={quote(url, safe='')}"
        )

    @staticmethod
    def prefer_jpeg_url(url: str) -> str:
        """
        豆瓣 CDN 同一张图同时支持 .webp / .jpg / .png 后缀，且不论后缀都返回 jpeg。
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

    def img(self, url: str = Query(..., description="豆瓣图片地址")):
        """
        匿名图片代理：补 Referer 取图后原样返回。
        """
        if not self._enabled:
            return Response(status_code=404)
        if not url or not self.is_douban_image(url):
            return Response(status_code=400)

        cached = self._cache.get(url)
        if cached:
            content, media_type = cached
            return Response(
                content=content,
                media_type=media_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )

        res = None
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
                logger.error(f"请求豆瓣图片出错：{err} {candidate}")
                continue
            if resp.status_code == 200 and resp.content:
                res = resp
                break
            logger.warn(f"豆瓣图片返回异常：{resp.status_code} {candidate}")

        if res is None:
            return Response(status_code=502)

        media_type = res.headers.get("Content-Type") or "image/jpeg"
        content, media_type = self.to_jpeg_if_needed(res.content, media_type)

        if len(self._cache) >= self._cache_limit:
            self._cache.clear()
        self._cache[url] = (content, media_type)

        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )
