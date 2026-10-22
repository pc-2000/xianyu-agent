"""Update the installer token without embedding Python code in cmd.exe."""

import os
from pathlib import Path
import re


def update_bridge_env(path: Path, token: str) -> None:
    if not token or "\n" in token or "\r" in token:
        raise ValueError("BRIDGE_TOKEN must be a non-empty single line")
    content = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    pattern = r"^BRIDGE_TOKEN=.*$"
    if re.search(pattern, content, flags=re.MULTILINE):
        content = re.sub(pattern, lambda _: f"BRIDGE_TOKEN={token}", content,
                         flags=re.MULTILINE)
    else:
        content += ("" if not content or content.endswith("\n") else "\n")
        content += f"BRIDGE_TOKEN={token}\n"
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    update_bridge_env(Path(__file__).resolve().parent.parent / ".env",
                      os.environ["BRIDGE_TOKEN"])
    print("[OK] Updated BRIDGE_TOKEN in .env")
