#!/usr/bin/env python3
"""
Test script to show TSPL commands for verification
"""

def show_tspl_commands():
    print("\n" + "="*60)
    print("  TSPL COMMAND COMPARISON")
    print("="*60)
    
    # Old format (without DIRECTION and REFERENCE)
    old_tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nCLS\\nTEXT 8,16,\\"2\\",0,1,1,\\"Test Product\\"\\nBAR 8,56,304,2\\nTEXT 8,66,\\"4\\",0,1,1,\\"R 250.00\\"\\nTEXT 8,95,\\"2\\",0,1,1,\\"Size: M\\"\\nTEXT 8,113,\\"2\\",0,1,1,\\"Black\\"\\nBARCODE 0,135,\\"39\\",65,0,0,1,1,\\"4753481977\\"\\nTEXT 100,210,\\"1\\",0,1,1,\\"TEST-ITEM-M\\"\\nPRINT 1\\n'
    
    # New format (with DIRECTION and REFERENCE)
    new_tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 8,16,\\"2\\",0,1,1,\\"Test Product\\"\\nBAR 8,56,304,2\\nTEXT 8,66,\\"4\\",0,1,1,\\"R 250.00\\"\\nTEXT 8,95,\\"2\\",0,1,1,\\"Size: M\\"\\nTEXT 8,113,\\"2\\",0,1,1,\\"Black\\"\\nBARCODE 0,135,\\"39\\",65,0,0,1,1,\\"4753481977\\"\\nTEXT 100,210,\\"1\\",0,1,1,\\"TEST-ITEM-M\\"\\nPRINT 1\\n'
    
    print("\n📋 OLD FORMAT (Missing calibration):")
    print("-" * 60)
    print(old_tspl.replace('\\n', '\n'))
    
    print("\n\n📋 NEW FORMAT (With DIRECTION and REFERENCE):")
    print("-" * 60)
    print(new_tspl.replace('\\n', '\n'))
    
    print("\n\n🔧 KEY CHANGES:")
    print("-" * 60)
    print("  ✓ Added: DIRECTION 0    - Sets print direction")
    print("  ✓ Added: REFERENCE 0,0  - Sets label reference point")
    print("\nThese commands help the printer:")
    print("  • Properly detect label gaps")
    print("  • Align content to the correct position")
    print("  • Prevent skipping labels")
    print("="*60)
    
    print("\n\n🧪 TO TEST:")
    print("="*60)
    print("1. Run: python3 label_printer.py")
    print("2. Select option 7 to calibrate printer")
    print("3. Then select option 1 to print a test label")
    print("4. Check if label is properly aligned")
    print("="*60)

if __name__ == "__main__":
    show_tspl_commands()
