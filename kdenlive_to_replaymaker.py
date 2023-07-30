import os
import sys
import argparse

import json
from lxml import etree
from datetime import datetime, timedelta
from typing import List

import demez_key_values as dkv


def parse_args() -> argparse.Namespace:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("-i", "--input")
    arg_parser.add_argument("-o", "--output")
    return arg_parser.parse_args()


def get_time_diff(dt_start, dt_end) -> timedelta:
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


def convert_property(item: str):
    if item is None:
        return item  # should i return an empty string?

    if item[0] == "\"" and item[-1] == "\"":
        item = item[1:-1]

    if ":" in item and "/" not in item and "\\" not in item and not '"' in item:
        try:
            return to_timedelta(item)
        except Exception as F:
            # temp error for now to see what is try to convert here
            print(f"Failed to convert to datetime: \"{item}\"")

    if item.startswith("{") or item.startswith("[") and ":" in item:
        try:
            return json.loads(item)
        except Exception as F:
            # uh, very long error lol
            print(f"Failed to convert from JSON: \"{item}\"")

    try:
        if "." in item:
            return float(item)
        else:
            return int(item)
    # except ValueError:
    except Exception as F:
        if item == "true":
            return True
        if item == "false":
            return False

    # no conversion needed
    return item


class Producer:
    def __init__(self, xml):
        self.xml = xml
        self.properties = {}
        self.id = xml.attrib["id"]
        self.timeIn = to_timedelta(xml.attrib["in"])
        self.timeOut = to_timedelta(xml.attrib["out"])
        self.parse_xml()

    def __str__(self):
        return f"id: {self.id} in: {self.timeIn} out: {self.timeOut}"

    def parse_xml(self):
        for root_elem in self.xml:
            if root_elem.tag == "property":
                name = root_elem.attrib["name"]
                attrib_count = len(root_elem.attrib)
                if attrib_count > 1:
                    print("Producer: More than 1 attribute on a property!")
                self.properties[name] = convert_property(root_elem.text)
            else:
                print(f"Producer: Unknown Element: \"{root_elem.tag}\"")


class PlaylistEntry:
    def __init__(self, xml):
        self.xml = xml
        self.properties = {}
        self.producerId = xml.attrib["producer"]
        self.timeIn = to_timedelta(xml.attrib["in"])
        self.timeOut = to_timedelta(xml.attrib["out"])
        self.parse_xml()

    def __str__(self):
        return f"{self.producerId} in: {str(self.timeIn)} out: {str(self.timeOut)} duration: {str(self.get_duration())}"

    def get_duration(self) -> timedelta:
        return get_time_diff(self.timeIn, self.timeOut)

    def parse_xml(self):
        for root_elem in self.xml:
            if root_elem.tag == "property":
                name = root_elem.attrib["name"]
                attrib_count = len(root_elem.attrib)
                if attrib_count > 1:
                    print("PlaylistEntry: More than 1 attribute on a property!")
                self.properties[name] = convert_property(root_elem.text)
            else:
                print(f"PlaylistEntry: Unknown Element: \"{root_elem.tag}\"")


class Playlist:
    def __init__(self, xml):
        self.xml = xml
        self.id = xml.attrib["id"]
        self.properties = {}
        self.entries = []
        self.guides = {}
        self.parse_xml()

    def __str__(self):
        return self.id

    def parse_xml(self):
        for root_elem in self.xml:
            if root_elem.tag == "property":
                name = root_elem.attrib["name"]
                attrib_count = len(root_elem.attrib)
                if attrib_count > 1:
                    print("Playlist: More than 1 attribute on a property!")
                self.properties[name] = convert_property(root_elem.text)

            elif root_elem.tag == "entry":
                self.entries.append(PlaylistEntry(root_elem))
            else:
                print(f"Playlist: Unknown Element: \"{root_elem.tag}\"")


class TractorFilter:
    def __init__(self, xml):
        self.xml = xml
        self.id = xml.attrib["id"]
        self.properties = {}
        self.parse_xml()

    def __str__(self):
        return self.id

    def parse_xml(self):
        for root_elem in self.xml:
            if root_elem.tag == "property":
                name = root_elem.attrib["name"]
                attrib_count = len(root_elem.attrib)
                if attrib_count > 1:
                    print("TractorFilter: More than 1 attribute on a property!")
                self.properties[name] = convert_property(root_elem.text)

            else:
                print(f"TractorFilter: Unknown Element: \"{root_elem.tag}\"")


