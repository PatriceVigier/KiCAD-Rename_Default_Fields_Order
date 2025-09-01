# V_ChangeDefaultFieldsOrder.py
# KiCad 9 — Action plugin to reorder the global Default Fields for Eeschema.
#
# What it edits:
#   %APPDATA%\kicad\9.0\eeschema.json  →  drawing.field_names
#
# The "field_names" value is an S-expression string like:
#   (templatefields
#     (field (name "MANUFACTURER") visible)
#     (field (name "COMPONENT_LINK_URL") url)
#     (field (name "ZZZ") visible url)
#     ...)
#
# This plugin:
#   • parses that string and extracts each field name plus any flags that follow
#     the name (e.g., "visible", "url", "visible url").
#   • shows a UI to reorder fields (buttons or Alt+Up / Alt+Down), and to add/remove.
#   • preserves flags and writes them INSIDE the same (field …) pair when saving.
#   • writes a `.bak` backup of eeschema.json, then saves.
#   • offers to close KiCad so changes take effect next launch.
#
# Implementation notes:
#   • Keep it as a single file so users can drop it into KiCad's plugin folder.
#   • Avoid external deps; rely on KiCad's shipped `pcbnew` and `wx` (wxPython).
#   • All I/O is guarded; failures are user-friendly.

import os, sys, json, shutil, re
import pcbnew          # KiCad's Python API (shipped with Pcbnew)
import wx              # KiCad bundles wxPython runtime

PLUGIN_NAME = "Change Default Fields Order (drawing.field_names)"
PLUGIN_CATEGORY = "Utility"
PLUGIN_DESCRIPTION = "Reorder Eeschema 'Default Fields' in drawing.field_names (eeschema.json)"
EXPORT_FILENAME = "default_fields_order.json"

# ------------------------------ Paths ------------------------------

def eeschema_json_path():
    """
    Resolve the user's eeschema.json path.
    Prefers KICAD_CONFIG_HOME, then platform defaults.
    Returns None if nothing exists (caller may prompt).
    """
    kch = os.environ.get("KICAD_CONFIG_HOME")
    if kch:
        p = os.path.join(os.path.abspath(os.path.expanduser(kch)), "eeschema.json")
        if os.path.isfile(p):
            return p
    if sys.platform.startswith("win"):
        appdata = os.getenv("APPDATA", "")
        if appdata:
            p = os.path.join(appdata, "kicad", "9.0", "eeschema.json")
            if os.path.isfile(p):
                return p
    elif sys.platform.startswith("darwin"):
        p = os.path.expanduser("~/Library/Preferences/kicad/9.0/eeschema.json")
        if os.path.isfile(p):
            return p
    else:
        p = os.path.expanduser("~/.config/kicad/9.0/eeschema.json")
        if os.path.isfile(p):
            return p
    return None

# ------------------------- S-expression helpers --------------------

# Regex that captures:
#   group 1 → field name (quoted, possibly with escaped quotes/backslashes)
#   group 2 → anything up to the closing ) of this field (flags, e.g. " visible url")
FIELD_RE = re.compile(
    r'\(field\s+\(name\s+"((?:[^"\\]|\\.)*)"\)\s*([^\)]*)\)',
    re.DOTALL
)

def _unescape(s: str) -> str:
    """Unescape \" and \\ as stored in JSON string literal."""
    return s.replace(r'\"', '"').replace(r'\\', '\\')

def _escape(s: str) -> str:
    """Escape quotes/backslashes so the name survives JSON → S-expr roundtrip."""
    return s.replace('\\', r'\\').replace('"', r'\"')

class FieldItem:
    """
    A single (field ...) entry:
      name   → the field name
      suffix → the raw tail captured after (name "..."), e.g. " visible", " url", " visible url".
               We keep it "raw" to preserve unknown tokens/spacing.
    """
    def __init__(self, name: str, suffix: str = ""):
        self.name = name
        self.suffix = (suffix or "")

    def with_suffix_inside(self) -> str:
        """
        Rebuild the exact (field ...) S-expression, keeping flags INSIDE.
        Ensures there is exactly one space before flags (if any).
        """
        suf = self.suffix.strip()
        suf = f" {suf}" if suf else ""
        return f'(field (name "{_escape(self.name)}"){suf})'

