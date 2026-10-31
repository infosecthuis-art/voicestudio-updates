"""Local English voice studio. Models: Resemble AI Chatterbox (separate download)."""
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent
for key, value in {'HF_HOME': ROOT/'cache/huggingface', 'TORCH_HOME': ROOT/'cache/torch', 'TEMP': ROOT/'tmp', 'TMP': ROOT/'tmp'}.items():
    Path(value).mkdir(parents=True, exist_ok=True)
    os.environ[key] = str(value)
import json
import queue
import threading
import traceback
import re
import subprocess
import sys
import time
import urllib.request
import hashlib
VERSION = "1.3"
UPDATE_BASE = "https://raw.githubusercontent.com/infosecthuis-art/voicestudio-updates/main/"
if sys.stdout is None or sys.stderr is None:
    log_stream = open(ROOT/'runtime.log', 'a', encoding='utf-8', buffering=1)
    if sys.stdout is None: sys.stdout = log_stream
    if sys.stderr is None: sys.stderr = log_stream
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def make_combined_reference(reference):
    """Experimental 6s reference: three 2s excerpts spread through the file.

    No speaker diarization, emotion classification or training. Intended only for
    clean recordings with one speaker. All excerpts fit within the short prompt.
    """
    import hashlib
    import numpy as np
    import librosa
    import soundfile as sf
    path = Path(reference).resolve()
    stat = path.stat()
    key = hashlib.sha256(f'{path}:{stat.st_size}:{stat.st_mtime_ns}:multi-v1'.encode()).hexdigest()[:24]
    folder = ROOT/'cache'/'references'; folder.mkdir(parents=True, exist_ok=True)
    result = folder/(key+'.wav')
    if result.is_file(): return str(result)
    audio, sr = librosa.load(str(path), sr=24000, mono=True)
    if not np.isfinite(audio).all() or len(audio) < sr*6:
        raise ValueError('Kies een helder bestand van minimaal 6 seconden voor meerdere plekken.')
    if len(audio) <= sr*10:
        return str(path)
    # Trim long outer silence and distribute excerpts over beginning/middle/end.
    audio, _ = librosa.effects.trim(audio, top_db=35)
    length = sr*2
    if len(audio) < length*3:
        raise ValueError('Te weinig bruikbaar geluid. Schakel meerdere plekken uit of kies een ander bestand.')
    pieces=[]; starts=[]
    for fraction in (0.12, 0.50, 0.88):
        center=int((len(audio)-length)*fraction)
        # Prefer a non-silent window near each target, without changing order.
        radius=min(sr*3, max(0, (len(audio)-length)//12))
        candidates=range(max(0,center-radius), min(len(audio)-length,center+radius)+1, max(1,sr//4))
        best=max(candidates, key=lambda start: float(np.mean(audio[start:start+length]**2)))
        piece=audio[best:best+length].copy()
        if float(np.sqrt(np.mean(piece**2))) < 0.0001:
            raise ValueError('Een gekozen plek bevat vrijwel alleen stilte. Gebruik een korter schoon bestand.')
        fade=min(120,len(piece)//2)
        piece[:fade] *= np.linspace(0,1,fade)
        piece[-fade:] *= np.linspace(1,0,fade)
        pieces.append(piece); starts.append(round(best/sr,2))
    sf.write(str(result), np.concatenate(pieces), sr)
    result.with_suffix('.json').write_text(json.dumps({'source':str(path),'starts_seconds':starts,'seconds_per_clip':2,'note':'Experimental mixed reference; no training or emotion analysis'}),encoding='utf-8')
    return str(result)

class Studio:
    def __init__(self, window):
        self.window = window
        self.model = None
        self.loaded_device = None
        self.condition_key = None
        self.events = queue.Queue()
        self.busy = False
        window.title('VoiceStudio 1.3 — Local English Narration')
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.geometry('850x690')
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill='both', expand=True)
        header = ttk.Frame(frame); header.pack(fill='x')
        ttk.Label(header, text='VoiceStudio 1.3', font=('Segoe UI', 24, 'bold')).pack(side='left')
        self.update_button = ttk.Button(header, text='Update', command=self.update)
        self.update_button.pack(side='right')
        ttk.Label(frame, text='Engelse voice-overs • lokaal • WAV-export').pack(anchor='w', pady=(0,15))
        self.reference = tk.StringVar()
        try:
            self.reference.set(json.loads((ROOT/'settings.json').read_text())['reference'])
        except (OSError, ValueError, KeyError):
            pass
        row = ttk.Frame(frame); row.pack(fill='x')
        ttk.Entry(row, textvariable=self.reference).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Kies stemvoorbeeld', command=self.choose).pack(side='right')
        ttk.Label(frame, text='Gebruik een helder Engels fragment van circa 10–20 seconden, zonder muziek.').pack(anchor='w', pady=5)
        self.multi_reference = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text='Meerdere plekken uit het bestand gebruiken (proef)', variable=self.multi_reference).pack(anchor='w')
        self.text = tk.Text(frame, wrap='word', height=16, font=('Segoe UI', 12), undo=True)
        self.text.pack(fill='both', expand=True, pady=10)
        self.text.insert('1.0', 'Every great story begins with a single moment. This is where ours begins.')
        controls = ttk.Frame(frame); controls.pack(fill='x')
        self.device = tk.StringVar(value='cuda')
        ttk.Label(controls, text='Rekenen:').pack(side='left')
        ttk.Combobox(controls, textvariable=self.device, values=['cuda','cpu'], state='readonly', width=8).pack(side='left', padx=8)
        ttk.Label(controls, text='Expressie:').pack(side='left')
        self.expression = tk.DoubleVar(value=0.5)
        self.expression_value = tk.StringVar(value='0.50')
        ttk.Scale(controls, from_=0.25, to=1.0, variable=self.expression, command=lambda v: self.expression_value.set(f'{float(v):.2f}')).pack(side='left', fill='x', expand=True)
        ttk.Label(controls, textvariable=self.expression_value, width=5).pack(side='left')
        self.button = ttk.Button(frame, text='Maak voice-over en sla op', command=self.generate)
        self.button.pack(fill='x', pady=15)
        self.status = tk.StringVar(value='Kies je stemvoorbeeld. De eerste generatie downloadt het model naar deze map.')
        ttk.Label(frame, textvariable=self.status, wraplength=790).pack(anchor='w')
        self.progress = ttk.Progressbar(frame, mode='indeterminate'); self.progress.pack(fill='x', pady=10)
        window.after(100, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo('Nog bezig', 'Wacht tot de voice-over klaar is voordat je afsluit.')
            return
        self.window.destroy()

    def update(self):
        if self.busy:
            messagebox.showinfo('Nog bezig', 'Wacht tot de voice-over klaar is voordat je bijwerkt.')
            return
        self.busy = True
        self.button.configure(state='disabled')
        self.update_button.configure(state='disabled')
        self.progress.start()
        self.status.set('Online naar updates zoeken…')
        threading.Thread(target=self.download_update, daemon=True).start()

    def download_update(self):
        try:
            def fetch(relative, limit):
                request = urllib.request.Request(UPDATE_BASE + relative, headers={'User-Agent':'VoiceStudio/'+VERSION,'Cache-Control':'no-cache'})
                with urllib.request.urlopen(request, timeout=30) as response:
                    if not response.geturl().startswith(UPDATE_BASE):
                        raise ValueError('Onverwachte downloadbestemming.')
                    data = response.read(limit+1)
                if len(data)>limit: raise ValueError('Updatebestand is te groot.')
                return data
            manifest = json.loads(fetch('update.json', 16000))
            version = manifest['version']
            if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,2}',version):
                raise ValueError('Ongeldig versienummer.')
            def parts(value): return tuple((list(map(int,value.split('.')))+[0,0,0])[:3])
            if parts(version) <= parts(VERSION):
                self.events.put(('current', 'Je hebt de nieuwste versie: '+VERSION))
                return
            filename = 'VoiceStudio-update-'+version+'.pyw'
            data = fetch('updates/'+filename, 2_000_000)
            if hashlib.sha256(data).hexdigest() != manifest['sha256']:
                raise ValueError('Downloadcontrole mislukt. Er is niets geïnstalleerd.')
            import ast
            ast.parse(data.decode('utf-8'))
            folder = ROOT/'cache'/'updates'; folder.mkdir(parents=True, exist_ok=True)
            destination = folder/filename
            temporary = destination.with_suffix('.part')
            temporary.write_bytes(data); os.replace(temporary,destination)
            self.events.put(('update_ready', str(destination)))
        except Exception:
            error = traceback.format_exc()
            (ROOT/'error.log').write_text(error,encoding='utf-8')
            self.events.put(('error', error))

    def choose(self):
        path = filedialog.askopenfilename(filetypes=[('Audio','*.wav *.mp3 *.flac'), ('Alle bestanden','*.*')])
        if path:
            self.reference.set(path)
            (ROOT/'settings.json').write_text(json.dumps({'reference':path}), encoding='utf-8')

    def generate(self):
        if self.busy: return
        text = self.text.get('1.0','end').strip()
        reference = self.reference.get().strip()
        if not text or not Path(reference).is_file():
            messagebox.showerror('Invoer ontbreekt','Vul tekst in en kies een bestaand stemvoorbeeld.'); return
        output_dir = ROOT/'output'; output_dir.mkdir(exist_ok=True)
        destination = filedialog.asksaveasfilename(initialdir=output_dir, defaultextension='.wav', filetypes=[('WAV audio','*.wav')])
        if not destination: return
        self.busy = True
        self.update_button.configure(state='disabled')
        self.button.configure(state='disabled'); self.progress.start()
        self.status.set('Model laden… De eerste keer kan dit enkele minuten duren.')
        threading.Thread(target=self.worker, args=(text,reference,destination,self.device.get(),self.expression.get(),self.multi_reference.get()), daemon=True).start()

    def worker(self, text, reference, destination, device, expression, multi_reference=False):
        try:
            if multi_reference:
                self.events.put(('status', 'Drie plekken uit de opname verzamelen…'))
                reference = make_combined_reference(reference)
            import torch
            import torchaudio
            from chatterbox.tts import ChatterboxTTS
            if device == 'cuda':
                if not torch.cuda.is_available():
                    raise RuntimeError('GPU is niet beschikbaar. Kies cpu of controleer de PyTorch-installatie.')
                torch.ones(1,device='cuda').add_(1)
            if self.model is None or self.loaded_device != device:
                self.model = None
                if torch.cuda.is_available(): torch.cuda.empty_cache()
                self.model = ChatterboxTTS.from_pretrained(device=device)
                self.loaded_device = device
                self.condition_key = None
            # Short chunks reduce runaway generation; concatenate with a short pause.
            sentences = re.split(r'(?<=[.!?])\s+|\n+', text)
            chunks = []
            for sentence in sentences:
                words = sentence.split()
                chunks.extend(' '.join(words[i:i+45]) for i in range(0,len(words),45))
            started = time.perf_counter()
            audio = []
            stat = Path(reference).stat()
            key = (str(Path(reference).resolve()), stat.st_mtime_ns, stat.st_size, expression, device)
            reuse = callable(getattr(self.model, 'prepare_conditionals', None))
            with torch.inference_mode():
                if reuse and self.condition_key != key:
                    self.events.put(('status', 'Stemvoorbeeld eenmaal verwerken…'))
                    self.model.prepare_conditionals(reference, exaggeration=expression)
                    self.condition_key = key
                for i, chunk in enumerate(chunks):
                    self.events.put(('status',f'Fragment {i+1} van {len(chunks)} maken…'))
                    wav = self.model.generate(chunk, audio_prompt_path=None if reuse else reference, exaggeration=expression, cfg_weight=0.3).detach().cpu()
                    if wav.ndim == 1: wav = wav.unsqueeze(0)
                    if audio: audio.append(torch.zeros(wav.shape[0], int(self.model.sr*0.18)))
                    audio.append(wav)
            torchaudio.save(destination, torch.cat(audio,dim=-1), self.model.sr)
            elapsed = time.perf_counter() - started
            self.events.put(('done',f'{destination} ({elapsed:.1f} sec generatie; model laden niet meegerekend)'))
        except Exception:
            error = traceback.format_exc()
            (ROOT/'error.log').write_text(error,encoding='utf-8')
            self.events.put(('error',error))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'status': self.status.set(value); continue
                self.busy = False; self.update_button.configure(state='normal'); self.progress.stop(); self.button.configure(state='normal')
                if kind == 'update_ready':
                    try:
                        interpreter = Path(sys.executable).with_name('pythonw.exe')
                        subprocess.Popen([str(interpreter),value,str(ROOT),'--automatic'],cwd=ROOT)
                    except OSError as exc:
                        messagebox.showerror('Update niet gestart',str(exc))
                        continue
                    self.window.destroy()
                    return
                if kind == 'current':
                    self.status.set(value)
                    messagebox.showinfo('Update',value)
                elif kind == 'done': self.status.set('Opgeslagen: '+value)
                else:
                    self.status.set('Er is een fout opgetreden. Details staan in error.log.')
                    messagebox.showerror('Generatie mislukt',value[-2200:])
        except queue.Empty: pass
        self.window.after(100,self.poll)

if __name__ == '__main__':
    root = tk.Tk()
    Studio(root)
    root.mainloop()
