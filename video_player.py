import os
import webbrowser
import subprocess

from time import sleep
from datetime import timedelta

from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

# from api2.shared import PrintWarning, TimePrintColor, Color

# to load mpv-1.dll or libmpv.dll.a from the thirdparty folder
os.environ["PATH"] = os.path.dirname(__file__) + os.pathsep + os.environ["PATH"]

import mpv


# MPV COMMANDS: https://mpv.io/manual/master/#list-of-input-commands


# duration times this for slider values
# increase for more accurate slider positions, though 10 is fine all around
# i would do frames, but i can't find that in libmpv currently
TIME_MULT = 1
MAX_VOLUME = 100


def dict_to_str(dictionary: dict, depth: int = 0) -> str:
    final_value = ""
    space = "{0}".format(depth * " ")
    for key, value in dictionary.items():
        value_type = type(value)
        
        try:
            value = dict_to_str(value.__dict__, depth + 1)
            final_value += f'{space}"{key}"\n{space}{{\n{value}\n{space}}}"'
        except Exception as F:
            print(str(F))
            
            if value and type(value) == dict:
                value = dict_to_str(value, depth + 1)
                final_value += f'{space}"{key}"\n{space}{{\n{value}\n{space}}}"'
            else:
                final_value += f'{space}"{key}" "{value}"'
                
    return final_value


# TODO: maybe jam player.demuxer_lavf_list into the VIDEO_EXTS stuff in qt5_client_embed.py?

# maybe useful mpv stuff:
# file_format
# filename
# file_size
# loop
# loop_file
# loop_playlist
# media_title (same as filename?)
# playlist: list of dictionaries
# playlist_filenames: list of urls/paths
# track_list


MONITOR_ZOOM_HACK = "0.825"  # 1


class VideoContainer(QWidget):
    def __init__(self, player):
        super().__init__(player)
        self.player = player
        layout = QVBoxLayout()
        self.setLayout(layout)
        self.setFocusPolicy(Qt.StrongFocus)
        # self.setMinimumHeight(400)
        # self.layout().addWidget(QLabel("Ready to Play Video"))
        self.w = 0
        self.h = 0
        
    def SetResolution(self, width: int, height: int):
        self.w = width
        self.h = height
        
    # TODO: this would need to check the ChatView resolution somehow
    def SetVideoScaling(self, use_scaling: bool):
        if use_scaling:
            # self.setMaximumSize(0, 0)
            self.setMinimumSize(0, 0)
        else:
            # self.setMaximumSize(self.w, self.h)
            # self.setMinimumSize(min(854, self.w), min(480, self.h))
            self.setMinimumSize(min(640, self.w), min(480, self.h))
    
    def mousePressEvent(self, event: QMouseEvent) -> None:
        # if event.button() == Qt.LeftButton:
        if event.button() == Qt.RightButton:
            self.player.playback_toggle()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Standard keyboard inputs
        if event.key() == Qt.Key_Left:
            self.player.seek(-2)

        if event.key() == Qt.Key_Right:
            self.player.seek(2)

        # LAZY HACK TO MATCH MY CONFIG FILE

        # reset key
        if event.key() == Qt.Key_0:
            self.player.player.command("set", "video-zoom", "0")
            self.player.player.command("set", "video-pan-x", "0")
            self.player.player.command("set", "video-pan-y", "0")

        # auto left monitor
        elif event.key() == Qt.Key_7:
            self.player.player.command("set", "video-zoom", MONITOR_ZOOM_HACK)
            self.player.player.command("set", "video-pan-x", "0.25")

        # auto right monitor
        elif event.key() == Qt.Key_8:
            self.player.player.command("set", "video-zoom", MONITOR_ZOOM_HACK)
            self.player.player.command("set", "video-pan-x", "-0.25")

        # auto karl pov right monitor
        elif event.key() == Qt.Key_5:
            self.player.player.command("set", "video-zoom", "0.8075")
            self.player.player.command("set", "video-pan-x", "-0.2145")

        elif event.modifiers() & Qt.AltModifier:
            if event.key() == Qt.Key_Plus:
                self.player.player.command("add", "video-zoom", "0.05")

            elif event.key() == Qt.Key_Minus:
                self.player.player.command("add", "video-zoom", "-0.05")

            elif event.key() == Qt.Key_Up:
                self.player.player.command("add", "video-pan-y", "0.05")

            elif event.key() == Qt.Key_Down:
                self.player.player.command("add", "video-pan-y", "-0.05")

            elif event.key() == Qt.Key_Right:
                self.player.player.command("add", "video-pan-x", "-0.05")

            elif event.key() == Qt.Key_Left:
                self.player.player.command("add", "video-pan-x", "0.05")
    
    
