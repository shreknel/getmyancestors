import sys
import os
import re
import time
import tempfile
import asyncio
from urllib.parse import unquote

# global imports
from tkinter import (
    Tk,
    StringVar,
    IntVar,
    filedialog,
    messagebox,
    Menu,
    TclError,
    PhotoImage,
)
from tkinter.ttk import Frame, Label, Entry, Button, Checkbutton, Treeview, Notebook

from threading import Thread
from diskcache import Cache
import requests
import babelfish

# local imports
import getmyancestors
from getmyancestors.translation import translations
from getmyancestors.constants import (
    MAX_PERSONS,
    FACT_EVEN,
    FACT_TAGS,
    FACT_TYPES,
    ORDINANCES,
    ORDINANCES_STATUS,
)

tmp_dir = os.path.join(tempfile.gettempdir(), "fstogedcom")
cache = Cache(tmp_dir)
lang = cache.get("lang")

# getmyancestors classes and functions
def cont(string):
    """parse a GEDCOM line adding CONT and CONT tags if necessary"""
    level = int(string[:1]) + 1
    lines = string.splitlines()
    res = list()
    max_len = 255
    for line in lines:
        c_line = line
        to_conc = list()
        while len(c_line.encode("utf-8")) > max_len:
            index = min(max_len, len(c_line) - 2)
            while (
                len(c_line[:index].encode("utf-8")) > max_len
                or re.search(r"[ \t\v]", c_line[index - 1 : index + 1])
            ) and index > 1:
                index -= 1
            to_conc.append(c_line[:index])
            c_line = c_line[index:]
            max_len = 248
        to_conc.append(c_line)
        res.append(("\n%s CONC " % level).join(to_conc))
        max_len = 248
    return ("\n%s CONT " % level).join(res) + "\n"


class Session:
    """Create a FamilySearch session
    :param username and password: valid FamilySearch credentials
    :param verbose: True to active verbose mode
    :param logfile: a file object or similar
    :param timeout: time before retry a request
    """

    def __init__(self, username, password, verbose=False, logfile=False, timeout=60):
        self.username = username
        self.password = password
        self.verbose = verbose
        self.logfile = logfile
        self.timeout = timeout
        self.fid = self.lang = self.display_name = None
        self.counter = 0
        self.logged = self.login()

    def write_log(self, text):
        """write text in the log file"""
        log = "[%s]: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text)
        if self.verbose:
            sys.stderr.write(log)
        if self.logfile:
            self.logfile.write(log)

    def login(self):
        """retrieve FamilySearch session ID
        (https://familysearch.org/developers/docs/guides/oauth2)
        """
        while True:
            try:
                url = "https://www.familysearch.org/auth/familysearch/login"
                self.write_log("Downloading: " + url)
                r = requests.get(url, params={"ldsauth": False}, allow_redirects=False)
                url = r.headers["Location"]
                self.write_log("Downloading: " + url)
                r = requests.get(url, allow_redirects=False)
                idx = r.text.index('name="params" value="')
                span = r.text[idx + 21 :].index('"')
                params = r.text[idx + 21 : idx + 21 + span]

                url = "https://ident.familysearch.org/cis-web/oauth2/v3/authorization"
                self.write_log("Downloading: " + url)
                r = requests.post(
                    url,
                    data={
                        "params": params,
                        "userName": self.username,
                        "password": self.password,
                    },
                    allow_redirects=False,
                )

                if "The username or password was incorrect" in r.text:
                    self.write_log("The username or password was incorrect")
                    return False

                if "Invalid Oauth2 Request" in r.text:
                    self.write_log("Invalid Oauth2 Request")
                    time.sleep(self.timeout)
                    continue

                url = r.headers["Location"]
                self.write_log("Downloading: " + url)
                r = requests.get(url, allow_redirects=False)
                self.fssessionid = r.cookies["fssessionid"]
            except requests.exceptions.ReadTimeout:
                self.write_log("Read timed out")
                continue
            except requests.exceptions.ConnectionError:
                self.write_log("Connection aborted")
                time.sleep(self.timeout)
                continue
            except requests.exceptions.HTTPError:
                self.write_log("HTTPError")
                time.sleep(self.timeout)
                continue
            except KeyError:
                self.write_log("KeyError")
                time.sleep(self.timeout)
                continue
            except ValueError:
                self.write_log("ValueError")
                time.sleep(self.timeout)
                continue
            self.write_log("FamilySearch session id: " + self.fssessionid)
            self.set_current()
            return True

    def get_url(self, url, headers=None):
        """retrieve JSON structure from a FamilySearch URL"""
        self.counter += 1
        if headers is None:
            headers = {"Accept": "application/x-gedcomx-v1+json"}
        while True:
            try:
                self.write_log("Downloading: " + url)
                r = requests.get(
                    "https://familysearch.org" + url,
                    cookies={"fssessionid": self.fssessionid},
                    timeout=self.timeout,
                    headers=headers,
                )
            except requests.exceptions.ReadTimeout:
                self.write_log("Read timed out")
                continue
            except requests.exceptions.ConnectionError:
                self.write_log("Connection aborted")
                time.sleep(self.timeout)
                continue
            self.write_log("Status code: %s" % r.status_code)
            if r.status_code == 204:
                return None
            if r.status_code in {404, 405, 410, 500}:
                self.write_log("WARNING: " + url)
                return None
            if r.status_code == 401:
                self.login()
                continue
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError:
                self.write_log("HTTPError")
                if r.status_code == 403:
                    if (
                        "message" in r.json()["errors"][0]
                        and r.json()["errors"][0]["message"]
                        == "Unable to get ordinances."
                    ):
                        self.write_log(
                            "Unable to get ordinances. "
                            "Try with an LDS account or without option -c."
                        )
                        return "error"
                    self.write_log(
                        "WARNING: code 403 from %s %s"
                        % (url, r.json()["errors"][0]["message"] or "")
                    )
                    return None
                time.sleep(self.timeout)
                continue
            try:
                return r.json()
            except Exception as e:
                self.write_log("WARNING: corrupted file from %s, error: %s" % (url, e))
                return None

    def set_current(self):
        """retrieve FamilySearch current user ID, name and language"""
        url = "/platform/users/current"
        data = self.get_url(url)
        if data:
            self.fid = data["users"][0]["personId"]
            self.lang = data["users"][0]["preferredLanguage"]
            self.display_name = data["users"][0]["displayName"]

    def _(self, string):
        """translate a string into user's language
        TODO replace translation file for gettext format
        """
        if string in translations and self.lang in translations[string]:
            return translations[string][self.lang]
        return string


class Note:
    """GEDCOM Note class
    :param text: the Note content
    :param tree: a Tree object
    :param num: the GEDCOM identifier
    """

    counter = 0

    def __init__(self, text="", tree=None, num=None):
        if num:
            self.num = num
        else:
            Note.counter += 1
            self.num = Note.counter
        self.text = text.strip()

        if tree:
            tree.notes.append(self)

    def print(self, file=sys.stdout):
        """print Note in GEDCOM format"""
        file.write(cont("0 @N%s@ NOTE %s" % (self.num, self.text)))

    def link(self, file=sys.stdout, level=1):
        """print the reference in GEDCOM format"""
        file.write("%s NOTE @N%s@\n" % (level, self.num))


class Source:
    """GEDCOM Source class
    :param data: FS Source data
    :param tree: a Tree object
    :param num: the GEDCOM identifier
    """

    counter = 0

    def __init__(self, data=None, tree=None, num=None):
        if num:
            self.num = num
        else:
            Source.counter += 1
            self.num = Source.counter

        self.tree = tree
        self.url = self.citation = self.title = self.fid = None
        self.notes = set()
        if data:
            self.fid = data["id"]
            if "about" in data:
                self.url = data["about"].replace(
                    "familysearch.org/platform/memories/memories",
                    "www.familysearch.org/photos/artifacts",
                )
            if "citations" in data:
                self.citation = data["citations"][0]["value"]
            if "titles" in data:
                self.title = data["titles"][0]["value"]
            if "notes" in data:
                for n in data["notes"]:
                    if n["text"]:
                        self.notes.add(Note(n["text"], self.tree))

    def print(self, file=sys.stdout):
        """print Source in GEDCOM format"""
        file.write("0 @S%s@ SOUR \n" % self.num)
        if self.title:
            file.write(cont("1 TITL " + self.title))
        if self.citation:
            file.write(cont("1 AUTH " + self.citation))
        if self.url:
            file.write(cont("1 PUBL " + self.url))
        for n in self.notes:
            n.link(file, 1)
        file.write("1 REFN %s\n" % self.fid)

    def link(self, file=sys.stdout, level=1):
        """print the reference in GEDCOM format"""
        file.write("%s SOUR @S%s@\n" % (level, self.num))


