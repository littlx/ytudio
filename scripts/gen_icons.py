#!/usr/bin/env python3
"""生成 PWA 图标：从 templates/icon.jpg 生成标准 PNG 图标。

输出：
  templates/icon-192.png         普通图标 192x192
  templates/icon-512.png         普通图标 512x512
  templates/icon-maskable-512.png  maskable 图标（带 20% 安全区 + 背景填充）

用法：python scripts/gen_icons.py
依赖：Pillow（pip install Pillow）
"""
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("请先安装 Pillow: pip install Pillow", file=sys.stderr)
    sys.exit(1)

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "templates" / "icon.jpg"
THEME_BG = (15, 17, 23)  # 与 manifest background_color 一致 (#0f1117)


def make_icon(size: int, dst: Path) -> None:
    img = Image.open(SRC).convert("RGBA")
    img = img.resize((size, size), Image.LANCZOS)
    # 合成到不透明背景，避免 JPEG 残留
    bg = Image.new("RGBA", (size, size), THEME_BG + (255,))
    bg.alpha_composite(img)
    bg.convert("RGB").save(dst, "PNG")
    print(f"已生成 {dst.relative_to(BASE)}")


def make_maskable(size: int, dst: Path) -> None:
    """maskable 图标：内容缩到 80%（留 10% 安全区），外圈填充背景色。"""
    img = Image.open(SRC).convert("RGBA")
    bg = Image.new("RGBA", (size, size), THEME_BG + (255,))
    # 安全区：内容占 80%，居中放置
    inner = int(size * 0.80)
    img = img.resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    bg.alpha_composite(img, (offset, offset))
    bg.convert("RGB").save(dst, "PNG")
    print(f"已生成 {dst.relative_to(BASE)} (maskable)")


def main() -> None:
    if not SRC.exists():
        print(f"源文件不存在: {SRC}", file=sys.stderr)
        sys.exit(1)
    out_dir = SRC.parent
    make_icon(192, out_dir / "icon-192.png")
    make_icon(512, out_dir / "icon-512.png")
    make_maskable(512, out_dir / "icon-maskable-512.png")
    print("\n图标生成完成。请确保 manifest.json 已引用这些文件。")


if __name__ == "__main__":
    main()
