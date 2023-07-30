import os
import sys
import shutil
import psutil
import subprocess
import hashlib
import threading
import argparse
import traceback
from typing import List, Dict, Tuple
import time

from datetime import datetime, timedelta

import demez_key_values as lexer
from video_player import VideoPlayer
from replay_logging import *


if os.name == "nt":
    from ctypes import windll, wintypes, byref, FormatError, WinError


# TEMP_FOLDER = ("TEMP" + os.sep + str(datetime.now()) + os.sep).replace(":", "-").replace(".", "-")
ROOT_FOLDER = f"{os.path.dirname(os.path.realpath(__file__))}{os.sep}"
TEMP_FOLDER = f"{ROOT_FOLDER}TEMP{os.sep}"

cmd_bar_line = "-----------------------------------------------------------"

ALL_SEARCH_PATHS = []


def parse_args() -> argparse.Namespace:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("-i", "--input", default="timestamps.txt")
    arg_parser.add_argument("-v", "--verbose", action="store_true")
    arg_parser.add_argument("-e", "--encode", action="store_true")
    arg_parser.add_argument("-2", "--encode-2pass", action="store_true")
    arg_parser.add_argument("-r", "--encode-raw", action="store_true")
    arg_parser.add_argument("-m", "--move-files", action="store_true")
    arg_parser.add_argument("-k", "--keep-temp", action="store_true")
    arg_parser.add_argument("--high", action="store_true", help="high priority")
    arg_parser.add_argument("--below-normal", action="store_true", help="below normal priority")
    arg_parser.add_argument("--raw-ffmpeg", action="store_true")
    arg_parser.add_argument("-c", "--cpus", nargs=2, help="cpu affinity range", default=[0, 8])
    return arg_parser.parse_args()


def PrintException(*args):
    print("", *args, traceback.format_exc())

        
def get_hash(string: str):
    return hashlib.md5(string.encode('utf-8')).hexdigest()


def get_date_modified(file: str):
    return os.path.getmtime(file) if os.name == "nt" else os.stat(file).st_mtime
    
    
def get_date_created(file: str) -> float:
    return os.path.getctime(file) if os.name == "nt" else os.stat(file).st_atime


def set_file_times(file: str, created: float, mod: float, access: float):
    if os.name == "nt":
        os.utime(file, (access, mod))
        win32_setctime(file, created)
    else:
        oldest_date = min(created, mod, access)
        os.utime(file, (access, oldest_date))


# https://github.com/Delgan/win32-setctime/blob/master/win32_setctime.py
def win32_setctime(file: str, timestamp: float):
    if not os.name == "nt":
        return
    
    """Set the "ctime" (creation time) attribute of a file given an unix timestamp (Windows only)."""
    file = os.path.normpath(os.path.abspath(str(file)))
    timestamp = int((timestamp * 10000000) + 116444736000000000)
    
    if not 0 < timestamp < (1 << 64):
        raise ValueError("The system value of the timestamp exceeds u64 size: %d" % timestamp)
    
    atime = wintypes.FILETIME(0xFFFFFFFF, 0xFFFFFFFF)
    mtime = wintypes.FILETIME(0xFFFFFFFF, 0xFFFFFFFF)
    ctime = wintypes.FILETIME(timestamp & 0xFFFFFFFF, timestamp >> 32)
    
    handle = wintypes.HANDLE(windll.kernel32.CreateFileW(file, 256, 0, None, 3, 128, None))
    if handle.value == wintypes.HANDLE(-1).value:
        raise WinError()
    
    if not wintypes.BOOL(windll.kernel32.SetFileTime(handle, byref(ctime), byref(atime), byref(mtime))):
        raise WinError()
    
    if not wintypes.BOOL(windll.kernel32.CloseHandle(handle)):
        raise WinError()


def calc_bitrate_from_bpp(width: float, height: float, fps: float, bits_per_pixel: float):
    return (width * height * fps * bits_per_pixel) / 1000


def calc_bpp(rate: float, width: float, height: float, fps: float):
    # data rate / (resolution * frames per second) = BPP
    return rate / (width * height * fps)


# ==================================================================================================
# Timestamp File Parsing
# ==================================================================================================

ALL_INPUT_VIDEOS = []


class VideoSettings:
    def __init__(self):
        self.cmd = []
        self.cmdRaw = []

        self.cmdPass1 = []
        self.cmdPass2 = []
        
        self.targetSize = 0  # in KB
        self.audioBitrate = 160  # ok
        
    def copy_settings(self, other):
        self.cmd = other.cmd.copy()
        self.cmdRaw = other.cmdRaw.copy()
        
        self.cmdPass1 = other.cmdPass1.copy()
        self.cmdPass2 = other.cmdPass2.copy()
        
        self.targetSize = other.targetSize
        self.audioBitrate = other.audioBitrate

    def parse_setting(self, config_folder: str, search_paths: List[str], block_obj: lexer.DemezKeyValue) -> bool:
        def get_input_dir() -> str:
            if os.path.isabs(block_obj.value):
                input_dir = os.path.normpath(block_obj.value) + os.sep
            else:
                # TODO: should this always be locol to the config folder,
                #  or should it be local to the last item in the inputDirStack?
                input_dir = os.path.normpath(config_folder + os.sep + block_obj.value) + os.sep
            return input_dir

        if block_obj.key == "$addSearchPath":
            input_dir = get_input_dir()

            if input_dir not in ALL_SEARCH_PATHS:
                ALL_SEARCH_PATHS.append(input_dir)

            if input_dir not in search_paths:
                search_paths.append(input_dir)
            else:
                warning(f"Search Path already used: \"{input_dir}\"")

            return True
    
        elif block_obj.key == "$rmSearchPath":
            input_dir = get_input_dir()
            if input_dir in search_paths:
                search_paths.remove(input_dir)
            else:
                warning(f"search path not found for removal: {input_dir}")
            return True
    
        elif block_obj.key == "$cmd":
            self.cmd.append(block_obj.value)
            return True

        elif block_obj.key == "$cmdPop":
            self.cmd.pop()
            return True
    
        elif block_obj.key == "$cmdRaw":
            self.cmdRaw.append(block_obj.value)
            return True
    
        elif block_obj.key == "$cmdPopRaw":
            self.cmdRaw.pop()
            return True
    
        elif block_obj.key == "$cmdPass1":
            self.cmdPass1.append(block_obj.value)
            return True
    
        elif block_obj.key == "$cmdPass2":
            self.cmdPass2.append(block_obj.value)
            return True
    
        elif block_obj.key == "$targetSize":
            self.targetSize = int(block_obj.value)
            return True
    
        elif block_obj.key == "$audioBitrate":
            self.audioBitrate = int(block_obj.value)
            return True
    
        return False
        
        
