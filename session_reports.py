"""Read-only report projection of visible assistant cards and archived media."""
import re
from urllib.parse import quote, unquote, urlsplit

MAX_REPORT_BYTES = 512 * 1024
_LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\((<[^>\n]+>|[^()\s]+)\)")
_ATTACH = re.compile(r"\[\[attach:([^\]\n]+)\]\]")
_ARTIFACT = re.compile(r"/app/v2/artifacts/(med_[a-f0-9]{32})$")


def report_ref(raw):
    value = str(raw or "").strip().removeprefix("<").removesuffix(">")
    if any(ord(c) < 32 for c in value):
        return None
    if _ARTIFACT.fullmatch(value):
        return value
    if value.startswith("file://"):
        parts = urlsplit(value)
        if parts.netloc not in ("", "localhost"):
            return None
        value = unquote(parts.path)
    value = re.sub(r":\d+(?::\d+)?$", "", value)
    if value.startswith("/") and re.search(r"\.(?:md|markdown)$", value, re.I):
        return value
    return None


def delivered_refs(cards):
    refs = {}
    for card in cards:
        if card.get("role") not in ("agent", "assistant") or card.get("kind", "text") not in ("text", "markdown"):
            continue
        body = card.get("body") or {}
        text = body if isinstance(body, str) else str(body.get("text") or "")
        # Examples inside code are not delivered documents.
        text = re.sub(r"(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1[^\n]*$", "", text)
        text = re.sub(r"(`+)[^`]*?\1", "", text)
        for match in _LINK.finditer(text):
            ref = report_ref(match[2])
            if ref:
                refs[ref] = match[1].strip()
        for match in _ATTACH.finditer(text):
            ref = report_ref(match[1])
            if ref:
                refs.setdefault(ref, "")
        if isinstance(body, dict):
            for att in body.get("attachments") or []:
                if not isinstance(att, dict) or not re.search(r"\.(md|markdown)$", str(att.get("filename") or ""), re.I):
                    continue
                ref = report_ref(att.get("download_url") or att.get("path"))
                if ref:
                    refs.setdefault(ref, att.get("filename", ""))
    return refs


def report_manifest(media, session_id, refs):
    items = {}
    for ref, title in refs.items():
        match = _ARTIFACT.fullmatch(ref)
        item = media.find_session_artifact(session_id, media_id=match[1] if match else None,
                                           source_ref=None if match else ref)
        if not item or not item["available"] or not re.search(r"\.(md|markdown)$", item["filename"], re.I):
            continue
        mid = item["media_id"]
        if mid not in items:
            items[mid] = {"media_id": mid, "filename": item["filename"], "title": title or item["filename"],
                          "byte_size": item["byte_size"], "sha256": item["sha256"], "refs": [],
                          "download_url": "/app/v2/sessions/" + quote(session_id, safe="") + "/reports?media_id=" + mid}
        items[mid]["refs"].append(ref)
    return list(items.values())
