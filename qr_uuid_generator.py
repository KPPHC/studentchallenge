import uuid
import qrcode
from PIL import Image

# Generate a UUID
unique_id = str(uuid.uuid4())
print(f"Generated UUID: {unique_id}")

# Create QR code instance
qr = qrcode.QRCode(
    version=1,
    error_correction=qrcode.constants.ERROR_CORRECT_L,
    box_size=10,
    border=4,
)

# Add UUID data to QR code
qr.add_data(unique_id)
qr.make(fit=True)

# Create an image from the QR code
img = qr.make_image(fill_color="black", back_color="white")

# Save the image as JPG
img.save("qr_code.jpg")
print("QR code saved as 'qr_code.jpg'")