class OutputVideoSettings(VideoSettings):
    def __init__(self):
        super().__init__()
        self.videoPrefix = ""
        self.videoPrefixRaw = ""
        
        self.videoExt = ""
        self.videoExtRaw = ""
        
        # self.useDateMod = True
        self.timeCfg = "v0"  # use input video 0's date modified by default
        
    def copy_settings(self, other):
        super().copy_settings(other)
        
        self.videoPrefix = other.videoPrefix
        self.videoPrefixRaw = other.videoPrefixRaw
    
        self.videoExt = other.videoExt
        self.videoExtRaw = other.videoExtRaw
        
        self.timeCfg = other.timeCfg

    # def parse_setting(self, block_obj: lexer.DemezKeyValue) -> bool:
    def parse_setting(self, configFolder: str, search_paths: List[str], block_obj: lexer.DemezKeyValue) -> bool:
        if super().parse_setting(configFolder, search_paths, block_obj):
            return True
        
        elif block_obj.key == "$outPrefix":
            self.videoPrefix = block_obj.value
            return True
    
        elif block_obj.key == "$outPrefixRaw":
            self.videoPrefixRaw = block_obj.value
            return True
    
        elif block_obj.key == "$outExt":
            self.videoExt = block_obj.value
            return True
    
        elif block_obj.key == "$outExtRaw":
            self.videoExtRaw = block_obj.value
            return True
    
        elif block_obj.key == "$time":
            # self.useDateMod = block_obj.value == "true" or block_obj.value == "1"
            self.timeCfg = block_obj.value
            return True
    
        return False
    

class VideoFile(VideoSettings):
    def __init__(self, videoPath: str):
        super().__init__()

        self.videoPath = os.path.abspath(videoPath)
        self.videoName = os.path.split(self.videoPath)[1]
        self.videoDir = os.path.split(self.videoPath)[0]
        
        self.timeRanges: List[List[timedelta]] = []
        self.timeRangesStr: List[List[str]] = []

        self.origInfo = {}

        self.markers_tmp = []

        self.get_orig_info()

        # add ourself to this list
        ALL_INPUT_VIDEOS.append(self.videoPath)

    def add_auto_marker(self, start: str, end: str):
        self.add_marker_ex("auto webm", start, end)

    def add_marker(self, name: str, time: str):
        final_time = self.get_video_length() if time == '' else to_timedelta(time)
        self.markers_tmp.append([name, final_time, final_time])

    def add_marker_ex(self, name: str, start: str, end: str):
        self.markers_tmp.append([name, to_timedelta(start), self.get_video_length() if end == '' else to_timedelta(end)])

    def add_time_range(self, start: str, end: str):
        self.timeRanges.append([to_timedelta(start), self.get_video_length() if end == '' else to_timedelta(end)])

    # duration in seconds
    def get_duration_range(self, index: int) -> float:
        start = self.timeRanges[index][0]
        end = self.timeRanges[index][1]
        duration_td = end - start
        return duration_td.total_seconds()

    # duration in seconds
    def get_duration(self) -> float:
        duration = 0
        for index, _ in enumerate(self.timeRanges):
            duration += self.get_duration_range(index)
        return duration
    
    # duration in seconds
    def get_duration_list(self) -> List[float]:
        duration = []
        for index, _ in enumerate(self.timeRanges):
            duration.append(self.get_duration_range(index))
        return duration
    
    def calc_target_bitrates(self, outputSize, audioBitrate) -> List[float]:
        # origBitrate = os.path.getsize(self.videoPath) / self.origInfo["duration"].total_seconds()

        # 500 MB / 3600 sec * 8 bits/byte * 1024 KB/MB ~ 1,137 kb/s
        # 1,165 kilobits/sec x 1,000 bits/kilobit x 3600 sec / 8 bits/byte / 1024 KB/byte / 1024 MB/Byte = 500 MB = 500 MiB
        origBitrate = self.origInfo["bitrate"]

        # CALC BPP
        # calc_bpp(rate: float, width: float, height: float, fps: float)
        bpp = calc_bpp(origBitrate, self.origInfo["width"], self.origInfo["height"], self.origInfo["fps"])

        newBitrate = calc_bitrate_from_bpp(self.origInfo["width"], self.origInfo["height"], self.origInfo["fps"], bpp)

        bitrates = []
        durations = self.get_duration_list()
        for index, _ in enumerate(self.timeRanges):
            # KB to MB to Kbit - dumb
            # bitrate = (outputSize * 0.001 * 8192) / durations[index]
            bitrate = (outputSize / durations[index]) * 8
            # subtract audio bitrate
            bitrate -= audioBitrate
            bitrates.append(bitrate)
        return bitrates

    def get_video_length(self):
        return self.origInfo["duration"]

    def get_orig_info(self):
        ffprobe_command = "ffprobe -threads 6 -v error -show_streams -show_format " \
                          "-of default=noprint_wrappers=1 \"" + self.videoPath + '"'

        output = subprocess.check_output(ffprobe_command, shell=True)

        self.origInfo["bitrate"] = 0
        self.origInfo["duration"] = 0
        self.origInfo["width"] = 0
        self.origInfo["height"] = 0
        self.origInfo["fps"] = 0
        self.origInfo["fps2"] = "0/0"

        # clean up the output
        for line in str(output).split("\\n"):
            if line.startswith("b\'"):
                line = line.split("b\'")[1]
            line = line.split("\\r")[0]
            
            if line.endswith("N/A") or line == '\'' or line == '"':
                continue
                
            key, value = line.rsplit("=", 1)
                
            if key == "bit_rate":
                self.origInfo["bitrate"] = float(value)

            elif key == "duration":
                self.origInfo["duration"] = to_timedelta(value)

            elif key == "width":
                self.origInfo["width"] = int(value)

            elif key == "height":
                self.origInfo["height"] = int(value)

            elif key == "r_frame_rate":
                if value == "0/0":
                    continue

                self.origInfo["fps2"] = value
                fps0, fps1 = value.split("/", 1)
                fps = float(fps0)/float(fps1)
                self.origInfo["fps"] = fps

        pass


