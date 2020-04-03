#!/bin/env python27

from rdflib import ConjunctiveGraph, Namespace
import urllib2, urllib, json, gzip
import os.path, sys, traceback, re
import time, random, math
import itertools, functools
import cPickle as pickle
from collections import defaultdict, Counter
from operator import itemgetter
from hashlib import md5
from StringIO import StringIO
from PIL import Image
from GlycanFormatter import GlycoCTFormat, WURCS20Format, GlycanParseError
from WURCS20MonoFormatter import WURCS20MonoFormat, UnsupportedSkeletonCodeError
from Glycan import Glycan
from memoize import memoize
import atexit


class GlyTouCanCredentialsNotFound(RuntimeError):
    pass


class GlyTouCanRegistrationError(RuntimeError):
    pass

class GlyTouCanRegistrationStatus:

    def __init__(self):
        self._warning = []
        self._error = []
        self._acc = None
        self._seq_type = None
        self._other = []

    def read_msg(self, msg_type, msg):
        if "error" in msg_type:
            self._error.append(msg)
        elif "warning" in msg_type:
            self._warning.append(msg)
        elif "glyconvert" in msg_type:
            self._seq_type = msg
        elif "wurcs2GTCID" in msg_type:
            self._acc = msg.split("/")[-1]
        else:
            self._other.append((msg_type, msg))

    def __str__(self):
        if self._error:
            return "error"
        if self._warning:
            return "warning"
        if self._acc:
            return "registered"
        return "not submitted"

    def accession(self):
        return self._acc

    def seq_type(self):
        return self._seq_type

    def warning(self):
        return self._warning

    def error(self):
        return self._error

    def other_msg(self):
        return self._other

    def has_warning(self):
        return len(self._warning) != 0

    def has_error(self):
        return len(self._error) != 0

    def not_submitted(self):
        if self._error or self._warning or self._acc or self._seq_type:
            return False
        return True

    def submitted(self):
        return not self.not_submitted()


