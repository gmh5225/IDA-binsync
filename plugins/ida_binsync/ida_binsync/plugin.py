# ----------------------------------------------------------------------------
# This file is simply the entrypoint from the initial call in ida_binsync,
# which will setup all the hooks for both the UI and IDB changes, and will
# also create the config window.
#
# ----------------------------------------------------------------------------
import logging
import os

from PyQt5 import sip
from PyQt5.QtCore import QObject

import idaapi
import ida_kernwin
import idc
import ida_hexrays
import idautils
from PyQt5.QtWidgets import QWidget, QVBoxLayout

from binsync.common.ui.version import set_ui_version
set_ui_version("PyQt5")
from binsync.common.ui.config_dialog import SyncConfig
from binsync.common.ui.control_panel import ControlPanel
from binsync.common.ui.magic_sync_dialog import display_magic_sync_dialog

from .hooks import MasterHook
from . import IDA_DIR, VERSION
from .controller import IDABinSyncController

l = logging.getLogger(__name__)
controller = IDABinSyncController()

# disable the annoying "Running Python script" wait box that freezes IDA at times
idaapi.set_script_timeout(0)


#
#   UI Hook, placed here for convenience of reading UI implementation
#

class ScreenHook(ida_kernwin.View_Hooks):
    def __init__(self):
        super(ScreenHook, self).__init__()
        self.hooked = False

    def view_activated(self, view):
        form_type = idaapi.get_widget_type(view)
        decomp_view = idaapi.get_widget_vdui(view)
        if not form_type:
            return

        # check if view is decomp or disassembly before doing expensive ea lookup
        if not decomp_view and not form_type == idaapi.BWN_DISASM:
            return

        ea = idc.get_screen_ea()
        if not ea:
            return

        controller.update_active_context(ea)
#
#   Action Handlers
#

class IDAActionHandler(idaapi.action_handler_t):
    def __init__(self, action, plugin, typ):
        super(IDAActionHandler, self).__init__()
        self.action = action
        self.plugin = plugin
        self.typ = typ

    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS



#
# Control Panel
#

class ControlPanelViewWrapper(object):
    NAME = "BinSync: Info Panel"

    def __init__(self, controller):
        # create a dockable view
        self.twidget = idaapi.create_empty_widget(ControlPanelViewWrapper.NAME)
        self.widget = sip.wrapinstance(int(self.twidget), QWidget)
        self.widget.name = ControlPanelViewWrapper.NAME
        self.width_hint = 250

        self._controller = controller
        self._w = None

        self._init_widgets()

    def _init_widgets(self):
        self._w = ControlPanel(self._controller)
        layout = QVBoxLayout()
        layout.addWidget(self._w)
        self.widget.setLayout(layout)

#
#   Base Plugin
#


class BinsyncPlugin(QObject, idaapi.plugin_t):
    """Plugin entry point. Does most of the skinning magic."""

    flags = idaapi.PLUGIN_FIX
    comment = "Syncing dbs between users"

    help = "This is help"
    wanted_name = "Binsync: settings"
    wanted_hotkey = "Ctrl-Shift-B"

    def __init__(self, *args, **kwargs):
        print("[BinSync] {} loaded!".format(VERSION))

        QObject.__init__(self, *args, **kwargs)
        idaapi.plugin_t.__init__(self)
        self.hooks_started = False

    def open_config_dialog(self):
        dialog = SyncConfig(controller)
        dialog.exec_()

        if not controller.check_client():
            return

        if not self.hooks_started:
            self.action_hooks.hook()
            self.view_hook.hook()

        self.open_control_panel()

        if dialog.open_magic_sync:
            #display_magic_sync_dialog(controller)
            l.debug("Magic Sync is disabled on startup for now.")

    def open_control_panel(self):
        """
        Open the control panel view and attach it to IDA View-A or Pseudocode-A.
        """

        wrapper = ControlPanelViewWrapper(controller)
        if not wrapper.twidget:
            raise RuntimeError("Unexpected: twidget does not exist.")

        flags = idaapi.PluginForm.WOPN_TAB | idaapi.PluginForm.WOPN_RESTORE | idaapi.PluginForm.WOPN_PERSIST
        idaapi.display_widget(wrapper.twidget, flags)
        wrapper.widget.visible = True

        # prioritize attaching the binsync panel to a decompilation window
        target = "Pseudocode-A"
        dwidget = idaapi.find_widget(target)
        if not dwidget:
            func_addr = next(idautils.Functions())
            ida_hexrays.open_pseudocode(func_addr, 0)
            dwidget = idaapi.find_widget(target)

            if not dwidget:
                target = "IDA View-A"

        # attach the panel to the found target
        idaapi.set_dock_pos(ControlPanelViewWrapper.NAME, target, idaapi.DP_RIGHT)

    def install_actions(self):
        self.install_control_panel_action()

    def install_control_panel_action(self):
        action_id = "binsync:control_panel"
        action_desc = idaapi.action_desc_t(
            action_id,
            "BinSync: ~C~ontrol Panel",
            IDAActionHandler(self.open_control_panel, None, None),
            None,
            "Open the BinSync control panel",
        )
        result = idaapi.register_action(action_desc)
        if not result:
            raise RuntimeError("Failed to register the control panel action.")

        result = idaapi.attach_action_to_menu(
            "View/Open subviews/Hex dump",
            action_id,
            idaapi.SETMENU_INS,
        )
        if not result:
            raise RuntimeError("Failed to attach the menu item for the control panel action.")

    def _init_hooks(self):
        # Hook UI Startup in IDA
        self.install_actions()

        # init later
        self.view_hook = ScreenHook()
        # Hook IDB & Decomp Actions in IDA
        self.action_hooks = MasterHook(controller)

    def init(self):
        self._init_hooks()

        return idaapi.PLUGIN_KEEP

    def run(self, arg):
        self.open_config_dialog()

    def term(self):
        print("term() called!")

#
#   Utils
#


def plugin_resource(resource_name):
    """
    Return the full path for a given plugin resource file.
    """
    plugin_path = os.path.abspath(os.path.dirname(__file__))

    return os.path.join(
        plugin_path,
        resource_name
    )




