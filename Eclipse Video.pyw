import os
import sys
import traceback
from pathlib import Path

root = Path(__file__).resolve().parent
runtime = root/'runtime/packages/PySide6'
if runtime.is_dir():
    os.environ['QT_PLUGIN_PATH'] = str(runtime/'plugins')

try:
    from app import main
    raise SystemExit(main())
except Exception:
    data = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Eclipse Video'
    data.mkdir(parents=True, exist_ok=True)
    (data/'startup-error.log').write_text(traceback.format_exc(), encoding='utf-8')
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, 'Eclipse Video could not start. Reinstall the app, then try again.\n\nDetails are saved in ' + str(data/'startup-error.log'), 'Eclipse Video', 0x10)
    raise SystemExit(1)
