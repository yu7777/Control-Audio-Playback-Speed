import re
import time
import os, subprocess
import anki.sound
import sys
from threading import Thread
from .Queue import Queue, Empty
from .Queue import Queue
from anki.sound import play
from anki.sound import mplayerQueue, mplayerClear, mplayerEvt
from anki.sound import MplayerMonitor
from anki.hooks import addHook
from aqt.reviewer import Reviewer
from aqt.utils import showInfo
from sys import platform

audio_file = ""
audio_speed = 1.0
audio_replay = False

stdoutQueue = Queue()

def writeAndFlush(bytes):
    if platform == "win32":
        # Windows
        mm = anki.sound.mplayerManager
        if not mm:
            return
        mm.mplayer.stdin.write(bytes+b"\n")
        mm.mplayer.stdin.flush()
    else:
        # Mac
        mm = anki.sound.mpvManager
        try:
            if bytes == b"pause":
                mm.togglePause()
            elif bytes == b"stop":
                mm.clearQueue()
            elif bytes.startswith(b"seek "):
                delta = int(bytes.split()[1])
                mm.seekRelative(delta)
        except anki.mpv.MPVCommandError:
            # attempting to seek while not playing, etc
            pass


def enqueue_output(out, queue):
    for line in iter(out.readline, b''):
        queue.put(line)
    out.close()


def my_keyHandler(key):
    global audio_speed, audio_replay

    if key == "p":
        audio_speed = 1.0
    elif key == "[":
        audio_speed = max(0.1, audio_speed - 0.1)
    elif key == "]":
        audio_speed = min(4.0, audio_speed + 0.1)

    if platform == "win32":
            # Windows
        if key in "p[]":
            if audio_replay:
                play(audio_file)

            elif anki.sound.mplayerManager is not None:
                if anki.sound.mplayerManager.mplayer is not None:
                    anki.sound.mplayerManager.mplayer.stdin.write(b"af_add scaletempo=stride=10:overlap=0.8\n")
                    anki.sound.mplayerManager.mplayer.stdin.write((b"speed_set %f \n" % audio_speed))
                    anki.sound.mplayerManager.mplayer.stdin.flush()
    else:
            # Mac and Linux
        mm = anki.sound.mpvManager
        if key in "[]" or key == "BS":
            if audio_replay:
                play(audio_file)

            elif mm is not None:
                if mm.command is not None:
                    mm.command ("keypress", key)
    # if key == "p":
    #     anki.sound.mplayerManager.mplayer.stdin.write(b"pause\n")
    # elif key == "l":
    #     audio_replay = not audio_replay
    #     if audio_replay:
    #         showInfo("Auto Replay ON")
    #     else:
    #         showInfo("Auto Replay OFF")

    if key == "r":
        anki.sound.mplayerClear = True


