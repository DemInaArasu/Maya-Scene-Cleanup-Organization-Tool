# -*- coding: utf-8 -*-
"""
P02 Maya Scene Cleanup & Organization Tool
Brenda Arras | Python + Maya cmds + PySide2 (PySide6 fallback for Maya 2025+)

RUN INSIDE MAYA'S PYTHON SCRIPT EDITOR:

    import sys
    sys.path.insert(0, r"C:\Users\bca240000\Documents\arras_BrendaC_P02_UtilityTool\Maya-Scene-Cleanup-Organization-Tool")
    import ArrasBrenda_P02_code as p02
    p02.show_tool()

Safety: Save a backup scene first. Cleanup and hierarchy actions can affect
rigged assets and animation. Review the preview and use Maya Undo if needed.
"""

import collections
import contextlib
import re
import traceback

try:
    import maya.cmds as cmds
    from maya import OpenMayaUI
except ImportError as error:
    raise RuntimeError("Run this script inside Autodesk Maya, not standalone Python.") from error

# >>> PYSIDE GUI IMPORTS -- INSIDE MAYA, NOT A TERMINAL <<<
# Maya 2024 and older ship PySide2; Maya 2025+ ship PySide6 (Qt6).
# Prefer the binding bundled with the running Maya version: mixing Qt5 and
# Qt6 libraries may crash Maya, not merely cause a Python ImportError.
try:
    _maya_version_match = re.search(r"\d{4}", str(cmds.about(version=True)))
    _maya_year = int(_maya_version_match.group()) if _maya_version_match else 2024
except Exception:
    _maya_year = 2024

if _maya_year >= 2025:
    from PySide6 import QtCore, QtWidgets
    import shiboken6 as shiboken
    QT_BINDING = "PySide6"
else:
    from PySide2 import QtCore, QtWidgets
    import shiboken2 as shiboken
    QT_BINDING = "PySide2"

WINDOW_OBJECT_NAME = "ArrasBrendaP02SceneUtility"
_ACTIVE_WINDOW = None


def maya_parent_window():
    """Make the interface a child of Maya's main window."""
    pointer = OpenMayaUI.MQtUtil.mainWindow()
    return shiboken.wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None



# ===== MAYA BACKEND: MAYA.CMDS SCENE ANALYSIS AND OPERATIONS =====

def short_name(path):
    return path.rsplit("|", 1)[-1]


def unique_paths(paths):
    return list(dict.fromkeys(paths))


def object_exists(path):
    try:
        return bool(cmds.objExists(path))
    except RuntimeError:
        return False


def node_type(path):
    try:
        return cmds.nodeType(path)
    except RuntimeError:
        return None


def all_transforms():
    return unique_paths(cmds.ls(type="transform", long=True) or [])


def is_mesh_transform(path):
    if not object_exists(path) or node_type(path) != "transform":
        return False
    shapes = cmds.listRelatives(path, shapes=True, noIntermediate=True, fullPath=True) or []
    return any(node_type(shape) == "mesh" for shape in shapes)


def mesh_transforms(scope="selection"):
    """Return long DAG paths to actual mesh transforms, never joints/cameras."""
    if scope == "scene":
        nodes = all_transforms()
    else:
        nodes = cmds.ls(selection=True, long=True, objectsOnly=True) or []
    result = []
    for node in nodes:
        if node_type(node) == "mesh":
            parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
            node = parents[0] if parents else node
        if is_mesh_transform(node):
            result.append(node)
    return unique_paths(result)


def selected_organizable():
    """Transforms selected directly, but exclude joints and Maya camera nodes."""
    selected = cmds.ls(selection=True, long=True, objectsOnly=True) or []
    result = []
    for node in selected:
        if node_type(node) == "transform":
            result.append(node)
    # Ignore child nodes when their parent is already among those selected.
    paths = set(result)
    return [node for node in unique_paths(result)
            if not any(node.startswith(parent + "|") for parent in paths if parent != node)]


