#!/usr/bin/env python3
"""Verify the TSPL output matches the current implementation.

This script intentionally prints a known working sample (static) and then prints
the current TSPL produced by `LabelPrinter.generate_label_tspl()` so the output
stays up-to-date with centering, SHIFT tweaks, and price-source changes.
"""

from label_printer import LabelPrinter


def _pretty(tspl: str) -> str:
	"""Make TSPL easier to read in terminal output."""
	return (tspl or "").replace('\\n', '\n').replace('\\"', '"')


print("=" * 60)
print("  TSPL FORMAT VERIFICATION")
print("=" * 60)

print("\n📋 WORKING SAMPLE (static reference):")
print("-" * 60)
working = (
	'SIZE 40 mm,30 mm\n'
	'GAP 2 mm,0\n'
	'CLS\n'
	'TEXT 50,10,"3",0,1,1,"Vula! Print"\n'
	'BAR 20,40,280,2\n'
	'TEXT 20,50,"2",0,1,1,"Al Maisa Cape - Blk"\n'
	'TEXT 20,80,"3",0,1,1,"R 950.00"\n'
	'BOX 260,75,300,105,2\n'
	'TEXT 270,80,"2",0,1,1,"L"\n'
	'BARCODE 0,120,"39",70,0,0,1,2,"99001"\n'
	'TEXT 0,200,"1",0,1,1,"ALM-CAP-SIN-BLK-L"\n'
	'PRINT 1'
)
print(working)

printer = LabelPrinter()

# Build minimal master-data for one example item
item_code = "TEST-ITEM-M"
printer.items_data[item_code] = {
	'barcode': '4753481977',
	'name': item_code,
	'rate': '0',
	'colour': 'Black',
	'size': 'M',
}

generated = printer.generate_label_tspl(
	item_code=item_code,
	quantity=1,
	current=1,
	is_last=False,
	item_name="Test Product Name",
	price_override="250",
)

print("\n\n📋 CURRENT GENERATED FORMAT (from label_printer.py):")
print("-" * 60)
print(_pretty(generated))

item_code_x = printer._center_x_for_text(item_code, font='1', xmul=1)

print("\n\n🔍 NOTES:")
print("-" * 60)
print(f"  ✓ Uses SHIFT {printer.horizontal_shift_dots} (global horizontal adjustment)")
print(f"  ✓ Barcode X = {printer.barcode_x_dots} (left-aligned margin)")
print(f"  ✓ Centers item code TEXT X ≈ {item_code_x}")
print("  ✓ Uses stock recon valuation_rate when printing real labels")
print("=" * 60)

print("\n\n🧪 QUICK TEST COMMAND:")
print("=" * 60)
print("Replace /dev/usb/lpX with your selected printer device:")
escaped_for_shell = generated.replace('"', '\\"')
print(f'  echo -e "{escaped_for_shell}" | sudo tee /dev/usb/lpX > /dev/null')
print("=" * 60)