class Tractor:
    def __init__(self, xml):
        self.xml = xml
        self.id = xml.attrib["id"]
        self.timeIn = to_timedelta(xml.attrib["in"])
        self.timeOut = to_timedelta(xml.attrib["out"])
        self.properties = {}
        self.tracks = []
        self.filters = []
        self.parse_xml()

    def __str__(self):
        return self.id

    def parse_xml(self):
        for root_elem in self.xml:
            if root_elem.tag == "property":
                name = root_elem.attrib["name"]
                attrib_count = len(root_elem.attrib)
                if attrib_count > 1:
                    print("Tractor: More than 1 attribute on a property!")
                self.properties[name] = convert_property(root_elem.text)

            elif root_elem.tag == "track":
                self.tracks.append(root_elem.attrib)

            elif root_elem.tag == "filter":
                self.filters.append(TractorFilter(root_elem))

            else:
                print(f"Tractor: Unknown Element: \"{root_elem.tag}\"")


class Profile:
    def __init__(self, xml):
        self.xml = xml
        self.fps = float(xml.attrib["frame_rate_num"])
        self.properties = {}
        self.parse_xml()

    def parse_xml(self):
        for root_elem in self.xml:
            if root_elem.tag == "property":
                name = root_elem.attrib["name"]
                attrib_count = len(root_elem.attrib)
                if attrib_count > 1:
                    print("Tractor: More than 1 attribute on a property!")
                self.properties[name] = convert_property(root_elem.text)

            else:
                print(f"Tractor: Unknown Element: \"{root_elem.tag}\"")


producers: List[Producer] = []
playlists: List[Playlist] = []
tractors: List[Tractor] = []


def get_producer(id: str) -> Producer:
    for producer in producers:
        if producer.id == id:
            return producer
    return None


def create_input_video():
    pass


def main():
    if not os.path.isfile(ARGS.input):
        print(f"File does not exist: \"{ARGS.input}\"")
        return

    try:
        with open(ARGS.input, "rb") as project:
            kdenlive = etree.fromstring(project.read())
    except Exception as F:
        print(F)

    # root = kdenlive.getroot()

    # producers_xml: List[etree.Element] = kdenlive.findall("producer")
    # playlists_xml: List[etree.Element] = kdenlive.findall("playlist")

    # for playlist_xml in playlists_xml:
    #     playlists.append(Playlist(playlist_xml))
    profile = None

    for root_elem in kdenlive:
        if root_elem.tag == "producer":
            producers.append(Producer(root_elem))
        elif root_elem.tag == "playlist":
            playlists.append(Playlist(root_elem))
        elif root_elem.tag == "tractor":
            tractors.append(Tractor(root_elem))
        elif root_elem.tag == "profile":
            profile = Profile(root_elem)
        else:
            print(f"Unknown Element: \"{root_elem.tag}\"")

    # build replay maker format now
    root: dkv.DemezKeyValueRoot = dkv.DemezKeyValueRoot()
    out_video = root.AddItem("TEST_REPLAYMAKER", [])
    prev_name = ""
    in_video = None

    for entry in playlists[1].entries:
        entry: PlaylistEntry = entry
        producer = get_producer(entry.producerId)
        if producer is None:
            print(f"null producer in entry?: {entry.producerId}")
            continue

        # check to see if input video was just used, don't make another
        name = producer.properties["resource"]
        if prev_name != name or in_video is None:
            in_video = out_video.AddItem(name, [])
            prev_name = name

        in_video.AddItem(str(entry.timeIn), str(entry.timeOut))

    if "kdenlive:docproperties.guides" in playlists[0].properties:
        guides = playlists[0].properties["kdenlive:docproperties.guides"]
        markers = out_video.AddItem("$markers", [])

        for guide in guides:
            guide_name = guide["comment"]
            guide_time = timedelta(seconds=float(guide["pos"] / profile.fps))
            markers.AddItem(guide_name, str(guide_time))

    # now write to file
    with open(ARGS.output, "w") as out_file:
        out_file.write(root.ToString())

    print("end")


if __name__ == "__main__":
    ARGS = parse_args()
    main()