class OutputVideo(OutputVideoSettings):
    def __init__(self, videoPath: str):
        super().__init__()

        self.videoPath = os.path.abspath(videoPath)
        self.videoName = os.path.split(self.videoPath)[1]
        self.videoDir = os.path.split(self.videoPath)[0]
        
        self.videoPrefix = ""
        self.videoPrefixRaw = ""
        
        self.videoExt = ""
        self.videoExtRaw = ""
        
        self.inputVideos: List[VideoFile] = []
        self.skip: bool = False
        self.hashList: List[str] = []
        self.dateFile = ""

        self.markers = []
        
    def get_video_name(self) -> str:
        if ARGS.encode_raw:
            return self.videoPrefixRaw + os.path.splitext(self.videoName)[0] + self.videoExtRaw
        else:
            return self.videoPrefix + os.path.splitext(self.videoName)[0] + self.videoExt
        
    def get_video_dir(self) -> str:
        return os.path.normpath(os.path.split(self.videoDir + os.sep + self.get_video_name())[0])
        
    def get_video_path(self) -> str:
        return os.path.normpath(self.videoDir + os.sep + self.get_video_name())
        
    def create_input_video(self, videoPath: str) -> VideoFile:
        # if check:
        #    for input_video_obj in self.inputVideos:
        #        if video_path == input_video_obj.abspath:
        #            return

        inputVideo = VideoFile(videoPath)
        inputVideo.copy_settings(self)
        self.inputVideos.append(inputVideo)
        return inputVideo
    
    def get_date_file(self) -> str:
        if self.timeCfg == "":
            return self.dateFile
        
        # TODO: parse this bizzare idea properly
        if self.timeCfg == "v0":
            self.dateFile = self.inputVideos[0].videoPath
            
        return self.dateFile

    # maybe use this? https://exiftool.org/
    def write_metadata(self):
        # USELESS RIGHT NOW !!!
        return
        
        metadataFolder = f"{ROOT_FOLDER}/metadata"
        videoName = f"{metadataFolder}/{os.path.splitext(os.path.basename(self.get_date_file()))[0]}.txt"
        
        cmd = (
            "ffmpeg -hide_banner -y",
            "-i \"" + self.dateFile + "\"" if self.dateFile else "",
            "-c copy",
            "-map_metadata 0",
            "-map_metadata:s:v 0:s:v",
            "-map_metadata:s:a 0:s:a",
            "-f ffmetadata",
            f'"{videoName}"'
        )

        if not os.path.exists(metadataFolder):
            os.makedirs(metadataFolder)
    
        subprocess.run(" ".join(cmd), shell=True)

        if not os.path.isfile(videoName):
            print("ffmpeg died")

    @staticmethod
    # def get_metadata_chapter_str(name, pos: timedelta) -> list:
    def get_metadata_chapter_str(name, start: timedelta, end: timedelta) -> list:
        name = name.replace('#', '\\#')
        start_new = start.total_seconds() * 1000
        end_new = end.total_seconds() * 1000
        metadata = [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_new}",
            f"END={end_new}",
            f"title={name}",
            ""  # empty line for spacing
        ]

        print(f"Adding Chapter Metadata: {name} - {start} - {end}")

        return metadata

    @staticmethod
    # def get_metadata_chapter_str(name, pos: timedelta) -> list:
    def get_metadata_chapter_str2(name, start: timedelta) -> list:
        name = name.replace('#', '\\#')
        start_new = start.total_seconds() * 1000
        metadata = [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_new}",
            f"END={start_new}",
            f"title={name}",
            ""  # empty line for spacing
        ]

        print(f"Adding Chapter Metadata: {name} - {start}")

        return metadata

    def get_metadata_cmd(self) -> List[str]:
        videoFile: str = self.get_date_file()
        if not videoFile:
            return []

        # write a metadata file
        metadataFolder = f"{ROOT_FOLDER}metadata"

        if not os.path.exists(metadataFolder):
            os.makedirs(metadataFolder)

        # videoName = f"{metadataFolder}/{os.path.splitext(os.path.basename(self.get_date_file()))[0]}.txt"
        videoName = f"{metadataFolder}/{self.videoName}.txt"

        date_mod = datetime.fromtimestamp(os.path.getmtime(videoFile))
        date_access = datetime.fromtimestamp(os.path.getatime(videoFile))

        metadata = [
            ";FFMETADATA1",
            "",
            # 'demez_date_encoded="' + str(datetime.now()).replace(':', '-') + '"',
            # 'demez_date_modified="' + str(date_mod).replace(':', '-') + '"',
        ]

        # if os.name == "nt":
        #     date_created = datetime.fromtimestamp(get_date_created(videoFile))
        #     metadata.append('demez_date_created="' + str(date_created).replace(':', '-') + '"')

        for marker in self.markers:
            # metadata.extend(self.get_metadata_chapter_str(marker[0], marker[1], marker[2]))
            metadata.extend(self.get_metadata_chapter_str2(marker[0], marker[1]))
            metadata.extend(self.get_metadata_chapter_str2(marker[0], marker[2]))

        with open(videoName, "w") as metadata_file:
            metadata_file.write("\n".join(metadata))

        metadata_cmd = [
            # f"-f ffmetadata -i \"metadata{os.sep}{videoName}.txt\"",
            f"-i \"{videoName}\"",
            f"-map_metadata 0",
            f"-map_metadata 1",
            '-metadata demez_date_encoded="' + str(datetime.now()).replace(':', '-') + '"',
            '-metadata demez_date_modified="' + str(date_mod).replace(':', '-') + '"',
        ]

        if os.name == "nt":
            date_created = datetime.fromtimestamp(get_date_created(videoFile))
            metadata_cmd.append('-metadata demez_date_created="' + str(date_created).replace(':', '-') + '"')

        return metadata_cmd

        # --------------------------------------------------
        # OLD STYLE
    
        metadata = [
            # f"-f ffmetadata -i \"metadata{os.sep}{videoName}.txt\"",
            f"-map_metadata 0",
            '-metadata demez_date_encoded="' + str(datetime.now()).replace(':', '-') + '"',
            '-metadata demez_date_modified="' + str(date_mod).replace(':', '-') + '"',
            '-metadata demez_date_access="' + str(date_access).replace(':', '-') + '"'
        ]
    
        if os.name == "nt":
            date_created = datetime.fromtimestamp(get_date_created(videoFile))
            metadata.append('-metadata demez_date_created="' + str(date_created).replace(':', '-') + '"')
    
        return metadata

    # duration in seconds
    def get_duration(self) -> float:
        duration = 0
        for inputVideo in self.inputVideos:
            duration += inputVideo.get_duration()
        return duration
    
    def get_video_index(self, inputVideo, timeIndex) -> int:
        index = -1
        
        for vidIndex, video in enumerate(self.inputVideos):
            for vidTimeIndex, _ in enumerate(video.timeRanges):
                index += 1

                if video == inputVideo and vidTimeIndex == timeIndex:
                    return index
        
        return index
    
    def calc_target_bitrate(self) -> List[float]:
        outFileSize = 0

        durationList = []
        bitrateListRaw = []
        for inputVideo in self.inputVideos:
            durationList.extend(inputVideo.get_duration_list())
            bitrateListRaw.extend(inputVideo.calc_target_bitrates(self.targetSize, self.audioBitrate))
        
        totalDuration = sum(durationList)
        totalVidBitrate = sum(bitrateListRaw)

        avgBitrate = (self.targetSize * 0.001 * 8192) / totalDuration
        avgBitrate -= self.audioBitrate
        bitrateMult = avgBitrate / totalVidBitrate
        bitrateList = [bitrateRaw * bitrateMult for bitrateRaw in bitrateListRaw]
        
        return bitrateList


def dump_output_video_info(out_video: OutputVideo):
    print(out_video.get_video_path())
    print(f"  Duration: {timedelta(seconds=out_video.get_duration())}")

    # print("  Inputs:")
    for in_video in out_video.inputVideos:
        print("  " + in_video.videoPath)

        for time_range in in_video.timeRanges:
            print(f"    {get_time_diff(time_range[0], time_range[1])}  "
                  f"({time_range[0]} - {str(time_range[1])})")
    print()


# some shitty thing to print what we just parsed
def print_timestamps(video_list: List[OutputVideo]):
    # print(f"{cmd_bar_line}\n  Timestamps File\n{cmd_bar_line}")
    print(f"{cmd_bar_line}\nClip Timestamps (.cts) - {len(video_list)} Total\n")

    for out_video in video_list:
        if out_video.skip:
            set_con_color(Color.GREEN)
            print("SKIPPED:")

        dump_output_video_info(out_video)

        if out_video.skip:
            set_con_color(Color.DEFAULT)

    # print("Default Output Folder: " + out_folder)
    # if final_encode:
    #    print( "Final Encode - Using H265 - CRF 8 - Slow Preset" )
    # else:
    # print( "Encode - Using H264 - CRF 24 - Ultrafast Preset" )
    # print( cmd_bar_line )