def is_referenced_or_locked(node):
    try:
        if cmds.referenceQuery(node, isNodeReferenced=True):
            return True
    except RuntimeError:
        pass
    try:
        if any(cmds.lockNode(node, query=True, lock=True) or []):
            return True
    except RuntimeError:
        pass
    return False


def is_rigged_or_driven(node):
    """Conservatively detect common deformers and incoming transform drivers."""
    unsafe = {"skinCluster", "blendShape", "cluster", "ffd", "lattice", "wire",
              "wrap", "deltaMush", "sculpt", "nonLinear", "proximityWrap"}
    try:
        history = cmds.listHistory(node, pruneDagObjects=True) or []
        if any(node_type(entry) in unsafe for entry in history):
            return True
        for attr in ("translateX", "translateY", "translateZ",
                     "rotateX", "rotateY", "rotateZ",
                     "scaleX", "scaleY", "scaleZ"):
            if cmds.listConnections(node + "." + attr,
                                    source=True, destination=False):
                return True
    except RuntimeError:
        return True
    return False


def is_valid_maya_name(name):
    # Keep names free from namespaces, pipes, spaces, and Maya auto-rename surprises.
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name))


def unused_name(base, reserved=None):
    reserved = reserved if reserved is not None else {
        short_name(obj) for obj in (cmds.ls(dag=True, long=True) or [])
    }
    if base not in reserved and not cmds.objExists(base):
        reserved.add(base)
        return base
    i = 1
    while True:
        candidate = "{}_{}".format(base, str(i).zfill(2))
        if candidate not in reserved and not cmds.objExists(candidate):
            reserved.add(candidate)
            return candidate
        i += 1


def empty_groups():
    """Only transforms with no children at all; shapes and nested objects count."""
    return [node for node in all_transforms()
            if not (cmds.listRelatives(node, children=True, fullPath=True) or [])]


def has_nondefault_transform(node):
    try:
        translate = cmds.getAttr(node + ".translate")[0]
        rotate = cmds.getAttr(node + ".rotate")[0]
        scale = cmds.getAttr(node + ".scale")[0]
        return (any(abs(v) > 1e-5 for v in translate) or
                any(abs(v) > 1e-5 for v in rotate) or
                any(abs(v - 1.0) > 1e-5 for v in scale))
    except (RuntimeError, TypeError, ValueError):
        return False


def has_construction_history(node):
    """True for a modifier upstream of a shape (not for the shape itself)."""
    try:
        history = cmds.listHistory(node, pruneDagObjects=True) or []
        shapes = set(cmds.listRelatives(node, shapes=True, fullPath=True) or [])
        return any(entry != node and entry not in shapes and
                   node_type(entry) not in ("mesh", "transform") for entry in history)
    except RuntimeError:
        return False


def analyze_scene():
    transforms = all_transforms()
    meshes = mesh_transforms("scene")
    duplicates = collections.Counter(short_name(path) for path in transforms)
    issues = []
    for obj in transforms:
        if duplicates[short_name(obj)] > 1:
            issues.append(("Duplicate name", obj, "Same short name in other hierarchy"))
    for obj in empty_groups():
        issues.append(("Empty transform", obj, "No children or shapes"))
    for obj in meshes:
        if is_rigged_or_driven(obj):
            issues.append(("Rigged / driven", obj, "Protected from cleanup operations"))
            continue
        if has_nondefault_transform(obj):
            issues.append(("Nondefault transform", obj, "Translation, rotation or scale"))
        if has_construction_history(obj):
            issues.append(("Construction history", obj, "May be removable on static geometry"))
    return {
        "transforms": len(transforms),
        "meshes": len(meshes),
        "selected_meshes": len(mesh_transforms("selection")),
        "empty_groups": sum(1 for issue in issues if issue[0] == "Empty transform"),
        "duplicate_names": len([n for n, count in duplicates.items() if count > 1]),
        "issues": issues,
    }


