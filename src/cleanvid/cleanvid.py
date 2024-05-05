#!/usr/bin/env python3

import argparse
import base64
import chardet
import codecs
import errno
import json
import os
import shutil
import sys
import re
import pysrt
import delegator
from datetime import datetime
from subliminal import *
from babelfish import Language
from collections import OrderedDict

try:
    from cleanvid.caselessdictionary import CaselessDictionary
except ImportError:
    from caselessdictionary import CaselessDictionary
from itertools import tee

__script_location__ = os.path.dirname(os.path.realpath(__file__))

VIDEO_DEFAULT_PARAMS = '-c:v libx264 -preset slow -crf 22'
AUDIO_DEFAULT_PARAMS = '-c:a aac -ab 224k -ar 44100'
# for downmixing, https://superuser.com/questions/852400 was helpful
AUDIO_DOWNMIX_FILTER = 'pan=stereo|FL=0.8*FC + 0.6*FL + 0.6*BL + 0.5*LFE|FR=0.8*FC + 0.6*FR + 0.6*BR + 0.5*LFE'
SUBTITLE_DEFAULT_LANG = 'eng'
PLEX_AUTO_SKIP_DEFAULT_CONFIG = '{"markers":{},"offsets":{},"tags":{},"allowed":{"users":[],"clients":[],"keys":[]},"blocked":{"users":[],"clients":[],"keys":[]},"clients":{},"mode":{}}'


# thanks https://docs.python.org/3/library/itertools.html#recipes
def pairwise(iterable):
    a, b = tee(iterable)
    next(b, None)
    return zip(a, b)


######## GetFormatAndStreamInfo ###############################################
def GetFormatAndStreamInfo(vidFileSpec):
    result = None
    if os.path.isfile(vidFileSpec):
        ffprobeCmd = "ffprobe -loglevel quiet -print_format json -show_format -show_streams \"" + vidFileSpec + "\""
        ffprobeResult = delegator.run(ffprobeCmd, block=True)
        if ffprobeResult.return_code == 0:
            result = json.loads(ffprobeResult.out)
    return result


######## GetStreamSubtitleMap ###############################################
def GetStreamSubtitleMap(vidFileSpec):
    result = None
    if os.path.isfile(vidFileSpec):
        ffprobeCmd = (
            "ffprobe -loglevel quiet -select_streams s -show_entries stream=index:stream_tags=language -of csv=p=0 \""
            + vidFileSpec
            + "\""
        )
        ffprobeResult = delegator.run(ffprobeCmd, block=True)
        if ffprobeResult.return_code == 0:
            # e.g. for ara and chi, "-map 0:5 -map 0:7" or "-map 0:s:3 -map 0:s:5"
            # 2,eng
            # 3,eng
            # 4,eng
            # 5,ara
            # 6,bul
            # 7,chi
            # 8,cze
            # 9,dan
            result = OrderedDict()
            for l in [x.split(',') for x in ffprobeResult.out.split()]:
                result[int(l[0])] = l[1]
    return result


######## HasAudioMoreThanStereo ###############################################
def HasAudioMoreThanStereo(vidFileSpec):
    result = False
    if os.path.isfile(vidFileSpec):
        ffprobeCmd = (
            "ffprobe -loglevel quiet -select_streams a -show_entries stream=channels -of csv=p=0 \""
            + vidFileSpec
            + "\""
        )
        ffprobeResult = delegator.run(ffprobeCmd, block=True)
        if ffprobeResult.return_code == 0:
            result = any(
                [
                    x
                    for x in [int(''.join([z for z in y if z.isdigit()])) for y in list(set(ffprobeResult.out.split()))]
                    if x > 2
                ]
            )
    return result


######## SplitLanguageIfForced #####################################################
def SplitLanguageIfForced(lang):
    srtLanguageSplit = lang.split(':')
    srtLanguage = srtLanguageSplit[0]
    srtForceIndex = int(srtLanguageSplit[1]) if len(srtLanguageSplit) > 1 else None
    return srtLanguage, srtForceIndex


