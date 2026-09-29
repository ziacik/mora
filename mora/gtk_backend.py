from __future__ import annotations

import copy, io, math, os, pathlib, re, threading
from dataclasses import dataclass, field
from typing import Any, Callable
from . import faculties
from .runtime import AffectiveRuntime, MoraError, Node, Program

BUTTON_RE = re.compile(r'^button\s+(?P<label>"[^"]+"|\S+)(?:\s+icon\s+(?P<icon>[\w-]+))?(?:\s+(?P<emph>emphasized))?\s+invites\s+(?P<desire>[A-Za-z_][\w]*)(?:\s+when\s+.+)?$')
ACTION_RE = re.compile(r'^action\s+"([^"]+)"\s+invites\s+([A-Za-z_][\w]*)$')

@dataclass
class UI:
    window: Any = None
    drawing: Any = None
    empty: Any = None
    source: Any = None
    frames: Any = None
    selected: Any = None
    assist: Any = None
    zoom: Any = None
    padding: Any = None
    spinner: Any = None
    status: Any = None
    toast: Any = None
    buttons: dict[str, Any] = field(default_factory=dict)

class GtkBackend:
    def __init__(self, program: Program):
        self.program, self.ui = program, UI()
        self.affect = AffectiveRuntime(program)
        self.affect.on_desire, self.affect.on_change = self.invoke_desire, self.refresh
        self._gtk()
        self.state = {
            "scan": None, "frames": [], "selection": None,
            "history": {"past": [], "future": []},
            "padding": 0, "view": {"zoom": 1.0, "x": 0.0, "y": 0.0},
            "message": "Open an image or scan one to begin.", "busy": False,
            "preview": None, "preview_ratio": 1.0, "drag": None, "hover": (0.0, 0.0),
        }
        self.app_id = self._identity() or "com.github.mora.Application"
        self.model = self._vision_model() or "gpt-5.6-terra"

    def _gtk(self):
        try:
            import gi
            for name, ver in (("Gtk","4.0"),("Adw","1"),("Gdk","4.0"),("GdkPixbuf","2.0")):
                gi.require_version(name, ver)
            from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk
        except Exception as exc:
            raise MoraError("GTK faculty unavailable. On Arch/Manjaro: sudo pacman -S python-gobject gtk4 libadwaita") from exc
        self.Adw, self.Gdk, self.GdkPixbuf, self.GLib, self.Gtk = Adw, Gdk, GdkPixbuf, GLib, Gtk

    def _identity(self):
        apps = self.program.declarations("app")
        for _, s in apps[0].statements if apps else []:
            m = re.match(r'identity\s+"([^"]+)"', s)
            if m: return m.group(1)

    def _vision_model(self):
        for n in self.program.declarations("faculty"):
            if n.name.startswith("vision "):
                for _, s in n.statements:
                    m = re.match(r'model\s+"([^"]+)"', s)
                    if m: return m.group(1)

    def run(self):
        app = self.Adw.Application(application_id=self.app_id)
        app.connect("activate", self._activate)
        app.run([])

    def _activate(self, app):
        scene = self.program.declaration("scene","Main") or self.program.declarations("scene")[0]
        win = scene.child("window")
        if not win: raise MoraError("scene requires a window")
        title, w, h, mw, mh = "Mora", 1100, 760, 640, 480
        for _, s in win.statements:
            if m:=re.match(r'title\s+"([^"]+)"',s): title=m.group(1)
            if m:=re.match(r'size\s+(\d+)\s+x\s+(\d+)',s): w,h=map(int,m.groups())
            if m:=re.match(r'minimum\s+(\d+)\s+x\s+(\d+)',s): mw,mh=map(int,m.groups())
        window=self.Adw.ApplicationWindow(application=app,title=title,default_width=w,default_height=h)
        window.set_size_request(mw,mh); self.ui.window=window
        header=self.Adw.HeaderBar(); header.set_title_widget(self.Adw.WindowTitle(title=title,subtitle="Affective photo sheet editor"))
        if n:=win.child("header"): self._header(header,n)
        sidebar=self._sidebar(win.child("sidebar"))
        canvas=self._canvas(next((c for c in win.children if c.kind=="canvas"),None))
        paned=self.Gtk.Paned(orientation=self.Gtk.Orientation.HORIZONTAL)
        paned.set_start_child(sidebar); paned.set_end_child(canvas); paned.set_position(310); paned.set_resize_start_child(False)
        toast=self.Adw.ToastOverlay(); toast.set_child(paned); self.ui.toast=toast
        toolbar=self.Adw.ToolbarView(); toolbar.add_top_bar(header); toolbar.set_content(toast); window.set_content(toolbar)
        self._css(); self.refresh(); window.present()

    def _button_spec(self,s):
        m=BUTTON_RE.match(s)
        if not m: return None
        label=m.group("label").strip('"')
        return label,m.group("icon"),bool(m.group("emph")),m.group("desire")

    def _button(self,label,icon,emph,desire):
        if icon:
            box=self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL,spacing=6)
            box.append(self.Gtk.Image.new_from_icon_name(icon)); box.append(self.Gtk.Label(label=label))
            b=self.Gtk.Button(); b.set_child(box)
        else: b=self.Gtk.Button(label=label)
        if emph: b.add_css_class("suggested-action")
        b.connect("clicked",lambda _b,d=desire:self.invoke_desire(d)); self.ui.buttons[desire]=b
        return b

    def _header(self,header,node):
        for _,s in node.statements:
            if spec:=self._button_spec(s):
                b=self._button(*spec)
                (header.pack_end if spec[2] or spec[3] in {"Undo","Redo","FitView"} else header.pack_start)(b)

    def _sidebar(self,node):
        G,A=self.Gtk,self.Adw
        side=G.Box(orientation=G.Orientation.VERTICAL,spacing=18); side.set_size_request(310,-1)
        for e,v in (("top",18),("bottom",14),("start",16),("end",16)): getattr(side,f"set_margin_{e}")(v)
        for group in node.children if node else []:
            if group.kind!="group": continue
            title=group.name.strip('"'); prefs=A.PreferencesGroup(title=title)
            if title=="Source":
                self.ui.source=A.ActionRow(title="No scan loaded",subtitle="PNG, JPEG, TIFF or SANE scanner"); prefs.add(self.ui.source)
            elif title=="Frames":
                self.ui.frames=A.ActionRow(title="Detected frames",subtitle="0 frames")
                for _,s in group.statements:
                    if spec:=self._button_spec(s):
                        b=self._button(*spec)
                        if spec[3]=="DeleteFrame": b.add_css_class("destructive-action")
                        self.ui.frames.add_suffix(b)
                prefs.add(self.ui.frames); self.ui.selected=A.ActionRow(title="Selected frame",subtitle=""); self.ui.selected.set_visible(False); prefs.add(self.ui.selected)
            elif title=="Crop":
                row=A.ActionRow(title="Padding",subtitle="Extra pixels around AI-detected prints")
                self.ui.padding=G.SpinButton.new_with_range(0,100,1); self.ui.padding.connect("value-changed",lambda s:self.state.__setitem__("padding",s.get_value_as_int()))
                row.add_suffix(self.ui.padding); prefs.add(row)
            elif title=="Adaptive assistance":
                self.ui.assist=A.ActionRow(title="Interaction model",subtitle="Learning your editing rhythm"); prefs.add(self.ui.assist)
            elif title=="View":
                self.ui.zoom=A.ActionRow(title="Zoom",subtitle="100%"); prefs.add(self.ui.zoom)
            side.append(prefs)
        row=G.Box(orientation=G.Orientation.HORIZONTAL,spacing=8); row.set_valign(G.Align.END); row.set_vexpand(True)
        self.ui.spinner=G.Spinner(); self.ui.spinner.set_visible(False)
        self.ui.status=G.Label(label=self.state["message"]); self.ui.status.set_wrap(True); self.ui.status.set_xalign(0); self.ui.status.add_css_class("dim-label")
        row.append(self.ui.spinner); row.append(self.ui.status); side.append(row); return side

    def _canvas(self,node):
        G,A=self.Gtk,self.Adw
        d=G.DrawingArea(); d.set_hexpand(True); d.set_vexpand(True); d.set_focusable(True); d.add_css_class("scan-canvas"); d.set_draw_func(self._draw); self.ui.drawing=d
        motion=G.EventControllerMotion(); motion.connect("motion",self._motion); d.add_controller(motion)
        drag=G.GestureDrag(); drag.set_button(1); drag.connect("drag-begin",self._drag_begin); drag.connect("drag-update",self._drag_update); drag.connect("drag-end",self._drag_end); d.add_controller(drag)
        scroll=G.EventControllerScroll.new(G.EventControllerScrollFlags.VERTICAL); scroll.connect("scroll",self._scroll); d.add_controller(scroll)
        empty=A.StatusPage(title="Open or scan an image",description="Open PNG, JPEG or TIFF, or scan directly from a SANE-compatible scanner",icon_name="scanner-symbolic")
        actions=G.Box(orientation=G.Orientation.HORIZONTAL,spacing=8); actions.set_halign(G.Align.CENTER); specs=[]
        if node:
            for n in node.walk():
                if n.header=="offer Empty":
                    for _,s in n.statements:
                        if m:=ACTION_RE.match(s): specs.append(m.groups())
        for label,desire in specs or [("Scan from Scanner…","ScanImage"),("Open Image…","OpenScan")]:
            b=G.Button(label=label); b.connect("clicked",lambda _b,d=desire:self.invoke_desire(d)); actions.append(b)
        empty.set_child(actions); self.ui.empty=empty
        over=G.Overlay(); over.set_child(d); over.add_overlay(empty); return over

    def _css(self):
        p=self.Gtk.CssProvider()
        css=b".scan-canvas { background: @view_bg_color; } .dim-label { opacity: .72; }"
        try: p.load_from_data(css)
        except TypeError: p.load_from_string(css.decode())
        if disp:=self.Gdk.Display.get_default(): self.Gtk.StyleContext.add_provider_for_display(disp,p,self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def invoke_desire(self,name):
        node=self.program.declaration("desire",name)
        if not node: return self._message(f"Unknown desire: {name}")
        text=node.text()
        if "requires scan" in text and not self.state["scan"]: return
        if "requires selection" in text and self.state["selection"] is None: return
        if "ask user for image-file through files" in text: self._open()
        elif "ask user for scanner among scanner.discover" in text: self._scan()
        elif "ask vision to perceive PhysicalPhoto" in text: self._detect()
        elif "imagine frame centered in scan" in text: self._add()
        elif "remove selection from frames" in text: self._delete()
        elif "restore previous world" in text: self._undo()
        elif "restore next world" in text: self._redo()
        elif "make view fitted" in text: self._fit()
        elif "ask user for folder through files" in text: self._export()
        else: self._message(f"No faculty can fulfill {name} yet.")

    def _open(self):
        q=self.Gtk.FileChooserNative.new("Open scan",self.ui.window,self.Gtk.FileChooserAction.OPEN,"Open","Cancel")
        f=self.Gtk.FileFilter(); f.set_name("Images")
        for p in ("*.png","*.jpg","*.jpeg","*.tif","*.tiff"): f.add_pattern(p)
        q.add_filter(f)
        def response(d,c):
            if c==self.Gtk.ResponseType.ACCEPT and (file:=d.get_file()) and (path:=file.get_path()):
                self._busy(True,f"Loading {pathlib.Path(path).name}…"); self._bg(lambda:faculties.load_image(path),lambda v,e:self._image_ready(v,e,path))
            d.destroy()
        q.connect("response",response); q.show()

    def _image_ready(self,image,error,source):
        if error: self._busy(False,f"Could not open image: {error}"); return
        self.state.update(scan={"image":image,"source":source},frames=[],selection=None,history={"past":[],"future":[]},view={"zoom":1.0,"x":0.0,"y":0.0})
        self._preview(); self._busy(False,f"Loaded {pathlib.Path(source).name}"); self.affect.perceive("scan arrived"); self._redraw()

    def _scan(self):
        self._busy(True,"Looking for scanners…"); self._bg(faculties.list_scanners,self._scanner_list)

    def _scanner_list(self,devices,error):
        self._busy(False)
        if error: self.affect.perceive("failure"); return self._message(str(error))
        if not devices: return self._message("No SANE scanners found.")
        d=self.Gtk.Dialog(title="Scan from scanner",transient_for=self.ui.window,modal=True); d.add_button("Cancel",self.Gtk.ResponseType.CANCEL); d.add_button("Scan",self.Gtk.ResponseType.OK)
        box=d.get_content_area(); box.set_spacing(12)
        combo=self.Gtk.ComboBoxText()
        for dev in devices: combo.append_text(dev.label)
        combo.set_active(0); dpi=self.Gtk.SpinButton.new_with_range(75,1200,25); dpi.set_value(600)
        box.append(combo); box.append(dpi)
        def response(dlg,code):
            if code==self.Gtk.ResponseType.OK:
                dev=devices[max(0,combo.get_active())]; res=int(dpi.get_value()); dlg.destroy(); self._busy(True,f"Scanning at {res} DPI…")
                self._bg(lambda:faculties.acquire_scan(dev.id,res),lambda v,e:self._scan_ready(v,e,dev.label)); return
            dlg.destroy()
        d.connect("response",response); d.present()

    def _scan_ready(self,image,error,label):
        if error: self._busy(False,f"Scan failed: {error}"); self.affect.perceive("failure"); return
        self.state.update(scan={"image":image,"source":label},frames=[],selection=None,history={"past":[],"future":[]},view={"zoom":1.0,"x":0.0,"y":0.0})
        self._preview(); self._busy(False,f"Scanned from {label}."); self.affect.perceive("scan arrived"); self._redraw()

    def _detect(self):
        if self.state["busy"] or not self.state["scan"]: return
        try: key=faculties.keyring_get(self.app_id,"openai-api-key")
        except Exception as e: return self._message(str(e))
        if not key: return self._api_key(self._detect)
        if self.state["frames"]: self.affect.perceive("repeated redetection")
        image=self.state["scan"]["image"].copy(); margin=self.state["padding"]; self._busy(True,"Detecting physical photos…")
        self._bg(lambda:faculties.detect_photos_openai(image,key,self.model,margin),self._detect_ready)

    def _api_key(self,after):
        d=self.Gtk.Dialog(title="OpenAI API key",transient_for=self.ui.window,modal=True); d.add_button("Cancel",self.Gtk.ResponseType.CANCEL); d.add_button("Save",self.Gtk.ResponseType.OK)
        box=d.get_content_area(); box.set_spacing(12); entry=self.Gtk.PasswordEntry(); entry.set_show_peek_icon(True)
        box.append(self.Gtk.Label(label="Photo detection uses your OpenAI API key. It is stored in the system keyring.",xalign=0)); box.append(entry)
        box.append(self.Gtk.LinkButton.new_with_label("https://platform.openai.com/api-keys","Create an API key…"))
        def response(dlg,code):
            if code==self.Gtk.ResponseType.OK and (value:=entry.get_text().strip()):
                try: faculties.keyring_set(self.app_id,"openai-api-key",value,"Scan Slicer Emo OpenAI API key"); dlg.destroy(); after(); return
                except Exception as e: self._message(str(e))
            dlg.destroy()
        d.connect("response",response); d.present()

    def _detect_ready(self,frames,error):
        self._busy(False)
        if error: self.affect.perceive("failure"); return self._message(f"Detection failed: {error}")
        self.state.update(frames=frames,selection=None,history={"past":[],"future":[]}); self._message(f"Detected {len(frames)} photo{'s' if len(frames)!=1 else ''}."); self.affect.perceive("detection succeeded")
        baseline=self.affect.event_counts.get("correction(frame)",0)
        def stable():
            if self.affect.event_counts.get("correction(frame)",0)==baseline: self.affect.perceive("detection succeeded and no correction for 20s")
            return False
        self.GLib.timeout_add_seconds(20,stable); self._redraw()

    def _push(self):
        h=self.state["history"]; h["past"].append({"frames":copy.deepcopy(self.state["frames"]),"selection":self.state["selection"]}); h["past"]=h["past"][-100:]; h["future"].clear()

    def _add(self):
        self._push(); w,h=self.state["scan"]["image"].size; fw,fh=max(100,w/3),max(100,h/3); x,y=(w-fw)/2,(h-fh)/2
        self.state["frames"].append({"corners":[[x,y],[x+fw,y],[x+fw,y+fh],[x,y+fh]]}); self.state["selection"]=len(self.state["frames"])-1
        self.affect.perceive("user adds missing frame"); self._message("Added a frame."); self._redraw()

    def _delete(self):
        i=self.state["selection"]
        if i is None:return
        self._push(); self.state["frames"].pop(i); self.state["selection"]=None; self.affect.perceive("user deletes detected frame"); self._redraw()

    def _undo(self):
        h=self.state["history"]
        if not h["past"]:return
        h["future"].append({"frames":copy.deepcopy(self.state["frames"]),"selection":self.state["selection"]}); s=h["past"].pop(); self.state["frames"],self.state["selection"]=s["frames"],s["selection"]; self.affect.perceive("undo"); self._redraw()

    def _redo(self):
        h=self.state["history"]
        if not h["future"]:return
        h["past"].append({"frames":copy.deepcopy(self.state["frames"]),"selection":self.state["selection"]}); s=h["future"].pop(); self.state["frames"],self.state["selection"]=s["frames"],s["selection"]; self._redraw()

    def _fit(self):
        self.state["view"]={"zoom":1.0,"x":0.0,"y":0.0}; self.refresh(); self._redraw()

    def _export(self):
        if not self.state["frames"]:return
        q=self.Gtk.FileChooserNative.new("Export photos",self.ui.window,self.Gtk.FileChooserAction.SELECT_FOLDER,"Export","Cancel")
        def response(d,c):
            if c==self.Gtk.ResponseType.ACCEPT and (f:=d.get_file()) and (folder:=f.get_path()): self._export_to(folder)
            d.destroy()
        q.connect("response",response); q.show()

    def _export_to(self,folder):
        self._busy(True,f"Exporting {len(self.state['frames'])} photos…"); image=self.state["scan"]["image"].copy(); frames=copy.deepcopy(self.state["frames"])
        source=str(self.state["scan"]["source"]); stem=pathlib.Path(source).stem if os.path.isfile(source) else "scan"
        def work():
            for i,f in enumerate(frames,1): faculties.perspective_crop(image,f["corners"]).save(os.path.join(folder,f"{stem}_{i:02}.png"),format="PNG")
            return len(frames)
        def done(n,e):
            self._busy(False)
            if e:self.affect.perceive("failure"); self._message(f"Export failed: {e}")
            else:self.affect.perceive("export succeeded"); self._message(f"Exported {n} photos to {folder}")
        self._bg(work,done)

    def _preview(self):
        from PIL import Image
        image=self.state["scan"]["image"]; thumb=image.copy(); thumb.thumbnail((2400,2400),Image.Resampling.LANCZOS)
        buf=io.BytesIO(); thumb.convert("RGBA").save(buf,format="PNG"); loader=self.GdkPixbuf.PixbufLoader.new_with_type("png"); loader.write(buf.getvalue()); loader.close()
        self.state["preview"]=loader.get_pixbuf(); self.state["preview_ratio"]=self.state["preview"].get_width()/image.size[0]

    def _transform(self,w,h):
        p=self.state["preview"]
        if not p:return None
        pw,ph=p.get_width(),p.get_height(); fit=min(max(1,w)/pw,max(1,h)/ph); v=self.state["view"]; scale=fit*v["zoom"]
        return {"scale":scale,"full":scale*self.state["preview_ratio"],"x":(w-pw*scale)/2+v["x"],"y":(h-ph*scale)/2+v["y"]}

    def _screen(self,p,t):return (t["x"]+p[0]*t["full"],t["y"]+p[1]*t["full"])
    def _image(self,p,t):return ((p[0]-t["x"])/t["full"],(p[1]-t["y"])/t["full"])
    def _mid(self,a,b):return ((a[0]+b[0])/2,(a[1]+b[1])/2)
    def _assist(self):return (11,24,True) if "PreciseEditing" in self.affect.offers else (7,0,False)

    def _hit(self,x,y):
        if not self.state["preview"]:return None
        t=self._transform(self.ui.drawing.get_width(),self.ui.drawing.get_height()); handle,_,_=self._assist(); sel=self.state["selection"]
        if sel is not None and sel<len(self.state["frames"]):
            pts=[self._screen(p,t) for p in self.state["frames"][sel]["corners"]]
            for i,p in enumerate(pts):
                if math.hypot(x-p[0],y-p[1])<=handle+4:return sel,"corner",i
            for i,(a,b) in enumerate(((0,1),(1,2),(2,3),(3,0))):
                m=self._mid(pts[a],pts[b])
                if math.hypot(x-m[0],y-m[1])<=handle+4:return sel,"edge",i
        ip=self._image((x,y),t)
        for i in range(len(self.state["frames"])-1,-1,-1):
            if faculties.point_in_quad(ip,self.state["frames"][i]["corners"]):return i,"move",None

    def _draw(self,_a,cr,w,h):
        p=self.state["preview"]
        if not p:return
        t=self._transform(w,h); cr.save(); cr.translate(t["x"],t["y"]); cr.scale(t["scale"],t["scale"]); self.Gdk.cairo_set_source_pixbuf(cr,p,0,0); cr.paint(); cr.restore()
        handle,_,mag=self._assist()
        for i,f in enumerate(self.state["frames"]):
            pts=[self._screen(q,t) for q in f["corners"]]; cr.move_to(*pts[0])
            for q in pts[1:]:cr.line_to(*q)
            cr.close_path(); cr.set_source_rgba(.2,.65,1,.95) if i==self.state["selection"] else cr.set_source_rgba(1,1,1,.82); cr.set_line_width(2.5 if i==self.state["selection"] else 1.6); cr.stroke()
            if i==self.state["selection"]:
                cr.set_source_rgba(.2,.65,1,1)
                for q in pts:cr.arc(q[0],q[1],handle,0,math.tau);cr.fill()
                for a,b in ((0,1),(1,2),(2,3),(3,0)):
                    q=self._mid(pts[a],pts[b]);cr.arc(q[0],q[1],max(4,handle-2),0,math.tau);cr.fill()
        if mag and self.state["drag"] and self.state["drag"]["kind"]!="pan":self._magnifier(cr,w,h,t)

    def _magnifier(self,cr,w,h,t):
        x0,y0=self.state["hover"]; ip=self._image((x0,y0),t); ratio=self.state["preview_ratio"]; px,py=ip[0]*ratio,ip[1]*ratio; size,zoom=150,5
        x=min(w-size-12,max(12,x0+24)); y=min(h-size-12,max(12,y0+24)); cr.save(); cr.rectangle(x,y,size,size);cr.clip();cr.translate(x+size/2-px*zoom,y+size/2-py*zoom);cr.scale(zoom,zoom);self.Gdk.cairo_set_source_pixbuf(cr,self.state["preview"],0,0);cr.paint();cr.restore();cr.rectangle(x,y,size,size);cr.set_source_rgba(.15,.55,.95,1);cr.stroke()

    def _motion(self,_c,x,y):self.state["hover"]=(x,y); self._redraw() if self.state["drag"] else None

    def _drag_begin(self,_g,x,y):
        if not self.state["scan"]:return
        hit=self._hit(x,y)
        if not hit:self.state["selection"]=None;self.state["drag"]={"kind":"pan","view":copy.deepcopy(self.state["view"])}
        elif self.state["selection"]==hit[0]:self._push();self.state["drag"]={"kind":hit[1],"part":hit[2],"index":hit[0],"frame":copy.deepcopy(self.state["frames"][hit[0]])}
        else:self.state["selection"]=hit[0];self.state["drag"]={"kind":"pan","view":copy.deepcopy(self.state["view"])}
        self.refresh();self._redraw()

    def _drag_update(self,_g,dx,dy):
        d=self.state["drag"]
        if not d:return
        if d["kind"]=="pan":self.state["view"]["x"]=d["view"]["x"]+dx;self.state["view"]["y"]=d["view"]["y"]+dy;return self._redraw()
        t=self._transform(self.ui.drawing.get_width(),self.ui.drawing.get_height());ix,iy=dx/t["full"],dy/t["full"];i=d["index"];pts=d["frame"]["corners"];w,h=self.state["scan"]["image"].size;_,snap,_=self._assist()
        if d["kind"]=="move":new=faculties.move_quad(pts,ix,iy,w,h)
        elif d["kind"]=="corner":new=faculties.move_corner(pts,d["part"],ix,iy,w,h,snap)
        else:
            a,b=((0,1),(1,2),(2,3),(3,0))[d["part"]];new=faculties.move_edge(pts,a,b,ix,iy,w,h,snap)
        self.state["frames"][i]["corners"]=new;self._redraw()

    def _drag_end(self,_g,dx,dy):
        d=self.state["drag"];self.state["drag"]=None
        if d and d["kind"]!="pan":self.affect.perceive("correction(frame)");self._message(f"Adjusted frame {d['index']+1}.")
        self.refresh();self._redraw()

    def _scroll(self,_c,dx,dy):
        if not self.state["scan"]:return False
        z=self.state["view"]["zoom"];self.state["view"]["zoom"]=max(.35,min(8,z*(.88 if dy>0 else 1.14)));self.affect.perceive("zooming");self.refresh();self._redraw();return True

    def _bg(self,work,done):
        def run():
            try:v,e=work(),None
            except Exception as exc:v,e=None,exc
            self.GLib.idle_add(lambda:(done(v,e),False)[1])
        threading.Thread(target=run,daemon=True).start()

    def _busy(self,value,message=None):
        self.state["busy"]=value
        if message is not None:self.state["message"]=message
        self.refresh()

    def _message(self,text):self.state["message"]=text;self.refresh()
    def _redraw(self):self.ui.drawing.queue_draw() if self.ui.drawing else None

    def refresh(self):
        if self.ui.status:self.ui.status.set_text(self.state["message"])
        if self.ui.spinner:self.ui.spinner.set_visible(self.state["busy"]);self.ui.spinner.set_spinning(self.state["busy"])
        if self.ui.empty:self.ui.empty.set_visible(not self.state["scan"])
        scan=self.state["scan"]
        if self.ui.source:
            if scan:self.ui.source.set_title(pathlib.Path(str(scan["source"])).name);self.ui.source.set_subtitle(f"{scan['image'].size[0]} × {scan['image'].size[1]} px")
            else:self.ui.source.set_title("No scan loaded");self.ui.source.set_subtitle("PNG, JPEG, TIFF or SANE scanner")
        if self.ui.frames:self.ui.frames.set_subtitle(f"{len(self.state['frames'])} frame{'s' if len(self.state['frames'])!=1 else ''}")
        if self.ui.selected:
            i=self.state["selection"];self.ui.selected.set_visible(i is not None)
            if i is not None and i<len(self.state["frames"]):
                l,t,r,b=faculties.frame_bounds(self.state["frames"][i]["corners"]);self.ui.selected.set_title(f"Frame {i+1}");self.ui.selected.set_subtitle(f"{int(r-l)} × {int(b-t)} px")
        if self.ui.assist:self.ui.assist.set_subtitle("Precise assistance" if "PreciseEditing" in self.affect.offers else "Learning your editing rhythm")
        if self.ui.zoom:self.ui.zoom.set_subtitle(f"{round(self.state['view']['zoom']*100)}%")
        idle=not self.state["busy"]
        for desire,b in self.ui.buttons.items():
            text=(self.program.declaration("desire",desire).text() if self.program.declaration("desire",desire) else "")
            ok=idle and ("requires scan" not in text or bool(scan)) and ("requires selection" not in text or self.state["selection"] is not None)
            if desire=="ExportPhotos":ok=ok and bool(self.state["frames"])
            if desire=="Undo":ok=ok and bool(self.state["history"]["past"])
            if desire=="Redo":ok=ok and bool(self.state["history"]["future"])
            b.set_sensitive(ok)
