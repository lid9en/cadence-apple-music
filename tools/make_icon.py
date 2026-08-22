"""Generate the Cadence app icon: an equaliser mark on a dark rounded tile."""
from PIL import Image, ImageDraw

ACCENT = (250, 45, 72)
BG_TOP = (28, 26, 38)
BG_BOT = (13, 13, 18)

def render(size):
    s = size * 4  # supersample, then downscale for clean edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # vertical gradient tile with rounded corners
    tile = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    for y in range(s):
        t = y / max(1, s - 1)
        c = tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3))
        td.line([(0, y), (s, y)], fill=c + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    img.paste(tile, (0, 0), mask)

    # five equaliser bars, tallest in the middle
    heights = [0.34, 0.58, 0.82, 0.50, 0.28]
    bw = s * 0.088
    gap = s * 0.055
    total = len(heights) * bw + (len(heights) - 1) * gap
    x = (s - total) / 2
    cy = s * 0.53
    for h in heights:
        bh = s * h
        d.rounded_rectangle(
            [x, cy - bh / 2, x + bw, cy + bh / 2],
            radius=bw / 2, fill=ACCENT + (255,),
        )
        x += bw + gap

    return img.resize((size, size), Image.LANCZOS)

sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [render(n) for n in sizes]
imgs[-1].save("assets/cadence.ico", format="ICO",
              sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
imgs[-1].save("assets/cadence.png")
print("wrote assets/cadence.ico and assets/cadence.png")
