# DoctorsOn — VS Code setup

## Requirements
- Python 3.10 or newer
- VS Code with the Python extension

## Run on Windows
1. Extract this ZIP.
2. Open the `doctorson` folder in VS Code.
3. Open Terminal → New Terminal.
4. Run:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

5. Open http://127.0.0.1:5000 in your browser.

If PowerShell blocks activation, use Command Prompt and run:
```bat
.venv\Scripts\activate.bat
```

Or simply double-click `run.bat` (it creates the virtual environment and installs Flask
the first time, then starts the app).

## Admin
- Open http://127.0.0.1:5000/admin/login
- Default password: `admin123`
- Change it before using this app with real users. In PowerShell, set it for the
  current terminal before starting:

```powershell
$env:DOCTORSON_ADMIN_PASSWORD="your-strong-password"
python app.py
```

## Data
The app creates `doctorson.db` in this folder on first run. Keep this file to
preserve your hospitals, doctors, schedules, medicines, and orders.

## Notes
- Keep the terminal running while you use the local app. Closing the terminal
  stops the server. For background/always-on hosting, deploy it to a server or
  configure a Windows service; this ZIP is for running locally in VS Code.
- The included sample records are fictional.


## Tamil Nadu time and prescription AI

- All app dates, prescription checks and order timestamps use `Asia/Kolkata` by default (India Standard Time, UTC+05:30). Prescription dates are accepted for the offer only when they equal the order date in India or the immediately previous India-local calendar date.
- Pharmacy checkout supports a mobile camera photo or an attached PDF/JPG/JPEG/PNG/WEBP file (up to 8 MB). The AI extracts the prescriber's name, date and medicine text. It does **not** diagnose, prescribe or authorise dispensing.
- To enable AI extraction, configure an OpenAI API key on the server before starting Flask. PowerShell example:

```powershell
$env:OPENAI_API_KEY="your-api-key"
$env:DOCTORSON_TZ="Asia/Kolkata"
$env:DOCTORSON_RX_DISCOUNT_PERCENT="15"
python app.py
```

- The customer must explicitly consent before the configured OpenAI API receives a prescription file. The request disables response storage, but provider abuse-monitoring and other applicable retention policies may still apply. Without a key or consent, the upload is still saved for manual pharmacist review and no automatic discount is granted.
- In Admin → Doctors, edit each real doctor and tick **Verified DoctorsOn enrolment — eligible for prescription offer checks** only after your team has verified the doctor. Existing/sample doctor records are not automatically discount-eligible.
- The offer is applied to the medicine subtotal (not delivery) only when the AI extraction succeeded, the doctor exactly matches an enrolled eligible DoctorsOn doctor, and the prescription date is valid on the actual order day. The configured percentage is capped at 15%.
- Prescription files are stored outside the public `static` folder and can be downloaded only by an authenticated admin. For production, deploy behind HTTPS, restrict server access, set a retention/deletion policy for uploaded prescriptions, and obtain all required patient and provider consents. A pharmacist must review every prescription before dispensing.
