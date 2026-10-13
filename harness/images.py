"""Durable, bounded local image attachments shared by the bridge and app-server."""
import hashlib
import io
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote

from PIL import Image

MAX_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 9
FORMATS = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp', 'GIF': '.gif'}


def snapshot(path, directory):
    if not isinstance(path, str) or not path:
        raise ValueError('需要本地图片路径')
    source = Path(path).expanduser()
    if not source.is_absolute() or not source.is_file():
        raise ValueError('图片必须是现有本地文件')
    with source.open('rb') as handle:
        data = handle.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise ValueError('图片大小需在 20 MiB 以内')
    try:
        with Image.open(io.BytesIO(data)) as image:
            suffix = FORMATS.get(image.format)
            image.verify()
        if not suffix:
            raise ValueError('仅支持 PNG/JPEG/WebP/GIF')
    except Exception as exc:
        raise ValueError('图片格式无效或不支持') from exc
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / (hashlib.sha256(data).hexdigest() + suffix)
    if not target.exists():
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return str(target)


def local_result_images(text):
    """Only explicit image links / MEDIA directives request delivery, never arbitrary paths."""
    paths = []
    pattern = r'!\[[^\]]*\]\(([^\n)]+)\)|^MEDIA:\s*(.+)$'
    for match in re.finditer(pattern, text, re.M):
        path = (match.group(1) or match.group(2)).strip().strip('<>')
        if path.startswith(('sandbox:', 'file://')):
            path = path.split(':', 1)[1]
            if path.startswith('//'):
                path = path[2:]
        path = unquote(path)
        if path.startswith('/') and path not in paths:
            paths.append(path)
    return paths[:MAX_IMAGES]
