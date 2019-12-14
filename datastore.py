import os
import hashlib
import traceback
import re
import util
import json

try:
    import argon2
    A2_IMPORT = True
except:
    # Maybe we can switch to a built-in passwordHasher?
    print("Can't import argon2-cffi, accounts functioning will be disabled.")
    A2_IMPORT = False

import pickle
import secrets

loggedInSessions = {}

if A2_IMPORT:
    ph = argon2.PasswordHasher()
else:
    ph = None

from sqlalchemy import Column, ForeignKey, Integer, String, Boolean, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy.schema import MetaData

Base = declarative_base()

class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    username = Column(String(20), nullable=False, unique=True)
    salt = Column(String(64), nullable=False)
    pwdhash = Column(String(128), nullable=False)
    nickname = Column(String(50), nullable=False, unique=True)
    skin = Column(Integer, nullable=False)
    squad = Column(String(10), nullable=False)
    isDev = Column(Boolean, nullable=False, default=False)
    wins = Column(Integer, nullable=False, default=0, server_default="0")
    deaths = Column(Integer, nullable=False, default=0, server_default="0")
    kills = Column(Integer, nullable=False, default=0, server_default="0")
    coins = Column(Integer, nullable=False, default=0, server_default="0")
    isBanned = Column(Boolean, nullable=False, default=False, server_default="0")
    def summary(self):
        return {"username":self.username, "nickname":self.nickname, "skin":self.skin, "squad":self.squad, "isDev":self.isDev,
            "wins":self.wins, "deaths":self.deaths, "kills":self.kills, "coins":self.coins}
    def privSummary(self):
        return {"id":self.id, "isBanned":self.isBanned}

def checkTableSchema(expected, actual):
    for col in expected.c.keys():
        if not col in actual.c:
            print("missing column from db: "+col)
            ec = expected.c[col]
            sql="ALTER TABLE "+expected.name+" ADD "+col+" "+str(ec.type.compile())
            if not ec.nullable:
                sql += " NOT NULL"
            if ec.server_default is not None:
                sql += " DEFAULT "+ec.server_default.arg
            print(sql)
            engine.execute(sql)

def checkTableSchemas(existingMetaData):
    for t in Base.metadata.tables.keys():
        if t in existingMetaData.tables:
            checkTableSchema(Base.metadata.tables[t], existingMetaData.tables[t])
        else:
            Base.metadata.tables[t].create()

def checkDb(host, port, user, password, db):
    global engine
    global DBSession
    engine = create_engine("mysql+mysqlconnector://"+user+":"+password+"@"+host+":"+str(port)+"/"+db, echo=False, pool_size=10000, pool_recycle=3600)
    Base.metadata.bind = engine
    Base.metadata.reflect()
    DBSession = sessionmaker(bind=engine)
    session = DBSession()
    existingMetaData = MetaData(bind=engine)
    existingMetaData.reflect()
    checkTableSchemas(existingMetaData)
    session.close()

def getDbSession():
    return DBSession()

def persistState(session):
    try:
        session.commit()
        return True
    except:
        session.rollback()
        return False

def register(session, username, password):
    if ph is None:
        return False, "account system disabled", None
    if len(username) < 3:
        return False, "username too short", None
    if len(username) > 20:
        return False, "username too long", None
    if not re.match('^[a-zA-Z0-9]+$', username):
        return False, "illegal character in username", None
    if len(password) < 8:
        return False, "password too short", None
    if len(password) > 120:
        return False, "password too long", None
    if 0<session.query(Account).filter_by(username=username).count():
        return False, "account already registered", None
    if not allowedNickname(username):
        return False, "nickname not allowed", None

    salt = hashlib.sha256(os.urandom(60)).hexdigest().encode('ascii')
    pwdhash = ph.hash(password.encode('utf-8')+salt)
    
    acc = Account(username=username, salt=salt, pwdhash=pwdhash, nickname=username,skin=0,squad="")
    session.add(acc)
    if not persistState(session):
        return False, "failed to save account", None

    acc2 = acc.summary()
    
    token = secrets.token_urlsafe(32)
    loggedInSessions[token] = username
    acc2["session"] = token
    return True, acc2, acc.privSummary()

