"""生成应用图标 assets/icon.ico（仅打包期使用，Pillow 不会被打进 exe）。

设计：深蓝→青绿渐变的圆角方块 + 白色「异」字，与界面配色一致。
若系统缺少中文字体，则退化为几何图形（同心环），保证一定生成成功。

用法：
    python tools\\make_icon.py
    python tools\\make_icon.py --out assets\\icon.ico --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIZES = [16, 24, 32, 48, 64, 128, 256]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
]


def _find_font() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def build_icon(size: int) -> "object":
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # 渐变背景：逐行插值，得到深蓝 -> 青绿
    top = (43, 109, 246)
    bottom = (126, 224, 192)
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        draw.line([(0, y), (size, y)], fill=color + (255,))

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=max(2, size // 5), fill=255
    )
    image.putalpha(mask)

    font_path = _find_font()
    glyph_drawn = False
    if font_path:
        try:
            font = ImageFont.truetype(font_path, int(size * 0.62))
            text = "异"
            box = draw.textbbox((0, 0), text, font=font)
            width = box[2] - box[0]
            height = box[3] - box[1]
            draw.text(
                ((size - width) / 2 - box[0], (size - height) / 2 - box[1]),
                text,
                font=font,
                fill=(8, 17, 31, 255),
            )
            glyph_drawn = True
        except Exception:
            glyph_drawn = False

    if not glyph_drawn:
        # 退化方案：同心环，象征「环」
        for index, radius_ratio in enumerate((0.42, 0.30, 0.18)):
            radius = size * radius_ratio
            width = max(1, int(size * 0.075))
            draw.ellipse(
                [(size / 2 - radius, size / 2 - radius), (size / 2 + radius, size / 2 + radius)],
                outline=(8, 17, 31, 255),
                width=width,
            )
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description="生成应用图标")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "assets" / "icon.ico"))
    parser.add_argument("--force", action="store_true", help="已存在时也重新生成")
    args = parser.parse_args()

    out_path = Path(args.out)
    if out_path.exists() and not args.force:
        print(f"图标已存在，跳过：{out_path}")
        return 0

    try:
        from PIL import Image
    except ImportError:
        print("未安装 Pillow，无法生成图标。请先执行：pip install pillow")
        print("（Pillow 仅用于生成图标，不会进入最终 exe）")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 以最大尺寸为基准，让 Pillow 向下采样生成其余尺寸（反过来会糊）
    base = build_icon(SIZES[-1])
    base.save(out_path, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"已生成图标：{out_path}（{out_path.stat().st_size} 字节，尺寸 {SIZES}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
