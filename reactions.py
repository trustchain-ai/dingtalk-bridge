"""DingTalk message emoji reactions (表情回复) for task-progress indicators.

The lightweight ``dingtalk-stream`` SDK can receive messages and reply, but it
cannot react to a message. DingTalk's reaction API lives in the full OpenAPI
SDK (``alibabacloud-dingtalk`` -> ``robot_1_0``). This module wraps the two
calls we need and uses them to mark progress on the *user's @ message*:

    🤔Thinking  -> added when the task starts
    🥳Done      -> swap-in when the task finishes ok
    😖Failed    -> swap-in when the task errors

Design notes:
- Reactions are purely cosmetic, so every failure path degrades to a no-op:
  if the SDK is missing, the token can't be fetched, or a call errors, the
  bridge keeps working with just its text replies.
- We use the SYNCHRONOUS ``*_with_options`` methods (not the ``_async`` ones
  hermes uses) because our brain runs inside a worker thread, not an event loop.
- The access token is the v1.0 "x-acs-dingtalk-access-token" minted from the
  app's own AppKey/AppSecret, cached until shortly before expiry.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("dingtalk-bridge.reactions")

try:
    from alibabacloud_dingtalk.robot_1_0 import client as _robot_client
    from alibabacloud_dingtalk.robot_1_0 import models as _robot_models
    from alibabacloud_tea_openapi import models as _open_api_models
    from alibabacloud_tea_util import models as _tea_util_models

    _SDK_OK = True
except Exception as exc:  # broad: the SDK transitively imports cryptography etc.
    _SDK_OK = False
    logger.warning("表情回复已禁用:alibabacloud-dingtalk 不可用 (%s)", exc)

# DingTalk "text emotion" constants (same as hermes uses). emotion_id is a
# fixed generic text-emotion id; the visible label comes from emotion_name.
_EMOTION_ID = "2659900"
_BACKGROUND_ID = "im_bg_1"
_EMOTION_TYPE = 2

_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_TOKEN_SKEW_SECONDS = 120


class Reactor:
    """Add / recall emoji reactions on a message for one robot (one AppKey)."""

    def __init__(self, app_key: str, app_secret: str, robot_code: str):
        self._app_key = app_key
        self._app_secret = app_secret
        self._robot_code = robot_code or app_key
        self._lock = threading.Lock()
        self._token = ""
        self._token_exp = 0.0
        self._client = None
        if _SDK_OK and app_key and app_secret:
            cfg = _open_api_models.Config()
            cfg.protocol = "https"
            cfg.region_id = "central"
            self._client = _robot_client.Client(cfg)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # --- token -----------------------------------------------------------

    def _access_token(self) -> str | None:
        with self._lock:
            if self._token and time.time() < self._token_exp - _TOKEN_SKEW_SECONDS:
                return self._token
        import requests  # dependency of dingtalk-stream, always present

        try:
            resp = requests.post(
                _TOKEN_URL,
                json={"appKey": self._app_key, "appSecret": self._app_secret},
                timeout=10,
            )
            data = resp.json()
        except Exception:
            logger.debug("表情回复:获取 access token 失败", exc_info=True)
            return None
        token = data.get("accessToken")
        if not token:
            logger.debug("表情回复:token 响应异常 %r", data)
            return None
        with self._lock:
            self._token = token
            self._token_exp = time.time() + int(data.get("expireIn", 7200))
        return token

    # --- public api ------------------------------------------------------

    def reply(self, emoji: str, msg_id: str, conversation_id: str) -> None:
        """Add an emoji reaction to a message."""
        self._emotion(emoji, msg_id, conversation_id, recall=False)

    def recall(self, emoji: str, msg_id: str, conversation_id: str) -> None:
        """Remove a previously-added emoji reaction from a message."""
        self._emotion(emoji, msg_id, conversation_id, recall=True)

    # --- internals -------------------------------------------------------

    def _emotion(self, emoji: str, msg_id: str, conversation_id: str, *, recall: bool) -> None:
        if not self._client or not msg_id or not conversation_id:
            return
        token = self._access_token()
        if not token:
            return
        action = "recall" if recall else "reply"
        try:
            runtime = _tea_util_models.RuntimeOptions()
            if recall:
                text_emotion = _robot_models.RobotRecallEmotionRequestTextEmotion(
                    emotion_id=_EMOTION_ID, emotion_name=emoji,
                    text=emoji, background_id=_BACKGROUND_ID,
                )
                request = _robot_models.RobotRecallEmotionRequest(
                    robot_code=self._robot_code, open_msg_id=msg_id,
                    open_conversation_id=conversation_id, emotion_type=_EMOTION_TYPE,
                    emotion_name=emoji, text_emotion=text_emotion,
                )
                headers = _robot_models.RobotRecallEmotionHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                self._client.robot_recall_emotion_with_options(request, headers, runtime)
            else:
                text_emotion = _robot_models.RobotReplyEmotionRequestTextEmotion(
                    emotion_id=_EMOTION_ID, emotion_name=emoji,
                    text=emoji, background_id=_BACKGROUND_ID,
                )
                request = _robot_models.RobotReplyEmotionRequest(
                    robot_code=self._robot_code, open_msg_id=msg_id,
                    open_conversation_id=conversation_id, emotion_type=_EMOTION_TYPE,
                    emotion_name=emoji, text_emotion=text_emotion,
                )
                headers = _robot_models.RobotReplyEmotionHeaders(
                    x_acs_dingtalk_access_token=token,
                )
                self._client.robot_reply_emotion_with_options(request, headers, runtime)
            logger.info("表情回复:%s %s on msg=%s", action, emoji, (msg_id or "")[:24])
        except Exception:
            logger.debug("表情回复:%s %s 失败", action, emoji, exc_info=True)


# Emoji labels used by the bridge (DingTalk text-emotion names).
THINKING = "🤔Thinking"
DONE = "🥳Done"
FAILED = "😖Failed"
