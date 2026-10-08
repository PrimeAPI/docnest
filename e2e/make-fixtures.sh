#!/bin/sh
# Generates synthetic test PDFs (image-only scans) using the backend dev image.
set -eu
cd "$(dirname "$0")/.."
mkdir -p e2e/fixtures
docker compose -f deploy/compose.dev.yml run --rm --no-deps -v "$PWD/e2e/fixtures:/out" dev python -c "
import io
from PIL import Image
from tests.pdfs import page_image, scanned_pdf, INSURANCE_LINES, INVOICE_LINES, payslip_lines
open('/out/insurance-scan.pdf','wb').write(scanned_pdf(INSURANCE_LINES))
open('/out/invoice-scan.pdf','wb').write(scanned_pdf(INVOICE_LINES))
for n,(name,m) in enumerate([('Januar',1),('Februar',2),('März',3)]):
    open(f'/out/payslip-{m:02d}.pdf','wb').write(scanned_pdf(payslip_lines(name, m)))
# a crooked page that the feeder scanned too long (grey backing below the sheet) + an empty backside
page = Image.open(io.BytesIO(page_image(INVOICE_LINES + ['Kundenservice Musterstadt'] * 12, dpi=150)))
crooked = page.rotate(2.5, expand=True, fillcolor=90)
scan = Image.new('L', (crooked.width, crooked.height + 300), 90)
scan.paste(crooked, (0, 0))
scan.save('/out/crooked-page.png', dpi=(150, 150))
Image.new('L', scan.size, 248).save('/out/blank-page.png', dpi=(150, 150))
print('fixtures written')
"
