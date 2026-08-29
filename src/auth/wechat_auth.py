"""Weixin (WeChat) native + mini-program authentication.

Unlike Google / Apple, WeChat doesn't hand the server a JWT — it hands
back an opaque *code* that the client has already exchanged for a
session on WeChat's side. The server's job is to POST that code to
WeChat's token endpoint and receive an ``openid`` (per-AppID user id)
and, if two AppIDs live under one Open Platform account, a shared
``unionid``.

Two flows, two endpoints:

- **Mini-program** (``platform="mp"``): The wxapp calls ``wx.login()``,
  gets a ``code``, POSTs to ``/api/auth/wechat/login``. This module
  exchanges the code at
  ``/sns/jscode2session`` — returns ``openid`` (+ ``unionid`` if
  unionid'd) and a ``session_key`` we don't use.
- **iOS App** (``platform="app"``): The Open Platform SDK returns a
  ``code`` via ``SendAuthReq`` callback, same shape POST. This module
  exchanges at ``/sns/oauth2/access_token`` — returns ``openid``,
  ``unionid``, ``access_token``, ``refresh_token``. We don't persist
  the access_token (we don't call any WeChat user APIs); the openid is
  all we need.

The two AppIDs (mp vs. app) namespace ``openid`` independently, so a
single WeChat user gets two different openids. Cross-app linking
depends on both AppIDs being registered under the *same* Weixin Open
Platform account, which surfaces a shared ``unionid``. Cf. the AppID /
unionid caveats in ``.claude/plans/ios-app-lucky-sonnet.md`` and
:meth:`UserService.find_or_create_by_wechat`.

Structurally *not* an :class:`OIDCProvider` — WeChat doesn't do OIDC,
doesn't sign JWTs, doesn't expose a JWKS. Kept as a plain class so
:mod:`src.auth.factory` can still cache one instance per process, same
pattern as the OIDC providers.
"""

from __future__ import annotations

import logging
from typing import Literal

import requests

logger = logging.getLogger(__name__)

MP_ENDPOINT = "https://api.weixin.qq.com/sns/jscode2session"
APP_ENDPOINT = "https://api.weixin.qq.com/sns/oauth2/access_token"

Platform = Literal["mp", "app"]


class WechatAuth:
    """Weixin code → (openid, unionid) exchanger.

    Attributes:
        mp_appid: Mini-program AppID (Public Platform). Empty when the
            deployment doesn't ship a wxapp.
        mp_appsecret: Mini-program secret. Empty when the mp AppID is.
        app_appid: iOS-App AppID (Open Platform). Empty when the
            deployment doesn't ship an iOS app.
        app_appsecret: iOS-App secret.
    """

    def __init__(
        self,
        mp_appid: str = "",
        mp_appsecret: str = "",
        app_appid: str = "",
        app_appsecret: str = "",
    ) -> None:
        self.mp_appid = mp_appid
        self.mp_appsecret = mp_appsecret
        self.app_appid = app_appid
        self.app_appsecret = app_appsecret

    def is_configured(self, platform: Platform) -> bool:
        """True when the AppID + secret pair for ``platform`` are both set."""
        if platform == "mp":
            return bool(self.mp_appid and self.mp_appsecret)
        return bool(self.app_appid and self.app_appsecret)

    def code_to_session(self, code: str, platform: Platform) -> dict | None:
        """Exchange a WeChat code for openid / unionid.

        Args:
            code: The one-time code the WeChat client SDK returned.
            platform: ``"mp"`` for mini-program (``jscode2session``),
                ``"app"`` for iOS App (``oauth2/access_token``).

        Returns:
            Dict with at least ``openid``; ``unionid`` when the two
            AppIDs share an Open Platform account. ``None`` on any
            failure (WeChat returned a non-zero ``errcode``, network
            error, malformed JSON, etc.).
        """
        if not code:
            return None
        if not self.is_configured(platform):
            logger.warning("Wechat platform=%s is not configured", platform)
            return None

        if platform == "mp":
            params = {
                "appid": self.mp_appid,
                "secret": self.mp_appsecret,
                "js_code": code,
                "grant_type": "authorization_code",
            }
            url = MP_ENDPOINT
        else:
            params = {
                "appid": self.app_appid,
                "secret": self.app_appsecret,
                "code": code,
                "grant_type": "authorization_code",
            }
            url = APP_ENDPOINT

        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data: dict = resp.json()
        except requests.RequestException as e:
            logger.error("Wechat code exchange network error (%s): %s", platform, e)
            return None
        except ValueError as e:
            logger.error("Wechat code exchange returned invalid JSON (%s): %s", platform, e)
            return None

        # WeChat signals failure via ``errcode`` != 0 with an HTTP 200.
        if data.get("errcode"):
            logger.warning(
                "Wechat code exchange failed (%s): errcode=%s errmsg=%s",
                platform,
                data.get("errcode"),
                data.get("errmsg"),
            )
            return None

        openid = data.get("openid")
        if not openid:
            logger.warning("Wechat code exchange returned no openid: %s", data)
            return None

        return {
            "openid": openid,
            "unionid": data.get("unionid"),
            # session_key (mp) / access_token+refresh_token (app) — we
            # currently don't use these, but returning them keeps the
            # module honest and lets a future caller pull user info
            # from WeChat without a second endpoint.
            "session_key": data.get("session_key"),
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_in": data.get("expires_in"),
        }
