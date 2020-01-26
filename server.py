import os
import sys
import datastore
import util
import objgraph

if sys.version_info.major != 3:
    sys.stderr.write("You need python 3.7 or later to run this script\n")
    if os.name == 'nt': # Enforce that the window opens in windows
        print("Press ENTER to exit")
        input()
    exit(1)

from twisted.python import log
from twisted.internet import task
log.startLogging(sys.stdout)

from autobahn.twisted import install_reactor
# we use an Autobahn utility to import the "best" available Twisted reactor
reactor = install_reactor(verbose=False,
                          require_optimal_reactor=False)

try:
    from discord_webhook import DiscordWebhook
    DWH_IMPORT = True
except Exception as e:
    print("Can't import discord_webhook, discord functioning will be disabled.")
    DWH_IMPORT = False

try:
    from captcha.image import ImageCaptcha
    CP_IMPORT = True
except:
    print("Can't import captcha, captcha functioning will be disabled.")
    CP_IMPORT = False

from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory
from twisted.internet.protocol import Factory
import json
import jsonschema
import string
import random
import base64
import hashlib
import traceback
import configparser
from io import BytesIO
from buffer import Buffer
from player import Player
from match import Match

NUM_GM = 3

class MyServerProtocol(WebSocketServerProtocol):
    def __init__(self, server):
        WebSocketServerProtocol.__init__(self)

        self.server = server
        self.address = str()
        self.recv = bytearray()

        self.pendingStat = None
        self.stat = str()
        self.username = str()
        self.session = str()
        self.player = None
        self.blocked = bool()
        self.account = {}
        self.accountPriv = {}

        self.dcTimer = None
        #self.maxConLifeTimer = None
        self.dbSession = datastore.getDbSession()

    def startDCTimerIndependent(self, time):
        reactor.callLater(time, self.sendClose2)

    def startDCTimer(self, time):
        self.stopDCTimer()
        self.dcTimer = reactor.callLater(time, self.sendClose2)

    def sendClose2(self):
        #this is a wrapper for debugging only
        #print("sendClose2")
        #self.crash()
        self.sendClose()

    def stopDCTimer(self):
        try:
            self.dcTimer.cancel()
        except:
            pass

    def onConnect(self, request):
        #print("Client connecting: {0}".format(request.peer))

        if "x-real-ip" in request.headers:
            self.address = request.headers["x-real-ip"]

    def onOpen(self):
        #print("WebSocket connection open.")

        if not self.address:
            self.address = self.transport.getPeer().host

        # A connection can only be alive for 20 minutes
        #self.maxConLifeTimer = reactor.callLater(20 * 60, self.sendClose2)
 
        self.startDCTimer(25)
        self.setState("l")

    def onClose(self, wasClean, code, reason):
        #print("WebSocket connection closed: {0}".format(reason))

        #try:
        #    self.maxConLifeTimer.cancel()
        #except:
        #    pass
        self.stopDCTimer()

        if self.address in self.server.captchas:
            del self.server.captchas[self.address]

        if self.username != "" and self.username in self.server.authd:
            self.server.authd.remove(self.username)

        if self.stat == "g" and self.player != None:
            if self.username != "":
                changed={}
                if not self.player.match.private:
                    if self.player.wins > 0:
                        changed["wins"] = self.player.wins
                    if self.player.deaths > 0:
                        changed["deaths"] = self.player.deaths
                    if self.player.kills > 0:
                        changed["kills"] = self.player.kills
                    if self.player.coins != 0:
                        changed["coins"] = self.player.coins
                if self.blocked:
                    changed["isBanned"] = True
                if self.player.forceRenamed:
                    changed["nickname"] = self.player.name
                    changed["squad"] = self.player.team
                if 0<len(changed):
                    datastore.updateStats(self.dbSession, self.accountPriv["id"], changed)
            self.server.players.remove(self.player)
            self.player.match.removePlayer(self.player)
            self.player.match = None
            self.player = None
            self.pendingStat = None
            self.stat = str()
        self.dbSession.close()

    def onMessage(self, payload, isBinary):
        if len(payload) == 0:
            return

        self.server.in_messages += 1

        try:
            if isBinary:
                self.recv += payload
                while len(self.recv) > 0:
                    if not self.onBinaryMessage():
                        break
            else:
                self.onTextMessage(payload.decode('utf8'))
        except Exception as e:
            traceback.print_exc()
            self.sendClose2()
            self.recv.clear()
            return

    def sendJSON(self, j):
        self.server.out_messages += 1
        #print("sendJSON: "+str(j))
        self.sendMessage(json.dumps(j).encode('utf-8'), False)

    def sendText(self, t):
        self.server.out_messages += 1
        self.sendMessage(t, False)

    def sendBin(self, code, buff):
        self.server.out_messages += 1
        msg=Buffer().writeInt8(code).write(buff.toBytes() if isinstance(buff, Buffer) else buff).toBytes()
        #print("sendBin: "+str(code)+" "+str(msg))
        self.sendMessage(msg, True)

    def loginSuccess(self):
        self.sendJSON({"packets": [
            {"name": self.player.name, "team": self.player.team, "type": "l01", "skin": self.player.skin}
        ], "type": "s01"})
    
    def setState(self, state):
        self.stat = self.pendingStat = state
        self.sendJSON({"packets": [
            {"state": state, "type": "s00"}
        ], "type": "s01"})

    def exception(self, message):
        self.sendJSON({"packets": [
            {"message": message, "type": "x00"}
        ], "type": "s01"})

    def block(self, reason):
        if self.blocked or len(self.player.match.players) == 1:
            return
        print("Player blocked: {0}".format(self.player.name))
        self.blocked = True
        if not self.player.dead:
            self.player.match.broadBin(0x11, Buffer().writeInt16(self.player.id), self.player.id) # KILL_PLAYER_OBJECT
        self.server.blockAddress(self.address, self.player.name, reason)

    def onTextMessage(self, payload):
        #print("Text message received: {0}".format(payload))
        packet = json.loads(payload)
        type = packet["type"]

        if self.stat == "l":
            if type == "l00": # Input state ready
                if self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return
                self.pendingStat = None
                self.stopDCTimer()

                if self.address != "127.0.0.1" and self.server.getPlayerCountByAddress(self.address) >= self.server.maxSimulIP:
                    self.sendClose2()
                    return

                if self.server.shuttingDown:
                    self.setState("g") # Ingame
                    return
                for b in self.server.blocked:
                    if b[0] == self.address:
                        self.blocked = True
                        self.setState("g") # Ingame
                        return
                if self.username != "":
                    if self.accountPriv["isBanned"]:
                        self.blocked = True
                        self.setState("g") # Ingame
                        return

                name = packet["name"]
                team = packet["team"][:3].strip().upper()
                priv = packet["private"] if "private" in packet else False
                skin = int(packet["skin"]) if "skin" in packet else 0
                if not self.account and self.server.restrictPublicSkins and 0<len(self.server.guestSkins):
                    if not skin in self.server.guestSkins:
                        skin = self.server.guestSkins[0]
                gm = int(packet["gm"]) if "gm" in packet else 0
                gm = gm if gm in range(NUM_GM) else 0
                gm = ["royale", "pvp", "hell"][gm]
                isDev = self.account["isDev"] if "isDev" in self.account else False
                self.player = Player(self,
                                     name,
                                     (team if (team != "" or priv) else self.server.defaultTeam).lower(),
                                     self.server.getMatch(team, priv, gm),
                                     skin if skin in range(self.server.skinCount) else 0,
                                     gm,
                                     isDev)
                #if priv:
                #    self.maxConLifeTimer.cancel()
                self.loginSuccess()
                self.server.players.append(self.player)
                
                self.setState("g") # Ingame

            elif type == "llg": #login
                if self.username != "" or self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return
                self.stopDCTimer()
                
                username = packet["username"].upper()
                if self.address in self.server.loginBlocked:
                    self.sendJSON({"type": "llg", "status": False, "msg": "max login tries reached.\ntry again in one minute."})
                    return
                elif username in self.server.authd:
                    self.sendJSON({"type": "llg", "status": False, "msg": "account already in use"})
                    return

                status, msg, self.accountPriv = datastore.login(self.dbSession, username, packet["password"])

                j = {"type": "llg", "status": status, "msg": msg}
                if status:
                    self.account = msg
                    j["username"] = self.username = username
                    self.session = msg["session"]
                    self.server.authd.append(self.username)
                else:
                    if self.address not in self.server.maxLoginTries:
                        self.server.maxLoginTries[self.address] = 1
                    else:
                        self.server.maxLoginTries[self.address] += 1
                        if self.server.maxLoginTries[self.address] >= 4:
                            del self.server.maxLoginTries[self.address]
                            self.server.loginBlocked.append(self.address)
                            reactor.callLater(60, self.server.loginBlocked.remove, self.address)
                self.sendJSON(j)

            elif type == "llo": #logout
                if self.username == "" or self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return
                
                datastore.logout(self.dbSession, self.session)
                self.sendJSON({"type": "llo"})

            elif type == "lrg": #register
                if self.username != "" or self.address not in self.server.captchas or self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return
                self.stopDCTimer()
                
                username = packet["username"].upper()
                if CP_IMPORT and len(packet["captcha"]) != 5:
                    status, msg = False, "invalid captcha"
                elif CP_IMPORT and packet["captcha"].upper() != self.server.captchas[self.address]:
                    status, msg = False, "incorrect captcha"
                elif util.checkCurse(username):
                    status, msg = False, "please choose a different username"
                else:
                    status, msg, self.accountPriv = datastore.register(self.dbSession, username, packet["password"])

                if status:
                    del self.server.captchas[self.address]
                    self.account = msg
                    self.username = username
                    self.session = msg["session"]
                    self.server.authd.append(self.username)
                self.sendJSON({"type": "lrg", "status": status, "msg": msg})

            elif type == "lrc": #request captcha
                if self.username != "" or self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return
                if not CP_IMPORT:
                    self.server.captchas[self.address] = ""
                    self.sendJSON({"type": "lrc", "data": ""})
                    return
                self.stopDCTimer()

                cp = ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(5))
                self.server.captchas[self.address] = cp
                
                imageCaptcha = ImageCaptcha()
                image = imageCaptcha.generate_image(cp)
                
                imgByteArr = BytesIO()
                image.save(imgByteArr, format='PNG')
                imgByteArr = imgByteArr.getvalue()
                
                self.sendJSON({"type": "lrc", "data": base64.b64encode(imgByteArr).decode("utf-8")})
                

            elif type == "lrs": #resume session
                if self.username != "" or self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return
                self.stopDCTimer()
                
                status, msg, self.accountPriv = datastore.resumeSession(self.dbSession, packet["session"])

                j = {"type": "lrs", "status": status, "msg": msg}
                if status:
                    if msg["username"] in self.server.authd:
                        self.sendJSON({"type": "lrs", "status": False, "msg": "account already in use"})
                        return
                    j["username"] = self.username = msg["username"]
                    self.account = msg
                    self.session = msg["session"]
                    self.server.authd.append(self.username)
                self.sendJSON(j)

            elif type == "lpr": #update profile
                if self.username == "" or self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return
                
                res = datastore.updateAccount(self.dbSession, self.username, packet)
                j = {"type": "lpr", "status":res[0], "changes":res[1], "msg":res[2]}
                self.sendJSON(j)

            elif type == "lpc": #password change
                if self.username == "" or self.player is not None or self.pendingStat is None:
                    self.sendClose2()
                    return

                datastore.changePassword(self.dbSession, self.username, packet["password"])

        elif self.stat == "g":
            if type == "g00": # Ingame state ready
                if self.player is None or self.pendingStat is None:
                    if self.server.shuttingDown:
                        levelName, levelData = self.server.getRandomLevel("maintenance", None)
                        self.sendJSON({"packets": [{"game": levelName, "levelData": json.dumps(levelData), "type": "g01"}], "type": "s01"})
                        return
                    if self.blocked:
                        levelName, levelData = self.server.getRandomLevel("jail", None)
                        self.sendJSON({"packets": [{"game": levelName, "levelData": json.dumps(levelData), "type": "g01"}], "type": "s01"})
                        return
                    self.sendClose2()
                    return
                self.pendingStat = None
                
                self.player.onEnterIngame()

            elif type == "g03": # World load completed
                if self.player is None:
                    if self.blocked or self.server.shuttingDown:
                        self.sendBin(0x02, Buffer().writeInt16(0).writeInt16(0).writeInt8(0))
                        #self.startDCTimer(15)
                        return
                    self.sendClose2()
                    return
                self.player.onLoadComplete()

            elif type == "g50": # Vote to start
                if self.player is None or self.player.voted or self.player.match.playing:
                    return
                
                self.player.voted = True
                self.player.match.voteStart()

            elif type == "g51": # (SPECIAL) Force start
                if self.server.mcode and self.server.mcode in packet["code"]:
                    self.player.match.start(True)
            
            elif type == "gsl":  # Level select
                if self.player is None or ((not self.server.enableLevelSelectInMultiPrivate and self.player.team != "") or not self.player.match.private) and not self.player.isDev:
                    return
                
                levelName = packet["name"]
                if levelName == "custom":
                    try:
                        self.player.match.selectCustomLevel(packet["data"])
                    except Exception as e:
                        estr = str(e)
                        estr = "\n".join(estr.split("\n")[:10])
                        self.sendJSON({"type":"gsl","name":levelName,"status":"error","message":estr})
                        return
                    
                    self.sendJSON({"type":"gsl","name":levelName,"status":"success","message":""})
                else:
                    self.player.match.selectLevel(levelName)
            elif type == "gbn":  # ban player
                if not self.account["isDev"]:
                    self.sendClose2()
                pid = packet["pid"]
                ban = packet["ban"]
                self.player.match.banPlayer(pid, ban)
            elif type == "gnm":  # rename player
                if not self.account["isDev"]:
                    self.sendClose2()
                pid = packet["pid"]
                newName = packet["name"]
                self.player.match.renamePlayer(pid, newName)
            elif type == "gsq":  # resquad player
                if not self.account["isDev"]:
                    self.sendClose2()
                pid = packet["pid"]
                newName = packet["name"].lower()
                if len(newName)>3:
                    newName = newName[:3]
                self.player.match.resquadPlayer(pid, newName)
            else:
                print("unknown message! "+payload)

    def onBinaryMessage(self):
        pktLenDict = { 0x10: 6, 0x11: 0, 0x12: 12, 0x13: 1, 0x17: 2, 0x18: 4, 0x19: 0, 0x20: 7, 0x30: 7 }

        code = self.recv[0]
        if code not in pktLenDict:
            #print("Unknown binary message received: {1} = {0}".format(repr(self.recv[1:]), hex(code)))
            self.recv.clear()
            return False
            
        pktLen = pktLenDict[code] + 1
        if len(self.recv) < pktLen:
            return False
        
        b = Buffer(self.recv[1:pktLen])
        del self.recv[:pktLen]
        
        if self.player is None or not self.player.loaded or self.blocked or (not self.player.match.closed and self.player.match.playing):
            self.recv.clear()
            return False

        #print("Binary message received: code="+str(code)+", content:"+",".join([str(x) for x in b.toBytes()]));
        self.player.handlePkt(code, b, b.toBytes())
        return True

