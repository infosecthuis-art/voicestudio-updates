"""Local RVC setup/training controller. Audio stays on this machine."""
import os,sys,json,threading,queue,subprocess,urllib.request,zipfile,shutil,time,hashlib
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
ROOT=Path(__file__).resolve().parent
HOME=ROOT/'rvc'
CODE=HOME/'engine'
ENV=HOME/'.venv'
PY=ENV/'Scripts'/'python.exe'
REV='81eed5e8f68b6bed1789f682fe78cdd324495afc'
EXPERIMENT='ember_story'
ENVIRON=os.environ.copy()
for name,path in {'TEMP':HOME/'tmp','TMP':HOME/'tmp','PIP_CACHE_DIR':HOME/'cache/pip','HF_HOME':HOME/'cache/huggingface','TORCH_HOME':HOME/'cache/torch','UV_CACHE_DIR':HOME/'cache/uv','UV_PYTHON_INSTALL_DIR':HOME/'python','UV_PYTHON_BIN_DIR':HOME/'bin','UV_TOOL_DIR':HOME/'tools'}.items():
 path.mkdir(parents=True,exist_ok=True);ENVIRON[name]=str(path)
ENVIRON.update({'PYTHONIOENCODING':'utf-8','PYTHONUNBUFFERED':'1','OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'2'})
ENVIRON['PATH']=str(CODE)+os.pathsep+ENVIRON.get('PATH','')
ENVIRON['weight_root']=str(CODE/'assets/weights')
ENVIRON['index_root']=str(CODE/'assets/indices')
ENVIRON['outside_index_root']=str(CODE/'assets/indices')
ENVIRON['rmvpe_root']=str(CODE/'assets/rmvpe')

def safe_extract(archive,directory,strip_root=False):
 base=Path(directory).resolve();base.mkdir(parents=True,exist_ok=True)
 with zipfile.ZipFile(archive) as z:
  for item in z.infolist():
   parts=Path(item.filename.replace('\\','/')).parts
   if strip_root:parts=parts[1:]
   if not parts:continue
   destination=base.joinpath(*parts).resolve()
   if not destination.is_relative_to(base):raise ValueError('Onveilig archiefpad')
   if item.is_dir():destination.mkdir(parents=True,exist_ok=True);continue
   destination.parent.mkdir(parents=True,exist_ok=True)
   with z.open(item) as source,open(destination,'wb') as target:shutil.copyfileobj(source,target)

def filelist_for(experiment):
 exp=Path(experiment);rows=[]
 for wav in sorted((exp/'0_gt_wavs').glob('*.wav')):
  paths=[wav,exp/'3_feature768'/(wav.stem+'.npy'),exp/'2a_f0'/(wav.name+'.npy'),exp/'2b-f0nsf'/(wav.name+'.npy')]
  if all(p.is_file() for p in paths):rows.append('|'.join(p.resolve().as_posix() for p in paths)+'|0')
 if len(rows)<5:raise RuntimeError('Minder dan vijf bruikbare trainingsfragmenten. Controleer preprocess/extractielog.')
 return rows


def module_command(args):
 """Run upstream entry points as packages so train/train.py cannot shadow train."""
 args=[str(x) for x in args]
 entries={
  'train/preprocess.py':'train.preprocess',
  'train/dataset/extract_f0.py':'train.dataset.extract_f0',
  'train/dataset/extract_hubert_feature.py':'train.dataset.extract_hubert_feature',
  'train/train.py':'train.train',
  'train/train_index.py':'train.train_index',
  'infer/cli.py':'infer.cli',
 }
 if len(args)>1 and args[1].replace('\\','/') in entries:
  return [args[0],'-m',entries[args[1].replace('\\','/')],*args[2:]]
 return args


def configure_low_memory(engine):
 """Avoid 4 Windows worker processes and 32 prefetched training batches."""
 import ast
 path=Path(engine)/'train/train.py'
 source=path.read_text(encoding='utf-8')
 tree=ast.parse(source)
 calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='DataLoader']
 if len(calls)!=1:raise RuntimeError('Onverwachte RVC-versie: DataLoader niet eenduidig gevonden. Geen training gestart.')
 call=calls[0];original=ast.get_source_segment(source,call)
 target={'num_workers':0,'pin_memory':False,'persistent_workers':False,'prefetch_factor':None}
 keys={item.arg for item in call.keywords}
 if not set(target)<=keys:raise RuntimeError('Onverwachte laadinstellingen. Geen training gestart.')
 for item in call.keywords:
  if item.arg in target:item.value=ast.Constant(target[item.arg])
 new=ast.unparse(ast.fix_missing_locations(call))
 changed=source.replace(original,new,1)
 ast.parse(changed)
 backup=path.with_suffix('.py.before-low-memory')
 if not backup.exists():backup.write_text(source,encoding='utf-8')
 temporary=path.with_suffix('.py.new');temporary.write_text(changed,encoding='utf-8');os.replace(temporary,path)
 return target

class Manager:
 def __init__(self,root):
  self.root=root;self.events=queue.Queue();self.process=None;self.busy=False;self.cancel=threading.Event()
  root.title('VoiceStudio — RVC-stemmodel');root.geometry('880x720');root.minsize(760,600)
  root.protocol('WM_DELETE_WINDOW',self.close)
  frame=ttk.Frame(root,padding=20);frame.pack(fill='both',expand=True)
  ttk.Label(frame,text='Je vaste RVC-stemmodel',font=('Segoe UI',20,'bold')).pack(anchor='w')
  ttk.Label(frame,text='Eenmalig installeren → je volledige opname voorbereiden → trainen',wraplength=800).pack(anchor='w',pady=(6,14))
  ttk.Label(frame,text='Bestanden blijven onder '+str(HOME),wraplength=800).pack(anchor='w')
  self.source=tk.StringVar(value=str(ROOT/'stem.mp3'))
  try:self.source.set(json.loads((ROOT/'settings.json').read_text()).get('reference',str(ROOT/'stem.mp3')))
  except (OSError,ValueError):pass
  row=ttk.Frame(frame);row.pack(fill='x',pady=12)
  ttk.Entry(row,textvariable=self.source).pack(side='left',fill='x',expand=True)
  ttk.Button(row,text='Volledige opname kiezen',command=self.choose).pack(side='right',padx=(8,0))
  ttk.Label(frame,text='Gebruik de volledige opname (bij jou 9:48), één stem zonder muziek. Een kort stemvoorbeeld is geen goede trainingsset.',wraplength=800).pack(anchor='w')
  steps=ttk.Frame(frame);steps.pack(fill='x',pady=14)
  self.buttons=[]
  for label,fn in [('1  RVC installeren',self.install),('2  Opname voorbereiden',self.prepare),('3  Trainen',self.train)]:
   b=ttk.Button(steps,text=label,command=lambda f=fn:self.start(f));b.pack(side='left',padx=(0,8));self.buttons.append(b)
  epochrow=ttk.Frame(frame);epochrow.pack(fill='x')
  ttk.Label(epochrow,text='Trainingsrondes:').pack(side='left')
  self.epochs=tk.StringVar(value='100');ttk.Spinbox(epochrow,from_=10,to=300,increment=10,textvariable=self.epochs,width=6).pack(side='left',padx=8)
  ttk.Label(epochrow,text='16 GB RAM-modus: één laadproces, batch 1, volledige precisie',wraplength=650).pack(side='left')
  self.status=tk.StringVar(value='Begin met stap 1. De installatie downloadt meerdere GB en kan lang duren.')
  ttk.Label(frame,textvariable=self.status,wraplength=800).pack(fill='x',pady=(14,8))
  self.progress=ttk.Progressbar(frame,mode='indeterminate');self.progress.pack(fill='x')
  self.log=tk.Text(frame,wrap='word',height=16,font=('Consolas',9));self.log.pack(fill='both',expand=True,pady=12)
  bottom=ttk.Frame(frame);bottom.pack(fill='x')
  ttk.Button(bottom,text='Stop huidige stap',command=self.stop).pack(side='left')
  ttk.Button(bottom,text='Terug naar studio',command=self.back).pack(side='right')
  root.after(100,self.poll)

 def choose(self):
  if self.busy:return
  path=filedialog.askopenfilename(filetypes=[('Audio','*.mp3 *.wav *.flac')])
  if path:self.source.set(path)

 def emit(self,line):self.events.put(('log',str(line)))
 def run(self,args,cwd=None):
  stages={'train/preprocess.py':'Trainingsaudio opsplitsen…','train/dataset/extract_f0.py':'Toonhoogte uit de opname halen…','train/dataset/extract_hubert_feature.py':'Stemkenmerken uit de opname halen…','train/train.py':'Stemmodel trainen. Dit kan lang duren; checkpoints worden opgeslagen.','train/train_index.py':'Index met opnamekenmerken maken…'}
  for argument in args:
   if str(argument) in stages:self.events.put(('status',stages[str(argument)]))
  if self.cancel.is_set():raise RuntimeError('Gestopt door gebruiker')
  args=module_command(args)
  with open(HOME/'setup-training.log','a',encoding='utf-8') as log:
   self.process=subprocess.Popen([str(x) for x in args],cwd=str(cwd or CODE),env=ENVIRON,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   for line in self.process.stdout:
    log.write(line);log.flush();self.emit(line.rstrip())
   code=self.process.wait();self.process=None
  if code or self.cancel.is_set():raise RuntimeError('Stap gestopt of mislukt. Zie de regels hierboven en rvc/setup-training.log.')

 def download(self,url,target):
  target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
  temp=target.with_suffix(target.suffix+'.part');self.emit('Download: '+target.name)
  with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'VoiceStudio-RVC/1.9'}),timeout=90) as response,open(temp,'wb') as f:
   size=int(response.headers.get('Content-Length','0'));received=0;last=0
   while True:
    if self.cancel.is_set():raise RuntimeError('Download gestopt')
    block=response.read(1024*1024)
    if not block:break
    f.write(block);received+=len(block)
    if time.monotonic()-last>2:
     self.emit(f'{target.name}: {received//1048576} MB'+(f' / {size//1048576} MB' if size else ''));last=time.monotonic()
  os.replace(temp,target)

 def install(self):
  if shutil.disk_usage(HOME).free<25*1024**3:raise RuntimeError('Maak minimaal 25 GB vrij op de schijf van VoiceStudio.')
  archive=HOME/'downloads'/('rvc-'+REV+'.zip')
  if not (CODE/'webui.py').is_file():
   self.download('https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/archive/'+REV+'.zip',archive)
   safe_extract(archive,CODE,strip_root=True)
  if not (HOME/'engine-revision.txt').exists():(HOME/'engine-revision.txt').write_text(REV)
  bootstrap=HOME/'bootstrap';bootstrap.mkdir(exist_ok=True)
  # uv itself is isolated in rvc/bootstrap, not installed into the studio environment.
  self.run([sys.executable,'-m','pip','install','--target',bootstrap,'uv>=0.9,<1'],cwd=HOME)
  ENVIRON['PYTHONPATH']=str(bootstrap)+os.pathsep+str(CODE)
  self.run([sys.executable,'-m','uv','python','install','3.12'],cwd=HOME)
  if not PY.is_file():self.run([sys.executable,'-m','uv','venv','--seed','--python','3.12',ENV],cwd=HOME)
  self.run([PY,'-m','pip','install','--upgrade','pip','setuptools<81','wheel'],cwd=CODE)
  self.run([PY,'-m','pip','install','torch==2.7.1','torchaudio==2.7.1','--index-url','https://download.pytorch.org/whl/cu118'])
  # Core training/conversion uses the official version bounds. Separation and
  # realtime-GUI dependencies are omitted because those entry points aren't used.
  requirement=(CODE/'requirments_cu118_py312.txt').read_text(encoding='utf-8')
  skip=('pymss','FreeSimpleGUI','onnxruntime','nvidia-','opencv-python','torchfcpe','sounddevice')
  lines=[line for line in requirement.splitlines() if line.strip() and not line.lstrip().startswith(('#','--')) and not line.strip().startswith(skip)]
  req=HOME/'requirements-core.txt';req.write_text('\n'.join(lines)+'\n',encoding='utf-8')
  self.run([PY,'-m','pip','install','-r',req,'--index-url','https://pypi.org/simple'])
  self.run([PY,'-m','pip','check'])
  self.run([PY,'-c',"import torch,faiss,librosa,transformers; from configs.config import get_device_dtype_sm; p=get_device_dtype_sm(0); print('RVC GPU:',p); assert p[0].type=='cuda','Geen ondersteunde CUDA GPU'; x=torch.ones(1,device='cuda'); print('Rekentest:',(x+x).item())"])
  assets=CODE/'assets'
  script="from huggingface_hub import snapshot_download,hf_hub_download; snapshot_download('lj1995/VoiceConversionWebUI',allow_patterns=['hubert_base/*','pretrained_v2/f0G40k.pth','pretrained_v2/f0D40k.pth'],local_dir='assets'); hf_hub_download('lj1995/VoiceConversionWebUI','rmvpe.pt',local_dir='assets/rmvpe')"
  self.run([PY,'-c',script])
  for binary in ['ffmpeg.exe','ffprobe.exe']:
   if not (CODE/binary).exists():self.download('https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main/'+binary,CODE/binary)
  required=[assets/'hubert_base/pytorch_model.bin',assets/'rmvpe/rmvpe.pt',assets/'pretrained_v2/f0G40k.pth',assets/'pretrained_v2/f0D40k.pth']
  if not all(p.is_file() for p in required):raise RuntimeError('Niet alle vereiste basismodellen zijn aanwezig.')
  self.run([PY,'infer/cli.py','--help'])
  (HOME/'installed.json').write_text(json.dumps({'revision':REV,'python':str(PY)}),encoding='utf-8')
  return 'RVC geïnstalleerd en GPU gecontroleerd. Ga naar stap 2.'

 def prepare(self):
  if not (HOME/'installed.json').is_file():raise RuntimeError('Voer eerst stap 1 uit.')
  source=Path(self.job_source)
  if not source.is_file():raise RuntimeError('Kies je volledige opname.')
  import librosa,numpy as np,soundfile as sf
  audio,sr=librosa.load(str(source),sr=40000,mono=True)
  if len(audio)/sr<120:raise RuntimeError('Deze opname is korter dan 2 minuten. Kies je volledige opname van 9:48.')
  if not np.isfinite(audio).all():raise RuntimeError('De opname bevat ongeldige samples.')
  dataset=HOME/'dataset';dataset.mkdir(exist_ok=True)
  sf.write(str(dataset/'voice.wav'),audio,sr,subtype='PCM_16')
  (HOME/'dataset.json').write_text(json.dumps({'source':str(source.resolve()),'seconds':len(audio)/sr,'sample_rate':sr}),encoding='utf-8')
  return f'Opname voorbereid: {len(audio)/sr/60:.1f} minuten. Geen muziek- of sprekerherkenning uitgevoerd. Ga naar stap 3.'

 def train(self):
  if not (HOME/'installed.json').is_file() or not (HOME/'dataset/voice.wav').is_file():raise RuntimeError('Voer eerst stap 1 en 2 uit.')
  configure_low_memory(CODE)
  self.emit('16 GB RAM-modus: geen extra laadprocessen, geen vooruitbuffer, geen vastgezet RAM. Batch blijft 1.')
  exp=CODE/'logs'/EXPERIMENT;exp.mkdir(parents=True,exist_ok=True)
  self.run([PY,'train/preprocess.py',HOME/'dataset','40000','2',exp,'False','3.7'])
  self.run([PY,'train/dataset/extract_f0.py','cuda','1','0','0',exp,'false'])
  self.run([PY,'train/dataset/extract_hubert_feature.py','cuda:0','1','0','0',exp,'v2','false'])
  rows=filelist_for(exp);(exp/'filelist.txt').write_text('\n'.join(rows)+'\n',encoding='utf-8')
  config=json.loads((CODE/'configs/v1/40k.json').read_text(encoding='utf-8'))
  config['train']['fp16_run']=False
  (exp/'config.json').write_text(json.dumps(config),encoding='utf-8')
  self.emit(f'Trainingsfragmenten: {len(rows)}. Training op GTX 1080 met batch 1; dit kan lang duren.')
  self.run([PY,'train/train.py','-e',EXPERIMENT,'-sr','40k','-f0','1','-bs','1','-g','0','-te',str(self.job_epochs),'-se','10','-pg','assets/pretrained_v2/f0G40k.pth','-pd','assets/pretrained_v2/f0D40k.pth','-l','1','-c','0','-sw','1','-v','v2'])
  self.run([PY,'train/train_index.py',EXPERIMENT,'v2','assets/indices','2','single'])
  weights=list((CODE/'assets/weights').glob(EXPERIMENT+'*.pth'))
  indices=list((CODE/'assets/indices').glob('added_*'+EXPERIMENT+'*.index'))+list(exp.glob('added_*.index'))
  if not weights or not indices:raise RuntimeError('Training eindigde zonder compleet model/index. Zie het log; er is geen model geactiveerd.')
  model=max(weights,key=lambda p:p.stat().st_mtime);index=max(indices,key=lambda p:p.stat().st_mtime)
  self.run([PY,'infer/cli.py','--model',model,'--list-speakers'])
  temporary=HOME/'voice.json.new';temporary.write_text(json.dumps({'model':str(model),'index':str(index),'revision':REV}),encoding='utf-8');os.replace(temporary,HOME/'voice.json')
  return 'Stemmodel en index zijn klaar. Ga terug naar de studio en vink RVC toepassen aan voor een korte proef.'

 def start(self,fn):
  if self.busy:return
  try:self.job_epochs=int(self.epochs.get());assert 10<=self.job_epochs<=300
  except (ValueError,AssertionError):messagebox.showerror('Trainingsrondes','Kies 10 tot 300 rondes.');return
  self.job_source=self.source.get();self.busy=True;self.cancel.clear()
  for b in self.buttons:b.configure(state='disabled')
  self.progress.start();self.status.set('Bezig: '+fn.__name__+'. Volg de uitvoer hieronder.')
  def work():
   try:self.events.put(('done',fn()))
   except Exception as exc:
    import traceback
    text=traceback.format_exc();self.emit(text);self.events.put(('error',str(exc)))
  threading.Thread(target=work,daemon=True).start()

 def stop(self):
  self.cancel.set()
  p=self.process
  if p and p.poll() is None:
   subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

 def back(self):
  if self.busy:messagebox.showinfo('Nog bezig','Wacht tot de stap klaar is of stop die eerst.');return
  subprocess.Popen([str(ROOT/'.venv/Scripts/pythonw.exe'),str(ROOT/'studio.py')],cwd=ROOT)
  self.root.destroy()

 def close(self):
  if self.busy:messagebox.showinfo('Nog bezig','Wacht tot de stap klaar is of kies Stop huidige stap.');return
  self.root.destroy()

 def poll(self):
  try:
   for _ in range(100):
    kind,text=self.events.get_nowait()
    if kind=='status':self.status.set(text);continue
    if kind=='log':self.log.insert('end',text+'\n');self.log.see('end');continue
    self.busy=False;self.progress.stop();self.status.set(text)
    for b in self.buttons:b.configure(state='normal')
    if kind=='error':messagebox.showerror('RVC-stap niet afgerond',text)
  except queue.Empty:pass
  self.root.after(100,self.poll)

if __name__=='__main__':
 root=tk.Tk();Manager(root);root.mainloop()