class Fact:
    """GEDCOM Fact class
    :param data: FS Fact data
    :param tree: a tree object
    """

    def __init__(self, data=None, tree=None):
        self.value = self.type = self.date = self.place = self.note = self.map = None
        if data:
            if "value" in data:
                self.value = data["value"]
            if "type" in data:
                self.type = data["type"]
                if self.type in FACT_EVEN:
                    self.type = tree.fs._(FACT_EVEN[self.type])
                elif self.type[:6] == "data:,":
                    self.type = unquote(self.type[6:])
                elif self.type not in FACT_TAGS:
                    self.type = None
            if "date" in data:
                self.date = data["date"]["original"]
            if "place" in data:
                place = data["place"]
                self.place = place["original"]
                if "description" in place and place["description"][1:] in tree.places:
                    self.map = tree.places[place["description"][1:]]
            if "changeMessage" in data["attribution"]:
                self.note = Note(data["attribution"]["changeMessage"], tree)
            if self.type == "http://gedcomx.org/Death" and not (
                self.date or self.place
            ):
                self.value = "Y"

    def print(self, file=sys.stdout):
        """print Fact in GEDCOM format
        the GEDCOM TAG depends on the type, defined in FACT_TAGS
        """
        if self.type in FACT_TAGS:
            tmp = "1 " + FACT_TAGS[self.type]
            if self.value:
                tmp += " " + self.value
            file.write(cont(tmp))
        elif self.type:
            file.write("1 EVEN\n2 TYPE %s\n" % self.type)
            if self.value:
                file.write(cont("2 NOTE Description: " + self.value))
        else:
            return
        if self.date:
            file.write(cont("2 DATE " + self.date))
        if self.place:
            file.write(cont("2 PLAC " + self.place))
        if self.map:
            latitude, longitude = self.map
            file.write("3 MAP\n4 LATI %s\n4 LONG %s\n" % (latitude, longitude))
        if self.note:
            self.note.link(file, 2)


class Memorie:
    """GEDCOM Memorie class
    :param data: FS Memorie data
    """

    def __init__(self, data=None):
        self.description = self.url = None
        if data and "links" in data:
            self.url = data["about"]
            if "titles" in data:
                self.description = data["titles"][0]["value"]
            if "descriptions" in data:
                self.description = (
                    "" if not self.description else self.description + "\n"
                ) + data["descriptions"][0]["value"]

    def print(self, file=sys.stdout):
        """print Memorie in GEDCOM format"""
        file.write("1 OBJE\n2 FORM URL\n")
        if self.description:
            file.write(cont("2 TITL " + self.description))
        if self.url:
            file.write(cont("2 FILE " + self.url))


class Name:
    """GEDCOM Name class
    :param data: FS Name data
    :param tree: a Tree object
    """

    def __init__(self, data=None, tree=None):
        self.given = ""
        self.surname = ""
        self.prefix = None
        self.suffix = None
        self.note = None
        if data:
            if "parts" in data["nameForms"][0]:
                for z in data["nameForms"][0]["parts"]:
                    if z["type"] == "http://gedcomx.org/Given":
                        self.given = z["value"]
                    if z["type"] == "http://gedcomx.org/Surname":
                        self.surname = z["value"]
                    if z["type"] == "http://gedcomx.org/Prefix":
                        self.prefix = z["value"]
                    if z["type"] == "http://gedcomx.org/Suffix":
                        self.suffix = z["value"]
            if "changeMessage" in data["attribution"]:
                self.note = Note(data["attribution"]["changeMessage"], tree)

    def print(self, file=sys.stdout, typ=None):
        """print Name in GEDCOM format
        :param typ: type for additional names
        """
        tmp = "1 NAME %s /%s/" % (self.given, self.surname)
        if self.suffix:
            tmp += " " + self.suffix
        file.write(cont(tmp))
        if typ:
            file.write("2 TYPE %s\n" % typ)
        if self.prefix:
            file.write("2 NPFX %s\n" % self.prefix)
        if self.note:
            self.note.link(file, 2)


class Ordinance:
    """GEDCOM Ordinance class
    :param data: FS Ordinance data
    """

    def __init__(self, data=None):
        self.date = self.temple_code = self.status = self.famc = None
        if data:
            if "completedDate" in data:
                self.date = data["completedDate"]
            if "completedTemple" in data:
                self.temple_code = data["completedTemple"]["code"]
            self.status = data["status"]

    def print(self, file=sys.stdout):
        """print Ordinance in Gecom format"""
        if self.date:
            file.write(cont("2 DATE " + self.date))
        if self.temple_code:
            file.write("2 TEMP %s\n" % self.temple_code)
        if self.status in ORDINANCES_STATUS:
            file.write("2 STAT %s\n" % ORDINANCES_STATUS[self.status])
        if self.famc:
            file.write("2 FAMC @F%s@\n" % self.famc.num)


