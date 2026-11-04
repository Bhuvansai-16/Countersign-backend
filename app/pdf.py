"""Evidence pack PDF: cover with verify QR, receipt timeline, evidence, response, cited PayPal policy."""

from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

STYLES = getSampleStyleSheet()


def _p(text: str, style: str = "BodyText") -> Paragraph:
    return Paragraph(escape(str(text)), STYLES[style])


def _qr(url: str, size: float = 1.6 * inch) -> Drawing:
    widget = QrCodeWidget(url)
    x1, y1, x2, y2 = widget.getBounds()
    d = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    d.add(widget)
    return d


def _table(rows: list[list], widths: list[float]) -> Table:
    t = Table([[_p(c) for c in row] for row in rows], colWidths=widths)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def build_evidence_pdf(case: dict, draft, evidence: list[dict], receipt_view: dict | None, verify_url: str,
                       cited_policy: list[dict], compress: bool = True) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, title=f"Dispute evidence {case['dispute_id']}",
                            pageCompression=1 if compress else 0, invariant=1)
    tri = case.get("triage") or {}
    story = [
        _p(f"Dispute evidence: {case['dispute_id']}", "Title"),
        _table([["Merchant", case.get("merchant_name", "")],
                ["Dispute", case["dispute_id"]],
                ["Reason", tri.get("reason", "")],
                ["Amount", f"{tri.get('amount', '')} {tri.get('currency', '')}"],
                ["Prepared with", "Countersign (seller-approved before submission)"]], [1.6 * inch, 4.6 * inch]),
        Spacer(1, 12),
    ]
    if receipt_view:
        v, rec = receipt_view["verification"], receipt_view["receipt"]
        story += [
            _qr(verify_url), _p(f"Verify this receipt independently: {verify_url}"), Spacer(1, 8),
            _p("Intent receipt", "Heading2"),
            _table([["Receipt", rec["id"]], ["Seal hash", rec.get("seal_hash") or "-"],
                    ["PayPal order custom_id", receipt_view.get("paypal_custom_id") or "-"],
                    ["Merchant signature valid", "yes" if v.get("sig_ok") else "no"],
                    ["Hash chain intact", "yes" if v.get("chain_ok") else "no"],
                    ["custom_id matches seal", "yes" if v.get("custom_id_match") else "no"],
                    ["Strength", v.get("strength", "")]], [2.0 * inch, 4.2 * inch]),
            Spacer(1, 6),
            _table([["#", "Event", "Time (UTC)", "Hash"]] +
                   [[e["seq"], e["type"], e["at"], e["hash"][:16] + "..."] for e in receipt_view["events"]],
                   [0.4 * inch, 2.2 * inch, 1.8 * inch, 1.8 * inch]),
        ]
    story += [_p("Evidence", "Heading2"),
              _table([["Ref", "Kind", "Summary"]] + [[e["id"], e["kind"], e["summary"]] for e in evidence],
                     [0.5 * inch, 1.6 * inch, 4.1 * inch]),
              _p("Seller response", "Heading2"), _p(draft.note)]
    for s in draft.sections:
        story += [_p(s.heading, "Heading3"), _p(s.text), _p("Refs: " + ", ".join(s.refs), "Italic")]
    if cited_policy:
        story.append(_p("PayPal policy cited", "Heading2"))
        story += [_p(f"[{c['id']}] {c['text']} Source: {c['source']}") for c in cited_policy]
    doc.build(story)
    return buf.getvalue()