def plan_cleanup(scope, do_history, do_freeze, freeze_translation, do_empty):
    actions, skips = [], []
    for node in mesh_transforms(scope):
        if is_referenced_or_locked(node):
            skips.append((node, "Referenced or locked"))
            continue
        if is_rigged_or_driven(node):
            skips.append((node, "Rigged, deformed, or transform-driven"))
            continue
        if do_history and has_construction_history(node):
            actions.append(("Delete history", node))
        if do_freeze:
            if cmds.listRelatives(node, children=True, type="transform", fullPath=True):
                skips.append((node, "Freeze skipped: transform has child transforms"))
            else:
                actions.append(("Freeze transforms", node))
    if do_empty:
        for node in sorted(empty_groups(), key=lambda path: path.count("|"), reverse=True):
            if is_referenced_or_locked(node):
                skips.append((node, "Empty group is referenced or locked"))
            elif cmds.listConnections(node, source=True, destination=True):
                skips.append((node, "Empty group has graph connections"))
            else:
                actions.append(("Delete empty transform", node))
    return actions, skips


def plan_rename(scope, prefix, base, suffix, first, padding):
    if not is_valid_maya_name(prefix + base + suffix):
        raise ValueError("Prefix + base + suffix must form a valid Maya name (letters, numbers, _).")
    nodes = sorted(mesh_transforms(scope), key=lambda obj: obj.count("|"), reverse=True)
    # Reserve ALL existing short names, preventing collisions across hierarchies.
    reserved = {short_name(node) for node in (cmds.ls(dag=True, long=True) or [])}
    actions, skips = [], []
    counter = first
    for node in nodes:
        if is_referenced_or_locked(node):
            skips.append((node, "Referenced or locked"))
            continue
        base_name = "{}{}_{:0{}d}{}".format(prefix, base, counter, padding, suffix)
        new_name = unused_name(base_name, reserved)
        actions.append((node, new_name))
        counter += 1
    return actions, skips


def classify_node(node):
    shapes = cmds.listRelatives(node, shapes=True, noIntermediate=True, fullPath=True) or []
    types = {node_type(shape) for shape in shapes}
    if "mesh" in types:
        return "GEO_GRP"
    if "light" in types or any(str(kind).endswith("Light") for kind in types):
        return "LIGHT_GRP"
    return "OTHER_GRP"


def plan_group(name, subdivide):
    if not is_valid_maya_name(name):
        raise ValueError("Group name must start with a letter or _, and contain no spaces.")
    targets, skips = [], []
    for obj in selected_organizable():
        if is_referenced_or_locked(obj):
            skips.append((obj, "Referenced or locked"))
        else:
            targets.append((obj, classify_node(obj) if subdivide else name))
    return targets, skips


@contextlib.contextmanager
def maya_undo_chunk():
    cmds.undoInfo(openChunk=True, chunkName="P02 Scene Utility")
    try:
        yield
    finally:
        cmds.undoInfo(closeChunk=True)



# ===== PYSIDE2 GUI: FOUR TABS AND SIGNAL/CALLBACK CONNECTIONS =====