class VideoConfig(OutputVideoSettings):
    def __init__(self):
        super().__init__()
        self.configPath = ""
        self.configFolder = ""
        self.configKV = None
        
        self.searchPaths: List[str] = []
        self.videoList: List[OutputVideo] = []
        self.moveFolder: str = ""
    
    def set_config_path(self, path: str) -> bool:
        if path:
            self.configPath = os.path.abspath(path)
        else:
            return False

        self.configFolder = os.path.split(self.configPath)[0]
        return True
        
    def load(self, path: str):
        if not self.set_config_path(path):
            return

        self.configKV: lexer.DemezKeyValueRoot = lexer.ReadFile(self.configPath)
        self.searchPaths.append(self.configFolder)

        if self.configFolder not in ALL_SEARCH_PATHS:
            ALL_SEARCH_PATHS.append(self.configFolder)

        self.parse_config(self.configKV)
        
    def parse_config(self, config: lexer.DemezKeyValueRoot):
        print_color(Color.CYAN, "Parsing Config: " + self.configPath)
    
        for kvBlock in config.value:
            kvBlock: lexer.DemezKeyValue = kvBlock
    
            if kvBlock.key == "$include":
                # prevInputDirStack: List[str] = self.inputDirStack.copy()
                prevConfigPath: str = self.configPath

                include_config: lexer.DemezKeyValueRoot = lexer.ReadFile(kvBlock.value)
                self.set_config_path(os.path.join(self.configFolder, os.path.split(kvBlock.value)[0]))
                # self.inputDirStack.append(self.configFolder)
                self.parse_config(include_config)
                
                # self.inputDirStack = prevInputDirStack.copy()
                self.set_config_path(prevConfigPath)

            elif kvBlock.key == "$moveFolder":
                self.moveFolder = kvBlock.value

            #elif kvBlock.key == "$pushInputDir":
            #    self.inputDirStack.append(kvBlock.value)

            #elif kvBlock.key == "$popInputDir":
            #    self.inputDirStack.pop()
                
            elif kvBlock.key.startswith("$"):
                if not self.parse_setting(self.configFolder, self.searchPaths, kvBlock):
                    print("Unknown setting: " + kvBlock.key)
    
            # is a video file
            else:
                addVideo = True
                if kvBlock.condition:
                    addVideo = False if ARGS.encode else True

                    if not addVideo:
                        # skip the video if we don't want it on this pass
                        if kvBlock.condition == "$RAW$" and ARGS.encode_raw:
                            addVideo = True
                        elif kvBlock.condition == "!$RAW$" and not ARGS.encode_raw:
                            addVideo = True
                
                # TODO: refactor this to store the output videos in two lists, raw and normal
                #  or store it as a fixed tuple size with 0 being normal and 1 being raw, either could be None
                #  this way, you can do both in one pass
                
                if addVideo:
                    outputVideo = OutputVideo(kvBlock.key)
                    outputVideo.copy_settings(self)

                    # outputVideo.hashList.append(get_hash(self.inputDirStack[-1]))
                    outputVideo.hashList.append(get_hash(str(ARGS.encode_raw)))
                    
                    if kvBlock.value:
                        self.parse_output_video(outputVideo, kvBlock)
                        
                    # add a bunch of hashes
                    if ARGS.encode_raw:
                        # outputVideo.hashList.append(get_hash(" ".join(outputVideo.cmdRaw)))
                        outputVideo.hashList.append(get_hash(outputVideo.videoPrefixRaw))
                    else:
                        # outputVideo.hashList.append(get_hash(" ".join(outputVideo.cmd)))
                        outputVideo.hashList.append(get_hash(outputVideo.videoPrefix))
                    
                    for inputVideo in outputVideo.inputVideos:
                        outputVideo.hashList.append(get_hash(inputVideo.videoPath))
                        
                        if ARGS.encode_raw:
                            outputVideo.hashList.append(get_hash(" ".join(inputVideo.cmdRaw)))
                        else:
                            outputVideo.hashList.append(get_hash(" ".join(inputVideo.cmd)))
                            
                        for timeRange in inputVideo.timeRanges:
                            outputVideo.hashList.append(get_hash(str(timeRange)))

                    # Manage Markers from Input Videos
                    last_video_time = timedelta(seconds=0)
                    for video in outputVideo.inputVideos:
                        if video.markers_tmp:
                            for marker_name, base_start, base_end in video.markers_tmp:

                                new_start_time = -1
                                new_end_time = -1
                                # Find what time range this marker is in
                                for time_range in video.timeRanges:
                                    if time_range[0] <= base_start and base_start <= time_range[1]:
                                        # We Found a Valid Time Range, get the offset and add to the main output video
                                        new_start_time = last_video_time + (base_start - time_range[0])

                                    if time_range[0] <= base_end and base_end <= time_range[1]:
                                        # We Found a Valid Time Range, get the offset and add to the main output video
                                        new_end_time = last_video_time + (base_end - time_range[0])

                                    if new_start_time != -1 and new_end_time != -1:
                                        outputVideo.markers.append([marker_name, new_start_time, new_end_time])
                                        break
                                else:
                                    print("Marker not in valid time range!")

                        # Add the duration of this input video to offset it for the next input video
                        last_video_time += timedelta(seconds=video.get_duration())

                    # Finally, Add the Output Video the list of videos to process
                    self.videoList.append(outputVideo)

                    if ARGS.move_files:
                        # print(f"  Output Video: {outputVideo.get_video_name()}")
                        continue

                    elif not check_hash_file(outputVideo, outputVideo.hashList):
                        set_con_color(Color.GREEN)
                        print(f"  Skipping Output: {outputVideo.get_video_name()}")
                        # print(f"  Skipping Output Video:")
                        # dump_output_video_info(outputVideo)
                        set_con_color(Color.DEFAULT)
                        outputVideo.skip = True

                    # elif not os.path.isfile(outputVideo.get_video_path()):
                    else:
                        print(f"  Adding Output:   {outputVideo.get_video_name()}")
                        # dump_output_video_info(outputVideo)
                        continue

                    # elif ARGS.move_files:
                    #     print(f"  Adding Output:   {outputVideo.get_video_name()}")

        print(cmd_bar_line)
        print_color(Color.CYAN, "Total Search Paths:")
        for search_path in ALL_SEARCH_PATHS:
            print_color(Color.CYAN, f"    \"{search_path}\"")

    def get_video_path(self, video_path) -> str:
        if os.path.isabs(video_path):
            if os.path.isfile(video_path):
                return video_path
            else:
                warning(f"absolute path file not found: {video_path}")
                return ""

        for search_path in self.searchPaths:
            new_path = os.path.normpath(search_path + os.sep + video_path)
            if os.path.isfile(new_path):
                if ARGS.move_files:
                    set_con_color(Color.DGREEN)
                    print(f"  Adding File to Move: {new_path}")
                    set_con_color(Color.DEFAULT)
                return new_path
        else:
            warning(f"file not found in search paths: {video_path}")
            return ""

    # why is this like this, and not just a "parse setting" thing?
    def parse_output_video(self, video_file: OutputVideo, block_obj: lexer.DemezKeyValue):
        if block_obj._value_type != list:
            block_obj.Warning("incorrect syntax")
        
        for video_block in block_obj.value:
            video_block.key = os.path.normpath(video_block.key)
        
            if video_block.key == "$time":
                # TODO: maybe if the value is 'self' or $useDateModified, use the input video date modified?
                #  you might not be able to get a date modified due to no input videos being added, oof
                #  and what if there is more than one video?
                if video_block.value:
                    video_file.time = to_datetime(video_block.value)

            elif video_block.key == "$markers":
                if video_block._value_type != list:
                    video_block.Warning("Not a List!!!")
                    continue

                for marker in video_block.value:
                    # TODO: SUPPORT END TIME HERE !!!!!!!
                    video_file.markers.append([marker.key, to_timedelta(marker.value), to_timedelta(marker.value)])
        
            # an output video setting
            elif video_block.key.startswith("$"):
                if not video_file.parse_setting(self.configFolder, self.searchPaths, video_block):
                    print("Unknown setting: " + video_block.key)
        
            # no video name, just timestamps, so then assume the input video is the output video name
            elif ":" in video_block.key and os.sep not in video_block.key:
                video_path = self.get_video_path(video_file.videoName)
                if video_path == "":
                    continue

                input_video = video_file.create_input_video(video_path)
                self.add_video_setting(input_video, video_block)
        
            # input video found
            else:
                if video_block._value_type != list:
                    video_block.Warning("incorrect syntax")

                video_path = self.get_video_path(video_block.key)
                if video_path == "":
                    continue
                    
                input_video = video_file.create_input_video(video_path)  # , False
                for in_video_item in video_block.value:
                    self.add_video_setting(input_video, in_video_item)

    def add_video_setting(self, video_file, in_video_item: lexer.DemezKeyValue):
        if in_video_item.key == "$cmd":
            video_file.cmd.append(in_video_item.value)
            
        elif in_video_item.key == "$cmdRaw":
            video_file.cmdRaw.append(in_video_item.value)

        elif in_video_item.key == "$markers":
            if in_video_item.condition == "$RAW$" and not ARGS.encode_raw:
                return

            elif in_video_item.condition == "!$RAW$" and ARGS.encode_raw:
                return

            elif in_video_item._value_type != list:
                in_video_item.Warning("Not a List!!!")
                return

            for marker in in_video_item.value:
                # TODO: support end time
                video_file.markers_tmp.append([marker.key, to_timedelta(marker.value), to_timedelta(marker.value)])

        else:
            if in_video_item.condition:
                # BLECH
                if in_video_item.condition == "$RAW$" and ARGS.encode_raw:
                    video_file.add_time_range(in_video_item.key, in_video_item.value)

                elif in_video_item.condition == "!$RAW$" and not ARGS.encode_raw:
                    video_file.add_time_range(in_video_item.key, in_video_item.value)
                #else:
                #    in_video_item.Warning("Unknown Condition")

                # Automatically add markers for this
                if in_video_item.condition != "$RAW$" and ARGS.encode_raw:
                    video_file.add_auto_marker(in_video_item.key, in_video_item.value)
            else:
                video_file.add_time_range(in_video_item.key, in_video_item.value)


