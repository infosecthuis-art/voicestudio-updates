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
VERSION = "1.9.1"
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


def split_script(text, max_words=45):
    """Pack short sentences together while keeping every word and a hard limit."""
    if max_words < 1: raise ValueError('max_words must be positive')
    chunks, current = [], []
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', text):
        words = sentence.split()
        if not words: continue
        if current and len(current)+len(words)>max_words:
            chunks.append(' '.join(current)); current=[]
        while len(words)>max_words:
            chunks.append(' '.join(words[:max_words])); words=words[max_words:]
        current.extend(words)
    if current: chunks.append(' '.join(current))
    return chunks


def apply_volume(wav, percent):
    """Apply output gain once; cap peaks above full scale without hard clipping."""
    if float(percent) == 100.0: return wav
    result = wav * (float(percent)/100.0)
    peak = float(result.abs().max().item())
    if peak > 0.99: result = result * (0.99/peak)
    return result


def narration_plan(text, storyteller=False):
    """Return text chunks with silence before each chunk, preserving all words.

    Story mode uses paragraph boundaries only: no semantic/emotion analysis.
    Single newlines remain ordinary line breaks; blank lines define paragraphs.
    """
    if not storyteller:
        return [(chunk,0.0 if i==0 else 0.18) for i,chunk in enumerate(split_script(text))]
    plan=[]
    for paragraph in re.split(r'\n\s*\n',text):
        for index,chunk in enumerate(split_script(paragraph)):
            pause=0.0 if not plan else (0.50 if index==0 else 0.18)
            plan.append((chunk,pause))
    return plan

def preview_script(text, limit=25):
    tokens=list(re.finditer(r'\S+',text))
    if not tokens:return ''
    return text[:tokens[min(limit,len(tokens))-1].end()].strip()


def rvc_voice():
    state=ROOT/'rvc/voice.json'
    if not state.is_file():raise RuntimeError('Er is nog geen getraind RVC-model. Open RVC-stemmodel en voer de drie stappen uit.')
    voice=json.loads(state.read_text(encoding='utf-8'))
    for key in ('model','index'):
        if not Path(voice.get(key,'')).is_file():raise RuntimeError('Het getrainde RVC-model of de index ontbreekt. Open RVC-stemmodel.')
    return voice