def parse_field_names_sexpr(sexpr: str):
    """
    Parse drawing.field_names into a list[FieldItem].
    Robust even if flags are absent or spacing varies.
    """
    items = []
    for m in FIELD_RE.finditer(sexpr or ""):
        name = _unescape(m.group(1))
        suffix = m.group(2) or ""
        items.append(FieldItem(name, suffix))
    return items

def build_field_names_sexpr(items):
    """
    Assemble the full S-expression for field_names.
    """
    return "(templatefields" + "".join(it.with_suffix_inside() for it in items) + ")"

# ---------------------------- Read / Write -------------------------

def load_drawing_field_names(cfg_path):
    """
    Read eeschema.json and return (data, drawing_dict, items).
    If the key is missing, items will be an empty list (safe to edit/insert).
    """
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    drawing = data.get("drawing", {})
    sexpr = drawing.get("field_names", "")
    items = parse_field_names_sexpr(sexpr)
    return data, drawing, items

def save_drawing_field_names(cfg_path, data, drawing, items):
    """
    Write back drawing.field_names, creating a .bak backup first.
    """
    try:
        shutil.copy2(cfg_path, cfg_path + ".bak")
    except Exception:
        pass
    drawing["field_names"] = build_field_names_sexpr(items)
    data["drawing"] = drawing
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ------------------------------- UI --------------------------------

ALT_UP_ID = 1001
ALT_DOWN_ID = 1002

