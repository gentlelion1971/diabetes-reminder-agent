import os
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))
EMAIL_USER = os.environ["EMAIL_USER"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
PARENT_EMAIL = os.environ["PARENT_EMAIL"]

msg = EmailMessage()
msg["From"] = EMAIL_USER
msg["To"] = PARENT_EMAIL
msg["Subject"] = "Julie diabetes agent test"
msg.set_content("This is a test email from Julie diabetes agent.")

if EMAIL_PORT == 465:
    with smtplib.SMTP_SSL(EMAIL_HOST, EMAIL_PORT, timeout=30) as smtp:
        smtp.login(EMAIL_USER, EMAIL_APP_PASSWORD)
        smtp.send_message(msg)
else:
    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(EMAIL_USER, EMAIL_APP_PASSWORD)
        smtp.send_message(msg)

print("sent")