class Indi:
    """GEDCOM individual class
    :param fid' FamilySearch id
    :param tree: a tree object
    :param num: the GEDCOM identifier
    """

    counter = 0

    def __init__(self, fid=None, tree=None, num=None):
        if num:
            self.num = num
        else:
            Indi.counter += 1
            self.num = Indi.counter
        self.fid = fid
        self.tree = tree
        self.famc_fid = set()
        self.fams_fid = set()
        self.famc_num = set()
        self.fams_num = set()
        self.name = None
        self.gender = None
        self.living = None
        self.parents = set()
        self.spouses = set()
        self.children = set()
        self.baptism = self.confirmation = self.initiatory = None
        self.endowment = self.sealing_child = None
        self.nicknames = set()
        self.facts = set()
        self.birthnames = set()
        self.married = set()
        self.aka = set()
        self.notes = set()
        self.sources = set()
        self.memories = set()

    def add_data(self, data):
        """add FS individual data"""
        if data:
            self.living = data["living"]
            for x in data["names"]:
                if x["preferred"]:
                    self.name = Name(x, self.tree)
                else:
                    if x["type"] == "http://gedcomx.org/Nickname":
                        self.nicknames.add(Name(x, self.tree))
                    if x["type"] == "http://gedcomx.org/BirthName":
                        self.birthnames.add(Name(x, self.tree))
                    if x["type"] == "http://gedcomx.org/AlsoKnownAs":
                        self.aka.add(Name(x, self.tree))
                    if x["type"] == "http://gedcomx.org/MarriedName":
                        self.married.add(Name(x, self.tree))
            if "gender" in data:
                if data["gender"]["type"] == "http://gedcomx.org/Male":
                    self.gender = "M"
                elif data["gender"]["type"] == "http://gedcomx.org/Female":
                    self.gender = "F"
                elif data["gender"]["type"] == "http://gedcomx.org/Unknown":
                    self.gender = "U"
            if "facts" in data:
                for x in data["facts"]:
                    if x["type"] == "http://familysearch.org/v1/LifeSketch":
                        self.notes.add(
                            Note(
                                "=== %s ===\n%s"
                                % (self.tree.fs._("Life Sketch"), x.get("value", "")),
                                self.tree,
                            )
                        )
                    else:
                        self.facts.add(Fact(x, self.tree))
            if "sources" in data:
                sources = self.tree.fs.get_url(
                    "/platform/tree/persons/%s/sources" % self.fid
                )
                if sources:
                    quotes = dict()
                    for quote in sources["persons"][0]["sources"]:
                        quotes[quote["descriptionId"]] = (
                            quote["attribution"]["changeMessage"]
                            if "changeMessage" in quote["attribution"]
                            else None
                        )
                    for source in sources["sourceDescriptions"]:
                        if source["id"] not in self.tree.sources:
                            self.tree.sources[source["id"]] = Source(source, self.tree)
                        self.sources.add(
                            (self.tree.sources[source["id"]], quotes[source["id"]])
                        )
            if "evidence" in data:
                url = "/platform/tree/persons/%s/memories" % self.fid
                memorie = self.tree.fs.get_url(url)
                if memorie and "sourceDescriptions" in memorie:
                    for x in memorie["sourceDescriptions"]:
                        if x["mediaType"] == "text/plain":
                            text = "\n".join(
                                val.get("value", "")
                                for val in x.get("titles", [])
                                + x.get("descriptions", [])
                            )
                            self.notes.add(Note(text, self.tree))
                        else:
                            self.memories.add(Memorie(x))

    def add_fams(self, fams):
        """add family fid (for spouse or parent)"""
        self.fams_fid.add(fams)

    def add_famc(self, famc):
        """add family fid (for child)"""
        self.famc_fid.add(famc)

    def get_notes(self):
        """retrieve individual notes"""
        notes = self.tree.fs.get_url("/platform/tree/persons/%s/notes" % self.fid)
        if notes:
            for n in notes["persons"][0]["notes"]:
                text_note = "=== %s ===\n" % n["subject"] if "subject" in n else ""
                text_note += n["text"] + "\n" if "text" in n else ""
                self.notes.add(Note(text_note, self.tree))

    def get_ordinances(self):
        """retrieve LDS ordinances
        need a LDS account
        """
        res = []
        famc = False
        if self.living:
            return res, famc
        url = "/service/tree/tree-data/reservations/person/%s/ordinances" % self.fid
        data = self.tree.fs.get_url(url, {})
        if data:
            for key, o in data["data"].items():
                if key == "baptism":
                    self.baptism = Ordinance(o)
                elif key == "confirmation":
                    self.confirmation = Ordinance(o)
                elif key == "initiatory":
                    self.initiatory = Ordinance(o)
                elif key == "endowment":
                    self.endowment = Ordinance(o)
                elif key == "sealingsToParents":
                    for subo in o:
                        self.sealing_child = Ordinance(subo)
                        relationships = subo.get("relationships", {})
                        father = relationships.get("parent1Id")
                        mother = relationships.get("parent2Id")
                        if father and mother:
                            famc = father, mother
                elif key == "sealingsToSpouses":
                    res += o
        return res, famc

    def get_contributors(self):
        """retrieve contributors"""
        temp = set()
        url = "/platform/tree/persons/%s/changes" % self.fid
        data = self.tree.fs.get_url(url, {"Accept": "application/x-gedcomx-atom+json"})
        if data:
            for entries in data["entries"]:
                for contributors in entries["contributors"]:
                    temp.add(contributors["name"])
        if temp:
            text = "=== %s ===\n%s" % (
                self.tree.fs._("Contributors"),
                "\n".join(sorted(temp)),
            )
            for n in self.tree.notes:
                if n.text == text:
                    self.notes.add(n)
                    return
            self.notes.add(Note(text, self.tree))

    def print(self, file=sys.stdout):
        """print individual in GEDCOM format"""
        file.write("0 @I%s@ INDI\n" % self.num)
        if self.name:
            self.name.print(file)
        for o in self.nicknames:
            file.write(cont("2 NICK %s %s" % (o.given, o.surname)))
        for o in self.birthnames:
            o.print(file)
        for o in self.aka:
            o.print(file, "aka")
        for o in self.married:
            o.print(file, "married")
        if self.gender:
            file.write("1 SEX %s\n" % self.gender)
        for o in self.facts:
            o.print(file)
        for o in self.memories:
            o.print(file)
        if self.baptism:
            file.write("1 BAPL\n")
            self.baptism.print(file)
        if self.confirmation:
            file.write("1 CONL\n")
            self.confirmation.print(file)
        if self.initiatory:
            file.write("1 WAC\n")
            self.initiatory.print(file)
        if self.endowment:
            file.write("1 ENDL\n")
            self.endowment.print(file)
        if self.sealing_child:
            file.write("1 SLGC\n")
            self.sealing_child.print(file)
        for num in self.fams_num:
            file.write("1 FAMS @F%s@\n" % num)
        for num in self.famc_num:
            file.write("1 FAMC @F%s@\n" % num)
        file.write("1 _FSFTID %s\n" % self.fid)
        for o in self.notes:
            o.link(file)
        for source, quote in self.sources:
            source.link(file, 1)
            if quote:
                file.write(cont("2 PAGE " + quote))


class Fam:
    """GEDCOM family class
    :param husb: husbant fid
    :param wife: wife fid
    :param tree: a Tree object
    :param num: a GEDCOM identifier
    """

    counter = 0

    def __init__(self, husb=None, wife=None, tree=None, num=None):
        if num:
            self.num = num
        else:
            Fam.counter += 1
            self.num = Fam.counter
        self.husb_fid = husb if husb else None
        self.wife_fid = wife if wife else None
        self.tree = tree
        self.husb_num = self.wife_num = self.fid = None
        self.facts = set()
        self.sealing_spouse = None
        self.chil_fid = set()
        self.chil_num = set()
        self.notes = set()
        self.sources = set()

    def add_child(self, child):
        """add a child fid to the family"""
        if child not in self.chil_fid:
            self.chil_fid.add(child)

    def add_marriage(self, fid):
        """retrieve and add marriage information
        :param fid: the marriage fid
        """
        if not self.fid:
            self.fid = fid
            url = "/platform/tree/couple-relationships/%s" % self.fid
            data = self.tree.fs.get_url(url)
            if data:
                if "facts" in data["relationships"][0]:
                    for x in data["relationships"][0]["facts"]:
                        self.facts.add(Fact(x, self.tree))
                if "sources" in data["relationships"][0]:
                    quotes = dict()
                    for x in data["relationships"][0]["sources"]:
                        quotes[x["descriptionId"]] = (
                            x["attribution"]["changeMessage"]
                            if "changeMessage" in x["attribution"]
                            else None
                        )
                    new_sources = quotes.keys() - self.tree.sources.keys()
                    if new_sources:
                        sources = self.tree.fs.get_url(
                            "/platform/tree/couple-relationships/%s/sources" % self.fid
                        )
                        for source in sources["sourceDescriptions"]:
                            if (
                                source["id"] in new_sources
                                and source["id"] not in self.tree.sources
                            ):
                                self.tree.sources[source["id"]] = Source(
                                    source, self.tree
                                )
                    for source_fid in quotes:
                        self.sources.add(
                            (self.tree.sources[source_fid], quotes[source_fid])
                        )

    def get_notes(self):
        """retrieve marriage notes"""
        if self.fid:
            notes = self.tree.fs.get_url(
                "/platform/tree/couple-relationships/%s/notes" % self.fid
            )
            if notes:
                for n in notes["relationships"][0]["notes"]:
                    text_note = "=== %s ===\n" % n["subject"] if "subject" in n else ""
                    text_note += n["text"] + "\n" if "text" in n else ""
                    self.notes.add(Note(text_note, self.tree))

    def get_contributors(self):
        """retrieve contributors"""
        if self.fid:
            temp = set()
            url = "/platform/tree/couple-relationships/%s/changes" % self.fid
            data = self.tree.fs.get_url(
                url, {"Accept": "application/x-gedcomx-atom+json"}
            )
            if data:
                for entries in data["entries"]:
                    for contributors in entries["contributors"]:
                        temp.add(contributors["name"])
            if temp:
                text = "=== %s ===\n%s" % (
                    self.tree.fs._("Contributors"),
                    "\n".join(sorted(temp)),
                )
                for n in self.tree.notes:
                    if n.text == text:
                        self.notes.add(n)
                        return
                self.notes.add(Note(text, self.tree))

    def print(self, file=sys.stdout):
        """print family information in GEDCOM format"""
        file.write("0 @F%s@ FAM\n" % self.num)
        if self.husb_num:
            file.write("1 HUSB @I%s@\n" % self.husb_num)
        if self.wife_num:
            file.write("1 WIFE @I%s@\n" % self.wife_num)
        for num in self.chil_num:
            file.write("1 CHIL @I%s@\n" % num)
        for o in self.facts:
            o.print(file)
        if self.sealing_spouse:
            file.write("1 SLGS\n")
            self.sealing_spouse.print(file)
        if self.fid:
            file.write("1 _FSFTID %s\n" % self.fid)
        for o in self.notes:
            o.link(file)
        for source, quote in self.sources:
            source.link(file, 1)
            if quote:
                file.write(cont("2 PAGE " + quote))


