"""附件文本提取（从 bridge.py 搬过来，未改动逻辑）。"""

from __future__ import annotations

from io import BytesIO

import httpx


async def extract_attachment_text(att: dict, max_chars: int = 30000) -> str:
    """下载附件并提取文本。失败返回错误描述字符串。"""
    name = att.get("name", "unknown")
    url = att.get("url", "")
    mime = (att.get("mime_type") or "").lower()
    if not url:
        return ""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception as e:
        return f"[下载失败: {name} - {e}]"

    text = ""
    lower_name = name.lower()

    # 文本类
    if (
        lower_name.endswith((".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".xml", ".log"))
        or mime.startswith("text/")
        or "json" in mime
    ):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = data.decode("latin-1", errors="replace")

    # PDF
    elif lower_name.endswith(".pdf") or "pdf" in mime:
        try:
            import pypdf
            reader = pypdf.PdfReader(BytesIO(data))
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
        except ImportError:
            return "[PDF 文本提取需要安装 pypdf：pip install pypdf]"
        except Exception as e:
            return f"[PDF 解析失败: {name} - {e}]"

    # DOCX
    elif lower_name.endswith(".docx") or "wordprocessing" in mime:
        try:
            import docx
            doc = docx.Document(BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            return "[DOCX 文本提取需要安装 python-docx：pip install python-docx]"
        except Exception as e:
            return f"[DOCX 解析失败: {name} - {e}]"

    else:
        return f"[不支持的文件类型: {name} ({mime})]"

    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... 文件过长，已截断到 {max_chars} 字符 ...]"
    return text


def split_image_and_text_attachments(
    attachments: list[dict],
) -> tuple[list[tuple[str, str]], list[dict]]:
    """把附件分成 (image_urls, text_attachments)。
    image_urls: [(url, mime_type), ...]，可直接传给 adapter
    text_attachments: 还需要 await extract_attachment_text() 提取的
    """
    images: list[tuple[str, str]] = []
    texts: list[dict] = []
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    for att in attachments:
        mime = (att.get("mime_type") or "").lower()
        name = att.get("name", "file")
        url = att.get("url", "")
        if mime.startswith("image/") or any(name.lower().endswith(e) for e in image_exts):
            images.append((url, mime or "image/png"))
        else:
            texts.append(att)
    return images, texts
