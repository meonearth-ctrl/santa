# Draws Santa's app icon (a microphone with a small hat) as a 1024 px PNG.
# Run with Santa's Python (Pillow is already a dependency of whisper-local).
import os, sys
from PIL import Image, ImageDraw
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'icon-1024.png')
S = 1024
im = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([40, 40, S - 40, S - 40], radius=220, fill=(198, 40, 40, 255))
d.rounded_rectangle([392, 190, 632, 610], radius=120, fill='white')
d.arc([272, 330, 752, 770], start=20, end=160, fill='white', width=56)
d.rectangle([486, 760, 538, 850], fill='white')
d.rounded_rectangle([372, 830, 652, 884], radius=26, fill='white')
d.polygon([(560, 120), (820, 150), (760, 330)], fill='white')
d.ellipse([770, 90, 880, 200], fill='white')
im.save(out)
print('wrote', out)
