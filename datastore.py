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
    coins = Column(Integer, nullable=False, default=0)
    def summary(self):
        return {"username":self.username, "nickname":self.nickname, "skin":self.skin, "squad":self.squad, "isDev":self.isDev, "coins":self.coins}

def checkTableSchema(expected, actual):
    for c in expected.c.keys():
        if not c in actual.c:
            print("missing column from db: "+c)
            engine.execute("ALTER TABLE "+expected.name+" ADD "+c+" "+str(expected.c[c].type.compile()))

def checkTableSchemas(existingMetaData):
    for t in Base.metadata.tables.keys():
        if t in existingMetaData.tables:
            checkTableSchema(Base.metadata.tables[t], existingMetaData.tables[t])
        else:
            Base.metadata.tables[t].create()

def checkDb(host, port, user, password, db):
    global engine
    global session
    engine = create_engine("mysql+mysqlconnector://"+user+":"+password+"@"+host+":"+str(port)+"/"+db, echo=False, pool_recycle=3600)
    Base.metadata.bind = engine
    Base.metadata.reflect()
    DBSession = sessionmaker(bind=engine)
    session = DBSession()
    existingMetaData = MetaData(bind=engine)
    existingMetaData.reflect()
    checkTableSchemas(existingMetaData)

def persistState():
    try:
        session.commit()
        return True
    except:
        session.rollback()
        return False

def register(username, password):
    if ph is None:
        return False, "account system disabled"
    if len(username) < 3:
        return False, "username too short"
    if len(username) > 20:
        return False, "username too long"
    if not re.match('^[a-zA-Z0-9]+$', username):
        return False, "illegal character in username"
    if len(password) < 8:
        return False, "password too short"
    if len(password) > 120:
        return False, "password too long"
    if 0<session.query(Account).filter_by(username=username).count():
        return False, "account already registered"
    if not allowedNickname(username):
        return (False, original, "nickname not allowed")

    salt = hashlib.sha256(os.urandom(60)).hexdigest().encode('ascii')
    pwdhash = ph.hash(password.encode('utf-8')+salt)
    
    acc = Account(username=username, salt=salt, pwdhash=pwdhash, nickname=username,skin=0,squad="")
    session.add(acc)
    if not persistState():
        return False, "failed to save account"

    acc2 = acc.summary()
    
    token = secrets.token_urlsafe(32)
    loggedInSessions[token] = username
    acc2["session"] = token
    return True, acc2

def login(username, password):
    if ph is None:
        return False, "account system disabled"
    
    invalidMsg = "invalid user name or password"
    if len(username) < 3:
        return False, invalidMsg
    if len(username) > 20:
        return False, invalidMsg
    if len(password) < 8:
        return False, invalidMsg
    if len(password) > 120:
        return False, invalidMsg

    accs = session.query(Account).filter_by(username=username).all()
    if 0==len(accs):
        return False, invalidMsg
    acc = accs[0]

    try:
        ph.verify(acc.pwdhash, password.encode('utf-8')+acc.salt.encode('ascii'))
    except argon2.exceptions.VerifyMismatchError:
        return False, invalidMsg
    
    acc2 = acc.summary()
    
    token = secrets.token_urlsafe(32)
    loggedInSessions[token] = username
    acc2["session"] = token
    return True, acc2

def resumeSession(token):
    if token not in loggedInSessions:
        return False, "session expired, please log in"

    username = loggedInSessions[token]
    accs = session.query(Account).filter_by(username=username).all()
    if 0==len(accs):
        return False, "invalid user name or password"
    acc = accs[0].summary()
    acc["session"] = token
    return True, acc

def allowedNickname(nickname):
    return not util.checkCurse(nickname)

def updateAccount(username, data):
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
    res = persistState()
    if res:
        return (True, changes, "")
    else:
        return (False, original, "failed to save to database")

def changePassword(username, password):
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
    persistState()

def logout(token):
    if token in loggedInSessions:
        del loggedInSessions[token]