class VideoProgress(QSlider):
    def __init__(self, player):
        super().__init__(Qt.Horizontal)
        self.player = player
        self.moved = False
        self.locked = False
        
    def slider_update(self, progress: int, *bruh) -> None:
        if not self.locked:
            self.blockSignals(True)
            self.setValue(int(progress))
            self.blockSignals(False)
        
    def slider_user_update(self, event: QMouseEvent) -> None:
        if self.player.has_video:
            # both work fine
            # self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), event.x(), self.width()))
            value = self.minimum() + ((self.maximum() - self.minimum()) * event.x()) / self.width()
            self.setValue(int(value))
    
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.locked = True
            self.slider_user_update(event)
            event.accept()
            self.locked = False
    
    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftToRight:
            self.locked = True
            self.slider_user_update(event)
            event.accept()
    
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        button = event.button()
        if event.button() == Qt.LeftButton:
            self.locked = False
            event.accept()
        

class VideoPlayer(QWidget):
    sig_has_video = pyqtSignal()
    sig_timeout = pyqtSignal()
    
    def __init__(self, parent=None, start_paused: bool = True):
        super().__init__(parent)
        
        self.videoEditor = parent
        
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        # main_widget = QWidget(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        self.player_widget = VideoContainer(self)
        self.player_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.position_slider = VideoProgress(self)
        self.position_slider.sliderPressed.connect(self.playback_toggle_slider)
        self.position_slider.sliderReleased.connect(self.playback_toggle_slider)
        # self.position_slider.sliderMoved.connect(self.slider_seek)
        self.position_slider.valueChanged.connect(self.slider_seek)
        
        pos_slider_widget_awful_killme = QWidget()
        pos_slider_layout = QHBoxLayout()
        pos_slider_widget_awful_killme.setLayout(pos_slider_layout)
        pos_slider_layout.setContentsMargins(8, 0, 8, 0)
        pos_slider_layout.addWidget(self.position_slider)
        
        self.position_thread = VideoPosition(self)
        
        playback_button_widget = QWidget()
        playback_button_layout = QHBoxLayout()
        
        self.btn_toggle_playback = QPushButton(">")
        self.btn_frame_prev = QPushButton("<|")
        self.btn_frame_next = QPushButton("|>")
        self.btn_full_screen = QPushButton("Fullscreen")
        self.btn_copy_time = QPushButton("Copy Time")
        self.btn_copy_filename = QPushButton("Copy Filename")
        self.volume_slider = QSlider(Qt.Horizontal)
        self.btn_open_folder = QPushButton("Open Folder")

        self.txt_file_path = QLabel("None")
        self.timeDisplay = QLabel("")
        self.volDisplay = QLabel("")
        
        self.btn_toggle_playback.setMaximumWidth(30)
        self.btn_frame_prev.setMaximumWidth(30)
        self.btn_frame_next.setMaximumWidth(30)
        self.btn_full_screen.setMaximumWidth(70)
        #self.btn_set_start_time.setFixedWidth(90)
        #self.btn_set_end_time.setFixedWidth(90)
        self.btn_copy_time.setFixedWidth(70)
        self.btn_copy_filename.setFixedWidth(100)
        self.btn_open_folder.setFixedWidth(80)
        self.timeDisplay.setFixedWidth(130)
        self.volDisplay.setFixedWidth(24)
        
        self.btn_toggle_playback.pressed.connect(self.playback_toggle)
        self.btn_frame_prev.pressed.connect(self._prev_frame)
        self.btn_frame_next.pressed.connect(self._next_frame)
        self.btn_full_screen.pressed.connect(self.toggle_full_screen)
        self.btn_copy_time.pressed.connect(self.replay_copy_time)
        self.btn_copy_filename.pressed.connect(self.replay_copy_filename)
        self.btn_open_folder.pressed.connect(self.open_folder)
        
        self.volume_slider.valueChanged.connect(self.change_volume)
        self.volume_slider.setRange(0, MAX_VOLUME)

        self.txt_file_path.setTextInteractionFlags(Qt.TextSelectableByMouse)

        playback_button_widget.setLayout(playback_button_layout)
        playback_button_layout.addWidget(self.btn_toggle_playback)
        playback_button_layout.addWidget(self.btn_frame_prev)
        playback_button_layout.addWidget(self.btn_frame_next)
        playback_button_layout.addWidget(self.timeDisplay)
        #playback_button_layout.addWidget(self.btn_full_screen)
        playback_button_layout.addWidget(self.btn_copy_time)
        playback_button_layout.addWidget(self.btn_copy_filename)
        playback_button_layout.addWidget(self.btn_open_folder)
        playback_button_layout.addWidget(self.txt_file_path)
        playback_button_layout.addStretch(-1)
        playback_button_layout.addWidget(self.volume_slider)
        playback_button_layout.addWidget(self.volDisplay)
        

        # playback_button_layout.setContentsMargins(0, 0, 0, 0)
        playback_button_layout.setContentsMargins(8, 0, 8, 8)
        #playback_button_layout.setContentsMargins(4, 0, 4, 4)
        
        self.setLayout(main_layout)

        # main_widget.setLayout(main_layout)
        # main_layout.addWidget(main_widget)
        main_layout.addWidget(self.player_widget)
        # main_layout.addStretch(0)
        #main_layout.addWidget(self.position_slider)
        main_layout.addWidget(pos_slider_widget_awful_killme)
        main_layout.addWidget(playback_button_widget)

        self.selected_video = None
        self.current_video = None
        self.file_dialog = None
        self.duration = None
        
        # options for mpv are set here, so to use --volume-max 400, you would add `volume_max=400`
        self.player = mpv.MPV(wid=str(int(self.player_widget.winId())), pause=start_paused, hr_seek="yes"
                              # vo='x11', # You may not need this
                              # log_handler=print, loglevel='debug'
                              )
        
        self.player.keep_open = True
        
        self.old_state = self.windowState()
        self.has_video = False
        self.has_audio = False

        self.sig_has_video.connect(self.__setup_video_player)
        self.sig_timeout.connect(self.__time_out)
        
        # volume: self.player.properties["volume"]
        
        # self.setAcceptDrops(True)
        self.show()
        self.player_widget.hide()
        
    def __del__(self):
        self.player.quit()
        del self.player

    # NOTE: unused function?
    def set_video_path(self, video_path: str):
        self.selected_video = video_path
        self.txt_file_path.setText(video_path)

    def dropEvent(self, event: QDropEvent):
        text = event.mimeData().text()
        header = ""
        if text.startswith("file:///"):
            header = "file:///"
            
        self.load_video(text[len(header):])

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        text = event.mimeData().text()
        if text.startswith("file:///"):
            event.accept()
        elif text.startswith("https://") or text.startswith("ftp://"):
            event.accept()

    def keyPressEvent(self, event):
        self.player_widget.keyPressEvent(event)
        super(VideoPlayer, self).keyPressEvent(event)

    def load_video(self, video_path: str) -> None:
        self.selected_video = video_path
        self.current_video = video_path
        self.txt_file_path.setText(video_path)
        
        if not self.has_vid_or_audio():
            self.btn_toggle_playback.setText(">")
            
        # self.player.play(video_path)
        self.player.loadfile(video_path)
        
        # self.player.observe_property("estimated_frame_count", self.position_slider.slider_update)
        # self.player.observe_property("ao-volume", self.position_slider.slider_update)
        # self.player.observe_property("ao-volume", self.volume_slider.setValue)
        
        self.position_slider.setValue(0)
        self.position_slider.setMaximum(0)
        self.position_thread.start(1)
        
        # volume = self.get_volume()
        # self.volume_slider.setValue(volume)
        # if self.prev_volume != -1:
        #     self.volume_slider.setValue(100)
        
        # wait for mpv to fully load the video by waiting for this property
        self.player.observe_property("seekable", self.media_loaded)
        
        # look at this when you come back to this again
        # https://github.com/jaseg/python-mpv/issues/79#issuecomment-449868473
        
    def media_loaded(self, *nothing):
        try:
            for track in self.player.track_list:
                #TimePrintColor(Color.GREEN, "MPV Widget: setting tracks")
                print("MPV Widget: setting tracks")
                if track["type"] == "audio":
                    self.has_audio = True
                if track["type"] == "video":
                    self.has_video = True
                    self.sig_has_video.emit()
                    # check resolution
            # sleep(2)
            if self.has_vid_or_audio():
                #TimePrintColor(Color.GREEN, f"MPV Widget: media loaded - \"{os.path.basename(self.current_video)}\"")
                
                volume = self.get_volume()
                self.volume_slider.setValue(int(volume))
                
                self.update_duration()
                
                print(f"MPV Widget: media loaded - \"{os.path.basename(self.current_video)}\"")
        except OSError:
            self.txt_file_path.setText("None")
            pass
        
    def __time_out(self):
        self.player_widget.show()
        self.player_widget.layout().addWidget(QLabel("Timed Out"))
        self.txt_file_path.setText("None")
        
    def __setup_video_player(self):
        # "demux-fps": 30.0
        self.player_widget.show()
        for track in self.player.track_list:
            if track["type"] == "video":
                if not track["selected"]:
                    continue
                self.player_widget.SetResolution(track["demux-w"], track["demux-h"])
                self.player_widget.SetVideoScaling(False)
                break
        
    def toggle_full_screen(self):
        if self.isFullScreen():
            self.setWindowFlags(Qt.Widget)
            self.setWindowState(self.old_state)
            self.player_widget.SetVideoScaling(False)
            self.show()
        else:
            self.old_state = self.windowState()
            self.setWindowFlags(Qt.Window)
            self.setWindowState(Qt.WindowFullScreen)
            self.player_widget.SetVideoScaling(True)
            self.show()

    def update_duration(self):
        if self.position_slider.maximum() == 0:
            frame_count = self.player.estimated_frame_count
            if frame_count is not None:
                self.position_slider.setRange(0, frame_count)
            
    def replay_set_start_time(self):
        self.videoEditor.set_start_time()
        
    def replay_set_end_time(self):
        self.videoEditor.set_end_time()
        
    def replay_copy_time(self):
        self.videoEditor.copy_current_time()
        
    def replay_copy_filename(self):
        self.videoEditor.copy_filename()
        
    def playback_toggle_slider(self, *uh) -> None:
        if self.has_vid_or_audio():
            self.player.command("seek", str(self.position_slider.value()), "absolute")
            if not self.player.pause:
                self.player.command("cycle", "pause")
                self.btn_toggle_playback.setText(">")
            else:
                self.btn_toggle_playback.setText(">")
        
    def playback_toggle(self, *uh) -> None:
        if self.selected_video and not self.current_video:
            self.load_video(self.selected_video)
        if self.has_vid_or_audio():
            self.player.command("cycle", "pause")
            text = ">" if self.player.pause else "||"
            self.btn_toggle_playback.setText(text)
        
    def pause_video(self, *uh) -> None:
        if self.get_video() and not self.player.pause:
            self.player.command("cycle", "pause")
            self.btn_toggle_playback.setText(">")
        
    def resume_video(self, *uh) -> None:
        if self.has_vid_or_audio() and self.player.pause:
            self.player.command("cycle", "pause")
            self.btn_toggle_playback.setText("||")
        
    def slider_seek(self, value: int) -> None:
        try:
            pass
            if self.has_vid_or_audio():
                new_time = self.get_percentage(value)
                self.player.command("seek", str(new_time), "absolute-percent")
                # self.player.command("seek", str(new_time), "absolute")
        except SystemError:
            print("video over")

    def seek(self, value: int) -> None:
        try:
            pass
            if self.has_vid_or_audio():
                # new_time = self.get_percentage(value)
                # self.player.command("seek", str(new_time), "absolute-percent")
                self.player.command("seek", str(value))
                # self.player.command("seek", str(new_time), "absolute")

                # Update Progress Slider
                try:
                    if self.player.pause:
                        # progress = self.player.time_pos
                        progress = self.player.estimated_frame_number
                        if progress:
                            self.position_slider.slider_update(progress)

                    self.position_thread.update_scroll()
                except Exception as F:
                    pass
        except SystemError:
            print("video over")
        
    def get_seconds_from_frames(self, frame_number: int) -> float:
        if self.has_video and self.player.display_fps is not None:
            return frame_number / self.player.display_fps
        return 0.0
        
    def get_playback_time(self) -> float:
        return self.player.playback_time if self.has_video else 0.0
        
    def get_duration(self) -> float:
        return self.player.duration if self.has_vid_or_audio() else 0.0
    
    def get_percent_completed(self) -> float:
        return self.player.percent_pos if self.has_video else 0.0
        
    def get_percentage(self, frame_number: int) -> float:
        if self.has_video and frame_number != 0:
            return (frame_number / self.player.estimated_frame_count) * 100
        return 0.0
        
    def get_total_frames(self) -> int:
        pass
    
    def get_current_frame(self) -> int:
        #return self.player.player.estimated_frame_number
        return self.player.estimated_frame_number
        
    def has_vid_or_audio(self) -> bool:
        return self.has_audio or self.has_video
        
    def get_video(self) -> str:
        return self.player.media_title
        
    # def get_playlist(self) -> bool:
    #     return self.player.playlist_filenames
    
    def _next_frame(self, *uh):
        if self.has_vid_or_audio():
            self.player.command("frame-step")
        # self.position_slider.setValue(self.get_seconds_from_frames(self.player.estimated_frame_number))
    
    def _prev_frame(self, *uh):
        if self.has_vid_or_audio():
            self.player.command("frame-back-step")
        # self.position_slider.setValue(self.get_seconds_from_frames(self.player.estimated_frame_number))
    
    def get_volume(self, *uh):
        if self.has_audio:
            # return self.player.command("get_property", "ao-volume")
            # return self.player.observe_property("ao-volume", )
            return self.player.ao_volume
    
    def set_volume(self, volume):
        if self.has_audio:
            return self.player.command("set_property", "ao-volume", str(volume))
    
    def change_volume(self, volume: int) -> None:
        if self.has_audio:
            try:
                volumeStr = str(min(volume, MAX_VOLUME))
                self.player.command("set", "ao-volume", volumeStr)
                self.volDisplay.setText(volumeStr)
            except SystemError:
                pass

    # https://www.geoffchappell.com/studies/windows/shell/explorer/cmdline.htm
    def open_folder(self) -> None:
        if self.current_video:
            file_path = self.current_video.replace("/", "\\")
            subprocess.Popen(fr'explorer /select,"{file_path}"')


