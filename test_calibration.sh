#!/bin/bash
# Test script to manually send calibration commands to printer

echo "==================================="
echo "  TSPL Printer Calibration Test"
echo "==================================="
echo ""

# Find printers
PRINTERS=$(ls /dev/usb/lp* 2>/dev/null)

if [ -z "$PRINTERS" ]; then
    echo "❌ No printers found in /dev/usb/"
    exit 1
fi

echo "Found printers:"
for printer in $PRINTERS; do
    echo "  - $printer"
done
echo ""

# Select printer (default to first one for quick testing)
PRINTER=$(echo $PRINTERS | awk '{print $1}')
echo "Using printer: $PRINTER"
echo ""

echo "Sending calibration sequence..."
echo ""

# Method 1: Basic calibration with SIZE, GAP, and CLS
echo "Test 1: Basic calibration"
echo -e "SIZE 40 mm,30 mm\nGAP 2 mm,0\nDIRECTION 0\nREFERENCE 0,0\nCLS\n" | sudo tee $PRINTER > /dev/null
sleep 1

# Method 2: With FORMFEED to advance one label
echo "Test 2: Calibration + FORMFEED (should advance one blank label)"
echo -e "SIZE 40 mm,30 mm\nGAP 2 mm,0\nDIRECTION 0\nREFERENCE 0,0\nCLS\nFORMFEED\n" | sudo tee $PRINTER > /dev/null
sleep 1

# Method 3: Full auto-calibration with SET commands
echo "Test 3: Full auto-calibration"
echo -e "SIZE 40 mm,30 mm\nGAP 2 mm,0\nSET TEAR ON\nSET CUTTER OFF\nDIRECTION 0\nREFERENCE 0,0\nCLS\n" | sudo tee $PRINTER > /dev/null
sleep 1

# Method 4: Simple test label after calibration
echo "Test 4: Printing test label"
echo -e "SIZE 40 mm,30 mm\nGAP 2 mm,0\nDIRECTION 0\nREFERENCE 0,0\nOFFSET 0 mm\nCLS\nTEXT 10,10,\"3\",0,1,1,\"ALIGNMENT TEST\"\nBAR 10,40,300,2\nTEXT 10,50,\"2\",0,1,1,\"Check label edges\"\nTEXT 10,70,\"2\",0,1,1,\"Content should be\"\nTEXT 10,90,\"2\",0,1,1,\"within label area\"\nBOX 5,5,315,235,2\nPRINT 1\n" | sudo tee $PRINTER > /dev/null

echo ""
echo "✓ Calibration complete!"
echo ""
echo "Please check the printed test label:"
echo "  1. Is the box within the label boundaries?"
echo "  2. Is text clearly visible and not cut off?"
echo "  3. Did it skip any labels?"
echo ""
