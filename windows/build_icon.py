"""Generate VulaPrint.ico from a source PNG for the Windows build.

Called by windows/build.ps1, but runnable standalone from Linux too:
    python windows/build_icon.py assets/Vula_Logo.png windows/VulaPrint.ico
"""
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: build_icon.py <source.png> <output.ico>", file=sys.stderr)
        return 2

    src, dst = sys.argv[1], sys.argv[2]

    try:
        from PIL import Image
    except ImportError:
        print(
            "Pillow is not installed. Run: pip install -r requirements_app.txt",
            file=sys.stderr,
        )
        return 1

    img = Image.open(src).convert("RGBA")
    img.save(
        dst,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())