class OrderDialog(wx.Dialog):
    """
    Main dialog:
      • listbox with the current field order
      • buttons + keyboard shortcuts for reordering
      • import/export
      • save & optional restart
    """
    def __init__(self, parent, cfg_path, data, drawing, items):
        super().__init__(parent, title="Reorder Default Fields (drawing.field_names)", size=(860, 640))
        self.cfg_path, self.data, self.drawing = cfg_path, data, drawing
        self.items = items[:]     # type: list[FieldItem]
        self.saved = False        # track if we already saved & prompted

        pnl = wx.Panel(self)
        v = wx.BoxSizer(wx.VERTICAL)

        info = wx.StaticText(
            pnl,
            label=(f"Config file: {cfg_path}\n"
                  # f'Key: "drawing.field_names"   Format: S-expression\n'
                   #"Reorder / Add / Remove. Save creates a .bak.\n"
                   "Alt+Up/Down shortcuts to move line \n"
                   "After saving you will be prompted to restart KiCad to apply changes.")
        )
        v.Add(info, 0, wx.ALL | wx.EXPAND, 8)

        self.lb = wx.ListBox(pnl, choices=[it.name for it in self.items], style=wx.LB_SINGLE)
        v.Add(self.lb, 1, wx.ALL | wx.EXPAND, 8)

        # Toolbar row
        row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in [
            ("Up", self.on_up), ("Down", self.on_down),
            ("Add…", self.on_add), ("Delete", self.on_del),
            ("Export…", self.on_export), ("Import…", self.on_import)
        ]:
            b = wx.Button(pnl, label=label)
            b.Bind(wx.EVT_BUTTON, handler)
            row.Add(b, 0, wx.RIGHT, 6)

        # Explicit Save + Cancel in the row
        self.btn_save = wx.Button(pnl, label="Save change(s) and Restart KiCAD")
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_apply)
        row.Add(self.btn_save, 0, wx.RIGHT, 6)

        self.btn_cancel = wx.Button(pnl, label="Cancel")
        self.btn_cancel.Bind(wx.EVT_BUTTON, lambda evt: self.EndModal(wx.ID_CANCEL))
        row.Add(self.btn_cancel, 0)

        v.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        v.Add(wx.StaticLine(pnl), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        # Bottom Save/Cancel (for users who expect standard dialog buttons)
        btns = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        ok = self.FindWindowById(wx.ID_OK, self)
        cancel = self.FindWindowById(wx.ID_CANCEL, self)
        if ok: ok.SetLabel("Save + Restart")
        if cancel: cancel.SetLabel("Cancel")
        v.Add(btns, 0, wx.ALL | wx.EXPAND, 8)

        pnl.SetSizer(v)
        s = wx.BoxSizer(wx.VERTICAL); s.Add(pnl, 1, wx.EXPAND)
        self.SetSizer(s); self.Layout()

        # Keyboard shortcuts
        accel = wx.AcceleratorTable([
            (wx.ACCEL_CTRL, ord('S'), self.btn_save.GetId()),  # Ctrl+S to save
            (wx.ACCEL_ALT, wx.WXK_UP,  ALT_UP_ID),             # Alt+Up / Alt+Down to move
            (wx.ACCEL_ALT, wx.WXK_DOWN, ALT_DOWN_ID),
        ])
        self.SetAcceleratorTable(accel)
        self.Bind(wx.EVT_MENU, self._accel_alt_up,   id=ALT_UP_ID)
        self.Bind(wx.EVT_MENU, self._accel_alt_down, id=ALT_DOWN_ID)

    # --------- list operations ---------

    def refresh(self, sel=None):
        """Refresh listbox items and keep a sensible selection."""
        self.lb.Set([it.name for it in self.items])
        if sel is None:
            if self.items:
                self.lb.SetSelection(0)
            else:
                self.lb.SetSelection(wx.NOT_FOUND)
        else:
            if 0 <= sel < len(self.items):
                self.lb.SetSelection(sel)
            else:
                self.lb.SetSelection(wx.NOT_FOUND)

    def _move_up(self):
        i = self.lb.GetSelection()
        if i == wx.NOT_FOUND or i == 0:
            return
        self.items[i-1], self.items[i] = self.items[i], self.items[i-1]
        self.refresh(i-1)

    def _move_down(self):
        i = self.lb.GetSelection()
        if i == wx.NOT_FOUND or i >= len(self.items)-1:
            return
        self.items[i+1], self.items[i] = self.items[i], self.items[i+1]
        self.refresh(i+1)

    def on_up(self, evt):   self._move_up()
    def on_down(self, evt): self._move_down()
    def _accel_alt_up(self, evt):   self._move_up()
    def _accel_alt_down(self, evt): self._move_down()

    def on_add(self, evt):
        with wx.TextEntryDialog(self, "Field name (UPPERCASE recommended):", "Add Field") as d:
            if d.ShowModal() == wx.ID_OK:
                name = d.GetValue().strip()
                if name and all(it.name != name for it in self.items):
                    self.items.append(FieldItem(name))
                    self.refresh(len(self.items)-1)

    def on_del(self, evt):
        i = self.lb.GetSelection()
        if i == wx.NOT_FOUND:
            return
        del self.items[i]
        self.refresh(min(i, len(self.items)-1))

    def on_export(self, evt):
        base_dir = os.path.dirname(self.cfg_path)
        with wx.FileDialog(self, "Export order (names only) to JSON",
                           defaultDir=base_dir,
                           defaultFile=EXPORT_FILENAME,
                           wildcard="JSON (*.json)|*.json|All (*.*)|*.*",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as fd:
            if fd.ShowModal() == wx.ID_OK:
                path = fd.GetPath()
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"fields": [it.name for it in self.items]}, f, indent=2, ensure_ascii=False)
                wx.MessageBox(f"Exported to:\n{path}", "Export", wx.ICON_INFORMATION)

    def on_import(self, evt):
        base_dir = os.path.dirname(self.cfg_path)
        default_name = "eeschema.json.bak"
        with wx.FileDialog(self, "Import order (names only) from JSON",
                           defaultDir=base_dir,
                           defaultFile=default_name,
                           wildcard="JSON (*.json)|*.json|All (*.*)|*.*",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fd:
            if fd.ShowModal() == wx.ID_OK:
                path = fd.GetPath()
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                names = data.get("fields")
                if isinstance(names, list):
                    # Keep existing flags for present names; append unknowns at the end.
                    suffix_map = {it.name: it.suffix for it in self.items}
                    new_items, seen = [], set()
                    for n in names:
                        if isinstance(n, str) and n not in seen:
                            new_items.append(FieldItem(n, suffix_map.get(n, "")))
                            seen.add(n)
                    for it in self.items:
                        if it.name not in seen:
                            new_items.append(it)
                    self.items = new_items
                    self.refresh(0)
                else:
                    wx.MessageBox("Invalid JSON (missing 'fields' array).", "Import", wx.ICON_WARNING)

    # --------- restart handling ---------

    def _close_kicad_safely(self):
        """
        Try to close KiCad gracefully:
          1) Close the Pcbnew frame (preferred).
          2) Close all top-level frames.
          3) Ask the app to exit its main loop.
        """
        try:
            frame = pcbnew.GetPcbFrame()
            if frame:
                frame.Close(True)
                return
        except Exception:
            pass
        try:
            for w in wx.GetTopLevelWindows():
                if isinstance(w, wx.Frame):
                    w.Close(True)
            return
        except Exception:
            pass
        try:
            app = wx.GetApp()
            if hasattr(app, "ExitMainLoop"):
                app.ExitMainLoop()
        except Exception:
            pass

    def on_apply(self, evt):
        """
        Save once, show restart prompt once, then close the dialog.
        """
        if not self.saved:
            try:
                save_drawing_field_names(self.cfg_path, self.data, self.drawing, self.items)
            except Exception as e:
                wx.MessageBox(f"Failed to write config:\n{e}", "Error", wx.ICON_ERROR)
                return

            dlg = wx.MessageDialog(
                self,
                "Configuration saved.\nKiCad must restart to apply changes.\n\n"
                "Close KiCad now?",
                "Restart KiCad",
                style=wx.YES_NO | wx.ICON_QUESTION | wx.NO_DEFAULT
            )
            res = dlg.ShowModal()
            dlg.Destroy()
            if res == wx.ID_YES:
                self._close_kicad_safely()

            self.saved = True

        self.EndModal(wx.ID_OK)

    def write_back(self):
        """
        Called if the bottom 'Save + Restart' was used.
        Do not show a second prompt if we've already saved via on_apply.
        """
        if self.saved:
            return
        try:
            save_drawing_field_names(self.cfg_path, self.data, self.drawing, self.items)
        except Exception as e:
            wx.MessageBox(f"Failed to write config:\n{e}", "Error", wx.ICON_ERROR)

# --------------------------- Action plugin -------------------------

class ChangeDefaultFieldsOrder_Drawing(pcbnew.ActionPlugin):
    """
    Registers the plugin in KiCad.
    """
    def defaults(self):
        self.name = PLUGIN_NAME
        self.category = PLUGIN_CATEGORY
        self.description = PLUGIN_DESCRIPTION
        # Show button in External Plugins toolbar (if supported by build)
        try:
            self.show_toolbar_button = True #Change to False if you do not want it in the toolbar
        except Exception:
            pass
        # Optional toolbar icon if a PNG is shipped alongside the script
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "V_ChangeDefaultFieldsOrder.png")

    def Run(self):
        # Locate eeschema.json (or let the user browse)
        cfg_path = eeschema_json_path()
        if not cfg_path:
            with wx.FileDialog(None, "Locate eeschema.json (KiCad 9)",
                               wildcard="JSON (*.json)|*.json|All (*.*)|*.*",
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fd:
                if fd.ShowModal() != wx.ID_OK:
                    return
                cfg_path = fd.GetPath()

        # Load config
        try:
            data, drawing, items = load_drawing_field_names(cfg_path)
        except Exception as e:
            wx.MessageBox(f"Failed to read config:\n{e}", "Error", wx.ICON_ERROR)
            return

        # Open dialog
        dlg = OrderDialog(None, cfg_path, data, drawing, items)
        if dlg.ShowModal() == wx.ID_OK:
            try:
                dlg.write_back()
            except Exception as e:
                wx.MessageBox(f"Failed to write config:\n{e}", "Error", wx.ICON_ERROR)
        dlg.Destroy()

# Register with KiCad
ChangeDefaultFieldsOrder_Drawing().register()
