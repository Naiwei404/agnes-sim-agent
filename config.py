"""Agnes 模型配置加载。

优先读取系统环境变量 AGNES_API_KEY / AGNES_BASE_URL / AGNES_MODEL；
若未设置则从项目根目录的 .env 文件读取（不依赖额外依赖包）。
密钥绝不写入代码或提交到仓库。
"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


# 先加载 .env（仅当对应变量尚未存在于环境中时填入）
_load_dotenv(_ROOT / ".env")

AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "")
AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://api.agnes-ai.cn/v1")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")


def require_key() -> str:
    if not AGNES_API_KEY or AGNES_API_KEY.startswith("sk-xxxx"):
        raise RuntimeError("AGNES_API_KEY 未配置：请复制 .env.example 为 .env 并填入真实 Key")
    return AGNES_API_KEY
