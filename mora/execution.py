from __future__ import annotations
import copy,re,shlex
from dataclasses import dataclass
from .runtime import MoraError,AffectiveRuntime

@dataclass
class Request:
    faculty:str; operation:str; args:list; options:dict; target:str|None; ask:bool=False

class Engine:
    def __init__(self,program,registry):
        self.program=program;self.registry=registry;self.world={};self.message='';self.affect=AffectiveRuntime(program);self.on_change=None;self.on_message=None
        self._init_world()
    def _init_world(self):
        apps=self.program.declarations('app')
        if not apps:return
        rem=apps[0].child('remembers')
        if not rem:return
        for _,s in rem.statements:
            p=s.split(None,1)
            if len(p)==2:self.world[p[0]]=self.literal(p[1])
    def literal(self,s):
        s=s.strip()
        if s in ('absent','none'):return None
        if s=='empty':return []
        if s=='fitted':return {'zoom':1.0,'x':0.0,'y':0.0}
        if s in ('true','false'):return s=='true'
        if s.startswith('"') and s.endswith('"'):return s[1:-1]
        try:return int(s)
        except:pass
        try:return float(s)
        except:return s
    def get(self,path,env=None):
        env=env or {}
        if path in env:return env[path]
        if path in self.world:return self.world[path]
        cur=env.get(path.split('.')[0],self.world.get(path.split('.')[0]))
        for part in path.split('.')[1:]:
            if cur is None:return None
            if isinstance(cur,dict):cur=cur.get(part)
            else:cur=getattr(cur,part,None)
        return cur
    def set(self,path,value):
        parts=path.split('.')
        if len(parts)==1:self.world[path]=value;return
        root=self.world.get(parts[0])
        if not isinstance(root,dict):
            root={};self.world[parts[0]]=root
        for p in parts[1:-1]:
            child=root.get(p)
            if not isinstance(child,dict):child={};root[p]=child
            root=child
        root[parts[-1]]=value
    def resolve(self,token,env):
        token=token.strip()
        if token in env:return env[token]
        if token.startswith('concept:'):return self.program.declaration('concept',token.split(':',1)[1])
        v=self.get(token,env)
        if v is not None or token.split('.')[0] in self.world:return v
        return self.literal(token)
    def condition(self,text,env=None):
        env=env or {};t=text.strip()
        if t.endswith(' exists'):return self.get(t[:-7].strip(),env) is not None
        if t.endswith(' absent'):return self.get(t[:-7].strip(),env) is None
        if t.endswith(' not empty'):return bool(self.get(t[:-10].strip(),env))
        if t.startswith('not '):return not self.condition(t[4:],env)
        root=t.split('.')[0]
        if root in env or root in self.world:
            value=self.get(t,env)
            return value if isinstance(value,bool) else value is not None
        return False
    def _options(self,node,env):
        out={}
        for _,s in node.statements:
            p=shlex.split(s)
            if not p:continue
            if len(p)==1:out[p[0]]=True
            elif len(p)==2:out[p[0]]=self.resolve(p[1],env)
            else:out[p[0]]=' '.join(p[1:])
        return out
    def _request(self,text,node,env,ask=False):
        p=shlex.split(text);p.pop(0);target=None
        if 'as' in p:
            i=p.index('as');target=p[i+1];p=p[:i]
        ref=p.pop(0)
        if '.' not in ref:raise MoraError(f'expected faculty.operation in {text}')
        faculty,op=ref.split('.',1);args=[]
        for tok in p:
            if self.program.declaration('concept',tok):args.append(self.program.declaration('concept',tok))
            else:args.append(self.resolve(tok,env))
        return Request(faculty,op,args,self._options(node,env) if node else {},target,ask)
    def desire(self,name):
        n=self.program.declaration('desire',name)
        if not n:raise MoraError(f'unknown desire {name}')
        return self._desire_gen(n,{})
    def _desire_gen(self,node,env):
        try:yield from self._run_items(node,env,skip_failure=True)
        except Exception as error:
            env['error']=error;fail=next((c for c in node.children if c.header=='on failure'),None)
            if fail is None:raise
            yield from self._run_items(fail,env)
    def _run_items(self,node,env,skip_failure=False):
        for kind,item in node.items:
            if kind=='node':
                child=item
                if skip_failure and child.header=='on failure':continue
                if child.header.startswith('ask '):
                    req=self._request(child.header,child,env,True);result=yield req
                    if req.target:env[req.target]=result
                elif child.header.startswith('attempt '):
                    req=self._request(child.header,child,env,False);result=yield req
                    if req.target:env[req.target]=result
                elif child.header.startswith('for every '):
                    m=re.match(r'for every\s+(\w+)\s+in\s+(.+)',child.header);var,path=m.groups();seq=self.resolve(path,env) or []
                    for i,value in enumerate(list(seq),1):
                        inner=dict(env);inner[var]=value;inner['index']=i
                        yield from self._run_items(child,inner)
                elif child.header.startswith('when '):
                    if self.condition(child.header[5:],env):yield from self._run_items(child,env)
                elif child.header=='on failure':continue
            else:
                _,s=item
                if s.startswith('requires '):
                    if not self.condition(s[9:],env):return
                elif s.startswith('ask '):
                    req=self._request(s,None,env,True);result=yield req
                    if req.target:env[req.target]=result
                elif s.startswith('attempt '):
                    req=self._request(s,None,env,False);result=yield req
                    if req.target:env[req.target]=result
                elif s.startswith('remember '):
                    m=re.match(r'remember\s+(\S+)\s+as\s+(.+)',s)
                    if not m:continue
                    path,expr=m.groups()
                    value=len(self.resolve(expr[11:],env) or [])-1 if expr.startswith('last-index ') else self.resolve(expr,env)
                    self.set(path,copy.deepcopy(value));self.changed()
                elif s.startswith('forget '):self.set(s[7:].strip(),None);self.changed()
                elif s.startswith('clear '):
                    p=s[6:].strip();cur=self.get(p);self.set(p,{'past':[],'future':[]} if isinstance(cur,dict) and ('past' in cur or 'future' in cur) else []);self.changed()
                elif s.startswith('add '):
                    m=re.match(r'add\s+(\S+)\s+to\s+(\S+)',s);value,path=m.groups();self.get(path).append(copy.deepcopy(self.resolve(value,env)));self.changed()
                elif s.startswith('remove-index '):
                    m=re.match(r'remove-index\s+(\S+)\s+from\s+(\S+)',s);idx,path=m.groups();i=self.resolve(idx,env);seq=self.get(path)
                    if i is not None and 0<=int(i)<len(seq):seq.pop(int(i));self.changed()
                elif s.startswith('snapshot '):
                    m=re.match(r'snapshot\s+(.+)\s+into\s+(\S+)',s);names,hpath=m.groups();h=self.world.setdefault(hpath,{'past':[],'future':[]})
                    if not isinstance(h,dict):h={'past':[],'future':[]};self.world[hpath]=h
                    h['past'].append({k:copy.deepcopy(self.get(k)) for k in names.split()});h['past']=h['past'][-100:];h['future'].clear();self.changed()
                elif s.startswith('restore previous '):self._restore(s,'previous');self.changed()
                elif s.startswith('restore next '):self._restore(s,'next');self.changed()
                elif s.startswith('perceive '):
                    body=s[9:];event=body
                    if ' when ' in body:
                        event,cond=body.split(' when ',1)
                        if not self.condition(cond,env):continue
                    self.affect.perceive(event.strip())
                    for desire in self.affect.take_pending():
                        target=self.program.declaration('desire',desire)
                        if target:yield from self._desire_gen(target,{})
                    self.changed()
                elif s.startswith('desire '):
                    target=self.program.declaration('desire',s[7:].strip())
                    if target:yield from self._desire_gen(target,{})
                elif s.startswith('tell user '):
                    msg=s[len('tell user '):].strip().strip('"');self.message=msg
                    if self.on_message:self.on_message(msg)
                    self.changed()
    def _restore(self,s,direction):
        m=re.match(r'restore\s+(?:previous|next)\s+(.+)\s+from\s+(\S+)',s);names,hpath=m.groups();h=self.world.setdefault(hpath,{'past':[],'future':[]})
        if not isinstance(h,dict):return
        src='past' if direction=='previous' else 'future';dst='future' if direction=='previous' else 'past'
        if not h[src]:return
        h[dst].append({k:copy.deepcopy(self.get(k)) for k in names.split()});snap=h[src].pop()
        for k,v in snap.items():self.set(k,v)
    def changed(self):
        if self.on_change:self.on_change()
