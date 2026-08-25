#!/usr/bin/env python3
"""
Diagnostic script to test label data parsing and generation
"""

import csv
from pathlib import Path

def test_items_csv():
    """Test parsing of Items.csv"""
    print("\n" + "="*60)
    print("  TESTING ITEMS.CSV PARSING")
    print("="*60)
    
    items_file = Path("CSV_INPUT/ITEMS/Item.csv")
    items_data = {}
    
    with open(items_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        current_item = None
        temp_colour = ''
        temp_size = ''
        
        for row in reader:
            item_code = row.get('Item Code', '').strip()
            
            # If we have an item code, it's a main item row
            if item_code and item_code not in ['', 'item_code']:
                # Save previous item if exists
                if current_item and current_item not in items_data:
                    items_data[current_item] = {
                        'barcode': temp_barcode,
                        'name': temp_name,
                        'rate': temp_rate,
                        'colour': temp_colour,
                        'size': temp_size
                    }
                
                # Start new item
                current_item = item_code
                temp_barcode = row.get('Barcode (Barcodes)', '').strip()
                temp_name = item_code
                temp_rate = row.get('Standard Selling Rate', '0').strip()
                temp_colour = ''
                temp_size = ''
                
                # Check if colour/size in this row's Attribute Value
                attr_val = row.get('Attribute Value (Variant Attributes)', '').strip()
                if attr_val:
                    if any(x in attr_val for x in ['L - 58', 'M - 56', 'S - 54', 'XL - 60', 'XS - 52', 'XXL', 'XXS', 'Standard', 'STD']):
                        temp_size = parse_size(attr_val)
                    else:
                        temp_colour = attr_val
            
            # If no item code, it's a continuation row with more variant attributes
            elif current_item:
                attr_val = row.get('Attribute Value (Variant Attributes)', '').strip()
                if attr_val:
                    if any(x in attr_val for x in ['L - 58', 'M - 56', 'S - 54', 'XL - 60', 'XS - 52', 'XXL', 'XXS', 'Standard', 'STD']):
                        temp_size = parse_size(attr_val)
                    else:
                        temp_colour = attr_val
        
        # Save last item
        if current_item and current_item not in items_data:
            items_data[current_item] = {
                'barcode': temp_barcode,
                'name': temp_name,
                'rate': temp_rate,
                'colour': temp_colour,
                'size': temp_size
            }
    
    print(f"\n✓ Parsed {len(items_data)} items")
    
    # Show first 10 items
    print("\nFirst 10 items:")
    for idx, (code, data) in enumerate(list(items_data.items())[:10], 1):
        print(f"\n{idx}. {code}")
        print(f"   Barcode: {data['barcode']}")
        print(f"   Name: {data['name']}")
        print(f"   Price: {data['rate']}")
        print(f"   Colour: {data['colour'] or 'N/A'}")
        print(f"   Size: {data['size'] or 'N/A'}")
    
    return items_data

def parse_size(attr_val: str) -> str:
    """Parse size from attribute value"""
    size_patterns = ['L - 58', 'M - 56', 'S - 54', 'XL - 60', 'XS - 52', 'XXL - 62', 'XXS - 50']
    for pattern in size_patterns:
        if pattern in attr_val:
            return pattern.split(' - ')[0]
    if 'STD' in attr_val or 'Standard' in attr_val:
        return 'STD'
    if len(attr_val) <= 5 and attr_val.replace('X', '').replace('L', '').replace('M', '').replace('S', '') == '':
        return attr_val
    return ''

def test_stock_csv():
    """Test parsing of Stock Recon CSV"""
    print("\n" + "="*60)
    print("  TESTING STOCK_RECON/Items.csv PARSING")
    print("="*60)
    
    stock_file = Path("CSV_INPUT/STOCK_RECON/Items.csv")
    stock_data = []
    
    with open(stock_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
        # Find the actual header line
        header_idx = -1
        for idx, line in enumerate(lines):
            if 'item_code' in line.lower() and 'qty' in line.lower():
                if '"item_code"' in line or 'item_code' in line:
                    header_idx = idx
                    break
        
        if header_idx == -1:
            print("❌ Could not find header row")
            return []
        
        print(f"\n✓ Found header at line {header_idx + 1}")
        print(f"Header: {lines[header_idx].strip()[:100]}...")
        
        # Parse from the header line onwards
        remaining_lines = lines[header_idx:]
        reader = csv.DictReader(remaining_lines)
        
        for row in reader:
            item_code_raw = row.get('item_code')
            qty_str_raw = row.get('qty')
            current_qty_raw = row.get('current_qty')
            item_name_raw = row.get('item_name')
            
            if item_code_raw is None or qty_str_raw is None:
                continue
            
            item_code = item_code_raw.strip()
            qty_str = qty_str_raw.strip()
            item_name = item_name_raw.strip() if item_name_raw else item_code
            
            # Skip header rows and empty entries
            if not item_code or item_code in ['', 'item_code', ' ', 'The CSV format is case sensitive']:
                continue
            
            if 'Do not edit' in item_code or 'CSV format' in item_code:
                continue
            
            if not qty_str or qty_str == ' ':
                continue
            
            try:
                qty = int(float(qty_str))
                current_qty = 0
                
                if current_qty_raw and current_qty_raw.strip():
                    try:
                        current_qty = int(float(current_qty_raw.strip()))
                    except (ValueError, TypeError):
                        current_qty = 0
                
                if qty > 0:
                    stock_data.append({
                        'item_code': item_code,
                        'item_name': item_name,
                        'quantity': qty,
                        'current_qty': current_qty
                    })
            except (ValueError, TypeError):
                continue
    
    print(f"\n✓ Parsed {len(stock_data)} items with stock")
    
    # Show first 10 items
    print("\nFirst 10 stock items:")
    for idx, item in enumerate(stock_data[:10], 1):
        print(f"\n{idx}. {item['item_code']}")
        print(f"   Name: {item['item_name']}")
        print(f"   Qty: {item['quantity']}")
        print(f"   Current: {item['current_qty']}")
    
    return stock_data

def test_label_generation(items_data, stock_data):
    """Test TSPL label generation for a sample item"""
    print("\n" + "="*60)
    print("  TESTING LABEL GENERATION")
    print("="*60)
    
    if not stock_data:
        print("❌ No stock data available")
        return
    
    # Find an item with colour and size
    test_item = None
    for stock in stock_data[:50]:
        item_code = stock['item_code']
        if item_code in items_data:
            item_info = items_data[item_code]
            if item_info.get('colour') and item_info.get('size'):
                test_item = stock
                break
    
    if not test_item:
        test_item = stock_data[0]
    
    item_code = test_item['item_code']
    item_name = test_item['item_name']
    quantity = test_item['quantity']
    
    if item_code not in items_data:
        print(f"❌ {item_code} not found in items data")
        return
    
    item_info = items_data[item_code]
    
    print(f"\nTesting with item: {item_code}")
    print(f"  Item Name: {item_name}")
    print(f"  Barcode: {item_info['barcode']}")
    print(f"  Price: {item_info['rate']}")
    print(f"  Colour: {item_info['colour'] or 'N/A'}")
    print(f"  Size: {item_info['size'] or 'N/A'}")
    print(f"  Quantity: {quantity}")
    
    # Generate TSPL
    barcode = item_info.get('barcode', item_code)
    display_name = item_name if item_name else item_info.get('name', 'Product')
    price = item_info.get('rate', '0')
    colour = item_info.get('colour', 'N/A')
    size = item_info.get('size', 'N/A')
    
    try:
        price_val = float(price) if price else 0
        price_display = f"R {price_val:.2f}"
    except:
        price_display = f"R {price}"
    
    product_desc = display_name
    if colour and colour != 'N/A':
        product_desc += f" - {colour}"
    
    # Text wrapping
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
    
    print(f"\nProduct description:")
    print(f"  Line 1: '{line1}' ({len(line1)} chars)")
    if line2:
        print(f"  Line 2: '{line2}' ({len(line2)} chars)")
    
    if line2:
        tspl = f'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nCLS\\nTEXT 8,16,\\"2\\",0,1,1,\\"{line1}\\"\\nTEXT 8,36,\\"2\\",0,1,1,\\"{line2}\\"\\nBAR 8,56,304,2\\nTEXT 8,66,\\"4\\",0,1,1,\\"{price_display}\\"\\nTEXT 8,95,\\"2\\",0,1,1,\\"Size: {size}\\"\\nTEXT 8,113,\\"2\\",0,1,1,\\"{colour}\\"\\nBARCODE 0,135,\\"39\\",65,0,0,1,1,\\"{barcode}\\"\\nTEXT 100,210,\\"1\\",0,1,1,\\"{item_code}\\"\\nPRINT 1\\n'
    else:
        tspl = f'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nCLS\\nTEXT 8,16,\\"2\\",0,1,1,\\"{line1}\\"\\nBAR 8,56,304,2\\nTEXT 8,66,\\"4\\",0,1,1,\\"{price_display}\\"\\nTEXT 8,95,\\"2\\",0,1,1,\\"Size: {size}\\"\\nTEXT 8,113,\\"2\\",0,1,1,\\"{colour}\\"\\nBARCODE 0,135,\\"39\\",65,0,0,1,1,\\"{barcode}\\"\\nTEXT 100,210,\\"1\\",0,1,1,\\"{item_code}\\"\\nPRINT 1\\n'
    
    print(f"\nGenerated TSPL command:")
    print(f"  {tspl[:100]}...")
    print(f"\nFull command to print:")
    print(f'echo -e "{tspl}"')
    
    # Generate stock count label
    print("\n" + "-"*60)
    print("Stock Count Label:")
    tspl_stock = f'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nCLS\\nTEXT 8,10,\\"3\\",0,1,1,\\"STOCK COUNT\\"\\nBAR 8,35,304,2\\nTEXT 8,45,\\"2\\",0,1,1,\\"Item: {item_code}\\"\\nTEXT 8,65,\\"2\\",0,1,1,\\"Printed: {quantity} labels\\"\\nBOX 8,90,304,200,2\\nTEXT 15,100,\\"2\\",0,1,1,\\"Actual Count:\\"\\nTEXT 15,130,\\"4\\",0,1,1,\\"_______________\\"\\nPRINT 1\\n'
    print(f'echo -e "{tspl_stock}"')

if __name__ == "__main__":
    try:
        items = test_items_csv()
        stock = test_stock_csv()
        test_label_generation(items, stock)
        
        print("\n" + "="*60)
        print("  DIAGNOSTIC COMPLETE")
        print("="*60)
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