class SceneUtilityWindow(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super(SceneUtilityWindow, self).__init__(parent)
        self.setObjectName(WINDOW_OBJECT_NAME)
        self.setWindowTitle("P02 | Maya Scene Utility")
        self.setMinimumSize(880, 620)
        self.resize(970, 745)
        try:
            deletion_flag = QtCore.Qt.WidgetAttribute.WA_DeleteOnClose
        except AttributeError:
            deletion_flag = QtCore.Qt.WA_DeleteOnClose
        self.setAttribute(deletion_flag, True)
        self._create_ui()
        self.refresh_analysis()
        self.log("Ready. Save a backup scene before running cleanup.")

    def _create_ui(self):
        self.setStyleSheet("""
            QDialog { background: #292e36; color: #eef1f6; }
            QLabel, QGroupBox, QCheckBox { color: #eef1f6; }
            QGroupBox { border: 1px solid #515b68; border-radius: 6px;
                        margin-top: 12px; padding-top: 10px; font-weight: bold; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QPushButton { background: #42566e; color: white; padding: 7px 12px;
                          border: 1px solid #657b91; border-radius: 4px; }
            QPushButton:hover { background: #516d8b; }
            QPushButton:disabled { background: #424850; color: #9298a0; }
            QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTableWidget {
                background: #1e242b; color: white; border: 1px solid #58616b;
                border-radius: 3px; padding: 4px; }
            QTabWidget::pane { border: 1px solid #58616b; }
            QTabBar::tab { padding: 10px 16px; background: #343e49; color: white; }
            QTabBar::tab:selected { background: #546c86; }
        """)
        outer = QtWidgets.QVBoxLayout(self)
        header = QtWidgets.QHBoxLayout()
        heading = QtWidgets.QLabel("MAYA SCENE CLEANUP & ORGANIZATION")
        heading.setStyleSheet("font-size: 18px; font-weight: 700;")
        header.addWidget(heading)
        header.addStretch()
        header.addWidget(QtWidgets.QLabel("GUI: {}".format(QT_BINDING)))
        outer.addLayout(header)
        subtitle = QtWidgets.QLabel(
            "Inspect, clean, rename, and organize polygon meshes. Preview changes before applying.")
        outer.addWidget(subtitle)

        scope_row = QtWidgets.QHBoxLayout()
        scope_row.addWidget(QtWidgets.QLabel("Mesh operation scope:"))
        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.addItem("Selected mesh objects (recommended)", "selection")
        self.scope_combo.addItem("All mesh objects in scene", "scene")
        self.scope_combo.currentIndexChanged.connect(self._clear_previews)
        scope_row.addWidget(self.scope_combo, 1)
        scope_row.addWidget(QtWidgets.QLabel("Scene-wide changes require confirmation."))
        outer.addLayout(scope_row)

        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._analysis_tab()
        self._cleanup_tab()
        self._rename_tab()
        self._organize_tab()

        footer = QtWidgets.QGroupBox("Activity log")
        footer_layout = QtWidgets.QVBoxLayout(footer)
        self.log_edit = QtWidgets.QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(108)
        footer_layout.addWidget(self.log_edit)
        outer.addWidget(footer)
        self.status = QtWidgets.QLabel("Ready")
        outer.addWidget(self.status)

    def _analysis_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        stats = QtWidgets.QHBoxLayout()
        self.stat_labels = {}
        for key, title in (("transforms", "Transforms"), ("meshes", "Meshes"),
                           ("selected_meshes", "Selected meshes"),
                           ("empty_groups", "Empty groups"),
                           ("duplicate_names", "Duplicate names")):
            group = QtWidgets.QGroupBox(title)
            box = QtWidgets.QVBoxLayout(group)
            label = QtWidgets.QLabel("—")
            label.setStyleSheet("font-size: 22px; font-weight: 700;")
            label.setAlignment(QtCore.Qt.AlignCenter)
            box.addWidget(label)
            self.stat_labels[key] = label
            stats.addWidget(group)
        layout.addLayout(stats)
        layout.addWidget(QtWidgets.QLabel("Scene issues (double-click a row to select its object):"))
        self.issue_table = QtWidgets.QTableWidget(0, 3)
        self.issue_table.setHorizontalHeaderLabels(["Issue", "Object (full Maya DAG path)", "Details"])
        self.issue_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.issue_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.issue_table.horizontalHeader().setStretchLastSection(True)
        self.issue_table.setColumnWidth(0, 155)
        self.issue_table.setColumnWidth(1, 420)
        self.issue_table.cellDoubleClicked.connect(self.select_issue_row)
        layout.addWidget(self.issue_table)
        buttons = QtWidgets.QHBoxLayout()
        refresh = QtWidgets.QPushButton("Refresh Scene Analysis")
        refresh.clicked.connect(self.refresh_analysis)
        export = QtWidgets.QPushButton("Export Analysis as TXT")
        export.clicked.connect(self.export_report)
        buttons.addWidget(refresh)
        buttons.addWidget(export)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.tabs.addTab(tab, "1. Analyze")

    def _cleanup_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        options = QtWidgets.QGroupBox("Cleanup options")
        option_layout = QtWidgets.QVBoxLayout(options)
        self.history_check = QtWidgets.QCheckBox("Delete construction history on static mesh objects")
        self.history_check.setChecked(True)
        self.freeze_check = QtWidgets.QCheckBox("Freeze rotation and scale")
        self.freeze_check.setChecked(True)
        self.translate_check = QtWidgets.QCheckBox("Also freeze translation (off by default)")
        self.empty_check = QtWidgets.QCheckBox(
            "Delete disconnected empty transforms in the ENTIRE SCENE (optional)")
        for widget in (self.history_check, self.freeze_check, self.translate_check, self.empty_check):
            option_layout.addWidget(widget)
            widget.toggled.connect(self._clear_previews)
        layout.addWidget(options)
        warning = QtWidgets.QLabel(
            "Safety: referenced, locked, deforming or transform-driven meshes are skipped. "
            "Freezing is skipped for mesh transforms with child transforms.")
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.cleanup_preview = QtWidgets.QPlainTextEdit()
        self.cleanup_preview.setReadOnly(True)
        self.cleanup_preview.setPlaceholderText("Select options, then click Preview Cleanup.")
        layout.addWidget(self.cleanup_preview)
        row = QtWidgets.QHBoxLayout()
        preview = QtWidgets.QPushButton("Preview Cleanup")
        preview.clicked.connect(self.preview_cleanup)
        execute = QtWidgets.QPushButton("Apply Cleanup (Undoable)")
        execute.clicked.connect(self.apply_cleanup)
        row.addWidget(preview)
        row.addWidget(execute)
        layout.addLayout(row)
        self.tabs.addTab(tab, "2. Clean")

    def _rename_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        form = QtWidgets.QFormLayout()
        self.prefix_input = QtWidgets.QLineEdit("geo_")
        self.base_input = QtWidgets.QLineEdit("asset")
        self.suffix_input = QtWidgets.QLineEdit("")
        self.start_spin = QtWidgets.QSpinBox()
        self.start_spin.setRange(0, 999999)
        self.start_spin.setValue(1)
        self.pad_spin = QtWidgets.QSpinBox()
        self.pad_spin.setRange(1, 6)
        self.pad_spin.setValue(3)
        form.addRow("Prefix:", self.prefix_input)
        form.addRow("Base name:", self.base_input)
        form.addRow("Suffix:", self.suffix_input)
        form.addRow("Starting number:", self.start_spin)
        form.addRow("Number padding:", self.pad_spin)
        layout.addLayout(form)
        layout.addWidget(QtWidgets.QLabel("Example: geo_asset_001. Existing names are never overwritten."))
        self.rename_preview = QtWidgets.QPlainTextEdit()
        self.rename_preview.setReadOnly(True)
        self.rename_preview.setPlaceholderText("Click Preview Rename to see original → proposed names.")
        layout.addWidget(self.rename_preview)
        row = QtWidgets.QHBoxLayout()
        preview = QtWidgets.QPushButton("Preview Rename")
        preview.clicked.connect(self.preview_rename)
        execute = QtWidgets.QPushButton("Apply Batch Rename (Undoable)")
        execute.clicked.connect(self.apply_rename)
        row.addWidget(preview)
        row.addWidget(execute)
        layout.addLayout(row)
        self.tabs.addTab(tab, "3. Rename")

    def _organize_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.addWidget(QtWidgets.QLabel(
            "Select mesh transforms, groups, or other transform objects in Maya first."))
        form = QtWidgets.QFormLayout()
        self.group_input = QtWidgets.QLineEdit("ASSETS_GRP")
        form.addRow("New parent group name:", self.group_input)
        layout.addLayout(form)
        self.subgroups_check = QtWidgets.QCheckBox(
            "Create GEO_GRP, LIGHT_GRP and OTHER_GRP subgroups as needed")
        self.subgroups_check.setChecked(True)
        layout.addWidget(self.subgroups_check)
        note = QtWidgets.QLabel(
            "Grouping uses Maya's absolute parenting to preserve world-space placement. "
            "Objects with selected ancestors are grouped only once.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.organize_preview = QtWidgets.QPlainTextEdit()
        self.organize_preview.setReadOnly(True)
        self.organize_preview.setPlaceholderText("Click Preview Hierarchy to inspect what will be grouped.")
        layout.addWidget(self.organize_preview)
        row = QtWidgets.QHBoxLayout()
        preview = QtWidgets.QPushButton("Preview Hierarchy")
        preview.clicked.connect(self.preview_group)
        execute = QtWidgets.QPushButton("Create Group Hierarchy (Undoable)")
        execute.clicked.connect(self.apply_group)
        row.addWidget(preview)
        row.addWidget(execute)
        layout.addLayout(row)
        self.tabs.addTab(tab, "4. Organize")

    def _clear_previews(self, *unused):
        for name in ("cleanup_preview", "rename_preview", "organize_preview"):
            widget = getattr(self, name, None)
            if widget:
                widget.clear()

    def scope(self):
        return self.scope_combo.currentData()

    def log(self, message):
        self.log_edit.appendPlainText(message)
        self.status.setText(message)

    def show_error(self, title, exception):
        self.log("ERROR: {}: {}".format(title, exception))
        cmds.warning("P02 Scene Utility: {}".format(exception))
        QtWidgets.QMessageBox.warning(self, title, str(exception))

    def refresh_analysis(self):
        try:
            report = analyze_scene()
            self._report = report
            for key, label in self.stat_labels.items():
                label.setText(str(report[key]))
            issues = report["issues"][:400]
            self.issue_table.setRowCount(len(issues))
            for row, item in enumerate(issues):
                for col, value in enumerate(item):
                    self.issue_table.setItem(row, col, QtWidgets.QTableWidgetItem(str(value)))
            if len(report["issues"]) > 400:
                self.log("Analysis shows first 400 issues; TXT export contains all.")
            self.status.setText("Analyzed scene: {} issue entries".format(len(report["issues"])))
        except Exception as error:
            self.show_error("Could not analyze scene", error)

    def select_issue_row(self, row, column):
        item = self.issue_table.item(row, 1)
        if item and object_exists(item.text()):
            cmds.select(item.text(), replace=True)
            self.log("Selected {}".format(item.text()))

    def export_report(self):
        try:
            report = analyze_scene()
            filepath, _filter = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save Scene Analysis", "Maya_Scene_Report.txt", "Text files (*.txt)")
            if not filepath:
                return
            with open(filepath, "w", encoding="utf-8") as handle:
                handle.write("MAYA SCENE ANALYSIS\n===================\n")
                for key in ("transforms", "meshes", "selected_meshes", "empty_groups", "duplicate_names"):
                    handle.write("{}: {}\n".format(key, report[key]))
                handle.write("\nISSUES\n------\n")
                for issue, obj, detail in report["issues"]:
                    handle.write("{} | {} | {}\n".format(issue, obj, detail))
            self.log("Saved analysis: {}".format(filepath))
        except Exception as error:
            self.show_error("Could not export analysis", error)

    def cleanup_plan(self):
        return plan_cleanup(self.scope(), self.history_check.isChecked(),
                            self.freeze_check.isChecked(), self.translate_check.isChecked(),
                            self.empty_check.isChecked())

    @staticmethod
    def plan_text(actions, skips, limit=120):
        lines = ["PLANNED ACTIONS: {}".format(len(actions))]
        for action in actions[:limit]:
            lines.append("  {} : {}".format(action[0], action[1]))
        if len(actions) > limit:
            lines.append("  ... {} additional actions".format(len(actions) - limit))
        lines.append("\nSKIPPED: {}".format(len(skips)))
        for item in skips[:limit]:
            lines.append("  {} : {}".format(item[0], item[1]))
        return "\n".join(lines)

    def preview_cleanup(self):
        try:
            actions, skips = self.cleanup_plan()
            self.cleanup_preview.setPlainText(self.plan_text(actions, skips))
            self.log("Previewed {} cleanup actions, {} skipped.".format(len(actions), len(skips)))
        except Exception as error:
            self.show_error("Cleanup preview failed", error)

    def confirm(self, title, action_count, scope_text):
        if action_count == 0:
            QtWidgets.QMessageBox.information(self, "Nothing to change", "No eligible operations were found.")
            return False
        reply = QtWidgets.QMessageBox.question(
            self, title,
            "Apply {} planned action(s)?\n\nScope: {}\n\nSave your scene first. "
            "Maya Undo should reverse this operation in one step.".format(action_count, scope_text),
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        return reply == QtWidgets.QMessageBox.Yes

    def apply_cleanup(self):
        try:
            actions, skips = self.cleanup_plan()
            self.cleanup_preview.setPlainText(self.plan_text(actions, skips))
            if not self.confirm("Confirm cleanup", len(actions),
                                "{}; empty-group scan {}".format(
                                    self.scope(), "scene-wide" if self.empty_check.isChecked() else "off")):
                return
            done, failed = 0, []
            with maya_undo_chunk():
                for operation, node in actions:
                    if not object_exists(node) or is_referenced_or_locked(node):
                        failed.append((node, "Missing or protected"))
                        continue
                    try:
                        if operation == "Delete history":
                            if is_rigged_or_driven(node):
                                failed.append((node, "Driven at execution"))
                                continue
                            cmds.delete(node, constructionHistory=True)
                        elif operation == "Freeze transforms":
                            if is_rigged_or_driven(node):
                                failed.append((node, "Driven at execution"))
                                continue
                            cmds.makeIdentity(node, apply=True,
                                              translate=self.translate_check.isChecked(),
                                              rotate=True, scale=True, normal=0,
                                              preserveNormals=True)
                        elif operation == "Delete empty transform":
                            if (cmds.listRelatives(node, children=True, fullPath=True) or []) or \
                                    cmds.listConnections(node, source=True, destination=True):
                                failed.append((node, "Not empty / has connections"))
                                continue
                            cmds.delete(node)
                        done += 1
                    except Exception as error:
                        failed.append((node, str(error)))
            self.log("Cleanup completed: {} success, {} skipped/errors.".format(done, len(skips) + len(failed)))
            for node, reason in failed[:15]:
                self.log("  SKIPPED {} ({})".format(node, reason))
            self.refresh_analysis()
        except Exception as error:
            self.show_error("Cleanup failed", error)

    def rename_plan(self):
        return plan_rename(self.scope(), self.prefix_input.text().strip(),
                           self.base_input.text().strip(), self.suffix_input.text().strip(),
                           self.start_spin.value(), self.pad_spin.value())

    def preview_rename(self):
        try:
            actions, skips = self.rename_plan()
            lines = ["PROPOSED RENAMES: {}".format(len(actions))]
            for old, new in actions[:150]:
                lines.append("  {}   -->   {}".format(old, new))
            lines.extend(["\nSKIPPED: {}".format(len(skips))] +
                         ["  {}: {}".format(old, why) for old, why in skips[:60]])
            self.rename_preview.setPlainText("\n".join(lines))
            self.log("Previewed {} renames.".format(len(actions)))
        except Exception as error:
            self.show_error("Rename preview failed", error)

    def apply_rename(self):
        try:
            actions, skips = self.rename_plan()
            self.preview_rename()
            if not self.confirm("Confirm batch rename", len(actions), self.scope()):
                return
            done, failed = 0, []
            with maya_undo_chunk():
                # Deepest nodes first: renaming a parent otherwise invalidates child paths.
                for old, new in actions:
                    if not object_exists(old):
                        failed.append((old, "Object path changed or disappeared"))
                        continue
                    try:
                        cmds.rename(old, new)
                        done += 1
                    except Exception as error:
                        failed.append((old, str(error)))
            self.log("Renamed {} meshes; {} skipped/errors.".format(done, len(skips) + len(failed)))
            for node, reason in failed[:15]:
                self.log("  SKIPPED {} ({})".format(node, reason))
            self.refresh_analysis()
        except Exception as error:
            self.show_error("Batch rename failed", error)

    def group_plan(self):
        return plan_group(self.group_input.text().strip(), self.subgroups_check.isChecked())

    def preview_group(self):
        try:
            targets, skips = self.group_plan()
            name = self.group_input.text().strip()
            lines = ["NEW GROUP: {}".format(name),
                     "SUBGROUPS: {}".format("on" if self.subgroups_check.isChecked() else "off"),
                     "OBJECTS TO PARENT: {}".format(len(targets))]
            lines.extend(["  {} -> {}".format(obj, group) for obj, group in targets[:150]])
            lines.append("\nSKIPPED: {}".format(len(skips)))
            lines.extend(["  {}: {}".format(obj, reason) for obj, reason in skips[:60]])
            self.organize_preview.setPlainText("\n".join(lines))
            self.log("Previewed hierarchy for {} objects.".format(len(targets)))
        except Exception as error:
            self.show_error("Hierarchy preview failed", error)

    def apply_group(self):
        try:
            targets, skips = self.group_plan()
            self.preview_group()
            if not self.confirm("Confirm hierarchy", len(targets), "selected transforms"):
                return
            done, failures = 0, []
            with maya_undo_chunk():
                # Build an empty group, then parent selected objects using absolute=True.
                root = cmds.group(empty=True, world=True,
                                  name=unused_name(self.group_input.text().strip()))
                subgroups = {}
                if self.subgroups_check.isChecked():
                    for group_key in sorted({key for _obj, key in targets}):
                        subgroups[group_key] = cmds.group(empty=True, parent=root, name=group_key)
                # Save current paths in objects; nodes have not been moved yet.
                for obj, category in targets:
                    if not object_exists(obj) or is_referenced_or_locked(obj):
                        failures.append((obj, "Missing or protected"))
                        continue
                    parent = subgroups[category] if subgroups else root
                    try:
                        cmds.parent(obj, parent, absolute=True)
                        done += 1
                    except Exception as error:
                        failures.append((obj, str(error)))
                cmds.select(root, replace=True)
            self.log("Created {} hierarchy; {} parented; {} skipped/errors.".format(
                root, done, len(skips) + len(failures)))
            for obj, reason in failures[:15]:
                self.log("  SKIPPED {} ({})".format(obj, reason))
            self.refresh_analysis()
        except Exception as error:
            self.show_error("Grouping failed", error)



# ===== MAYA SCRIPT EDITOR ENTRY POINT =====

def show_tool():
    """Launch one Maya UI instance; safe to rerun from the Script Editor."""
    global _ACTIVE_WINDOW
    if _ACTIVE_WINDOW is not None:
        try:
            _ACTIVE_WINDOW.close()
            _ACTIVE_WINDOW.deleteLater()
        except RuntimeError:
            pass
    _ACTIVE_WINDOW = SceneUtilityWindow(parent=maya_parent_window())
    _ACTIVE_WINDOW.show()
    _ACTIVE_WINDOW.raise_()
    _ACTIVE_WINDOW.activateWindow()
    return _ACTIVE_WINDOW


if __name__ == "__main__":
    show_tool()
