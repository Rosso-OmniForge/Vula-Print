# Vula! Print Label Printer

## Desktop Printing Application

A modern, branded desktop application for printing product labels directly from the Vula! Print inventory system.

### Features

- 🖨️ **Auto-Discovery**: Automatically scans for USB label printers
- 🎯 **Calibration**: Built-in printer calibration with test labels
- 🔄 **Auto-Sync**: Automatically fetches print requests from the server every 30 seconds
- 🚀 **Auto-Start + Auto-Connect**: Starts at login and reconnects to saved API URL
- ✅ **Modern UI**: Clean, branded PyQt6 interface
- 🔒 **Secure**: API key authentication
- 🧾 **POS Slip Printing**: Auto-polls POS queue and prints slips with cutter support
- 🧪 **Test POS Printer**: Prints a 6-item sample receipt for roll-change and update checks
- 📊 **Real-time Progress**: Live printing progress with status updates

### Installation

#### System Requirements

- **OS**: Debian 13 Trixie (or compatible Linux distro)
- **Printer**: USB-connected TSC/TSPL label printer (40mm x 30mm labels)
- **Python**: 3.10 or higher
- **Network**: Connection to Vula! Print backend server

#### Quick Install

```bash
cd /path/to/printer
chmod +x install_printer_app.sh
./install_printer_app.sh
```

This will:
1. Install system dependencies (Python, PyQt6, USB libraries)
2. Create a Python virtual environment
3. Install all required Python packages
4. Prompt for Vula API Base URL and API Key
5. Write secure runtime config to `.env` (permissions `600`)
6. Install and start the systemd user service

### Running the Application

#### Option 1: Using the Launcher (Recommended)

```bash
./launch_printer.sh
```

#### Option 2: Manual Launch

```bash
source venv/bin/activate
python3 vula_print_app.py
```

### Startup on Boot (systemd user service)

The installer creates and enables this user service:

```bash
~/.config/systemd/user/vula-print.service
```

Useful commands:

```bash
systemctl --user status vula-print
systemctl --user restart vula-print
systemctl --user stop vula-print
```

### Configuration

The app reads backend configuration from `.env` in the project root.

On install, you are prompted for:
- `PRINTER_API_BASE_URL`
- `PRINTER_API_KEY`

The installer writes these values into `.env` before starting the service.

Manual setup (optional):

```bash
cp .env.example .env
```

1. Set required values in `.env`:
   - `PRINTER_API_BASE_URL`
   - `PRINTER_API_KEY`
   - `PRINTER_USER_ID` (required for POS queue routing)

2. In the app UI:
   - Select the **Label printer** and **POS slip printer** separately.
   - Verify **PRINTER USER ID** is set correctly.
   - Use **Test Connection** to validate backend connectivity.
   - Use **Test POS Printer** to print/cut a 6-item sample slip.

### Usage Guide

#### First-Time Setup

1. **Launch the Application**
   ```bash
   ./launch_printer.sh
   ```

2. **Connect Printer**
   - Connect your USB label printer
   - Click "Scan for Printers"
   - Select your printer from the dropdown

3. **Configure API + POS Routing**
   - Ensure `.env` has API URL/key and `PRINTER_USER_ID`
   - In app, select POS slip printer device
   - Click "Test Connection"
   - Verify connection is successful (green indicator)

4. **Calibrate Printer**
   - Click "Calibrate & Test Print"
   - Verify the test label prints correctly
   - Confirm calibration when prompted

#### Printing Labels

1. **Refresh Queue**
   - Click "🔄 Refresh Queue" or wait for auto-refresh (30s)
   - Pending print requests will appear in the table

2. **Review Request**
   - Click on a request to see details
   - Review items, quantities, and source

3. **Print**
   - Click the "🖨️ Print" button for the desired request
   - Monitor progress in the progress bar
   - Request will be marked as completed automatically

#### Printing POS Slips

1. **Auto Mode**
   - POS worker polls every 5 seconds.
   - When pending slips exist for `PRINTER_USER_ID`, they are printed automatically.
   - Slip is completed on backend only after successful print write.

2. **Test Mode**
   - Click **Test POS Printer**.
   - App prints sample slip with 6 items and performs cut.
   - Use this after paper roll changes or app updates.

### UI Design

The application features a modern dark theme with Vula! Print branding:

- **Color Scheme**: Dark background (#1a1a1a) with orange accents (#ff6b35)
- **Header**: Displays the Primary Text Logo scaled to 60px height with real-time status indicators
  - Connection Status: Green (●) when connected, Red (●) when disconnected
  - Printer Status: Shows current printer state
- **Styled Components**:
  - Orange gradient buttons with hover effects
  - Dark-themed tables with orange selection highlights
  - Bordered panels with orange accents
  - Monospace details display with orange text
- **Professional Appearance**: Consistent branding throughout the interface matching Vula! Print's visual identity

### Auto-Generated Print Requests

Print requests are automatically created when:

#### From Procurement

- **Price Unchanged**: When stock is added, prints labels for only the new items
- **Price Changed**: When stock is added AND price changes, prints labels for ALL items (existing + new)

#### Manual Requests

- Staff can manually create print requests via the admin panel
- Useful for replacing damaged labels

### Troublesoting

#### Printer Not Found

```bash
# Check USB connection
ls -la /dev/usb/lp*

# Check USB devices
lsusb

# Ensure permissions
sudo usermod -a -G lp $USER
```

#### Connection Failed

- Verify backend server is running
- Check firewall settings
- Confirm API key matches backend configuration
- Test with: `curl -H "X-API-Key: YOUR_KEY" http://your-server:8000/admin/api/label-printing/pending`

#### Label Alignment Issues

1. Verify label size is exactly 40mm x 30mm
2. Adjust gap setting in printer (should be 2mm)
3. Run calibration again
4. Check `horizontal_shift_dots` in code if needed (line ~273 in `vula_print_app.py`)

#### PyQt6 Installation Issues

```bash
# On Debian 13, you may need:
sudo apt-get install python3-pyqt6 python3-pyqt6.qtcore python3-pyqt6.qtwidgets

# If pip install fails, use system packages:
pip install --no-deps PyQt6
```

### API Endpoints Used

- `GET /admin/api/label-printing/pending` - Fetch pending requests
- `GET /admin/api/label-printing/request/{id}` - Get request details
- `POST /admin/api/label-printing/complete` - Mark request as completed
- `GET /admin/api/pos-slips/pending` - Fetch pending POS slips
- `GET /admin/api/pos-slips/request/{id}` - Fetch POS slip detail payload
- `POST /admin/api/pos-slips/complete` - Mark POS slip as completed

### Security Notes

⚠️ **Important**: Never commit production API keys.

1. Generate a secure random key:
   ```bash
   openssl rand -base64 32
   ```

2. Set it in `.env` as `PRINTER_API_KEY`.

3. Keep backend key and app key aligned.

### Development

#### Running in Development Mode

```bash
# Activate virtual environment
source venv/bin/activate

# Run with debug output
python3 vula_print_app.py

# Or with verbose logging
python3 -v vula_print_app.py
```

#### Customizing Labels

Edit the `_generate_label_tspl()` method in `vula_print_app.py` to customize:
- Font sizes and positions
- Barcode settings (type, size, position)
- Label layout and content

### Support

For issues or questions, contact the OmniForge.

### License

Proprietary - OmniForge © 2026
# vulaprint
