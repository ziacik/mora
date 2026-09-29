from __future__ import annotations
import argparse, pathlib, re, sys, time
from dataclasses import dataclass, field

class MoraError(Exception): pass

FORBIDDEN=[re.compile(r'\bfn\s+'),re.compile(r'\bdef\s+'),re.compile(r'\bclass\s+'),re.compile(r'\blambda\b'),re.compile(r'\bstate\.'),re.compile(r'\.connect\s*\(')]
TOP={'app','belief','meaning','pattern','offer','withdraw','faculty','law','preference','desire','scene','concept','scenario','when'}
BRING_RE=re.compile(r'^bring\s+["\'](.+?)["\']\s*$')
LANG_RE=re.compile(r'^language\s+mora\s+([0-9]+(?:\.[0-9]+)*)\s*$')

@dataclass
class Node:
    header:str; source:pathlib.Path; line:int
    children:list['Node']=field(default_factory=list)
    statements:list[tuple[int,str]]=field(default_factory=list)
    items:list[tuple[str,object]]=field(default_factory=list)
    @property
    def kind(self): return self.header.split(None,1)[0] if self.header else ''
    @property
    def name(self):
        p=self.header.split(None,1); return p[1].strip() if len(p)>1 else ''
    def walk(self):
        yield self
        for c in self.children: yield from c.walk()
    def text(self):
        return '\n'.join([self.header]+[s for _,s in self.statements]+[c.text() for c in self.children])
    def child(self,kind,name=None):
        return next((c for c in self.children if c.kind==kind and (name is None or c.name==name)),None)

@dataclass
class Program:
    nodes:list[Node]; files:list[pathlib.Path]; language:str|None=None
    def walk(self):
        for n in self.nodes: yield from n.walk()
    def declarations(self,kind): return [n for n in self.walk() if n.kind==kind]
    def declaration(self,kind,name): return next((n for n in self.declarations(kind) if n.name==name),None)

def _strip(line):
    out=[]; q=None; esc=False
    for ch in line:
        if esc: out.append(ch); esc=False; continue
        if ch=='\\': out.append(ch); esc=True; continue
        if q:
            out.append(ch)
            if ch==q:q=None
            continue
        if ch in "\"'": q=ch; out.append(ch); continue
        if ch=='#': break
        out.append(ch)
    return ''.join(out).rstrip()

def parse_file(path:pathlib.Path,seen=None):
    path=path.resolve(); seen=set() if seen is None else set(seen)
    if path in seen: raise MoraError(f'recursive bring: {path}')
    if not path.exists(): raise MoraError(f'file not found: {path}')
    seen.add(path); nodes=[]; files=[path]; lang=None; stack=[]
    for lineno,raw in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
        line=_strip(raw).strip()
        if not line: continue
        if not stack and (m:=BRING_RE.match(line)):
            p=parse_file(path.parent/m.group(1),seen); nodes.extend(p.nodes); files.extend(p.files); lang=lang or p.language; continue
        if not stack and (m:=LANG_RE.match(line)): lang=m.group(1); continue
        for rx in FORBIDDEN:
            if rx.search(line): raise MoraError(f'{path}:{lineno}: host-language-shaped construct is not Mora: {line}')
        if line=='}':
            if not stack: raise MoraError(f'{path}:{lineno}: unexpected }}')
            stack.pop(); continue
        if line.endswith('{'):
            header=line[:-1].strip(); n=Node(header,path,lineno)
            if stack: stack[-1].children.append(n); stack[-1].items.append(('node',n))
            else:
                if header.split(None,1)[0] not in TOP: raise MoraError(f'{path}:{lineno}: unknown top-level form')
                nodes.append(n)
            stack.append(n); continue
        if '{' in line or '}' in line: raise MoraError(f'{path}:{lineno}: braces must delimit blocks')
        if not stack: raise MoraError(f'{path}:{lineno}: statement outside declaration')
        stack[-1].statements.append((lineno,line)); stack[-1].items.append(('statement',(lineno,line)))
    if stack: raise MoraError(f'{stack[-1].source}:{stack[-1].line}: unclosed block {stack[-1].header}')
    uniq=[]; s=set()
    for f in files:
        if f not in s:s.add(f);uniq.append(f)
    return Program(nodes,uniq,lang)

INTENSITY={'faintly':.025,'gently':.055,'moderately':.11,'strongly':.20}

def duration(text):
    m=re.match(r'([0-9.]+)\s*(ms|s|m|h)?',text.strip())
    if not m:return 0
    return float(m.group(1))*{'ms':.001,'s':1,'m':60,'h':3600}[m.group(2) or 's']

def event_shape(s):
    return re.sub(r'\([^)]*\)','(*)',s.strip())

@dataclass
class Belief:
    name:str; values:dict[str,float]