######## ExtractSubtitles #####################################################
def ExtractSubtitles(vidFileSpec, srtLanguage):
    subFileSpec = ""
    srtLanguage, srtForceIndex = SplitLanguageIfForced(srtLanguage)
    if (streamInfo := GetStreamSubtitleMap(vidFileSpec)) and (
        stream := (
            next(iter([k for k, v in streamInfo.items() if (v == srtLanguage)]), None)
            if not srtForceIndex
            else srtForceIndex
        )
    ):
        subFileParts = os.path.splitext(vidFileSpec)
        subFileSpec = subFileParts[0] + "." + srtLanguage + ".srt"
        ffmpegCmd = (
            "ffmpeg -hide_banner -nostats -loglevel error -y -i \""
            + vidFileSpec
            + f"\" -map 0:{stream} \""
            + subFileSpec
            + "\""
        )
        ffmpegResult = delegator.run(ffmpegCmd, block=True)
        if (ffmpegResult.return_code != 0) or (not os.path.isfile(subFileSpec)):
            subFileSpec = ""
    return subFileSpec


######## GetSubtitles #########################################################
def GetSubtitles(vidFileSpec, srtLanguage, offline=False):
    subFileSpec = ExtractSubtitles(vidFileSpec, srtLanguage)
    if not os.path.isfile(subFileSpec):
        if offline:
            subFileSpec = ""
        else:
            if os.path.isfile(vidFileSpec):
                subFileParts = os.path.splitext(vidFileSpec)
                srtLanguage, srtForceIndex = SplitLanguageIfForced(srtLanguage)
                subFileSpec = subFileParts[0] + "." + str(Language(srtLanguage)) + ".srt"
                if not os.path.isfile(subFileSpec):
                    video = Video.fromname(vidFileSpec)
                    bestSubtitles = download_best_subtitles([video], {Language(srtLanguage)})
                    savedSub = save_subtitles(video, [bestSubtitles[video][0]])

            if subFileSpec and (not os.path.isfile(subFileSpec)):
                subFileSpec = ""

    return subFileSpec


######## UTF8Convert #########################################################
# attempt to convert any text file to UTF-* without BOM and normalize line endings
def UTF8Convert(fileSpec, universalEndline=True):
    # Read from file
    with open(fileSpec, 'rb') as f:
        raw = f.read()

    # Decode
    raw = raw.decode(chardet.detect(raw)['encoding'])

    # Remove windows line endings
    if universalEndline:
        raw = raw.replace('\r\n', '\n')

    # Encode to UTF-8
    raw = raw.encode('utf8')

    # Remove BOM
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw.replace(codecs.BOM_UTF8, '', 1)

    # Write to file
    with open(fileSpec, 'wb') as f:
        f.write(raw)


