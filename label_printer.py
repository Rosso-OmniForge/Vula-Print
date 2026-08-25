#!/usr/bin/env python3
"""
Barcode Label Printer for Vula! Print Store
Handles stock reconciliation and label printing with batch control
"""

import csv
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Dict, Optional

class LabelPrinter:
    def __init__(self):
        self.stock_recon_file = Path("CSV_INPUT/STOCK_RECON/Items.csv")
        self.items_file = Path("CSV_INPUT/ITEMS/Item.csv")
        self.printers = []
        self.selected_printer = None
        self.items_data = {}
        self.stock_data = []

        # TSPL uses dot coordinates; at 203dpi it's ~8 dots/mm.
        # For a 40mm label width, that is ~320 dots.
        self.label_width_dots = 320

        # Global horizontal alignment tweak.
        # If printing is shifted left/right, adjust this. Positive values shift RIGHT.
        # 2mm at 203dpi ≈ 16 dots.
        self.horizontal_shift_dots = 16

        # Barcode horizontal placement.
        # If the barcode looks too far right/left, adjust this.
        # This does NOT affect the item code text (which is centered separately).
        self.barcode_x_dots = 30

    def _tspl_escape(self, s: str) -> str:
        """Escape a string for inclusion inside TSPL double-quoted fields."""
        return (s or "").replace('\\', '\\\\').replace('"', '\\"')

    def _center_x_for_text(self, text: str, font: str, xmul: int = 1) -> int:
        """Approximate centering for TSPL built-in fonts.

        TSPL TEXT uses left-origin X. We estimate text width in dots using common
        TSC font metrics; this may vary slightly by printer/firmware.
        """
        # Common approximate widths for TSC built-in fonts (dots per character)
        font_char_width = {
            '1': 8,
            '2': 12,
            '3': 16,
            '4': 24,
            '5': 32,
            '6': 14,
            '7': 14,
            '8': 14,
        }
        char_w = font_char_width.get(str(font), 8) * max(1, int(xmul))
        width = len(text or "") * char_w
        x = int((self.label_width_dots - width) / 2)
        return max(0, x)

    def _center_x_for_code39(self, data: str, narrow: int = 1, wide: int = 2) -> int:
        """Approximate centering for Code39 barcode.

        Each Code39 character is 9 elements (3 wide, 6 narrow). Printers typically
        add a narrow inter-character gap. Many firmwares also add start/stop.
        This estimate is good enough to visually center across typical lengths.
        """
        n = max(1, int(narrow))
        w = max(n, int(wide))

        # Estimate including start/stop characters.
        char_count = len(data or "") + 2
        per_char_modules = (3 * w) + (6 * n)
        inter_gap = n
        width = (char_count * per_char_modules) + ((char_count - 1) * inter_gap)
        x = int((self.label_width_dots - width) / 2)
        return max(0, x)
        
    def find_printers(self) -> List[str]:
        """Detect all USB printers connected to the system"""
        try:
            # Check /dev/usb/ for connected printers
            usb_path = Path("/dev/usb")
            if usb_path.exists():
                printers = sorted([str(p) for p in usb_path.glob("lp*")])
                return printers
            return []
        except Exception as e:
            print(f"Error finding printers: {e}")
            return []
    
    def test_print_all_printers(self):
        """Send test label to each printer to identify which is the label printer"""
        print("\n" + "="*60)
        print("  PRINTER TEST - Sending test label to each device")
        print("="*60)
        
        # Create a simple test label with initialization commands
        test_tspl_template = f'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0,0\\nSHIFT {self.horizontal_shift_dots}\\nCLS\\nTEXT 50,10,\\"3\\",0,1,1,\\"TEST PRINT\\"\\nBAR 20,40,280,2\\nTEXT 20,60,\\"4\\",0,1,1,\\"{{}}\\",\\nTEXT 20,100,\\"2\\",0,1,1,\\"Which printer produced\\"\\nTEXT 20,120,\\"2\\",0,1,1,\\"this label?\\"\\nPRINT 1\\n'
        
        for idx, printer in enumerate(self.printers, 1):
            printer_name = printer.split('/')[-1].upper()  # Extract 'LP0', 'LP2' etc
            label = test_tspl_template.format(printer_name)
            
            print(f"\n  Sending test to {printer_name}...", end=' ')
            
            try:
                # Use echo -e like in the sample
                cmd = f'echo -e "{label}" | sudo tee {printer} > /dev/null'
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if result.returncode == 0:
                    print("✓ Sent")
                else:
                    print("❌ Failed")
            except Exception as e:
                print(f"❌ Error: {e}")
        
        print("\n" + "="*60)
    
    def select_printer(self) -> bool:
        """Allow user to select a printer from available options"""
        self.printers = self.find_printers()
        
        if not self.printers:
            print("\n⚠️  No printers found in /dev/usb/")
            print("Please ensure your printer is connected.")
            return False
        
        # Test print on all printers
        self.test_print_all_printers()
        
        print("\n" + "="*50)
        print("  Select Your Label Printer")
        print("="*50)
        for idx, printer in enumerate(self.printers, 1):
            printer_name = printer.split('/')[-1].upper()
            print(f"  {idx}. {printer_name} ({printer})")
        print("="*50)
        
        while True:
            try:
                choice = input("\nWhich printer produced the label? (number or 'q' to quit): ").strip()
                if choice.lower() == 'q':
                    return False
                    
                printer_idx = int(choice) - 1
                if 0 <= printer_idx < len(self.printers):
                    self.selected_printer = self.printers[printer_idx]
                    printer_name = self.selected_printer.split('/')[-1].upper()
                    print(f"\n✓ Selected: {printer_name} ({self.selected_printer})")
                    return True
                else:
                    print(f"Please enter a number between 1 and {len(self.printers)}")
            except ValueError:
                print("Invalid input. Please enter a number.")
    
    def load_items_master_data(self):
        """Load the master items data with barcode and product information"""
        print("\n📂 Loading master item data...")
        
        try:
            with open(self.items_file, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                current_item = None
                temp_colour = ''
                temp_size = ''
                
                for row in reader:
                    item_code = row.get('Item Code', '').strip()
                    
                    # If we have an item code, it's a main item row
                    if item_code and item_code not in ['', 'item_code']:
                        # Save previous item if exists
                        if current_item and current_item not in self.items_data:
                            self.items_data[current_item] = {
                                'barcode': temp_barcode,
                                'name': temp_name,
                                'rate': temp_rate,
                                'colour': temp_colour,
                                'size': temp_size
                            }
                        
                        # Start new item
                        current_item = item_code
                        temp_barcode = row.get('Barcode (Barcodes)', '').strip()
                        temp_name = item_code  # Use item code as the display name
                        temp_rate = row.get('Standard Selling Rate', '0').strip()
                        temp_colour = ''
                        temp_size = ''
                        
                        # Check if colour/size in this row's Attribute Value
                        attr_val = row.get('Attribute Value (Variant Attributes)', '').strip()
                        if attr_val:
                            if any(x in attr_val for x in ['L - 58', 'M - 56', 'S - 54', 'XL - 60', 'XS - 52', 'XXL', 'XXS', 'Standard', 'STD']):
                                temp_size = self.parse_size(attr_val)
                            else:
                                temp_colour = attr_val
                    
                    # If no item code, it's a continuation row with more variant attributes
                    elif current_item:
                        attr_val = row.get('Attribute Value (Variant Attributes)', '').strip()
                        if attr_val:
                            if any(x in attr_val for x in ['L - 58', 'M - 56', 'S - 54', 'XL - 60', 'XS - 52', 'XXL', 'XXS', 'Standard', 'STD']):
                                temp_size = self.parse_size(attr_val)
                            else:
                                temp_colour = attr_val
                
                # Save last item
                if current_item and current_item not in self.items_data:
                    self.items_data[current_item] = {
                        'barcode': temp_barcode,
                        'name': temp_name,
                        'rate': temp_rate,
                        'colour': temp_colour,
                        'size': temp_size
                    }
            
            print(f"✓ Loaded {len(self.items_data)} items")
            return True
        except Exception as e:
            print(f"❌ Error loading items data: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def parse_size(self, attr_val: str) -> str:
        """Parse size from attribute value"""
        size_patterns = ['L - 58', 'M - 56', 'S - 54', 'XL - 60', 'XS - 52', 'XXL - 62', 'XXS - 50']
        for pattern in size_patterns:
            if pattern in attr_val:
                return pattern.split(' - ')[0]
        if 'STD' in attr_val or 'Standard' in attr_val:
            return 'STD'
        # Extract just letters if it looks like a size
        if len(attr_val) <= 5 and attr_val.replace('X', '').replace('L', '').replace('M', '').replace('S', '') == '':
            return attr_val
        return ''
    
    def extract_colour(self, row: Dict) -> str:
        """Extract colour information from variant attributes (DEPRECATED - kept for compatibility)"""
        return ''
    
    def extract_size(self, row: Dict) -> str:
        """Extract size information from variant attributes (DEPRECATED - kept for compatibility)"""
        return ''
    
    def load_stock_recon_data(self):
        """Load stock reconciliation data with current_qty for recon comparison.

        Note: We also capture `valuation_rate` from this file to use as the label price.
        The exported template may have different header casing (e.g. "Valuation Rate"
        vs "valuation_rate"), so we normalize keys.
        """
        print("\n📊 Loading stock reconciliation data...")
        
        try:
            with open(self.stock_recon_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
                # Find the actual header line (the one with field names)
                header_idx = -1
                for idx, line in enumerate(lines):
                    lower = line.lower()
                    # Support both underscore headers and title-case headers
                    if (('item_code' in lower and 'qty' in lower) or ('item code' in lower and 'quantity' in lower)):
                        header_idx = idx
                        break
                
                if header_idx == -1:
                    print("❌ Could not find header row")
                    return False
                
                # Parse from the header line onwards
                remaining_lines = lines[header_idx:]
                reader = csv.DictReader(remaining_lines)
                self.stock_data = []
                
                for row in reader:
                    # Normalize keys to handle different export header formats
                    normalized_row = {
                        (k or '').strip().lower().replace(' ', '_'): v
                        for k, v in row.items()
                    }

                    # Safely get values with None check
                    item_code_raw = normalized_row.get('item_code')
                    qty_str_raw = normalized_row.get('qty') or normalized_row.get('quantity')  # New count from recon
                    current_qty_raw = normalized_row.get('current_qty')
                    item_name_raw = normalized_row.get('item_name')
                    valuation_rate_raw = normalized_row.get('valuation_rate')
                    
                    if item_code_raw is None or qty_str_raw is None:
                        continue
                    
                    item_code = item_code_raw.strip()
                    qty_str = qty_str_raw.strip()
                    item_name = item_name_raw.strip() if item_name_raw else item_code
                    
                    # Skip header rows, empty entries, and instructional text
                    if not item_code or item_code in ['', 'item_code', ' ', 'The CSV format is case sensitive']:
                        continue
                    
                    # Skip rows with instructional text
                    if 'Do not edit' in item_code or 'CSV format' in item_code:
                        continue
                    
                    if not qty_str or qty_str == ' ':
                        continue
                    
                    try:
                        qty = int(float(qty_str))  # Reconciled quantity
                        current_qty = 0
                        valuation_rate = ''
                        
                        # Try to get current qty
                        if current_qty_raw and current_qty_raw.strip():
                            try:
                                current_qty = int(float(current_qty_raw.strip()))
                            except (ValueError, TypeError):
                                current_qty = 0

                        if valuation_rate_raw and str(valuation_rate_raw).strip():
                            valuation_rate = str(valuation_rate_raw).strip()
                        
                        if qty > 0:
                            self.stock_data.append({
                                'item_code': item_code,
                                'item_name': item_name,  # Store item name
                                'quantity': qty,  # Reconciled count
                                'current_qty': current_qty,  # System count
                                'valuation_rate': valuation_rate  # Price source for labels
                            })
                    except (ValueError, TypeError):
                        continue
            
            print(f"✓ Loaded {len(self.stock_data)} items with stock")
            return True
        except Exception as e:
            print(f"❌ Error loading stock data: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def generate_label_tspl(self, item_code: str, quantity: int, current: int, is_last: bool = False, item_name: str = '', price_override: Optional[str] = None) -> str:
        """Generate TSPL command for a product label.

        Note: The script previously supported a special end-of-batch "stock count" label
        (is_last=True). That behavior has been removed from the printing flows; this
        parameter is kept for backward compatibility with existing call sites.
        """
        item_info = self.items_data.get(item_code, {})
        
        # Get product details
        barcode = item_info.get('barcode', item_code)
        # Use item_name from stock_recon if provided, otherwise fall back to name from items
        display_name = item_name if item_name else item_info.get('name', 'Product')
        price = price_override if (price_override is not None and str(price_override).strip() != '') else item_info.get('rate', '0')
        colour = item_info.get('colour', 'N/A')
        size = item_info.get('size', 'N/A')
        
        # Format price with R currency
        try:
            price_val = float(price) if price else 0
            price_display = f"R {price_val:.2f}"
        except:
            price_display = f"R {price}"
        
        # Build product description
        product_desc = display_name
        if colour and colour != 'N/A':
            product_desc += f" - {colour}"
        
        # Text wrapping: split into two lines if too long
        line1 = ""
        line2 = ""
        if len(product_desc) > 24:
            words = product_desc.split()
            for word in words:
                test_line = line1 + (" " if line1 else "") + word
                if len(test_line) <= 24:
                    line1 = test_line
                else:
                    line2 = " ".join([line2, word]).strip() if line2 else word
            if len(line2) > 24:
                line2 = line2[:21] + "..."
        else:
            line1 = product_desc
        
        # Product label layout: Product name, Bar, Price, Size (left), Colour (left), Barcode, Item code
        # Each label includes SIZE/GAP to reset any drift
        # Barcode: left aligned with margin (user preference), not centered.
        barcode_x = max(0, int(self.barcode_x_dots))
        item_code_x = self._center_x_for_text(str(item_code), font='1', xmul=1)

        safe_line1 = self._tspl_escape(line1)
        safe_line2 = self._tspl_escape(line2)
        safe_price = self._tspl_escape(price_display)
        safe_size = self._tspl_escape(size)
        safe_colour = self._tspl_escape(colour)
        safe_barcode = self._tspl_escape(str(barcode))
        safe_item_code = self._tspl_escape(str(item_code))

        if line2:
            tspl = (
                f'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nSHIFT {self.horizontal_shift_dots}\\nCLS\\n'
                f'TEXT 20,16,\\"2\\",0,1,1,\\"{safe_line1}\\"\\n'
                f'TEXT 20,36,\\"2\\",0,1,1,\\"{safe_line2}\\"\\n'
                f'BAR 20,56,280,2\\n'
                f'TEXT 20,66,\\"3\\",0,1,1,\\"{safe_price}\\"\\n'
                f'TEXT 20,95,\\"2\\",0,1,1,\\"Size: {safe_size}\\"\\n'
                f'TEXT 20,113,\\"2\\",0,1,1,\\"{safe_colour}\\"\\n'
                f'BARCODE {barcode_x},135,\\"39\\",70,0,0,1,2,\\"{safe_barcode}\\"\\n'
                f'TEXT {item_code_x},210,\\"1\\",0,1,1,\\"{safe_item_code}\\"\\n'
                f'PRINT 1\\n'
            )
        else:
            tspl = (
                f'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nSHIFT {self.horizontal_shift_dots}\\nCLS\\n'
                f'TEXT 20,16,\\"2\\",0,1,1,\\"{safe_line1}\\"\\n'
                f'BAR 20,56,280,2\\n'
                f'TEXT 20,66,\\"3\\",0,1,1,\\"{safe_price}\\"\\n'
                f'TEXT 20,95,\\"2\\",0,1,1,\\"Size: {safe_size}\\"\\n'
                f'TEXT 20,113,\\"2\\",0,1,1,\\"{safe_colour}\\"\\n'
                f'BARCODE {barcode_x},135,\\"39\\",70,0,0,1,2,\\"{safe_barcode}\\"\\n'
                f'TEXT {item_code_x},210,\\"1\\",0,1,1,\\"{safe_item_code}\\"\\n'
                f'PRINT 1\\n'
            )
        
        return tspl
    
    def calibrate_printer(self) -> bool:
        """Calibrate the printer to detect label gaps and edges"""
        try:
            # Full printer initialization sequence to reset all settings
            # This ensures consistent behavior across power cycles
            init_commands = [
                '~!T',  # Reset printer to defaults
                'SIZE 40 mm,30 mm',
                'GAP 2 mm,0',
                'DIRECTION 0',  # Set to normal direction (single arg — TSPL standard)
                f'SHIFT {self.horizontal_shift_dots}',
                'OFFSET 0',  # No vertical offset
                'SPEED 4',  # Set print speed
                'DENSITY 8',  # Set darkness
                'SET TEAR ON',  # Enable tear-off mode
                'CLS'  # Clear buffer
            ]
            calibration_cmd = '\n'.join(init_commands) + '\n'
            cmd = f'echo -e "{calibration_cmd}" | sudo tee {self.selected_printer} > /dev/null'
            subprocess.run(cmd, shell=True, capture_output=True, text=True)
            time.sleep(0.5)  # Give printer time to process
            return True
        except Exception as e:
            print(f"❌ Calibration error: {e}")
            return False
    
    def send_to_printer(self, tspl_command: str) -> bool:
        """Send TSPL command to the selected printer using echo -e"""
        try:
            # Use echo -e to properly interpret escape sequences, matching the sample format
            cmd = f'echo -e "{tspl_command}" | sudo tee {self.selected_printer} > /dev/null'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            return result.returncode == 0
        except Exception as e:
            print(f"❌ Print error: {e}")
            return False
    
    def print_batch(self, items_to_print: List[tuple], start_idx: int, batch_size: int = 50):
        """Print a batch of labels with user control"""
        end_idx = min(start_idx + batch_size, len(items_to_print))
        batch = items_to_print[start_idx:end_idx]
        
        print(f"\n{'='*60}")
        print(f"  Printing batch: Labels {start_idx + 1} to {end_idx}")
        print(f"{'='*60}")
        
        # Calibrate printer before starting batch
        if start_idx == 0:
            print("📐 Calibrating printer...")
            self.calibrate_printer()
        
        labels_printed = 0
        
        for item_code, qty_needed, current_label, is_last in batch:
            # Get item_name from stock_data
            item_name = ''
            valuation_rate = ''
            for stock_item in self.stock_data:
                if stock_item['item_code'] == item_code:
                    item_name = stock_item.get('item_name', '')
                    valuation_rate = stock_item.get('valuation_rate', '')
                    break
            
            tspl = self.generate_label_tspl(item_code, qty_needed, current_label, is_last, item_name, price_override=valuation_rate)
            
            if self.send_to_printer(tspl):
                labels_printed += 1
                print(f"✓ Label {start_idx + labels_printed}/{len(items_to_print)}: {item_code} ({current_label}/{qty_needed})")
            else:
                print(f"❌ Failed to print label for {item_code}")
                
        print(f"\n✓ Batch complete: {labels_printed} labels printed")
        
        return end_idx
    
    def print_all_labels(self, print_mode='fresh'):
        """Main printing function with batch control"""
        if not self.stock_data:
            print("❌ No stock data loaded")
            return
        
        # Build complete list of labels to print
        print(f"\n📝 Preparing labels ({print_mode.upper()} mode)...")
        items_to_print = []
        
        for stock_item in self.stock_data:
            item_code = stock_item['item_code']
            quantity = stock_item['quantity']  # Reconciled count
            current_qty = stock_item.get('current_qty', 0)  # System count
            
            if item_code not in self.items_data:
                print(f"⚠️  Warning: {item_code} not found in master data, skipping...")
                continue
            
            # Determine how many labels to print based on mode
            if print_mode == 'recon':
                # Print only the DIFFERENCE (new stock found during recon)
                labels_needed = quantity - current_qty
                if labels_needed <= 0:
                    continue  # No new stock, skip
            else:
                # Fresh print: print all labels
                labels_needed = quantity
            
            # Add regular labels
            for i in range(1, labels_needed + 1):
                items_to_print.append((item_code, labels_needed, i, False))
        
        total_labels = len(items_to_print)
        print(f"\n{'='*60}")
        print(f"  READY TO PRINT: {total_labels} total labels")
        print(f"  ({len(self.stock_data)} products)")
        print(f"{'='*60}")
        
        input("\nPress ENTER to start printing...")
        
        current_idx = 0
        batch_size = 50
        
        while current_idx < total_labels:
            # Print batch
            current_idx = self.print_batch(items_to_print, current_idx, batch_size)
            
            if current_idx < total_labels:
                remaining = total_labels - current_idx
                print(f"\n⏸️  Batch complete. {remaining} labels remaining.")
                print("\nOptions:")
                print("  1. Continue with next batch")
                print("  2. Reprint last batch")
                print("  3. Quit")
                
                while True:
                    choice = input("\nYour choice (1-3): ").strip()
                    if choice == '1':
                        break
                    elif choice == '2':
                        current_idx = max(0, current_idx - batch_size)
                        break
                    elif choice == '3':
                        print("\n🛑 Printing stopped.")
                        print(f"Progress: {current_idx}/{total_labels} labels printed")
                        return
                    else:
                        print("Invalid choice. Please enter 1, 2, or 3.")
        
        print(f"\n{'='*60}")
        print(f"  ✅ ALL LABELS PRINTED SUCCESSFULLY!")
        print(f"  Total: {total_labels} labels")
        print(f"{'='*60}")
    
    def print_single_test(self):
        """Print a single test label to verify format"""
        if not self.stock_data:
            print("❌ No stock data loaded")
            return
        
        # Pick a random item with stock that has colour and size
        import random
        items_with_variants = [
            item for item in self.stock_data 
            if item['item_code'] in self.items_data 
            and self.items_data[item['item_code']].get('colour')
            and self.items_data[item['item_code']].get('size')
        ]
        
        if items_with_variants:
            stock_item = random.choice(items_with_variants)
        else:
            # Fallback to any item
            stock_item = random.choice(self.stock_data)
        
        item_code = stock_item['item_code']
        quantity = stock_item['quantity']
        
        if item_code not in self.items_data:
            print("❌ Selected item not found in master data")
            return
        
        item_info = self.items_data[item_code]
        print(f"\n{'='*60}")
        print(f"  SINGLE TEST PRINT (Random Item)")
        print(f"  Item: {item_code}")
        print(f"  Colour: {item_info.get('colour', 'N/A')}")
        print(f"  Size: {item_info.get('size', 'N/A')}")
        print(f"  Price: {item_info.get('rate', 'N/A')}")
        print(f"{'='*60}")
        
        input("\nPress ENTER to print 1 test label...")
        
        item_name = stock_item.get('item_name', '')
        valuation_rate = stock_item.get('valuation_rate', '')
        tspl = self.generate_label_tspl(item_code, quantity, 1, False, item_name, price_override=valuation_rate)
        
        if self.send_to_printer(tspl):
            print(f"\n✓ Test label printed for {item_code}")
            print(f"\nPlease verify the label format.")
        else:
            print(f"\n❌ Failed to print test label")
    
    def print_test_batch(self):
        """Print a small batch of product labels for testing"""
        if not self.stock_data:
            print("❌ No stock data loaded")
            return
        
        # Pick a random item with stock
        import random
        items_with_variants = [
            item for item in self.stock_data 
            if item['item_code'] in self.items_data 
            and self.items_data[item['item_code']].get('colour')
            and self.items_data[item['item_code']].get('size')
            and item['quantity'] >= 3  # At least 3 items
        ]
        
        if items_with_variants:
            stock_item = random.choice(items_with_variants)
        else:
            # Fallback to any item with at least 3 quantity
            stock_item = random.choice([s for s in self.stock_data if s['quantity'] >= 3])
        
        item_code = stock_item['item_code']
        quantity = min(stock_item['quantity'], 5)  # Max 5 labels for test
        
        if item_code not in self.items_data:
            print("❌ Selected item not found in master data")
            return
        
        item_info = self.items_data[item_code]
        print(f"\n{'='*60}")
        print(f"  TEST BATCH PRINT (Random Item)")
        print(f"  Item: {item_code}")
        print(f"  Colour: {item_info.get('colour', 'N/A')}")
        print(f"  Size: {item_info.get('size', 'N/A')}")
        print(f"  Price: {item_info.get('rate', 'N/A')}")
        print(f"  Labels to print: {quantity}")
        print(f"{'='*60}")
        
        input(f"\nPress ENTER to print {quantity} test labels...")
        
        # Print regular product labels
        item_name = stock_item.get('item_name', '')
        valuation_rate = stock_item.get('valuation_rate', '')
        for i in range(1, quantity + 1):
            tspl = self.generate_label_tspl(item_code, quantity, i, False, item_name, price_override=valuation_rate)
            if self.send_to_printer(tspl):
                print(f"✓ Label {i}/{quantity}: {item_code}")
            else:
                print(f"❌ Failed: Label {i}")

        print(f"\n✓ Test batch complete: {quantity} product labels")
        print(f"\nPlease verify:")
        print(f"  1. Product labels show correct info")
        print(f"  2. All barcodes scan correctly")
    
    def print_sample(self, num_labels: int = 25):
        """Print a sample batch for testing"""
        if not self.stock_data:
            print("❌ No stock data loaded")
            return
        
        print(f"\n{'='*60}")
        print(f"  SAMPLE PRINT: First {num_labels} labels")
        print(f"{'='*60}")
        
        # Build sample list
        items_to_print = []
        label_count = 0
        
        for stock_item in self.stock_data:
            if label_count >= num_labels:
                break
                
            item_code = stock_item['item_code']
            quantity = stock_item['quantity']
            
            if item_code not in self.items_data:
                continue
            
            # Add labels for this product
            for i in range(1, quantity + 1):
                if label_count >= num_labels:
                    break
                items_to_print.append((item_code, quantity, i, False))
                label_count += 1
            
            # Note: summary/stock-count labels removed; sample prints product labels only
        
        input(f"\nPress ENTER to print {len(items_to_print)} sample labels...")
        
        # Print all at once (sample mode)
        for idx, (item_code, qty, current, is_last) in enumerate(items_to_print, 1):
            # Get item_name from stock_data
            item_name = ''
            valuation_rate = ''
            for stock_item in self.stock_data:
                if stock_item['item_code'] == item_code:
                    item_name = stock_item.get('item_name', '')
                    valuation_rate = stock_item.get('valuation_rate', '')
                    break
            
            tspl = self.generate_label_tspl(item_code, qty, current, is_last, item_name, price_override=valuation_rate)
            
            if self.send_to_printer(tspl):
                print(f"✓ Sample {idx}/{len(items_to_print)}: {item_code} ({current}/{qty})")
            else:
                print(f"❌ Failed: {item_code}")
        
        print(f"\n✓ Sample complete: {len(items_to_print)} labels printed")
    
    def run(self):
        """Main application loop"""
        print("\n" + "="*60)
        print("  VULA! PRINT - Print Manager")
        print("  Ultra High-End Clothing Store")
        print("="*60)
        
        # Select printer
        if not self.select_printer():
            print("\n❌ No printer selected. Exiting.")
            return
        
        # Load data
        if not self.load_items_master_data():
            print("\n❌ Failed to load master data. Exiting.")
            return
        
        if not self.load_stock_recon_data():
            print("\n❌ Failed to load stock data. Exiting.")
            return
        
        # Main menu
        while True:
            print("\n" + "="*60)
            print("  MAIN MENU")
            print("="*60)
            print("  1. Print SINGLE test label")
            print("  2. Print TEST BATCH (5 labels)")
            print("  3. Print SAMPLE (25 labels)")
            print("  4. Print ALL - FRESH mode (all labels)")
            print("  5. Print ALL - RECON mode (difference only)")
            print("  6. View statistics")
            print("  7. Calibrate printer (fix alignment)")
            print("  8. Change printer")
            print("  9. Exit")
            print("="*60)
            print("\n  FRESH: Prints all stock labels")
            print("  RECON: Prints only difference (QTY - Current QTY)")
            print("="*60)
            
            choice = input("\nSelect option (1-9): ").strip()
            
            if choice == '1':
                self.print_single_test()
            elif choice == '2':
                self.print_test_batch()
            elif choice == '3':
                self.print_sample()
            elif choice == '4':
                self.print_all_labels(print_mode='fresh')
            elif choice == '5':
                self.print_all_labels(print_mode='recon')
            elif choice == '6':
                self.show_statistics()
            elif choice == '7':
                print("\n📐 Calibrating printer...")
                if self.calibrate_printer():
                    print("✓ Calibration complete. Try printing a test label.")
                else:
                    print("❌ Calibration failed.")
            elif choice == '8':
                if not self.select_printer():
                    print("❌ Printer selection cancelled.")
            elif choice == '9':
                print("\n👋 Thank you for using the Print Manager!")
                break
            else:
                print("❌ Invalid choice. Please enter 1-9.")
    
    def show_statistics(self):
        """Show statistics about the current data"""
        print("\n" + "="*60)
        print("  STATISTICS")
        print("="*60)
        
        total_products = len(self.stock_data)
        total_labels = sum(item['quantity'] for item in self.stock_data)
        total_stock = sum(item['quantity'] for item in self.stock_data)
        
        print(f"  Products with stock: {total_products}")
        print(f"  Total units in stock: {total_stock}")
        print(f"  Total labels to print: {total_labels}")
        print(f"    - Product labels: {total_stock}")
        print("="*60)
        
        # Show top 10 items by quantity
        sorted_items = sorted(self.stock_data, key=lambda x: x['quantity'], reverse=True)
        print("\n  Top 10 Items by Quantity:")
        print("  " + "-"*56)
        for idx, item in enumerate(sorted_items[:10], 1):
            item_code = item['item_code']
            qty = item['quantity']
            item_info = self.items_data.get(item_code, {})
            name = item_info.get('name', 'Unknown')[:30]
            print(f"  {idx:2d}. {item_code:25s} | Qty: {qty:3d} | {name}")
        print("="*60)


def main():
    """Entry point"""
    printer = LabelPrinter()
    
    try:
        printer.run()
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user. Exiting...")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
