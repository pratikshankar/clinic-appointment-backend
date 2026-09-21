"""Invoice, receipt and insurance-statement PDFs (Section 19).

Pure rendering: every function takes loaded ORM objects and returns `bytes`.
Nothing here touches the database or the network, which is what makes the output
testable — a test can assert on a real PDF without a running server.

Documents are generated **on demand rather than stored**. A finalised bill is
immutable and a payment is never edited, so regenerating always produces the
same document; keeping copies would only add a cache that can go stale.

reportlab rather than an HTML-to-PDF engine: WeasyPrint needs system cairo and
pango, which turns "deploy the backend" into a support ticket.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.config import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Brand
# --------------------------------------------------------------------------- #
NAVY = colors.HexColor("#123A6E")
BLUE = colors.HexColor("#1C7FC4")
INK = colors.HexColor("#1F2933")
MUTED = colors.HexColor("#6B7280")
RULE = colors.HexColor("#D5DBE3")
ZEBRA = colors.HexColor("#F5F7FA")

PAGE_MARGIN = 16 * mm

#: The logo is fitted into a box rather than set to a fixed width, because
#: brands are not all the same shape. A square mark scaled to 22 mm wide is
#: 22 mm tall; a 2.5:1 wordmark scaled the same way is 9 mm tall and unreadable.
#: Whichever dimension binds first wins, so both read at a comparable size.
LOGO_MAX_WIDTH = 42 * mm
LOGO_MAX_HEIGHT = 22 * mm


#: Accepted logo filenames, in preference order. Deliberately not a single
#: hardcoded name: whoever drops the file in should not have to know that this
#: module wanted `.png` specifically, and a logo silently ignored because it was
#: saved as `.jpg` is a confusing way to lose branding.
LOGO_STEMS = ("paineasy-logo", "logo")
LOGO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")


ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


@dataclass(frozen=True, slots=True)
class Brand:
    """The identity a document is issued under.

    Resolved per document from its clinic, because a document is issued *by a
    clinic* -- one branch of the chain may trade under its own name and file
    under its own registration. Anything the clinic leaves unset falls back to
    the chain, so a normally-branded clinic needs no data at all.
    """

    name: str
    tagline: str
    logo: Path | None
    footer: str


def _chain_logo() -> Path | None:
    configured = settings.LOGO_PATH.strip()
    if configured:
        candidate = Path(configured)
        return candidate if candidate.is_file() else None

    for stem in LOGO_STEMS:
        for suffix in LOGO_SUFFIXES:
            candidate = ASSETS_DIR / f"{stem}{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _logo_path() -> Path | None:
    """The chain logo. Kept for callers that have no clinic in hand."""
    return _chain_logo()


def brand_for(clinic) -> Brand:
    """Brand for a clinic, falling back field by field to the chain."""
    name = (getattr(clinic, "brand_name", None) or "").strip() or settings.BRAND_NAME
    tagline = (
        getattr(clinic, "brand_tagline", None) or ""
    ).strip() or settings.BRAND_TAGLINE
    footer = (
        getattr(clinic, "document_footer", None) or ""
    ).strip() or settings.DOCUMENT_FOOTER.strip()

    logo = None
    filename = (getattr(clinic, "logo_filename", None) or "").strip()
    if filename:
        # Basename only: a stored path must never be able to read outside the
        # assets directory.
        candidate = ASSETS_DIR / Path(filename).name
        logo = candidate if candidate.is_file() else None
        if logo is None:
            logger.warning(
                "Clinic logo %r not found in %s; falling back to the chain logo",
                filename,
                ASSETS_DIR,
            )
    return Brand(name, tagline, logo or _chain_logo(), footer)


#: Pixel width the cached logo is downscaled to. The widest a logo is drawn is
#: 42 mm, about 500 px at 300 dpi, so 600 keeps it crisp while still cutting a
#: 1000 px source to a fraction of its size.
LOGO_RENDER_PX = 600


@lru_cache(maxsize=4)
def _logo_bytes(path: str, mtime: float) -> bytes | None:
    """A downscaled copy of the logo, cached in memory.

    A 1000x1000 source embedded verbatim added ~107 KB to every document, for
    something printed 22 mm wide. Since these are emailed and sent over
    WhatsApp, that is bandwidth spent on pixels nobody can see. `mtime` is part
    of the cache key so replacing the file takes effect without a restart.
    """
    del mtime  # cache key only
    try:
        with PILImage.open(path) as image:
            if image.width <= LOGO_RENDER_PX:
                return Path(path).read_bytes()
            ratio = LOGO_RENDER_PX / image.width
            resized = image.convert("RGB").resize(
                (LOGO_RENDER_PX, max(1, round(image.height * ratio))),
                PILImage.LANCZOS,
            )
            buffer = BytesIO()
            resized.save(buffer, format="JPEG", quality=88, optimize=True)
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a bad image must not block the invoice
        logger.warning("Logo at %s could not be processed; using the wordmark", path)
        return None


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Heading1"], fontSize=15, leading=18,
            textColor=NAVY, spaceAfter=0,
        ),
        "docmeta": ParagraphStyle(
            "docmeta", parent=base["Normal"], fontSize=9, leading=12,
            textColor=MUTED, alignment=TA_RIGHT,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=9, leading=12, textColor=BLUE,
            spaceAfter=3, fontName="Helvetica-Bold",
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontSize=9.5, leading=13, textColor=INK
        ),
        "small": ParagraphStyle(
            "small", parent=base["Normal"], fontSize=8, leading=11, textColor=MUTED
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["Normal"], fontSize=9, leading=12, textColor=INK
        ),
        "cellright": ParagraphStyle(
            "cellright", parent=base["Normal"], fontSize=9, leading=12,
            textColor=INK, alignment=TA_RIGHT,
        ),
        "provisional": ParagraphStyle(
            "provisional", parent=base["Normal"], fontSize=10, leading=13,
            textColor=colors.HexColor("#92400E"), alignment=TA_CENTER,
            fontName="Helvetica-Bold",
        ),
        "footer": ParagraphStyle(
            "footer", parent=base["Normal"], fontSize=7.5, leading=10,
            textColor=MUTED, alignment=TA_CENTER,
        ),
    }


def money(value) -> str:
    """Rupees with two decimals and thousands separators, Indian grouping aside.

    Deliberately not `Intl`-style lakh grouping: a document that may be read by
    an insurer abroad is safer with plain grouping, and the figure is the same.
    """
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"Rs. {amount:,.2f}"


def _date(value: date | datetime | None) -> str:
    return f"{value:%d-%b-%Y}" if value else "-"


#: `.title()` would render UPI as "Upi", which looks like a typo on a document a
#: patient keeps. Spelled out explicitly instead.
PAYMENT_METHOD_LABELS = {
    "CASH": "Cash",
    "UPI": "UPI",
    "CARD": "Card",
    "BANK_TRANSFER": "Bank transfer",
    "OTHER": "Other",
}


def payment_method_label(method) -> str:
    raw = getattr(method, "value", str(method))
    return PAYMENT_METHOD_LABELS.get(raw, raw.replace("_", " ").title())


# --------------------------------------------------------------------------- #
# Shared blocks
# --------------------------------------------------------------------------- #
def _brand_header(
    styles, title: str, meta_rows: list[tuple[str, str]], brand_info: Brand
) -> Table:
    """Logo and brand on the left, document title and reference on the right."""
    logo = brand_info.logo
    brand = None
    if logo is not None:
        data = _logo_bytes(str(logo), logo.stat().st_mtime)
        if data is not None:
            try:
                reader = ImageReader(BytesIO(data))
                width, height = reader.getSize()
                scale = min(LOGO_MAX_WIDTH / width, LOGO_MAX_HEIGHT / height)
                brand = Image(
                    BytesIO(data), width=width * scale, height=height * scale
                )
            except Exception:  # noqa: BLE001 - a bad image must not block the invoice
                logger.warning("Logo at %s could not be read; using the wordmark", logo)
                brand = None

    if brand is None:
        # Text wordmark in the brand's own two colours, so a missing file looks
        # deliberate rather than broken.
        # Split roughly in half so a two-word brand picks up both colours.
        split = len(brand_info.name) // 2 or len(brand_info.name)
        brand = Paragraph(
            f'<font color="#123A6E"><b>{brand_info.name[:split].upper()}</b></font>'
            f'<font color="#1C7FC4"><b>{brand_info.name[split:].upper()}</b></font>',
            ParagraphStyle("wordmark", fontSize=17, leading=20),
        )

    left = [
        brand,
        Spacer(1, 2 * mm),
        Paragraph(f"<b>{brand_info.name}</b>", styles["body"]),
        Paragraph(brand_info.tagline, styles["small"]),
    ]
    right = [Paragraph(title, styles["title"])]
    right += [
        Paragraph(f"{label}&nbsp;&nbsp;<b>{value}</b>", styles["docmeta"])
        for label, value in meta_rows
    ]

    table = Table([[left, right]], colWidths=[95 * mm, 68 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("LINEBELOW", (0, 0), (-1, -1), 1.2, BLUE),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _two_column(styles, left_title, left_lines, right_title, right_lines) -> Table:
    def block(title, lines):
        out = [Paragraph(title.upper(), styles["h2"])]
        out += [Paragraph(line, styles["body"]) for line in lines if line]
        return out

    table = Table(
        [[block(left_title, left_lines), block(right_title, right_lines)]],
        colWidths=[82 * mm, 81 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (-1, 0), (-1, 0), 0),
            ]
        )
    )
    return table


def _clinic_lines(clinic) -> list[str]:
    if clinic is None:
        return ["-"]
    # `address` and `location` are the actual column names. This previously
    # read `address_line1`/`address_line2`, which exist on no model, so every
    # invoice quietly omitted the street address -- fine to overlook on screen,
    # not fine on a tax document.
    parts = [
        f"<b>{clinic.name}</b>",
        getattr(clinic, "address", None),
        getattr(clinic, "location", None),
        ", ".join(
            str(bit)
            for bit in (
                getattr(clinic, "city", None),
                getattr(clinic, "state", None),
                getattr(clinic, "pin_code", None),
            )
            if bit
        ),
        f"Phone: {clinic.phone}" if getattr(clinic, "phone", None) else None,
        f"Email: {clinic.email}" if getattr(clinic, "email", None) else None,
    ]
    return [part for part in parts if part]


def _patient_lines(patient) -> list[str]:
    if patient is None:
        return ["-"]
    detail = ", ".join(
        str(bit)
        for bit in (
            f"{patient.age} yrs" if getattr(patient, "age", None) else None,
            patient.gender.value.title() if getattr(patient, "gender", None) else None,
        )
        if bit
    )
    return [
        f"<b>{patient.full_name}</b>",
        f"Patient ID: {patient.patient_code}",
        detail or None,
        f"Mobile: {patient.mobile}" if getattr(patient, "mobile", None) else None,
    ]


def _table_style(money_columns: tuple[int, ...], rows: int) -> TableStyle:
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("GRID", (0, 0), (-1, -1), 0.4, RULE),
    ]
    for column in money_columns:
        commands.append(("ALIGN", (column, 0), (column, -1), "RIGHT"))
    # Zebra striping: with a dozen session dates in a column, unstriped rows are
    # genuinely hard to read across.
    for index in range(1, rows):
        if index % 2 == 0:
            commands.append(("BACKGROUND", (0, index), (-1, index), ZEBRA))
    return TableStyle(commands)


def _totals_block(styles, rows: list[tuple[str, str, bool]]) -> Table:
    data = []
    for label, value, emphasise in rows:
        style = "cell" if not emphasise else "cellright"
        data.append(
            [
                Paragraph(f"<b>{label}</b>" if emphasise else label, styles["cellright"]),
                Paragraph(f"<b>{value}</b>" if emphasise else value, styles["cellright"]),
            ]
        )
        del style

    table = Table(data, colWidths=[110 * mm, 53 * mm], hAlign="RIGHT")
    commands = [
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]
    for index, (_, _, emphasise) in enumerate(rows):
        if emphasise:
            commands += [
                ("LINEABOVE", (0, index), (-1, index), 0.8, NAVY),
                ("BACKGROUND", (0, index), (-1, index), ZEBRA),
            ]
    table.setStyle(TableStyle(commands))
    return table


def _build(story: list, title: str, subject: str, brand: Brand | None = None) -> bytes:
    brand = brand or Brand(settings.BRAND_NAME, settings.BRAND_TAGLINE, _chain_logo(), "")
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=title,
        subject=subject,
        author=brand.name,
    )

    styles = _styles()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(
            A4[0] / 2,
            10 * mm,
            f"{brand.name}  ·  computer-generated document  ·  page {doc.page}",
        )
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    del styles
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Invoice: one per bill
# --------------------------------------------------------------------------- #
def render_invoice(bill) -> bytes:
    styles = _styles()
    brand = brand_for(bill.clinic)
    story: list = [
        _brand_header(
            styles,
            "INVOICE",
            [
                ("Invoice no.", bill.bill_number),
                ("Date", _date(bill.bill_date)),
                ("Status", bill.payment_status.value.title()),
            ],
            brand,
        ),
        Spacer(1, 6 * mm),
        _two_column(
            styles,
            "Clinic",
            _clinic_lines(bill.clinic),
            "Patient",
            _patient_lines(bill.patient),
        ),
        Spacer(1, 6 * mm),
    ]

    header = ["#", "Description", "Qty", "Unit price", "Amount"]
    data = [header]
    for index, item in enumerate(bill.items, start=1):
        data.append(
            [
                Paragraph(str(index), styles["cell"]),
                Paragraph(item.description, styles["cell"]),
                Paragraph(str(item.quantity), styles["cellright"]),
                Paragraph(money(item.unit_price), styles["cellright"]),
                Paragraph(money(item.amount), styles["cellright"]),
            ]
        )
    table = Table(data, colWidths=[10 * mm, 83 * mm, 14 * mm, 28 * mm, 28 * mm], repeatRows=1)
    table.setStyle(_table_style((2, 3, 4), len(data)))
    story.append(table)
    story.append(Spacer(1, 4 * mm))

    totals = [("Subtotal", money(bill.subtotal_amount), False)]
    if bill.discount_amount and bill.discount_amount > 0:
        totals.append(("Discount", f"- {money(bill.discount_amount)}", False))
    if bill.tax_amount and bill.tax_amount > 0:
        totals.append(("Tax", money(bill.tax_amount), False))
    totals.append(("Total", money(bill.total_amount), True))
    totals.append(("Paid", money(bill.amount_paid), False))
    totals.append(("Balance due", money(bill.balance_amount), True))
    story.append(_totals_block(styles, totals))

    if bill.payments:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph("PAYMENTS RECEIVED", styles["h2"]))
        pay_data = [["Date", "Method", "Reference", "Amount"]]
        for payment in bill.payments:
            pay_data.append(
                [
                    Paragraph(_date(payment.payment_date), styles["cell"]),
                    Paragraph(payment_method_label(payment.payment_method), styles["cell"]),
                    Paragraph(payment.reference_number or "-", styles["cell"]),
                    Paragraph(money(payment.amount), styles["cellright"]),
                ]
            )
        pay_table = Table(pay_data, colWidths=[28 * mm, 34 * mm, 73 * mm, 28 * mm], repeatRows=1)
        pay_table.setStyle(_table_style((3,), len(pay_data)))
        story.append(pay_table)

    if bill.notes:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("NOTES", styles["h2"]))
        story.append(Paragraph(bill.notes, styles["body"]))

    if brand.footer:
        story.append(Spacer(1, 6 * mm))
        story.append(Paragraph(brand.footer, styles["small"]))

    return _build(story, f"Invoice {bill.bill_number}", "Invoice", brand)


# --------------------------------------------------------------------------- #
# Receipt: one per payment
# --------------------------------------------------------------------------- #
def receipt_number(payment, bill=None) -> str:
    """A stable receipt reference derived from the payment's own id.

    Derived rather than stored so no migration or sequence table is needed, and
    deterministic so regenerating a receipt months later reproduces the same
    number the patient was originally handed.

    A clinic with its own invoice series gets its receipts prefixed to match
    (``PC-RCP-2026-000042``), so a receipt is visibly that entity's. The
    statutory requirement is on the *invoice* series, which is why the receipt
    number is derived rather than sequenced -- but a Physiocare receipt should
    still not look like a PainEasy one.
    """
    year = (payment.payment_date or date.today()).year
    series = ""
    clinic = getattr(bill, "clinic", None) if bill is not None else None
    prefix = (getattr(clinic, "bill_number_prefix", None) or "").strip().upper()
    if prefix and prefix != "INV":
        series = f"{prefix}-"
    return f"{series}RCP-{year}-{payment.id:06d}"


def render_receipt(payment, bill) -> bytes:
    styles = _styles()
    brand = brand_for(bill.clinic)
    paid_after = sum(
        (item.amount for item in bill.payments if item.id <= payment.id), Decimal("0.00")
    )
    outstanding = max(bill.total_amount - paid_after, Decimal("0.00"))

    story: list = [
        _brand_header(
            styles,
            "PAYMENT RECEIPT",
            [
                ("Receipt no.", receipt_number(payment, bill)),
                ("Date", _date(payment.payment_date)),
                ("Against invoice", bill.bill_number),
            ],
            brand,
        ),
        Spacer(1, 6 * mm),
        _two_column(
            styles,
            "Clinic",
            _clinic_lines(bill.clinic),
            "Received from",
            _patient_lines(bill.patient),
        ),
        Spacer(1, 7 * mm),
    ]

    # The amount is the point of the document, so it gets its own banner rather
    # than being a row someone has to hunt for.
    amount_banner = Table(
        [
            [
                Paragraph("AMOUNT RECEIVED", styles["h2"]),
                Paragraph(
                    f'<font size="17" color="#123A6E"><b>{money(payment.amount)}</b></font>',
                    styles["cellright"],
                ),
            ]
        ],
        colWidths=[80 * mm, 83 * mm],
    )
    amount_banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ZEBRA),
                ("BOX", (0, 0), (-1, -1), 0.8, BLUE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(amount_banner)
    story.append(Spacer(1, 5 * mm))

    detail = [
        ["Field", "Detail"],
        [
            Paragraph("Payment method", styles["cell"]),
            Paragraph(payment_method_label(payment.payment_method), styles["cell"]),
        ],
        [
            Paragraph("Transaction reference", styles["cell"]),
            Paragraph(payment.reference_number or "-", styles["cell"]),
        ],
        [
            Paragraph("Received by", styles["cell"]),
            Paragraph(
                payment.received_by.full_name if payment.received_by else "-", styles["cell"]
            ),
        ],
        [
            Paragraph("Invoice total", styles["cell"]),
            Paragraph(money(bill.total_amount), styles["cell"]),
        ],
        [
            Paragraph("Balance after this payment", styles["cell"]),
            Paragraph(money(outstanding), styles["cell"]),
        ],
    ]
    detail_table = Table(detail, colWidths=[60 * mm, 103 * mm], repeatRows=1)
    detail_table.setStyle(_table_style((), len(detail)))
    story.append(detail_table)

    if payment.notes:
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("NOTES", styles["h2"]))
        story.append(Paragraph(payment.notes, styles["body"]))

    story.append(Spacer(1, 10 * mm))
    story.append(
        Paragraph(
            "This receipt acknowledges the amount stated above only. "
            "Retain it for your records.",
            styles["small"],
        )
    )

    if brand.footer:
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph(brand.footer, styles["small"]))

    return _build(
        story, f"Receipt {receipt_number(payment, bill)}", "Payment receipt", brand
    )




def _therapist_label(therapist) -> str:
    """Name plus registration number, when the practitioner has one.

    An insurer checking a claim wants to see that treatment was given by a
    registered practitioner, so the number belongs next to the name rather than
    somewhere they have to go looking for it.
    """
    if therapist is None:
        return "-"
    number = (getattr(therapist, "registration_number", None) or "").strip()
    if not number:
        return therapist.full_name
    return f"{therapist.full_name}<br/><font size=\"7\">Reg. {number}</font>"


def _signatory_label(sessions) -> str:
    """Who signs the statement: the physiotherapist who actually treated them.

    Taken from the delivered sessions rather than left as a generic label, so
    the signature line names a person an insurer can check.
    """
    for item in sessions:
        therapist = getattr(item, "therapist", None)
        if therapist is not None:
            number = (getattr(therapist, "registration_number", None) or "").strip()
            suffix = f" · Reg. {number}" if number else ""
            return f"{therapist.full_name}{suffix}"
    return "Treating physiotherapist"


# --------------------------------------------------------------------------- #
# Session statement: for an insurance claim
# --------------------------------------------------------------------------- #
def render_session_statement(package, sessions, bills) -> bytes:
    """Session-by-session treatment record with the money paid against it.

    This is the document an insurer reads, so it answers their questions in
    order: who was treated, for what, on which dates, by whom, and what was
    paid. Marked PROVISIONAL while sessions remain, so a mid-course copy cannot
    be mistaken for a final claim.
    """
    styles = _styles()
    brand = brand_for(package.clinic)
    delivered = [item for item in sessions if not item.is_voided]
    is_final = package.sessions_taken >= package.sessions_registered
    patient = package.patient

    story: list = [
        _brand_header(
            styles,
            "TREATMENT STATEMENT",
            [
                ("Statement no.", f"STM-{package.id:06d}"),
                ("Issued", _date(date.today())),
                ("Status", "Final" if is_final else "Provisional"),
            ],
            brand,
        ),
        Spacer(1, 5 * mm),
    ]

    if not is_final:
        banner = Table(
            [
                [
                    Paragraph(
                        "PROVISIONAL &mdash; this course of treatment is still in progress "
                        f"({package.sessions_taken} of {package.sessions_registered} sessions "
                        "delivered). A final statement can be issued once it is complete.",
                        styles["provisional"],
                    )
                ]
            ],
            colWidths=[163 * mm],
        )
        banner.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7")),
                    ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#D97706")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story += [banner, Spacer(1, 5 * mm)]

    story.append(
        _two_column(
            styles,
            "Clinic",
            _clinic_lines(package.clinic),
            "Patient",
            _patient_lines(patient),
        )
    )
    story.append(Spacer(1, 5 * mm))

    complaint = getattr(patient, "chief_complaint", None) if patient else None
    story.append(
        _two_column(
            styles,
            "Treatment",
            [
                f"<b>{package.package_name or 'Physiotherapy package'}</b>",
                f"Sessions registered: {package.sessions_registered}",
                f"Sessions delivered: {package.sessions_taken}",
                f"Commenced: {_date(package.start_date)}",
            ],
            "Presenting complaint",
            [complaint or "Not recorded"],
        )
    )
    story.append(Spacer(1, 6 * mm))

    story.append(Paragraph("SESSIONS DELIVERED", styles["h2"]))
    data = [["#", "Date", "Therapist", "Treatment provided"]]
    for item in delivered:
        data.append(
            [
                Paragraph(str(item.session_number), styles["cell"]),
                Paragraph(_date(item.session_date), styles["cell"]),
                Paragraph(_therapist_label(item.therapist), styles["cell"]),
                Paragraph(item.treatment_provided or "Physiotherapy session", styles["cell"]),
            ]
        )
    if len(data) == 1:
        data.append(
            [
                Paragraph("-", styles["cell"]),
                Paragraph("-", styles["cell"]),
                Paragraph("-", styles["cell"]),
                Paragraph("No sessions delivered yet", styles["cell"]),
            ]
        )
    session_table = Table(
        data, colWidths=[10 * mm, 26 * mm, 42 * mm, 85 * mm], repeatRows=1
    )
    session_table.setStyle(_table_style((), len(data)))
    story.append(session_table)
    story.append(Spacer(1, 6 * mm))

    billed = sum((bill.total_amount for bill in bills), Decimal("0.00"))
    paid = sum((bill.amount_paid for bill in bills), Decimal("0.00"))

    story.append(Paragraph("AMOUNTS BILLED AND PAID", styles["h2"]))
    money_data = [["Invoice", "Date", "Total", "Paid", "Balance"]]
    for bill in bills:
        money_data.append(
            [
                Paragraph(bill.bill_number, styles["cell"]),
                Paragraph(_date(bill.bill_date), styles["cell"]),
                Paragraph(money(bill.total_amount), styles["cellright"]),
                Paragraph(money(bill.amount_paid), styles["cellright"]),
                Paragraph(money(bill.balance_amount), styles["cellright"]),
            ]
        )
    if len(money_data) == 1:
        money_data.append(
            [Paragraph("No invoices raised", styles["cell"]), "", "", "", ""]
        )
    money_table = Table(
        money_data, colWidths=[40 * mm, 27 * mm, 32 * mm, 32 * mm, 32 * mm], repeatRows=1
    )
    money_table.setStyle(_table_style((2, 3, 4), len(money_data)))
    story.append(money_table)
    story.append(Spacer(1, 4 * mm))
    story.append(
        _totals_block(
            styles,
            [
                ("Total billed", money(billed), False),
                ("Total paid", money(paid), True),
                ("Outstanding", money(max(billed - paid, Decimal("0.00"))), False),
            ],
        )
    )

    # Signature block: an unsigned statement is the usual reason a claim comes
    # back, so the space for it is part of the document rather than an
    # afterthought someone has to draw on by hand.
    story.append(Spacer(1, 14 * mm))
    signature = Table(
        [
            [
                Paragraph(_signatory_label(delivered), styles["small"]),
                Paragraph("For " + brand.name, styles["small"]),
            ],
            [Spacer(1, 12 * mm), Spacer(1, 12 * mm)],
            [
                Paragraph("Signature &amp; date", styles["small"]),
                Paragraph("Signature &amp; clinic stamp", styles["small"]),
            ],
        ],
        colWidths=[81 * mm, 82 * mm],
    )
    signature.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 2), (-1, 2), 0.6, RULE),
                ("TOPPADDING", (0, 2), (-1, 2), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(KeepTogether(signature))

    footer_text = brand.footer or (
        "Issued for the patient's records and for submission to an insurer. "
        "Figures reflect amounts recorded in the clinic's billing system on the date of issue."
    )
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(footer_text, styles["small"]))

    return _build(
        story,
        f"Treatment statement {package.id}",
        "Treatment and payment statement",
        brand,
    )
