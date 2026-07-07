import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

EMAILS_DIR = Path(__file__).resolve().parents[2] / "emails"


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class Rendered:
    html: str
    text: str
    hash: str


async def render_batch(template_ref: str, props_list: list[dict]) -> list[Rendered]:
    proc = await asyncio.create_subprocess_exec(
        str(EMAILS_DIR / "node_modules" / ".bin" / "tsx"),
        str(EMAILS_DIR / "render.tsx"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=EMAILS_DIR,
    )
    payload = json.dumps({"template": template_ref, "props": props_list}).encode()
    out, err = await proc.communicate(payload)
    if proc.returncode != 0:
        raise RenderError(f"render failed for {template_ref!r}: {err.decode()[:2000]}")
    return [
        Rendered(html=item["html"], text=item["text"],
                 hash=hashlib.sha256(item["html"].encode()).hexdigest())
        for item in json.loads(out)
    ]