class Tree:
    """family tree class
    :param fs: a Session object
    """

    def __init__(self, fs=None):
        self.fs = fs
        self.indi = dict()
        self.fam = dict()
        self.notes = list()
        self.sources = dict()
        self.places = dict()
        self.display_name = self.lang = None
        if fs:
            self.display_name = fs.display_name
            self.lang = babelfish.Language.fromalpha2(fs.lang).name

    def add_indis(self, fids):
        """add individuals to the family tree
        :param fids: an iterable of fid
        """

        async def add_datas(loop, data):
            futures = set()
            for person in data["persons"]:
                self.indi[person["id"]] = Indi(person["id"], self)
                futures.add(
                    loop.run_in_executor(None, self.indi[person["id"]].add_data, person)
                )
            for future in futures:
                await future

        new_fids = [fid for fid in fids if fid and fid not in self.indi]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while new_fids:
            data = self.fs.get_url(
                "/platform/tree/persons?pids=" + ",".join(new_fids[:MAX_PERSONS])
            )
            if data:
                if "places" in data:
                    for place in data["places"]:
                        if place["id"] not in self.places:
                            self.places[place["id"]] = (
                                str(place["latitude"]),
                                str(place["longitude"]),
                            )
                loop.run_until_complete(add_datas(loop, data))
                if "childAndParentsRelationships" in data:
                    for rel in data["childAndParentsRelationships"]:
                        father = (
                            rel["parent1"]["resourceId"] if "parent1" in rel else None
                        )
                        mother = (
                            rel["parent2"]["resourceId"] if "parent2" in rel else None
                        )
                        child = rel["child"]["resourceId"] if "child" in rel else None
                        if child in self.indi:
                            self.indi[child].parents.add((father, mother))
                        if father in self.indi:
                            self.indi[father].children.add((father, mother, child))
                        if mother in self.indi:
                            self.indi[mother].children.add((father, mother, child))
                if "relationships" in data:
                    for rel in data["relationships"]:
                        if rel["type"] == "http://gedcomx.org/Couple":
                            person1 = rel["person1"]["resourceId"]
                            person2 = rel["person2"]["resourceId"]
                            relfid = rel["id"]
                            if person1 in self.indi:
                                self.indi[person1].spouses.add(
                                    (person1, person2, relfid)
                                )
                            if person2 in self.indi:
                                self.indi[person2].spouses.add(
                                    (person1, person2, relfid)
                                )
            new_fids = new_fids[MAX_PERSONS:]

    def add_fam(self, father, mother):
        """add a family to the family tree
        :param father: the father fid or None
        :param mother: the mother fid or None
        """
        if (father, mother) not in self.fam:
            self.fam[(father, mother)] = Fam(father, mother, self)

    def add_trio(self, father, mother, child):
        """add a children relationship to the family tree
        :param father: the father fid or None
        :param mother: the mother fid or None
        :param child: the child fid or None
        """
        if father in self.indi:
            self.indi[father].add_fams((father, mother))
        if mother in self.indi:
            self.indi[mother].add_fams((father, mother))
        if child in self.indi and (father in self.indi or mother in self.indi):
            self.indi[child].add_famc((father, mother))
            self.add_fam(father, mother)
            self.fam[(father, mother)].add_child(child)

    def add_parents(self, fids):
        """add parents relationships
        :param fids: a set of fids
        """
        parents = set()
        for fid in fids & self.indi.keys():
            for couple in self.indi[fid].parents:
                parents |= set(couple)
        if parents:
            self.add_indis(parents)
        for fid in fids & self.indi.keys():
            for father, mother in self.indi[fid].parents:
                if (
                    mother in self.indi
                    and father in self.indi
                    or not father
                    and mother in self.indi
                    or not mother
                    and father in self.indi
                ):
                    self.add_trio(father, mother, fid)
        return set(filter(None, parents))

    def add_spouses(self, fids):
        """add spouse relationships
        :param fids: a set of fid
        """

        async def add(loop, rels):
            futures = set()
            for father, mother, relfid in rels:
                if (father, mother) in self.fam:
                    futures.add(
                        loop.run_in_executor(
                            None, self.fam[(father, mother)].add_marriage, relfid
                        )
                    )
            for future in futures:
                await future

        rels = set()
        for fid in fids & self.indi.keys():
            rels |= self.indi[fid].spouses
        loop = asyncio.get_event_loop()
        if rels:
            self.add_indis(
                set.union(*({father, mother} for father, mother, relfid in rels))
            )
            for father, mother, _ in rels:
                if father in self.indi and mother in self.indi:
                    self.indi[father].add_fams((father, mother))
                    self.indi[mother].add_fams((father, mother))
                    self.add_fam(father, mother)
            loop.run_until_complete(add(loop, rels))

    def add_children(self, fids):
        """add children relationships
        :param fids: a set of fid
        """
        rels = set()
        for fid in fids & self.indi.keys():
            rels |= self.indi[fid].children if fid in self.indi else set()
        children = set()
        if rels:
            self.add_indis(set.union(*(set(rel) for rel in rels)))
            for father, mother, child in rels:
                if child in self.indi and (
                    mother in self.indi
                    and father in self.indi
                    or not father
                    and mother in self.indi
                    or not mother
                    and father in self.indi
                ):
                    self.add_trio(father, mother, child)
                    children.add(child)
        return children

    def add_ordinances(self, fid):
        """retrieve ordinances
        :param fid: an individual fid
        """
        if fid in self.indi:
            ret, famc = self.indi[fid].get_ordinances()
            if famc and famc in self.fam:
                self.indi[fid].sealing_child.famc = self.fam[famc]
            for o in ret:
                spouse_id = o["relationships"]["spouseId"]
                if (fid, spouse_id) in self.fam:
                    self.fam[fid, spouse_id].sealing_spouse = Ordinance(o)
                elif (spouse_id, fid) in self.fam:
                    self.fam[spouse_id, fid].sealing_spouse = Ordinance(o)

    def reset_num(self):
        """reset all GEDCOM identifiers"""
        for husb, wife in self.fam:
            self.fam[(husb, wife)].husb_num = self.indi[husb].num if husb else None
            self.fam[(husb, wife)].wife_num = self.indi[wife].num if wife else None
            self.fam[(husb, wife)].chil_num = set(
                self.indi[chil].num for chil in self.fam[(husb, wife)].chil_fid
            )
        for fid in self.indi:
            self.indi[fid].famc_num = set(
                self.fam[(husb, wife)].num for husb, wife in self.indi[fid].famc_fid
            )
            self.indi[fid].fams_num = set(
                self.fam[(husb, wife)].num for husb, wife in self.indi[fid].fams_fid
            )

    def print(self, file=sys.stdout):
        """print family tree in GEDCOM format"""
        file.write("0 HEAD\n")
        file.write("1 CHAR UTF-8\n")
        file.write("1 GEDC\n")
        file.write("2 VERS 5.1.1\n")
        file.write("2 FORM LINEAGE-LINKED\n")
        file.write("1 SOUR getmyancestors\n")
        file.write("2 VERS %s\n" % getmyancestors.__version__)
        file.write("2 NAME getmyancestors\n")
        file.write("1 DATE %s\n" % time.strftime("%d %b %Y"))
        file.write("2 TIME %s\n" % time.strftime("%H:%M:%S"))
        file.write("1 SUBM @SUBM@\n")
        file.write("0 @SUBM@ SUBM\n")
        file.write("1 NAME %s\n" % self.display_name)
        file.write("1 LANG %s\n" % self.lang)

        for fid in sorted(self.indi, key=lambda x: self.indi.__getitem__(x).num):
            self.indi[fid].print(file)
        for husb, wife in sorted(self.fam, key=lambda x: self.fam.__getitem__(x).num):
            self.fam[(husb, wife)].print(file)
        sources = sorted(self.sources.values(), key=lambda x: x.num)
        for s in sources:
            s.print(file)
        notes = sorted(self.notes, key=lambda x: x.num)
        for i, n in enumerate(notes):
            if i > 0:
                if n.num == notes[i - 1].num:
                    continue
            n.print(file)
        file.write("0 TRLR\n")


