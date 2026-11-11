from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SHOT_DIR = DATA_DIR / "screenshots"
DATA_DIR.mkdir(exist_ok=True)
SHOT_DIR.mkdir(exist_ok=True)

_cfg_file = ROOT / "config.yaml"
if not _cfg_file.exists():
    raise SystemExit("找不到 config.yaml，请先把 config.example.yaml 复制为 config.yaml 并修改")

cfg = yaml.safe_load(_cfg_file.read_text(encoding="utf-8"))

STREAMER = cfg["streamer"]
BILI = cfg.get("bilibili", {})
QQ = cfg["qq"]
WEB = cfg["web"]
RULES = cfg.get("rules", {})

QQ_GROUPS = {int(g) for g in QQ.get("groups", [])}
QQ_ADMINS = {int(a) for a in QQ.get("admins", [])}