def login(session, username, password):
    if ph is None:
        return False, "account system disabled", None
    
    invalidMsg = "invalid user name or password"
    if len(username) < 3:
        return False, invalidMsg, None
    if len(username) > 20:
        return False, invalidMsg, None
    if len(password) < 8:
        return False, invalidMsg, None
    if len(password) > 120:
        return False, invalidMsg, None

    accs = session.query(Account).filter_by(username=username).all()
    if 0==len(accs):
        return False, invalidMsg, None
    acc = accs[0]

    try:
        ph.verify(acc.pwdhash, password.encode('utf-8')+acc.salt.encode('ascii'))
    except argon2.exceptions.VerifyMismatchError:
        return False, invalidMsg, None
    
    acc2 = acc.summary()
    
    token = secrets.token_urlsafe(32)
    loggedInSessions[token] = username
    acc2["session"] = token
    return True, acc2, acc.privSummary()

def resumeSession(session, token):
    if token not in loggedInSessions:
        return False, "session expired, please log in", None

    username = loggedInSessions[token]
    accs = session.query(Account).filter_by(username=username).all()
    if 0==len(accs):
        return False, "invalid user name or password", None
    acc = accs[0]
    acc2 = acc.summary()
    acc2["session"] = token
    return True, acc2, acc.privSummary()

def allowedNickname(nickname):
    return not util.checkCurse(nickname)

def updateAccount(session, username, data):
    accs = session.query(Account).filter_by(username=username).all()
    if 0==len(accs):
        return (False, {}, "invalid account")

    acc = accs[0]
    original = {"nickname": acc.nickname, "squad": acc.squad, "skin": acc.skin}   #to send rollback to user after a failed DB update
    changes = {}

    setNickname = False
    if "nickname" in data and len(data["nickname"])<=50 and data["nickname"] != acc.nickname:
        if not acc.isDev and not allowedNickname(data["nickname"]):
            return (False, original, "nickname not allowed")
        dupenicks = session.query(Account).filter_by(nickname=data["nickname"]).all()
        if 0<len(dupenicks):
            return (False, original, "nickname already in use")
        setNickname = True

    if setNickname:
        acc.nickname = data["nickname"]
        changes["nickname"] = data["nickname"]
    if "squad" in data:
        if 3<len(data["squad"]):
            data["squad"] = data["squad"][:3]
        acc.squad = data["squad"]
        changes["squad"] = data["squad"]
    if "skin" in data:
        acc.skin = data["skin"]
        changes["skin"] = data["skin"]
    res = persistState(session)
    if res:
        return (True, changes, "")
    else:
        return (False, original, "failed to save to database")

def changePassword(session, username, password):
    accs = session.query(Account).filter_by(username=username).all()
    if 0==len(accs):
        return
    if len(password) < 8:
        return
    if len(password) > 120:
        return

    salt = hashlib.sha256(os.urandom(60)).hexdigest().encode('ascii')
    pwdhash = ph.hash(password.encode('utf-8')+salt)

    acc = accs[0]
    acc.salt = salt
    acc.pwdhash = pwdhash
    persistState(session)

def logout(dbSession, token):
    if token in loggedInSessions:
        del loggedInSessions[token]

def updateStats(session, accId, fields):
    accs = session.query(Account).filter_by(id=accId).all()
    if 0==len(accs):
        return
    acc = accs[0]
    if "wins" in fields:
        acc.wins += fields["wins"]
    if "deaths" in fields:
        acc.deaths += fields["deaths"]
    if "kills" in fields:
        acc.kills += fields["kills"]
    if "coins" in fields:
        acc.coins = max(0,acc.coins+fields["coins"])
    if "isBanned" in fields:
        acc.isBanned = fields["isBanned"]
    if "nickname" in fields:
        acc.nickname = fields["nickname"]
    if "squad" in fields:
        acc.squad = fields["squad"]
    persistState(session)

def getLeaderBoard():
    session = DBSession()
    try:
        coinLB = session.query(Account).order_by(Account.coins.desc()).limit(10)
        winsLB = session.query(Account).order_by(Account.wins.desc()).limit(10)
        killsLB = session.query(Account).order_by(Account.kills.desc()).limit(10)
        return {"coinLeaderBoard":[{"pos":i, "nickname": x.nickname, "coins": x.coins} for i,x in enumerate(coinLB, 1)],
            "winsLeaderBoard":[{"pos":i, "nickname": x.nickname, "wins": x.wins} for i,x in enumerate(winsLB, 1)],
            "killsLeaderBoard":[{"pos":i, "nickname": x.nickname, "kills": x.kills} for i,x in enumerate(killsLB, 1)]
        }
    finally:
        session.close()
