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
    signature: str = "Ravasco TDS System",
) -> tuple:
    """
    Build (html_body, text_body) for a formal, consistently-branded email.

    greeting                    — e.g. "Hi Dishant," or "Hello," / "Hi,"
    body_paragraphs             — paragraphs rendered before the highlight box
    highlight_value             — optional value shown in a highlighted box (an OTP code)
    highlight_label             — label above the highlighted value
    after_highlight_paragraphs  — paragraphs rendered after the highlight box
                                   (e.g. "if you didn't request this..." guidance)
    closing                     — closing line before the signature (e.g. "Regards,")
    signature                   — signed name (e.g. "Ravasco TDS System")
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
        highlight_html = f"""
            <div style="background:#FEF3C7;border:2px dashed #D4940A;border-radius:8px;
                        padding:20px;text-align:center;margin:0 0 24px;">
              <p style="margin:0 0 4px;font-size:10px;font-weight:700;letter-spacing:.15em;
                        text-transform:uppercase;color:#C17F0A;">{highlight_label}</p>
              <p style="margin:0;font-family:'Courier New',monospace;font-size:36px;
                        font-weight:900;letter-spacing:.25em;color:#1A2535;">{highlight_value}</p>
            </div>"""

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8" /></head>
<body style="margin:0;padding:0;background:#F0F2F5;font-family:Inter,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 0;">
    <tr><td align="center">
      <table width="480" cellpadding="0" cellspacing="0"
             style="background:#fff;border:1px solid #E2E8F0;border-radius:10px;overflow:hidden;">
        <tr>
          <td style="background:#1A2535;padding:24px 32px;border-bottom:3px solid #D4940A;">
            <p style="margin:0;font-family:Montserrat,Arial,sans-serif;font-size:18px;
                      font-weight:900;letter-spacing:.04em;color:#fff;">
              INDUS <span style="color:#F0B429;">TDS</span> SYSTEM
            </p>
            <p style="margin:4px 0 0;font-size:11px;color:rgba(255,255,255,.6);
                      letter-spacing:.08em;text-transform:uppercase;">
              Ravasco Transmission &amp; Packing
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:32px;">
            <p style="margin:0 0 16px;font-size:14px;color:#1A202C;">{greeting}</p>
            {paragraphs_html}
            {highlight_html}
            {after_highlight_html}
            <p style="margin:16px 0 0;font-size:13px;color:#1A202C;line-height:1.6;">
              {closing}<br />{signature}
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#F7F8FA;padding:16px 32px;border-top:1px solid #E2E8F0;">
            <p style="margin:0;font-size:10px;color:#A0AEC0;text-align:center;">
              ISO 9001:2015 Certified. Internal Use Only.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
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
    text_body = "\n".join(text_lines) + "\n"

    return html_body, text_body
