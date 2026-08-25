#!/usr/bin/env python3
"""
Advanced TSPL printer diagnostic and fix tool
Tests different TSPL configurations to fix alignment issues
"""

import subprocess
import time

PRINTER = "/dev/usb/lp2"  # Adjust if needed

def send_tspl(command: str, description: str = ""):
    """Send TSPL command to printer"""
    if description:
        print(f"\n{description}")
    try:
        cmd = f'echo -e "{command}" | sudo tee {PRINTER} > /dev/null'
        subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return True
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def test_1_basic():
    """Test 1: Current implementation"""
    print("\n" + "="*60)
    print("TEST 1: Current Implementation")
    print("="*60)
    
    tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 10,10,\\"3\\",0,1,1,\\"TEST 1: CURRENT\\"\\nBOX 5,5,315,235,2\\nTEXT 10,50,\\"2\\",0,1,1,\\"Check: Content within box?\\"\\nTEXT 10,70,\\"2\\",0,1,1,\\"Check: Box within label?\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing test label...")
    time.sleep(2)

def test_2_with_offset():
    """Test 2: With OFFSET command"""
    print("\n" + "="*60)
    print("TEST 2: With OFFSET 0 mm")
    print("="*60)
    
    tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nOFFSET 0 mm\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 10,10,\\"3\\",0,1,1,\\"TEST 2: OFFSET\\"\\nBOX 5,5,315,235,2\\nTEXT 10,50,\\"2\\",0,1,1,\\"Added OFFSET command\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing with OFFSET...")
    time.sleep(2)

def test_3_shift_reference():
    """Test 3: Different REFERENCE point"""
    print("\n" + "="*60)
    print("TEST 3: Shifted REFERENCE")
    print("="*60)
    
    tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nREFERENCE 0,2\\nCLS\\nTEXT 10,10,\\"3\\",0,1,1,\\"TEST 3: REF SHIFT\\"\\nBOX 5,5,315,235,2\\nTEXT 10,50,\\"2\\",0,1,1,\\"Reference shifted Y+2mm\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing with shifted reference...")
    time.sleep(2)

def test_4_shift_content():
    """Test 4: Shift content coordinates"""
    print("\n" + "="*60)
    print("TEST 4: Content Shifted Down")
    print("="*60)
    
    tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 10,20,\\"3\\",0,1,1,\\"TEST 4: SHIFTED\\"\\nBOX 5,15,315,245,2\\nTEXT 10,60,\\"2\\",0,1,1,\\"All Y coords +10 pixels\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing with content shifted...")
    time.sleep(2)

def test_5_smaller_gap():
    """Test 5: Different GAP setting"""
    print("\n" + "="*60)
    print("TEST 5: GAP 3 mm (instead of 2 mm)")
    print("="*60)
    
    tspl = 'SIZE 40 mm,30 mm\\nGAP 3 mm,0\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 10,10,\\"3\\",0,1,1,\\"TEST 5: GAP 3mm\\"\\nBOX 5,5,315,235,2\\nTEXT 10,50,\\"2\\",0,1,1,\\"Gap set to 3mm\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing with larger gap...")
    time.sleep(2)

def test_6_with_speed():
    """Test 6: With speed settings"""
    print("\n" + "="*60)
    print("TEST 6: With SPEED setting")
    print("="*60)
    
    tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nSPEED 4\\nDENSITY 8\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 10,10,\\"3\\",0,1,1,\\"TEST 6: SPEED\\"\\nBOX 5,5,315,235,2\\nTEXT 10,50,\\"2\\",0,1,1,\\"Speed 4, Density 8\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing with speed/density...")
    time.sleep(2)

def test_7_calibrate_then_print():
    """Test 7: Explicit calibration before print"""
    print("\n" + "="*60)
    print("TEST 7: Calibration + Print")
    print("="*60)
    
    # Send calibration
    cal = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nREFERENCE 0,0\\nHOME\\nCLS\\n'
    send_tspl(cal, "Sending calibration with HOME...")
    time.sleep(1)
    
    # Print label
    tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 10,10,\\"3\\",0,1,1,\\"TEST 7: CALIBRATED\\"\\nBOX 5,5,315,235,2\\nTEXT 10,50,\\"2\\",0,1,1,\\"After HOME command\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing after calibration...")
    time.sleep(2)

def test_8_full_product_label():
    """Test 8: Full product label with real data"""
    print("\n" + "="*60)
    print("TEST 8: Full Product Label (Realistic)")
    print("="*60)
    
    tspl = 'SIZE 40 mm,30 mm\\nGAP 2 mm,0\\nDIRECTION 0\\nREFERENCE 0,0\\nCLS\\nTEXT 8,16,\\"2\\",0,1,1,\\"Test Product Name\\"\\nBAR 8,56,304,2\\nTEXT 8,66,\\"4\\",0,1,1,\\"R 250.00\\"\\nTEXT 8,95,\\"2\\",0,1,1,\\"Size: M\\"\\nTEXT 8,113,\\"2\\",0,1,1,\\"Black\\"\\nBARCODE 0,135,\\"39\\",65,0,0,1,1,\\"4753481977\\"\\nTEXT 100,210,\\"1\\",0,1,1,\\"TEST-ITEM-M\\"\\nPRINT 1\\n'
    send_tspl(tspl, "Printing full product label...")
    time.sleep(2)

def main():
    print("\n" + "="*60)
    print("  ADVANCED TSPL ALIGNMENT DIAGNOSTIC")
    print("  Will print 8 test labels with different settings")
    print("="*60)
    print(f"\nUsing printer: {PRINTER}")
    print("\nEach test will:")
    print("  1. Show what it's testing")
    print("  2. Print a label")
    print("  3. Wait 2 seconds before next test")
    
    input("\nPress ENTER to start test sequence...")
    
    test_1_basic()
    test_2_with_offset()
    test_3_shift_reference()
    test_4_shift_content()
    test_5_smaller_gap()
    test_6_with_speed()
    test_7_calibrate_then_print()
    test_8_full_product_label()
    
    print("\n" + "="*60)
    print("  TEST SEQUENCE COMPLETE")
    print("="*60)
    print("\nPlease examine all 8 labels and note:")
    print("  • Which test(s) have proper alignment?")
    print("  • Are labels sliding up/down/left/right?")
    print("  • Are any labels being skipped?")
    print("  • Does content fit within label boundaries?")
    print("\nReport which test number(s) worked best!")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