def my_runHandler(self):
    #global messageBuff
    global currentlyPlaying
    global audio_speed, audio_replay

    self.mplayer = None
    self.deadPlayers = []

    while 1:
        anki.sound.mplayerEvt.wait()
        anki.sound.mplayerEvt.clear()

        # clearing queue?
        if anki.sound.mplayerClear and self.mplayer:
            try:
                self.mplayer.stdin.write(b"stop\n")
            except:
                # mplayer quit by user (likely video)
                self.deadPlayers.append(self.mplayer)
                self.mplayer = None

        # loop through files to play
        while anki.sound.mplayerQueue:
            # ensure started
            if not self.mplayer:
                my_startProcessHandler(self)
                #self.startProcess()

            # pop a file
            try:
                item = anki.sound.mplayerQueue.pop(0)
            except IndexError:
                # queue was cleared by main thread
                continue
            if anki.sound.mplayerClear:
                anki.sound.mplayerClear = False
                extra = ""
            else:
                extra = " 1"

            cmd = b'loadfile "%s"%s\n' % (item.encode("utf8"), extra.encode("utf8"))
            # cmd = 'loadfile "%s"%s\n' % (item, extra)
            # cmd = cmd.encode('ascii')
            #cmd = ('loadfile "' + item +'" ' + extra + '\n').encode()

            try:
                self.mplayer.stdin.write(cmd)
                self.mplayer.stdin.flush()
            except:
                # mplayer has quit and needs restarting
                self.deadPlayers.append(self.mplayer)
                self.mplayer = None
                my_startProcessHandler(self)
                #self.startProcess()
                self.mplayer.stdin.write(cmd)
                self.mplayer.stdin.flush()

            if abs(audio_speed - 1.0) > 0.01:
                self.mplayer.stdin.write(b"af_add scaletempo=stride=10:overlap=0.8\n")
                self.mplayer.stdin.write(b"speed_set %f \n" % audio_speed)
                self.mplayer.stdin.write(b"seek 0 1\n")
                self.mplayer.stdin.flush()

            # Clear out rest of queue
            extraOutput = True
            while extraOutput:
                try:
                    extraLine = stdoutQueue.get_nowait()
                    #messageBuff += "ExtraLine: " + line
                except Empty:
                    extraOutput = False

            # Wait until the file finished playing before adding the next file
            finishedPlaying = False
            while not finishedPlaying and not anki.sound.mplayerClear:
                # poll stdout for an 'EOF code' message
                try:
                    line = stdoutQueue.get_nowait()
                    #messageBuff += line
                except Empty:
                    # nothing, sleep for a bit
                    finishedPlaying = False
                    time.sleep(0.05)
                else:
                    # check the line
                    #messageBuff += line
                    lineParts = line.decode('utf8').split(':')
                    if lineParts[0] == 'EOF code':
                        finishedPlaying = True

            # Clear out rest of queue
            extraOutput = True
            while extraOutput:
                try:
                    extraLine = stdoutQueue.get_nowait()
                    #messageBuff += "ExtraLine: " + line
                except Empty:
                    extraOutput = False

        # if we feed mplayer too fast it loses files
        time.sleep(0.1)
        # end adding to queue

        # wait() on finished processes. we don't want to block on the
        # wait, so we keep trying each time we're reactivated
        def clean(pl):
            if pl.poll() is not None:
                pl.wait()
                return False
            else:
                showInfo("Clean")
                return True
        self.deadPlayers = [pl for pl in self.deadPlayers if clean(pl)]


def my_startProcessHandler(self):
    try:
        cmd = anki.sound.mplayerCmd + ["-slave", "-idle", '-msglevel', 'all=0:global=6']
        cmd, env = anki.sound._packagedCmd(cmd)
        #showInfo(str(type(cmd)))

        # open up stdout PIPE to check when files are done playing
        self.mplayer = subprocess.Popen(
            cmd, startupinfo=anki.sound.si, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        # setup
        t = Thread(target=enqueue_output, args=(self.mplayer.stdout, stdoutQueue))
        t.daemon = True
        t.start()
    except OSError:
        anki.sound.mplayerEvt.clear()
        raise Exception("Did you install mplayer?")


def addKeys(keys):
    if platform == "win32":
        # Windows
        keys.append(("n", lambda: writeAndFlush(b"pause")))
        keys.append(("m", lambda: writeAndFlush(b"stop")))
        keys.append(("5", lambda: writeAndFlush(b"pause")))
        keys.append(("6", lambda: writeAndFlush(b"seek -5 0")))
        keys.append(("7", lambda: writeAndFlush(b"seek 5 0")))
        keys.append(("8", lambda: writeAndFlush(b"stop")))
        keys.append(("p", lambda: my_keyHandler("p")))
        #keys.append(("[", lambda: writeAndFlush(b"af_add scaletempo=stride=10:overlap=0.8\nspeed_set %f \n "% 0.5)))
        keys.append(("[", lambda: my_keyHandler("[")))
        keys.append(("]", lambda: my_keyHandler("]")))

    else:
        # Mac and linux
        keys.append(("n", lambda: writeAndFlush(b"pause")))
        keys.append(("m", lambda: writeAndFlush(b"stop")))
        keys.append(("5", lambda: writeAndFlush(b"pause")))
        keys.append(("6", lambda: writeAndFlush(b"seek -5 0")))
        keys.append(("7", lambda: writeAndFlush(b"seek 5 0")))
        keys.append(("8", lambda: writeAndFlush(b"stop")))
        keys.append(("p", lambda: my_keyHandler("p")))
        keys.append(("[", lambda: my_keyHandler("[")))
        keys.append(("]", lambda: my_keyHandler("]")))
        keys.append(("backspace", lambda: my_keyHandler("BS")))

MplayerMonitor.run = my_runHandler
MplayerMonitor.startProcess = my_startProcessHandler
addHook("reviewStateShortcuts", addKeys)
