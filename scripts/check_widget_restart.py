"""Keep the test supervisor alive while Godot replaces its own process."""
from pathlib import Path
import os
import subprocess
import time

root=Path(__file__).resolve().parents[1]
data=Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'godot/app_userdata/Strawberry'
for name in ['widget_restart_test.json','widget_restart_test.cfg']:
    (data/name).unlink(missing_ok=True)
report=root/'widget/restart_checks.json'
report.unlink(missing_ok=True)
log=Path('/tmp/strawberry-widget-restart-check.log')
with log.open('w') as output:
    process=subprocess.Popen(['godot','--headless','--path',str(root/'widget'),'--script','res://validate_widget_restart.gd','--','--ws=ws://127.0.0.1:1/ws'],stdout=output,stderr=subprocess.STDOUT)
    process.wait(timeout=15)
    deadline=time.monotonic()+10
    while not report.exists() and time.monotonic()<deadline:
        time.sleep(.1)
print(log.read_text())
if not report.exists():
    raise SystemExit('Restarted process did not produce its verification report')
print(report.read_text())
import json
assert json.loads(report.read_text())['passed']