def timedelta_to_str(time: timedelta):
    hours, remainder = divmod(time.total_seconds(), 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{:02}:{:02}:{:02}".format(int(hours), int(minutes), float(seconds))


class VideoPosition(QTimer):
    def __init__(self, player: VideoPlayer):
        super().__init__(player)
        self.player = player
        self.exiting = False
        self.timeout.connect(self.update_scroll)
    
    def update_scroll(self):
        try:
            if self.player.has_video and not self.player.player.pause:
                # progress = self.player.player.time_pos
                progress = self.player.player.estimated_frame_number
                if progress:
                    self.player.position_slider.slider_update(progress)

            if self.player.has_video:
                #self.player.timeDisplay.setText(f"{self.player.player.time_pos}")
                playbackPos = str(timedelta(seconds=self.player.player.playback_time))
                duration = str(timedelta(seconds=self.player.player.duration))
                
                if "." not in playbackPos:
                    playbackPos += ".000"
                else:
                    playbackPos = playbackPos[:-3]
                
                if "." not in duration:
                    duration += ".000"
                else:
                    duration = duration[:-3]
                
                self.player.timeDisplay.setText(f"{playbackPos} / {duration}")
        except (TypeError, AttributeError) as F:
            print(F)
            sleep(0.1)
            #self.stop()
            #return




