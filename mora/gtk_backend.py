from __future__ import annotations
import copy,io,math,re,threading
from dataclasses import dataclass,field
from .runtime import MoraError
from .execution import Engine,Request
from .faculties import Registry,MissingSecret,quad_bounds,point_in_quad

BUTTON_RE=re.compile(r'^button\s+(?P<label>"[^"]+"|\S+)(?:\s+icon\s+(?P<icon>[\w-]+))?(?:\s+(?P<emph>emphasized))?\s+invites\s+(?P<desire>\w+)(?:\s+when\s+(?P<when>.+))?$')
ACTION_RE=re.compile(r'^action\s+"([^"]+)"\s+invites\s+(\w+)$')

@dataclass
class UI:
    window:object=None;drawing:object=None;empty:object=None;status:object=None;spinner:object=None;toast:object=None
    buttons:dict=field(default_factory=dict);bindings:list=field(default_factory=list);numbers:list=field(default_factory=list)

class GtkBackend:
    def __init__(self,program):
        self.program=program;self._load_gtk();self.app_id=self._identity() or 'org.mora.Application';self.registry=Registry(program,self.app_id);self.engine=Engine(program,self.registry);self.engine.on_change=self.refresh;self.engine.on_message=self._message
        self.ui=UI();self.canvas={};self.drag=None;self.hover=(0.,0.);self.preview=None;self.preview_ratio=1.0;self._keys=set();self._timers=[]
    def _load_gtk(self):
        try:
            import gi
            gi.require_foreign('cairo')
            for n,v in [('Gtk','4.0'),('Adw','1'),('Gdk','4.0'),('GdkPixbuf','2.0')]:gi.require_version(n,v)
            from gi.repository import Gtk,Adw,Gdk,GdkPixbuf,GLib
        except Exception as e:raise MoraError('GTK4/libadwaita faculty unavailable') from e
        self.Gtk,self.Adw,self.Gdk,self.GdkPixbuf,self.GLib=Gtk,Adw,Gdk,GdkPixbuf,GLib
    def _identity(self):
        apps=self.program.declarations('app')
        if not apps:return None
        for _,s in apps[0].statements:
            m=re.match(r'identity\s+"([^"]+)"',s)
            if m:return m.group(1)
    def run(self):
        app=self.Adw.Application(application_id=self.app_id);app.connect('activate',self._activate);app.run([])
    def _activate(self,app):
        scene=self.program.declarations('scene')[0];win=scene.child('window');title='Mora';w,h=1100,760;mw,mh=640,480
        for _,s in win.statements:
            if m:=re.match(r'title\s+"([^"]+)"',s):title=m.group(1)
            if m:=re.match(r'size\s+(\d+)\s+x\s+(\d+)',s):w,h=map(int,m.groups())
            if m:=re.match(r'minimum\s+(\d+)\s+x\s+(\d+)',s):mw,mh=map(int,m.groups())
        window=self.Adw.ApplicationWindow(application=app,title=title,default_width=w,default_height=h);window.set_size_request(mw,mh);self.ui.window=window
        header=self.Adw.HeaderBar();header.set_title_widget(self.Adw.WindowTitle(title=title,subtitle='Mora application'))
        if n:=win.child('header'):self._header(header,n)
        side_node=win.child('sidebar');canvas=self._canvas(next((c for c in win.children if c.kind=='canvas'),None))
        content=canvas
        if side_node:
            side=self._sidebar(side_node);paned=self.Gtk.Paned(orientation=self.Gtk.Orientation.HORIZONTAL);paned.set_start_child(side);paned.set_end_child(canvas);paned.set_position(310);paned.set_resize_start_child(False);content=paned
        toast=self.Adw.ToastOverlay();toast.set_child(content);self.ui.toast=toast
        toolbar=self.Adw.ToolbarView();toolbar.add_top_bar(header);toolbar.set_content(toast);window.set_content(toolbar);self._css();self._install_keyboard(window,win.child('keyboard'));self._install_clock(win.child('clock'));self.refresh();window.present()
    def _button_spec(self,s):
        m=BUTTON_RE.match(s)
        if not m:return None
        return m.group('label').strip('"'),m.group('icon'),bool(m.group('emph')),m.group('desire'),m.group('when')
    def _make_button(self,spec):
        label,icon,emph,desire,cond=spec
        if icon:
            box=self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL,spacing=6);box.append(self.Gtk.Image.new_from_icon_name(icon));box.append(self.Gtk.Label(label=label));b=self.Gtk.Button();b.set_child(box)
        else:b=self.Gtk.Button(label=label)
        if emph:b.add_css_class('suggested-action')
        b.connect('clicked',lambda _b,d=desire:self.invoke(d));self.ui.buttons[desire]=(b,cond);return b
    def _header(self,header,node):
        for _,s in node.statements:
            if spec:=self._button_spec(s):
                b=self._make_button(spec);(header.pack_end if spec[2] else header.pack_start)(b)
    def _sidebar(self,node):
        side=self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL,spacing=18);side.set_size_request(310,-1)
        for edge,val in [('top',18),('bottom',14),('start',16),('end',16)]:getattr(side,f'set_margin_{edge}')(val)
        for group in node.children if node else []:
            if group.kind!='group':continue
            title=group.name.strip('"');prefs=self.Adw.PreferencesGroup(title=title)
            for _,s in group.statements:
                if spec:=self._button_spec(s):
                    row=self.Adw.ActionRow(title=spec[0]);row.add_suffix(self._make_button(spec));prefs.add(row);continue
                if s.startswith('value '):
                    body=s[6:];label=title;cond=None;fallback=None
                    if ' when ' in body:body,cond=body.split(' when ',1)
                    if ' label ' in body:
                        expr,labelpart=body.split(' label ',1);body=expr;label=labelpart.strip().strip('"')
                    if ' fallback ' in body:
                        expr,fb=body.split(' fallback ',1);body=expr;fallback=fb.strip().strip('"')
                    row=self.Adw.ActionRow(title=label,subtitle='');prefs.add(row);self.ui.bindings.append((row,body.strip(),fallback,cond))
                elif s.startswith('number '):
                    m=re.match(r'number\s+(\S+)\s+([0-9.]+)\.\.([0-9.]+)(?:\s+label\s+"([^"]+)")?',s)
                    if m:
                        path,lo,hi,label=m.groups();row=self.Adw.ActionRow(title=label or path);spin=self.Gtk.SpinButton.new_with_range(float(lo),float(hi),1);spin.connect('value-changed',lambda sp,p=path:(self.engine.set(p,sp.get_value_as_int()),self.engine.changed()));row.add_suffix(spin);prefs.add(row);self.ui.numbers.append((spin,path))
            side.append(prefs)
        row=self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL,spacing=8);row.set_valign(self.Gtk.Align.END);row.set_vexpand(True);self.ui.spinner=self.Gtk.Spinner();self.ui.spinner.set_visible(False);self.ui.status=self.Gtk.Label(label='');self.ui.status.set_wrap(True);self.ui.status.set_xalign(0);row.append(self.ui.spinner);row.append(self.ui.status);side.append(row);return side
    def _canvas(self,node):
        d=self.Gtk.DrawingArea();d.set_hexpand(True);d.set_vexpand(True);d.set_focusable(True);d.add_css_class('mora-canvas');d.set_draw_func(self._draw);self.ui.drawing=d
        self.canvas={'image':None,'shapes':None,'focus':None,'view':None,'adapt':None,'gestures':{},'coordinates':None,'background':None,'foreground':None,'primitives':[]}
        if node:
            for _,s in node.statements:
                p=s.split()
                if not p:continue
                if p[0]=='image':self.canvas['image']=p[1]
                elif p[0]=='shapes':self.canvas['shapes']=p[1]
                elif p[0]=='focus':self.canvas['focus']=p[1]
                elif p[0]=='view':self.canvas['view']=p[1]
                elif p[0]=='adapt' and len(p)>=3:self.canvas['adapt']=p[2]
                elif p[0]=='coordinates' and len(p)>=4 and p[2]=='x':self.canvas['coordinates']=(float(p[1]),float(p[3]))
                elif p[0]=='background' and len(p)>=2:self.canvas['background']=p[1].strip('"')
                elif p[0]=='foreground' and len(p)>=2:self.canvas['foreground']=p[1].strip('"')
            for c in node.children:
                if c.header.startswith('gesture '):self.canvas['gestures'][c.header[8:]]=c
                elif c.kind in {'rectangle','circle','line','text'}:self.canvas['primitives'].append(c)
        motion=self.Gtk.EventControllerMotion();motion.connect('motion',self._motion);d.add_controller(motion)
        drag=self.Gtk.GestureDrag();drag.set_button(1);drag.connect('drag-begin',self._drag_begin);drag.connect('drag-update',self._drag_update);drag.connect('drag-end',self._drag_end);d.add_controller(drag)
        scroll=self.Gtk.EventControllerScroll.new(self.Gtk.EventControllerScrollFlags.VERTICAL);scroll.connect('scroll',self._scroll);d.add_controller(scroll)
        empty=self.Adw.StatusPage(title='No content',description='');actions=self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL,spacing=8);actions.set_halign(self.Gtk.Align.CENTER)
        if node:
            for c in node.walk():
                if c.header.startswith('empty when '):
                    self.canvas['empty_condition']=c.header[len('empty when '):]
                    for _,s in c.statements:
                        if m:=re.match(r'icon\s+(\S+)',s):empty.set_icon_name(m.group(1))
                        elif m:=re.match(r'title\s+"([^"]+)"',s):empty.set_title(m.group(1))
                        elif m:=ACTION_RE.match(s):
                            b=self.Gtk.Button(label=m.group(1));b.connect('clicked',lambda _b,d=m.group(2):self.invoke(d));actions.append(b)
        empty.set_child(actions);self.ui.empty=empty;over=self.Gtk.Overlay();over.set_child(d);over.add_overlay(empty);return over
    def _install_keyboard(self,window,node):
        if not node:return
        bindings={}
        for _,s in node.statements:
            m=re.match(r'key\s+"?([^"\s]+)"?\s+(pressed|released)\s+invites\s+(\w+)',s)
            if m:bindings[(m.group(1).lower(),m.group(2))]=m.group(3)
        if not bindings:return
        ctrl=self.Gtk.EventControllerKey()
        def pressed(_c,keyval,_keycode,_state):
            name=(self.Gdk.keyval_name(keyval) or '').lower()
            if name in self._keys:return False
            self._keys.add(name);desire=bindings.get((name,'pressed'))
            if desire:self.invoke(desire)
            return bool(desire)
        def released(_c,keyval,_keycode,_state):
            name=(self.Gdk.keyval_name(keyval) or '').lower();self._keys.discard(name);desire=bindings.get((name,'released'))
            if desire:self.invoke(desire)
        ctrl.connect('key-pressed',pressed);ctrl.connect('key-released',released);window.add_controller(ctrl)
    def _install_clock(self,node):
        if not node:return
        for _,s in node.statements:
            m=re.match(r'every\s+([0-9.]+)(ms|s)\s+invites\s+(\w+)',s)
            if not m:continue
            value,unit,desire=m.groups();ms=max(1,int(float(value)*(1000 if unit=='s' else 1)))
            ident=self.GLib.timeout_add(ms,lambda d=desire:(self.invoke(d),True)[1]);self._timers.append(ident)
    def _css(self):
        p=self.Gtk.CssProvider();p.load_from_string('.mora-canvas { background: @view_bg_color; }');disp=self.Gdk.Display.get_default()
        if disp:self.Gtk.StyleContext.add_provider_for_display(disp,p,self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    def invoke(self,name):
        try:g=self.engine.desire(name)
        except Exception as e:return self._message(str(e))
        self._drive(g)
    def _drive(self,g,value=None,error=None):
        try:req=g.throw(error) if error else g.send(value)
        except StopIteration:return self.refresh()
        except Exception as e:return self._message(str(e))
        if not isinstance(req,Request):return self._drive(g,req)
        provider=self.registry.provider(req.faculty)
        if provider=='desktop.files':return self._files_request(req,g)
        if provider=='desktop.forms':return self._form_request(req,g)
        if not self.registry.is_async(req.faculty):
            try:return self._drive(g,self.registry.invoke(req.faculty,req.operation,req.args,req.options))
            except Exception as e:return self._drive(g,None,e)
        def work():
            try:return self.registry.invoke(req.faculty,req.operation,req.args,req.options),None
            except Exception as e:return None,e
        def done(pair):
            val,err=pair
            if isinstance(err,MissingSecret):return self._secret_dialog(err,lambda:self._dispatch_again(req,g))
            self._drive(g,val,err)
        threading.Thread(target=lambda:self.GLib.idle_add(done,work()),daemon=True).start()
    def _dispatch_again(self,req,g):
        def work():
            try:return self.registry.invoke(req.faculty,req.operation,req.args,req.options),None
            except Exception as e:return None,e
        threading.Thread(target=lambda:self.GLib.idle_add(lambda pair:(self._drive(g,pair[0],pair[1]),False)[1],work()),daemon=True).start()
    def _files_request(self,req,g):
        if req.operation=='numbered-path':return self._drive(g,self.registry.invoke(req.faculty,req.operation,req.args,req.options))
        action=self.Gtk.FileChooserAction.OPEN if req.operation=='choose-image' else self.Gtk.FileChooserAction.SELECT_FOLDER;title=str(req.options.get('title','Choose'))
        q=self.Gtk.FileChooserNative.new(title,self.ui.window,action,'Choose','Cancel')
        if req.operation=='choose-image' and (ext:=req.options.get('extensions')):
            f=self.Gtk.FileFilter();f.set_name('Files')
            for x in str(ext).split():f.add_pattern('*.'+x)
            q.add_filter(f)
        def response(d,c):
            val=None
            if c==self.Gtk.ResponseType.ACCEPT and (f:=d.get_file()):val=f.get_path()
            d.destroy();self._drive(g,val,None if val else MoraError('cancelled'))
        q.connect('response',response);q.show()
    def _form_request(self,req,g):
        if req.operation!='choose':return self._drive(g,None,MoraError(f'unsupported form operation {req.operation}'))
        opts=req.options;items=opts.get('options') or []
        d=self.Gtk.Dialog(title=str(opts.get('title','Choose')),transient_for=self.ui.window,modal=True);d.add_button('Cancel',self.Gtk.ResponseType.CANCEL);d.add_button('OK',self.Gtk.ResponseType.OK);box=d.get_content_area();box.set_spacing(12)
        combo=self.Gtk.ComboBoxText()
        for x in items:combo.append_text(getattr(x,'label',str(x)))
        combo.set_active(0);box.append(combo);spins={}
        for key,val in opts.items():
            if key.startswith('field-'):
                m=re.search(r'default\s+([0-9.]+).*min\s+([0-9.]+).*max\s+([0-9.]+)',str(val))
                if m:
                    sp=self.Gtk.SpinButton.new_with_range(float(m.group(2)),float(m.group(3)),1);sp.set_value(float(m.group(1)));box.append(self.Gtk.Label(label=key[6:]));box.append(sp);spins[key[6:]]=sp
        def response(_d,c):
            if c!=self.Gtk.ResponseType.OK:d.destroy();return self._drive(g,None,MoraError('cancelled'))
            result={'option':items[max(0,combo.get_active())] if items else None}
            for k,sp in spins.items():result[k]=sp.get_value_as_int()
            d.destroy();self._drive(g,result)
        d.connect('response',response);d.present()
    def _secret_dialog(self,missing,after):
        cfg=missing.config;d=self.Gtk.Dialog(title=cfg.get('secret_label',missing.key),transient_for=self.ui.window,modal=True);d.add_button('Cancel',self.Gtk.ResponseType.CANCEL);d.add_button('Save',self.Gtk.ResponseType.OK);box=d.get_content_area();box.set_spacing(12)
        if cfg.get('secret_help'):box.append(self.Gtk.Label(label=cfg['secret_help'],wrap=True,xalign=0))
        entry=self.Gtk.PasswordEntry();entry.set_show_peek_icon(True);box.append(entry)
        if cfg.get('secret_link'):box.append(self.Gtk.LinkButton.new_with_label(cfg['secret_link'],'Open link…'))
        def response(_d,c):
            if c==self.Gtk.ResponseType.OK and (v:=entry.get_text().strip()):
                from .faculties import secret_set;secret_set(self.app_id,missing.key,v,cfg.get('secret_label',missing.key));d.destroy();after();return
            d.destroy()
        d.connect('response',response);d.present()
    def _value(self,expr):
        expr=expr.strip()
        if m:=re.match(r'count\((.+)\)',expr):return len(self.engine.get(m.group(1)) or [])
        if m:=re.match(r'dimensions\((.+)\)',expr):
            v=self._value(m.group(1))
            if hasattr(v,'size'):return f'{v.size[0]} × {v.size[1]}'
            if isinstance(v,(list,tuple)) and len(v)==4:
                l,t,r,b=quad_bounds(v);return f'{int(r-l)} × {int(b-t)}'
            return ''
        if m:=re.match(r'item-at\(([^,]+),\s*([^)]+)\)',expr):
            seq=self.engine.get(m.group(1).strip()) or [];idx=self.engine.get(m.group(2).strip());return seq[idx] if idx is not None and 0<=int(idx)<len(seq) else None
        if m:=re.match(r'percent\((.+)\)',expr):
            v=self.engine.get(m.group(1)) or 0;return f'{round(float(v)*100)}%'
        return self.engine.get(expr)
    def refresh(self):
        if self.ui.status:self.ui.status.set_text(self.engine.message)
        for row,expr,fallback,cond in self.ui.bindings:
            visible=True if not cond else self.engine.condition(cond);row.set_visible(visible)
            val=self._value(expr);row.set_subtitle(str(val if val not in (None,'') else fallback or ''))
        for spin,path in self.ui.numbers:
            v=self.engine.get(path)
            if v is not None and int(spin.get_value())!=int(v):spin.set_value(float(v))
        for _,(b,cond) in self.ui.buttons.items():b.set_sensitive(True if not cond else self.engine.condition(cond))
        if self.ui.empty:self.ui.empty.set_visible(self.engine.condition(self.canvas.get('empty_condition','false')))
        self._ensure_preview();self._redraw()
    def _ensure_preview(self):
        image=self.engine.get(self.canvas.get('image')) if self.canvas.get('image') else None
        if image is None:self.preview=None;return
        if getattr(self,'_preview_source',None) is image:return
        from PIL import Image
        thumb=image.copy();thumb.thumbnail((2400,2400),Image.Resampling.LANCZOS);buf=io.BytesIO();thumb.convert('RGBA').save(buf,format='PNG');loader=self.GdkPixbuf.PixbufLoader.new_with_type('png');loader.write(buf.getvalue());loader.close();self.preview=loader.get_pixbuf();self.preview_ratio=self.preview.get_width()/image.size[0];self._preview_source=image
    def _view(self):
        p=self.canvas.get('view');v=self.engine.get(p) if p else None
        if not isinstance(v,dict):v={'zoom':1.,'x':0.,'y':0.};self.engine.set(p,v)
        return v
    def _transform(self,w,h):
        if not self.preview:return None
        pw,ph=self.preview.get_width(),self.preview.get_height();v=self._view();fit=min(max(1,w)/pw,max(1,h)/ph);scale=fit*float(v.get('zoom',1));return {'scale':scale,'full':scale*self.preview_ratio,'x':(w-pw*scale)/2+v.get('x',0),'y':(h-ph*scale)/2+v.get('y',0)}
    def _screen(self,p,t):return (t['x']+p[0]*t['full'],t['y']+p[1]*t['full'])
    def _image_point(self,p,t):return ((p[0]-t['x'])/t['full'],(p[1]-t['y'])/t['full'])
    def _assist(self):
        name=self.canvas.get('adapt')
        if not name or name not in self.engine.affect.offers:return 7.,0.,False
        n=self.program.declaration('offer',name);handle,snap,mag=7.,0.,False
        if n:
            for _,s in n.statements:
                if m:=re.match(r'handles\s+([0-9.]+)px',s):handle=float(m.group(1))
                elif m:=re.match(r'snapping\s+([0-9.]+)px',s):snap=float(m.group(1))
                elif s.startswith('magnifier '):mag=True
        return handle,snap,mag
    def _shapes(self):return self.engine.get(self.canvas.get('shapes')) or []
    def _focus(self):return self.engine.get(self.canvas.get('focus'))
    def _hit(self,x,y):
        if not self.preview:return None
        t=self._transform(self.ui.drawing.get_width(),self.ui.drawing.get_height());h,_,_=self._assist();focus=self._focus();shapes=self._shapes()
        if focus is not None and 0<=int(focus)<len(shapes):
            pts=[self._screen(p,t) for p in shapes[int(focus)]]
            for i,p in enumerate(pts):
                if math.hypot(x-p[0],y-p[1])<=h+4:return int(focus),'corner',i
            for i,(a,b) in enumerate(((0,1),(1,2),(2,3),(3,0))):
                m=((pts[a][0]+pts[b][0])/2,(pts[a][1]+pts[b][1])/2)
                if math.hypot(x-m[0],y-m[1])<=h+4:return int(focus),'edge',i
        ip=self._image_point((x,y),t)
        for i in range(len(shapes)-1,-1,-1):
            if point_in_quad(ip,shapes[i]):return i,'shape',None
    def _color(self,text,default):
        if not text:return default
        s=str(text).strip()
        if s.startswith('#') and len(s) in (7,9):
            try:
                vals=[int(s[i:i+2],16)/255 for i in range(1,len(s),2)]
                return tuple(vals[:3]+([vals[3]] if len(vals)>3 else [1.]))
            except:pass
        return default
    def _scene_value(self,token):
        token=str(token).strip()
        if token.startswith('"') and token.endswith('"'):return token[1:-1]
        root=token.split('.')[0]
        if root in self.engine.world:return self.engine.get(token)
        try:return float(token)
        except:return token
    def _primitive_props(self,node):
        out={}
        for _,s in node.statements:
            p=s.split(None,1)
            if p:out[p[0]]=self._scene_value(p[1]) if len(p)>1 else True
        return out
    def _world_transform(self,w,h):
        dims=self.canvas.get('coordinates')
        if not dims:return (1.,0.,0.)
        cw,ch=dims;scale=min(max(1,w)/cw,max(1,h)/ch);return scale,(w-cw*scale)/2,(h-ch*scale)/2
    def _draw_primitives(self,cr,w,h):
        if not self.canvas.get('primitives'):return
        scale,ox,oy=self._world_transform(w,h);bg=self._color(self.canvas.get('background'),(.06,.06,.07,1));fg=self._color(self.canvas.get('foreground'),(.95,.95,.95,1))
        cr.save();cr.set_source_rgba(*bg);cr.paint();cr.translate(ox,oy);cr.scale(scale,scale)
        for node in self.canvas['primitives']:
            p=self._primitive_props(node);cr.set_source_rgba(*fg)
            if node.kind=='rectangle':
                cr.rectangle(float(p.get('x',0)),float(p.get('y',0)),float(p.get('width',1)),float(p.get('height',1)));cr.fill()
            elif node.kind=='circle':
                cr.arc(float(p.get('x',0)),float(p.get('y',0)),float(p.get('radius',1)),0,math.tau);cr.fill()
            elif node.kind=='line':
                if p.get('dashed'):cr.set_dash([8,10])
                cr.set_line_width(float(p.get('width',2)));cr.move_to(float(p.get('x1',0)),float(p.get('y1',0)));cr.line_to(float(p.get('x2',0)),float(p.get('y2',0)));cr.stroke();cr.set_dash([])
            elif node.kind=='text':
                cr.select_font_face('Sans');cr.set_font_size(float(p.get('size',24)));cr.move_to(float(p.get('x',0)),float(p.get('y',0)));cr.show_text(str(p.get('value','')))
        cr.restore()
    def _draw(self,_a,cr,w,h):
        self._draw_primitives(cr,w,h)
        if not self.preview:return
        t=self._transform(w,h);cr.save();cr.translate(t['x'],t['y']);cr.scale(t['scale'],t['scale']);self.Gdk.cairo_set_source_pixbuf(cr,self.preview,0,0);cr.paint();cr.restore();focus=self._focus();handle,_,mag=self._assist()
        for i,q in enumerate(self._shapes()):
            pts=[self._screen(p,t) for p in q];cr.move_to(*pts[0])
            for p in pts[1:]:cr.line_to(*p)
            cr.close_path();cr.set_source_rgba(.2,.65,1,.95) if i==focus else cr.set_source_rgba(1,1,1,.82);cr.set_line_width(2.5 if i==focus else 1.6);cr.stroke()
            if i==focus:
                cr.set_source_rgba(.2,.65,1,1)
                for p in pts:cr.arc(p[0],p[1],handle,0,math.tau);cr.fill()
        if mag and self.drag and self.drag.get('kind')!='pan':self._magnifier(cr,w,h,t)
    def _magnifier(self,cr,w,h,t):
        x0,y0=self.hover;ip=self._image_point((x0,y0),t);px,py=ip[0]*self.preview_ratio,ip[1]*self.preview_ratio;size,zoom=150,5;x=min(w-size-12,max(12,x0+24));y=min(h-size-12,max(12,y0+24));cr.save();cr.rectangle(x,y,size,size);cr.clip();cr.translate(x+size/2-px*zoom,y+size/2-py*zoom);cr.scale(zoom,zoom);self.Gdk.cairo_set_source_pixbuf(cr,self.preview,0,0);cr.paint();cr.restore();cr.rectangle(x,y,size,size);cr.set_source_rgba(.15,.55,.95,1);cr.stroke()
    def _motion(self,_c,x,y):self.hover=(x,y);self._redraw() if self.drag else None
    def _gesture(self,name):return self.canvas.get('gestures',{}).get(name)
    def _drag_begin(self,_g,x,y):
        if not self.preview:return
        hit=self._hit(x,y);focus=self._focus();view=copy.deepcopy(self._view())
        if hit is None:self.drag={'kind':'pan','view':view};return
        idx,part,sub=hit
        if focus!=idx:self.engine.set(self.canvas['focus'],idx);self.engine.changed();self.drag={'kind':'pan','view':view};return
        kind='shape' if part=='shape' else part;self.drag={'kind':kind,'index':idx,'part':sub,'start':copy.deepcopy(self._shapes()[idx])}
    def _drag_update(self,_g,dx,dy):
        if not self.drag:return
        if self.drag['kind']=='pan':v=self._view();v['x']=self.drag['view'].get('x',0)+dx;v['y']=self.drag['view'].get('y',0)+dy;return self._redraw()
        t=self._transform(self.ui.drawing.get_width(),self.ui.drawing.get_height());ix,iy=dx/t['full'],dy/t['full'];idx=self.drag['index'];q=self.drag['start'];image=self.engine.get(self.canvas['image']);_,snap,_=self._assist();g=self._gesture(f'drag focused {self.drag["kind"]}')
        op=None
        if g:
            for _,s in g.statements:
                if s.startswith('use '):op=s[4:].strip()
        if not op:return
        alias,operation=op.split('.',1);args=[q,ix,iy,image]
        if operation=='move-corner':args=[q,self.drag['part'],ix,iy,image,snap]
        elif operation=='move-edge':args=[q,self.drag['part'],ix,iy,image,snap]
        self._shapes()[idx]=self.registry.invoke(alias,operation,args);self._redraw()
    def _drag_end(self,_g,dx,dy):
        if not self.drag:return
        kind=self.drag['kind'];self.drag=None
        if kind!='pan':
            g=self._gesture(f'drag focused {kind}')
            if g:
                for _,s in g.statements:
                    if m:=re.match(r'perceive\s+(.+)\s+on finish',s):self.engine.affect.perceive(m.group(1));self.engine.changed()
        self.refresh()
    def _scroll(self,_c,dx,dy):
        if not self.preview:return False
        v=self._view();v['zoom']=max(.35,min(8,float(v.get('zoom',1))*(.88 if dy>0 else 1.14)));g=self._gesture('wheel')
        if g:
            for _,s in g.statements:
                if s.startswith('perceive '):self.engine.affect.perceive(s[9:].strip())
        self.engine.changed();return True
    def _message(self,text):
        if self.ui.toast:self.ui.toast.add_toast(self.Adw.Toast.new(text))
        self.refresh()
    def _redraw(self):
        if self.ui.drawing:self.ui.drawing.queue_draw()