# mergemyancestors classes
class Gedcom:
    """Parse a GEDCOM file into a Tree"""

    def __init__(self, file, tree):
        self.f = file
        self.num = None
        self.tree = tree
        self.level = 0
        self.pointer = None
        self.tag = None
        self.data = None
        self.flag = False
        self.indi = dict()
        self.fam = dict()
        self.note = dict()
        self.sour = dict()
        self.__parse()
        self.__add_id()

    def __parse(self):
        """Parse the GEDCOM file into self.tree"""
        while self.__get_line():
            if self.tag == "INDI":
                self.num = int(self.pointer[2 : len(self.pointer) - 1])
                self.indi[self.num] = Indi(tree=self.tree, num=self.num)
                self.__get_indi()
            elif self.tag == "FAM":
                self.num = int(self.pointer[2 : len(self.pointer) - 1])
                if self.num not in self.fam:
                    self.fam[self.num] = Fam(tree=self.tree, num=self.num)
                self.__get_fam()
            elif self.tag == "NOTE":
                self.num = int(self.pointer[2 : len(self.pointer) - 1])
                if self.num not in self.note:
                    self.note[self.num] = Note(tree=self.tree, num=self.num)
                self.__get_note()
            elif self.tag == "SOUR" and self.pointer:
                self.num = int(self.pointer[2 : len(self.pointer) - 1])
                if self.num not in self.sour:
                    self.sour[self.num] = Source(num=self.num)
                self.__get_source()
            elif self.tag == "SUBM" and self.pointer:
                self.__get_subm()

    def __get_subm(self):
        while self.__get_line() and self.level > 0:
            if not self.tree.display_name or not self.tree.lang:
                if self.tag == "NAME":
                    self.tree.display_name = self.data
                elif self.tag == "LANG":
                    self.tree.lang = self.data
        self.flag = True

    def __get_line(self):
        """Parse a new line
        If the flag is set, skip reading a newline
        """
        if self.flag:
            self.flag = False
            return True
        words = self.f.readline().split()

        if not words:
            return False
        self.level = int(words[0])
        if words[1][0] == "@":
            self.pointer = words[1]
            self.tag = words[2]
            self.data = " ".join(words[3:])
        else:
            self.pointer = None
            self.tag = words[1]
            self.data = " ".join(words[2:])
        return True

    def __get_indi(self):
        """Parse an individual"""
        while self.f and self.__get_line() and self.level > 0:
            if self.tag == "NAME":
                self.__get_name()
            elif self.tag == "SEX":
                self.indi[self.num].gender = self.data
            elif self.tag in FACT_TYPES or self.tag == "EVEN":
                self.indi[self.num].facts.add(self.__get_fact())
            elif self.tag == "BAPL":
                self.indi[self.num].baptism = self.__get_ordinance()
            elif self.tag == "CONL":
                self.indi[self.num].confirmation = self.__get_ordinance()
            elif self.tag == "ENDL":
                self.indi[self.num].endowment = self.__get_ordinance()
            elif self.tag == "SLGC":
                self.indi[self.num].sealing_child = self.__get_ordinance()
            elif self.tag == "FAMS":
                self.indi[self.num].fams_num.add(int(self.data[2 : len(self.data) - 1]))
            elif self.tag == "FAMC":
                self.indi[self.num].famc_num.add(int(self.data[2 : len(self.data) - 1]))
            elif self.tag == "_FSFTID":
                self.indi[self.num].fid = self.data
            elif self.tag == "NOTE":
                num = int(self.data[2 : len(self.data) - 1])
                if num not in self.note:
                    self.note[num] = Note(tree=self.tree, num=num)
                self.indi[self.num].notes.add(self.note[num])
            elif self.tag == "SOUR":
                self.indi[self.num].sources.add(self.__get_link_source())
            elif self.tag == "OBJE":
                self.indi[self.num].memories.add(self.__get_memorie())
        self.flag = True

    def __get_fam(self):
        """Parse a family"""
        while self.__get_line() and self.level > 0:
            if self.tag == "HUSB":
                self.fam[self.num].husb_num = int(self.data[2 : len(self.data) - 1])
            elif self.tag == "WIFE":
                self.fam[self.num].wife_num = int(self.data[2 : len(self.data) - 1])
            elif self.tag == "CHIL":
                self.fam[self.num].chil_num.add(int(self.data[2 : len(self.data) - 1]))
            elif self.tag in FACT_TYPES:
                self.fam[self.num].facts.add(self.__get_fact())
            elif self.tag == "SLGS":
                self.fam[self.num].sealing_spouse = self.__get_ordinance()
            elif self.tag == "_FSFTID":
                self.fam[self.num].fid = self.data
            elif self.tag == "NOTE":
                num = int(self.data[2 : len(self.data) - 1])
                if num not in self.note:
                    self.note[num] = Note(tree=self.tree, num=num)
                self.fam[self.num].notes.add(self.note[num])
            elif self.tag == "SOUR":
                self.fam[self.num].sources.add(self.__get_link_source())
        self.flag = True

    def __get_name(self):
        """Parse a name"""
        parts = self.__get_text().split("/")
        name = Name()
        added = False
        name.given = parts[0].strip()
        name.surname = parts[1].strip()
        if parts[2]:
            name.suffix = parts[2]
        if not self.indi[self.num].name:
            self.indi[self.num].name = name
            added = True
        while self.__get_line() and self.level > 1:
            if self.tag == "NPFX":
                name.prefix = self.data
            elif self.tag == "TYPE":
                if self.data == "aka":
                    self.indi[self.num].aka.add(name)
                    added = True
                elif self.data == "married":
                    self.indi[self.num].married.add(name)
                    added = True
            elif self.tag == "NICK":
                nick = Name()
                nick.given = self.data
                self.indi[self.num].nicknames.add(nick)
            elif self.tag == "NOTE":
                num = int(self.data[2 : len(self.data) - 1])
                if num not in self.note:
                    self.note[num] = Note(tree=self.tree, num=num)
                name.note = self.note[num]
        if not added:
            self.indi[self.num].birthnames.add(name)
        self.flag = True

    def __get_fact(self):
        """Parse a fact"""
        fact = Fact()
        if self.tag != "EVEN":
            fact.type = FACT_TYPES[self.tag]
            fact.value = self.data
        while self.__get_line() and self.level > 1:
            if self.tag == "TYPE":
                fact.type = self.data
            if self.tag == "DATE":
                fact.date = self.__get_text()
            elif self.tag == "PLAC":
                fact.place = self.__get_text()
            elif self.tag == "MAP":
                fact.map = self.__get_map()
            elif self.tag == "NOTE":
                if self.data[:12] == "Description:":
                    fact.value = self.data[13:]
                    continue
                num = int(self.data[2 : len(self.data) - 1])
                if num not in self.note:
                    self.note[num] = Note(tree=self.tree, num=num)
                fact.note = self.note[num]
            elif self.tag == "CONT":
                fact.value += "\n" + self.data
            elif self.tag == "CONC":
                fact.value += self.data
        self.flag = True
        return fact

    def __get_map(self):
        """Parse map coordinates"""
        latitude = None
        longitude = None
        while self.__get_line() and self.level > 3:
            if self.tag == "LATI":
                latitude = self.data
            elif self.tag == "LONG":
                longitude = self.data
        self.flag = True
        return (latitude, longitude)

    def __get_text(self):
        """Parse a multiline text"""
        text = self.data
        while self.__get_line():
            if self.tag == "CONT":
                text += "\n" + self.data
            elif self.tag == "CONC":
                text += self.data
            else:
                break
        self.flag = True
        return text

    def __get_source(self):
        """Parse a source"""
        while self.__get_line() and self.level > 0:
            if self.tag == "TITL":
                self.sour[self.num].title = self.__get_text()
            elif self.tag == "AUTH":
                self.sour[self.num].citation = self.__get_text()
            elif self.tag == "PUBL":
                self.sour[self.num].url = self.__get_text()
            elif self.tag == "REFN":
                self.sour[self.num].fid = self.data
                if self.data in self.tree.sources:
                    self.sour[self.num] = self.tree.sources[self.data]
                else:
                    self.tree.sources[self.data] = self.sour[self.num]
            elif self.tag == "NOTE":
                num = int(self.data[2 : len(self.data) - 1])
                if num not in self.note:
                    self.note[num] = Note(tree=self.tree, num=num)
                self.sour[self.num].notes.add(self.note[num])
        self.flag = True

    def __get_link_source(self):
        """Parse a link to a source"""
        num = int(self.data[2 : len(self.data) - 1])
        if num not in self.sour:
            self.sour[num] = Source(num=num)
        page = None
        while self.__get_line() and self.level > 1:
            if self.tag == "PAGE":
                page = self.__get_text()
        self.flag = True
        return (self.sour[num], page)

    def __get_memorie(self):
        """Parse a memorie"""
        memorie = Memorie()
        while self.__get_line() and self.level > 1:
            if self.tag == "TITL":
                memorie.description = self.__get_text()
            elif self.tag == "FILE":
                memorie.url = self.__get_text()
        self.flag = True
        return memorie

    def __get_note(self):
        """Parse a note"""
        self.note[self.num].text = self.__get_text()
        self.flag = True

    def __get_ordinance(self):
        """Parse an ordinance"""
        ordinance = Ordinance()
        while self.__get_line() and self.level > 1:
            if self.tag == "DATE":
                ordinance.date = self.__get_text()
            elif self.tag == "TEMP":
                ordinance.temple_code = self.data
            elif self.tag == "STAT":
                ordinance.status = ORDINANCES[self.data]
            elif self.tag == "FAMC":
                num = int(self.data[2 : len(self.data) - 1])
                if num not in self.fam:
                    self.fam[num] = Fam(tree=self.tree, num=num)
                ordinance.famc = self.fam[num]
        self.flag = True
        return ordinance

    def __add_id(self):
        """Reset GEDCOM identifiers"""
        for num in self.fam:
            if self.fam[num].husb_num:
                self.fam[num].husb_fid = self.indi[self.fam[num].husb_num].fid
            if self.fam[num].wife_num:
                self.fam[num].wife_fid = self.indi[self.fam[num].wife_num].fid
            for chil in self.fam[num].chil_num:
                self.fam[num].chil_fid.add(self.indi[chil].fid)
        for num in self.indi:
            for famc in self.indi[num].famc_num:
                self.indi[num].famc_fid.add(
                    (self.fam[famc].husb_fid, self.fam[famc].wife_fid)
                )
            for fams in self.indi[num].fams_num:
                self.indi[num].fams_fid.add(
                    (self.fam[fams].husb_fid, self.fam[fams].wife_fid)
                )