class AffectiveRuntime:
    def __init__(self,program):
        self.program=program; self.beliefs={}; self.events=[]; self.offers=set(); self.pending=[]; self._since={}; self._load()
    def _load(self):
        for n in self.program.declarations('belief'):
            m=re.match(r'belief\s+(\w+)\s+about\s+(\w+)',n.header)
            if not m: continue
            vals={}
            for _,s in n.statements:
                p=s.split()
                if len(p)==2:
                    try: vals[p[0]]=float(p[1])
                    except: pass
            self.beliefs[m.group(1)]=Belief(m.group(1),vals)
    def _matches(self,pattern,event): return event_shape(pattern)==event_shape(event)
    def _meaning_holds(self,head,event,now):
        p=head[len('meaning '):]
        if p.startswith('repeated '):
            rest=p[len('repeated '):]; base,_,window=rest.partition(' within ')
            if not self._matches(base,event): return False
            d=duration(window); return sum(1 for t,e in self.events if self._matches(base,e) and now-t<=d)>=3
        if ' after ' in p and ' within ' in p:
            current,rest=p.split(' within ',1); win,after=rest.split(' after ',1)
            return self._matches(current,event) and any(self._matches(after,e) and now-t<=duration(win) for t,e in self.events[:-1])
        return self._matches(p,event)
    def perceive(self,event,belief='user'):
        now=time.monotonic(); self.events.append((now,event)); b=self.beliefs.get(belief)
        if b:
            for n in self.program.declarations('meaning'):
                if not self._meaning_holds(n.header,event,now): continue
                for _,s in n.statements:
                    m=re.match(r'(suggests|weakens)\s+(\w+)\s+(faintly|gently|moderately|strongly)',s)
                    if m:
                        d=INTENSITY[m.group(3)]*(1 if m.group(1)=='suggests' else -1)
                        b.values[m.group(2)]=max(0,min(1,b.values.get(m.group(2),0)+d))
        self._journeys(event); return dict(b.values) if b else {}
    def pattern(self,name):
        n=self.program.declaration('pattern',name)
        if not n:return False
        now=time.monotonic(); ors=[]
        for _,s in n.statements:
            s=s.removeprefix('or ').strip()
            if s.startswith('repeated '):
                rest=s[len('repeated '):]; base,_,win=rest.partition(' within '); d=duration(win)
                ors.append(sum(1 for t,e in self.events if self._matches(base,e) and now-t<=d)>=3)
            elif ' after ' in s and ' within ' in s:
                current,rest=s.split(' within ',1); win,after=rest.split(' after ',1); d=duration(win)
                last=next((t for t,e in reversed(self.events) if self._matches(current,e)),None)
                ors.append(last is not None and any(self._matches(after,e) and 0<=last-t<=d for t,e in self.events))
            elif s.startswith('belief '):
                m=re.match(r'belief\s+(\w+)\.(\w+)\s+dominates\s+(.+?)\s+for\s+(.+)',s)
                if not m: ors.append(False); continue
                who,key,others,span=m.groups(); b=self.beliefs.get(who); ok=False
                if b: ok=all(b.values.get(key,0)>b.values.get(o.split('.')[-1],0) for o in others.split())
                token=(n.name,s)
                if ok:self._since.setdefault(token,now)
                else:self._since.pop(token,None)
                ors.append(ok and now-self._since.get(token,now)>=duration(span))
        return any(ors)
    def _condition(self,c,event): return self._matches(c,event) or self.pattern(c)
    def _journeys(self,event):
        for j in self.program.declarations('journey'):
            for c in j.children:
                if c.header.startswith('when ') and self._condition(c.header[5:].strip(),event):
                    for _,s in c.statements:
                        if s.startswith('offer '):self.offers.add(s[6:].strip())
                        elif s.startswith('withdraw '):self.offers.discard(s[9:].strip())
                        elif s.startswith('desire '):self.pending.append(s[7:].strip())
    def take_pending(self):
        p=self.pending[:]; self.pending.clear(); return p

def inspect(program):
    return '\n'.join([f'Mora {program.language or "?"} — {len(program.files)} source file(s)']+[f'{n.kind:10} {n.name}' for n in program.nodes])

def main(argv=None):
    ap=argparse.ArgumentParser(prog='mora'); sub=ap.add_subparsers(dest='cmd',required=True)
    for cmd in ('check','inspect'):
        p=sub.add_parser(cmd);p.add_argument('file')
    p=sub.add_parser('simulate');p.add_argument('file');p.add_argument('events',nargs='+')
    p=sub.add_parser('run');p.add_argument('file')
    a=ap.parse_args(argv)
    try:
        pr=parse_file(pathlib.Path(a.file))
        if pr.language and pr.language!='0.4':raise MoraError(f'runtime implements Mora 0.4, source requests {pr.language}')
        if a.cmd=='check': print(f'OK: {a.file} (Mora {pr.language or "0.4"}, {len(pr.files)} files)');return 0
        if a.cmd=='inspect':print(inspect(pr));return 0
        if a.cmd=='simulate':
            rt=AffectiveRuntime(pr); print('start',rt.beliefs.get('user').values if 'user' in rt.beliefs else {})
            for e in a.events:print(e,'=>',rt.perceive(e))
            print('offers',sorted(rt.offers));return 0
        from .gtk_backend import GtkBackend; GtkBackend(pr).run();return 0
    except MoraError as e:print(f'Mora error: {e}',file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
