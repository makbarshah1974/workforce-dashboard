@echo off
REM Opens TCP port 5000 for the Workforce Dashboard so other PCs and phones
REM on the same Wi-Fi can reach it. Run as Administrator.
REM (Right-click this file -> "Run as administrator")
netsh advfirewall firewall add rule name="Workforce Dashboard (TCP 5000)" dir=in action=allow protocol=TCP localport=5000 profile=private
if %errorlevel%==0 (
    echo.
    echo [OK] Firewall rule added for TCP port 5000 (private networks).
) else (
    echo.
    echo [!] Failed. Make sure you ran this file as Administrator.
)
echo.
echo Other devices on the same Wi-Fi can now open:
echo   http://THIS_PC_LAN_IP:5000
echo.
echo To find THIS_PC_LAN_IP, run:  ipconfig | findstr IPv4
pause