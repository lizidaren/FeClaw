"""
QR code generation utility.
Generates QR code images as base64 data URLs (no external API calls).
"""

import base64
import io

try:
    import qrcode
    from PIL import Image

    def generate_qr_data_url(data: str, size: int = 200) -> str:
        """生成 QR code 图片的 data URL（data:image/png;base64,...）"""
        qr = qrcode.QRCode(
            version=None,  # auto-detect
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        img = img.resize((size, size), Image.NEAREST)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"

except ImportError:
    # Fallback: SVG text-based QR (small codes only, for emergency)
    import logging
    logger = logging.getLogger(__name__)

    def generate_qr_data_url(data: str, size: int = 200) -> str:
        logger.warning("qrcode[pil] not installed, using text fallback")
        # Return a placeholder - qrcode[pil] is a required dependency
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">
  <rect width="{size}" height="{size}" fill="white"/>
  <text x="{size//2}" y="{size//2}" text-anchor="middle" fill="red" font-size="14">Install qrcode[pil]</text>
</svg>'''
        b64 = base64.b64encode(svg.encode()).decode()
        return f"data:image/svg+xml;base64,{b64}"