def get_time_diff(dt_start, dt_end):
    time_difference = dt_end.total_seconds() - dt_start.total_seconds()
    if time_difference <= 0:
        raise Exception("Time difference less than 0: " + str(time_difference))
    return timedelta(seconds=time_difference)


def to_timedelta(timestamp_str: str) -> timedelta:
    if timestamp_str == "":
        return timedelta(seconds=0)
        
    time_split = timestamp_str.split(":")
    time_split.reverse()
    
    total_seconds = float(time_split[0])
    
    for index in range(1, len(time_split)):
        total_seconds += int(time_split[index]) * (60 ** index)
    
    return timedelta(seconds=total_seconds)


def to_datetime(datetime_str: str) -> datetime:
    date, time = datetime_str.split(" ", 1)
    year, month, day = date.split('-')
    hour, minute, second = time.split('-')
    
    return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))


# ==================================================================================================
# Video Encoding
# ==================================================================================================


def check_hash_file(videoFile: OutputVideo, hashList):
    # filename = os.path.basename(videoFile.get_video_name())
    filename = get_hash(videoFile.get_video_path())
    if ARGS.verbose:
        print("Checking Hash: " + filename + ".hash")
    
    video_crc_path = os.path.join(ROOT_FOLDER, "hashes", filename + ".hash")
    
    if os.path.isfile(video_crc_path):
        with open(video_crc_path, mode="r", encoding="utf-8") as file:
            crc_file = file.read().splitlines()
        
        valid_crcs = []
        for video_crc in crc_file:
            if video_crc not in hashList:
                # print("Invalid Hash: " + video_obj.filename + ".crc")
                return True
            else:
                valid_crcs.append(video_crc)
        
        else:
            if valid_crcs != hashList:
                if ARGS.verbose:
                    print("    Not all Hash's validated")
                return True
            return False
    else:
        # print("Hash File does not exist: " + video_crc_path)
        return True


def ffmpeg_line_reader(ffmpeg, outFile: str, max_size: int):
    ffmpeg_output = b""
    while True:
        time.sleep(0.1)
        poll = ffmpeg.poll()
        if poll is not None:
            break

        # lines: List[bytes] = ffmpeg.stdout.readlines()

        for line in ffmpeg.stdout:
            # time.sleep(0.1)
            sys.stdout.write(line.decode())
            ffmpeg_output += line

            if max_size is not None:
                continue

            if b"size=" in line:
                # use the total time range and divide it by ffmpeg's current time in the encode to get a percentage
                current_size = line.split(b"size= ")[1].split(b"kB time=")[0].strip()
                current_bitrate = line.split(b"bitrate=")[1].split(b" speed=")[0].strip()

                current_size = int(current_size.decode())

                if current_bitrate != b"N/A":
                    current_bitrate = int(current_bitrate.decode())
                else:
                    current_bitrate = 0.0

                # print("cur size: " + str(current_size))

        if max_size is not None and os.path.isfile(outFile):
            size = os.path.getsize(outFile)

            if size > max_size:
                ffmpeg.kill()
                print("ASDIHJAIU(WDHJUIO(WDHIUADW")


