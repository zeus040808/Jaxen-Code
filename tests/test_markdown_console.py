"""终端渲染必须在中文 Windows 上不崩、不错位。

真实故障（2026-08-19 人工试用）：
  * 模型用 Markdown 回答，终端直接 print，满屏 ** 和 |---|---|
  * 渲染里硬写 • 和 ─，在 GBK 终端上抛 UnicodeEncodeError——比不渲染更糟
  * 表格用 len() 算列宽，CJK 占两列，整张表错位
"""

from hh_agent_pro.markdown_console import MarkdownConsole, display_width


def _plain(text, encoding=None):
    return MarkdownConsole(use_color=False, encoding=encoding).render(text)


def test_cjk_counts_as_two_columns():
    assert display_width("abc") == 3
    assert display_width("能力") == 4
    assert display_width("a能力b") == 6


def test_markup_syntax_does_not_reach_the_screen():
    out = _plain("## 标题\n\n- **名称**：HH\n- 用 `rg` 搜索\n")

    for noise in ("##", "**", "`"):
        assert noise not in out, f"{noise!r} 不该出现在终端输出里"
    assert "标题" in out and "名称" in out and "rg" in out


def test_table_columns_line_up_with_cjk():
    out = _plain(
        "| 能力 | 是否支持 |\n"
        "|------|----------|\n"
        "| 搜索本地代码 | 支持 |\n"
        "| 联网 | 不支持 |\n"
    )
    rows = [line for line in out.splitlines() if line.strip()]
    assert len(rows) == 4  # 表头 + 分隔 + 两行正文

    # 每列起始位置必须一致，否则表格是歪的
    def second_column_start(line):
        return display_width(line.split("  ")[0] + "  ")

    widths = {display_width(row) for row in rows}
    assert len(widths) == 1, f"各行显示宽度不一致：{widths}"


def test_gbk_terminal_falls_back_to_ascii_glyphs():
    """GBK 打不出 • 和 ─，必须降级而不是抛异常。"""

    out = _plain("- 第一项\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="gbk")

    out.encode("gbk")  # 关键断言：能编码，就不会在真实终端上崩
    assert "•" not in out and "─" not in out


def test_utf8_terminal_keeps_nicer_glyphs():
    out = _plain("- 第一项\n", encoding="utf-8")

    assert "•" in out


def test_code_fence_content_is_kept_but_markers_dropped():
    out = _plain("说明：\n```python\nprint('hi')\n```\n")

    assert "print('hi')" in out
    assert "```" not in out


def test_inline_code_is_not_parsed_as_emphasis():
    """`a * b * c` 里的星号是代码，不是强调。"""

    out = _plain("公式 `a * b * c` 结束")

    assert "a * b * c" in out


def test_plain_text_survives_untouched():
    out = _plain("就是一句普通的话。")

    assert out == "就是一句普通的话。"


def test_color_mode_emits_ansi_and_plain_mode_does_not():
    colored = MarkdownConsole(use_color=True, encoding="utf-8").render("**粗**")
    plain = MarkdownConsole(use_color=False, encoding="utf-8").render("**粗**")

    assert "\033[" in colored
    assert "\033[" not in plain
    assert plain == "粗"
