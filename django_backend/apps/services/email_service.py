"""
apps/services/email_service.py — Shared content builder for every outgoing
email in the app (password-reset OTP, device-login OTP, new-device
notifications, admin alerts, the daily report).

This module only builds (html_body, text_body). It never calls send_mail()
itself and knows nothing about SMTP configuration, fail_silently policy, or
per-caller dev-mode console fallbacks — each sender in otp_service.py /
device_service.py / tds_report_service.py keeps its own send_mail() call and
its own error handling exactly as before; only the content construction was
centralized here so every email shares one consistent, formal layout.
"""


def render_email(
    greeting: str,
    body_paragraphs: list,
    highlight_value: str = None,
    highlight_label: str = "One-Time Password",
    after_highlight_paragraphs: list = None,
    closing: str = "Regards,",
    signature: str = "Ravasco Transmission and Packing Pvt Ltd.",
) -> tuple:
    """
    Build (html_body, text_body) for a formal, consistently-branded email.

    greeting                    - e.g. "Hi Dishant," or "Hello," / "Hi,"
    body_paragraphs             - paragraphs rendered before the highlight box
    highlight_value             - optional value shown in a highlighted box (an OTP code)
    highlight_label             - label above the highlighted value
    after_highlight_paragraphs  - paragraphs rendered after the highlight box
                                   (e.g. "if you didn't request this..." guidance)
    closing                     - closing line before the signature (e.g. "Regards,")
    signature                   - signed name (e.g. "Ravasco Transmission and Packing Pvt Ltd.")
    """
    after_highlight_paragraphs = after_highlight_paragraphs or []

    def _paragraphs_html(paragraphs, small=False):
        style = (
            'margin:0 0 12px;font-size:12px;color:#718096;line-height:1.5;' if small
            else 'margin:0 0 16px;font-size:13px;color:#4A5568;line-height:1.6;'
        )
        return "".join(f'<p style="{style}">{p}</p>' for p in paragraphs)

    paragraphs_html = _paragraphs_html(body_paragraphs)
    after_highlight_html = _paragraphs_html(after_highlight_paragraphs, small=True)

    highlight_html = ""
    if highlight_value:
        highlight_html = (
            f'<p style="margin:0 0 20px;font-size:13px;color:#1A202C;">'
            f'{highlight_label}: '
            f'<span style="font-weight:700;letter-spacing:.05em;">{highlight_value}</span></p>'
        )

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:16px;background:#FFFFFF;font-family:Arial,Helvetica,sans-serif;">
  <p style="margin:0 0 20px;font-size:13px;color:#1A202C;">{greeting}</p>
  {paragraphs_html}
  {highlight_html}
  {after_highlight_html}
  <p style="margin:20px 0 0;font-size:13px;color:#1A202C;line-height:1.6;">
    {closing}<br />{signature}
  </p>
  <p style="margin:24px 0 0;font-size:11px;color:#718096;">
    This is a system generated email. Please do not reply.
  </p>
</body>
</html>"""

    text_lines = [greeting, ""]
    text_lines.extend(p for p in body_paragraphs)
    if highlight_value:
        text_lines.append("")
        text_lines.append(f"{highlight_label}: {highlight_value}")
    if after_highlight_paragraphs:
        text_lines.append("")
        text_lines.extend(p for p in after_highlight_paragraphs)
    text_lines.append("")
    text_lines.append(closing)
    text_lines.append(signature)
    text_lines.append("")
    text_lines.append("This is a system generated email. Please do not reply.")
    text_body = "\n".join(text_lines) + "\n"

    return html_body, text_body