#################################################################################
class VidCleaner(object):
    inputVidFileSpec = ""
    inputSubsFileSpec = ""
    cleanSubsFileSpec = ""
    edlFileSpec = ""
    jsonFileSpec = ""
    tmpSubsFileSpec = ""
    assSubsFileSpec = ""
    outputVidFileSpec = ""
    swearsFileSpec = ""
    swearsPadMillisec = 0
    embedSubs = False
    fullSubs = False
    subsOnly = False
    edl = False
    hardCode = False
    reEncodeVideo = False
    reEncodeAudio = False
    unalteredVideo = False
    subsLang = SUBTITLE_DEFAULT_LANG
    vParams = VIDEO_DEFAULT_PARAMS
    aParams = AUDIO_DEFAULT_PARAMS
    aDownmix = False
    threadsInput = None
    threadsEncoding = None
    plexAutoSkipJson = ""
    plexAutoSkipId = ""
    swearsMap = CaselessDictionary({})
    muteTimeList = []
    jsonDumpList = None

    ######## init #################################################################

    def __init__(
        self,
        iVidFileSpec,
        iSubsFileSpec,
        oVidFileSpec,
        oSubsFileSpec,
        iSwearsFileSpec,
        swearsPadSec=0.0,
        embedSubs=False,
        fullSubs=False,
        subsOnly=False,
        edl=False,
        jsonDump=False,
        subsLang=SUBTITLE_DEFAULT_LANG,
        reEncodeVideo=False,
        reEncodeAudio=False,
        hardCode=False,
        vParams=VIDEO_DEFAULT_PARAMS,
        aParams=AUDIO_DEFAULT_PARAMS,
        aDownmix=False,
        threadsInput=None,
        threadsEncoding=None,
        plexAutoSkipJson="",
        plexAutoSkipId="",
    ):
        if (iVidFileSpec is not None) and os.path.isfile(iVidFileSpec):
            self.inputVidFileSpec = iVidFileSpec
        else:
            raise IOError(errno.ENOENT, os.strerror(errno.ENOENT), iVidFileSpec)

        if (iSubsFileSpec is not None) and os.path.isfile(iSubsFileSpec):
            self.inputSubsFileSpec = iSubsFileSpec

        if (iSwearsFileSpec is not None) and os.path.isfile(iSwearsFileSpec):
            self.swearsFileSpec = iSwearsFileSpec
        else:
            raise IOError(errno.ENOENT, os.strerror(errno.ENOENT), iSwearsFileSpec)

        if (oVidFileSpec is not None) and (len(oVidFileSpec) > 0):
            self.outputVidFileSpec = oVidFileSpec
            if os.path.isfile(self.outputVidFileSpec):
                os.remove(self.outputVidFileSpec)

        if (oSubsFileSpec is not None) and (len(oSubsFileSpec) > 0):
            self.cleanSubsFileSpec = oSubsFileSpec
            if os.path.isfile(self.cleanSubsFileSpec):
                os.remove(self.cleanSubsFileSpec)

        self.swearsPadMillisec = round(swearsPadSec * 1000.0)
        self.embedSubs = embedSubs
        self.fullSubs = fullSubs
        self.subsOnly = subsOnly or edl or (plexAutoSkipJson and plexAutoSkipId)
        self.edl = edl
        self.jsonDumpList = [] if jsonDump else None
        self.plexAutoSkipJson = plexAutoSkipJson
        self.plexAutoSkipId = plexAutoSkipId
        self.reEncodeVideo = reEncodeVideo
        self.reEncodeAudio = reEncodeAudio
        self.hardCode = hardCode
        self.subsLang = subsLang
        self.vParams = vParams
        self.aParams = aParams
        self.aDownmix = aDownmix
        self.threadsInput = threadsInput
        self.threadsEncoding = threadsEncoding
        if self.vParams.startswith('base64:'):
            self.vParams = base64.b64decode(self.vParams[7:]).decode('utf-8')
        if self.aParams.startswith('base64:'):
            self.aParams = base64.b64decode(self.aParams[7:]).decode('utf-8')

    ######## del ##################################################################
    def __del__(self):
        if (not os.path.isfile(self.outputVidFileSpec)) and (not self.unalteredVideo):
            if os.path.isfile(self.cleanSubsFileSpec):
                os.remove(self.cleanSubsFileSpec)
            if os.path.isfile(self.edlFileSpec):
                os.remove(self.edlFileSpec)
            if os.path.isfile(self.jsonFileSpec):
                os.remove(self.jsonFileSpec)
        if os.path.isfile(self.tmpSubsFileSpec):
            os.remove(self.tmpSubsFileSpec)
        if os.path.isfile(self.assSubsFileSpec):
            os.remove(self.assSubsFileSpec)

    ######## CreateCleanSubAndMuteList #################################################
    def CreateCleanSubAndMuteList(self):
        if (self.inputSubsFileSpec is None) or (not os.path.isfile(self.inputSubsFileSpec)):
            raise IOError(
                errno.ENOENT,
                f"Input subtitle file unspecified or not found ({os.strerror(errno.ENOENT)})",
                self.inputSubsFileSpec,
            )

        subFileParts = os.path.splitext(self.inputSubsFileSpec)

        self.tmpSubsFileSpec = subFileParts[0] + "_utf8" + subFileParts[1]
        shutil.copy2(self.inputSubsFileSpec, self.tmpSubsFileSpec)
        UTF8Convert(self.tmpSubsFileSpec)

        if not self.cleanSubsFileSpec:
            self.cleanSubsFileSpec = subFileParts[0] + "_clean" + subFileParts[1]

        if not self.edlFileSpec:
            cleanSubFileParts = os.path.splitext(self.cleanSubsFileSpec)
            self.edlFileSpec = cleanSubFileParts[0] + '.edl'

        if (self.jsonDumpList is not None) and (not self.jsonFileSpec):
            cleanSubFileParts = os.path.splitext(self.cleanSubsFileSpec)
            self.jsonFileSpec = cleanSubFileParts[0] + '.json'

        lines = []

        with open(self.swearsFileSpec) as f:
            lines = [line.rstrip('\n') for line in f]

        for line in lines:
            lineMap = line.split("|")
            if len(lineMap) > 1:
                self.swearsMap[lineMap[0]] = lineMap[1]
            else:
                self.swearsMap[lineMap[0]] = "*****"

        replacer = re.compile(r'\b(' + '|'.join(self.swearsMap.keys()) + r')\b', re.IGNORECASE)

        subs = pysrt.open(self.tmpSubsFileSpec)
        newSubs = pysrt.SubRipFile()
        newTimestampPairs = []

        # append a dummy sub at the very end so that pairwise can peek and see nothing
        subs.append(
            pysrt.SubRipItem(
                index=len(subs) + 1,
                start=(subs[-1].end.seconds if subs else 0) + 1,
                end=(subs[-1].end.seconds if subs else 0) + 2,
                text='Fin',
            )
        )

        # for each subtitle in the set
        # if text contains profanity...
        # OR if the next text contains profanity and lies within the pad ...
        # OR if the previous text contained profanity and lies within the pad ...
        # then include the subtitle in the new set
        prevNaughtySub = None
        for sub, subPeek in pairwise(subs):
            newText = replacer.sub(lambda x: self.swearsMap[x.group()], sub.text)
            newTextPeek = (
                replacer.sub(lambda x: self.swearsMap[x.group()], subPeek.text) if (subPeek is not None) else None
            )
            # this sub contains profanity, or
            if (
                (newText != sub.text)
                or
                # we have defined a pad, and
                (
                    (self.swearsPadMillisec > 0)
                    and (newTextPeek is not None)
                    and
                    # the next sub contains profanity and is within pad seconds of this one, or
                    (
                        (
                            (newTextPeek != subPeek.text)
                            and ((subPeek.start.ordinal - sub.end.ordinal) <= self.swearsPadMillisec)
                        )
                        or
                        # the previous sub contained profanity and is within pad seconds of this one
                        (
                            (prevNaughtySub is not None)
                            and ((sub.start.ordinal - prevNaughtySub.end.ordinal) <= self.swearsPadMillisec)
                        )
                    )
                )
            ):
                subScrubbed = newText != sub.text
                if subScrubbed and (self.jsonDumpList is not None):
                    self.jsonDumpList.append(
                        {
                            'old': sub.text,
                            'new': newText,
                            'start': str(sub.start),
                            'end': str(sub.end),
                        }
                    )
                newSub = sub
                newSub.text = newText
                newSubs.append(newSub)
                if subScrubbed:
                    prevNaughtySub = sub
                    newTimes = [
                        pysrt.SubRipTime.from_ordinal(sub.start.ordinal - self.swearsPadMillisec).to_time(),
                        pysrt.SubRipTime.from_ordinal(sub.end.ordinal + self.swearsPadMillisec).to_time(),
                    ]
                else:
                    prevNaughtySub = None
                    newTimes = [sub.start.to_time(), sub.end.to_time()]
                newTimestampPairs.append(newTimes)
            else:
                if self.fullSubs:
                    newSubs.append(sub)
                prevNaughtySub = None
        # This will remove any formatting from the subtitles
        newSubs = newSubs.text.strip_style()

        newSubs.save(self.cleanSubsFileSpec)
        if self.jsonDumpList is not None:
            with open(self.jsonFileSpec, "w") as f:
                f.write(
                    json.dumps(
                        {
                            "now": datetime.now().isoformat(),
                            "edits": self.jsonDumpList,
                            "media": {
                                "input": self.inputVidFileSpec,
                                "output": self.outputVidFileSpec,
                                "ffprobe": GetFormatAndStreamInfo(self.inputVidFileSpec),
                            },
                            "subtitles": {
                                "input": self.inputSubsFileSpec,
                                "output": self.cleanSubsFileSpec,
                            },
                        },
                        indent=4,
                    )
                )

        self.muteTimeList = []
        edlLines = []
        plexDict = json.loads(PLEX_AUTO_SKIP_DEFAULT_CONFIG) if self.plexAutoSkipId and self.plexAutoSkipJson else None

        if plexDict:
            plexDict["markers"][self.plexAutoSkipId] = []
            plexDict["mode"][self.plexAutoSkipId] = "volume"

        for timePair, timePairPeek in pairwise(newTimestampPairs):
            lineStart = (
                (timePair[0].hour * 60.0 * 60.0)
                + (timePair[0].minute * 60.0)
                + timePair[0].second
                + (timePair[0].microsecond / 1000000.0)
            )
            lineEnd = (
                (timePair[1].hour * 60.0 * 60.0)
                + (timePair[1].minute * 60.0)
                + timePair[1].second
                + (timePair[1].microsecond / 1000000.0)
            )
            lineStartPeek = (
                (timePairPeek[0].hour * 60.0 * 60.0)
                + (timePairPeek[0].minute * 60.0)
                + timePairPeek[0].second
                + (timePairPeek[0].microsecond / 1000000.0)
            )
            self.muteTimeList.append(
                "afade=enable='between(t,"
                + format(lineStart, '.3f')
                + ","
                + format(lineEnd, '.3f')
                + ")':t=out:st="
                + format(lineStart, '.3f')
                + ":d=10ms"
            )
            self.muteTimeList.append(
                "afade=enable='between(t,"
                + format(lineEnd, '.3f')
                + ","
                + format(lineStartPeek, '.3f')
                + ")':t=in:st="
                + format(lineEnd, '.3f')
                + ":d=10ms"
            )
            if self.edl:
                edlLines.append(f"{format(lineStart, '.1f')}\t{format(lineEnd, '.3f')}\t1")
            if plexDict:
                plexDict["markers"][self.plexAutoSkipId].append(
                    {"start": round(lineStart * 1000.0), "end": round(lineEnd * 1000.0), "mode": "volume"}
                )
        if self.edl and (len(edlLines) > 0):
            with open(self.edlFileSpec, 'w') as edlFile:
                for item in edlLines:
                    edlFile.write(f"{item}\n")
        if plexDict and (len(plexDict["markers"][self.plexAutoSkipId]) > 0):
            json.dump(
                plexDict,
                open(self.plexAutoSkipJson, 'w'),
                indent=4,
            )

    ######## MultiplexCleanVideo ###################################################
    def MultiplexCleanVideo(self):
        # if we're don't *have* to generate a new video file, don't
        # we need to generate a video file if any of the following are true:
        # - we were explicitly asked to re-encode
        # - we are hard-coding (burning) subs
        # - we are embedding a subtitle stream
        # - we are not doing "subs only" or EDL mode and there more than zero mute sections
        if (
            self.reEncodeVideo
            or self.reEncodeAudio
            or self.hardCode
            or self.embedSubs
            or ((not self.subsOnly) and (len(self.muteTimeList) > 0))
        ):
            if self.reEncodeVideo or self.hardCode:
                if self.hardCode and os.path.isfile(self.cleanSubsFileSpec):
                    self.assSubsFileSpec = self.cleanSubsFileSpec + '.ass'
                    subConvCmd = f"ffmpeg -hide_banner -nostats -loglevel error -y -i {self.cleanSubsFileSpec} {self.assSubsFileSpec}"
                    subConvResult = delegator.run(subConvCmd, block=True)
                    if (subConvResult.return_code == 0) and os.path.isfile(self.assSubsFileSpec):
                        videoArgs = f"{self.vParams} -vf \"ass={self.assSubsFileSpec}\""
                    else:
                        print(subConvCmd)
                        print(subConvResult.err)
                        raise ValueError(f'Could not process {self.cleanSubsFileSpec}')
                else:
                    videoArgs = self.vParams
            else:
                videoArgs = "-c:v copy"
            if self.aDownmix and HasAudioMoreThanStereo(self.inputVidFileSpec):
                self.muteTimeList.insert(0, AUDIO_DOWNMIX_FILTER)
            if (not self.subsOnly) and (len(self.muteTimeList) > 0):
                audioFilter = " -af \"" + ",".join(self.muteTimeList) + "\" "
            else:
                audioFilter = " "
            if self.embedSubs and os.path.isfile(self.cleanSubsFileSpec):
                outFileParts = os.path.splitext(self.outputVidFileSpec)
                subsArgs = f" -i \"{self.cleanSubsFileSpec}\" -map 0 -map -0:s -map 1 -c:s {'mov_text' if outFileParts[1] == '.mp4' else 'srt'} -disposition:s:0 default -metadata:s:s:0 language={self.subsLang} "
            else:
                subsArgs = " -sn "
            ffmpegCmd = (
                f"ffmpeg -hide_banner -nostats -loglevel error -y {'' if self.threadsInput is None else ('-threads '+ str(int(self.threadsInput)))} -i \""
                + self.inputVidFileSpec
                + "\""
                + subsArgs
                + videoArgs
                + audioFilter
                + f"{self.aParams} {'' if self.threadsEncoding is None else ('-threads '+ str(int(self.threadsEncoding)))} \""
                + self.outputVidFileSpec
                + "\""
            )
            ffmpegResult = delegator.run(ffmpegCmd, block=True)
            if (ffmpegResult.return_code != 0) or (not os.path.isfile(self.outputVidFileSpec)):
                print(ffmpegCmd)
                print(ffmpegResult.err)
                raise ValueError(f'Could not process {self.inputVidFileSpec}')
        else:
            self.unalteredVideo = True


