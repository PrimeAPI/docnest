#!/bin/sh
# Generates synthetic test PDFs (image-only scans) using the backend dev image.
set -eu
cd "$(dirname "$0")/.."
mkdir -p e2e/fixtures
docker compose -f deploy/compose.dev.yml run --rm --no-deps -v "$PWD/e2e/fixtures:/out" dev python -c "
from tests.pdfs import scanned_pdf, INSURANCE_LINES, INVOICE_LINES, payslip_lines
open('/out/insurance-scan.pdf','wb').write(scanned_pdf(INSURANCE_LINES))
open('/out/invoice-scan.pdf','wb').write(scanned_pdf(INVOICE_LINES))
for n,(name,m) in enumerate([('Januar',1),('Februar',2),('März',3)]):
    open(f'/out/payslip-{m:02d}.pdf','wb').write(scanned_pdf(payslip_lines(name, m)))
print('fixtures written')
"
