"""Unit tests for ``app/services/error_copy.py`` (Issue #5).

Contract source: ``docs/design/issue-5-error-copy.md`` §4.1, §5.

These tests are RED until ``app/services/error_copy.py`` exists and exports:

  - ``ERROR_COPY: dict[str, str]`` — single source of truth, machine code → 中文 message
  - ``message_for(code: str) -> str`` — lookup helper; returns
    ``"操作失败，请稍后重试"`` for unknown codes (per §5).

The dictionary keys MUST equal the union of the 24 codes the design §4.1 lists.
This test file is the design-doc-derived oracle; if a code is missing from the
implementation, the assertion of equality fails.
"""
from __future__ import annotations

import pytest


# Single source of truth: design §4.1's table, transcribed verbatim. If the
# design changes, update this dict + the implementation in lock-step.
EXPECTED_COPY: dict[str, str] = {
    # auth.py
    "rate_limit": "请求过于频繁，请稍后再试",
    "mail_send_failed": "验证码邮件发送失败，请稍后重试",
    "bad_request": "请求参数有误，请检查后重试",
    "invalid_or_expired": "验证码无效或已过期，请重新获取",
    "unauthenticated": "登录已过期，请刷新页面重新登录",
    "bad_origin": "请求来源校验失败，请刷新页面重试",
    # research.py
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
    # research.py / history.py — HTTPException(404)
    "not_found": "记录不存在或已被删除",
    # file_processor.py — LimitExceededError
    "files_too_many": "上传文件数超出单次 8 个的上限，请删减后重试",
    "unsupported_type": (
        "文件类型不支持，请改为 md/txt/pdf/docx/pptx/xlsx/png/jpg/jpeg/webp/gif"
    ),
    "empty_file": "上传的文件是空的（0 字节），请检查后重试",
    "file_too_large": "单个文件超过 50 MB 上限，请压缩或拆分后再上传",
    "total_too_large": "上传总大小超过限制，请删减后重试",
    "mime_mismatch": "文件内容与扩展名不一致，请重新选择文件",
}


def test_module_imports_and_exports_dict_and_helper() -> None:
    """Module must exist and expose ``ERROR_COPY`` + ``message_for``."""
    from app.services import error_copy

    assert hasattr(error_copy, "ERROR_COPY"), "missing ERROR_COPY"
    assert isinstance(error_copy.ERROR_COPY, dict)
    assert hasattr(error_copy, "message_for"), "missing message_for()"
    assert callable(error_copy.message_for)


def test_error_copy_keys_equal_design_set() -> None:
    """Key set MUST equal design §4.1's 24-code union (no missing, no extra)."""
    from app.services.error_copy import ERROR_COPY

    actual = set(ERROR_COPY.keys())
    expected = set(EXPECTED_COPY.keys())
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"ERROR_COPY missing codes from design §4.1: {sorted(missing)}"
    assert not extra, (
        f"ERROR_COPY has codes NOT in design §4.1 (must register first): {sorted(extra)}"
    )


@pytest.mark.parametrize(
    ("code", "expected_message"),
    sorted(EXPECTED_COPY.items()),
    ids=sorted(EXPECTED_COPY.keys()),
)
def test_error_copy_values_match_design_exactly(code: str, expected_message: str) -> None:
    """Each Chinese string must match design §4.1 character-for-character."""
    from app.services.error_copy import ERROR_COPY

    assert ERROR_COPY[code] == expected_message


@pytest.mark.parametrize(
    ("code", "expected_message"),
    sorted(EXPECTED_COPY.items()),
    ids=sorted(EXPECTED_COPY.keys()),
)
def test_message_for_returns_design_string(code: str, expected_message: str) -> None:
    """``message_for(code)`` returns the same string as ``ERROR_COPY[code]``."""
    from app.services.error_copy import message_for

    assert message_for(code) == expected_message


def test_message_for_unknown_code_returns_generic_fallback() -> None:
    """Per design §5: unknown code returns ``"操作失败，请稍后重试"``."""
    from app.services.error_copy import message_for

    assert message_for("totally_unknown_code_xyz") == "操作失败，请稍后重试"


def test_message_for_empty_string_returns_generic_fallback() -> None:
    """Empty string is treated as unknown."""
    from app.services.error_copy import message_for

    assert message_for("") == "操作失败，请稍后重试"
