from __future__ import annotations
import base64, io, json, math, os, pathlib, subprocess, tempfile
from dataclasses import dataclass
from .runtime import MoraError

class MissingSecret(MoraError):
    def __init__(self, faculty, key, config):
        self.faculty=faculty; self.key=key; self.config=config
        super().__init__(f'missing secret {key} for {faculty}')

@dataclass
class Device:
    id:str; label:str

def _image_load(path):
    from PIL import Image
    im=Image.open(path).convert('RGBA'); im.load(); return im

def _image_save(image,path):
    image.save(path,format='PNG'); return path

def _scanner_discover():
    try:r=subprocess.run(['scanimage','-L'],capture_output=True,text=True,check=False)
    except FileNotFoundError as e: raise MoraError('SANE scanimage is required') from e
    if r.returncode!=0:raise MoraError(r.stderr.strip() or 'could not discover devices')
    out=[]
    for raw in r.stdout.splitlines():
        line=raw.strip()
        if not line.startswith('device '):continue
        rest=line[7:]; q=rest[0] if rest else ''
        if q not in "'`":continue
        end=rest.find("'",1)
        if end<0:continue
        ident=rest[1:end].strip(); desc=rest[end+1:].strip()
        if desc.startswith('is a '):desc=desc[5:]
        out.append(Device(ident,desc or ident))
    return out

def _scanner_acquire(device,dpi=600):
    device_id=device.id if hasattr(device,'id') else str(device)
    fd,path=tempfile.mkstemp(prefix='mora-',suffix='.png');os.close(fd)
    try:
        r=subprocess.run(['scanimage','-d',device_id,'--format=png','--mode','Color','--resolution',str(int(dpi)),'-o',path],capture_output=True,text=True,check=False)
        if r.returncode!=0:raise MoraError(r.stderr.strip() or 'device acquisition failed')
        return _image_load(path)
    finally:
        try:os.remove(path)
        except FileNotFoundError:pass

def _secret_get(app,key):
    try:r=subprocess.run(['secret-tool','lookup','application',app,'key',key],capture_output=True,text=True,check=False)
    except FileNotFoundError as e:raise MoraError('libsecret secret-tool is required') from e
    return r.stdout.strip() or None if r.returncode==0 else None

def secret_set(app,key,value,label):
    r=subprocess.run(['secret-tool','store',f'--label={label}','application',app,'key',key],input=value,text=True,capture_output=True,check=False)
    if r.returncode!=0:raise MoraError(r.stderr.strip() or 'could not store secret')

def _dist(a,b):return math.hypot(a[0]-b[0],a[1]-b[1])
def quad_valid(q):
    if not isinstance(q,(list,tuple)) or len(q)!=4:return False
    if any(_dist(q[i],q[(i+1)%4])<20 for i in range(4)):return False
    sign=0
    for i in range(4):
        a,b,c=q[i],q[(i+1)%4],q[(i+2)%4]; cross=(b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0])
        if abs(cross)<1:return False
        s=1 if cross>0 else -1
        if not sign:sign=s
        elif sign!=s:return False
    return True

def quad_bounds(q):
    xs=[p[0] for p in q];ys=[p[1] for p in q];return min(xs),min(ys),max(xs),max(ys)
def point_in_quad(p,q):
    pos=neg=False
    for i in range(4):
        a,b=q[i],q[(i+1)%4];c=(b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]);pos|=c>0;neg|=c<0
        if pos and neg:return False
    return True

def _move(q,dx,dy,image):
    w,h=image.size;l,t,r,b=quad_bounds(q);dx=max(-l,min(w-r,dx));dy=max(-t,min(h-b,dy));return [[p[0]+dx,p[1]+dy] for p in q]
def _snap(v,targets,r):
    cand=[(abs(v-t),t) for t in targets if abs(v-t)<=r];return min(cand)[1] if cand else v
def _snap_quad(q,image,r):
    if r<=0:return q
    w,h=image.size;n=[[_snap(p[0],[0.,float(w)],r),_snap(p[1],[0.,float(h)],r)] for p in q];return n if quad_valid(n) else q
def _move_corner(q,index,dx,dy,image,snap=0):
    w,h=image.size;n=[[float(x),float(y)] for x,y in q];n[index]=[max(0,min(w,n[index][0]+dx)),max(0,min(h,n[index][1]+dy))];n=_snap_quad(n,image,snap);return n if quad_valid(n) else q
