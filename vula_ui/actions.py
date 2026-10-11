"""Mixin for VulaPrintApp — see vula_app.py for composition."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox

from vula_http import HttpWorker
from vula_workers import PrintJob, POSSlipPrintJob


class ActionsMixin:
    """See vula_app.py for composition."""

    def print_request(self, request: Dict[str, Any]):
        """Print labels for a specific request, using its owning connection."""
        conn = self._connection_for_request(request)
        if not conn:
            QMessageBox.critical(self, "Error", "Could not determine store connection for this request.")
            return

        if not self.printer_roles.get("label"):
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        if not self.printer_calibrated:
            reply = QMessageBox.question(
                self,
                "Printer Not Calibrated",
                "Printer has not been calibrated. Print anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        w = HttpWorker(
            tag=f"labeldetail:{conn.connection_id}:{request['id']}",
            method="GET",
            url=f"{conn.api_base_url.rstrip('/')}/admin/api/label-printing/request/{request['id']}",
            headers=self._headers_for(conn),
            timeout=10.0,
        )
        w.done.connect(
            lambda r, c=conn, req=request: self._on_label_detail_fetched(r, c, req)
        )
        w.finished.connect(lambda w=w: self._forget_http_worker(w))
        self._http_workers.append(w)
        w.start()
        self.status_bar.showMessage(f"Fetching request #{request['id']} ({conn.name})…")

    def _on_label_detail_fetched(self, result, conn, request):
        """Detail fetch completed off-thread; start the print job on the main thread."""
        if not (result.ok and result.status == 200):
            QMessageBox.critical(self, "Error", "Failed to fetch print job details")
            return
        data = result.data if isinstance(result.data, dict) else {}
        items = data.get("items", [])
        if not items:
            QMessageBox.warning(self, "No Items", "This request has no items to print.")
            return
        label_printer = self.printer_roles.get("label")
        if not label_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        self._current_print_request = request
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.print_job = PrintJob(label_printer, items, self.label_layout)
        self.print_job.progress.connect(self.on_print_progress)
        self.print_job.completed.connect(
            lambda s, m: self.on_print_finished(s, m, request['id'], conn.connection_id)
        )
        self.print_job.start()
        self.status_bar.showMessage(f"Printing request #{request['id']} ({conn.name})...")

    def on_print_progress(self, current: int, total: int):
        """Update progress bar."""
        if total > 0:
            percentage = int((current / total) * 100)
            self.progress_bar.setValue(percentage)
            self.status_bar.showMessage(f"Printing: {current}/{total} labels")

    def on_print_finished(self, success: bool, message: str, request_id: int, connection_id: str):
        """Handle print job completion, completing on the SAME connection that supplied it."""
        self.progress_bar.setVisible(False)
        conn = self.get_connection_by_id(connection_id)

        if success:
            self._save_to_history(self._current_print_request)
            QMessageBox.information(self, "Success", message)
            if conn:
                self._complete_label_request_async(conn, request_id)
            self.fetch_pending_requests()
        else:
            QMessageBox.critical(self, "Print Failed", message)

        self.status_bar.showMessage("Ready")

    def calibrate_printer(self):
        """Calibrate printer and print test label."""
        label_printer = self.printer_roles.get("label")
        if not label_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        if self.calibration_job and self.calibration_job.isRunning():
            QMessageBox.information(self, "Calibration In Progress", "Calibration is already running.")
            return

        # ── TSPL calibration sequence — do not add GAPDETECT or HOME
        # without a per-printer opt-in; they misbehave on the clone
        # hardware we ship against.
        calibration_tspl = (
            "~!T\n"
            "SIZE 40 mm,30 mm\n"
            "GAP 2 mm,0\n"
            "DIRECTION 0\n"
            "SHIFT 16\n"
            "OFFSET 0\n"
            "SPEED 4\n"
            "DENSITY 8\n"
            "SET TEAR ON\n"
            "CLS\n"
        )
        try:
            from vula_device_io import write_to_device
            write_to_device(label_printer, calibration_tspl.encode('utf-8'))
        except PermissionError:
            QMessageBox.critical(
                self, "Permission Denied",
                f"Cannot write to {label_printer}.\n\n"
                f"The printer device requires your user account to be in the 'lp' group.\n\n"
                f"Re-run the install script to fix this automatically, or run:\n"
                f"  sudo usermod -aG lp $USER\n\n"
                f"Then log out and back in (or reboot) for the change to take effect."
            )
            return
        except Exception as e:
            QMessageBox.critical(self, "Calibration Error", f"Failed to calibrate: {e}")
            return

        self.status_bar.showMessage("Calibrating printer…")
        QTimer.singleShot(1500, self._print_calibration_test_label)

    def _print_calibration_test_label(self):
        """Second half of calibration: prints the test label after the feed."""
        label_printer = self.printer_roles.get("label")
        if not label_printer:
            self.status_bar.showMessage("Printer unassigned during calibration")
            return

        test_item = {
            "title": "VULA! PRINT",
            "variant_label": "Calibration Test",
            "sku": "CALIB-TEST",
            "code39": "CALIBTEST",
            "price_cents": 95000,
            "currency": "ZAR",
            "qty_to_print": 1,
        }

        self.calibration_job = PrintJob(label_printer, [test_item], self.label_layout)
        self.calibration_job.completed.connect(self.on_test_print_finished)
        self.calibration_job.start()

    def on_test_print_finished(self, success: bool, message: str):
        """Handle test print completion."""
        self.calibration_job = None
        if success:
            reply = QMessageBox.question(
                self,
                "Test Print",
                "Test label printed. Does it look correct?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.printer_calibrated = True
                if hasattr(self, "calibration_status"):
                    self.calibration_status.setText("Calibrated")
                    self.calibration_status.setStyleSheet(self._pill_style("good"))
                self.status_bar.showMessage("Printer calibrated successfully")
            else:
                QMessageBox.information(
                    self,
                    "Calibration Help",
                    "Please check:\n"
                    "- Label size is 40mm x 30mm\n"
                    "- Gap is 2mm\n"
                    "- Printer alignment settings\n\n"
                    "Try calibrating again or adjust printer settings."
                )
        else:
            QMessageBox.critical(self, "Test Print Failed", message)

    def print_test_label_standalone(self):
        """Print a single representative test label to check layout without calibrating."""
        label_printer = self.printer_roles.get("label")
        if not label_printer:
            QMessageBox.warning(self, "No Printer", "Please select a printer first.")
            return

        test_item = {
            "title": "Vula! Print",
            "variant_label": "Al Maisa Cape - Black",
            "sku": "ALM-CAP-SIN-BLK-L",
            "code39": "99001",
            "price_cents": 95000,
            "currency": "ZAR",
            "qty_to_print": 1,
        }

        reply = QMessageBox.question(
            self, "Print Test Label",
            "This will print 1 test label using sample data.\n"
            "SKU: ALM-CAP-SIN-BLK-L  |  Price: R950.00\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        job = PrintJob(label_printer, [test_item], self.label_layout)
        job.completed.connect(self._on_test_label_standalone_finished)
        self.status_bar.showMessage("Printing test label…")
        job.start()
        self._test_label_job = job

    def _on_test_label_standalone_finished(self, success: bool, message: str):
        self._test_label_job = None
        if success:
            QMessageBox.information(self, "Test Label Sent",
                "Test label sent to printer.\n\n"
                "Check the label for:\n"
                "  • Title and variant text at top\n"
                "  • Price in font 3 (medium, not giant)\n"
                "  • Barcode fits on the 40 mm width\n"
                "  • SKU readable at bottom")
        else:
            QMessageBox.critical(self, "Test Label Failed", message)
        self.status_bar.showMessage("Ready")

    def _build_sample_pos_payload(self) -> Dict[str, Any]:
        """Build a six-item sample payload for POS printer testing."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {
            "request": {
                "id": 999999,
                "invoice_number": "TEST-POS-0001",
                "created_at": now,
                "payment_type": "card",
            },
            "business": {
                "brand_name": "Vula! Print Demo Store",
                "phone": "+27 11 555 0101",
                "email": "info@vula.local",
                "vat_number": "4555555555",
                "address_line1": "1 Orange Street",
                "address_line2": "Unit B",
                "city": "Johannesburg",
                "province": "Gauteng",
                "postal_code": "2000",
                "country": "ZA",
            },
            "store": {
                "name": "Sandton Demo Counter",
                "address": "123 Example Ave\nSandton\nGauteng\n2196\nZA",
                "phone": "+27 11 555 0111",
                "email": "sandton@vula.local",
            },
            "cashier_username": "printer_test",
            "customer_email": "",
            "footer_note": "Test print completed. Please verify alignment and cutter.",
            "items": [
                {"qty": 1, "title": "Premium Hoodie", "variant_label": "Black / M", "sku": "HD-BLK-M", "unit_price_cents": 89900, "line_tax_cents": 11726, "line_total_cents": 89900},
                {"qty": 2, "title": "Athletic Socks", "variant_label": "White / L", "sku": "SOCK-WHT-L", "unit_price_cents": 12900, "line_tax_cents": 3366, "line_total_cents": 25800},
                {"qty": 1, "title": "Sports Bottle", "variant_label": "750ml", "sku": "BOT-750", "unit_price_cents": 14900, "line_tax_cents": 1943, "line_total_cents": 14900},
                {"qty": 1, "title": "Running Cap", "variant_label": "Grey", "sku": "CAP-GRY", "unit_price_cents": 19900, "line_tax_cents": 2596, "line_total_cents": 19900},
                {"qty": 1, "title": "Compression Tee", "variant_label": "Navy / XL", "sku": "TEE-NVY-XL", "unit_price_cents": 34900, "line_tax_cents": 4552, "line_total_cents": 34900},
                {"qty": 1, "title": "Gift Wrap", "variant_label": "Standard", "sku": "WRAP-STD", "unit_price_cents": 2500, "line_tax_cents": 326, "line_total_cents": 2500},
            ],
            "totals": {
                "vat_bps": 1500,
                "tax_cents": 24509,
                "subtotal_before_discount_cents": 198900,
                "manual_discount_cents": 20000,
                "voucher_discount_cents": 0,
                "subtotal_cents": 178900,
                "total_cents": 203409,
                "currency": "ZAR",
            },
            "website_url": "https://www.example.com/",
            "logo_url": "",
            "qr_data": "https://www.example.com/",
        }

    def print_test_pos_slip(self):
        """Print a six-item sample POS slip for cutter/alignment verification."""
        pos_printer = self.printer_roles.get("pos_slip")
        if not pos_printer:
            QMessageBox.warning(self, "No POS Printer", "Please select a POS slip printer first.")
            return
        if self.pos_print_job and self.pos_print_job.isRunning():
            QMessageBox.information(self, "POS Print Busy", "A POS slip is already printing.")
            return

        confirm = QMessageBox.question(
            self,
            "Test POS Printer",
            "This prints a sample POS slip with 6 items and performs paper cut.\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        sample_payload = self._build_sample_pos_payload()
        self.pos_print_job = POSSlipPrintJob(
            pos_printer,
            sample_payload,
            width_chars=self.pos_width_chars,
            qr_mode=self.pos_qr_mode,
            qr_module_px=self.pos_qr_module_px,
        )
        self.pos_print_job.completed.connect(self._on_test_pos_finished)
        self.pos_print_job.start()
        self.status_bar.showMessage("Printing sample POS slip...")

    def _on_test_pos_finished(self, success: bool, message: str):
        self.pos_print_job = None
        if success:
            QMessageBox.information(
                self,
                "POS Test Printed",
                "Sample POS slip printed and cut.\n\n"
                "Verify text clarity, spacing, and cutter operation.",
            )
        else:
            QMessageBox.critical(self, "POS Test Failed", message)
        self.status_bar.showMessage("Ready")