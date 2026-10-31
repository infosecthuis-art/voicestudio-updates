"""VoiceStudio 1.3 installer: stdlib only, preserves models/settings/environment."""
import base64, zlib, json, os, sys, subprocess, datetime, ast, time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, filedialog
PAYLOAD = "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"

def main():
    root=tk.Tk(); root.withdraw()
    target=Path(sys.argv[1]) if len(sys.argv)>1 else Path(r'D:\VoiceStudio')
    if not (target/'.venv/Scripts/python.exe').is_file():
        choice=filedialog.askdirectory(title='Selecteer je bestaande VoiceStudio-map')
        if not choice: return
        target=Path(choice)
    if not (target/'studio.py').is_file() or not (target/'.venv/Scripts/python.exe').is_file():
        messagebox.showerror('Studio niet gevonden','Selecteer de bestaande map met studio.py en .venv.'); return
    automatic = '--automatic' in sys.argv
    if automatic: time.sleep(1)
    if not automatic and not messagebox.askokcancel('VoiceStudio bijwerken',f'Sluit eerst de studio en wacht tot alle opnames klaar zijn.\n\nUpdate 1.3 installeren in:\n{target}\n\nJe stem, modellen, instellingen en audio blijven bewaard. Er wordt een reservekopie van de gewijzigde bestanden gemaakt.'):
        return
    previous={}
    try:
        files=json.loads(zlib.decompress(base64.b64decode(PAYLOAD)))
        ast.parse(files['studio.py'])
        stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup=target/'backups'/stamp; backup.mkdir(parents=True)
        for name in files:
            dest=target/name
            previous[name]=dest.read_bytes() if dest.exists() else None
            if previous[name] is not None: (backup/name).write_bytes(previous[name])
        for name, text in files.items():
            dest=target/name
            temporary=target/(name+'.new')
            temporary.write_text(text,encoding='utf-8')
            os.replace(temporary,dest)
    except Exception as exc:
        for name, data in previous.items():
            try:
                if data is not None: (target/name).write_bytes(data)
                else: (target/name).unlink(missing_ok=True)
            except OSError: pass
        messagebox.showerror('Update mislukt',str(exc)+'\nReservekopie: '+str(target/'backups'))
        return
    if automatic or messagebox.askyesno('Update klaar','VoiceStudio 1.3 is geïnstalleerd. Nu openen?'):
        subprocess.Popen([str(target/'.venv/Scripts/pythonw.exe'),str(target/'studio.py')],cwd=target)
    root.destroy()

if __name__=='__main__':
    main()
