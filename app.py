import argparse
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QLockFile, QRectF, QEasingCurve, QParallelAnimationGroup, QPropertyAnimation, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QIcon, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink, QMediaMetaData
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QCheckBox, QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton, QSizeGrip, QSlider, QToolButton, QVBoxLayout, QWidget

from bridge import Bridge, PORT
from sync import finite, palette, target_ms
from updater import Updater

VERSION = '1.6.0'

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'Eclipse Video'
SETTINGS = DATA / 'settings.json'
UPDATES = DATA / 'updates'


class Canvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.colors = palette({})
        self.setMinimumSize(320, 180)
        self.setAcceptDrops(True)
        self.open_file = None
        self.hover = False

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 6, 6)
        p.setClipPath(clip)
        colors = self.colors['Bg']
        if self.image.isNull():
            gradient = QLinearGradient(0, 0, 0, self.height())
            gradient.setColorAt(0, QColor(colors[0]))
            gradient.setColorAt(1, QColor(colors[-1]))
            p.fillRect(self.rect(), gradient)
            color = QColor(self.colors['Txt'][0])
            color.setAlpha(210 if self.hover else 150)
            p.setPen(color)
            p.setFont(QFont('Roboto Mono', 12))
            p.drawText(self.rect().adjusted(0, -12, 0, -12), Qt.AlignmentFlag.AlignCenter, 'Drop a video here')
            color.setAlpha(110)
            p.setPen(color)
            p.setFont(QFont('Roboto Mono', 10))
            p.drawText(self.rect().adjusted(0, 14, 0, 14), Qt.AlignmentFlag.AlignCenter, 'or use OPEN VIDEO  ·  mp4, mov, mkv, webm')
            p.setPen(Qt.PenStyle.NoPen)
            for row in range(10):
                for col in range(13):
                    strength = max(0, 1-((col/13)**2+(row/10)**2)**.5)
                    color.setAlpha(int(24*strength))
                    p.setBrush(color)
                    r = 1+1.7*strength
                    p.drawEllipse(QRectF(self.width()-20-col*11-(row%2)*5, self.height()-20-row*10, r*2, r*2))
        else:
            p.fillRect(self.rect(), QColor(colors[-1]).darker(260))
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x, y = (self.width()-size.width())//2, (self.height()-size.height())//2
            p.drawImage(x, y, self.image.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        p.setClipping(False)
        stroke = QColor(self.colors['Strk'][0])
        if self.hover:
            stroke = QColor(self.colors['MenuSel'][0])
        p.setPen(QPen(stroke, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 6, 6)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            self.hover = True
            self.update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.hover = False
        self.update()

    def dropEvent(self, event):
        self.hover = False
        self.update()
        for url in event.mimeData().urls():
            if url.isLocalFile() and self.open_file:
                self.open_file(url.toLocalFile())
                event.acceptProposedAction()
                break


class Title(QWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        w = self.window()
        w.showNormal() if w.isMaximized() else w.showMaximized()


class Player(QMainWindow):
    def __init__(self, port=PORT, settings_path=SETTINGS, diagnostics=None):
        super().__init__()
        for name in ('consola.ttf', 'consolab.ttf'):
            font_path = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'/name
            if font_path.is_file():
                QFontDatabase.addApplicationFont(str(font_path))
        QFontDatabase.addApplicationFont(str(ROOT/'assets/RobotoMono-Regular.ttf'))
        self.settings_path = Path(settings_path)
        try:
            self.settings = json.loads(self.settings_path.read_text())
        except (OSError, ValueError):
            self.settings = {}
        self.setWindowTitle('Eclipse Video')
        self.setWindowIcon(QIcon(str(ROOT/'assets/eclipse-app.ico')))
        self.always_on_top = self.settings.get('alwaysOnTop') is True
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        if self.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.resize(820, 503)
        self.setMinimumSize(760, 323)
        self.owner = None
        self.last_message = 0.0
        self.seq = -1
        self.remote = None
        self.control = None
        self.control_seq = 0
        self.control_at = 0.0
        self.restore_volume = int(finite(self.settings.get('restoreVolume', self.settings.get('volume')), 50, 0, 100))
        self.deadline = None
        self.arm_request = None
        self.arm_until = 0
        self.frame_ms = 0.0
        self.frame_received = 0.0
        self.frames = deque(maxlen=12)
        self.displayed_ms = 0.0
        self.last_correction = 0.0
        self.last_target = 0.0
        self.last_drift = 0.0
        self.media_path = None
        self.prepared = False
        self.media_error = ''
        self.diagnostics = Path(diagnostics) if diagnostics else None
        self.metrics = []
        self.scheduled_at = None
        self.played_at = None
        self.first_frame_at = None
        self.colors = palette(self.settings.get('theme', {}))
        self.audio = QAudioOutput(self)
        self.media = QMediaPlayer(self)
        self.media.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.media.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self.frame)
        self.media.errorOccurred.connect(self.failed)
        self.media.durationChanged.connect(self.duration_changed)
        self.media.positionChanged.connect(self.position_changed)
        self.media.playbackStateChanged.connect(self.playback_changed)
        self.media.mediaStatusChanged.connect(self.media_status)
        self.installing = False
        self.checked_by_hand = False
        self.revealed = False
        self.leaving = False
        self.motion = {}
        self.updater = Updater(VERSION, UPDATES, self)
        self.updater.found.connect(self.update_found)
        self.updater.current.connect(self.update_current)
        self.updater.progress.connect(self.update_progress)
        self.updater.ready.connect(self.update_ready)
        self.updater.failed.connect(self.update_failed)
        self.build()
        self.apply_theme(self.colors)
        self.set_connected(False)
        self.audio.setVolume(self.volume.value()/100)
        self.audio.setMuted(self.mute.isChecked())
        self.bridge = Bridge(self, port, VERSION)
        self.bridge.received.connect(self.command)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        QShortcut(QKeySequence('Space'), self, activated=self.toggle_play)
        QShortcut(QKeySequence('M'), self, activated=lambda: self.mute.toggle())
        QShortcut(QKeySequence('Left'), self, activated=lambda: self.step(-1))
        QShortcut(QKeySequence('Right'), self, activated=lambda: self.step(1))
        last = self.settings.get('file')
        if last and Path(last).is_file():
            QTimer.singleShot(0, lambda: self.open_video(last))
        if self.auto_update.isChecked():
            QTimer.singleShot(2500, self.updater.check)

    def button(self, text, callback, parent=None, kind=None):
        b = QPushButton(text, parent)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(28)
        b.clicked.connect(callback)
        if kind:
            b.setObjectName(kind)
        return b

    @staticmethod
    def caption(text):
        label = QLabel(text)
        label.setObjectName('Caption')
        return label

    def build(self):
        root = QFrame()
        root.setObjectName('Root')
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        title = Title()
        row = QHBoxLayout(title)
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(6)
        self.dot = QLabel()
        self.dot.setObjectName('Dot')
        self.dot.setFixedSize(8, 8)
        self.dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.dot)
        self.status = QLabel('ECLIPSE VIDEO')
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status.setObjectName('Title')
        row.addWidget(self.status)
        self.state = QLabel('Not connected')
        self.state.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.state.setObjectName('Muted')
        self.state.setMinimumWidth(QFontMetrics(self.state.font()).horizontalAdvance('Not connected')+4)
        row.addWidget(self.state)
        row.addStretch()
        self.filename = QLabel('')
        self.filename.setObjectName('Muted')
        self.filename.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.filename)
        row.addSpacing(6)
        self.open_button = self.button('OPEN VIDEO', self.pick_file)
        row.addWidget(self.open_button)
        self.settings_button = self.button('SETTINGS', self.toggle_settings)
        self.settings_button.setCheckable(True)
        row.addWidget(self.settings_button)
        self.pin = self.button('PIN', lambda: None)
        self.pin.setCheckable(True)
        self.pin.setChecked(self.always_on_top)
        self.pin.setFixedWidth(46)
        self.pin.setToolTip('Keep Eclipse Video on top of other windows')
        self.pin.toggled.connect(self.set_pinned)
        row.addWidget(self.pin)
        row.addSpacing(6)
        for text, action, name in [('−', self.showMinimized, 'Window'), ('□', lambda: self.showNormal() if self.isMaximized() else self.showMaximized(), 'Window'), ('×', self.close, 'Close')]:
            b = self.button(text, action, kind=name)
            b.setFixedWidth(28)
            row.addWidget(b)
        layout.addWidget(title)
        self.banner = QFrame()
        self.banner.setObjectName('Banner')
        banner = QHBoxLayout(self.banner)
        banner.setContentsMargins(12, 6, 8, 6)
        banner.setSpacing(8)
        self.banner_text = QLabel('')
        banner.addWidget(self.banner_text)
        banner.addStretch()
        self.banner_action = self.button('UPDATE', self.update_action, kind='Primary')
        banner.addWidget(self.banner_action)
        banner.addWidget(self.button('LATER', lambda: self.slide(self.banner, False), kind='Ghost'))
        layout.addWidget(self.banner)
        self.banner.hide()
        self.options = QFrame()
        self.options.setObjectName('Panel')
        panel = QVBoxLayout(self.options)
        panel.setContentsMargins(12, 8, 12, 8)
        panel.setSpacing(6)
        options = QHBoxLayout()
        options.setSpacing(8)
        options.addWidget(self.caption('PLAYBACK'))
        options.addSpacing(6)
        options.addWidget(QLabel('Delay (ms)'))
        self.offset = QDoubleSpinBox()
        self.offset.setRange(-600000, 600000)
        self.offset.setDecimals(0)
        self.offset.setValue(finite(self.settings.get('delay'), 0, -600000, 600000))
        self.offset.valueChanged.connect(self.options_changed)
        self.offset.setToolTip('Positive delays the video. Negative advances it.')
        options.addWidget(self.spin_field(self.offset, 62, 'Delay'))
        options.addSpacing(10)
        options.addWidget(QLabel('Speed'))
        self.speed = QDoubleSpinBox()
        self.speed.setRange(.25, 4)
        self.speed.setSingleStep(.05)
        self.speed.setSuffix('×')
        self.speed.setValue(finite(self.settings.get('speed'), 1, .25, 4))
        self.speed.valueChanged.connect(self.options_changed)
        options.addWidget(self.spin_field(self.speed, 52, 'Speed'))
        options.addStretch()
        options.addWidget(self.button('RESET', lambda: (self.offset.setValue(0), self.speed.setValue(1)), kind='Ghost'))
        panel.addLayout(options)
        divider = QFrame()
        divider.setObjectName('Divider')
        divider.setFixedHeight(1)
        panel.addWidget(divider)
        updates = QHBoxLayout()
        updates.setSpacing(8)
        updates.addWidget(self.caption('UPDATES'))
        updates.addSpacing(6)
        version = QLabel(f'Version {VERSION}')
        version.setObjectName('Muted')
        updates.addWidget(version)
        updates.addSpacing(10)
        self.auto_update = QCheckBox('Auto-update')
        self.auto_update.setChecked(self.settings.get('autoUpdate', True) is not False)
        self.auto_update.setToolTip('Download new versions in the background and install them when you close the app')
        self.auto_update.toggled.connect(self.auto_update_changed)
        updates.addWidget(self.auto_update)
        updates.addStretch()
        self.update_status = QLabel('')
        self.update_status.setObjectName('Muted')
        updates.addWidget(self.update_status)
        self.update_button = self.button('CHECK FOR UPDATES', self.update_action, kind='Ghost')
        updates.addWidget(self.update_button)
        panel.addLayout(updates)
        layout.addWidget(self.options)
        self.options.hide()
        self.canvas = Canvas()
        self.canvas.open_file = self.open_video
        layout.addWidget(self.canvas, 1)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.setRange(0, 0)
        self.seek.setCursor(Qt.CursorShape.PointingHandCursor)
        self.seek.sliderPressed.connect(self.begin_seek)
        self.seek.sliderMoved.connect(self.update_timestamp)
        self.seek.valueChanged.connect(self.update_timestamp)
        self.seek.sliderReleased.connect(self.finish_seek)
        layout.addWidget(self.seek)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.previous = self.button('−1', lambda: self.step(-1), kind='Ghost')
        self.previous.setFixedWidth(38)
        self.previous.setToolTip('Previous frame  (Left)')
        controls.addWidget(self.previous)
        self.play = self.button('PLAY', self.toggle_play, kind='Primary')
        self.play.setFixedWidth(78)
        self.play.setToolTip('Play / pause  (Space)')
        controls.addWidget(self.play)
        self.next = self.button('+1', lambda: self.step(1), kind='Ghost')
        self.next.setFixedWidth(38)
        self.next.setToolTip('Next frame  (Right)')
        controls.addWidget(self.next)
        controls.addSpacing(6)
        self.timestamp = QLabel('00:00.000 / 00:00.000 · Frame 0')
        self.timestamp.setObjectName('Muted')
        controls.addWidget(self.timestamp)
        controls.addStretch()
        self.sync = QCheckBox('Sync to Eclipse')
        self.sync.setChecked(True)
        self.sync.setToolTip('Follow the Eclipse timeline while the plugin is connected')
        self.sync.toggled.connect(self.sync_changed)
        self.mute = self.button('MUTE', lambda: None, kind='Ghost')
        self.mute.setCheckable(True)
        self.mute.setChecked(self.settings.get('muted', False))
        self.mute.setText('UNMUTE' if self.mute.isChecked() else 'MUTE')
        self.mute.setFixedWidth(68)
        self.mute.setToolTip('Mute  (M)')
        self.mute.toggled.connect(self.set_muted)
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(0 if self.mute.isChecked() else int(finite(self.settings.get('volume'), 50, 0, 100)))
        self.volume.setFixedWidth(90)
        self.volume.setCursor(Qt.CursorShape.PointingHandCursor)
        self.volume.valueChanged.connect(self.set_volume)
        self.volume.setToolTip('Volume')
        controls.addWidget(self.sync)
        controls.addSpacing(6)
        controls.addWidget(self.mute)
        controls.addWidget(self.volume)
        grip = QSizeGrip(self)
        grip.setFixedSize(12, 12)
        controls.addWidget(grip)
        layout.addLayout(controls)

    def spin_field(self, spin, width, name):
        field = QFrame()
        field.setObjectName('SpinField')
        row = QHBoxLayout(field)
        row.setContentsMargins(2, 2, 3, 2)
        row.setSpacing(1)
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setFixedWidth(width)
        row.addWidget(spin)
        for direction, arrow, callback in [('up', Qt.ArrowType.UpArrow, spin.stepUp), ('down', Qt.ArrowType.DownArrow, spin.stepDown)]:
            button = QToolButton()
            button.setObjectName(name + direction.title())
            button.setAccessibleName(f'{name} {direction}')
            button.setArrowType(arrow)
            button.setFixedSize(19, 24)
            button.setAutoRepeat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(callback)
            row.addWidget(button)
        return field

    def apply_theme(self, raw):
        self.colors = palette(raw)
        c = {key: values[0] for key, values in self.colors.items()}
        bg = self.colors['Bg']
        text = QColor(c['Txt'])
        muted = text.darker(140).name()
        faint = text.darker(190).name()
        hover = QColor(c['TopBtn']).lighter(122).name()
        ghost = QColor(c['Pnl']).lighter(112).name()
        self.setStyleSheet(f'''
            QWidget {{ font-family: 'Roboto Mono'; font-size: 11px; color: {c['Txt']}; }}
            QFrame#Root {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 {bg[0]},stop:1 {bg[-1]}); border: 1px solid {c['Strk']}; border-radius: 8px; }}
            QLabel#Title {{ font-size: 12px; font-weight: bold; letter-spacing: 1px; }}
            QLabel#Muted {{ color: {muted}; }}
            QLabel#Caption {{ color: {faint}; font-size: 10px; font-weight: bold; letter-spacing: 1px; }}
            QLabel#Dot {{ background: {faint}; border-radius: 4px; }}
            QLabel#Dot[connected="true"] {{ background: #5ad48a; }}
            QFrame#Panel {{ background: {c['Pnl']}; border: 1px solid {c['Strk']}; border-radius: 6px; }}
            QFrame#Banner {{ background: {c['TopBtn']}; border: 1px solid {c['Strk']}; border-left: 3px solid {QColor(c['MenuSel']).lighter(130).name()}; border-radius: 6px; }}
            QFrame#Banner QLabel {{ font-weight: bold; }}
            QFrame#Divider {{ background: {c['Div']}; border: none; }}
            QPushButton {{ background: {c['TopBtn']}; border: 1px solid transparent; border-radius: 5px; padding: 3px 10px; }}
            QPushButton:hover {{ border: 1px solid {c['Strk']}; background: {hover}; }}
            QPushButton:checked, QPushButton:pressed {{ background: {c['MenuSel']}; border-color: {c['Strk']}; }}
            QPushButton:disabled {{ color: {faint}; background: {c['Pnl']}; }}
            QPushButton#Primary {{ background: {c['MenuSel']}; font-weight: bold; }}
            QPushButton#Primary:hover {{ background: {QColor(c['MenuSel']).lighter(118).name()}; border-color: {QColor(c['MenuSel']).lighter(140).name()}; }}
            QPushButton#Primary:disabled {{ background: {c['Pnl']}; color: {faint}; }}
            QPushButton#Ghost {{ background: transparent; border: 1px solid {c['Strk']}; }}
            QPushButton#Ghost:hover {{ background: {ghost}; }}
            QPushButton#Ghost:checked {{ background: {c['MenuSel']}; }}
            QPushButton#Window, QPushButton#Close {{ background: transparent; border: none; color: {muted}; font-size: 13px; padding: 0; }}
            QPushButton#Window:hover {{ background: {ghost}; color: {c['Txt']}; }}
            QPushButton#Close:hover {{ background: #b8404a; color: white; }}
            QFrame#SpinField {{ background: {bg[-1]}; border: 1px solid {c['Strk']}; border-radius: 5px; }}
            QDoubleSpinBox {{ background: transparent; border: none; padding: 3px; }}
            QToolButton {{ background: {c['TopBtn']}; border: none; border-radius: 3px; padding: 0; }}
            QToolButton:hover {{ background: {hover}; }}
            QToolButton:pressed {{ background: {c['MenuSel']}; }}
            QSlider::groove:horizontal {{ height: 5px; background: {c['Div']}; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {c['MenuSel']}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ width: 12px; margin: -4px 0; background: {c['Txt']}; border-radius: 6px; }}
            QSlider::handle:horizontal:hover {{ background: white; }}
            QSlider::sub-page:horizontal:disabled {{ background: {c['Div']}; }}
            QSlider::handle:horizontal:disabled {{ background: {faint}; }}
            QCheckBox {{ spacing: 6px; }}
            QCheckBox::indicator {{ width: 13px; height: 13px; border: 1px solid {c['Strk']}; border-radius: 3px; background: {bg[-1]}; }}
            QCheckBox::indicator:hover {{ border-color: {QColor(c['Strk']).lighter(140).name()}; }}
            QCheckBox::indicator:checked {{ background: {c['MenuSel']}; border-color: {QColor(c['MenuSel']).lighter(140).name()}; }}
            QToolTip {{ background: {c['Pnl']}; color: {c['Txt']}; border: 1px solid {c['Strk']}; padding: 3px; }}
        ''')
        self.canvas.colors = self.colors
        self.canvas.update()

    def set_connected(self, connected):
        if self.dot.property('connected') == connected:
            return
        self.dot.setProperty('connected', connected)
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.state.setText('Connected' if connected else 'Not connected')

    def show_filename(self):
        name = self.media_path.name if self.media_path else ''
        metrics = QFontMetrics(self.filename.font())
        budget = max(90, self.width()-620)
        self.filename.setMaximumWidth(budget)
        self.filename.setText(metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, budget))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'filename'):
            self.show_filename()

    def auto_update_changed(self, enabled):
        self.save()
        if enabled and not self.updater.installer:
            self.updater.check()

    def update_action(self):
        updater = self.updater
        if updater.installer:
            if updater.install(relaunch=True):
                self.installing = True
                self.close()
            return
        if updater.release:
            self.update_status.setText('Downloading…')
            self.banner_text.setText(f'Downloading Eclipse Video {updater.release["version"]}…')
            self.banner_action.setEnabled(False)
            updater.download()
            return
        self.update_status.setText('Checking…')
        self.update_button.setEnabled(False)
        self.checked_by_hand = True
        updater.check()

    def update_found(self, release):
        self.update_button.setEnabled(True)
        self.update_status.setText(f'{release["version"]} available')
        self.update_button.setText('DOWNLOAD UPDATE')
        if self.auto_update.isChecked():
            self.update_action()
            return
        self.banner_text.setText(f'Eclipse Video {release["version"]} is available')
        self.banner_action.setText('DOWNLOAD')
        self.banner_action.setEnabled(True)
        self.slide(self.banner, True)

    def update_current(self):
        self.update_button.setEnabled(True)
        self.update_status.setText(f'Up to date · checked {time.strftime("%H:%M")}')
        self.checked_by_hand = False

    def update_progress(self, percent):
        self.update_status.setText(f'Downloading {percent}%')

    def update_ready(self, path):
        version = self.updater.release['version']
        self.update_status.setText(f'{version} ready · installs on restart')
        self.update_button.setText('RESTART TO UPDATE')
        self.update_button.setEnabled(True)
        self.banner_text.setText(f'Eclipse Video {version} is ready')
        self.banner_action.setText('RESTART NOW')
        self.banner_action.setEnabled(True)
        self.slide(self.banner, True)

    def update_failed(self, message):
        self.update_button.setEnabled(True)
        self.banner_action.setEnabled(True)
        if self.updater.release:
            self.update_status.setText('Download failed · try again')
            self.update_button.setText('DOWNLOAD UPDATE')
        elif self.checked_by_hand:
            self.update_status.setText('Could not check · offline?')
        self.checked_by_hand = False

    def pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open video', str(self.media_path.parent if self.media_path else Path.home()/'Videos'), 'Videos (*.mp4 *.mov *.mkv *.webm *.avi *.wmv *.m4v);;All files (*)')
        if path:
            self.open_video(path)

    def open_video(self, path):
        path = Path(path)
        if not path.is_file():
            return
        self.deadline = None
        self.media.pause()
        self.media_path = path
        self.prepared = False
        self.media_error = ''
        self.frames.clear()
        self.frame_ms = self.displayed_ms = self.last_target = self.last_drift = 0.0
        self.frame_received = 0.0
        self.last_correction = time.perf_counter()
        self.played_at = self.first_frame_at = self.scheduled_at = None
        if self.arm_request:
            self.arm_request.finish({'error': 'video changed'})
            self.arm_request = None
        self.canvas.image = QImage()
        self.canvas.update()
        # A repeated source does not emit LoadedMedia again on every backend.
        self.media.setSource(QUrl())
        self.media.setSource(QUrl.fromLocalFile(str(path)))
        self.show_filename()
        self.filename.setToolTip(path.name)
        self.save()

    def failed(self, error, text):
        self.media_error = text or 'Cannot play this video'

    def media_status(self, status):
        ready = {QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia}
        if status in ready and not self.prepared:
            self.prepared = True
            self.media.pause()
            self.last_correction = time.perf_counter()
            target = target_ms(self.remote, self.last_correction, self.speed.value(), -self.offset.value()) if self.following() else 0
            self.media.setPosition(round(min(self.media.duration(), max(0, target))))
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self.prepared = False

    def frame(self, frame):
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        self.frame_ms = frame.startTime()/1000 if frame.startTime() >= 0 else self.media.position()
        self.frame_received = time.perf_counter()
        if self.following() and self.remote.get('playing') and self.media.isPlaying():
            self.frames.append((self.frame_ms, image))
            return
        self.present(self.frame_ms, image)

    def present(self, position, image):
        self.displayed_ms = position
        self.canvas.image = image
        self.canvas.update()
        if self.played_at and not self.first_frame_at:
            self.first_frame_at = time.perf_counter()
            self.metrics.append({'scheduled': self.scheduled_at, 'played': self.played_at, 'firstFrame': self.first_frame_at, 'frameMs': self.frame_ms})

    def duration_changed(self, duration):
        self.seek.setRange(0, duration)
        self.update_timestamp(self.seek.value())

    @staticmethod
    def clock_text(value):
        value = max(0, int(value))
        return f'{value//60000:02d}:{value//1000%60:02d}.{value%1000:03d}'

    def position_changed(self, value):
        if not self.seek.isSliderDown():
            self.seek.setValue(value)
            self.update_timestamp(value)

    def update_timestamp(self, value):
        if not hasattr(self, 'timestamp'):
            return
        fps = float(self.media.metaData().value(QMediaMetaData.Key.VideoFrameRate) or 30)
        frame = max(0, int(value*fps/1000 + .001))
        self.timestamp.setText(f'{self.clock_text(value)} / {self.clock_text(self.media.duration())} · Frame {frame}')

    def begin_seek(self):
        self.media.pause()

    def finish_seek(self):
        value = self.seek.value()
        if self.following():
            self.queue_control('seek', value)
        else:
            self.media.setPosition(value)

    def queue_control(self, action, position=None):
        if not self.following() or not self.remote.get('controllable', True):
            return
        self.control_seq += 1
        self.control_at = time.perf_counter()
        self.control = {'id': self.control_seq, 'action': action}
        if position is not None:
            self.control['position'] = max(0, (position+self.offset.value())/(1000*self.speed.value()))
        if action != 'play':
            self.deadline = None
            self.media.pause()
        self.refresh_play_label()

    def playback_changed(self, state):
        self.refresh_play_label()

    def wants_playback(self):
        if self.following():
            if self.control:
                return self.control['action'] == 'play'
            return bool(not self.seek.isSliderDown() and (self.remote.get('playing') or self.remote.get('preparing')))
        return self.media.isPlaying()

    def refresh_play_label(self):
        if hasattr(self, 'play'):
            text = 'PAUSE' if self.wants_playback() else 'PLAY'
            if self.play.text() != text:
                self.play.setText(text)

    def following(self):
        return bool(self.sync.isChecked() and self.owner and self.remote and time.perf_counter()-self.last_message < 3)

    def toggle_play(self):
        if not self.prepared:
            return
        if self.following():
            self.queue_control('pause' if self.wants_playback() else 'play')
            return
        if self.media.isPlaying():
            self.media.pause()
        else:
            self.media.setPlaybackRate(self.speed.value())
            self.media.play()

    def step(self, amount):
        fps = self.media.metaData().value(QMediaMetaData.Key.VideoFrameRate) or 30
        self.media.pause()
        value = max(0, min(self.media.duration(), round(self.seek.value()+amount*1000/float(fps))))
        self.seek.setValue(value)
        if self.following():
            self.queue_control('seek', value)
        else:
            self.media.setPosition(value)

    def set_muted(self, muted):
        if muted:
            self.restore_volume = self.volume.value()
        value = 0 if muted else self.restore_volume
        self.volume.blockSignals(True)
        self.volume.setValue(value)
        self.volume.blockSignals(False)
        self.audio.setVolume(value/100)
        self.audio.setMuted(muted)
        self.mute.setText('UNMUTE' if muted else 'MUTE')
        self.volume.setToolTip(f'Volume {value}%')
        self.save()

    def set_volume(self, value):
        if value > 0:
            self.restore_volume = value
            if self.mute.isChecked():
                self.mute.setChecked(False)
        self.audio.setVolume(value/100)
        self.volume.setToolTip(f'Volume {value}%')
        self.save()

    def options_changed(self):
        if hasattr(self, 'speed'):
            self.media.setPlaybackRate(self.speed.value())
            self.last_correction = 0
            self.save()

    def sync_changed(self):
        self.control = None
        self.deadline = None
        self.media.pause()
        self.save()

    def set_pinned(self, enabled):
        self.always_on_top = bool(enabled)
        visible, state, geometry = self.isVisible(), self.windowState(), self.geometry()
        without_activation = self.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.always_on_top)
        self.setWindowState(state)
        if not state & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen):
            self.setGeometry(geometry)
        if visible:
            self.show()
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, without_activation)
        self.save()

    def toggle_settings(self):
        show = not self.options.isVisible() or self.motion.get(self.options) == 'closing'
        self.slide(self.options, show)
        self.settings_button.setChecked(show)

    def slide(self, widget, show):
        if show and widget.isVisible() and self.motion.get(widget) != 'closing':
            return
        if not show and not widget.isVisible():
            return
        old = getattr(widget, 'motion', None)
        if old:
            old.stop()
        start = widget.height() if widget.isVisible() else 0
        if show:
            widget.setFixedHeight(max(0, start))
            widget.show()
            end = widget.sizeHint().height()
        else:
            end = 0
        self.motion[widget] = 'opening' if show else 'closing'
        anim = QVariantAnimation(self)
        anim.setDuration(280 if show else 170)
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setEasingCurve(QEasingCurve.Type.OutBack if show else QEasingCurve.Type.InCubic)
        anim.valueChanged.connect(lambda value: widget.setFixedHeight(int(value)))

        def settle():
            self.motion.pop(widget, None)
            widget.motion = None
            if show:
                widget.setMinimumHeight(0)
                widget.setMaximumHeight(16777215)
            else:
                widget.hide()
                widget.setMinimumHeight(0)
                widget.setMaximumHeight(16777215)

        anim.finished.connect(settle)
        widget.motion = anim
        anim.start()

    def showEvent(self, event):
        super().showEvent(event)
        if self.revealed:
            return
        self.revealed = True
        final = self.geometry()
        dx, dy = int(final.width()*.03), int(final.height()*.04)
        self.setWindowOpacity(0)
        self.setGeometry(final.adjusted(dx, dy, -dx, -dy))
        group = QParallelAnimationGroup(self)
        fade = QPropertyAnimation(self, b'windowOpacity', group)
        fade.setDuration(220)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        grow = QPropertyAnimation(self, b'geometry', group)
        grow.setDuration(300)
        grow.setStartValue(final.adjusted(dx, dy, -dx, -dy))
        grow.setEndValue(final)
        grow.setEasingCurve(QEasingCurve.Type.OutBack)
        group.addAnimation(fade)
        group.addAnimation(grow)
        group.start()
        self.intro = group

    def depart(self):
        final = self.geometry()
        dx, dy = int(final.width()*.03), int(final.height()*.04)
        group = QParallelAnimationGroup(self)
        fade = QPropertyAnimation(self, b'windowOpacity', group)
        fade.setDuration(170)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InCubic)
        group.addAnimation(fade)
        if not self.isMaximized():
            shrink = QPropertyAnimation(self, b'geometry', group)
            shrink.setDuration(170)
            shrink.setStartValue(final)
            shrink.setEndValue(final.adjusted(dx, dy, -dx, -dy))
            shrink.setEasingCurve(QEasingCurve.Type.InCubic)
            group.addAnimation(shrink)
        group.finished.connect(self.close)
        group.start()
        self.outro = group

    def save(self):
        if not hasattr(self, 'volume'):
            return
        data = {'alwaysOnTop': self.always_on_top, 'volume': self.volume.value(), 'restoreVolume': self.restore_volume, 'muted': self.mute.isChecked(), 'delay': self.offset.value(), 'speed': self.speed.value(), 'sync': self.sync.isChecked(), 'autoUpdate': self.auto_update.isChecked(), 'theme': self.colors}
        if self.media_path:
            data['file'] = str(self.media_path)
        temporary = self.settings_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        temporary.replace(self.settings_path)

    def snapshot(self):
        return {'alwaysOnTop': self.always_on_top, 'connected': bool(self.owner), 'ready': self.prepared, 'file': self.media_path.name if self.media_path else '', 'positionMs': self.media.position(), 'frameMs': self.frame_ms, 'displayedMs': self.displayed_ms, 'playing': self.media.isPlaying(), 'driftMs': round(self.last_drift, 2), 'scheduledAt': self.scheduled_at, 'playedAt': self.played_at, 'firstFrameAt': self.first_frame_at, 'sync': self.sync.isChecked(), 'delayMs': self.offset.value(), 'speed': self.speed.value(), 'volume': self.volume.value(), 'muted': self.mute.isChecked(), 'control': self.control}

    def command(self, request):
        data = request.data
        now = time.perf_counter()
        action = data.get('action')
        if action not in {'state', 'arm', 'start', 'stop', 'disconnect', 'status'}:
            return request.finish({'error': 'unknown command'})
        session = data.get('session')
        if not isinstance(session, str) or not 1 <= len(session) <= 100:
            return request.finish({'error': 'invalid session'})
        if self.owner and self.owner != session and now-self.last_message < 3:
            return request.finish({'error': 'another Eclipse session is connected'})
        if self.owner != session:
            self.seq = -1
            self.remote = None
            self.control = None
        self.owner = session
        self.last_message = now
        seq = int(finite(data.get('seq'), 0, 0, 2**53))
        if seq <= self.seq:
            return request.finish(self.snapshot())
        self.seq = seq
        if self.control and finite(data.get('ack'), -1, -1, 2**53) >= self.control['id']:
            self.control = None
        if 'theme' in data:
            self.apply_theme(data['theme'])
        if action == 'disconnect':
            self.media.pause()
            self.deadline = None
            self.remote = None
            self.owner = None
            self.control = None
            self.refresh_play_label()
            return request.finish(self.snapshot())
        if action in {'state', 'arm', 'start', 'stop'}:
            state = {'position': finite(data.get('position'), 0, 0, 86400), 'at': finite(data.get('at'), now, now-60, now+5),
                     'playing': bool(data.get('playing', False)), 'preparing': bool(data.get('preparing', False)), 'fps': finite(data.get('fps'), 60, 1, 1000),
                     'loop': bool(data.get('loop')), 'endTime': finite(data.get('endTime'), 0, 0, 86400),
                     'controllable': bool(data.get('controllable', True))}
            if action in {'arm', 'stop'}:
                state['playing'] = False
                state['preparing'] = action == 'arm'
            self.remote = state
            self.refresh_play_label()
            if action == 'state':
                self.deadline = state['at'] if state['playing'] and state['at'] > now else None
        if action == 'stop':
            self.deadline = None
            self.frames.clear()
            self.media.pause()
        if action == 'arm' and self.sync.isChecked() and self.prepared:
            self.deadline = None
            self.frames.clear()
            self.media.pause()
            target = min(self.media.duration(), max(0, target_ms(self.remote, now, self.speed.value(), -self.offset.value())))
            if abs(self.media.position()-target) > .5:
                self.media.setPosition(round(target))
            if self.arm_request:
                self.arm_request.finish({'error': 'superseded'})
            self.arm_request = request
            self.arm_target = min(target, self.media.duration())
            self.arm_until = now+2
            self.finish_arm(now)
            return
        if action == 'start' and self.sync.isChecked() and self.prepared:
            if self.arm_request:
                self.arm_request.finish({'error': 'superseded'})
                self.arm_request = None
            self.deadline = finite(data.get('deadline'), now, now-60, now+3)
            self.remote['at'] = self.deadline
            self.remote['playing'] = True
            self.remote['preparing'] = False
            self.frames.clear()
            self.media.pause()
            target = min(self.media.duration(), max(0, target_ms(self.remote, now, self.speed.value(), -self.offset.value())))
            if abs(self.media.position()-target) > .5:
                self.media.setPosition(round(target))
            self.scheduled_at = self.deadline
            self.played_at = None
            self.first_frame_at = None
            self.timer.setInterval(1)
        self.refresh_play_label()
        request.finish(self.snapshot())

    def finish_arm(self, now):
        if not self.arm_request:
            return
        video_fps = float(self.media.metaData().value(QMediaMetaData.Key.VideoFrameRate) or 30)
        tolerance = max(1000/video_fps, 1000/(self.remote or {}).get('fps', 30))+2
        ready = abs(self.frame_ms-self.arm_target) <= tolerance
        if ready or now >= self.arm_until:
            request, self.arm_request = self.arm_request, None
            result = self.snapshot()
            result['ready'] = ready
            request.finish(result)

    def tick(self):
        now = time.perf_counter()
        linked = self.following()
        interval = 1 if linked and (self.deadline is not None or self.remote.get('playing')) else 16
        if self.timer.interval() != interval:
            self.timer.setInterval(interval)
        self.finish_arm(now)
        if self.owner and now-self.last_message >= 3:
            self.deadline = None
            self.remote = None
            self.owner = None
            self.control = None
            self.media.pause()
            self.save()
        self.set_connected(bool(self.owner))
        self.filename.setToolTip(self.media_error or (str(self.media_path) if self.media_path else 'No video'))
        for control in [self.play, self.previous, self.next, self.seek]:
            control.setEnabled(self.prepared and (not linked or self.remote.get('controllable', True)))
        if self.control and now-self.control_at > 3:
            self.control = None
        self.refresh_play_label()
        if not linked or not self.prepared or self.arm_request or self.control or self.seek.isSliderDown():
            return
        if self.deadline is not None:
            if now < self.deadline:
                return
            self.deadline = None
            self.timer.setInterval(8)
            self.media.setPlaybackRate(self.speed.value())
            self.media.play()
            self.played_at = now
            self.last_correction = now
        state = self.remote
        target = target_ms(state, now, self.speed.value(), -self.offset.value())
        target = min(self.media.duration(), max(0, target))
        chosen = None
        while self.frames and self.frames[0][0] <= target+.5:
            chosen = self.frames.popleft()
        if chosen:
            self.present(*chosen)
        if not state['playing'] or target <= 0 or target >= self.media.duration():
            self.media.pause()
            if abs(self.media.position()-target) > .5:
                self.media.setPosition(round(target))
            self.last_target = target
            return
        if not self.media.isPlaying():
            self.media.setPosition(round(target))
            self.media.setPlaybackRate(self.speed.value())
            self.media.play()
            self.last_correction = now
        # Seeking again before the decoder returns a frame can starve playback.
        # Allow up to one second to decode after a start/seek, then recover using
        # the media clock if that backend does not timestamp its frames.
        fresh = self.frame_received >= self.last_correction and now-self.frame_received < .5
        if not fresh and now-self.last_correction < 1.0:
            self.last_target = target
            return
        actual = self.frame_ms + max(0, now-self.frame_received)*1000*self.media.playbackRate() if fresh else self.media.position()
        self.last_drift = target-actual
        threshold = max(100, 2000/state['fps'])
        wrapped = target < self.last_target-50
        if (wrapped or abs(self.last_drift)>threshold) and now-self.last_correction>.5:
            self.frames.clear()
            self.media.setPosition(round(target))
            self.last_correction = now
        elif now-self.last_correction>.3:
            rate = self.speed.value()*(1+max(-.025,min(.025,self.last_drift/2000)))
            if abs(rate-self.media.playbackRate())>.002:
                self.media.setPlaybackRate(rate)
        self.last_target = target

    def closeEvent(self, event):
        if not self.leaving:
            self.leaving = True
            event.ignore()
            self.depart()
            return
        self.timer.stop()
        self.media.stop()
        if self.arm_request:
            self.arm_request.finish({'error': 'player closed'})
        self.save()
        self.bridge.close()
        if self.diagnostics:
            self.diagnostics.write_text(json.dumps(self.metrics, indent=2))
        if not self.installing and self.auto_update.isChecked():
            self.updater.install(relaunch=False)
        event.accept()


def main():
    if sys.platform != 'win32':
        raise SystemExit('Eclipse Video is for Windows.')
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('riftkuro.EclipseVideo')
    args = argparse.ArgumentParser()
    args.add_argument('--port', type=int, default=PORT)
    args.add_argument('--settings', type=Path, default=SETTINGS)
    args.add_argument('--video', type=Path)
    args.add_argument('--diagnostics', type=Path)
    options = args.parse_args()
    options.settings.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setApplicationName('Eclipse Video')
    lock = QLockFile(str(options.settings.parent/f'.player-{options.port}.lock'))
    if not lock.tryLock(0):
        return 0
    window = Player(options.port, options.settings, options.diagnostics)
    if options.video:
        window.open_video(options.video)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    window.show()
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
