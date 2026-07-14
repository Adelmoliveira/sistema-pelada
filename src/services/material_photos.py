import base64
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_UPLOAD_BYTES = 4 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def _jpeg_data_url(image, max_size, quality):
    prepared = image.copy()
    prepared.thumbnail(max_size, Image.Resampling.LANCZOS)
    output = BytesIO()
    prepared.save(output, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def process_material_photo(upload):
    if not upload or not upload.filename:
        return None
    raw = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("A foto deve ter no máximo 4 MB.")
    try:
        with Image.open(BytesIO(raw)) as opened:
            if opened.format not in ALLOWED_FORMATS:
                raise ValueError("Formato inválido. Envie uma foto JPG, PNG ou WebP.")
            if opened.width * opened.height > 20_000_000:
                raise ValueError("A resolução da foto é muito alta. Envie uma imagem menor.")
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                image.seek(0)
            image = image.convert("RGB")
            photo = _jpeg_data_url(image, (1200, 1200), 82)
            thumbnail = _jpeg_data_url(image, (180, 180), 76)
            return photo, thumbnail
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("A foto enviada é inválida ou está corrompida.")
