"""Generate a shields-style coverage SVG badge from ``coverage.xml``.

CI 每次跑完 pytest 后执行：读取 ``coverage.xml`` 的行覆盖率，生成徽标写入
``badges/coverage.svg``，随后提交回仓库供 README 引用。

自包含实现，不依赖外部徽标工具（``coverage-badge`` 依赖的 ``pkg_resources``
在现代 setuptools 中已不可用）。
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

LABEL_WIDTH_CHAR = 7
VALUE_WIDTH_CHAR = 7
PADDING = 8


def _color(line_rate: float) -> str:
    """按覆盖率区间返回徽标颜色。"""
    if line_rate >= 0.9:
        return "brightgreen"
    if line_rate >= 0.75:
        return "green"
    if line_rate >= 0.6:
        return "yellowgreen"
    if line_rate >= 0.4:
        return "orange"
    return "red"


def _badge_svg(label: str, value: str, color: str) -> str:
    """构造 shields 风格徽标：左段 label（灰），右段 value（彩色）。"""
    label_w = PADDING + len(label) * LABEL_WIDTH_CHAR
    value_w = PADDING + len(value) * VALUE_WIDTH_CHAR
    total_w = label_w + value_w
    label_cx = label_w // 2
    value_cx = label_w + value_w // 2
    font = "Verdana,Geneva,DejaVu Sans,sans-serif"

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20"',
        f'  role="img" aria-label="{label}: {value}">',
        f"  <title>{label}: {value}</title>",
        '  <linearGradient id="s" x2="0" y2="100%">',
        '    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>',
        '    <stop offset="1" stop-opacity=".1"/>',
        "  </linearGradient>",
        f'  <clipPath id="r"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></clipPath>',
        '  <g clip-path="url(#r)">',
        f'    <rect width="{label_w}" height="20" fill="#555"/>',
        f'    <rect x="{label_w}" width="{value_w}" height="20" fill="{color}"/>',
        f'    <rect width="{total_w}" height="20" fill="url(#s)"/>',
        "  </g>",
        f'  <g fill="#fff" text-anchor="middle" font-family="{font}"',
        '    text-rendering="geometricPrecision" font-size="11">',
        f'    <text x="{label_cx}" y="15" fill="#010101" fill-opacity=".3">{label}</text>',
        f'    <text x="{label_cx}" y="14">{label}</text>',
        f'    <text x="{value_cx}" y="15" fill="#010101" fill-opacity=".3">{value}</text>',
        f'    <text x="{value_cx}" y="14">{value}</text>',
        "  </g>",
        "</svg>",
    ]
    return "\n".join(lines)


def main() -> int:
    coverage_xml = Path("coverage.xml")
    output = Path("badges/coverage.svg")
    if not coverage_xml.exists():
        print(f"coverage.xml not found at {coverage_xml.resolve()}", file=sys.stderr)
        return 1

    root = ET.parse(coverage_xml).getroot()
    line_rate = float(root.attrib["line-rate"])  # 0.0 ~ 1.0
    value = f"{line_rate * 100:.0f}%"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_badge_svg("coverage", value, _color(line_rate)), encoding="utf-8")
    print(f"Wrote {output} with coverage {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