class GlyTouCan(object):
    endpt = 'http://ts.glytoucan.org/sparql'
    substr_endpt = 'http://test.ts.glytoucan.org/sparql'
    api = 'https://api.glytoucan.org/'
    cachefile = ".gtccache"

    def __init__(self, user=None, apikey=None, usecache=False):

        self.g = None
        self.ssg = None
        self.opener = None
        self.user = user
        self.apikey = apikey
        self.delaytime = .2
        self.delaybatch = 1
        self.maxattempts = 3
        self._lastrequesttime = 0
        self._lastrequestcount = 0
        self.alphamap = None
        self.glycoct_format = None
        self.wurcs_format = None
        self.wurcs_mono_format = None
        self.usecache = usecache
        self.cachedata = None
        self.cacheupdated = False
        if self.usecache:
            if os.path.exists(self.cachefile):
                try:
                    self.cachedata = pickle.load(gzip.open(self.cachefile, 'rb'))
                    # print >>sys.stderr, "Loaded cached data"
                except:
                    self.cachedata = {}
            else:
                self.cachedata = {}
            atexit.register(self.__del__)

    def __del__(self):
        if self.cacheupdated:
            try:
                wh = gzip.open(self.cachefile, 'wb')
                pickle.dump(self.cachedata, wh, -1)
                wh.close()
                # print >>sys.stderr, "Saved cached data"
                self.cacheupdated = False
            except:
                pass

    def cachegetmany(self, valuekey, acc, iterable):
        if valuekey not in self.cachedata:
            self.cachedata[valuekey] = reduce(lambda d, x: d.setdefault(x[0], []).append(x[1]) or d,
                                              iterable, {})
            self.cacheupdated = True
        return self.cachedata[valuekey].get(acc, [])

    def cacheget(self, valuekey, acc, iterable):
        if valuekey not in self.cachedata:
            self.cachedata[valuekey] = dict(iterable)
            self.cacheupdated = True
        return self.cachedata[valuekey].get(acc)

    def cache_haskey(self, valuekey):
        return (valuekey in self.cachedata)

    def cachegetall(self, valuekey):
        return self.cachedata[valuekey].iteritems()

    def setup_sparql(self):
        self.g = ConjunctiveGraph(store='SPARQLStore')
        self.g.open(self.endpt)

    def setup_substr_sparql(self):
        self.ssg = ConjunctiveGraph(store='SPARQLStore')
        self.ssg.open(self.substr_endpt)

    def setup_api(self, user=None, apikey=None):
        if user == None:
            user = self.user
            apikey = self.apikey
        if user == None:
            user, apikey = self.getcredentials()
        # print user,apikey
        self.password_mgr = urllib2.HTTPPasswordMgrWithDefaultRealm()
        self.password_mgr.add_password(None, self.api, user, apikey)
        self.handler = urllib2.HTTPBasicAuthHandler(self.password_mgr)
        self.opener = urllib2.build_opener(self.handler)

    def _wait(self, delaytime=None):
        if delaytime != None:
            time.sleep(delaytime)
            return
        if (self._lastrequestcount % self.delaybatch) == 0 and self._lastrequestcount > 0:
            time.sleep(self.delaytime)
        self._lastrequesttime = time.time()
        self._lastrequestcount += 1

    @memoize()
    def query(self, sparql, substr=False):
        # print >>sys.stderr, sparql
        self._wait()
        if substr:
            if self.ssg == None:
                self.setup_substr_sparql()
        else:
            if self.g == None:
                self.setup_sparql()

        attempt = 0
        response = None
        while response == None and attempt < self.maxattempts:
            try:
                attempt += 1
                if substr:
                    response = self.ssg.query(sparql)
                else:
                    response = self.g.query(sparql)
            except:
                traceback.print_exc()
                self._wait(self.delaytime ** attempt)

        if response == None:
            raise IOError("Cannot query SPARQL endpoint")

        return response

    # Partition query based on GlyTouCan Accessions
    def query_partition(self, q):
        for i in range(10):
            prefix = "^G%s" % i
            query = q % prefix
            # print query
            yield self.query(query)

    exists_sparql = """
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	
	SELECT ?Saccharide
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id "%(accession)s"
	}
    """

    def exists(self, accession):
        response = self.query(self.exists_sparql % dict(accession=accession))
        for row in response.bindings:
            return True
        return False

    getseq_sparql = """
	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	
	SELECT DISTINCT ?Sequence
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id "%(accession)s" .
   	    ?Saccharide glycan:has_glycosequence ?GlycoSequence .
   	    ?GlycoSequence glycan:has_sequence ?Sequence .
   	    ?GlycoSequence glycan:in_carbohydrate_format glycan:carbohydrate_format_%(format)s .
	}
    """

    def getseq(self, accession, format="wurcs"):
        assert (format in ("wurcs", "glycoct", "iupac_extended", "iupac_condensed"))

        if self.usecache:
            return self.cacheget('seq', (accession, format),
                                 itertools.imap(lambda t: ((t[0], t[1]), t[2]), self.allseq()))

        response = self.query(self.getseq_sparql % dict(accession=accession, format=format))

        seqkey = response.vars[0]
        seq = None
        for row in response.bindings:
            seq = str(row[seqkey].strip())
            seq = re.sub(r'\n\n+', r'\n', seq)
            break
        return seq

    allseq_sparql_old = """
    	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
    	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>

    	SELECT DISTINCT ?Accession ?Format ?Sequence
    	WHERE {
       	    ?Saccharide glytoucan:has_primary_id ?Accession .
       	    ?Saccharide glycan:has_glycosequence ?GlycoSequence .
       	    ?GlycoSequence glycan:has_sequence ?Sequence .
       	    ?GlycoSequence glycan:in_carbohydrate_format ?Format
    	}
        """

    allseq_sparql = """
    	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
    	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>

    	SELECT DISTINCT ?Accession ?Format ?Sequence
    	WHERE {
       	    ?Saccharide glytoucan:has_primary_id ?Accession .
       	    ?Saccharide glycan:has_glycosequence ?GlycoSequence .
       	    ?GlycoSequence glycan:has_sequence ?Sequence .
       	    ?GlycoSequence glycan:in_carbohydrate_format ?Format
       	    
       	    FILTER regex(?Accession, "%s")
    	}
        """

    def allseq(self):
        if self.usecache and self.cache_haskey('seq'):
            for it in self.cachegetall('seq'):
                yield it
            raise StopIteration
        # response = self.query(self.allseq_sparql)
        response_list = self.query_partition(self.allseq_sparql)
        for response in response_list:
            for row in response.bindings:
                acc, format, seq = tuple(map(str, map(row.get, response.vars)))
                seq = re.sub(r'\n\n+', r'\n', seq)
                format = format.rsplit('/', 1)[1].split('_', 2)[-1]
                yield acc, format, seq

    allmass_sparql_old = """
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	
	SELECT ?accession ?mass
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id ?accession .
   	    ?Saccharide glytoucan:has_derivatized_mass ?mass
	}
    """

    allmass_sparql = """
    	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>

    	SELECT ?accession ?mass
    	WHERE {
       	    ?Saccharide glytoucan:has_primary_id ?accession .
       	    ?Saccharide glytoucan:has_derivatized_mass ?mass
       	    
       	    FILTER regex(?accession, "%s")
    	}
        """

    def allmass(self):
        if self.usecache and self.cache_haskey('mass'):
            for it in self.cachegetall('mass'):
                yield it
            raise StopIteration
        # response = self.query(self.allmass_sparql)
        response_list = self.query_partition(self.allmass_sparql)
        for response in response_list:
            acckey = response.vars[0]
            mwkey = response.vars[1]
            mass = None
            for row in response.bindings:
                try:
                    accval = str(row[acckey])
                    mwval = float(row[mwkey].split('/')[-1])
                except (TypeError, ValueError):
                    continue
                yield accval, mwval

    hasmass_sparql = """
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	
	SELECT DISTINCT ?accession ?mass
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id ?accession .
   	    ?Saccharide glytoucan:has_derivatized_mass ?mass .
            FILTER(regex(str(?mass),"/%(valueregex)s"))
	}
    """

    def hasmass(self, target, precision=2):
        target = float(target)
        scale = 10.0 ** precision;
        value = round(target, precision)
        value1 = value - (1.0 / scale)
        # valueregex = str(value)
        valueregex = '(%s|%s)' % (value, value1)
        backslash = '\\'
        valueregex = valueregex.replace('.', backslash * 2 + '.')
        # print valueregex

        response = self.query(self.hasmass_sparql % dict(valueregex=valueregex))
        for row in response.bindings:
            acc, mass = tuple(map(str, map(row.get, response.vars)))
            try:
                mass = float(mass.split('/')[-1])
            except (TypeError, ValueError):
                continue
            if round(mass, precision) == value:
                yield acc

    getmass_sparql = """
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	
	SELECT ?mass
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id "%(accession)s" .
   	    ?Saccharide glytoucan:has_derivatized_mass ?mass
	}
    """

    def getmass(self, accession):

        if self.usecache:
            return self.cacheget('mass', accession, self.allmass())

        response = self.query(self.getmass_sparql % dict(accession=accession))
        masskey = response.vars[0]
        mass = None
        for row in response.bindings:
            try:
                mass = float(row[masskey].split('/')[-1])
                break
            except (TypeError, ValueError):
                pass
        return mass

    allmonocount_sparql_old = """
	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX wurcs: <http://www.glycoinfo.org/glyco/owl/wurcs#>
	
	SELECT ?accession ?cnt
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id ?accession .
   	    ?Saccharide glycan:has_glycosequence ?GlycoSequence .
   	    ?GlycoSequence glycan:in_carbohydrate_format glycan:carbohydrate_format_wurcs . 
	    ?GlycoSequence wurcs:RES_count ?cnt
	}
    """

    allmonocount_sparql = """
    	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
    	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
    	PREFIX wurcs: <http://www.glycoinfo.org/glyco/owl/wurcs#>

    	SELECT ?accession ?cnt
    	WHERE {
       	    ?Saccharide glytoucan:has_primary_id ?accession .
       	    ?Saccharide glycan:has_glycosequence ?GlycoSequence .
       	    ?GlycoSequence glycan:in_carbohydrate_format glycan:carbohydrate_format_wurcs . 
    	    ?GlycoSequence wurcs:RES_count ?cnt
    	    
    	    FILTER regex(?accession, "%s")
    	}
        """

    def allmonocount(self):

        response_list = self.query_partition(self.allmonocount_sparql)
        for response in response_list:
            #response = self.query(self.allmonocount_sparql)
            acckey = response.vars[0]
            cntkey = response.vars[1]
            for row in response.bindings:
                try:
                    accval = str(row[acckey])
                    cntval = int(row[cntkey])
                except (TypeError, ValueError):
                    continue
                yield accval, cntval

    getmonocount_sparql = """
	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX wurcs: <http://www.glycoinfo.org/glyco/owl/wurcs#>
	
	SELECT ?cnt
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id "%(accession)s" .
   	    ?Saccharide glycan:has_glycosequence ?GlycoSequence .
   	    ?GlycoSequence glycan:in_carbohydrate_format glycan:carbohydrate_format_wurcs . 
	    ?GlycoSequence wurcs:RES_count ?cnt
	}
    """

    def getmonocount(self, accession):

        if self.usecache:
            return self.cacheget('monocnt', accession, self.allmonocount())

        response = self.query(self.getmonocount_sparql % dict(accession=accession))
        key = response.vars[0]
        value = None
        for row in response.bindings:
            try:
                value = int(row[key])
                break
            except (TypeError, ValueError):
                pass
        return value

    def getimage(self, accession, notation="cfg", style="extended", avoidcache=False, trials=1):
        assert (notation in ("cfg","snfg") and style in ("extended",))
        self._wait()
        if trials > 1:
            avoidcache = True
        imgcnt = Counter()
        hash2img = dict()
        for t in range(trials):
            rand = ""
            if avoidcache:
                rand = "&rand=" + ("%.8f" % (random.random(),)).split('.', 1)[1]
            try:
                imgstr = urllib.urlopen("https://glytoucan.org/glycans/%s/image?format=png&notation=%s&style=%s%s" % (
                                        accession, notation, style, rand)).read()
                if len(imgstr) == 0:
                    imgstr = None
            except IOError:
                imgstr = None
            if imgstr != None:
                imghash = md5(imgstr).hexdigest().lower()
                imgcnt[imghash] += 1
                hash2img[imghash] = imgstr
        # print imgcnt
        if len(imgcnt) == 0:
            return None, None, None
        imgstr = hash2img[max(imgcnt.items(), key=itemgetter(1))[0]]
        pngh = StringIO(imgstr)
        try:
            pngimg = Image.open(pngh)
        except IOError:
            return imgstr, None, None
        width, height = pngimg.size
        return imgstr, width, height

    allmotif_sparql = """
	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX rdfs: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
	PREFIX rdf: <http://www.w3.org/2000/01/rdf-schema#>
	
	SELECT DISTINCT ?primary_id ?label ?redend
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id ?primary_id . 
	    ?Saccharide rdfs:type glycan:glycan_motif . 
	    ?Saccharide rdf:label ?label .
	    ?Saccharide glytoucan:is_reducing_end ?redend
	}
    """

    def allmotifs(self):
        response = self.query(self.allmotif_sparql)
        for row in response.bindings:
            yield tuple(map(str, map(row.get, response.vars)))

    getmotif_sparql = """
	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX rdfs: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
	PREFIX rdf: <http://www.w3.org/2000/01/rdf-schema#>
	
	SELECT DISTINCT ?motif_id
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id "%(accession)s" . 
	    ?Saccharide glycan:has_motif ?Motif .
	    ?Motif rdfs:type glycan:glycan_motif . 
   	    ?Motif glytoucan:has_primary_id ?motif_id
	}
    """

    def getmotif(self, accession):

        if self.usecache:
            return self.cachegetmany('motif', accession, self.allmotifaligns())

        response = self.query(self.getmotif_sparql % dict(accession=accession))
        return map(lambda row: str(row.get(response.vars[0])), response.bindings)

    allmotifaligns_sparql_old = """
	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX rdfs: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
	PREFIX rdf: <http://www.w3.org/2000/01/rdf-schema#>
	
	SELECT DISTINCT ?acc ?motif_id 
	WHERE {
   	    ?Saccharide glytoucan:has_primary_id ?acc . 
	    ?Saccharide glycan:has_motif ?Motif .
	    ?Motif rdfs:type glycan:glycan_motif . 
   	    ?Motif glytoucan:has_primary_id ?motif_id
	}
    """

    allmotifaligns_sparql = """
    	PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
    	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
    	PREFIX rdfs: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    	PREFIX rdf: <http://www.w3.org/2000/01/rdf-schema#>

    	SELECT DISTINCT ?acc ?motif_id 
    	WHERE {
       	    ?Saccharide glytoucan:has_primary_id ?acc . 
    	    ?Saccharide glycan:has_motif ?Motif .
    	    ?Motif rdfs:type glycan:glycan_motif . 
       	    ?Motif glytoucan:has_primary_id ?motif_id

       	    FILTER regex(?acc, "%s")
    	}
        """

    def allmotifaligns(self):
        # response = self.query(self.allmotifaligns_sparql)
        response_list = self.query_partition(self.allmotifaligns_sparql)
        for response in response_list:
            for row in response.bindings:
                yield tuple(map(str, map(row.get, response.vars)))

    credfile = ".gtccred"

    @staticmethod
    def getcredentials():

        # script directory
        dir = os.path.split(sys.argv[0])[0]
        credfile = os.path.join(dir, GlyTouCan.credfile)
        if os.path.exists(credfile):
            user, apikey = open(credfile).read().split()
            return user, apikey

        # local directory
        credfile = GlyTouCan.credfile
        if os.path.exists(credfile):
            user, apikey = open(credfile).read().split()
            return user, apikey

        # home directory
        dir = os.path.expanduser("~")
        credfile = os.path.join(dir, GlyTouCan.credfile)
        if os.path.exists(credfile):
            user, apikey = open(credfile).read().split()
            return user, apikey

        raise GlyTouCanCredentialsNotFound()

    def fixcompwurcs(self, wurcsseq):
        if not self.alphamap:
            self.alphamap = dict()
            for i, c in enumerate(range(ord('a'), ord('z') + 1)):
                self.alphamap[i + 1] = chr(c)
                self.alphamap[chr(c)] = (i + 1)
            for i, c in enumerate(range(ord('A'), ord('Z') + 1)):
                self.alphamap[i + 1 + 26] = chr(c)
                self.alphamap[chr(c)] = (i + 1 + 26)
        prefix, counts, rest = wurcsseq.split('/', 2)
        unodes, nodes, edges = counts.split(',')
        nodes = int(nodes)
        assert '0+' in edges
        edges = (nodes - 1)
        ambignode = "|".join(map(lambda i: "%s?" % (self.alphamap[i],), range(1, nodes + 1)))
        ambigedge = "%s}-{%s" % (ambignode, ambignode)
        ambigedges = [ambigedge] * edges
        return "%s/%s,%d,%d/%s%s" % (prefix, unodes, nodes, edges, rest, "_".join(ambigedges))

    def monotocomp(self, mstr):
        m = re.search(r'-(\d)[abx]_\d-(\d|\?)', mstr)
        if m:
            mstr = re.sub(r'-\d[abx]_\d-(\d|\?)', '', mstr)
            assert mstr[int(m.group(1)) - 1] == 'a', mstr
            mstr = list(mstr)
            if m.group(1) == '2':
                mstr[1] = 'U'
            else:
                mstr[0] = 'u'
            mstr = "".join(mstr)
        return mstr

    def monotobasecomp(self, mstr):
        mstr = self.monotocomp(mstr)
        skelplus = mstr.split('_', 1)
        skelplus[0] = re.sub(r'[1234]', 'x', skelplus[0])
        return "_".join(skelplus)

    def wurcscomptrans(self, wurcsseq, monotrans):
        prefix, counts, rest = wurcsseq.split('/', 2)
        monos, rest = rest.split(']/', 1)
        ids, rest = rest.split('/', 1)
        origfreq = defaultdict(int)
        for id in ids.split('-'):
            origfreq[int(id) - 1] += 1
        monolist = monos.lstrip('[').split('][')
        # print monolist
        newmonolist = []
        for mstr in monolist:
            newmonolist.append(monotrans(mstr))
        freq = defaultdict(int)
        for i, mstr in enumerate(newmonolist):
            freq[mstr] += origfreq[i]
        # print freq
        uniq = len(freq)
        total = sum(freq.values())
        newmonolist = sorted(freq, key=lambda k: newmonolist.index(k))
        # print newmonolist
        counts = "%d,%d,0+" % (uniq, total)
        monostr = "".join(map(lambda s: "[%s]" % (s,), newmonolist))
        ids = []
        for i, mstr in enumerate(newmonolist):
            ids.extend([str(i + 1)] * freq[mstr])
        idstr = '-'.join(ids)
        theseq = self.fixcompwurcs("%s/%s/%s/%s/" % (prefix, counts, monostr, idstr))
        # acc,new = self.register(theseq)
        # return self.getseq(acc,'wurcs')
        return theseq

    def makecompwurcs(self, wurcsseq):
        return self.wurcscomptrans(wurcsseq, self.monotocomp)

    def makebasecompwurcs(self, wurcsseq):
        return self.wurcscomptrans(wurcsseq, self.monotobasecomp)

    def haspage(self, accession):
        req = urllib2.Request('https://glytoucan.org/Structures/Glycans/%s' % (accession,))
        self._wait()
        handle = urllib2.urlopen(req)
        # print handle.getcode()
        # print handle.info()
        page = handle.read()
        m = re.search(r'<title>(.*)</title>', page)
        if m and m.group(1).startswith('Accession Number'):
            return True
        return False

    gettopo_sparql = """
        PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	
	SELECT DISTINCT ?topo
	WHERE {
	  {
   	    ?Saccharide glytoucan:has_primary_id "%(accession)s" .
	    ?Saccharide relation:has_topology ?topo
          } UNION {
	    ?Saccharide relation:has_topology glytoucanacc:%(accession)s .
	    ?Saccharide relation:has_topology ?topo
          }
	}
    """

    def gettopo(self, accession):

        if self.usecache:
            return self.cacheget('topo', accession, self.alltopo())

        response = self.query(self.gettopo_sparql % dict(accession=accession))
        key = response.vars[0]
        value = None
        for row in response.bindings:
            value = str(row[key])
            break
        if not value:
            return None
        return value.rsplit('/', 1)[1]

    alltopo_sparql = """
	PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
	
	SELECT DISTINCT ?sacc ?topo
	WHERE {
	    ?sacc relation:has_topology ?topo
	}
    """

    def alltopo(self):
        response = self.query(self.alltopo_sparql)
        seen = set()
        for row in response.bindings:
            t = map(lambda uri: str(uri).rsplit('/', 1)[1], map(row.get, response.vars))
            if (t[0], t[1]) not in seen:
                yield t[0], t[1]
                seen.add((t[0], t[1]))
            if (t[1], t[1]) not in seen:
                yield t[1], t[1]
                seen.add((t[0], t[1]))

    hastopo_sparql = """
    PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	
	SELECT DISTINCT ?acc
	WHERE {
          {
   	    ?Saccharide glytoucan:has_primary_id ?acc .
	    ?Saccharide relation:has_topology glytoucanacc:%(accession)s
          } UNION {
	    ?Saccharide relation:has_topology glytoucanacc:%(accession)s .
	    ?Saccharide relation:has_topology ?topo .
	    ?topo glytoucan:has_primary_id ?acc
          }
	}
    """

    def hastopo(self, accession):
        response = self.query(self.hastopo_sparql % dict(accession=accession))
        key = response.vars[0]
        for row in response.bindings:
            yield str(row[key])

    getcomp_sparql = """
	PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	
	SELECT DISTINCT ?comp
	WHERE {
	  {
   	    ?topo glytoucan:has_primary_id "%(topo)s" .
	    ?topo relation:has_composition ?comp
          } UNION {
	    ?Saccharide relation:has_composition glytoucanacc:%(accession)s .
	    ?Saccharide relation:has_composition ?comp
	  }
	}
    """

    def getcomp(self, accession):

        if self.usecache:
            return self.cacheget('comp', accession, self.allcomp())

        topo = self.gettopo(accession)
        response = self.query(self.getcomp_sparql % dict(accession=accession, topo=topo))
        key = response.vars[0]
        value = None
        for row in response.bindings:
            value = str(row[key])
            break
        if not value:
            return None
        return value.rsplit('/', 1)[1]

    allcomp_sparql = """
	PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	
	SELECT DISTINCT ?topo ?comp
	WHERE {
	    ?topo relation:has_composition_with_linkage ?comp
	}
    """

    def allcomp(self):
        topo = defaultdict(set)
        for s, t in self.alltopo():
            topo[t].add(s)
        response = self.query(self.allcomp_sparql)
        seen = set()
        for row in response.bindings:
            t, c = map(lambda uri: str(uri).rsplit('/', 1)[1], map(row.get, response.vars))
            for s in topo[t]:
                if (s, c) not in seen:
                    yield s, c
                    seen.add((s, c))
                if (c, c) not in seen:
                    yield c, c
                    seen.add((c, c))

    hascomp_sparql = """
    PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	
	SELECT DISTINCT ?acc
	WHERE {
          {
   	    ?Saccharide glytoucan:has_primary_id ?acc .
	    ?Saccharide relation:has_topology ?topo .
	    ?topo relation:has_composition_with_linkage glytoucanacc:%(accession)s
          } UNION {
   	    ?topo glytoucan:has_primary_id ?acc .
	    ?topo relation:has_composition_with_linkage glytoucanacc:%(accession)s
          } UNION {
	    ?Saccharide relation:has_composition_with_linkage glytoucanacc:%(accession)s .
	    ?Saccharide relation:has_composition_with_linkage ?comp .
	    ?comp glytoucan:has_primary_id ?acc
          }
	}
    """

    def hascomp(self, accession):
        response = self.query(self.hascomp_sparql % dict(accession=accession))
        key = response.vars[0]
        for row in response.bindings:
            yield str(row[key])

    allbasecomp_sparql = """
	PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	
	SELECT DISTINCT ?comp ?bcomp
	WHERE {
	    ?comp relation:has_base_composition ?bcomp
	}
    """

    def allbasecomp(self):
        comp = defaultdict(set)
        for s, c in self.allcomp():
            comp[c].add(s)
        response = self.query(self.allbasecomp_sparql)
        seen = set()
        for row in response.bindings:
            c, bc = map(lambda uri: str(uri).rsplit('/', 1)[1], map(row.get, response.vars))
            for s in comp[c]:
                if (s, bc) not in seen:
                    yield s, bc
                    seen.add((s, bc))
                if (bc, bc) not in seen:
                    yield bc, bc
                    seen.add((bc, bc))

    getbasecomp_sparql = """
	PREFIX glytoucanacc: <http://rdf.glycoinfo.org/glycan/>
	PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
	PREFIX relation:  <http://www.glycoinfo.org/glyco/owl/relation#>
	
	SELECT DISTINCT ?bcomp
	WHERE {
	  {
   	    ?comp glytoucan:has_primary_id "%(comp)s" .
	    ?comp relation:has_base_composition ?bcomp
          } UNION {
	    ?Saccharide relation:has_base_composition glytoucanacc:%(accession)s .
	    ?Saccharide relation:has_base_composition ?bcomp
	  }
	}
    """

    def getbasecomp(self, accession):

        if self.usecache:
            return self.cacheget('basecomp', accession, self.allbasecomp())

        comp = self.getcomp(accession)
        response = self.query(self.getbasecomp_sparql % dict(accession=accession, comp=comp))
        key = response.vars[0]
        value = None
        for row in response.bindings:
            value = str(row[key])
            break
        if not value:
            return None
        return value.rsplit('/', 1)[1]

    @staticmethod
    def intstrsortkey(value):
        if len(value) == 1:
            try:
                return (int(value[0]), "")
            except:
                pass
            return (1e+20, value[0])
        try:
            return (value[0], int(value[1]), "")
        except:
            pass
        return (value[0], 1e+20, value[1])

    getcrossrefs_sparql = """
        PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
        PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>

        SELECT ?gdb
        WHERE {
             ?saccharide glytoucan:has_primary_id "%(accession)s" .
	     ?saccharide a glycan:saccharide . 
             ?saccharide glycan:has_resource_entry ?gdb
        } 
    """
    resources = ['glycosciences_de', 'pubchem', 'kegg', 'unicarbkb', 'glyconnect', 'glycome-db', 'unicarb-db',
                 'carbbank', 'pdb', 'cfg', 'bcsdb']

    def getcrossrefs(self, accession, resource=None):
        assert resource in [None] + self.resources

        if self.usecache:
            key = 'crossrefs'
            if resource != None:
                key += (":" + resource)
            return self.cachegetmany(key, accession, self.allcrossrefs())

        response = self.query(self.getcrossrefs_sparql % dict(accession=accession))
        key = response.vars[0]
        xrefs = []
        for row in response.bindings:
            xref = str(row[key]).rsplit('/', 2)[-2:]
            if resource != None and xref[0] == resource:
                xrefs.append(tuple(xref[1:]))
            elif resource == None and xref[0] in self.resources:
                xrefs.append(tuple(xref))
        return map(lambda t: ":".join(t), sorted(set(xrefs), key=self.intstrsortkey))

    allcrossrefs_sparql = """
        PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
        PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>

        SELECT DISTINCT ?acc ?gdb
        WHERE {
             ?saccharide glytoucan:has_primary_id ?acc .
	     ?saccharide a glycan:saccharide . 
             ?saccharide glycan:has_resource_entry ?gdb
        } 
    """

    def allcrossrefs(self, resource=None):
        assert resource in [None] + self.resources
        response = self.query(self.allcrossrefs_sparql)
        for row in response.bindings:
            acc, xref = map(row.get, response.vars)
            acc = str(acc)
            xref = str(xref).rsplit('/', 2)[-2:]
            if resource != None and xref[0] == resource:
                yield (acc, xref[1])
            elif resource == None and xref[0] in self.resources:
                yield (acc, ":".join(xref))

    getrefs_sparql = """
        PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
        PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
        PREFIX dcterms: <http://purl.org/dc/terms/>
        
        SELECT ?ref
        WHERE {
            ?Saccharide glytoucan:has_primary_id "%(accession)s" .
	    ?Saccharide a glycan:saccharide . 
            ?Saccharide dcterms:references ?ref
        }
    """

    def getrefs(self, accession):
        if self.usecache:
            key = 'references'
            return self.cachegetmany(key, accession, self.allrefs())
        response = self.query(self.getrefs_sparql % dict(accession=accession))
        key = response.vars[0]
        refs = []
        for row in response.bindings:
            refs.append(str(row[key]).rsplit('/', 1)[1])
        return sorted(set(refs), key=int)

    getallrefs_sparql = """
        PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
        PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
        PREFIX dcterms: <http://purl.org/dc/terms/>
        
        SELECT ?acc ?ref
        WHERE {
            ?Saccharide glytoucan:has_primary_id ?acc .
	    ?Saccharide a glycan:saccharide . 
            ?Saccharide dcterms:references ?ref
        }
    """

    def allrefs(self):
        response = self.query(self.getallrefs_sparql)
        for row in response.bindings:
            yield tuple(map(lambda s: s.rsplit('/')[-1], map(row.get, response.vars)))

    gettaxa_sparql = """
    PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
    PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

    SELECT DISTINCT ?taxon
    WHERE {
       {
         ?saccharide glytoucan:has_primary_id "%(accession)s" .
         ?saccharide a glycan:saccharide .
         ?saccharide skos:exactMatch ?gdb .
         ?gdb glycan:has_reference ?ref .
         ?ref glycan:is_from_source ?taxon
       } UNION {
         ?saccharide glytoucan:has_primary_id "%(accession)s" .
         ?saccharide a glycan:saccharide .
         ?saccharide glycan:is_from_source ?taxon
       }
    }
    """

    def gettaxa(self, accession):
        if self.usecache:
            key = 'taxa'
            return self.cachegetmany(key, accession, self.alltaxa())
        response = self.query(self.gettaxa_sparql % dict(accession=accession))
        key = response.vars[0]
        taxa = []
        for row in response.bindings:
            try:
                taxid = int(str(row[key]).rsplit('/', 1)[1])
                taxa.append(str(taxid))
            except ValueError:
                pass
        return sorted(set(taxa), key=int)

    getalltaxa_sparql = """
    PREFIX glycan: <http://purl.jp/bio/12/glyco/glycan#>
    PREFIX glytoucan: <http://www.glytoucan.org/glyco/owl/glytoucan#>
    PREFIX skos: <http://www.w3.org/2004/02/skos/core#>

    SELECT DISTINCT ?acc ?taxon
    WHERE {
       {
         ?saccharide glytoucan:has_primary_id ?acc .
         ?saccharide a glycan:saccharide .
         ?saccharide skos:exactMatch ?gdb .
         ?gdb glycan:has_reference ?ref .
         ?ref glycan:is_from_source ?taxon
       } UNION {
         ?saccharide glytoucan:has_primary_id ?acc .
         ?saccharide a glycan:saccharide .
         ?saccharide glycan:is_from_source ?taxon
       }
    }
    """

    def alltaxa(self):
        response = self.query(self.getalltaxa_sparql)
        for row in response.bindings:
            vals = map(lambda s: s.rsplit('/')[-1], map(row.get, response.vars))
            try:
                dummy = int(vals[1])
            except ValueError:
                continue
            yield tuple(vals)

    def getsubstr(self, acc):
        sparql = self.getsubstr_sparql(acc)
        if sparql:
            sparql = """
                PREFIX foaf:<http://xmlns.com/foaf/0.1/>
                PREFIX glycan:<http://purl.jp/bio/12/glyco/glycan#>
                PREFIX glytoucan:<http://www.glytoucan.org/glyco/owl/glytoucan#>
                PREFIX wurcs:<http://www.glycoinfo.org/glyco/owl/wurcs#>
                SELECT DISTINCT ?acc
		WHERE {
                  {
		    SELECT DISTINCT ?acc
                    %(from)s
                    WHERE {
                      %(where)s
                      ?glycan glytoucan:has_primary_id ?acc
                    }
	            ORDER BY ?acc
	          }
                }
                """ % sparql
            response = self.query(sparql, substr=True)
            for r in response.bindings:
                yield r.get(response.vars[0])

    def getsubstr_sparql(self, acc):
        seq = self.getseq(acc, 'wurcs')
        if not seq:
            return None
        params = dict(sequence=seq, format='wurcs')

        if not self.opener:
            self.setup_api()

        req = urllib2.Request(self.api + 'glycans/sparql/substructure?' + urllib.urlencode(params))
        req.add_header('Accept', 'application/json')
        try:
            response = None
            self._wait()
            response = json.loads(self.opener.open(req).read())
        except (ValueError, IOError), e:
            self.opener = None
            return None
        return response

    def register_old(self, glycan, user=None, apikey=None):
        if not self.opener:
            self.setup_api(user=user, apikey=apikey)
        if isinstance(glycan, Glycan):
            if not self.glycoct_format:
                self.glycoct_format = GlycoCTFormat()
            sequence = self.glycoct2wurcs(self.glycoct_format.toStr(glycan))
            # sequence = self.glycoct_format.toStr(glycan)
            # print sequence
            if not glycan.has_root():
                acc, new = self.register(sequence)
                # print acc,new
                wurcs = self.getseq(acc)
                # print wurcs
                if '0+' in wurcs:
                    sequence = self.fixcompwurcs(wurcs)
                else:
                    return acc, new
        else:
            if glycan.strip().startswith('RES'):
                glycan = self.glycoct2wurcs(glycan)
            sequence = glycan
        sequence = re.sub(r'\n\n+', r'\n', sequence)
        params = json.dumps(dict(sequence=sequence))
        # print params
        req = urllib2.Request(self.api + 'glycan/register', params)
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json')
        try:
            response = None
            self._wait()
            response = json.loads(self.opener.open(req).read())
            accession = response['message']
        except (ValueError, IOError), e:
            # print traceback.format_exc()
            # print response
            # force reinitialization of opener...
            self.opener = None
            raise GlyTouCanRegistrationError(str(e))
        if response['error'] == "":
            new = True
        else:
            new = False
        return accession, new

    allhash2wurcs_sparql = """
            PREFIX repo: <http://repository.sparqlite.com/terms#>

            SELECT DISTINCT ?hashkey ?WURCSLabel
            WHERE{
              # HashKey
              ?hash_uri ?p_sacc ?sacc_uri.
              BIND(STRAFTER(STR(?hash_uri), "http://repository.sparqlite.com/key#") AS ?hashkey)

              # WURCS
              ?hash_uri ?p_detect "wurcs".
              ?hash_uri repo:input ?WURCSLabel.
            }"""

    hash_and_wurcs = []

    def allhashandwurcs(self):
        # TODO query partition
        response = self.query(self.allhash2wurcs_sparql)
        for row in response.bindings:
            hashkey, wurcs = tuple(map(str, map(row.get, response.vars)))
            self.hash_and_wurcs.append((hashkey, wurcs))

    def wurcs2hash(self, wurcs):
        if not self.hash_and_wurcs:
            self.allhashandwurcs()
        for h, w in self.hash_and_wurcs:
            if w == wurcs:
                return h

    def hash2wurcs(self, hashkey):
        if not self.hash_and_wurcs:
            self.allhashandwurcs()
        for h, w in self.hash_and_wurcs:
            if h == hashkey:
                return w

    status_sparql = """
        PREFIX repo: <http://repository.sparqlite.com/terms#>

        SELECT DISTINCT ?batch_p ?batch_value
        FROM <http://glycosmos.org/structureType>
        FROM <http://glycosmos.org/batch/wurcsvalid>
        FROM <http://glycosmos.org/batch/wurcs/accession>
        FROM <http://glycosmos.org/batch/image>
        FROM <http://glycosmos.org/batch/resource>
        WHERE{
        # HashKey
            VALUES ?HashKey {"%s"}
            BIND(IRI(CONCAT("http://repository.sparqlite.com/key#", ?HashKey)) AS ?hash_uri)
            ?hash_uri ?batch_p ?batch_value.
        }"""

    def status(self, wurcs):
        hashkey = self.wurcs2hash(wurcs)
        if hashkey:
            return self.status_by_hash(hashkey)
        else:
            return GlyTouCanRegistrationStatus()

    def status_by_hash(self, hashkey):
        status = GlyTouCanRegistrationStatus()
        response = self.query(self.status_sparql % hashkey)

        for row in response.bindings:
            msg_type, msg = tuple(map(str, map(row.get, response.vars)))
            status.read_msg(msg_type, msg)
        return status

    def find(self, wurcs):
        status = self.status(wurcs)
        return status.accession()

    def register(self, glycan):
        sequence = self.anyglycan2wurcs(glycan)
        status = self.status(sequence)
        if status.accession():
            return status.accession()

        if status.has_error():
            for e in status.error():
                print >> sys.stderr, e
            raise GlyTouCanRegistrationError()

        if status.not_submitted():
            print "Registering"
            self.register_request(sequence)
        return None

    def anyglycan2wurcs(self, glycan):
        sequence = ""
        if isinstance(glycan, Glycan):
            if not self.glycoct_format:
                self.glycoct_format = GlycoCTFormat()
            sequence = self.glycoct2wurcs(self.glycoct_format.toStr(glycan))
            if '0+' in sequence:
                sequence = self.fixcompwurcs(sequence)
        else:
            sequence = re.sub(r'\n\n+', r'\n', glycan)
            if sequence.strip().startswith('RES'):
                sequence = self.glycoct2wurcs(glycan)
        return sequence

    def register_request(self, sequence):
        if not self.opener:
            self.setup_api()

        params = json.dumps(dict(sequence=sequence))
        # print params
        req = urllib2.Request(self.api + 'glycan/register', params)
        req.add_header('Content-Type', 'application/json')
        req.add_header('Accept', 'application/json')
        try:
            response = None
            self._wait()
            response = json.loads(self.opener.open(req).read())
            accession = response['message']
        except (ValueError, IOError), e:
            # print traceback.format_exc()
            # print response
            # force reinitialization of opener...
            # self.opener = None
            # raise GlyTouCanRegistrationError(str(e))
            pass
        return None

    def glycoct2wurcs(self, seq):
        requestURL = "https://api.glycosmos.org/glycanformatconverter/2.3.2-snapshot/glycoct2wurcs/"
        encodedseq = urllib.quote(seq, safe='')
        requestURL += encodedseq
        req = urllib2.Request(requestURL)
        # self._wait(delaytime=0.5)
        try:
            response = urllib2.urlopen(req).read()
        except urllib2.URLError:
            print "Bad internet connection"
            return None

        result = json.loads(response)

        try:
            wurcs = result["WURCS"]
        except:
            raise ValueError("GlycoCT 2 WURCS conversion failed")

        return wurcs.strip()

    def getUnsupportedSkeletonCodes(self, acc):
        codes = set()
        sequence = self.getseq(acc, 'wurcs')
        if not sequence:
            return codes
        if not self.wurcs_mono_format:
            self.wurcs_mono_format = WURCS20MonoFormat()
        monos = sequence.split('/[', 1)[1].split(']/')[0].split('][')
        for m in monos:
            try:
                g = self.wurcs_mono_format.parsing(m)
            except UnsupportedSkeletonCodeError, e:
                codes.add(e.message.rsplit(None, 1)[-1])
            except GlycanParseError:
                pass
        return codes

    def getGlycan(self, acc, format=None):
        if not format or (format == 'wurcs'):
            sequence = self.getseq(acc, 'wurcs')
            if sequence:
                if not self.wurcs_format:
                    self.wurcs_format = WURCS20Format()
                try:
                    return self.wurcs_format.toGlycan(sequence)
                except GlycanParseError:
                    pass  # traceback.print_exc()
        if not format or (format == 'glycoct'):
            sequence = self.getseq(acc, 'glycoct')
            if sequence:
                if not self.glycoct_format:
                    self.glycoct_format = GlycoCTFormat()
                try:
                    return self.glycoct_format.toGlycan(sequence)
                except GlycanParseError:
                    pass
        return None


