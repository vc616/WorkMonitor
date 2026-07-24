from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "assets"
CANVAS_SIZE = 1024


def build_icon() -> Image.Image:
    image = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # A clock and activity pulse communicate time-based work monitoring at small sizes.
    draw.rounded_rectangle((72, 72, 952, 952), radius=210, fill="#153B3A")
    draw.rounded_rectangle((112, 112, 912, 912), radius=175, fill="#167663")
    draw.ellipse((236, 220, 788, 772), fill="#F7FAF9")
    draw.ellipse((286, 270, 738, 722), fill="#153B3A")
    draw.rounded_rectangle((472, 302, 552, 521), radius=40, fill="#F7FAF9")
    draw.polygon(((512, 476), (680, 574), (640, 642), (472, 544)), fill="#F7FAF9")

    # The lower pulse remains readable in the 16 px taskbar rendition.
    draw.line(
        ((160, 724), (326, 724), (388, 622), (470, 826), (558, 682), (626, 724), (864, 724)),
        fill="#69D5B5",
        width=58,
        joint="curve",
    )
    return image


def main() -> None:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    image = build_icon()
    image.save(ASSET_DIR / "work_monitor.png", optimize=True)
    image.save(
        ASSET_DIR / "work_monitor.ico",
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