def run_ffmpeg(outFile: str, cmd: List[str], max_size: int = None):
    # if ARGS.raw_ffmpeg:
    print("\nCommand Line: " + " ".join(cmd) + "\n")

    # if not max_size:
    # subprocess.run(" ".join(cmd))

    # elif max_size:
    # NOTE: bring back when i feel like using the priority and cpu affinity
    if True:
        priority = subprocess.NORMAL_PRIORITY_CLASS

        if ARGS.high:
            priority = subprocess.HIGH_PRIORITY_CLASS
        elif ARGS.below_normal:
            priority = subprocess.BELOW_NORMAL_PRIORITY_CLASS

        ffmpeg = subprocess.Popen(
            " ".join(cmd),
            universal_newlines=True,
            # stdout=subprocess.PIPE,
            # stderr=subprocess.STDOUT,
            # stderr=subprocess.PIPE,
            # stdout=sys.stdout,
            # stderr=sys.stdout,
            # shell=True,  # breaks setting cpu affinity
            creationflags=priority,
        )

        time.sleep(2)

        try:
            # LAZY HACK AHCAIODSW
            if "concat" not in cmd:
                p: psutil.Process = psutil.Process(ffmpeg.pid)
                p.cpu_affinity(CPUS)

                if ARGS.high:
                    p.nice(psutil.HIGH_PRIORITY_CLASS)
                elif ARGS.below_normal:
                    p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)

        except Exception as F:
            # could of closed already so oh well
            print("error getting process with psutil: " + str(F))

        index = 0
        ffmpeg_output = ""

        # p1 = threading.Thread(target=ffmpeg_line_reader, args=(ffmpeg, outFile, max_size))
        # p1.daemon = True
        # p1.start()

        while True:
            poll = ffmpeg.poll()
            if poll is not None:
                break
            time.sleep(0.1)

        # --- do whatever here and then kill process and thread if needed
        # if ffmpeg.poll() is None:  # kill process; will automatically stop thread
        #     ffmpeg.kill()
        #     ffmpeg.wait()

        # if p1 and p1.is_alive():  # wait for thread to finish
        #     p1.join()

        while False:
            time.sleep(0.1)
            # poll = ffmpeg.poll()
            # if poll is not None:
            #     break

            lines = ffmpeg.stdout.read(1)

            for line in lines:
                time.sleep(0.1)
                ffmpeg_output += line

                if max_size is not None:
                    continue

                if "size=" in line:
                    # use the total time range and divide it by ffmpeg's current time in the encode to get a percentage
                    current_size = line.split("size= ")[1].split(" kB time=")[0].strip()
                    current_bitrate = line.split("bitrate=")[1].split(" speed=")[0].strip()

                    current_size = int(current_size)
                    current_bitrate = int(current_bitrate)

                    print("cur size: " + str(current_size))


            if max_size is not None and os.path.isfile(outFile):
                size = os.path.getsize(outFile)

                if size > max_size:
                    ffmpeg.kill()
                    print("ASDIHJAIU(WDHJUIO(WDHIUADW")


    # not working correctly??
    '''
    with subprocess.Popen(
            " ".join(cmd),
            # shell=True,  # breaks setting cpu affinity
            # creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS # set to lower priority
            creationflags=subprocess.HIGH_PRIORITY_CLASS  # set to lower priority
    ) as ffmpeg:
        p: psutil.Process = psutil.Process(ffmpeg.pid)
        # p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        p.nice(psutil.HIGH_PRIORITY_CLASS)
        p.cpu_affinity(CPUS)
    
        # reset current priority
        # p.nice(old_priority)
    '''
    
    # TODO: maybe do a final check for if the video duration is correct?
    if not os.path.isfile(outFile) or os.path.getsize(outFile) == 0:
        # raise Exception("ffmpeg died")
        warning(f"\n\nffmpeg failed on file: {outFile}\n")
        return False
    return True

    '''
    # maybe clean up and enable this old code later?
    else:
        ffmpeg_run = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        
        if total_frames:
            UpdateProgressBar(0.00, 52)  # start it at 0
        
        ffmpeg_output = ''
        for line in ffmpeg_run.stdout:
            ffmpeg_output += line
            if total_frames:
                if "frame=" in line:
                    # use the total time range and divide it by ffmpeg's current time in the encode to get a percentage
                    current_frame = line.split("frame= ")[1].split(" fps=")[0]
                    percentage = GetPercent(int(current_frame), total_frames, 2)
                    UpdateProgressBar(percentage, 52)
                    # TODO: IDEA: replace the progress bar with "\n" once it's done?
        
        if total_frames:
            UpdateProgressBar(100.0, 52)  # usually it finishes before we can catch the last frame
        
        if not os.path.isfile(out_file) or os.path.getsize(out_file) == 0:
            print()
            raise Exception("ffmpeg died - output:\n\n" + ffmpeg_output)
    '''


def gen_common_cmd(outputVideo: OutputVideo, inputVideo: VideoFile, index: int, tempFolder: str, timeRange: list, timeIndex: int):
    timeStart = str(timeRange[0])
    timeEnd = str(timeRange[1])
    
    outputName = f"{index}__{os.path.splitext(inputVideo.videoName)[0]}__{timeIndex}"
    
    if ARGS.encode_raw:
        outputName = "raw_" + outputName + outputVideo.videoExtRaw
    else:
        outputName += outputVideo.videoExt
    
    outputName = tempFolder + outputName

    cmd = ["ffmpeg -y -hide_banner"]

    if timeStart != "0:00:00":
        cmd.append(f"-ss {timeStart}")
    
    if timeEnd != "0:00:00":
        cmd.append(f"-to {timeEnd}")
    
    cmd.append(f"-i \"{inputVideo.videoPath}\"")
    
    if ARGS.encode_raw:
        if inputVideo.cmdRaw:
            # cmd.append(" ".join(inputVideo.cmdRaw))
            cmd.append(inputVideo.cmdRaw[-1])
    elif inputVideo.cmd:
        # cmd.append(" ".join(inputVideo.cmd))
        cmd.append(inputVideo.cmd[-1])

    return outputName, cmd
    
    
def encode_pass(outputVideo: OutputVideo, inputVideo: VideoFile, index: int, tempFolder: str, timeRange: list,
                timeIndex: int, bitrate: float, isPass2: bool):
    outputName, cmd = gen_common_cmd(outputVideo, inputVideo, index, tempFolder, timeRange, timeIndex)

    # http://forum.doom9.org/archive/index.php/t-172614.html
    # only specify bitrate in 2nd pass?
    # cmd.append(f"-b:v {bitrate}k")
    # cmd.append(f"-b:v {bitrate}k -maxrate {bitrate + inputVideo.audioBitrate}k")
    # cmd.append(f"-minrate {bitrate * 1000} -b:v {bitrate * 1000} -maxrate {bitrate + inputVideo.audioBitrate}k")
    cmd.append(f"-minrate {bitrate * 1000} -maxrate {bitrate * 1000} -b:v {bitrate * 1000}")
    # cmd.append(f"-bufsize 700K -minrate {bitrate * 1000} -maxrate {bitrate * 1000} -b:v {bitrate * 1000}")

    if isPass2:
        # cmd.append(f"-b:v {bitrate}k -maxrate {bitrate + inputVideo.audioBitrate}k -b:a {inputVideo.audioBitrate}k -pass 2")
        # cmd.append(f"-b:a {inputVideo.audioBitrate}k -pass 2")
        cmd.append(f"-b:a {inputVideo.audioBitrate}k")
        if inputVideo.cmdPass2:
            cmd.append(" ".join(inputVideo.cmdPass2))
    else:
        cmd.append(f"-an -pass 1")
        if inputVideo.cmdPass1:
            cmd.append(" ".join(inputVideo.cmdPass1))
        
    cmd.append(f'\"{outputName}\"')

    if isPass2:
        # nvm, final bitrate is wildly different, so uh, that's cool
        run_ffmpeg(outputName, cmd, MAX_FILE_SIZE)
        # run_ffmpeg(outputName, cmd)
    else:
        run_ffmpeg(outputName, cmd)

    return outputName


def get_file_bitrate(path: str):
    ffprobe_command = "ffprobe -threads 6 -v error -select_streams v:0 -show_entries format=bit_rate " \
                      "-of default=noprint_wrappers=1:nokey=1 \"" + path + '"'

    output = subprocess.check_output(ffprobe_command, shell=True)

    # clean up the output
    for line in str(output).split("\\n"):
        if line.startswith("b\'"):
            line = line.split("b\'")[1]
        line = line.split("\\r")[0]

        if line.endswith("N/A") or line == '\'':
            continue

        return float(line)

    return 0.0


