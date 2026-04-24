#!/usr/bin/env python3
"""
Standalone UI test for the Cryostat Sample Holder panel.
Run directly to preview the widget before integrating into app.py.

    python sample_holder_ui.py

The sample holder is a linear vertical stage (mm). Each of the 5 slots is
~8 mm apart. Fill in the real absolute position of slot 1 (SLOT1_MM) and
the read/write/stop PV names before integrating.
"""

import wx

# ── Configuration (fill in real values) ──────────────────────────────────────
SLOT1_MM        = 0.0          # absolute mm position of slot 1 — fill in real value
SLOT_SPACING_MM = 8.0          # centre-to-centre spacing between slots
N_SLOTS         = 5
POSITION_TOLERANCE_MM = 1.0    # mm — within this distance = slot is active

STAGE_READ_PV   = ""           # fill in read PV  (e.g. SMTR...:mm:fbk)
STAGE_WRITE_PV  = ""           # fill in write PV (e.g. SMTR...:mm)
STAGE_STOP_PV   = ""           # fill in stop PV if available

SAMPLE_POSITIONS = [SLOT1_MM + i * SLOT_SPACING_MM for i in range(N_SLOTS)]
# ─────────────────────────────────────────────────────────────────────────────

_BG           = wx.Colour(40, 40, 40)
_SLOT_IDLE    = wx.Colour(65, 65, 65)
_SLOT_ACTIVE  = wx.Colour(210, 110, 20)
_SLOT_FG      = wx.Colour(220, 220, 220)
_STOP_BG      = wx.Colour(190, 50, 50)


class SampleHolderPanel(wx.Panel):
    """
    Panel for a 5-position linear cryostat sample holder stage.

    Clicking a slot button populates the Go-to field (no immediate move).
    The Set button / Enter key fires on_goto(pos_mm).
    Call update_position(mm) from the EPICS callback to refresh readback
    and slot highlight.
    """

    def __init__(self, parent, positions=None,
                 tolerance=POSITION_TOLERANCE_MM,
                 on_goto=None, on_stop=None):
        super().__init__(parent)
        self._positions = positions or SAMPLE_POSITIONS
        self._tolerance = tolerance
        self._on_goto   = on_goto   # callable(pos_mm)
        self._on_stop   = on_stop   # callable()
        self._slot_btns = []
        self._goto_ctrl = None
        self._rbk_label = None
        self.SetBackgroundColour(_BG)
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.AddSpacer(10)

        # Title
        title = wx.StaticText(self, label="Cryostat Sample Holder")
        f = title.GetFont()
        f.MakeBold()
        f.SetPointSize(f.GetPointSize() + 1)
        title.SetFont(f)
        title.SetForegroundColour(wx.Colour(180, 210, 255))
        outer.Add(title, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 8)

        outer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        outer.AddSpacer(8)

        # Current position readback
        rbk_row = wx.BoxSizer(wx.HORIZONTAL)
        cur_lbl = wx.StaticText(self, label="Current position:")
        cur_lbl.SetForegroundColour(wx.Colour(150, 150, 150))
        rbk_row.Add(cur_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._rbk_label = wx.StaticText(self, label="--", size=(55, -1))
        self._rbk_label.SetForegroundColour(wx.WHITE)
        rbk_row.Add(self._rbk_label, 0, wx.ALIGN_CENTER_VERTICAL)
        mm_lbl = wx.StaticText(self, label="mm")
        mm_lbl.SetForegroundColour(wx.Colour(110, 110, 110))
        rbk_row.Add(mm_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        outer.Add(rbk_row, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 10)

        # Slot buttons — vertical column, oval-ish via fixed tall size
        for i, pos in enumerate(self._positions):
            btn = wx.Button(self, label=f"Slot {i + 1}\n{pos:.2f} mm",
                            size=(130, 52))
            btn.SetBackgroundColour(_SLOT_IDLE)
            btn.SetForegroundColour(_SLOT_FG)
            btn.Bind(wx.EVT_BUTTON, lambda evt, p=pos: self._on_slot_click(p))
            self._slot_btns.append(btn)
            outer.Add(btn, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 5)

        outer.AddSpacer(8)
        outer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        outer.AddSpacer(8)

        # Go-to row
        goto_row = wx.BoxSizer(wx.HORIZONTAL)
        go_lbl = wx.StaticText(self, label="Go to:")
        go_lbl.SetForegroundColour(wx.Colour(150, 150, 150))
        goto_row.Add(go_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._goto_ctrl = wx.TextCtrl(self, size=(72, -1),
                                      style=wx.TE_PROCESS_ENTER | wx.BORDER_SIMPLE)
        self._goto_ctrl.SetBackgroundColour(wx.Colour(58, 58, 58))
        self._goto_ctrl.SetForegroundColour(wx.WHITE)
        self._goto_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_goto_enter)
        goto_row.Add(self._goto_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        set_btn = wx.Button(self, label="Set", size=(42, -1))
        set_btn.Bind(wx.EVT_BUTTON, self._on_goto_enter)
        goto_row.Add(set_btn, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(goto_row, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 8)

        # Stop button
        stop_btn = wx.Button(self, label="Stop", size=(100, -1))
        stop_btn.SetBackgroundColour(_STOP_BG)
        stop_btn.SetForegroundColour(wx.WHITE)
        stop_btn.Bind(wx.EVT_BUTTON, self._on_stop_click)
        outer.Add(stop_btn, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 12)

        self.SetSizer(outer)

    # ── Event handlers ─────────────────────────────────────────────────────

    def _on_slot_click(self, pos_mm):
        if self._goto_ctrl:
            self._goto_ctrl.SetValue(f"{pos_mm:.2f}")

    def _on_goto_enter(self, event):
        if self._goto_ctrl is None or self._on_goto is None:
            return
        try:
            self._on_goto(float(self._goto_ctrl.GetValue()))
        except ValueError:
            pass

    def _on_stop_click(self, _event):
        if self._on_stop:
            self._on_stop()

    # ── Public API ─────────────────────────────────────────────────────────

    def update_position(self, pos_mm):
        """Update readback and highlight the slot closest to pos_mm (if within tolerance)."""
        if self._rbk_label:
            self._rbk_label.SetLabel(f"{pos_mm:.3f}")
        for btn, slot_pos in zip(self._slot_btns, self._positions):
            active = abs(pos_mm - slot_pos) <= self._tolerance
            btn.SetBackgroundColour(_SLOT_ACTIVE if active else _SLOT_IDLE)
            btn.Refresh()


# ── Standalone test harness ────────────────────────────────────────────────────

class _TestFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Cryostat Sample Holder — UI Test",
                         size=(220, 510))
        self.SetBackgroundColour(_BG)

        self._panel = SampleHolderPanel(
            self,
            on_goto=lambda v: print(f"[test] Go to {v:.3f} mm"),
            on_stop=lambda:   print("[test] Stop"),
        )

        sz = wx.BoxSizer(wx.VERTICAL)
        sz.Add(self._panel, 1, wx.EXPAND)
        self.SetSizer(sz)
        self.Center()

        # Demo: cycle active slot every 1.5 s
        self._demo_idx = 0
        self._panel.update_position(SAMPLE_POSITIONS[self._demo_idx])
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self._timer)
        self._timer.Start(1500)

    def _tick(self, _evt):
        self._demo_idx = (self._demo_idx + 1) % N_SLOTS
        self._panel.update_position(SAMPLE_POSITIONS[self._demo_idx])


if __name__ == "__main__":
    app = wx.App(False)
    _TestFrame().Show()
    app.MainLoop()