#################################################################################
def RunCleanvid():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-s',
        '--subs',
        help='.srt subtitle file (will attempt auto-download if unspecified and not --offline)',
        metavar='<srt>',
    )
    parser.add_argument('-i', '--input', required=True, help='input video file', metavar='<input video>')
    parser.add_argument('-o', '--output', help='output video file', metavar='<output video>')
    parser.add_argument(
        '--plex-auto-skip-json',
        help='custom JSON file for PlexAutoSkip (also implies --subs-only)',
        metavar='<output JSON>',
        dest="plexAutoSkipJson",
    )
    parser.add_argument(
        '--plex-auto-skip-id',
        help='content identifier for PlexAutoSkip (also implies --subs-only)',
        metavar='<content identifier>',
        dest="plexAutoSkipId",
    )
    parser.add_argument('--subs-output', help='output subtitle file', metavar='<output srt>', dest="subsOut")
    parser.add_argument(
        '-w',
        '--swears',
        help='text file containing profanity (with optional mapping)',
        default=os.path.join(__script_location__, 'swears.txt'),
        metavar='<profanity file>',
    )
    parser.add_argument(
        '-l',
        '--lang',
        help=f'language for extracting srt from video file or srt download (default is "{SUBTITLE_DEFAULT_LANG}")',
        default=SUBTITLE_DEFAULT_LANG,
        metavar='<language>',
    )
    parser.add_argument(
        '-p', '--pad', help='pad (seconds) around profanity', metavar='<int>', dest="pad", type=float, default=0.0
    )
    parser.add_argument(
        '-e',
        '--embed-subs',
        help='embed subtitles in resulting video file',
        dest='embedSubs',
        action='store_true',
    )
    parser.add_argument(
        '-f',
        '--full-subs',
        help='include all subtitles in output subtitle file (not just scrubbed)',
        dest='fullSubs',
        action='store_true',
    )
    parser.add_argument(
        '--subs-only',
        help='only operate on subtitles (do not alter audio)',
        dest='subsOnly',
        action='store_true',
    )
    parser.add_argument(
        '--offline',
        help="don't attempt to download subtitles",
        dest='offline',
        action='store_true',
    )
    parser.add_argument(
        '--edl',
        help='generate MPlayer EDL file with mute actions (also implies --subs-only)',
        dest='edl',
        action='store_true',
    )
    parser.add_argument(
        '--json',
        help='generate JSON file with muted subtitles and their contents',
        dest='json',
        action='store_true',
    )
    parser.add_argument('--re-encode-video', help='Re-encode video', dest='reEncodeVideo', action='store_true')
    parser.add_argument('--re-encode-audio', help='Re-encode audio', dest='reEncodeAudio', action='store_true')
    parser.add_argument(
        '-b', '--burn', help='Hard-coded subtitles (implies re-encode)', dest='hardCode', action='store_true'
    )
    parser.add_argument(
        '-v',
        '--video-params',
        help='Video parameters for ffmpeg (only if re-encoding)',
        dest='vParams',
        default=VIDEO_DEFAULT_PARAMS,
    )
    parser.add_argument(
        '-a', '--audio-params', help='Audio parameters for ffmpeg', dest='aParams', default=AUDIO_DEFAULT_PARAMS
    )
    parser.add_argument(
        '-d', '--downmix', help='Downmix to stereo (if not already stereo)', dest='aDownmix', action='store_true'
    )
    parser.add_argument(
        '--threads-input',
        help='ffmpeg global options -threads value',
        metavar='<int>',
        dest="threadsInput",
        type=int,
        default=None,
    )
    parser.add_argument(
        '--threads-encoding',
        help='ffmpeg encoding options -threads value',
        metavar='<int>',
        dest="threadsEncoding",
        type=int,
        default=None,
    )
    parser.add_argument(
        '--threads',
        help='ffmpeg -threads value (for both global options and encoding)',
        metavar='<int>',
        dest="threads",
        type=int,
        default=None,
    )
    parser.set_defaults(
        embedSubs=False,
        fullSubs=False,
        subsOnly=False,
        offline=False,
        reEncodeVideo=False,
        reEncodeAudio=False,
        hardCode=False,
        edl=False,
    )
    args = parser.parse_args()

    inFile = args.input
    outFile = args.output
    subsFile = args.subs
    lang = args.lang
    plexFile = args.plexAutoSkipJson
    if inFile:
        inFileParts = os.path.splitext(inFile)
        if not outFile:
            outFile = inFileParts[0] + "_clean" + inFileParts[1]
        if not subsFile:
            subsFile = GetSubtitles(inFile, lang, args.offline)
        if args.plexAutoSkipId and not plexFile:
            plexFile = inFileParts[0] + "_PlexAutoSkip_clean.json"

    if plexFile and not args.plexAutoSkipId:
        raise ValueError(
            f'Content ID must be specified if creating a PlexAutoSkip JSON file (https://github.com/mdhiggins/PlexAutoSkip/wiki/Identifiers)'
        )

    cleaner = VidCleaner(
        inFile,
        subsFile,
        outFile,
        args.subsOut,
        args.swears,
        args.pad,
        args.embedSubs,
        args.fullSubs,
        args.subsOnly,
        args.edl,
        args.json,
        lang,
        args.reEncodeVideo,
        args.reEncodeAudio,
        args.hardCode,
        args.vParams,
        args.aParams,
        args.aDownmix,
        args.threadsInput if args.threadsInput is not None else args.threads,
        args.threadsEncoding if args.threadsEncoding is not None else args.threads,
        plexFile,
        args.plexAutoSkipId,
    )
    cleaner.CreateCleanSubAndMuteList()
    cleaner.MultiplexCleanVideo()


#################################################################################
if __name__ == '__main__':
    RunCleanvid()

#################################################################################