STATE_NONE = 0
STATE_SMALLER = 1
STATE_BIGGER = 2

# Discord Old 8 MB
# MAX_FILE_SIZE = 8388008
# MIN_FILE_SIZE = 7602176

# https://www.gbmb.org/mb-to-bytes
# Discord 25 MB Update
MAX_FILE_SIZE = 26214400 # 25 MB
MIN_FILE_SIZE = 22020096 # 21 MB
# MIN_FILE_SIZE = 24641536 # 23.5 MB


# 2 Pass Encoding
def encode_input_videos(outputVideo: OutputVideo, inputVideo: VideoFile, index: int, tempFolder: str) -> List[str]:
    if ARGS.encode_raw:
        return encode_input_videos_raw(outputVideo, inputVideo, index, tempFolder)
    
    subVideos = []
    targetBitrates = outputVideo.calc_target_bitrate()

    redo_encode = True
    count = 0
    max_count = 10

    bitrateHistory = [[]] * max_count
    lastState = STATE_NONE
    prevFileSize = 0
    stateChanged = False

    MAX_BITRATE_DEFAULT = 100000000000000000.0
    max_bitrate = [MAX_BITRATE_DEFAULT] * len(targetBitrates)
    min_bitrate = [0.0] * len(targetBitrates)

    # max_bitrate_mult = [1.0] * len(targetBitrates)
    # min_bitrate_mult = [1.0] * len(targetBitrates)
    
    def encode_loop():
        nonlocal redo_encode
        nonlocal count
        nonlocal lastState
        nonlocal stateChanged
        nonlocal prevFileSize

        for timeIndex, timeRange in enumerate(inputVideo.timeRanges):
            inputIndex = outputVideo.get_video_index(inputVideo, timeIndex)
            bitrate = targetBitrates[inputIndex]
            
            if ARGS.encode_2pass:
                outputName = encode_pass(outputVideo, inputVideo, index, tempFolder, timeRange, timeIndex, bitrate, True)
                # outputName = encode_pass(outputVideo, inputVideo, index, tempFolder, timeRange, timeIndex, bitrate, False)
                # encode_pass(outputVideo, inputVideo, index, tempFolder, timeRange, timeIndex, bitrate, True)
                
            else:
                outputName, cmd = gen_common_cmd(outputVideo, inputVideo, index, tempFolder, timeRange, timeIndex)
        
                cmd.append(f"-b:v {bitrate}k -b:a {inputVideo.audioBitrate}k")
                cmd.append(f'\"{outputName}\"')
        
                if not run_ffmpeg(outputName, cmd):
                    subVideos.clear()
                    count = max_count
                    redo_encode = False
                    return
            
            subVideos.append(outputName)
            
        if count >= max_count:
            return

        try:
            totalSize = 0
            ffmpegBitrateList = []
            for video in subVideos:
                if os.path.isfile(video):
                    totalSize += os.path.getsize(video)
                    ffmpegBitrateList.append(get_file_bitrate(video) * 0.001)

        except subprocess.CalledProcessError:
            print("ffprobe failed, skipping")
            count = max_count
            redo_encode = False
            return

        except Exception as F:
            print(f"exception: {F}")
            count = max_count
            redo_encode = False
            return

        if totalSize == prevFileSize:
            print(f"Attempt {count+1}: well shit, the video is the exact same fucking filesize, actually impossible to encode")
            count = max_count
            redo_encode = False
            return

        # https://www.gbmb.org/mb-to-bytes
        # if above (almost) 8 MB or below 7.25 MB, redo the encode, not good enough
        # if totalSize > 8388008 or totalSize < 7340032:
        if totalSize > MAX_FILE_SIZE or totalSize < MIN_FILE_SIZE:
            print()
            smaller = totalSize < MIN_FILE_SIZE
            if smaller:
                print(f"Attempt {count+1}: Output video is smaller than target file size!!!")
            else:
                print(f"Attempt {count+1}: Output video is larger than target file size!!!")

            # stateChanged = False
            if smaller and lastState == STATE_BIGGER:
                stateChanged = True
                print(f"  cool we managed to go from being too big to being too small, god ffmpeg why")
            elif not smaller and lastState == STATE_SMALLER:
                stateChanged = True
                print(f"  cool we managed to go from being too small to being too big, god ffmpeg why")

            # reencode videos at an adjusted bitrate based on what ffmpeg felt like encoding it as cause it's DUMB
            for i, _bitrate in enumerate(targetBitrates):
                ffmpegBitrate = (ffmpegBitrateList[i])

                if not smaller:
                    if max_bitrate[i] > _bitrate:
                        max_bitrate[i] = _bitrate
                        # max_bitrate_mult[i] = bitrateHistory[count-1][1]

                    bitrateDiv = max(ffmpegBitrate, _bitrate)
                    bitrateBase = min(ffmpegBitrate, _bitrate)
                else:
                    if min_bitrate[i] < _bitrate:
                        min_bitrate[i] = _bitrate
                        # min_bitrate_mult[i] = bitrateHistory[count-1][1]

                    bitrateDiv = min(ffmpegBitrate, _bitrate)
                    bitrateBase = max(ffmpegBitrate, _bitrate)

                # could be something like 0.00017052145616207467, so wtf
                # maybe clamp it to 0.1 min?
                bitrateMult = bitrateBase / bitrateDiv

                # TODO: somehow need to make this run in a loop and
                #  make sure the new bitrate isn't lower than a previous bitrate,
                #  and lower than a previous bitrate

                # NOTE: could also just store the original bitrate and only use a multiplier on that, idk

                # TODO: also store filesize - if the filesize of a new video is the same as the previous
                #  just give up i guess lol

                if stateChanged:
                    targetBitrates[i] = (max_bitrate[i] + min_bitrate[i]) / 2.0
                    bitrateHistory[count] = [targetBitrates[i], 1.0]

                    print(f"  INPUT {i}: Trying new target bitrate of {targetBitrates[i]}")
                    print(f"  INPUT {i}: Current Min/Max Bitrates: {min_bitrate[i]}/{max_bitrate[i]}")
                else:
                    if bitrateMult < 0.2:
                        print(f"  INPUT {i}: wait wtf we just calculated a bitrate multiplier below 0.2, clamping to 0.2")
                        bitrateMult = 0.2

                    targetBitrates[i] = _bitrate * bitrateMult

                    bitrateHistory[count] = [targetBitrates[i], bitrateMult]
                    print(f"  INPUT {i}: Trying new target bitrate of {targetBitrates[i]}\n    "
                          f"{_bitrate} * {bitrateMult}  ->  "
                          f"{_bitrate} * ({bitrateBase} / {bitrateDiv})")

                    if bitrateMult < 0.1:
                        print(f"  INPUT {i}: wait wtf we just calculated a bitrate multiplier below 0.1, uh, fuck this")
                        count = max_count
                        redo_encode = False
                        break

                    if bitrateMult > 4:
                        print(f"Bitrate Multiplier is greater than 3, fuck this, it's probably fine")
                        count = max_count
                        redo_encode = False
                        return

                    if targetBitrates[i] <= 0.01:
                        print(f"  INPUT {i}: wtf we just calculated a bitrate of 0.01 or below, uh, fuck this")
                        count = max_count
                        redo_encode = False
                        break

                # if count > 0:
                #     print(f"  INPUT {i}: Previous bitrate: {bitrateHistory[count-1][0]}")

            lastState = STATE_SMALLER if smaller else STATE_BIGGER
            prevFileSize = totalSize
            subVideos.clear()

        else:
            count = max_count
            redo_encode = False

    # don't get stuck in a loop here and only go X times max
    while redo_encode and count < max_count:
        encode_loop()
        count += 1

    if count == max_count:
        print(f"HIT MAX RETRY COUNT OF {max_count}, SKIPPING VIDEO")
    
    return subVideos


