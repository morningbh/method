"""Single source of truth for error-code → Chinese message mapping.

Contract source: ``docs/design/issue-5-error-copy.md`` §4.1, §5.

Every 4xx/5xx JSON error body the backend emits must include a human-readable
``message`` field keyed off the machine ``error`` code. Routers call
``message_for(code)`` when assembling the response; the frontend renders
``body.message`` (or falls back to a generic string — it never shows the raw
code to the end user).

Adding a new error code: register it here FIRST, update the design doc's
§4.1 table in the same commit. Tests in ``tests/unit/test_error_copy.py``
assert equality between this dict's key set and the design set.
"""
from __future__ import annotations


# The 24-code union from design §4.1 (verbatim Chinese copy). Keep sorted by
# the grouping in the design table (auth → research → file_processor) for
# review clarity.
ERROR_COPY: dict[str, str] = {
    # --- auth.py -----------------------------------------------------------
    "rate_limit": "请求过于频繁，请稍后再试",
    "mail_send_failed": "验证码邮件发送失败，请稍后重试",
    "bad_request": "请求参数有误，请检查后重试",
    "invalid_or_expired": "验证码无效或已过期，请重新获取",
    "unauthenticated": "登录已过期，请刷新页面重新登录",
    "bad_origin": "请求来源校验失败，请刷新页面重试",
    # --- research.py -------------------------------------------------------
    "empty_question": "请输入研究问题",
    "question_too_long": "问题过长，请精简后再提交",
    "invalid_mode": "研究模式不合法，请刷新页面重试",
    "internal": "服务器开小差了，请稍后重试",
    "plan_missing": "方案文件缺失，请联系管理员",
    "request_busy": "请求仍在处理中，请等它结束后再操作",
    "request_not_finalized": "当前请求还在生成中，请等它结束再评论",
    "anchor_text_invalid": "选中的原文不合法，请重新框选",
    "body_invalid": "评论内容不符合要求（长度或格式），请修改后重试",
    "anchor_context_too_long": "选中段落上下文过长，请缩短后重试",
    "body_empty": "评论不能为空",
    "ai_reply_not_deletable": "AI 回复不能被删除",
    # --- shared (HTTPException detail="not_found") ------------------------
    "not_found": "记录不存在或已被删除",
    # --- file_processor.py / LimitExceededError ---------------------------
    "files_too_many": "上传文件数超出单次 8 个的上限，请删减后重试",
    "unsupported_type": (
        "文件类型不支持，请改为 md/txt/pdf/docx/pptx/xlsx/png/jpg/jpeg/webp/gif"
    ),
    "empty_file": "上传的文件是空的（0 字节），请检查后重试",
    "file_too_large": "单个文件超过 50 MB 上限，请压缩或拆分后再上传",
    "total_too_large": "上传总大小超过限制，请删减后重试",
    "mime_mismatch": "文件内容与扩展名不一致，请重新选择文件",
}


_GENERIC_FALLBACK = "操作失败，请稍后重试"


def message_for(code: str) -> str:
    """Return the Chinese message for ``code``, or a generic fallback.

    Per design §5: unknown / empty codes get ``"操作失败，请稍后重试"`` so
    that a routing oversight never leaks a raw machine code to the user.
    """
    if not code:
        return _GENERIC_FALLBACK
    return ERROR_COPY.get(code, _GENERIC_FALLBACK)


def error_response_body(code: str) -> dict[str, str]:
    """Return the canonical ``{"error": code, "message": <中文>}`` body.

    Routers that build ``JSONResponse`` manually can import this helper to
    avoid spelling out both keys inline at every call site.
    """
    return {"error": code, "message": message_for(code)}
