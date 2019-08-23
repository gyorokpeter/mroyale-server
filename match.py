from twisted.internet import reactor
from buffer import Buffer
import os
import json
import random
import util

class Match(object):
    def __init__(self, server, roomName, private, gameMode):
        self.server = server

        self.forceLevel = ""
        self.customLevelData = {}
        self.isLobby = True
        self.world = "lobby"
        self.roomName = roomName
        self.closed = False
        self.private = private
        self.gameMode = gameMode
        self.levelMode = self.gameMode if self.gameMode != "pvp" else "royale"
        self.playing = False
        self.autoStartTimer = None
        self.startTimer = int()
        self.votes = int()
        self.winners = int()
        self.lastId = -1
        self.players = list()
        self.getRandomLevel("lobby", None)

        self.goldFlowerTaken = bool()

    def getRandomLevel(self, type, mode):
        self.world, self.customLevelData = self.server.getRandomLevel(type, mode)

    def getLevel(self, level):
        self.world, self.customLevelData = self.server.getLevel(level)

    def getNextPlayerId(self):
        self.lastId += 1
        return self.lastId

    def addPlayer(self, player):
        self.players.append(player)
        return self.getNextPlayerId()

    def removePlayer(self, player):
        if player not in self.players:
            return
        self.players.remove(player)
        
        if len(self.players) == 0:
            try:
                self.autoStartTimer.cancel()
            except:
                pass
            self.server.removeMatch(self)
            return
        
        if not player.dead and not player.win: # Don't kill podium players
            self.broadBin(0x11, Buffer().writeInt16(player.id)) # KILL_PLAYER_OBJECT

        self.broadPlayerList()

        if player.voted:
            self.votes -= 1
        elif self.server.enableVoteStart and not self.playing and self.votes >= len(self.players) * self.server.voteRateToStart:
            self.start()

    def getPlayer(self, pid):
        for player in self.players:
            if player.id == pid:
                return player
        return None
            
    def getWinners(self):
        self.winners += 1
        return self.winners

    def broadJSON(self, j):
        for player in self.players:
            if not player.loaded:
                continue
            player.sendJSON(j)

    def broadBin(self, code, buff, ignore = None):
        buff = buff.toBytes() if isinstance(buff, Buffer) else buff
        for player in self.players:
            if not player.loaded or (ignore is not None and player.id == ignore):
                continue
            player.sendBin(code, buff)

    def getLoadMsg(self):
        msg = {"game": self.world, "type": "g01"}
        if self.world == "custom":
            msg["levelData"] = json.dumps(self.customLevelData)
        j = {"packets": [msg], "type": "s01"}
        return json.dumps(j).encode('utf-8')

    def broadLoadWorld(self):
        msg = self.getLoadMsg() #only serialize once!
        for player in self.players:
            player.loadWorld(self.world, msg)

    def broadStartTimer(self, time):
        self.startTimer = time * 30
        for player in self.players:
            if not player.loaded:
                continue
            player.setStartTimer(self.startTimer)
        
        if time > 0:
            reactor.callLater(1, self.broadStartTimer, time - 1)
        else:
            self.closed = True

    def broadPlayerList(self):
        if self.closed:
            return # Don't broad player list when in main game
        data = {"packets": [
            {"players": self.getPlayersData(),
             "type": "g12"}
        ], "type": "s01"}
        for player in self.players:
            if not player.loaded:
                continue
            player.sendJSON(data)

    def getPlayersData(self):
        playersData = []
        for player in self.players:
            # We need to include even not loaded players as the remaining player count
            # only updates on the start timer screen
            playersData.append(player.getSimpleData())
        return playersData

    def broadPlayerUpdate(self, player, pktData):
        data = Buffer().writeInt16(player.id).write(pktData).toBytes()
        for p in self.players:
            if not p.loaded or p.id == player.id:
                continue
            if not p.win and (p.level != player.level or p.zone != player.zone):
                continue
            p.sendBin(0x12, data)

    def onPlayerEnter(self, player):
        pass

    def onPlayerReady(self, player):
        if (not self.private or (self.roomName != "" and self.server.enableAutoStartInMultiPrivate)) and not self.playing: # Ensure that the game starts even with fewer players
            if self.autoStartTimer is not None:
                try:
                    self.autoStartTimer.reset(self.server.autoStartTime)
                except:
                    pass
            else:
                self.autoStartTimer = reactor.callLater(self.server.autoStartTime, self.start, True)

        if self.isLobby and self.goldFlowerTaken:
            self.broadBin(0x20, Buffer().writeInt16(-1).writeInt8(0).writeInt8(0).writeInt32(458761).writeInt8(0))

        if self.isLobby or not player.lobbier or self.closed:
            for p in self.players:
                if not p.loaded or p == player:
                    continue
                player.sendBin(0x10, p.serializePlayerObject())
            if self.startTimer != 0 or self.closed:
                player.setStartTimer(self.startTimer)
        self.broadPlayerList()

        if not self.playing:
            if len(self.players) >= self.server.playerCap:
                self.start(True)
            # This is needed because if the votes is sufficient to start but there isn't sufficient players,
            # when someone enters the game, it can make it possible to start the game.
            elif self.server.enableVoteStart and self.votes >= len(self.players) * self.server.voteRateToStart:
                self.start()

    def onPlayerWarp(self, player, level, zone):
        for p in self.players:
            if not p.loaded or p.lastUpdatePkt is None or p.id == player.id:
                continue
            # Tell fellows that the player warped
            if p.level == player.level and p.zone == player.zone:
                p.sendBin(0x12, Buffer().writeInt16(player.id).writeInt8(level).writeInt8(zone).write(player.lastUpdatePkt[2:]))
                continue
            elif p.level != level or p.zone != zone:
                continue
            player.sendBin(0x12, Buffer().writeInt16(p.id).write(p.lastUpdatePkt))

    def voteStart(self):
        self.votes += 1
        if self.server.enableVoteStart and not self.playing and self.votes >= len(self.players) * self.server.voteRateToStart:
            self.start()

    def start(self, forced = False):
        if self.playing or (not forced and len(self.players) < (1 if self.private and self.roomName == "" else self.server.playerMin)): # We need at-least 10 players to start
            return
        self.playing = True
        self.isLobby = False
        
        try:
            self.autoStartTimer.cancel()
        except:
            pass

        if self.forceLevel != "":
            self.getLevel(self.forceLevel)
        else:
            self.getRandomLevel("game", self.levelMode)

        if not self.private:
            reactor.callLater(3, self.broadLoadWorld)
            reactor.callLater(4, self.broadStartTimer, self.server.startTimer)
        else:
            self.broadLoadWorld()
            reactor.callLater(1, self.broadStartTimer, self.server.startTimer)
            
    def validateCustomLevel(self, level):
        lk = json.loads(level)
        util.validateLevel(lk)
        return lk

    def selectLevel(self, level):
        print(level)
        if level == "" or level in self.server.levels:
            self.forceLevel = level
            self.broadLevelSelect()

    def broadLevelSelect(self):
        data = {"type":"gsl", "name":self.forceLevel, "status":"update", "message":""}
        for player in self.players:
            player.sendJSON(data)

    def selectCustomLevel(self, level):
        lk = self.validateCustomLevel(level)
        self.forceLevel = "custom"
        self.customLevelData = lk
        self.broadLevelSelect()