class MyServerFactory(WebSocketServerFactory):

    def __init__(self, url):
        self.configFilePath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.cfg")
        self.blockedFilePath = os.path.join(os.path.dirname(os.path.abspath(__file__)),"blocked.json")
        self.levelsPath = os.path.join(os.path.dirname(os.path.abspath(__file__)),"levels")
        self.shutdownFilePath = os.path.join(os.path.dirname(os.path.abspath(__file__)),"shutdown")
        if not os.path.isdir(self.levelsPath):
            self.levelsPath = ""
        self.fileHash = {}
        self.levels = {}
        self.guestSkins = []
        self.ownLevels = False
        self.shuttingDown = False
        if not self.tryReloadFile(self.configFilePath, self.readConfig):
            sys.stderr.write("The file \"server.cfg\" does not exist or is invalid, consider renaming \"server.cfg.example\" to \"server.cfg\".\n")
            if os.name == 'nt': # Enforce that the window opens in windows
                print("Press ENTER to exit")
                input()
            exit(1)
        if self.levelsPath:
            self.ownLevels = True
            self.reloadLevels()
        if self.assetsMetadataPath:
            self.tryReloadFile(self.assetsMetadataPath, self.readAssetsMetadata)
        if self.mysqlHost:
            datastore.checkDb(self.mysqlHost, self.mysqlPort, self.mysqlUser, self.mysqlPass, self.mysqlDB)

        WebSocketServerFactory.__init__(self, url.format(self.listenPort))

        self.players = list()
        self.matches = list()
        
        self.blocked = list()
        try:
            with open(self.blockedFilePath, "r") as f:
                self.blocked = json.loads(f.read())
        except:
            pass

        if DWH_IMPORT:
            self.discordWebhook = DiscordWebhook(url=self.discordWebhookUrl)
        else:
            self.discordWebhook = None

        self.randomWorldList = dict()

        self.maxLoginTries = {}
        self.loginBlocked = []
        self.captchas = {}
        self.authd = []

        self.in_messages = 0
        self.out_messages = 0

        reactor.callLater(5, self.generalUpdate)

        l = task.LoopingCall(self.updateLeaderBoard)
        l.start(60.0)

    def updateLeaderBoard(self):
        if self.leaderBoardPath != '':
            print("updating leader board at "+self.leaderBoardPath)
            leaderBoard = datastore.getLeaderBoard()
            with open(self.leaderBoardPath, "w") as f:
                f.write(json.dumps(leaderBoard))
        if self.debugMemoryLeak:
            objgraph.show_growth(limit=50)
            [objgraph.show_backrefs(x,filename="debug/refs"+str(i)+".dot") for i,x in enumerate(objgraph.by_type("Match"))]

    def reloadLevel(self, level):
        fullPath = os.path.join(self.levelsPath, level)
        try:
            with open(fullPath, "r", encoding="utf-8-sig") as f:
                content = f.read()
                lk = json.loads(content)
                util.validateLevel(lk)
                lk["mtime"] = os.stat(fullPath).st_mtime
                isNew = not level in self.levels
                self.levels[level] = lk
                print(level+" "+ ("loaded" if isNew else "reloaded") +".")
        except:
            print("error while loading "+level+":")
            raise


    def reloadLevels(self):
        files = os.listdir(self.levelsPath)
        files.sort()
        deletedLevels = set(self.levels.keys())-set(files)
        for f in deletedLevels:
            del self.levels[f]
            print(f+" deleted")
        newLevels = set(files)-set(self.levels.keys())
        for f in self.levels.keys(): #we do this first so levels only contains the still-existing levels
            oldMt = self.levels[f]["mtime"]
            newMt = os.stat(os.path.join(self.levelsPath, f)).st_mtime
            if (newMt>oldMt):
                self.reloadLevel(f)
        for f in newLevels:
            self.reloadLevel(f)

    def tryReloadFile(self, fn, callback):
        try:
            with open(fn, "r") as f:
                cfgHash = hashlib.md5(f.read().encode('utf-8')).hexdigest()
                if not fn in self.fileHash or cfgHash != self.fileHash[fn]:
                    self.fileHash[fn] = cfgHash
                    callback()
                    print(fn+" loaded.")
            return True
        except Exception as e:
            print("Failed to load "+fn)
            traceback.print_exc()
            return False

    def readAssetsMetadata(self):
        with open(self.assetsMetadataPath, "r") as f:
            meta = json.loads(f.read())
            self.skinCount = meta["skins"]["count"]
            self.guestSkins=[x["id"] for x in meta["skins"]["properties"] if "forGuests" in x and x["forGuests"]]

    def readConfig(self):
        config = configparser.ConfigParser()
        config.read('server.cfg')

        self.listenPort = config.getint('Server', 'ListenPort')
        self.mcode = config.get('Server', 'MCode').strip()
        self.statusPath = config.get('Server', 'StatusPath').strip()
        self.leaderBoardPath = config.get('Server', 'LeaderBoardPath', fallback='').strip()
        self.assetsMetadataPath = config.get('Server', 'AssetsMetadataPath').strip()
        self.defaultName = config.get('Server', 'DefaultName').strip()
        self.defaultTeam = config.get('Server', 'DefaultTeam').strip()
        self.maxSimulIP = config.getint('Server', 'MaxSimulIP')
        if not self.assetsMetadataPath:
            self.skinCount = config.getint('Server', 'SkinCount')
        self.discordWebhookUrl = config.get('Server', 'DiscordWebhookUrl').strip()
        self.mysqlHost = config.get('Server', 'MySqlHost')
        self.mysqlPort = config.getint('Server', 'MySqlPort')
        self.mysqlUser = config.get('Server', 'MySqlUser')
        self.mysqlPass = config.get('Server', 'MySqlPass')
        self.mysqlDB = config.get('Server', 'MySqlDB')
        self.debugMemoryLeak = config.getint('Server', 'debugMemoryLeak', fallback=0)
        self.restrictPublicSkins = config.getboolean('Server', 'restrictPublicSkins', fallback=False)
        self.banPowerUpInLobby = config.getboolean('Server', 'banPowerUpInLobby', fallback=False)
        if self.debugMemoryLeak:
            if not os.path.exists("debug"):
                os.mkdir("debug")

        self.playerMin = config.getint('Match', 'PlayerMin')
        try:
            oldCap = self.playerCap
        except:
            oldCap = 0
        self.playerCap = config.getint('Match', 'PlayerCap')
        if self.playerCap < oldCap:
            try:
                for match in self.matches:
                    if len(match.players) >= self.playerCap:
                        match.start()
            except:
                print("Couldn't start matches after player cap change...")
        self.autoStartTime = config.getint('Match', 'AutoStartTime')
        self.startTimer = config.getint('Match', 'StartTimer')
        self.enableAutoStartInMultiPrivate = config.getboolean('Match', 'EnableAutoStartInMultiPrivate')
        self.enableLevelSelectInMultiPrivate = config.getboolean('Match', 'EnableLevelSelectInMultiPrivate')
        self.enableVoteStart = config.getboolean('Match', 'EnableVoteStart')
        self.voteRateToStart = config.getfloat('Match', 'VoteRateToStart')
        self.allowLateEnter = config.getboolean('Match', 'AllowLateEnter')
        self.coinRewardFlagpole = config.getint('Match', 'coinRewardFlagpole', fallback=500)
        self.coinRewardPodium1 = config.getint('Match', 'coinRewardPodium1', fallback=200)
        self.coinRewardPodium2 = config.getint('Match', 'coinRewardPodium2', fallback=100)
        self.coinRewardPodium3 = config.getint('Match', 'coinRewardPodium3', fallback=50)
        if not self.levelsPath:
            self.worlds = config.get('Match', 'Worlds').strip().split(',')
            self.worldsPvP = config.get('Match', 'WorldsPVP').strip()
            if len(self.worldsPvP) == 0:
                self.worldsPvP = list(self.worlds)
            else:
                self.worldsPvP = self.worldsPvP.split(',')
            self.worldsHell = config.get('Match', 'WorldsHell').strip()
            if len(self.worldsHell) == 0:
                self.worldsHell = list(self.worlds)
            else:
                self.worldsHell = self.worldsHell.split(',')

    def generalUpdate(self):
        playerCount = len(self.players)

        print("pc: {0}, mc: {1}, in: {2}, out: {3}".format(playerCount, len(self.matches), self.in_messages, self.out_messages))
        self.in_messages = 0
        self.out_messages = 0

        self.tryReloadFile(self.configFilePath, self.readConfig)
        if self.assetsMetadataPath:
            self.tryReloadFile(self.assetsMetadataPath, self.readAssetsMetadata)
        # Just to keep self.blocked synchronized with blocked.json
        try:
            with open(self.blockedFilePath, "r") as f:
                self.blocked = json.loads(f.read())
        except:
            pass

        if self.levelsPath:
            try:
                self.reloadLevels()
            except:
                traceback.print_exc()

        if os.path.exists(self.shutdownFilePath):
            self.shuttingDown = True
            print("shutting down...")
            os.remove(self.shutdownFilePath)
            for player in self.players:
                player.hurryUp(180)
            reactor.callLater(240, self.shutdown)

        if self.statusPath:
            try:
                with open(self.statusPath, "w") as f:
                    f.write(json.dumps({"active":playerCount, "maintenance":self.shuttingDown}))
            except:
                pass

        if self.shuttingDown and playerCount == 0:
            reactor.stop()

        reactor.callLater(5, self.generalUpdate)

    def shutdown(self):
        reactor.stop()

    def blockAddress(self, address, playerName, reason):
        if not address in self.blocked:
            self.blocked.append([address, playerName, reason])
            try:
                with open(self.blockedFilePath, "w") as f:
                    f.write(json.dumps(self.blocked))
            except:
                pass

    def getPlayerCountByAddress(self, address):
        count = 0
        for player in self.players:
            if player.client.address == address:
                count += 1
        return count

    def buildProtocol(self, addr):
        protocol = MyServerProtocol(self)
        protocol.factory = self
        return protocol

    def getMatch(self, roomName, private, gameMode):
        if private and roomName == "":
            return Match(self, roomName, private, gameMode)
        
        fmatch = None
        for match in self.matches:
            if not match.closed and len(match.players) < self.playerCap and gameMode == match.gameMode and private == match.private and (not private or match.roomName == roomName):
                if not self.allowLateEnter and match.playing:
                    continue
                fmatch = match
                break

        if fmatch == None:
            fmatch = Match(self, roomName, private, gameMode)
            self.matches.append(fmatch)

        return fmatch

    def removeMatch(self, match):
        if match in self.matches:
            self.matches.remove(match)
                

    def getLevel(self, level):
        if not self.ownLevels:
            return (level, "")
        else:
            return ("custom", self.levels[level])

    def getLevelList(self, type, mode):
        possibleLevels = [x for x in self.levels if self.levels[x]["type"] == type]
        if mode is not None:
            possibleLevels = [x for x in possibleLevels if self.levels[x]["mode"] == mode]
        if len(possibleLevels) == 0:
            raise Exception("no levels match type: {} mode: {}".format(type, mode))
        return possibleLevels

    def getRandomLevel(self, type, mode):
        if not self.ownLevels:
            if type == "lobby":
                return ("lobby", "")
            elif type == "jail":
                return ("jail", "")
            elif type == "game":
                if mode == "royale":
                    return (random.choice(self.worlds), "")
                elif mode == "pvp":
                    return (random.choice(self.worldsPVP), "")
                elif mode == "hell":
                    return (random.choice(self.worldsHell), "")
                else:
                    raise Exception("unknown game mode: "+mode)
            else:
                raise Exception("unknown level type: "+type)
        possibleLevels = self.getLevelList(type, mode)
        chosenLevel = random.choice(possibleLevels)
        return ("custom", self.levels[chosenLevel])

if __name__ == '__main__':
    factory = MyServerFactory(u"ws://127.0.0.1:{0}/royale/ws")
    factory.setProtocolOptions(autoPingInterval=5, autoPingTimeout=5)

    reactor.listenTCP(factory.listenPort, factory)
    reactor.run()
