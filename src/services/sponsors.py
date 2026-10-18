"""Public metadata cache and bounded, verified sponsor assets."""
import hashlib
import ipaddress
import logging
from io import BytesIO
from pathlib import PurePath
import re
from threading import RLock
import time
from urllib.parse import urlsplit
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 2 * 1024 * 1024
MAX_LOGO = 128 * 1024
CACHE_SECONDS = 60
_cache = {}
_lock = RLock()


def validate_link(value):
    value = (value or '').strip()
    if not value:
        return ''
    if len(value) > 2048 or any(ord(c) < 33 for c in value) or '\\' in value:
        raise ValueError('Link inválido. Utilize um endereço HTTPS público.')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError()
        host = parsed.hostname.rstrip('.').lower()
        if host == 'localhost' or '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if address is None:
            try:
                ascii_host = host.encode('idna').decode('ascii')
            except UnicodeError:
                raise ValueError() from None
            labels = ascii_host.split('.')
            if (len(ascii_host) > 253 or not any(c.isalpha() for c in labels[-1])
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
                raise ValueError()
    except ValueError:
        raise ValueError('Link inválido. Utilize um endereço HTTPS público.') from None
    return value


def process_logo(upload):
    formats = {'PNG': ('image/png', {'.png'}), 'JPEG': ('image/jpeg', {'.jpg', '.jpeg'}),
               'WEBP': ('image/webp', {'.webp'})}
    raw = upload.read(MAX_UPLOAD + 1)
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError('Envie uma logomarca de até 2 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                expected = formats.get(image.format)
                if (not expected or upload.mimetype != expected[0]
                        or PurePath(upload.filename or '').suffix.lower() not in expected[1]):
                    raise ValueError('Envie PNG, JPEG ou WebP com tipo e extensão correspondentes.')
                if image.width * image.height > 12_000_000 or max(image.size) > 8000:
                    raise ValueError('Resolução máxima: 12 megapixels e 8000 pixels por lado.')
                if getattr(image, 'is_animated', False):
                    raise ValueError('Logomarcas animadas não são aceitas.')
                image.load()
                prepared = ImageOps.exif_transpose(image).convert('RGBA')
                prepared.thumbnail((800, 320), Image.Resampling.LANCZOS)
                prepared.info.clear()
                output = BytesIO()
                prepared.save(output, format='WEBP', quality=85, method=6)
        data = output.getvalue()
        if len(data) > MAX_LOGO:
            raise ValueError('Imagem otimizada excede 128 KB. Simplifique a logomarca.')
        return data, hashlib.sha256(data).hexdigest()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('Imagem inválida ou corrompida.') from None


def invalidate_cache():
    with _lock:
        _cache.clear()


def public_sponsors(db, namespace):
    # Cache only public metadata, never image bytes, credentials or session data.
    key = hashlib.sha256(namespace.encode()).hexdigest()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        db.execute('SAVEPOINT sponsor_footer')
        try:
            rows = db.execute('''SELECT id,name,primary_slot,sort_order,link_url,logo_version
                FROM sponsors WHERE active=1 AND logo_data IS NOT NULL
                ORDER BY CASE WHEN primary_slot IS NULL THEN 1 ELSE 0 END,primary_slot,sort_order,id''').fetchall()
            rows = [dict(row) for row in rows]
        except Exception as exc:
            # An additive, optional footer must not break existing screens during rollout.
            db.execute('ROLLBACK TO SAVEPOINT sponsor_footer')
            logging.getLogger(__name__).warning('Sponsor footer unavailable: %s', type(exc).__name__)
            rows = []
        finally:
            db.execute('RELEASE SAVEPOINT sponsor_footer')
        if len(_cache) >= 32:
            _cache.clear()
        _cache[key] = (time.monotonic() + CACHE_SECONDS, rows)
        return rows