def encode_input_videos_raw(outputVideo: OutputVideo, inputVideo: VideoFile, index: int, tempFolder: str) -> List[str]:
    subVideos = []
    
    for timeIndex, timeRange in enumerate(inputVideo.timeRanges):
        outputName, cmd = gen_common_cmd(outputVideo, inputVideo, index, tempFolder, timeRange, timeIndex)
        
        cmd.append(f'\"{outputName}\"')
        
        run_ffmpeg(outputName, cmd)
        subVideos.append(outputName)
    
    return subVideos


def create_output_video(tempFolder: str, subVideoList: List[str], outputVideo: OutputVideo):
    if len(subVideoList) == 0:
        warning("No Input Videos in Output Video, Skipping")
        return
    
    # stuff for ffmpeg concat shit
    concatFile = tempFolder + "concat.txt"
    with open(concatFile, "w", encoding="utf-8") as temp_file_io:
        for sub_video in subVideoList:
            temp_file_io.write("file '" + sub_video + "'\n")

    # outputVideo.write_metadata()
            
    metadata = outputVideo.get_metadata_cmd()

    metadata_inputs = []

    # HACK HACK METADATA INPUT FILE
    index = 0
    while index < len(metadata):
        if metadata[index].startswith("-i"):
            metadata_inputs.append(metadata[index])
            metadata.remove(metadata[index])
        else:
            index += 1

    cmd = [
        "ffmpeg -y -hide_banner",
        "-safe 0 -f concat -i \"" + concatFile + '"',
        *metadata_inputs,
        "-c copy -map 0",
        *metadata,
        f"\"{outputVideo.get_video_path()}\"",
    ]
    
    if not os.path.exists(outputVideo.get_video_dir()):
        os.makedirs(outputVideo.get_video_dir())
    
    if not run_ffmpeg(outputVideo.get_video_path(), cmd):
        os.remove(concatFile)
        return
    
    if outputVideo.dateFile:
        date_created = get_date_created(outputVideo.dateFile)
        date_mod = os.path.getmtime(outputVideo.dateFile)
        date_access = os.path.getatime(outputVideo.dateFile)
    
        set_file_times(outputVideo.get_video_path(), date_created, date_mod, date_access)
        
        if ARGS.verbose:
            print("Set Date Created, Modified, and Accessed")
    
    os.remove(concatFile)
    

def delete_temp_folder(tempFolder: str):
    if ARGS.keep_temp:
        return

    for fileName in os.listdir(tempFolder):
        filePath = os.path.join(tempFolder, fileName)
        try:
            os.unlink(filePath)
        except Exception as e:
            print('Failed to delete %s. Reason: %s' % (filePath, e))
            
            
def write_hash_file(video_name, hashList: List[str]):
    hashPath = os.path.join(ROOT_FOLDER, "hashes")
    if not os.path.exists(hashPath):
        os.makedirs(hashPath)
        
    hashPath += os.sep + video_name + ".hash"
    with open(hashPath, mode="w", encoding="utf-8") as hashFile:
        hashFile.write("\n".join(hashList))
    return


def move_video_check(outputVideo: OutputVideo):
    if VIDEO_CONFIG.moveFolder and ARGS.move_files:
        for index, inputVideo in enumerate(outputVideo.inputVideos):
            # remove from queue
            ALL_INPUT_VIDEOS.remove(inputVideo.videoPath)

            # if video isn't used again later, then we can safely move it
            if inputVideo.videoPath not in ALL_INPUT_VIDEOS:
                move_video(outputVideo, inputVideo)


def run_encoding():
    if not os.path.exists(TEMP_FOLDER):
        os.makedirs(TEMP_FOLDER)
    
    print_timestamps(VIDEO_CONFIG.videoList)
    
    for outputVideo in VIDEO_CONFIG.videoList:
        if outputVideo.skip or not ARGS.encode:
            move_video_check(outputVideo)
            continue

        print(cmd_bar_line)
        print_color(Color.CYAN, f"Output Video: {outputVideo.get_video_path()}")

        tempFolder = TEMP_FOLDER + os.path.splitext(outputVideo.videoName)[0] + os.sep
        if not os.path.exists(tempFolder):
            os.makedirs(tempFolder)
        else:
            # print("Deleting old TEMP Folder: " + tempFolder)
            delete_temp_folder(tempFolder)
        
        subVideoList: List[str] = []
        for index, inputVideo in enumerate(outputVideo.inputVideos):
            print("\nInput: " + inputVideo.videoName)

            subVideoList.extend(encode_input_videos(outputVideo, inputVideo, index, tempFolder))
            
        # now combine all the sub videos together
        create_output_video(tempFolder, subVideoList, outputVideo)
        # print("\nDeleting TEMP Folder: " + tempFolder)

        try:
            if not ARGS.keep_temp:
                delete_temp_folder(tempFolder)  # useless if im doing rmtree below?
                shutil.rmtree(tempFolder)
        except Exception as F:
            print("Failed to delete temp folder - " + str(F))
            
        if len(subVideoList) == 0:
            continue

        # write_hash_file(os.path.basename(outputVideo.get_video_name()), outputVideo.hashList)
        write_hash_file(get_hash(outputVideo.get_video_path()), outputVideo.hashList)
        
        # move inputs to "move" folder
        move_video_check(outputVideo)
        
    print("\nFinished!")
    
    
def move_video(outputVideo: OutputVideo, inputVideo: VideoFile):
    print("\nMoving Input: " + inputVideo.videoPath)
    postFix = os.path.commonprefix([outputVideo.videoDir, inputVideo.videoDir])
    inputNewDir = VIDEO_CONFIG.moveFolder + "/" + inputVideo.videoDir[len(postFix):]
    # if not os.path.isdir(inputNewDir):
    #     os.makedirs(inputNewDir)
    # os.rename(inputVideo.videoPath, inputNewDir + "/" + inputVideo.videoName)
    # shutil.move(inputVideo.videoPath, inputNewDir + "/" + inputVideo.videoName)
    shutil.move(inputVideo.videoPath, VIDEO_CONFIG.moveFolder + "/" + inputVideo.videoName)


# ==================================================================================================
# Other 2
# ==================================================================================================


if __name__ == "__main__":
    ARGS = parse_args()
    VIDEO_CONFIG = VideoConfig()
    VIDEO_CONFIG.load(ARGS.input)
    
    CPUS = list(range(*[int(cpu) for cpu in ARGS.cpus]))
    run_encoding()