def _move_edge(q,index,dx,dy,image,snap=0):
    pairs=((0,1),(1,2),(2,3),(3,0));a,b=pairs[int(index)];n=[[float(x),float(y)] for x,y in q];p1,p2=n[a],n[b];ex,ey=p2[0]-p1[0],p2[1]-p1[1];length=math.hypot(ex,ey)
    if length<1:return q
    nx,ny=-ey/length,ex/length;off=dx*nx+dy*ny;w,h=image.size
    for i in (a,b):n[i]=[max(0,min(w,n[i][0]+nx*off)),max(0,min(h,n[i][1]+ny*off))]
    n=_snap_quad(n,image,snap);return n if quad_valid(n) else q

def _centered_quad(image,wf=.33,hf=.33):
    w,h=image.size;qw=max(100,w*float(wf));qh=max(100,h*float(hf));x=(w-qw)/2;y=(h-qh)/2;return [[x,y],[x+qw,y],[x+qw,y+qh],[x,y+qh]]
def _expand(q,margin,image):
    m=float(margin);w,h=image.size
    if m<=0:return q
    cx=sum(p[0] for p in q)/4;cy=sum(p[1] for p in q)/4;n=[]
    for p in q:
        vx,vy=p[0]-cx,p[1]-cy;l=max(1,math.hypot(vx,vy));n.append([max(0,min(w,p[0]+vx/l*m)),max(0,min(h,p[1]+vy/l*m))])
    return n if quad_valid(n) else q
def _expand_each(items,margin,image):return [_expand(q,margin,image) for q in items]

def _filter_quads(items,image,min_side_fraction=.04,min_area=.003,max_area=.80,max_touched_edges=3,edge_fraction=.02):
    w,h=image.size;scale=min(w,h);out=[]
    for q in items:
        if not quad_valid(q):continue
        l,t,r,b=quad_bounds(q);bw,bh=max(0,r-l),max(0,b-t);area=(bw*bh)/max(1.,float(w*h))
        if bw<scale*float(min_side_fraction) or bh<scale*float(min_side_fraction):continue
        if not (float(min_area)<=area<=float(max_area)):continue
        edge=scale*float(edge_fraction);touched=int(l<=edge)+int(t<=edge)+int(r>=w-edge)+int(b>=h-edge)
        if touched>=int(max_touched_edges):continue
        out.append(q)
    return out

def _overlap_ratio(a,b):
    al,at,ar,ab=quad_bounds(a);bl,bt,br,bb=quad_bounds(b);l,t=max(al,bl),max(at,bt);r,bm=min(ar,br),min(ab,bb)
    if r<=l or bm<=t:return 0.
    inter=(r-l)*(bm-t);aa=(ar-al)*(ab-at);ba=(br-bl)*(bb-bt);return inter/max(1.,min(aa,ba))

def _dedupe_quads(items,threshold=.85):
    out=[]
    for q in items:
        if any(_overlap_ratio(q,k)>float(threshold) for k in out):continue
        out.append(q)
    return out

