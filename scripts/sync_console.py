"""构建后的回放页面必须进入 Rust 引擎的编译输入目录。"""
from pathlib import Path
import shutil


root = Path(__file__).resolve().parents[1]
source = root / "fuju-trace-console" / "dist"
target = root / "fuju-trace-engine" / "crates" / "fuju-trace-engine" / "console_dist"
if not (source / "index.html").is_file():
    raise SystemExit("先运行 cd fuju-trace-console && VITE_API=http npm run build")
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target)
print(target)
