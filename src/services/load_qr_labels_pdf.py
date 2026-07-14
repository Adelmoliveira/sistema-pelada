from io import BytesIO

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


LABEL_SIZES = {
    "small": {"label": (48 * mm, 30 * mm), "columns": 4, "rows": 9},
    "standard": {"label": (65 * mm, 42 * mm), "columns": 3, "rows": 6},
    "large": {"label": (95 * mm, 62 * mm), "columns": 2, "rows": 4},
}


def _qr_image(payload):
    qr = qrcode.QRCode(
        version=None, box_size=7, border=2,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    output = BytesIO()
    image.save(output, "PNG")
    output.seek(0)
    return ImageReader(output)


def _fitted_text(value, width, font_name, font_size):
    value = " ".join(str(value or "").split())
    if stringWidth(value, font_name, font_size) <= width:
        return value
    suffix = "..."
    while value and stringWidth(value.rstrip() + suffix, font_name, font_size) > width:
        value = value[:-1]
    return value.rstrip() + suffix


def build_load_qr_labels_pdf(entries, base_url, size="standard"):
    config = LABEL_SIZES.get(size, LABEL_SIZES["standard"])
    label_width, label_height = config["label"]
    columns, rows = config["columns"], config["rows"]
    page_width, page_height = A4
    margin_x = (page_width - columns * label_width) / 2
    margin_y = (page_height - rows * label_height) / 2
    per_page = columns * rows

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    pdf.setTitle("Etiquetas QR - Relação de Carga")
    pdf.setAuthor("PELADEIROS GPCTA")

    for index, entry in enumerate(entries):
        position = index % per_page
        if index and position == 0:
            pdf.showPage()
        column, row = position % columns, position // columns
        x = margin_x + column * label_width
        y = page_height - margin_y - (row + 1) * label_height

        pdf.setStrokeColor(colors.HexColor("#AAB7BF"))
        pdf.setLineWidth(0.35)
        pdf.roundRect(
            x + 1.2 * mm, y + 1.2 * mm,
            label_width - 2.4 * mm, label_height - 2.4 * mm, 2 * mm,
        )

        qr_size = min(label_height - 7 * mm, label_width * 0.48)
        qr_x = x + 3.5 * mm
        qr_y = y + (label_height - qr_size) / 2
        detail_url = f"{base_url.rstrip('/')}/infra/load-relation/{entry['id']}"
        pdf.drawImage(
            _qr_image(detail_url), qr_x, qr_y, qr_size, qr_size,
            preserveAspectRatio=True, mask="auto",
        )

        text_x = qr_x + qr_size + 3 * mm
        text_width = x + label_width - 3.5 * mm - text_x
        pdf.setFillColor(colors.HexColor("#073B5C"))
        code_font = 6.6 if size == "small" else (8.2 if size == "standard" else 10)
        pdf.setFont("Helvetica-Bold", code_font)
        code, area = entry["bmp"].split(" | ", 1)
        pdf.drawString(text_x, y + label_height - 8 * mm, code)
        pdf.setFont("Helvetica-Bold", code_font + 1)
        pdf.drawString(text_x, y + label_height - 13 * mm, area)

        if size != "small":
            pdf.setFillColor(colors.HexColor("#253D4A"))
            description_size = 7 if size == "standard" else 8.5
            pdf.setFont("Helvetica", description_size)
            pdf.drawString(
                text_x, y + label_height - 20 * mm,
                _fitted_text(entry["material_description"], text_width, "Helvetica", description_size),
            )
            if entry["location"]:
                location_size = 6.5 if size == "standard" else 8
                pdf.setFont("Helvetica", location_size)
                pdf.drawString(
                    text_x, y + 5 * mm,
                    _fitted_text(f"Local: {entry['location']}", text_width, "Helvetica", location_size),
                )

        pdf.setFillColor(colors.HexColor("#6C757D"))
        pdf.setFont("Helvetica", 5.5 if size == "small" else 6.5)
        pdf.drawRightString(x + label_width - 3.5 * mm, y + 2.8 * mm, "GPCTA")

    if not entries:
        pdf.setFont("Helvetica", 12)
        pdf.drawCentredString(page_width / 2, page_height / 2, "Nenhum BMP selecionado.")
    pdf.save()
    output.seek(0)
    return output
