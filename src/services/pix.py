import io
import base64
import unicodedata
import qrcode

def pix_text(value, limit):
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return " ".join(value.upper().split())[:limit]

def pix_tlv(identifier, value):
    return f"{identifier}{len(value):02d}{value}"

def pix_crc16(payload):
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"

def pix_payload(amount_cents, pix_key, merchant_name, merchant_city):
    merchant_account = pix_tlv("00", "br.gov.bcb.pix") + pix_tlv("01", pix_key)
    payload = "".join((
        pix_tlv("00", "01"),
        pix_tlv("26", merchant_account),
        pix_tlv("52", "0000"),
        pix_tlv("53", "986"),
        pix_tlv("54", f"{amount_cents / 100:.2f}"),
        pix_tlv("58", "BR"),
        pix_tlv("59", pix_text(merchant_name, 25)),
        pix_tlv("60", pix_text(merchant_city, 15)),
        pix_tlv("62", pix_tlv("05", "***")),
    )) + "6304"
    return payload + pix_crc16(payload)

def generate_qrcode_base64(payload):
    qr = qrcode.QRCode(version=None, box_size=8, border=3)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = io.BytesIO()
    image.save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")
