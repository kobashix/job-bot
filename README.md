# job-bot 2.0

## Windows 11 (PowerShell) setup and run

1. **Install Python 3.10+**
   - Download and install from https://www.python.org/downloads/windows/
   - During install, check **“Add Python to PATH.”**

2. **Create and activate a virtual environment**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install dependencies**
   ```powershell
   pip install playwright python-dotenv
   python -m playwright install chromium
   ```

4. **Configure your profile**
   - Edit `config.json` and fill in your profile values (name, email, phone, etc.).
   - Optionally, set environment overrides in PowerShell:
     ```powershell
     $env:APPLY_EMAIL = "you@example.com"
     $env:APPLY_PHONE = "555-555-5555"
     $env:APPLY_FULL_NAME = "Your Name"
     ```

5. **Start Edge or Chrome with remote debugging (separate profile)**
   PowerShell needs the call operator (`&`) when launching an executable with arguments.

   - **Edge (recommended):**
     ```powershell
     & "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
       --remote-debugging-port=9223 `
       --user-data-dir="C:\temp\job-bot-edge-profile"
     ```

   - **Chrome (separate profile, not your normal Chrome):**
     ```powershell
     & "C:\Program Files\Google\Chrome\Application\chrome.exe" `
       --remote-debugging-port=9223 `
       --user-data-dir="C:\temp\job-bot-chrome-profile"
     ```

   > If you prefer, you can also use `Start-Process`:
   > ```powershell
   > Start-Process -FilePath "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" -ArgumentList "--remote-debugging-port=9223 --user-data-dir=C:\temp\job-bot-edge-profile"
   > ```

6. **Run a single job apply**
   ```powershell
   python .\easy_apply_single.py "https://www.indeed.com/viewjob?jk=YOUR_JOB_ID"
   ```

7. **Training mode (optional)**
   ```powershell
   python .\easy_apply_single.py --train "https://www.indeed.com/viewjob?jk=YOUR_JOB_ID"
   ```

> **Tip:** If you change the remote debugging port or profile directory, update `config.json` accordingly.
