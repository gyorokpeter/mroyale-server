import os
import json

curse = []
cursePath=os.path.join(os.path.dirname(os.path.abspath(__file__)),"words.json")
if os.path.exists(cursePath):
    with open(cursePath, "r") as f:
        curse = json.loads(f.read())

def leet2(word):
    REPLACE = { str(index): str(letter) for index, letter in enumerate('oizeasgtb') }
    letters = [ REPLACE.get(l, l) for l in word.lower() ]
    return ''.join(letters)

def checkForBannedWords(name, blacklist):
    if checkCheckCurse(name, blacklist):
        return True
    name = leet2(name)
    if checkCheckCurse(name, blacklist):
        return True
    name = name.replace("|", "i").replace("$", "s").replace("@", "a").replace("&", "e")
    name = ''.join(e for e in name if e.isalnum())
    if checkCheckCurse(name, blacklist):
        return True
    return False

def checkCurse(name):
    return checkForBannedWords(name, curse)

def checkCheckCurse(name, blacklist):
    if len(name) <= 3:
        return False
    name = name.lower()
    for w in blacklist:
        if len(w) <= 3:
            continue
        if w in name:
            return True
    return False