def convert_rvc(destination, index_rate):
    import shutil
    voice=rvc_voice();engine=ROOT/'rvc/engine';python=ROOT/'rvc/.venv/Scripts/python.exe'
    if not python.is_file():raise RuntimeError('De aparte RVC-omgeving ontbreekt.')
    folder=ROOT/'cache/rvc-conversion';folder.mkdir(parents=True,exist_ok=True)
    source=folder/('tts-'+str(time.time_ns())+'.wav');shutil.copyfile(destination,source)
    env=os.environ.copy()
    env.update({'HF_HOME':str(ROOT/'rvc/cache/huggingface'),'TORCH_HOME':str(ROOT/'rvc/cache/torch'),
                'TEMP':str(ROOT/'rvc/tmp'),'TMP':str(ROOT/'rvc/tmp'),'PYTHONIOENCODING':'utf-8',
                'PYTHONPATH':str(engine),'weight_root':str(engine/'assets/weights'),
                'index_root':str(engine/'assets/indices'),'rmvpe_root':str(engine/'assets/rmvpe')})
    env['PATH']=str(engine)+os.pathsep+env.get('PATH','')
    with open(ROOT/'rvc/conversion.log','w',encoding='utf-8') as log:
        result=subprocess.run([str(python),'infer/cli.py','--model',voice['model'],'--index',voice['index'],
            '--input',str(source),'--output',str(destination),'--index-rate',str(index_rate),'--f0-method','rmvpe','--overwrite'],
            cwd=engine,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError('RVC-conversie mislukt. Je oorspronkelijke TTS staat in '+str(source)+'; details in rvc/conversion.log.')
    source.unlink(missing_ok=True)

class Studio:
    def __init__(self, window):
        self.window = window
        self.model = None
        self.loaded_device = None
        self.condition_key = None
        self.events = queue.Queue()
        self.busy = False
        self.last_output = None
        self.last_preview = None
        window.title('VoiceStudio 1.9.1 — Jouw voice-overstudio')
        window.geometry('1160x800')
        window.minsize(980, 700)
        window.configure(bg='#0b1120')
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.option_add('*Font', ('Segoe UI', 10))
        style = ttk.Style(window)
        style.theme_use('clam')
        style.configure('.', background='#0b1120', foreground='#e6edf7', font=('Segoe UI', 10))
        style.configure('TFrame', background='#0b1120')
        style.configure('Card.TFrame', background='#141e30')
        style.configure('TLabel', background='#0b1120', foreground='#e6edf7')
        style.configure('Card.TLabel', background='#141e30', foreground='#e6edf7')
        style.configure('Muted.TLabel', background='#141e30', foreground='#a7b6cc', font=('Segoe UI', 9))
        style.configure('Heading.TLabel', background='#141e30', foreground='#f4f7ff', font=('Segoe UI', 13, 'bold'))
        style.configure('TButton', background='#23334c', foreground='#e6edf7', borderwidth=0, padding=(12,9))
        style.map('TButton', background=[('active','#344b6b'),('disabled','#192438')], foreground=[('disabled','#73839b')])
        style.configure('Primary.TButton', background='#8b7cf8', foreground='#ffffff', font=('Segoe UI',11,'bold'), padding=(16,13))
        style.map('Primary.TButton', background=[('active','#a195ff'),('disabled','#35405d')], foreground=[('disabled','#96a4ba')])
        style.configure('TEntry', fieldbackground='#0c1424', foreground='#e6edf7', insertcolor='#ffffff', bordercolor='#30425d', padding=8)
        style.configure('TSpinbox', fieldbackground='#0c1424', foreground='#e6edf7', insertcolor='#ffffff', arrowcolor='#d5dded', padding=6)
        style.configure('TCombobox', fieldbackground='#0c1424', foreground='#e6edf7', arrowcolor='#d5dded', padding=6)
        style.map('TCombobox', fieldbackground=[('readonly','#0c1424')], foreground=[('readonly','#e6edf7')])
        window.option_add('*TCombobox*Listbox.background','#142035')
        window.option_add('*TCombobox*Listbox.foreground','#e6edf7')
        style.configure('TCheckbutton', background='#141e30', foreground='#cad5e7', padding=(0,5))
        style.map('TCheckbutton',background=[('active','#141e30')],foreground=[('disabled','#73839b')])
        style.configure('Horizontal.TProgressbar', background='#8b7cf8', troughcolor='#223047', borderwidth=0, thickness=7)
        style.configure('Vertical.TScrollbar', background='#33435c', troughcolor='#0e1728', borderwidth=0, arrowcolor='#a7b6cc')
        outer = ttk.Frame(window, padding=(24,20)); outer.pack(fill='both',expand=True)
        header = ttk.Frame(outer); header.pack(fill='x',pady=(0,20))
        branding = ttk.Frame(header); branding.pack(side='left')
        ttk.Label(branding,text='VoiceStudio',font=('Segoe UI',26,'bold')).pack(anchor='w')
        ttk.Label(branding,text='Jouw verhaal. Jouw vaste stem.',foreground='#a7b6cc').pack(anchor='w',pady=(3,0))
        self.update_button = ttk.Button(header,text='Update',command=self.update); self.update_button.pack(side='right')
        ttk.Button(header,text='RVC-stemmodel',command=self.open_rvc).pack(side='right',padx=(0,8))
        ttk.Label(header,text='LOKAAL  •  v1.9.1',foreground='#a7b6cc',font=('Segoe UI',9)).pack(side='right',padx=18)
        body = ttk.Frame(outer); body.pack(fill='both',expand=True)
        body.columnconfigure(0,weight=1); body.columnconfigure(1,weight=0,minsize=310); body.rowconfigure(0,weight=1)
        editor = ttk.Frame(body,style='Card.TFrame',padding=18); editor.grid(row=0,column=0,sticky='nsew',padx=(0,16))
        editor.rowconfigure(2,weight=1); editor.columnconfigure(0,weight=1)
        title = ttk.Frame(editor,style='Card.TFrame'); title.grid(row=0,column=0,columnspan=2,sticky='ew')
        ttk.Label(title,text='01  Je script',style='Heading.TLabel').pack(side='left')
        ttk.Button(title,text='Tekst openen',command=self.import_text).pack(side='right')
        ttk.Label(editor,text='Plak je Engelse tekst en geef je verhaal een stem.',style='Muted.TLabel').grid(row=1,column=0,columnspan=2,sticky='w',pady=(8,14))
        self.text = tk.Text(editor,wrap='word',height=12,width=40,font=('Segoe UI',12),undo=True,
                            bg='#0e1728',fg='#e6edf7',insertbackground='#b6a9ff',selectbackground='#514882',
                            relief='flat',borderwidth=0,padx=16,pady=16,spacing1=3,spacing3=5)
        self.text.grid(row=2,column=0,sticky='nsew')
        scroll=ttk.Scrollbar(editor,orient='vertical',command=self.text.yview); scroll.grid(row=2,column=1,sticky='ns')
        self.text.configure(yscrollcommand=scroll.set)
        self.script_stats=tk.StringVar(value='0 woorden')
        ttk.Label(editor,textvariable=self.script_stats,style='Muted.TLabel').grid(row=3,column=0,columnspan=2,sticky='w',pady=(12,0))
        self.text.bind('<<Modified>>',self.script_changed)
        try: draft=(ROOT/'draft.txt').read_text(encoding='utf-8')
        except OSError: draft='Every great story begins with a single moment. This is where ours begins.'
        self.text.insert('1.0',draft)
        self.text.edit_modified(False); self.script_changed()
        side_shell=ttk.Frame(body,style='Card.TFrame');side_shell.grid(row=0,column=1,sticky='nsew')
        side_canvas=tk.Canvas(side_shell,bg='#141e30',highlightthickness=0,width=310,height=350)
        side_scroll=ttk.Scrollbar(side_shell,orient='vertical',command=side_canvas.yview)
        side_scroll.pack(side='right',fill='y');side_canvas.pack(side='left',fill='both',expand=True)
        side_canvas.configure(yscrollcommand=side_scroll.set)
        side=ttk.Frame(side_canvas,style='Card.TFrame',padding=18)
        side_window=side_canvas.create_window((0,0),window=side,anchor='nw')
        side.bind('<Configure>',lambda event:side_canvas.configure(scrollregion=side_canvas.bbox('all')))
        side_canvas.bind('<Configure>',lambda event:side_canvas.itemconfigure(side_window,width=event.width))
        ttk.Label(side,text='02  Je stem',style='Heading.TLabel').pack(anchor='w')
        ttk.Button(side,text='RVC-stemmodel · installeren / trainen',command=self.open_rvc).pack(fill='x',pady=(10,4))
        ttk.Label(side,text='Je vaste stemvoorbeeld',style='Muted.TLabel').pack(anchor='w',pady=(16,7))
        self.reference=tk.StringVar()
        try:
            preferences=json.loads((ROOT/'settings.json').read_text())
            if not isinstance(preferences,dict): preferences={}
        except (OSError,ValueError): preferences={}
        self.reference.set(str(preferences.get('reference','')))
        ttk.Entry(side,textvariable=self.reference,width=26).pack(fill='x')
        ttk.Button(side,text='Kies stemvoorbeeld',command=self.choose).pack(fill='x',pady=(8,8))
        ttk.Label(side,text='Helder Engels, één spreker, zonder muziek.',style='Muted.TLabel',wraplength=270).pack(anchor='w')
        self.multi_reference=tk.BooleanVar(value=False)
        ttk.Checkbutton(side,text='Meerdere plekken gebruiken (proef)',variable=self.multi_reference).pack(anchor='w',pady=(8,18))
        ttk.Separator(side).pack(fill='x',pady=(0,16))
        ttk.Label(side,text='Voordracht',style='Heading.TLabel').pack(anchor='w')
        self.storyteller=tk.BooleanVar(value=False)
        ttk.Button(side,text='Storyteller instellen',command=self.apply_storyteller).pack(fill='x',pady=(10,4))
        ttk.Checkbutton(side,text='Vertelpauzes tussen alinea’s',variable=self.storyteller).pack(anchor='w')
        ttk.Label(side,text='Startpreset voor verhalen. Gebruik een lege regel voor een alinea.',style='Muted.TLabel',wraplength=265).pack(anchor='w',pady=(2,6))
        ttk.Label(side,text='Expressie',style='Card.TLabel').pack(anchor='w',pady=(14,0))
        self.expression=tk.DoubleVar(value=0.5); self.expression_value=tk.StringVar(value='0.50')
        self.expression_scale=tk.Scale(side,from_=0.25,to=1.0,resolution=0.01,bigincrement=0.01,
            orient='horizontal',showvalue=False,variable=self.expression,command=self.expression_slid,
            bg='#141e30',fg='#cad5e7',troughcolor='#2b3c56',activebackground='#b3a6ff',
            highlightthickness=0,relief='flat',borderwidth=0,sliderrelief='flat',sliderlength=24,takefocus=True)
        self.expression_scale.pack(fill='x',pady=(5,0))
        captions=ttk.Frame(side,style='Card.TFrame');captions.pack(fill='x')
        ttk.Label(captions,text='Rustiger',style='Muted.TLabel').pack(side='left')
        ttk.Label(captions,text='Expressiever',style='Muted.TLabel').pack(side='right')
        exact=ttk.Frame(side,style='Card.TFrame');exact.pack(fill='x',pady=(10,10))
        ttk.Button(exact,text='−',width=3,command=lambda:self.set_expression(self.expression.get()-0.01)).pack(side='left')
        self.expression_input=ttk.Spinbox(exact,from_=0.25,to=1.0,increment=0.01,format='%.2f',width=7,textvariable=self.expression_value,command=self.commit_expression)
        self.expression_input.pack(side='left',fill='x',expand=True,padx=8)
        self.expression_input.bind('<Return>',lambda event:self.commit_expression())
        self.expression_input.bind('<FocusOut>',lambda event:self.commit_expression())
        ttk.Button(exact,text='+',width=3,command=lambda:self.set_expression(self.expression.get()+0.01)).pack(side='right')
        ttk.Label(side,text='Tip: begin met 0,65 voor meer expressie.',style='Muted.TLabel',wraplength=270).pack(anchor='w',pady=(0,12))
        self.volume=tk.DoubleVar(value=100.0);self.volume_value=tk.StringVar(value='100')
        self.variation=tk.DoubleVar(value=0.8);self.variation_value=tk.StringVar(value='0.80')
        for label,variable,display,low,high,step,digits,caption in [
            ('Volume (%)',self.volume,self.volume_value,0,150,1,'%.0f','100% = origineel · pieken worden begrensd'),
            ('Variatie',self.variation,self.variation_value,0.3,1.2,0.01,'%.2f','Links: voorspelbaarder · rechts: vrijer')]:
            ttk.Label(side,text=label,style='Card.TLabel').pack(anchor='w',pady=(12,2))
            line=ttk.Frame(side,style='Card.TFrame');line.pack(fill='x')
            tk.Scale(line,from_=low,to=high,resolution=step,bigincrement=step,orient='horizontal',showvalue=False,
                variable=variable,command=lambda value,d=display,f=digits:d.set(f % float(value)),
                bg='#141e30',fg='#cad5e7',troughcolor='#2b3c56',activebackground='#b3a6ff',highlightthickness=0,
                relief='flat',borderwidth=0,sliderrelief='flat',sliderlength=24,length=155).pack(side='left',fill='x',expand=True)
            entry=ttk.Spinbox(line,from_=low,to=high,increment=step,format=digits,width=6,textvariable=display,command=self.commit_sound_controls)
            entry.pack(side='right',padx=(8,0))
            entry.bind('<Return>',lambda event:self.commit_sound_controls())
            entry.bind('<FocusOut>',lambda event:self.commit_sound_controls())
            ttk.Label(side,text=caption,style='Muted.TLabel',wraplength=265).pack(anchor='w')
        ttk.Button(side,text='Standaardinstellingen',command=self.reset_controls).pack(fill='x',pady=(14,4))
        ttk.Separator(side).pack(fill='x',pady=(14,8))
        self.rvc_enabled=tk.BooleanVar(value=False)
        self.rvc_index=tk.DoubleVar(value=0.75)
        ttk.Checkbutton(side,text='Getrainde RVC-stem toepassen',variable=self.rvc_enabled,command=self.check_rvc).pack(anchor='w')
        ttk.Label(side,text='Opnamekenmerken (RVC-index)',style='Card.TLabel').pack(anchor='w',pady=(8,0))
        tk.Scale(side,from_=0,to=1,resolution=0.05,orient='horizontal',variable=self.rvc_index,
            bg='#141e30',fg='#cad5e7',troughcolor='#2b3c56',highlightthickness=0).pack(fill='x')
        ttk.Label(side,text='Pas beschikbaar na training. Hogere index volgt kenmerken uit de trainingsopname sterker; dit is geen ElevenLabs-kloonkracht.',style='Muted.TLabel',wraplength=265).pack(anchor='w')
        device_row=ttk.Frame(side,style='Card.TFrame');device_row.pack(fill='x',pady=(8,0))
        ttk.Label(device_row,text='Rekenen',style='Card.TLabel').pack(side='left')
        self.device=tk.StringVar(value='cuda')
        ttk.Combobox(device_row,textvariable=self.device,values=['cuda','cpu'],state='readonly',width=8).pack(side='right')
        ttk.Label(side,text='cuda = videokaart  ·  cpu = processor',style='Muted.TLabel').pack(anchor='w',pady=(8,0))
        footer=ttk.Frame(outer,style='Card.TFrame',padding=16);footer.pack(fill='x',pady=(16,0))
        action=ttk.Frame(footer,style='Card.TFrame');action.pack(fill='x')
        self.button=ttk.Button(action,text='Maak voice-over',style='Primary.TButton',command=self.generate);self.button.pack(side='left')
        self.preview_button=ttk.Button(action,text='Luistervoorbeeld',command=self.preview);self.preview_button.pack(side='left',padx=(10,0))
        ttk.Button(action,text='Stop',width=4,command=self.stop_preview).pack(side='left',padx=(6,0))
        self.listen_button=ttk.Button(action,text='Beluister resultaat',command=self.play_output,state='disabled');self.listen_button.pack(side='left',padx=10)
        ttk.Button(action,text='Open uitvoermap',command=self.open_output).pack(side='right')
        self.status=tk.StringVar(value='Klaar voor je volgende verhaal. Kies je stem en vul je script in.')
        self.status_label=ttk.Label(footer,textvariable=self.status,style='Muted.TLabel',wraplength=1000)
        self.status_label.pack(fill='x',pady=(14,8))
        self.status_label.bind('<Configure>',lambda event:self.status_label.configure(wraplength=max(200,event.width-8)))
        self.progress=ttk.Progressbar(footer,mode='indeterminate');self.progress.pack(fill='x')
        self.restore_controls(preferences)
        window.after(100,self.poll)

    def open_rvc(self):
        if self.busy:
            messagebox.showinfo('Nog bezig','Laat de huidige opname eerst afronden.');return
        self.save_draft();self.stop_preview()
        try:
            python=ROOT/'.venv/Scripts/pythonw.exe'
            subprocess.Popen([str(python),str(ROOT/'rvc_manager.py')],cwd=ROOT)
        except OSError as exc:
            messagebox.showerror('RVC niet geopend',str(exc));return
        self.window.destroy()

    def check_rvc(self):
        if self.rvc_enabled.get():
            try:rvc_voice()
            except Exception as exc:
                self.rvc_enabled.set(False)
                messagebox.showinfo('RVC-model nodig',str(exc))
        self.save_preferences()

    def restore_controls(self, preferences):
        import math
        for key,var,display,default,low,high,fmt in [
            ('expression',self.expression,self.expression_value,0.5,0.25,1.0,'%.2f'),
            ('volume',self.volume,self.volume_value,100.0,0,150,'%.0f'),
            ('variation',self.variation,self.variation_value,0.8,0.3,1.2,'%.2f')]:
            try:
                value=float(preferences.get(key,default))
                if not math.isfinite(value): value=default
            except (ValueError,TypeError): value=default
            value=max(low,min(high,value));var.set(value);display.set(fmt % value)
        self.rvc_enabled.set(bool(preferences.get('rvc_enabled',False)) and (ROOT/'rvc/voice.json').is_file())
        try: self.rvc_index.set(max(0,min(1,float(preferences.get('rvc_index',0.75)))))
        except (TypeError,ValueError): self.rvc_index.set(0.75)
        self.multi_reference.set(bool(preferences.get('multi_reference',False)))
        self.storyteller.set(bool(preferences.get('storyteller',False)))
        device=preferences.get('device','cuda')
        self.device.set(device if device in ('cuda','cpu') else 'cuda')

    def commit_sound_controls(self):
        import math
        values=[]
        for variable,display,low,high,fmt in [
            (self.volume,self.volume_value,0,150,'%.0f'),
            (self.variation,self.variation_value,0.3,1.2,'%.2f')]:
            try:
                value=float(display.get().replace(',','.'))
                if not math.isfinite(value): raise ValueError('Ongeldig getal')
            except (ValueError,tk.TclError):
                display.set(fmt % variable.get())
                return False
            values.append((variable,display,max(low,min(high,value)),fmt))
        for variable,display,value,fmt in values:
            value=float(fmt % value);variable.set(value);display.set(fmt % value)
        return True

    def apply_storyteller(self):
        if self.busy:
            messagebox.showinfo('Nog bezig','Laat de huidige opname eerst afronden.');return
        self.set_expression(0.60)
        self.variation.set(0.65);self.variation_value.set('0.65')
        self.volume.set(100.0);self.volume_value.set('100')
        self.storyteller.set(True)
        self.save_preferences()
        self.status.set('Storyteller ingesteld · expressie 0,60 · variatie 0,65 · langere pauze bij lege regels. Probeer Luistervoorbeeld.')

    def reset_controls(self):
        self.set_expression(0.5)
        self.storyteller.set(False)
        self.rvc_enabled.set(False)
        self.rvc_index.set(0.75)
        self.volume.set(100.0);self.volume_value.set('100')
        self.variation.set(0.8);self.variation_value.set('0.80')
        self.save_preferences()

    def save_preferences(self):
        try:
            settings={'reference':self.reference.get(),'expression':self.expression.get(),
                      'volume':self.volume.get(),'variation':self.variation.get(),
                      'device':self.device.get(),'multi_reference':self.multi_reference.get(),'storyteller':self.storyteller.get(),'rvc_enabled':self.rvc_enabled.get(),'rvc_index':self.rvc_index.get()}
            temporary=ROOT/'settings.json.new'
            temporary.write_text(json.dumps(settings),encoding='utf-8')
            os.replace(temporary,ROOT/'settings.json')
        except (OSError,tk.TclError): pass

    def preview(self):
        if self.busy: return
        if not self.commit_expression() or not self.commit_sound_controls():
            messagebox.showerror('Expressie','Controleer de getallen bij expressie, volume en variatie.'); return
        reference=self.reference.get().strip()
        if not Path(reference).is_file():
            messagebox.showerror('Stemvoorbeeld','Kies eerst een bestaand stemvoorbeeld.'); return
        try: text=self.text.get('sel.first','sel.last').strip()
        except tk.TclError: text=self.text.get('1.0','end').strip()
        if not text:
            messagebox.showinfo('Tekst ontbreekt','Vul eerst Engelse tekst in.'); return
        sample=preview_script(text,limit=25)
        stat=Path(reference).stat()
        key=hashlib.sha256(json.dumps([VERSION,sample,str(Path(reference).resolve()),stat.st_mtime_ns,stat.st_size,self.expression.get(),self.device.get(),self.multi_reference.get(),self.volume.get(),self.variation.get(),self.storyteller.get(),self.rvc_enabled.get(),self.rvc_index.get(),(ROOT/'rvc/voice.json').stat().st_mtime_ns if (ROOT/'rvc/voice.json').exists() else 0]).encode()).hexdigest()[:24]
        folder=ROOT/'cache'/'previews';folder.mkdir(parents=True,exist_ok=True)
        destination=folder/(key+'.wav')
        if destination.is_file() and destination.with_suffix('.ready').is_file():
            self.last_preview=str(destination)
            self.status.set('Luistervoorbeeld afspelen · maximaal 25 woorden · gekozen stem en expressie')
            self.play_preview(); return
        self.stop_preview()
        self.save_draft()
        self.busy=True
        self.button.configure(state='disabled');self.preview_button.configure(state='disabled');self.update_button.configure(state='disabled')
        self.progress.configure(mode='indeterminate',value=0);self.progress.start()
        self.status.set('Kort luistervoorbeeld maken… De eerste keer moet het model laden.')
        threading.Thread(target=self.worker,args=(sample,reference,str(destination),self.device.get(),self.expression.get(),self.multi_reference.get(),'preview',self.volume.get(),self.variation.get(),self.storyteller.get(),self.rvc_enabled.get(),self.rvc_index.get()),daemon=True).start()

    def play_preview(self):
        try:
            import winsound
            winsound.PlaySound(self.last_preview,winsound.SND_FILENAME|winsound.SND_ASYNC)
        except Exception as exc:
            messagebox.showerror('Voorbeeld niet afgespeeld',str(exc))

    def stop_preview(self):
        try:
            import winsound
            winsound.PlaySound(None,0)
        except (ImportError,RuntimeError): pass

    def script_changed(self, event=None):
        if event is not None and not self.text.edit_modified(): return
        content=self.text.get('1.0','end-1c')
        count=len(content.split())
        minutes,seconds=divmod(round(count/150*60),60)
        self.script_stats.set(f'{count} woorden  ·  {len(content)} tekens  ·  circa {minutes}:{seconds:02d} spreektijd')
        self.text.edit_modified(False)

    def save_draft(self):
        self.save_preferences()
        try: (ROOT/'draft.txt').write_text(self.text.get('1.0','end-1c'),encoding='utf-8')
        except OSError: pass

    def import_text(self):
        if self.busy: return
        path=filedialog.askopenfilename(title='Open een script',filetypes=[('Tekstbestand','*.txt')])
        if not path: return
        try: content=Path(path).read_text(encoding='utf-8-sig')
        except (OSError,UnicodeError) as exc:
            messagebox.showerror('Tekst niet geopend',str(exc));return
        self.text.delete('1.0','end');self.text.insert('1.0',content)
        self.save_draft()

    def open_output(self):
        folder=ROOT/'output';folder.mkdir(exist_ok=True)
        try: os.startfile(str(folder))
        except OSError as exc: messagebox.showerror('Map niet geopend',str(exc))

    def play_output(self):
        if self.last_output and Path(self.last_output).is_file():
            try: os.startfile(self.last_output)
            except OSError as exc: messagebox.showerror('Audio niet geopend',str(exc))

    def set_expression(self, value):
        value = round(max(0.25, min(1.0, float(value))), 2)
        self.expression.set(value)
        self.expression_value.set(f'{value:.2f}')

    def expression_slid(self, value):
        self.expression_value.set(f'{float(value):.2f}')

    def commit_expression(self):
        import math
        try:
            value = float(self.expression_value.get().replace(',', '.'))
            if not math.isfinite(value): raise ValueError('Ongeldig getal')
        except (ValueError, tk.TclError):
            self.expression_value.set(f'{self.expression.get():.2f}')
            return False
        self.set_expression(value)
        return True

    def close(self):
        if self.busy:
            messagebox.showinfo('Nog bezig', 'Wacht tot de voice-over klaar is voordat je afsluit.')
            return
        self.save_draft()
        self.stop_preview()
        self.window.destroy()

    def update(self):
        if self.busy:
            messagebox.showinfo('Nog bezig', 'Wacht tot de voice-over klaar is voordat je bijwerkt.')
            return
        self.busy = True
        self.button.configure(state='disabled')
        self.update_button.configure(state='disabled')
        self.preview_button.configure(state='disabled')
        self.stop_preview()
        self.progress.start()
        self.save_draft()
        self.progress.configure(mode='indeterminate',value=0)
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
            self.save_preferences()

    def generate(self):
        if self.busy: return
        if not self.commit_expression() or not self.commit_sound_controls():
            messagebox.showerror('Expressie', 'Controleer de getallen bij expressie, volume en variatie.'); return
        text = self.text.get('1.0','end').strip()
        reference = self.reference.get().strip()
        if not text or not Path(reference).is_file():
            messagebox.showerror('Invoer ontbreekt','Vul tekst in en kies een bestaand stemvoorbeeld.'); return
        output_dir = ROOT/'output'; output_dir.mkdir(exist_ok=True)
        destination = filedialog.asksaveasfilename(initialdir=output_dir, defaultextension='.wav', filetypes=[('WAV audio','*.wav')])
        if not destination: return
        self.busy = True
        self.save_draft()
        self.update_button.configure(state='disabled')
        self.progress.configure(mode='indeterminate',value=0)
        self.preview_button.configure(state='disabled')
        self.stop_preview()
        self.button.configure(state='disabled'); self.progress.start()
        self.status.set('Model laden… De eerste keer kan dit enkele minuten duren.')
        threading.Thread(target=self.worker, args=(text,reference,destination,self.device.get(),self.expression.get(),self.multi_reference.get(),'full',self.volume.get(),self.variation.get(),self.storyteller.get(),self.rvc_enabled.get(),self.rvc_index.get()), daemon=True).start()

    def worker(self, text, reference, destination, device, expression, multi_reference=False, job_type="full", volume=100.0, variation=0.8, storyteller=False, use_rvc=False, rvc_index=0.75):
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
            plan = narration_plan(text,storyteller)
            chunks = [chunk for chunk,pause in plan]
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
                    wav = self.model.generate(chunk, audio_prompt_path=None if reuse else reference, exaggeration=expression, cfg_weight=0.3, temperature=variation).detach().cpu()
                    if wav.ndim == 1: wav = wav.unsqueeze(0)
                    if audio: audio.append(torch.zeros(wav.shape[0], int(self.model.sr*plan[i][1])))
                    audio.append(wav)
            torchaudio.save(destination, apply_volume(torch.cat(audio,dim=-1),volume), self.model.sr)
            if use_rvc:
                self.events.put(('status','Getrainde RVC-stem toepassen…'))
                self.model=None;self.loaded_device=None;self.condition_key=None
                if torch.cuda.is_available():torch.cuda.empty_cache()
                convert_rvc(destination,rvc_index)
            elapsed = time.perf_counter() - started
            if job_type == 'preview':
                Path(destination).with_suffix('.ready').write_text('complete',encoding='utf-8')
                self.events.put(('preview_done',destination))
            else:
                self.events.put(('output',destination))
                self.events.put(('done',f'Opname klaar · {elapsed:.1f} sec generatie · {Path(destination).name}'))
        except Exception:
            error = traceback.format_exc()
            (ROOT/'error.log').write_text(error,encoding='utf-8')
            self.events.put(('error',error))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'output':
                    self.last_output=value
                    self.listen_button.configure(state='normal')
                    continue
                if kind == 'status':
                    self.status.set(value)
                    match=re.search(r'Fragment (\d+) van (\d+)',value)
                    if match:
                        self.progress.stop()
                        self.progress.configure(mode='determinate',maximum=100,value=100*(int(match[1])-1)/max(1,int(match[2])))
                    continue
                self.busy = False; self.update_button.configure(state='normal'); self.preview_button.configure(state='normal'); self.progress.stop(); self.button.configure(state='normal')
                if kind == 'update_ready':
                    try:
                        interpreter = Path(sys.executable).with_name('pythonw.exe')
                        subprocess.Popen([str(interpreter),value,str(ROOT),'--automatic'],cwd=ROOT)
                    except OSError as exc:
                        messagebox.showerror('Update niet gestart',str(exc))
                        continue
                    self.window.destroy()
                    return
                if kind == 'preview_done':
                    self.last_preview=value
                    self.progress.configure(mode='determinate',value=100)
                    self.status.set('Luistervoorbeeld klaar · pas de expressie aan of maak je volledige voice-over')
                    self.play_preview()
                elif kind == 'current':
                    self.status.set(value)
                    messagebox.showinfo('Update',value)
                elif kind == 'done':
                    self.status.set(value)
                    self.progress.configure(mode='determinate',value=100)
                else:
                    self.status.set('Er is een fout opgetreden. Details staan in error.log.')
                    messagebox.showerror('Generatie mislukt',value[-2200:])
        except queue.Empty: pass
        self.window.after(100,self.poll)

if __name__ == '__main__':
    root = tk.Tk()
    Studio(root)
    root.mainloop()







