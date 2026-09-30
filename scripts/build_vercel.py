from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "public"

if OUTPUT.exists():
    shutil.rmtree(OUTPUT)
OUTPUT.mkdir()
shutil.copytree(ROOT / "smart_building" / "static", OUTPUT / "static")
shutil.copytree(ROOT / "vercel", OUTPUT / "vercel")
shutil.copy2(ROOT / "vercel" / "index.html", OUTPUT / "index.html")
print("Built the LocalStorage Vercel deployment in public/")
