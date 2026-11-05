"""把模型返回的 Markdown 渲染成终端能看的样子。

模型习惯用 Markdown 回答：`## 标题`、`**粗体**`、`- 列表`、表格、代码块。
直接 print 出去，终端里看到的是一堆 `**` 和 `|---|---|`，很难读。

这里只做**终端里真正有意义**的那几种转换，不追求完整的 Markdown 实现：

* 标题        → 加粗着色，并按层级缩进
* 粗体/行内码 → ANSI 加粗 / 反显
* 列表        → 统一成 `•` 和缩进
* 表格        → 按列宽对齐，CJK 按双宽计算
* 代码块      → 灰色缩进，围栏本身不显示

不支持颜色时（重定向到文件、管道）全部降级为纯文本，不留 ANSI 垃圾。
"""

from __future__ import annotations

import re
import unicodedata

# 终端里 CJK 与全角符号占两列，len() 会算少，表格因此错位。
def display_width(text):
    width = 0
    for char in _strip_ansi(str(text)):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


_ANSI = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text):
    return _ANSI.sub("", str(text))


def _pad(text, width):
    """按显示宽度右侧补空格。"""
    return str(text) + " " * max(0, width - display_width(text))


class MarkdownConsole:
    """行式 Markdown 渲染器。逐行处理，不构建语法树。"""

    HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
    BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
    ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
    FENCE = re.compile(r"^\s*```(.*)$")
    TABLE_SEP = re.compile(r"^\s*\|?[\s:-]*[-]{2,}[\s:|-]*\|?\s*$")
    BOLD = re.compile(r"\*\*(.+?)\*\*")
    ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
    CODE = re.compile(r"`([^`\n]+)`")

    # 中文 Windows 终端常常是 GBK，打不出 • 和 ─。按目标编码探测后降级，
    # 否则渲染本身会抛 UnicodeEncodeError，比不渲染还糟。
    GLYPHS = (
        {"bullet": "•", "rule": "─"},   # • ─
        {"bullet": "-", "rule": "-"},              # 纯 ASCII 兜底
    )

    def __init__(self, use_color=True, encoding=None):
        self.use_color = bool(use_color)
        self.glyphs = self._pick_glyphs(encoding)

    @classmethod
    def _pick_glyphs(cls, encoding):
        if not encoding:
            return cls.GLYPHS[0]
        for candidate in cls.GLYPHS:
            try:
                "".join(candidate.values()).encode(encoding)
                return candidate
            except (UnicodeEncodeError, LookupError):
                continue
        return cls.GLYPHS[-1]

    def _paint(self, text, code):
        if not self.use_color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def _inline(self, text):
        """处理行内标记。顺序要紧：先取出代码，避免它内部的 * 被当成强调。"""
        placeholders = []

        def stash(match):
            placeholders.append(match.group(1))
            return f"\x00{len(placeholders) - 1}\x00"

        text = self.CODE.sub(stash, text)
        text = self.BOLD.sub(lambda m: self._paint(m.group(1), "1"), text)
        text = self.ITALIC.sub(lambda m: self._paint(m.group(1), "3"), text)
        for index, code in enumerate(placeholders):
            text = text.replace(f"\x00{index}\x00", self._paint(code, "36"))
        return text

    def _render_table(self, rows):
        """rows 是若干行原始 Markdown 表格文本（含分隔行）。"""
        parsed = []
        for row in rows:
            if self.TABLE_SEP.match(row):
                continue
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            parsed.append(cells)
        if not parsed:
            return []

        columns = max(len(row) for row in parsed)
        parsed = [row + [""] * (columns - len(row)) for row in parsed]
        rendered = [[self._inline(cell) for cell in row] for row in parsed]
        widths = [
            max(display_width(row[index]) for row in rendered)
            for index in range(columns)
        ]

        out = []
        header, *body = rendered
        out.append("  " + "  ".join(
            self._paint(_pad(cell, widths[i]), "1") for i, cell in enumerate(header)
        ))
        rule = self.glyphs["rule"]
        out.append("  " + "  ".join(self._paint(rule * widths[i], "90") for i in range(columns)))
        for row in body:
            out.append("  " + "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))
        return out

    def render(self, text):
        lines = str(text).splitlines()
        out = []
        in_fence = False
        table_buffer = []

        def flush_table():
            if table_buffer:
                out.extend(self._render_table(table_buffer))
                table_buffer.clear()

        for line in lines:
            fence = self.FENCE.match(line)
            if fence:
                flush_table()
                in_fence = not in_fence
                continue
            if in_fence:
                out.append(self._paint("    " + line, "90"))
                continue

            # 表格：正文行与分隔行都以 | 开头，攒够一段再统一对齐
            if line.strip().startswith("|") or (table_buffer and self.TABLE_SEP.match(line)):
                table_buffer.append(line)
                continue
            flush_table()

            heading = self.HEADING.match(line)
            if heading:
                level = len(heading.group(1))
                body = self._inline(heading.group(2))
                color = "36;1" if level <= 2 else "37;1"
                out.append("")
                out.append(self._paint(body, color))
                continue

            bullet = self.BULLET.match(line)
            if bullet:
                indent = len(bullet.group(1))
                out.append(" " * (indent + 2) + self._paint(self.glyphs["bullet"], "36") + " " + self._inline(bullet.group(2)))
                continue

            ordered = self.ORDERED.match(line)
            if ordered:
                indent = len(ordered.group(1))
                marker = self._paint(f"{ordered.group(2)}.", "36")
                out.append(" " * (indent + 2) + marker + " " + self._inline(ordered.group(3)))
                continue

            if not line.strip():
                out.append("")
                continue

            out.append(self._inline(line))

        flush_table()

        # 折叠连续空行，避免渲染后到处是空档
        collapsed = []
        for line in out:
            if not line.strip() and collapsed and not collapsed[-1].strip():
                continue
            collapsed.append(line)
        return "\n".join(collapsed).strip("\n")