# fstogedcom classes and functions
def _(string):
    if string in translations and lang in translations[string]:
        return translations[string][lang]
    return string


class EntryWithMenu(Entry):
    """Entry widget with right-clic menu to copy/cut/paste"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.bind("<Button-3>", self.click_right)

    def click_right(self, event):
        """open menu"""
        menu = Menu(self, tearoff=0)
        try:
            self.selection_get()
            state = "normal"
        except TclError:
            state = "disabled"
        menu.add_command(label=_("Copy"), command=self.copy, state=state)
        menu.add_command(label=_("Cut"), command=self.cut, state=state)
        menu.add_command(label=_("Paste"), command=self.paste)
        menu.post(event.x_root, event.y_root)

    def copy(self):
        """copy in clipboard"""
        self.clipboard_clear()
        text = self.selection_get()
        self.clipboard_append(text)

    def cut(self):
        """move in clipboard"""
        self.copy()
        self.delete("sel.first", "sel.last")

    def paste(self):
        """paste from clipboard"""
        try:
            text = self.selection_get(selection="CLIPBOARD")
            self.insert("insert", text)
        except TclError:
            pass


class FilesToMerge(Treeview):
    """List of GEDCOM files to merge"""

    def __init__(self, master, **kwargs):
        super().__init__(master, selectmode="extended", height=5, **kwargs)
        self.heading("#0", text=_("Files"))
        self.column("#0", width=300)
        self.files = dict()
        self.bind("<Button-3>", self.popup)

    def add_file(self, filename):
        """add a GEDCOM file"""
        if any(f.name == filename for f in self.files.values()):
            messagebox.showinfo(
                _("Error"),
                message=_("File already exist: ") + os.path.basename(filename),
            )
            return
        if not os.path.exists(filename):
            messagebox.showinfo(
                _("Error"), message=_("File not found: ") + os.path.basename(filename)
            )
            return
        file = open(filename, "r", encoding="utf-8")
        new_id = self.insert("", 0, text=os.path.basename(filename))
        self.files[new_id] = file

    def popup(self, event):
        """open menu to remove item"""
        item = self.identify_row(event.y)
        if item:
            menu = Menu(self, tearoff=0)
            menu.add_command(label=_("Remove"), command=self.delete_item(item))
            menu.post(event.x_root, event.y_root)

    def delete_item(self, item):
        """return a function to remove a file"""

        def delete():
            self.files[item].close()
            self.files.pop(item)
            self.delete(item)

        return delete


class Merge(Frame):
    """Merge GEDCOM widget"""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        warning = Label(
            self,
            font=("a", 7),
            wraplength=300,
            justify="center",
            text=_(
                "Warning: This tool should only be used to merge GEDCOM files from this software. "
                "If you use other GEDCOM files, the result is not guaranteed."
            ),
        )
        self.files_to_merge = FilesToMerge(self)
        self.btn_add_file = Button(self, text=_("Add files"), command=self.add_files)
        buttons = Frame(self, borderwidth=20)
        self.btn_quit = Button(buttons, text=_("Quit"), command=self.quit)
        self.btn_save = Button(buttons, text=_("Merge"), command=self.save)
        warning.pack()
        self.files_to_merge.pack()
        self.btn_add_file.pack()
        self.btn_quit.pack(side="left", padx=(0, 40))
        self.btn_save.pack(side="right", padx=(40, 0))
        buttons.pack(side="bottom")

    def add_files(self):
        """open file explorer to pick a file"""
        for filename in filedialog.askopenfilenames(
            title=_("Open"),
            defaultextension=".ged",
            filetypes=(("GEDCOM", ".ged"), (_("All files"), "*.*")),
        ):
            self.files_to_merge.add_file(filename)

    def save(self):
        """merge GEDCOM files"""
        if not self.files_to_merge.files:
            messagebox.showinfo(_("Error"), message=_("Please add GEDCOM files"))
            return

        filename = filedialog.asksaveasfilename(
            title=_("Save as"),
            defaultextension=".ged",
            filetypes=(("GEDCOM", ".ged"), (_("All files"), "*.*")),
        )
        tree = Tree()

        indi_counter = 0
        fam_counter = 0

        # read the GEDCOM data
        for file in self.files_to_merge.files.values():
            ged = Gedcom(file, tree)

            # add informations about individuals
            for num in ged.indi:
                fid = ged.indi[num].fid
                if fid not in tree.indi:
                    indi_counter += 1
                    tree.indi[fid] = Indi(tree=tree, num=indi_counter)
                    tree.indi[fid].tree = tree
                    tree.indi[fid].fid = ged.indi[num].fid
                tree.indi[fid].fams_fid |= ged.indi[num].fams_fid
                tree.indi[fid].famc_fid |= ged.indi[num].famc_fid
                tree.indi[fid].name = ged.indi[num].name
                tree.indi[fid].birthnames = ged.indi[num].birthnames
                tree.indi[fid].nicknames = ged.indi[num].nicknames
                tree.indi[fid].aka = ged.indi[num].aka
                tree.indi[fid].married = ged.indi[num].married
                tree.indi[fid].gender = ged.indi[num].gender
                tree.indi[fid].facts = ged.indi[num].facts
                tree.indi[fid].notes = ged.indi[num].notes
                tree.indi[fid].sources = ged.indi[num].sources
                tree.indi[fid].memories = ged.indi[num].memories
                tree.indi[fid].baptism = ged.indi[num].baptism
                tree.indi[fid].confirmation = ged.indi[num].confirmation
                tree.indi[fid].endowment = ged.indi[num].endowment
                if not (
                    tree.indi[fid].sealing_child and tree.indi[fid].sealing_child.famc
                ):
                    tree.indi[fid].sealing_child = ged.indi[num].sealing_child

            # add informations about families
            for num in ged.fam:
                husb, wife = (ged.fam[num].husb_fid, ged.fam[num].wife_fid)
                if (husb, wife) not in tree.fam:
                    fam_counter += 1
                    tree.fam[(husb, wife)] = Fam(husb, wife, tree, fam_counter)
                    tree.fam[(husb, wife)].tree = tree
                tree.fam[(husb, wife)].chil_fid |= ged.fam[num].chil_fid
                tree.fam[(husb, wife)].fid = ged.fam[num].fid
                tree.fam[(husb, wife)].facts = ged.fam[num].facts
                tree.fam[(husb, wife)].notes = ged.fam[num].notes
                tree.fam[(husb, wife)].sources = ged.fam[num].sources
                tree.fam[(husb, wife)].sealing_spouse = ged.fam[num].sealing_spouse

        # merge notes by text
        tree.notes = sorted(tree.notes, key=lambda x: x.text)
        for i, n in enumerate(tree.notes):
            if i == 0:
                n.num = 1
                continue
            if n.text == tree.notes[i - 1].text:
                n.num = tree.notes[i - 1].num
            else:
                n.num = tree.notes[i - 1].num + 1

        # compute number for family relationships and print GEDCOM file
        tree.reset_num()
        with open(filename, "w", encoding="utf-8") as file:
            tree.print(file)
        messagebox.showinfo(_("Info"), message=_("Files successfully merged"))

    def quit(self):
        """prevent exception on quit during download"""
        super().quit()
        os._exit(1)


class SignIn(Frame):
    """Sign In widget"""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.username = StringVar()
        self.username.set(cache.get("username") or "")
        self.password = StringVar()
        label_username = Label(self, text=_("Username:"))
        entry_username = EntryWithMenu(self, textvariable=self.username, width=30)
        label_password = Label(self, text=_("Password:"))
        entry_password = EntryWithMenu(
            self, show="●", textvariable=self.password, width=30
        )
        label_username.grid(row=0, column=0, pady=15, padx=(0, 5))
        entry_username.grid(row=0, column=1)
        label_password.grid(row=1, column=0, padx=(0, 5))
        entry_password.grid(row=1, column=1)
        entry_username.focus_set()
        entry_username.bind("<Key>", self.enter)
        entry_password.bind("<Key>", self.enter)

    def enter(self, evt):
        """enter event"""
        if evt.keysym in {"Return", "KP_Enter"}:
            self.master.master.command_in_thread(self.master.master.login)()


class StartIndis(Treeview):
    """List of starting individuals"""

    def __init__(self, master, **kwargs):
        super().__init__(
            master, selectmode="extended", height=5, columns=("fid",), **kwargs
        )
        self.heading("#0", text=_("Name"))
        self.column("#0", width=250)
        self.column("fid", width=80)
        self.indis = dict()
        self.heading("fid", text="Id")
        self.bind("<Button-3>", self.popup)

    def add_indi(self, fid):
        """add an individual fid"""
        if not fid:
            return None
        if fid in self.indis.values():
            messagebox.showinfo(_("Error"), message=_("ID already exist"))
            return None
        if not re.match(r"[A-Z0-9]{4}-[A-Z0-9]{3}", fid):
            messagebox.showinfo(
                _("Error"), message=_("Invalid FamilySearch ID: ") + fid
            )
            return None
        fs = self.master.master.master.fs
        data = fs.get_url("/platform/tree/persons/%s" % fid)
        if data and "persons" in data:
            if "names" in data["persons"][0]:
                for name in data["persons"][0]["names"]:
                    if name["preferred"]:
                        self.indis[
                            self.insert(
                                "", 0, text=name["nameForms"][0]["fullText"], values=fid
                            )
                        ] = fid
                        return True
        messagebox.showinfo(_("Error"), message=_("Individual not found"))
        return None

    def popup(self, event):
        """open menu to remove item"""
        item = self.identify_row(event.y)
        if item:
            menu = Menu(self, tearoff=0)
            menu.add_command(label=_("Remove"), command=self.delete_item(item))
            menu.post(event.x_root, event.y_root)

    def delete_item(self, item):
        """return a function to remove a fid"""

        def delete():
            self.indis.pop(item)
            self.delete(item)

        return delete


class Options(Frame):
    """Options form"""

    def __init__(self, master, ordinances=False, **kwargs):
        super().__init__(master, **kwargs)
        self.ancestors = IntVar()
        self.ancestors.set(4)
        self.descendants = IntVar()
        self.spouses = IntVar()
        self.ordinances = IntVar()
        self.contributors = IntVar()
        self.start_indis = StartIndis(self)
        self.fid = StringVar()
        btn = Frame(self)
        entry_fid = EntryWithMenu(btn, textvariable=self.fid, width=16)
        entry_fid.bind("<Key>", self.enter)
        label_ancestors = Label(self, text=_("Number of generations to ascend"))
        entry_ancestors = EntryWithMenu(self, textvariable=self.ancestors, width=5)
        label_descendants = Label(self, text=_("Number of generations to descend"))
        entry_descendants = EntryWithMenu(self, textvariable=self.descendants, width=5)
        btn_add_indi = Button(
            btn, text=_("Add a FamilySearch ID"), command=self.add_indi
        )
        btn_spouses = Checkbutton(
            self,
            text="\t" + _("Add spouses and couples information"),
            variable=self.spouses,
        )
        btn_ordinances = Checkbutton(
            self, text="\t" + _("Add Temple information"), variable=self.ordinances
        )
        btn_contributors = Checkbutton(
            self,
            text="\t" + _("Add list of contributors in notes"),
            variable=self.contributors,
        )
        self.start_indis.grid(row=0, column=0, columnspan=3)
        entry_fid.grid(row=0, column=0, sticky="w")
        btn_add_indi.grid(row=0, column=1, sticky="w")
        btn.grid(row=1, column=0, columnspan=2, sticky="w")
        entry_ancestors.grid(row=2, column=0, sticky="w")
        label_ancestors.grid(row=2, column=1, sticky="w")
        entry_descendants.grid(row=3, column=0, sticky="w")
        label_descendants.grid(row=3, column=1, sticky="w")
        btn_spouses.grid(row=4, column=0, columnspan=2, sticky="w")
        if ordinances:
            btn_ordinances.grid(row=5, column=0, columnspan=3, sticky="w")
        btn_contributors.grid(row=6, column=0, columnspan=3, sticky="w")
        entry_ancestors.focus_set()

    def add_indi(self):
        """add a fid"""
        if self.start_indis.add_indi(self.fid.get()):
            self.fid.set("")

    def enter(self, evt):
        """enter event"""
        if evt.keysym in {"Return", "KP_Enter"}:
            self.add_indi()


class Download(Frame):
    """Main widget"""

    def __init__(self, master, **kwargs):
        super().__init__(master, borderwidth=20, **kwargs)
        self.fs = None
        self.tree = None
        self.logfile = None

        # User information
        self.info_tree = False
        self.start_time = None
        info = Frame(self, borderwidth=10)
        self.info_label = Label(
            info,
            wraplength=350,
            borderwidth=20,
            justify="center",
            font=("a", 10, "bold"),
        )
        self.info_indis = Label(info)
        self.info_fams = Label(info)
        self.info_sources = Label(info)
        self.info_notes = Label(info)
        self.time = Label(info)
        self.info_label.grid(row=0, column=0, columnspan=2)
        self.info_indis.grid(row=1, column=0)
        self.info_fams.grid(row=1, column=1)
        self.info_sources.grid(row=2, column=0)
        self.info_notes.grid(row=2, column=1)
        self.time.grid(row=3, column=0, columnspan=2)

        self.form = Frame(self)
        self.sign_in = SignIn(self.form)
        self.options = None
        self.title = Label(
            self, text=_("Sign In to FamilySearch"), font=("a", 12, "bold")
        )
        buttons = Frame(self)
        self.btn_quit = Button(
            buttons, text=_("Quit"), command=Thread(target=self.quit).start
        )
        self.btn_valid = Button(
            buttons, text=_("Sign In"), command=self.command_in_thread(self.login)
        )
        self.title.pack()
        self.sign_in.pack()
        self.form.pack()
        self.btn_quit.pack(side="left", padx=(0, 40))
        self.btn_valid.pack(side="right", padx=(40, 0))
        info.pack()
        buttons.pack(side="bottom")
        self.pack()
        self.update_needed = False

    def info(self, text):
        """dislay informations"""
        self.info_label.config(text=text)

    def save(self):
        """save the GEDCOM file"""
        filename = filedialog.asksaveasfilename(
            title=_("Save as"),
            defaultextension=".ged",
            filetypes=(("GEDCOM", ".ged"), (_("All files"), "*.*")),
        )
        if not filename:
            return
        with open(filename, "w", encoding="utf-8") as file:
            self.tree.print(file)

    def login(self):
        """log in FamilySearch"""
        global _
        username = self.sign_in.username.get()
        password = self.sign_in.password.get()
        if not (username and password):
            messagebox.showinfo(
                message=_("Please enter your FamilySearch username and password.")
            )
            return
        self.btn_valid.config(state="disabled")
        self.info(_("Login to FamilySearch..."))
        self.logfile = open("download.log", "w", encoding="utf-8")
        self.fs = Session(
            self.sign_in.username.get(),
            self.sign_in.password.get(),
            verbose=True,
            logfile=self.logfile,
            timeout=1,
        )
        if not self.fs.logged:
            messagebox.showinfo(
                _("Error"), message=_("The username or password was incorrect")
            )
            self.btn_valid.config(state="normal")
            self.info("")
            return
        self.tree = Tree(self.fs)
        _ = self.fs._
        self.title.config(text=_("Options"))
        cache.delete("lang")
        cache.add("lang", self.fs.lang)
        cache.delete("username")
        cache.add("username", username)
        url = "/service/tree/tree-data/reservations/person/%s/ordinances" % self.fs.fid
        lds_account = self.fs.get_url(url, {}).get("status") == "OK"
        self.options = Options(self.form, lds_account)
        self.info("")
        self.sign_in.destroy()
        self.options.pack()
        self.master.change_lang()
        self.btn_valid.config(
            command=self.command_in_thread(self.download),
            state="normal",
            text=_("Download"),
        )
        self.options.start_indis.add_indi(self.fs.fid)
        self.update_needed = False

    def quit(self):
        """prevent exception during download"""
        self.update_needed = False
        if self.logfile:
            self.logfile.close()
        super().quit()
        os._exit(1)

    def download(self):
        """download family tree"""
        todo = [
            self.options.start_indis.indis[key]
            for key in sorted(self.options.start_indis.indis)
        ]
        for fid in todo:
            if not re.match(r"[A-Z0-9]{4}-[A-Z0-9]{3}", fid):
                messagebox.showinfo(
                    _("Error"), message=_("Invalid FamilySearch ID: ") + fid
                )
                return
        self.start_time = time.time()
        self.options.destroy()
        self.form.destroy()
        self.title.config(text="FamilySearch to GEDCOM")
        self.btn_valid.config(state="disabled")
        self.info(_("Downloading starting individuals..."))
        self.info_tree = True
        self.tree.add_indis(todo)
        todo = set(todo)
        done = set()
        for i in range(self.options.ancestors.get()):
            if not todo:
                break
            done |= todo
            self.info(_("Downloading %s. of generations of ancestors...") % (i + 1))
            todo = self.tree.add_parents(todo) - done

        todo = set(self.tree.indi.keys())
        done = set()
        for i in range(self.options.descendants.get()):
            if not todo:
                break
            done |= todo
            self.info(_("Downloading %s. of generations of descendants...") % (i + 1))
            todo = self.tree.add_children(todo) - done

        if self.options.spouses.get():
            self.info(_("Downloading spouses and marriage information..."))
            todo = set(self.tree.indi.keys())
            self.tree.add_spouses(todo)
        ordi = self.options.ordinances.get()
        cont = self.options.contributors.get()

        async def download_stuff(loop):
            futures = set()
            for fid, indi in self.tree.indi.items():
                futures.add(loop.run_in_executor(None, indi.get_notes))
                if ordi:
                    futures.add(
                        loop.run_in_executor(None, self.tree.add_ordinances, fid)
                    )
                if cont:
                    futures.add(loop.run_in_executor(None, indi.get_contributors))
            for fam in self.tree.fam.values():
                futures.add(loop.run_in_executor(None, fam.get_notes))
                if cont:
                    futures.add(loop.run_in_executor(None, fam.get_contributors))
            for future in futures:
                await future

        loop = asyncio.get_event_loop()
        self.info(
            _("Downloading notes")
            + ((("," if cont else _(" and")) + _(" ordinances")) if ordi else "")
            + (_(" and contributors") if cont else "")
            + "..."
        )
        loop.run_until_complete(download_stuff(loop))

        self.tree.reset_num()
        self.btn_valid.config(command=self.save, state="normal", text=_("Save"))
        self.info(text=_("Success ! Click below to save your GEDCOM file"))
        self.update_info_tree()
        self.update_needed = False

    def command_in_thread(self, func):
        """command to update widget in a new Thread"""

        def res():
            self.update_needed = True
            Thread(target=self.update_gui).start()
            Thread(target=func).start()

        return res

    def update_info_tree(self):
        """update informations"""
        if self.info_tree and self.start_time and self.tree:
            self.info_indis.config(text=_("Individuals: %s") % len(self.tree.indi))
            self.info_fams.config(text=_("Families: %s") % len(self.tree.fam))
            self.info_sources.config(text=_("Sources: %s") % len(self.tree.sources))
            self.info_notes.config(text=_("Notes: %s") % len(self.tree.notes))
            t = round(time.time() - self.start_time)
            minutes = t // 60
            seconds = t % 60
            self.time.config(
                text=_("Elapsed time: %s:%s") % (minutes, str(seconds).zfill(2))
            )

    def update_gui(self):
        """update widget"""
        while self.update_needed:
            self.update_info_tree()
            self.master.update()
            time.sleep(0.1)


class FStoGEDCOM(Notebook):
    """Main notebook"""

    def __init__(self, master, **kwargs):
        super().__init__(master, width=400, **kwargs)
        self.download = Download(self)
        self.merge = Merge(self)
        self.add(self.download, text=_("Download GEDCOM"))
        self.add(self.merge, text=_("Merge GEDCOMs"))
        self.pack()

    def change_lang(self):
        """update text with user's language"""
        self.tab(self.index(self.download), text=_("Download GEDCOM"))
        self.tab(self.index(self.merge), text=_("Merge GEDCOMs"))
        self.download.btn_quit.config(text=_("Quit"))
        self.merge.btn_quit.config(text=_("Quit"))
        self.merge.btn_save.config(text=_("Merge"))
        self.merge.btn_add_file.config(text=_("Add files"))