def _sort_visual(items,row_height=40):
    return sorted(items,key=lambda q:(quad_bounds(q)[1]//float(row_height),quad_bounds(q)[0]))

def _crop(image,q):
    import cv2,numpy as np
    from PIL import Image
    w=max(2,int(round(max(_dist(q[0],q[1]),_dist(q[3],q[2])))));h=max(2,int(round(max(_dist(q[0],q[3]),_dist(q[1],q[2])))))
    src=np.array(q,dtype=np.float32);dst=np.array([[0,0],[w-1,0],[w-1,h-1],[0,h-1]],dtype=np.float32);mat=cv2.getPerspectiveTransform(src,dst);rgba=np.array(image.convert('RGBA'));out=cv2.warpPerspective(rgba,mat,(w,h),flags=cv2.INTER_CUBIC,borderMode=cv2.BORDER_CONSTANT);return Image.fromarray(out,'RGBA')

def _numbered_path(folder,source,index,ext='png'):
    stem=pathlib.Path(str(source)).stem or 'item';return str(pathlib.Path(folder)/f'{stem}_{int(index):02}.{str(ext).lstrip(".")}')

def _concept_text(node):
    lines=[];result='text'
    for _,s in node.statements:
        if s.startswith('describe '): lines.append(s[len('describe '):].strip().strip('"'))
        elif s.startswith('result '): result=s[len('result '):].strip()
        else: lines.append(s)
    return ' '.join(lines),result

def _vision_perceive(image,concept,model,api_key,detail='high'):
    import requests
    from PIL import Image
    prompt,result=_concept_text(concept)
    if result!='list of Quad':raise MoraError(f'vision result shape not supported yet: {result}')
    preview=image.copy();preview.thumbnail((1800,1800),Image.Resampling.LANCZOS);buf=io.BytesIO();preview.convert('RGB').save(buf,format='JPEG',quality=88);url='data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode()
    point={'type':'object','properties':{'x':{'type':'number','minimum':0,'maximum':1000},'y':{'type':'number','minimum':0,'maximum':1000}},'required':['x','y'],'additionalProperties':False}
    schema={'type':'object','properties':{'items':{'type':'array','items':{'type':'object','properties':{'corners':{'type':'array','items':point,'minItems':4,'maxItems':4}},'required':['corners'],'additionalProperties':False}}},'required':['items'],'additionalProperties':False}
    instruction=prompt+' Return each match as exactly four outer corners clockwise, normalized 0..1000 relative to the submitted image.'
    body={'model':model,'store':False,'max_output_tokens':1500,'input':[{'role':'user','content':[{'type':'input_text','text':instruction},{'type':'input_image','image_url':url,'detail':detail}]}],'text':{'format':{'type':'json_schema','name':'mora_visual_entities','strict':True,'schema':schema}}}
    r=requests.post('https://api.openai.com/v1/responses',headers={'Authorization':'Bearer '+api_key,'Content-Type':'application/json'},json=body,timeout=45)
    if not r.ok:
        try:msg=r.json().get('error',{}).get('message',r.text[:300])
        except:msg=r.text[:300]
        raise MoraError(f'vision provider returned {r.status_code}: {msg}')
    data=r.json();text=data.get('output_text')
    if text is None:
        for item in data.get('output',[]):
            for c in item.get('content',[]):
                if c.get('type')=='output_text':text=c.get('text');break
    if text is None:raise MoraError('vision provider returned no structured text')
    parsed=json.loads(text);w,h=image.size;out=[]
    for item in parsed.get('items',[]):
        q=[[max(0,min(1000,float(p['x'])))/1000*w,max(0,min(1000,float(p['y'])))/1000*h] for p in item.get('corners',[])]
        if quad_valid(q):out.append(q)
    return out

class Registry:
    def __init__(self,program,app_id):
        self.program=program;self.app_id=app_id;self.faculties={}
        for n in program.declarations('faculty'):
            m=n.header.split(' from ',1)
            if len(m)!=2:continue
            alias=m[0].split(None,1)[1].strip();provider=m[1].strip();cfg={}
            for _,s in n.statements:
                if s.startswith('model '):cfg['model']=s[6:].strip().strip('"')
                elif s.startswith('secret '):
                    p=s.split();cfg['secret_key']=p[1]
                elif s.startswith('secret-label '):cfg['secret_label']=s[len('secret-label '):].strip().strip('"')
                elif s.startswith('secret-help '):cfg['secret_help']=s[len('secret-help '):].strip().strip('"')
                elif s.startswith('secret-link '):cfg['secret_link']=s[len('secret-link '):].strip().strip('"')
            self.faculties[alias]=(provider,cfg)
    def provider(self,alias):return self.faculties[alias][0]
    def config(self,alias):return self.faculties[alias][1]
    def invoke(self,alias,operation,args,options=None):
        if alias not in self.faculties:raise MoraError(f'unknown faculty {alias}')
        provider,cfg=self.faculties[alias];options=options or {}
        if provider=='image-codec':
            return {'load':_image_load,'save':_image_save}[operation](*args)
        if provider=='sane':return {'discover':_scanner_discover,'acquire':_scanner_acquire}[operation](*args)
        if provider=='opencv':
            table={'centered-quad':_centered_quad,'expand-each':_expand_each,'perspective-crop':_crop,'move':_move,'move-corner':_move_corner,'move-edge':_move_edge,'bounds':quad_bounds,'contains':point_in_quad,'filter':_filter_quads,'dedupe':_dedupe_quads,'sort-visual':_sort_visual}
            return table[operation](*args)
        if provider=='desktop.files' and operation=='numbered-path':return _numbered_path(*args)
        if provider=='openai.responses' and operation=='perceive':
            key=cfg.get('secret_key');secret=_secret_get(self.app_id,key) if key else None
            if key and not secret:raise MissingSecret(alias,key,cfg)
            image,concept=args[:2];detail=options.get('detail','high')
            return _vision_perceive(image,concept,cfg.get('model','gpt-5.6-terra'),secret,detail)
        raise MoraError(f'provider {provider} does not implement {operation}')