if __name__ == "__main__":

    import sys


    def items():
        any = False
        for f in sys.argv[1:]:
            any = True
            yield f.strip()
        if not any:
            for l in sys.stdin:
                yield l.strip()


    cmd = sys.argv.pop(1)

    if cmd.lower() == "register":

        gtc = GlyTouCan()
        for f in items():
            h = open(f);
            sequence = h.read().strip();
            h.close()
            if not sequence:
                continue
            try:
                acc, new = gtc.register(sequence)
                print f, acc, ("new" if new else "")
            except GlyTouCanRegistrationError:
                # traceback.print_exc()
                print f, "-", "error"
            sys.stdout.flush()

    elif cmd.lower() in ("wurcs", "glycoct"):

        gtc = GlyTouCan()
        for acc in items():
            print gtc.getseq(acc, cmd)

    elif cmd.lower() in ("image",):

        gtc = GlyTouCan()
        fmt = "extended"
        for acc in items():
            if acc in ("extended", "compact", "normal"):
                fmt = acc
                continue
            imgstr, width, height = gtc.getimage(acc, style=fmt, trials=5)
            if imgstr:
                print acc, width, height
                wh = open(acc + ".png", 'w')
                wh.write(imgstr)
                wh.close()

    elif cmd.lower() == "summary":

        gtc = GlyTouCan()
        for acc in items():
            print "Exists:", gtc.exists(acc)
            print "WURCS:", bool(gtc.getseq(acc, 'wurcs'))
            print "GlycoCT:", bool(gtc.getseq(acc, 'glycoct'))
            print "KEGG:", ", ".join(gtc.getcrossrefs(acc, 'kegg'))
            print "PubChem:", ", ".join(gtc.getcrossrefs(acc, 'pubchem'))
            print "UniCarbKB:", ", ".join(gtc.getcrossrefs(acc, 'unicarbkb'))
            print "XRefs:", ", ".join(gtc.getcrossrefs(acc))
            print "Taxa:", ", ".join(gtc.gettaxa(acc))
            print "Motif:", ", ".join(gtc.getmotif(acc))
            print "Mass:", gtc.getmass(acc)
            print "Topology:", gtc.gettopo(acc)
            print "Composition:", gtc.getcomp(acc)
            print "BaseComposition:", gtc.getbasecomp(acc)
            print "Has Topology:", ", ".join(gtc.hastopo(acc))
            print "Has Composition:", ", ".join(gtc.hascomp(acc))
            imgstr, width, height = gtc.getimage(acc, style='extended')
            if not imgstr:
                print "Extended Image: None"
            else:
                print "Extended Image: %s (%sx%s)" % (bool(imgstr), width, height,)
            imgstr, width, height = gtc.getimage(acc, style='normal')
            if not imgstr:
                print "Normal Image: None"
            else:
                print "Normal Image: %s (%sx%s)" % (bool(imgstr), width, height,)
            imgstr, width, height = gtc.getimage(acc, style='compact')
            if not imgstr:
                print "Compacct Image: None"
            else:
                print "Compact Image: %s (%sx%s)" % (bool(imgstr), width, height,)
            print "References: %s" % (", ".join(gtc.getrefs(acc), ))
            print "HasPage:", gtc.haspage(acc)

    elif cmd.lower() == "motifs":

        gtc = GlyTouCan()
        for acc, label, redend in gtc.allmotifs():
            print acc, label, redend

    elif cmd.lower() == "references":

        gtc = GlyTouCan()
        for acc, pubmed in gtc.allrefs():
            print acc, pubmed

    elif cmd.lower() == "taxa":

        gtc = GlyTouCan()
        for acc, taxid in gtc.alltaxa():
            print acc, taxid

    elif cmd.lower() == "bytaxa":

        gtc = GlyTouCan()
        for acc, taxid in gtc.alltaxa():
            if taxid == sys.argv[1]:
                print acc

    elif cmd.lower() == "kegg":

        gtc = GlyTouCan()
        for acc, keggacc in gtc.allcrossrefs('kegg'):
            print acc, keggacc

    elif cmd.lower() == "xref":

        gtc = GlyTouCan()
        for acc, xref in gtc.allcrossrefs():
            res, id = xref.split(':', 1)
            print acc, res, id

    elif cmd.lower() == "fully_determined":

        gtc = GlyTouCan()
        for acc in items():
            g = gtc.getGlycan(acc)
            print acc, g.fully_determined()

    elif cmd.lower() == "composition":

        gtc = GlyTouCan()
        for acc in items():
            g = gtc.getGlycan(acc)
            print acc, " ".join(
                map(lambda t: "%s: %d" % t, filter(lambda t: t[1] > 0, sorted(g.iupac_composition().items()))))

    elif cmd.lower() == "getglycan":

        gtc = GlyTouCan()
        for acc in items():
            g = gtc.getGlycan(acc)
            if g:
                print acc
                print g.glycoct()
                for m in g.all_nodes():
                    print m
                print g.underivitized_molecular_weight()
                print g.permethylated_molecular_weight()

    elif cmd.lower() == "substructure":

        gtc = GlyTouCan()
        for acc in items():
            for ss in gtc.getsubstr(acc):
                print "\t".join([acc, ss])

    elif cmd.lower() == "allmass":

        gtc = GlyTouCan(usecache=True)
        for s, m in gtc.allmass():
            print "\t".join(map(str, [s, m]))

    elif cmd.lower() == "allcomp":

        gtc = GlyTouCan()
        for s, c in gtc.allcomp():
            print "\t".join([s, c])

    elif cmd.lower() == "alltopo":

        gtc = GlyTouCan()
        for s, t in gtc.alltopo():
            print "\t".join([s, t])

    elif cmd.lower() == "makecomp4uckb":

        gtc = GlyTouCan()
        skels = dict(Hex="uxxxxh",
                     HexNAc="uxxxxh_2*NCC/3=O",
                     dHex="uxxxxm",
                     NeuAc="AUd21122h_5*NCC/3=O",
                     NeuGc="AUd21122h_5*NCCO/3=O")
        for compstr in sys.argv[1:]:
            vals = re.split(r'(\d+)', compstr)
            comp = dict()
            for i in range(0, len(vals) - 1, 2):
                key, cnt = vals[i], int(vals[i + 1])
                if cnt > 0:
                    comp[skels[key]] = cnt
            skellist = list(comp)
            total = sum(comp.values())
            uniq = len(skellist)
            wurcsseq = "WURCS=2.0/%s,%s,%s/" % (uniq, total, "0+")
            wurcsseq += "".join(map(lambda sk: "[%s]" % (sk,), skellist)) + "/"
            inds = []
            for i, sk in enumerate(skellist):
                inds.extend([str(i + 1)] * comp[sk])
            wurcsseq += "-".join(inds)
            wurcsseq += "/"
            acc, new = gtc.register(gtc.fixcompwurcs(wurcsseq))
            print acc, compstr

    else:
        print >> sys.stderr, "Bad command: %s" % (cmd,)
        sys.exit(1)
